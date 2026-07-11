"""Prefix-command parser for the MeshCore BBS.

Users interact via DMs containing prefix commands (!help, !post, ...).
This module is deliberately pure: it depends only on BBSStore, not on
MeshCore or the config, so it can be unit-tested without any hardware.

The BBS is pull-based: !post only stores a message, and other users see it
when they themselves send !read. Likewise !msg queues a private message
that the recipient pulls with !inbox. Consequently every command only ever
produces a reply to the sender — nothing is pushed to other users.

Rooms are NOT created here. They are provided via config.yaml and synced
into the store at startup by bbs.py; !join only ever joins an existing room.
"""

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from bbs.messages import Messages
from bbs.solar import SolarProvider
from bbs.store import BBSStore
from bbs.weather import WeatherProvider

from .util import fmt_ago

_LOGGER = logging.getLogger(__name__)

# Fallback defaults — overridden by config values passed to CommandRouter.
_DEFAULT_MAX_LEN = 150
_DEFAULT_USER_LIST_LIMIT = 5
_DEFAULT_READ_LIMIT = 5
_DEFAULT_UNDO_WINDOW = 600  # seconds a post stays !undo-able (0 = no time limit)  # posts per !read without an explicit number (0 = unlimited)
_DEFAULT_RATE_LIMIT = 10    # commands per user per minute (0 = no limit)
_RATE_WINDOW = 60.0         # sliding-window length in seconds

# Commands that are only available when listed in config bbs.features.commands.
_OPTIONAL_COMMANDS: dict[str, str] = {
    "weather": "!weather (location) — current weather",
    "ping":    "!ping — signal quality",
    "solar":   "!solar — solar and HF band conditions",
}


@dataclass
class CommandResult:
    """A command's reply, plus optional callbacks.

    `messages` are sent to the sender in order. `on_delivered`, if set, is
    invoked by bbs.py only after ALL messages were sent successfully — used
    by !read/!inbox to advance the seen/delivered state, so a failed radio
    send doesn't silently drop messages the user never received.

    `inbox_notify_pubkey`, if set, is the pubkey of another user who should
    be notified immediately that they have a new inbox message.
    """
    messages: list[str] = field(default_factory=list)
    on_delivered: Callable[[], None] | None = None
    inbox_notify_pubkey: str | None = None


class CommandRouter:
    def __init__(
        self,
        store: BBSStore,
        max_message_length: int = _DEFAULT_MAX_LEN,
        user_list_limit: int = _DEFAULT_USER_LIST_LIMIT,
        read_limit: int = _DEFAULT_READ_LIMIT,
        undo_window: int = _DEFAULT_UNDO_WINDOW,
        rate_limit: int = _DEFAULT_RATE_LIMIT,
        messages: Messages | None = None,
        weather_provider: WeatherProvider | None = None,
        weather_location: str = "",
        solar_provider: SolarProvider | None = None,
        additional_commands: list[str] | None = None,
    ) -> None:
        self._store = store
        self._max_len = max_message_length
        self._user_list_limit = user_list_limit
        self._read_limit = read_limit
        self._undo_window = undo_window
        self._rate_limit = rate_limit
        # Rate-limit state is deliberately in-memory, not in the store —
        # it is ephemeral protection state and may reset on restart.
        self._rate_events: dict[str, deque[float]] = {}
        self._rate_warned: set[str] = set()
        self._t = (messages or Messages()).t
        self._weather_provider = weather_provider
        self._weather_location = weather_location
        self._solar_provider = solar_provider
        self._additional_commands: frozenset[str] = frozenset(additional_commands or [])

    async def handle(
        self, pubkey: str, name: str, text: str, signal_info: dict | None = None
    ) -> CommandResult:
        """Parse and dispatch a single incoming DM from `pubkey`/`name`."""
        # Airtime guard: check the per-user rate limit before doing ANY work
        # (even the upsert) — a limited sender gets one warning, then silence.
        limited = self._check_rate_limit(pubkey)
        if limited is not None:
            return limited

        # Record/refresh the sender so they can be addressed by name (!msg)
        # and have per-user state (current room, seen posts).
        self._store.upsert_user(pubkey, name)

        text = (text or "").strip()
        if not text.startswith("!"):
            return CommandResult([self._t("Send !help for a list of commands.")])

        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        handler = self._COMMANDS.get(cmd)
        if handler is None:
            return CommandResult([self._t("Unknown command '!{cmd}'. Send !help.", cmd=cmd)])
        if cmd in _OPTIONAL_COMMANDS and cmd not in self._additional_commands:
            return CommandResult([self._t("Unknown command '!{cmd}'. Send !help.", cmd=cmd)])

        # Pass signal_info explicitly to the one handler that needs it, so
        # it is never stored as an instance attribute (which would be unsafe
        # if handle() were ever called concurrently).
        kwargs = {"signal_info": signal_info} if cmd == "ping" else {}
        result = handler(self, pubkey, name, arg, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        assert isinstance(result, CommandResult)  # handlers return CommandResult
        return result

    # --- Command implementations ----------------------------------------

    def _cmd_help(self, pubkey: str, name: str, arg: str) -> CommandResult:
        lines = [self._t(line) for line in [
            "Commands:",
            "!rooms — list rooms",
            "!join <room> — enter a room",
            "!leave — leave current room",
            "!post <text> — post to current room",
            "!read (n) — read new posts",
            "!search <text> — search posts",
            "!undo — remove your last post",
            "!msg [name] <text> — private message",
            "!inbox — read private messages",
            "!reply <text> — answer your last inbox message",
            "!who — members of current room",
            "!users — recent users",
            "!seen <name> — last activity of a user",
            "!whoami — your name",
            "!whereami or !pwd — current room",
            "!stats — user and post counts",
        ]]
        for cmd, description in _OPTIONAL_COMMANDS.items():
            if cmd in self._additional_commands:
                lines.append(self._t(description))
        return CommandResult(self._chunk(lines))

    def _cmd_rooms(self, pubkey: str, name: str, arg: str) -> CommandResult:
        rooms = self._store.list_rooms_with_stats()
        if not rooms:
            return CommandResult([self._t("No rooms available.")])
        now = int(time.time())
        lines = [self._t("Rooms:")]
        for r in rooms:
            n = r["member_count"]
            members = self._t("{n} members" if n != 1 else "{n} member", n=n)
            if r["last_post_at"]:
                lines.append(self._t(
                    "{room} ({members}, {ago} ago)",
                    room=r["name"], members=members, ago=fmt_ago(now - r["last_post_at"]),
                ))
            else:
                lines.append(f"{r['name']} ({members})")
        return CommandResult(self._chunk(lines))

    def _cmd_join(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = arg.strip()
        if not room:
            return CommandResult([self._t("Usage: !join <room>")])
        if not self._store.room_exists(room):
            return CommandResult([self._t("Room '{room}' does not exist. Send !rooms.", room=room)])
        self._store.join_room(pubkey, room)
        self._store.set_current_room(pubkey, room)
        return CommandResult([self._t("Joined '{room}'. !read for new posts, !post <text> to write.", room=room)])

    def _cmd_leave(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult([self._t("You are not in a room.")])
        self._store.leave_room(pubkey, room)
        self._store.set_current_room(pubkey, None)
        return CommandResult([self._t("Left '{room}'.", room=room)])

    def _cmd_post(self, pubkey: str, name: str, arg: str) -> CommandResult:
        body = arg.strip()
        if not body:
            return CommandResult([self._t("Usage: !post <text>")])
        room = self._current_room(pubkey)
        if not room:
            return CommandResult([self._t("Join a room first: !join <room>")])
        self._store.add_post(room, pubkey, name, body)
        self._store.update_room_activity(pubkey, room)
        return CommandResult([self._t("Posted to '{room}'.", room=room)])

    def _cmd_read(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult([self._t("Join a room first: !join <room>")])

        # Airtime guard: without an explicit number, cap at read_limit so a
        # user returning after weeks doesn't trigger dozens of LoRa messages
        # in one go (0 = unlimited). An explicit "!read <n>" is the user's
        # own call and overrides the default.
        limit: int | None = self._read_limit or None
        if arg.strip():
            try:
                limit = max(1, int(arg.strip()))
            except ValueError:
                return CommandResult([self._t("Usage: !read or !read <number>")])

        self._store.update_room_activity(pubkey, room)
        posts = self._store.unseen_posts(pubkey, room, limit=limit)
        if not posts:
            return CommandResult([self._t("No new posts in '{room}'.", room=room)])

        now = int(time.time())
        lines = [f"{p['author_name']} {fmt_ago(now - p['created_at'])}: {p['text']}" for p in posts]
        remaining = self._store.count_unseen_posts(pubkey, room) - len(posts)
        if remaining > 0:
            lines.append(self._t("+{remaining} more — send !read again", remaining=remaining))
        last_id = posts[-1]["id"]

        def commit() -> None:
            self._store.mark_room_seen(pubkey, room, last_id)

        return CommandResult(self._chunk(lines), on_delivered=commit)

    def _cmd_undo(self, pubkey: str, name: str, arg: str) -> CommandResult:
        """Soft-delete the caller's newest post. Repeatable: each call
        removes the next-newest remaining post inside the window. Copies
        already delivered to other readers are gone from the air — undo
        only stops FUTURE delivery, which the reply wording reflects."""
        post = self._store.last_post_by(pubkey)
        if post is None:
            return CommandResult([self._t("Nothing to undo — you have no posts.")])
        age = int(time.time()) - post["created_at"]
        if self._undo_window and age > self._undo_window:
            return CommandResult(
                [self._t("Too late — !undo works within {minutes}m of posting.", minutes=self._undo_window // 60)]
            )
        self._store.delete_post(post["id"])
        text = post["text"]
        snippet = text if len(text) <= 30 else text[:29] + "…"
        return CommandResult([self._t("Removed your post in '{room}': {snippet}", room=post["room"], snippet=snippet)])

    # Recipient for !msg: either a bracket-wrapped name that may contain
    # spaces/emoji — [Peter Bosch] or the MeshCore mention form @[Peter Bosch]
    # (the @ is optional) — or, for convenience, a single bare word with no
    # spaces. The rest of the line is the message body. User-facing help/usage
    # text deliberately shows the plain [name] form, because the MeshCore
    # client renders a literal "@[" as a mention and would mangle it.
    # A target that can be a pubkey prefix: at least 4 hex chars, so short
    # words like "abc" or "ed" stay ordinary names.
    _PUBKEY_PREFIX = re.compile(r"^[0-9a-fA-F]{4,64}$")

    _MSG_TARGET = re.compile(r"^\s*(?:@?\[(?P<wrapped>[^\]]+)\]|(?P<bare>\S+))\s+(?P<body>.+)$", re.DOTALL)

    def _cmd_msg(self, pubkey: str, name: str, arg: str) -> CommandResult:
        m = self._MSG_TARGET.match(arg)
        if m is None:
            if "[" in arg:
                return CommandResult([self._t("Usage: !msg [name] <text>  (missing message text?)")])
            return CommandResult([self._t("Usage: !msg [name] <text>  (brackets required if the name has spaces)")])

        target_name = (m.group("wrapped") or m.group("bare")).strip()
        body = m.group("body").strip()

        # A leftover bracket in the resolved name means the user typed an
        # unterminated/incomplete bracket group (e.g. "[Peter Bosch]" with no
        # text, which the bare-word branch mis-splits into name="[Peter"
        # body="Bosch]"). Treat that as a usage error rather than a bogus
        # lookup for a name nobody has.
        if "[" in target_name or "]" in target_name:
            return CommandResult([self._t("Usage: !msg [name] <text>  (check the [ ] brackets and message text)")])

        if not body:
            return CommandResult([self._t("Usage: !msg [name] <text>")])

        target, error_lines = self._resolve_msg_target(target_name)
        if target is None:
            return CommandResult(self._chunk(error_lines))
        if target["pubkey"] == pubkey:
            return CommandResult([self._t("You cannot send a message to yourself.")])

        return self._queue_pm(pubkey, name, target, body)

    def _resolve_msg_target(self, token: str, hint: str = "Send: !msg <keyprefix> <text>"):
        """Resolve a !msg/!seen target to a single user.

        Resolution order: exact name -> pubkey prefix (if the token is
        >=4 hex chars) -> name prefix. An exact name always wins, so a user
        who happens to be called "abcd" is not shadowed by key prefixes.
        Returns (user_row, None) on success or (None, error_lines); on
        ambiguity the error lists the candidates with their key prefixes,
        teaching the unambiguous addressing form via `hint`."""
        exact = self._store.find_users_by_name(token)
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, self._ambiguous_lines(token, exact, hint)

        if self._PUBKEY_PREFIX.match(token):
            by_key = self._store.find_users_by_pubkey_prefix(token.lower())
            if len(by_key) == 1:
                return by_key[0], None
            if len(by_key) > 1:
                return None, self._ambiguous_lines(token, by_key, hint)

        by_prefix = self._store.find_users_by_name_prefix(token)
        if len(by_prefix) == 1:
            return by_prefix[0], None
        if len(by_prefix) > 1:
            return None, self._ambiguous_lines(token, by_prefix, hint)

        return None, [self._t("No user '{token}' known. Try !users.", token=token)]

    def _ambiguous_lines(self, token: str, candidates: list, hint: str) -> list[str]:
        lines = [self._t("'{token}' is ambiguous — pick a key prefix:", token=token)]
        lines += [f"[{u['name']}] {u['pubkey'][:8]}" for u in candidates[:5]]
        lines.append(self._t(hint))
        return lines

    def _queue_pm(self, pubkey: str, name: str, target, body: str) -> CommandResult:
        """Shared tail of !msg and !reply: queue the PM and request an
        immediate inbox notification for the recipient."""
        self._store.add_private_message(pubkey, name, target["pubkey"], body)
        return CommandResult(
            [self._t("Message queued for {name}.", name=target["name"])],
            inbox_notify_pubkey=target["pubkey"],
        )

    def _cmd_reply(self, pubkey: str, name: str, arg: str) -> CommandResult:
        body = arg.strip()
        if not body:
            return CommandResult([self._t("Usage: !reply <text>")])
        user = self._store.get_user(pubkey)
        last_from = user["last_pm_from"] if user else None
        if not last_from:
            return CommandResult([self._t("No one to reply to yet — read your !inbox first.")])
        target = self._store.get_user(last_from)
        if target is None:
            return CommandResult([self._t("That user is no longer known to the BBS.")])
        return self._queue_pm(pubkey, name, target, body)

    def _cmd_inbox(self, pubkey: str, name: str, arg: str) -> CommandResult:
        pms = self._store.undelivered_private(pubkey)
        if not pms:
            return CommandResult([self._t("No new messages.")])

        now = int(time.time())
        lines = [f"{m['sender_name']} {fmt_ago(now - m['created_at'])}: {m['text']}" for m in pms]
        ids = [m["id"] for m in pms]
        last_sender = pms[-1]["sender"]

        def commit() -> None:
            for mid in ids:
                self._store.mark_private_delivered(mid)
            # The newest delivered message defines the !reply target — set
            # only after successful delivery, like the delivered flags.
            self._store.set_last_pm_from(pubkey, last_sender)

        return CommandResult(self._chunk(lines), on_delivered=commit)

    # The whole argument of !seen is the target name, so spaces work even
    # without brackets; the @?[...] wrapper is still accepted so a name can
    # be pasted in the same form !users and !who display it.
    _BRACKETED_NAME = re.compile(r"^@?\[(?P<wrapped>[^\]]+)\]$")

    def _cmd_seen(self, pubkey: str, name: str, arg: str) -> CommandResult:
        token = arg.strip()
        m = self._BRACKETED_NAME.match(token)
        if m:
            token = m.group("wrapped").strip()
        if not token:
            return CommandResult([self._t("Usage: !seen <name>")])
        target, error_lines = self._resolve_msg_target(token, hint="Send: !seen <keyprefix>")
        if target is None:
            return CommandResult(self._chunk(error_lines))
        ago = fmt_ago(int(time.time()) - target["last_seen"])
        return CommandResult(
            [self._t("[{name}] was last active {ago} ago.", name=target["name"], ago=ago)]
        )

    def _cmd_search(self, pubkey: str, name: str, arg: str) -> CommandResult:
        """Search old posts in the current room. Deliberately NOT room
        activity and does not touch the seen-marker — searching is browsing
        history, not reading new posts."""
        term = arg.strip()
        if not term:
            return CommandResult([self._t("Usage: !search <text>")])
        if len(term) < 2:
            return CommandResult([self._t("Search term too short — use at least 2 characters.")])
        room = self._current_room(pubkey)
        if not room:
            return CommandResult([self._t("Join a room first: !join <room>")])

        # Same airtime guard as !read: cap the newest matches at read_limit
        # (0 = unlimited); the user narrows the term instead of paging.
        limit: int | None = self._read_limit or None
        posts = self._store.search_posts(room, term, limit=limit)
        if not posts:
            return CommandResult(
                [self._t("No posts matching '{term}' in '{room}'.", term=term, room=room)]
            )

        now = int(time.time())
        lines = [f"{p['author_name']} {fmt_ago(now - p['created_at'])}: {p['text']}" for p in posts]
        remaining = self._store.count_search_posts(room, term) - len(posts)
        if remaining > 0:
            lines.append(self._t("+{remaining} more — refine your search", remaining=remaining))
        return CommandResult(self._chunk(lines))

    def _cmd_who(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult([self._t("You are not in a room. Use !join <room>.")])
        members = self._store.room_members(room)
        if not members:
            return CommandResult([self._t("No members in '{room}'.", room=room)])
        now = int(time.time())
        lines = [self._t("'{room}' members:", room=room)] + [
            f"[{m['name']}] {fmt_ago(now - m['last_activity']) if m['last_activity'] else '—'}"
            for m in members
        ]
        return CommandResult(self._chunk(lines))

    def _cmd_users(self, pubkey: str, name: str, arg: str) -> CommandResult:
        users = self._store.list_recent_users(limit=self._user_list_limit, exclude_pubkey=pubkey)
        if not users:
            return CommandResult([self._t("No other users known yet.")])
        now = int(time.time())
        # The key prefix makes every user addressable via "!msg <prefix>",
        # even when the display name is hard to type (emoji) or duplicated.
        lines = [self._t("Recent users:")] + [
            f"[{u['name']}] {u['pubkey'][:8]} {fmt_ago(now - u['last_seen'])}"
            for u in users
        ]
        return CommandResult(self._chunk(lines))

    def _cmd_whoami(self, pubkey: str, name: str, arg: str) -> CommandResult:
        # handle() already upserted the user, so get_user() is populated;
        # fall back to the live name just in case.
        user = self._store.get_user(pubkey)
        known = user["name"] if user else name
        return CommandResult([self._t("You are known as [{name}].", name=known)])

    def _cmd_whereami(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult([self._t("You are not in any room. Use !join <room>.")])
        unread = self._store.count_unseen_posts(pubkey, room)
        if unread:
            suffix = self._t(" {n} unread posts." if unread != 1 else " {n} unread post.", n=unread)
        else:
            suffix = self._t(" No unread posts.")
        return CommandResult([self._t("You are in room '{room}'.", room=room) + suffix])

    def _cmd_stats(self, pubkey: str, name: str, arg: str) -> CommandResult:
        s = self._store.get_stats()
        return CommandResult([
            self._t(
                "Stats: {users}, {posts}, {rooms}.",
                users=self._t("{n} users" if s["users"] != 1 else "{n} user", n=s["users"]),
                posts=self._t("{n} posts" if s["posts"] != 1 else "{n} post", n=s["posts"]),
                rooms=self._t("{n} rooms" if s["rooms"] != 1 else "{n} room", n=s["rooms"]),
            )
        ])

    def _cmd_ping(self, pubkey: str, name: str, arg: str, signal_info: dict | None = None) -> CommandResult:
        info = signal_info
        if info is None:
            return CommandResult([self._t("No signal data available.")])
        snr = info.get("snr", "?")
        rssi = info.get("rssi", "?")
        hops = info.get("hops", 0)
        path = info.get("path", [])
        path_str = " → ".join(path) if path else self._t("direct")
        lines = [
            f"SNR: {snr} dB  RSSI: {rssi} dBm",
            f"Hops: {hops}  Path: {path_str}",
        ]
        # Trend over the last 24h — only with more than one sample (the
        # current packet alone is already shown above, not a trend).
        stats = self._store.signal_stats(pubkey)
        if stats and stats["count"] > 1:
            lines.append(self._t(
                "24h: avg SNR {snr} dB, {rssi} dBm ({n} packets)",
                snr=round(stats["snr"], 1), rssi=round(stats["rssi"]), n=stats["count"],
            ))
        return CommandResult(self._chunk(lines))

    async def _cmd_weather(self, pubkey: str, name: str, arg: str) -> CommandResult:
        location = arg.strip() or self._weather_location
        if not location:
            return CommandResult([self._t("Usage: !weather <location>")])
        if self._weather_provider is None:
            return CommandResult([self._t("Weather is not configured.")])
        text = await self._weather_provider.fetch(location)
        return CommandResult(self._chunk([text]))

    async def _cmd_solar(self, pubkey: str, name: str, arg: str) -> CommandResult:
        if self._solar_provider is None:
            return CommandResult([self._t("Solar data is not configured.")])
        text = await self._solar_provider.fetch()
        return CommandResult(self._chunk(text.splitlines()))

    # --- Helpers ---------------------------------------------------------

    def _check_rate_limit(self, pubkey: str) -> CommandResult | None:
        """Sliding-window rate limit (_RATE_WINDOW seconds).

        Returns None while the sender is within the limit. The first message
        over the limit gets a warning; everything after that is dropped with
        an EMPTY result — replying to every excess message would burn the
        very airtime the limit protects. Once the window has room again,
        service (and a future warning) resumes."""
        if self._rate_limit <= 0:
            return None
        now = time.monotonic()
        events = self._rate_events.setdefault(pubkey, deque())
        while events and now - events[0] > _RATE_WINDOW:
            events.popleft()
        if len(events) >= self._rate_limit:
            if pubkey in self._rate_warned:
                return CommandResult([])
            self._rate_warned.add(pubkey)
            _LOGGER.info(f"Rate limit hit by {pubkey[:8]} — warned, now muting.")
            return CommandResult([self._t("Too many commands — wait a minute.")])
        events.append(now)
        self._rate_warned.discard(pubkey)
        return None

    def _current_room(self, pubkey: str) -> str | None:
        user = self._store.get_user(pubkey)
        return user["current_room"] if user else None

    def _chunk(self, lines: list[str]) -> list[str]:
        """Pack lines into as few messages as possible without exceeding the
        per-message limit in UTF-8 BYTES (the device limit is bytes, not
        characters — umlauts are 2 bytes, emoji up to 4). Lines are joined
        with newlines; a single line longer than the limit is hard-truncated
        with an ellipsis."""
        messages: list[str] = []
        current = ""
        current_bytes = 0

        for line in lines:
            line_bytes = self._blen(line)

            if line_bytes > self._max_len:
                if current:
                    messages.append(current)
                    current, current_bytes = "", 0
                if self._max_len > 3:
                    messages.append(self._btrunc(line, self._max_len - 3) + "...")
                else:
                    messages.append(self._btrunc(line, self._max_len))
                continue

            if not current:
                current, current_bytes = line, line_bytes
                continue

            joined_bytes = current_bytes + 1 + line_bytes  # +1 for the "\n"
            if joined_bytes > self._max_len:
                messages.append(current)
                current, current_bytes = line, line_bytes
            else:
                current = f"{current}\n{line}"
                current_bytes = joined_bytes

        if current:
            messages.append(current)
        return messages

    @staticmethod
    def _blen(s: str) -> int:
        """UTF-8 byte length of a string."""
        return len(s.encode("utf-8"))

    @staticmethod
    def _btrunc(s: str, max_bytes: int) -> str:
        """Truncate to at most max_bytes UTF-8 bytes without splitting a
        multibyte character."""
        encoded = s.encode("utf-8")
        if len(encoded) <= max_bytes:
            return s
        # errors="ignore" silently drops a partial trailing sequence, which
        # is exactly the behaviour we want at a byte cut point.
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    _COMMANDS = {
        "help": _cmd_help,
        "rooms": _cmd_rooms,
        "join": _cmd_join,
        "leave": _cmd_leave,
        "post": _cmd_post,
        "read": _cmd_read,
        "search": _cmd_search,
        "undo": _cmd_undo,
        "msg": _cmd_msg,
        "inbox": _cmd_inbox,
        "reply": _cmd_reply,
        "who": _cmd_who,
        "users": _cmd_users,
        "seen": _cmd_seen,
        "whoami": _cmd_whoami,
        "whereami": _cmd_whereami,
        "pwd": _cmd_whereami,
        "stats": _cmd_stats,
        "weather": _cmd_weather,
        "ping": _cmd_ping,
        "solar": _cmd_solar,
    }
