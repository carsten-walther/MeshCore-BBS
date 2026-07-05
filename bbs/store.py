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
    created_at  INTEGER NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_room ON posts (room, id);

CREATE TABLE IF NOT EXISTS private_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0
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
        # Migrations: add columns introduced after the initial schema.
        for stmt in [
            "ALTER TABLE memberships ADD COLUMN last_activity INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE posts ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE private_messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                self._conn.execute(stmt)
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

    def list_all_users(self) -> list[sqlite3.Row]:
        """Return all users ordered by most recently active."""
        return self._db.execute(
            "SELECT * FROM users ORDER BY last_seen DESC, rowid DESC"
        ).fetchall()

    def kick_user(self, pubkey: str) -> list[str]:
        """Remove a user from all rooms. Returns the list of rooms they left."""
        rows = self._db.execute(
            "SELECT room FROM memberships WHERE pubkey=?", (pubkey,)
        ).fetchall()
        rooms = [r["room"] for r in rows]
        for room in rooms:
            self.leave_room(pubkey, room)
        self.set_current_room(pubkey, None)
        return rooms

    def delete_user(self, pubkey: str) -> bool:
        """Physically delete a user and soft-delete their posts and messages.
        Returns True if the user existed."""
        if not self.get_user(pubkey):
            return False
        self.kick_user(pubkey)
        self._db.execute("UPDATE posts SET deleted=1 WHERE author=? AND deleted=0", (pubkey,))
        self._db.execute(
            "UPDATE private_messages SET deleted=1 WHERE (sender=? OR recipient=?) AND deleted=0",
            (pubkey, pubkey),
        )
        self._db.execute("DELETE FROM users WHERE pubkey=?", (pubkey,))
        self._db.commit()
        return True

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

    def list_rooms_with_stats(self) -> list[sqlite3.Row]:
        """Return rooms with member count and last post timestamp."""
        cur = self._db.execute(
            """
            SELECT r.name,
                   COUNT(DISTINCT m.pubkey) AS member_count,
                   MAX(p.created_at) AS last_post_at
            FROM rooms r
            LEFT JOIN memberships m ON m.room = r.name
            LEFT JOIN posts p ON p.room = r.name AND p.deleted = 0
            GROUP BY r.name
            ORDER BY r.name
            """
        )
        return cur.fetchall()

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

    def room_members(self, room: str) -> list[sqlite3.Row]:
        """Return all current members of `room`, most recently active first."""
        cur = self._db.execute(
            """
            SELECT u.name, m.last_activity
            FROM memberships m
            JOIN users u ON u.pubkey = m.pubkey
            WHERE m.room = ?
            ORDER BY m.last_activity DESC
            """,
            (room,),
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

    def count_unseen_posts(self, pubkey: str, room: str) -> int:
        """Return the number of unread posts without fetching them."""
        cur = self._db.execute(
            """
            SELECT COUNT(*) FROM posts p
            JOIN memberships m ON m.room = p.room AND m.pubkey = ?
            WHERE p.room = ? AND p.id > m.last_seen_post AND p.deleted = 0
            """,
            (pubkey, room),
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def unseen_posts(self, pubkey: str, room: str, limit: int | None = None) -> list[sqlite3.Row]:
        """Return posts in `room` newer than what this user has already seen.

        Does NOT mark them seen — call mark_room_seen() after they've
        actually been delivered, so a send failure doesn't silently skip
        messages.
        """
        sql = """
            SELECT p.* FROM posts p
            JOIN memberships m ON m.room = p.room AND m.pubkey = ?
            WHERE p.room = ? AND p.id > m.last_seen_post AND p.deleted = 0
            ORDER BY p.id
            """
        if limit is not None:
            cur = self._db.execute(sql + " LIMIT ?", (pubkey, room, limit))
        else:
            cur = self._db.execute(sql, (pubkey, room))
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
            WHERE recipient=? AND delivered=0 AND deleted=0
            ORDER BY id
            """,
            (recipient,),
        )
        return cur.fetchall()

    def mark_private_delivered(self, message_id: int) -> None:
        self._db.execute(
            "UPDATE private_messages SET delivered=1, deleted=1 WHERE id=?", (message_id,)
        )
        self._db.commit()

    def recipients_with_undelivered_private(self) -> list[str]:
        """Return pubkeys of all users who have at least one undelivered private message."""
        cur = self._db.execute(
            "SELECT DISTINCT recipient FROM private_messages WHERE delivered=0 AND deleted=0"
        )
        return [r["recipient"] for r in cur.fetchall()]

    def list_posts(self, room: str, limit: int = 20) -> list[sqlite3.Row]:
        """Return the most recent non-deleted posts in a room, newest first."""
        return self._db.execute(
            "SELECT * FROM posts WHERE room=? AND deleted=0 ORDER BY id DESC LIMIT ?",
            (room, limit),
        ).fetchall()

    def delete_post(self, post_id: int) -> bool:
        """Soft-delete a post by ID. Returns True if the post existed and was deleted."""
        cur = self._db.execute(
            "UPDATE posts SET deleted=1 WHERE id=? AND deleted=0", (post_id,)
        )
        self._db.commit()
        return cur.rowcount > 0

    def delete_posts_in_room(self, room: str) -> int:
        """Soft-delete all posts in a room. Returns the number of posts deleted."""
        cur = self._db.execute(
            "UPDATE posts SET deleted=1 WHERE room=? AND deleted=0", (room,)
        )
        self._db.commit()
        return cur.rowcount

    def get_stats(self) -> dict:
        """Return counts of users, non-deleted posts, and rooms."""
        row = self._db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM posts WHERE deleted=0) AS posts,
                (SELECT COUNT(*) FROM rooms) AS rooms
            """
        ).fetchone()
        return {"users": row["users"], "posts": row["posts"], "rooms": row["rooms"]}

    def expire_posts(self, ttl_secs: int) -> int:
        """Soft-delete room posts older than ttl_secs. Returns the number of posts marked."""
        cutoff = int(time.time()) - ttl_secs
        cur = self._db.execute(
            "UPDATE posts SET deleted=1 WHERE created_at < ? AND deleted=0",
            (cutoff,),
        )
        self._db.commit()
        return cur.rowcount