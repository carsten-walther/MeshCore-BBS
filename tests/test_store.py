"""Tests for the SQLite persistence layer."""

import pytest

from bbs.store import BBSStore

ALICE = "aa" * 32
BOB = "bb" * 32


@pytest.fixture
def store(tmp_path):
    s = BBSStore(tmp_path / "test.db")
    s.connect()
    yield s
    s.close()


class TestUsers:
    def test_upsert_creates_and_updates(self, store):
        store.upsert_user(ALICE, "Alice")
        assert store.get_user(ALICE)["name"] == "Alice"
        store.upsert_user(ALICE, "Alice2")
        assert store.get_user(ALICE)["name"] == "Alice2"

    def test_find_by_name_is_case_insensitive(self, store):
        store.upsert_user(ALICE, "Alice")
        assert store.find_user_by_name("alice")["pubkey"] == ALICE

    def test_find_by_name_returns_none_on_ambiguity(self, store):
        # Two users with the same display name: refusing to guess is the
        # documented behaviour (a name is not an identity on the mesh).
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Alice")
        assert store.find_user_by_name("Alice") is None

    def test_delete_user_soft_deletes_content(self, store):
        store.upsert_user(ALICE, "Alice")
        store.create_room("lobby", "config")
        store.join_room(ALICE, "lobby")
        store.add_post("lobby", ALICE, "Alice", "hi")
        assert store.delete_user(ALICE)
        assert store.get_user(ALICE) is None
        assert store.list_posts("lobby") == []          # post soft-deleted
        assert store.get_stats()["posts"] == 0


class TestRoomsAndPosts:
    def test_unseen_marks_only_after_commit(self, store):
        """The deferred-commit contract: reading posts must NOT mark them
        seen; that happens only after successful delivery."""
        store.upsert_user(ALICE, "Alice")
        store.create_room("lobby", "config")
        store.join_room(ALICE, "lobby")
        p1 = store.add_post("lobby", BOB, "Bob", "first")
        store.add_post("lobby", BOB, "Bob", "second")

        posts = store.unseen_posts(ALICE, "lobby")
        assert [p["text"] for p in posts] == ["first", "second"]
        # Not marked yet — a failed radio send must not lose posts.
        assert len(store.unseen_posts(ALICE, "lobby")) == 2

        store.mark_room_seen(ALICE, "lobby", p1)
        assert [p["text"] for p in store.unseen_posts(ALICE, "lobby")] == ["second"]

    def test_expire_posts_respects_ttl(self, store):
        store.create_room("lobby", "config")
        pid = store.add_post("lobby", ALICE, "Alice", "old")
        store.add_post("lobby", ALICE, "Alice", "fresh")
        # Backdate one post beyond the TTL; the cutoff is strictly
        # `created_at < now - ttl`, so a fresh post must survive.
        store._db.execute("UPDATE posts SET created_at = created_at - 100 WHERE id = ?", (pid,))
        store._db.commit()
        assert store.expire_posts(ttl_secs=50) == 1
        assert [p["text"] for p in store.list_posts("lobby")] == ["fresh"]

    def test_delete_room_evicts_and_resets_current_room(self, store):
        store.upsert_user(ALICE, "Alice")
        store.create_room("tech", "config")
        store.join_room(ALICE, "tech")
        store.set_current_room(ALICE, "tech")
        assert store.delete_room("tech")
        assert store.get_user(ALICE)["current_room"] is None
        assert not store.is_member(ALICE, "tech")


class TestPrivateMessages:
    def test_delivery_flow(self, store):
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Bob")
        mid = store.add_private_message(ALICE, "Alice", BOB, "hallo")

        assert store.recipients_with_undelivered_private() == [BOB]
        pms = store.undelivered_private(BOB)
        assert len(pms) == 1 and pms[0]["text"] == "hallo"

        store.mark_private_delivered(mid)
        assert store.undelivered_private(BOB) == []
        assert store.recipients_with_undelivered_private() == []


class TestPragmas:
    def test_wal_and_busy_timeout_are_set(self, store):
        # Regression for concurrent admin.py access (review point 2.5).
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


class TestReplyTarget:
    def test_last_pm_from_roundtrip(self, store):
        store.upsert_user(ALICE, "Alice")
        assert store.get_user(ALICE)["last_pm_from"] is None
        store.set_last_pm_from(ALICE, BOB)
        assert store.get_user(ALICE)["last_pm_from"] == BOB

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        """Simulate a pre-!reply database: users table without the column;
        connect() must add it via the ALTER TABLE migration."""
        import sqlite3

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE users (pubkey TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " current_room TEXT, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL);"
        )
        conn.execute(
            "INSERT INTO users VALUES (?, 'Alt', NULL, 1, 1)", (ALICE,)
        )
        conn.commit()
        conn.close()

        s = BBSStore(db)
        s.connect()
        assert s.get_user(ALICE)["last_pm_from"] is None
        s.set_last_pm_from(ALICE, BOB)
        assert s.get_user(ALICE)["last_pm_from"] == BOB
        s.close()


class TestUserLookup:
    def test_name_prefix_escapes_like_wildcards(self, store):
        store.upsert_user(ALICE, "100% Bob")
        store.upsert_user(BOB, "100x Bob")
        # A literal '%' in the prefix must not act as a wildcard.
        assert len(store.find_users_by_name_prefix("100%")) == 1
        assert store.find_users_by_name_prefix("100%")[0]["pubkey"] == ALICE

    def test_pubkey_prefix_lookup(self, store):
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Bob")
        assert [u["pubkey"] for u in store.find_users_by_pubkey_prefix("aaaa")] == [ALICE]
        assert store.find_users_by_pubkey_prefix("ffff") == []


class TestLastPostBy:
    def test_returns_newest_non_deleted(self, store):
        store.create_room("lobby", "config")
        store.add_post("lobby", ALICE, "Alice", "eins")
        p2 = store.add_post("lobby", ALICE, "Alice", "zwei")
        assert store.last_post_by(ALICE)["id"] == p2
        store.delete_post(p2)
        assert store.last_post_by(ALICE)["text"] == "eins"

    def test_none_for_unknown_author(self, store):
        assert store.last_post_by("ff" * 32) is None
