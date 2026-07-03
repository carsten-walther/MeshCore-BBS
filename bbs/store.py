"""SQLite persistence layer for the MeshCore BBS.

Holds users, rooms, room memberships, room posts, and private messages,
plus the per-user "seen" state that powers store-and-forward delivery.

Design notes:
- Users are keyed by their FULL public key (hex), never the 12-char
  pubkey_prefix from incoming events — prefixes can collide, full keys
  can't, and this table is the BBS's own source of truth.
- sqlite3 is synchronous, but the data is tiny and the LoRa message rate
  is low, so blocking the asyncio loop for a query is negligible here;
  pulling in aiosqlite would be needless complexity.
- The connection uses a Row factory so callers get dict-like rows.
"""

import logging
import sqlite3
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    pubkey       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    current_room TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    name       TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    pubkey         TEXT NOT NULL,
    room           TEXT NOT NULL,
    joined_at      INTEGER NOT NULL,
    last_seen_post INTEGER NOT NULL DEFAULT 0,
    last_activity  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pubkey, room)
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT NOT NULL,
    author      TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_room ON posts (room, id);

CREATE TABLE IF NOT EXISTS private_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pm_recipient ON private_messages (recipient, delivered);
"""


class BBSStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Open the database and create the schema if it doesn't exist."""
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        # Enforce foreign-key-ish integrity via app logic; enable WAL so a
        # crash mid-write can't corrupt the file.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # Migration: add last_activity if the DB predates this column.
        try:
            self._conn.execute(
                "ALTER TABLE memberships ADD COLUMN last_activity INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        self._conn.commit()
        _LOGGER.info(f"BBS store opened at {self._path}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("BBSStore.connect() must be called before use.")
        return self._conn

    # --- Users -----------------------------------------------------------

    def upsert_user(self, pubkey: str, name: str) -> None:
        """Record/refresh a user on every interaction (updates name + last_seen)."""
        now = int(time.time())
        self._db.execute(
            """
            INSERT INTO users (pubkey, name, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pubkey) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen
            """,
            (pubkey, name, now, now),
        )
        self._db.commit()

    def get_user(self, pubkey: str) -> sqlite3.Row | None:
        cur = self._db.execute("SELECT * FROM users WHERE pubkey=?", (pubkey,))
        return cur.fetchone()

    def find_user_by_name(self, name: str) -> sqlite3.Row | None:
        """Look up a user by (case-insensitive) name — used to address a
        private message by name. Returns None on no match or ambiguity."""
        cur = self._db.execute(
            "SELECT * FROM users WHERE name=? COLLATE NOCASE", (name,)
        )
        rows = cur.fetchall()
        return rows[0] if len(rows) == 1 else None

    def list_recent_users(self, limit: int = 5, exclude_pubkey: str | None = None) -> list[sqlite3.Row]:
        """Return the most-recently-active users, newest first.

        Rows include last_seen so callers *can* show activity, but the
        !users command deliberately doesn't render it. `exclude_pubkey`
        drops the requesting user from their own listing.
        """
        if exclude_pubkey is not None:
            cur = self._db.execute(
                "SELECT * FROM users WHERE pubkey != ? ORDER BY last_seen DESC, rowid DESC LIMIT ?",
                (exclude_pubkey, limit),
            )
        else:
            cur = self._db.execute(
                "SELECT * FROM users ORDER BY last_seen DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        return cur.fetchall()

    def set_current_room(self, pubkey: str, room: str | None) -> None:
        self._db.execute("UPDATE users SET current_room=? WHERE pubkey=?", (room, pubkey))
        self._db.commit()

    # --- Rooms -----------------------------------------------------------

    def room_exists(self, name: str) -> bool:
        cur = self._db.execute("SELECT 1 FROM rooms WHERE name=?", (name,))
        return cur.fetchone() is not None

    def create_room(self, name: str, created_by: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO rooms (name, created_at, created_by) VALUES (?, ?, ?)",
            (name, int(time.time()), created_by),
        )
        self._db.commit()

    def list_rooms(self) -> list[str]:
        cur = self._db.execute("SELECT name FROM rooms ORDER BY name")
        return [r["name"] for r in cur.fetchall()]

    # --- Memberships -----------------------------------------------------

    def join_room(self, pubkey: str, room: str) -> None:
        now = int(time.time())
        # On re-join, refresh last_activity so the timeout clock restarts.
        self._db.execute(
            """
            INSERT INTO memberships (pubkey, room, joined_at, last_seen_post, last_activity)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(pubkey, room) DO UPDATE SET last_activity=excluded.last_activity
            """,
            (pubkey, room, now, now),
        )
        self._db.commit()

    def leave_room(self, pubkey: str, room: str) -> None:
        self._db.execute("DELETE FROM memberships WHERE pubkey=? AND room=?", (pubkey, room))
        self._db.commit()

    def update_room_activity(self, pubkey: str, room: str) -> None:
        """Refresh the last-activity timestamp for a user's room membership."""
        self._db.execute(
            "UPDATE memberships SET last_activity=? WHERE pubkey=? AND room=?",
            (int(time.time()), pubkey, room),
        )
        self._db.commit()

    def inactive_members(self, timeout_seconds: int) -> list[sqlite3.Row]:
        """Return memberships whose last_activity is older than timeout_seconds.

        Rows with last_activity=0 (memberships predating this feature) are
        excluded — they would otherwise all expire on the first check.
        """
        cutoff = int(time.time()) - timeout_seconds
        cur = self._db.execute(
            "SELECT pubkey, room FROM memberships WHERE last_activity > 0 AND last_activity < ?",
            (cutoff,),
        )
        return cur.fetchall()

    def is_member(self, pubkey: str, room: str) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM memberships WHERE pubkey=? AND room=?", (pubkey, room)
        )
        return cur.fetchone() is not None

    # --- Posts (room messages) ------------------------------------------

    def add_post(self, room: str, author: str, author_name: str, text: str) -> int:
        cur = self._db.execute(
            """
            INSERT INTO posts (room, author, author_name, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (room, author, author_name, text, int(time.time())),
        )
        self._db.commit()
        return cur.lastrowid

    def unseen_posts(self, pubkey: str, room: str) -> list[sqlite3.Row]:
        """Return posts in `room` newer than what this user has already seen.

        Does NOT mark them seen — call mark_room_seen() after they've
        actually been delivered, so a send failure doesn't silently skip
        messages.
        """
        cur = self._db.execute(
            """
            SELECT p.* FROM posts p
            JOIN memberships m ON m.room = p.room AND m.pubkey = ?
            WHERE p.room = ? AND p.id > m.last_seen_post
            ORDER BY p.id
            """,
            (pubkey, room),
        )
        return cur.fetchall()

    def mark_room_seen(self, pubkey: str, room: str, up_to_id: int) -> None:
        self._db.execute(
            "UPDATE memberships SET last_seen_post=? WHERE pubkey=? AND room=?",
            (up_to_id, pubkey, room),
        )
        self._db.commit()

    # --- Private messages ------------------------------------------------

    def add_private_message(self, sender: str, sender_name: str, recipient: str, text: str) -> int:
        cur = self._db.execute(
            """
            INSERT INTO private_messages (sender, sender_name, recipient, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sender, sender_name, recipient, text, int(time.time())),
        )
        self._db.commit()
        return cur.lastrowid

    def undelivered_private(self, recipient: str) -> list[sqlite3.Row]:
        """Return this user's not-yet-delivered private messages. As with
        unseen_posts(), marking-as-delivered is a separate step."""
        cur = self._db.execute(
            """
            SELECT * FROM private_messages
            WHERE recipient=? AND delivered=0
            ORDER BY id
            """,
            (recipient,),
        )
        return cur.fetchall()

    def mark_private_delivered(self, message_id: int) -> None:
        self._db.execute("UPDATE private_messages SET delivered=1 WHERE id=?", (message_id,))
        self._db.commit()

    def recipients_with_undelivered_private(self) -> list[str]:
        """Return pubkeys of all users who have at least one undelivered private message."""
        cur = self._db.execute(
            "SELECT DISTINCT recipient FROM private_messages WHERE delivered=0"
        )
        return [r["recipient"] for r in cur.fetchall()]