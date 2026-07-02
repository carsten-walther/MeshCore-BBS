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

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from bbs.store import BBSStore

_LOGGER = logging.getLogger(__name__)

# Conservative default for a MeshCore direct (contact) message. Contact
# messages don't embed a sender-name prefix in the text (unlike channel
# messages), so the usable body is larger — but staying conservative keeps
# replies safely inside the limit regardless of firmware specifics.
_DEFAULT_MAX_LEN = 150

# How many recently-active users !users shows by default.
_USER_LIST_LIMIT = 5


@dataclass
class CommandResult:
    """A command's reply, plus an optional commit callback.

    `messages` are sent to the sender in order. `on_delivered`, if set, is
    invoked by bbs.py only after ALL messages were sent successfully — used
    by !read/!inbox to advance the seen/delivered state, so a failed radio
    send doesn't silently drop messages the user never received.
    """
    messages: list[str] = field(default_factory=list)
    on_delivered: Callable[[], None] | None = None


class CommandRouter:
    def __init__(self, store: BBSStore, max_message_length: int = _DEFAULT_MAX_LEN) -> None:
        self._store = store
        self._max_len = max_message_length

    def handle(self, pubkey: str, name: str, text: str) -> CommandResult:
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
        return handler(self, pubkey, name, arg)

    # --- Command implementations ----------------------------------------

    def _cmd_help(self, pubkey: str, name: str, arg: str) -> CommandResult:
        return CommandResult(self._chunk([
            "Commands:",
            "!rooms — list rooms",
            "!join <room> — enter a room",
            "!leave — leave current room",
            "!post <text> — post to current room",
            "!read — read new posts",
            "!msg [name] <text> — private message",
            "!inbox — read private messages",
            "!users — recent users",
            "!whoami — your name",
            "!whereami or !pwd — current room",
        ]))

    def _cmd_rooms(self, pubkey: str, name: str, arg: str) -> CommandResult:
        rooms = self._store.list_rooms()
        if not rooms:
            return CommandResult(["No rooms available."])
        return CommandResult(self._chunk(["Rooms: " + ", ".join(rooms)]))

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
        self._store.update_room_activity(pubkey, room)
        posts = self._store.unseen_posts(pubkey, room)
        if not posts:
            return CommandResult([f"No new posts in '{room}'."])

        lines = [f"{p['author_name']}: {p['text']}" for p in posts]
        last_id = posts[-1]["id"]

        # Deferred commit: only advance the seen marker once bbs.py confirms
        # every message was actually sent.
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
            # Either never seen by the BBS, or the name is ambiguous — in
            # both cases we can't safely pick a delivery target.
            return CommandResult([f"Unknown or ambiguous user '{target_name}'."])

        self._store.add_private_message(pubkey, name, target["pubkey"], body)
        return CommandResult([f"Message queued for {target['name']}."])

    def _cmd_inbox(self, pubkey: str, name: str, arg: str) -> CommandResult:
        pms = self._store.undelivered_private(pubkey)
        if not pms:
            return CommandResult(["No new messages."])

        lines = [f"{m['sender_name']}: {m['text']}" for m in pms]
        ids = [m["id"] for m in pms]

        def commit() -> None:
            for mid in ids:
                self._store.mark_private_delivered(mid)

        return CommandResult(self._chunk(lines), on_delivered=commit)

    def _cmd_users(self, pubkey: str, name: str, arg: str) -> CommandResult:
        users = self._store.list_recent_users(limit=_USER_LIST_LIMIT, exclude_pubkey=pubkey)
        if not users:
            return CommandResult(["No other users known yet."])
        # Show names in the [name] form so they can be pasted straight into
        # !msg. One per line so _chunk() can pack/split cleanly.
        lines = ["Recent users:"] + [f"[{u['name']}]" for u in users]
        return CommandResult(self._chunk(lines))

    def _cmd_whoami(self, pubkey: str, name: str, arg: str) -> CommandResult:
        # handle() already upserted the user, so get_user() is populated;
        # fall back to the live name just in case.
        user = self._store.get_user(pubkey)
        known = user["name"] if user else name
        return CommandResult([f"You are known as [{known}]."])

    def _cmd_whereami(self, pubkey: str, name: str, arg: str) -> CommandResult:
        room = self._current_room(pubkey)
        if room:
            return CommandResult([f"You are in room '{room}'."])
        return CommandResult(["You are not in any room. Use !join <room>."])

    # --- Helpers ---------------------------------------------------------

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
        "users": _cmd_users,
        "whoami": _cmd_whoami,
        "whereami": _cmd_whereami,
        "pwd": _cmd_whereami,
    }