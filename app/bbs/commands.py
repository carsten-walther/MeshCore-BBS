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
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field

from bbs.store import BBSStore
from bbs.weather import WeatherProvider

_LOGGER = logging.getLogger(__name__)

# Fallback defaults — overridden by config values passed to CommandRouter.
_DEFAULT_MAX_LEN = 150
_DEFAULT_USER_LIST_LIMIT = 5

# Commands that are only available when listed in config bbs.features.commands.
_OPTIONAL_COMMANDS: dict[str, str] = {
    "weather": "!weather (location) — current weather",
    "ping":    "!ping — signal quality",
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
        weather_provider: WeatherProvider | None = None,
        weather_location: str = "",
        advert_callback: Callable[[], Coroutine] | None = None,
        advert_channels_callback: Callable[[], Coroutine] | None = None,
        restart_callback: Callable[[], Coroutine] | None = None,
        admin_pubkeys: list[str] | None = None,
        additional_commands: list[str] | None = None,
    ) -> None:
        self._store = store
        self._max_len = max_message_length
        self._user_list_limit = user_list_limit
        self._weather_provider = weather_provider
        self._weather_location = weather_location
        self._advert_callback = advert_callback
        self._advert_channels_callback = advert_channels_callback
        self._restart_callback = restart_callback
        self._admin_pubkeys = admin_pubkeys or []
        self._additional_commands: frozenset[str] = frozenset(additional_commands or [])

    async def handle(
        self, pubkey: str, name: str, text: str, signal_info: dict | None = None
    ) -> CommandResult:
        """Parse and dispatch a single incoming DM from `pubkey`/`name`."""
        # Record/refresh the sender so they can be addressed by name (!msg)
        # and have per-user state (current room, seen posts).
        self._store.upsert_user(pubkey, name)

        text = (text or "").strip()
        if not text.startswith("!"):
            return CommandResult(["Send !help for a list of commands."])

        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        handler = self._COMMANDS.get(cmd)
        if handler is None:
            return CommandResult([f"Unknown command '!{cmd}'. Send !help."])
        if cmd in _OPTIONAL_COMMANDS and cmd not in self._additional_commands:
            return CommandResult([f"Unknown command '!{cmd}'. Send !help."])

        # Pass signal_info explicitly to the one handler that needs it, so
        # it is never stored as an instance attribute (which would be unsafe
        # if handle() were ever called concurrently).
        kwargs = {"signal_info": signal_info} if cmd == "ping" else {}
        result = handler(self, pubkey, name, arg, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    # --- Command implementations ----------------------------------------

    def _cmd_help(self, pubkey: str, name: str, arg: str) -> CommandResult:
        lines = [
            "Commands:",
            "!rooms — list rooms",
            "!join <room> — enter a room",
            "!leave — leave current room",
            "!post <text> — post to current room",
            "!read (n) — read new posts",
            "!msg [name] <text> — private message",
            "!inbox — read private messages",
            "!who — members of current room",
            "!users — recent users",
            "!whoami — your name",
            "!whereami or !pwd — current room",
            "!stats — user and post counts",
        ]
        for cmd, description in _OPTIONAL_COMMANDS.items():
            if cmd in self._additional_commands:
                lines.append(description)
        return CommandResult(self._chunk(lines))

    def _cmd_rooms(self, pubkey: str, name: str, arg: str) -> CommandResult:
        rooms = self._store.list_rooms_with_stats()
        if not rooms:
            return CommandResult(["No rooms available."])
        now = int(time.time())
        lines = ["Rooms:"]
        for r in rooms:
            n = r["member_count"]
            members = f"{n} member{'s' if n != 1 else ''}"
            if r["last_post_at"]:
                lines.append(f"{r['name']} ({members}, {self._fmt_ago(now - r['last_post_at'])} ago)")
            else:
                lines.append(f"{r['name']} ({members})")
        return CommandResult(self._chunk(lines))

    def _cmd_join(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = arg.strip()
        if not room:
            return CommandResult(["Usage: !join <room>"])
        if not self._store.room_exists(room):
            return CommandResult([f"Room '{room}' does not exist. Send !rooms."])
        self._store.join_room(pubkey, room)
        self._store.set_current_room(pubkey, room)
        return CommandResult([f"Joined '{room}'. !read for new posts, !post <text> to write."])

    def _cmd_leave(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult(["You are not in a room."])
        self._store.leave_room(pubkey, room)
        self._store.set_current_room(pubkey, None)
        return CommandResult([f"Left '{room}'."])

    def _cmd_post(self, pubkey: str, name: str, arg: str) -> CommandResult:
        body = arg.strip()
        if not body:
            return CommandResult(["Usage: !post <text>"])
        room = self._current_room(pubkey)
        if not room:
            return CommandResult(["Join a room first: !join <room>"])
        self._store.add_post(room, pubkey, name, body)
        self._store.update_room_activity(pubkey, room)
        return CommandResult([f"Posted to '{room}'."])

    def _cmd_read(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult(["Join a room first: !join <room>"])

        limit: int | None = None
        if arg.strip():
            try:
                limit = max(1, int(arg.strip()))
            except ValueError:
                return CommandResult(["Usage: !read or !read <number>"])

        self._store.update_room_activity(pubkey, room)
        posts = self._store.unseen_posts(pubkey, room, limit=limit)
        if not posts:
            return CommandResult([f"No new posts in '{room}'."])

        now = int(time.time())
        lines = [f"{p['author_name']} {self._fmt_ago(now - p['created_at'])}: {p['text']}" for p in posts]
        last_id = posts[-1]["id"]

        def commit() -> None:
            self._store.mark_room_seen(pubkey, room, last_id)

        return CommandResult(self._chunk(lines), on_delivered=commit)

    # Recipient for !msg: either a bracket-wrapped name that may contain
    # spaces/emoji — [Peter Bosch] or the MeshCore mention form @[Peter Bosch]
    # (the @ is optional) — or, for convenience, a single bare word with no
    # spaces. The rest of the line is the message body. User-facing help/usage
    # text deliberately shows the plain [name] form, because the MeshCore
    # client renders a literal "@[" as a mention and would mangle it.
    _MSG_TARGET = re.compile(r"^\s*(?:@?\[(?P<wrapped>[^\]]+)\]|(?P<bare>\S+))\s+(?P<body>.+)$", re.DOTALL)

    def _cmd_msg(self, pubkey: str, name: str, arg: str) -> CommandResult:
        m = self._MSG_TARGET.match(arg)
        if m is None:
            if "[" in arg:
                return CommandResult(['Usage: !msg [name] <text>  (missing message text?)'])
            return CommandResult(['Usage: !msg [name] <text>  (brackets required if the name has spaces)'])

        target_name = (m.group("wrapped") or m.group("bare")).strip()
        body = m.group("body").strip()

        # A leftover bracket in the resolved name means the user typed an
        # unterminated/incomplete bracket group (e.g. "[Peter Bosch]" with no
        # text, which the bare-word branch mis-splits into name="[Peter"
        # body="Bosch]"). Treat that as a usage error rather than a bogus
        # lookup for a name nobody has.
        if "[" in target_name or "]" in target_name:
            return CommandResult(['Usage: !msg [name] <text>  (check the [ ] brackets and message text)'])

        if not body:
            return CommandResult(['Usage: !msg [name] <text>'])

        target = self._store.find_user_by_name(target_name)
        if target is None:
            return CommandResult([f"Unknown or ambiguous user '{target_name}'."])
        if target["pubkey"] == pubkey:
            return CommandResult(["You cannot send a message to yourself."])

        self._store.add_private_message(pubkey, name, target["pubkey"], body)
        return CommandResult(
            [f"Message queued for {target['name']}."],
            inbox_notify_pubkey=target["pubkey"],
        )

    def _cmd_inbox(self, pubkey: str, name: str, arg: str) -> CommandResult:
        pms = self._store.undelivered_private(pubkey)
        if not pms:
            return CommandResult(["No new messages."])

        now = int(time.time())
        lines = [f"{m['sender_name']} {self._fmt_ago(now - m['created_at'])}: {m['text']}" for m in pms]
        ids = [m["id"] for m in pms]

        def commit() -> None:
            for mid in ids:
                self._store.mark_private_delivered(mid)

        return CommandResult(self._chunk(lines), on_delivered=commit)

    def _cmd_who(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult(["You are not in a room. Use !join <room>."])
        members = self._store.room_members(room)
        if not members:
            return CommandResult([f"No members in '{room}'."])
        now = int(time.time())
        lines = [f"'{room}' members:"] + [
            f"[{m['name']}] {self._fmt_ago(now - m['last_activity']) if m['last_activity'] else '—'}"
            for m in members
        ]
        return CommandResult(self._chunk(lines))

    def _cmd_users(self, pubkey: str, name: str, arg: str) -> CommandResult:
        users = self._store.list_recent_users(limit=self._user_list_limit, exclude_pubkey=pubkey)
        if not users:
            return CommandResult(["No other users known yet."])
        now = int(time.time())
        lines = ["Recent users:"] + [
            f"[{u['name']}] {self._fmt_ago(now - u['last_seen'])}"
            for u in users
        ]
        return CommandResult(self._chunk(lines))

    def _cmd_whoami(self, pubkey: str, name: str, arg: str) -> CommandResult:
        # handle() already upserted the user, so get_user() is populated;
        # fall back to the live name just in case.
        user = self._store.get_user(pubkey)
        known = user["name"] if user else name
        return CommandResult([f"You are known as [{known}]."])

    def _cmd_whereami(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if not room:
            return CommandResult(["You are not in any room. Use !join <room>."])
        unread = self._store.count_unseen_posts(pubkey, room)
        suffix = f" {unread} unread post{'s' if unread != 1 else ''}." if unread else " No unread posts."
        return CommandResult([f"You are in room '{room}'.{suffix}"])

    def _cmd_stats(self, pubkey: str, name: str, arg: str) -> CommandResult:
        s = self._store.get_stats()
        return CommandResult([
            f"Stats: {s['users']} user{'s' if s['users'] != 1 else ''}, "
            f"{s['posts']} post{'s' if s['posts'] != 1 else ''}, "
            f"{s['rooms']} room{'s' if s['rooms'] != 1 else ''}."
        ])

    async def _cmd_restart(self, pubkey: str, name: str, arg: str) -> CommandResult:
        if not self._is_admin(pubkey):
            return CommandResult(["Unknown command '!restart'. Send !help."])
        if self._restart_callback is None:
            return CommandResult(["Restart not available."])
        await self._restart_callback()
        return CommandResult(["Restarting..."])

    def _cmd_ping(self, pubkey: str, name: str, arg: str, signal_info: dict | None = None) -> CommandResult:
        info = signal_info
        if info is None:
            return CommandResult(["No signal data available."])
        snr = info.get("snr", "?")
        rssi = info.get("rssi", "?")
        hops = info.get("hops", 0)
        path = info.get("path", [])
        path_str = " → ".join(path) if path else "direct"
        return CommandResult(self._chunk([
            f"SNR: {snr} dB  RSSI: {rssi} dBm",
            f"Hops: {hops}  Path: {path_str}",
        ]))

    async def _cmd_advert(self, pubkey: str, name: str, arg: str) -> CommandResult:
        if not self._is_admin(pubkey):
            return CommandResult(["Unknown command '!advert'. Send !help."])
        if self._advert_callback is None:
            return CommandResult(["Advert not available."])
        await self._advert_callback()
        return CommandResult(["Advert sent."])

    async def _cmd_advert_channels(self, pubkey: str, name: str, arg: str) -> CommandResult:
        if not self._is_admin(pubkey):
            return CommandResult(["Unknown command '!advert_channels'. Send !help."])
        if self._advert_channels_callback is None:
            return CommandResult(["Channel advert not configured."])
        await self._advert_channels_callback()
        return CommandResult(["Channel advert sent."])

    async def _cmd_weather(self, pubkey: str, name: str, arg: str) -> CommandResult:
        location = arg.strip() or self._weather_location
        if not location:
            return CommandResult(["Usage: !weather <location>"])
        if self._weather_provider is None:
            return CommandResult(["Weather is not configured."])
        text = await self._weather_provider.fetch(location)
        return CommandResult(self._chunk([text]))

    # --- Helpers ---------------------------------------------------------

    @staticmethod
    def _fmt_ago(secs: int) -> str:
        """Return a compact relative-time string: '5m', '2h', '3d'."""
        if secs < 3600:
            return f"{max(1, secs // 60)}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"

    def _is_admin(self, pubkey: str) -> bool:
        return bool(self._admin_pubkeys) and any(pubkey.startswith(p) for p in self._admin_pubkeys)

    def _current_room(self, pubkey: str) -> str | None:
        user = self._store.get_user(pubkey)
        return user["current_room"] if user else None

    def _chunk(self, lines: list[str]) -> list[str]:
        """Pack lines into as few messages as possible without exceeding the
        per-message limit. Lines are joined with newlines; a single line
        longer than the limit is hard-truncated with an ellipsis."""
        messages: list[str] = []
        current = ""

        for line in lines:
            if len(line) > self._max_len:
                if current:
                    messages.append(current)
                    current = ""
                if self._max_len > 3:
                    messages.append(line[:self._max_len - 3] + "...")
                else:
                    messages.append(line[:self._max_len])
                continue

            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) > self._max_len:
                messages.append(current)
                current = line
            else:
                current = candidate

        if current:
            messages.append(current)
        return messages

    _COMMANDS = {
        "help": _cmd_help,
        "rooms": _cmd_rooms,
        "join": _cmd_join,
        "leave": _cmd_leave,
        "post": _cmd_post,
        "read": _cmd_read,
        "msg": _cmd_msg,
        "inbox": _cmd_inbox,
        "who": _cmd_who,
        "users": _cmd_users,
        "whoami": _cmd_whoami,
        "whereami": _cmd_whereami,
        "pwd": _cmd_whereami,
        "stats": _cmd_stats,
        "weather": _cmd_weather,
        "ping": _cmd_ping,
        "advert": _cmd_advert,
        "advert_channels": _cmd_advert_channels,
        "restart": _cmd_restart,
    }