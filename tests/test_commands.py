"""Tests for the command router — runs entirely without hardware."""

import pytest

from bbs.commands import CommandRouter
from bbs.store import BBSStore

ALICE = "aa" * 32
BOB = "bb" * 32


@pytest.fixture
def store(tmp_path):
    s = BBSStore(tmp_path / "test.db")
    s.connect()
    s.create_room("lobby", "config")
    yield s
    s.close()


@pytest.fixture
def router(store):
    return CommandRouter(store, max_message_length=150)


class TestChunking:
    """Regression for review point 1.2: the device limit is UTF-8 BYTES."""

    def test_no_message_exceeds_byte_limit(self, router):
        lines = [
            "Schöne Grüße aus Zwönitz — ä ö ü ß 📬 " * 4,  # forces truncation
            "kurz",
            "äöü" * 40,  # 120 chars but 240 bytes
        ]
        for msg in router._chunk(lines):
            assert len(msg.encode("utf-8")) <= 150

    def test_btrunc_never_splits_multibyte(self):
        s = "abc📬def"
        for n in range(len(s.encode())):
            out = CommandRouter._btrunc(s, n)
            assert len(out.encode()) <= n
            out.encode().decode("utf-8")  # must not raise

    def test_lines_are_packed_not_one_per_message(self, router):
        msgs = router._chunk(["a", "b", "c"])
        assert msgs == ["a\nb\nc"]


class TestAdminAuth:
    """Regression for review point 1.1: '' must never grant admin."""

    def test_empty_string_grants_nothing(self, store):
        r = CommandRouter(store, admin_pubkeys=[""])
        assert not r._is_admin("deadbeef" * 8)

    def test_no_admins_configured(self, store):
        r = CommandRouter(store, admin_pubkeys=[])
        assert not r._is_admin(ALICE)

    def test_match_is_case_insensitive(self, store):
        r = CommandRouter(store, admin_pubkeys=["a3f2c19e8b7d5f04"])
        assert r._is_admin("A3F2C19E8B7D5F04" + "00" * 24)

    async def test_admin_commands_hidden_from_non_admins(self, router):
        result = await router.handle(ALICE, "Alice", "!restart")
        assert "Unknown command" in result.messages[0]


class TestMsgParsing:
    """The bracket syntax for names with spaces (@?[name])."""

    async def test_bare_word_target(self, store, router):
        store.upsert_user(BOB, "Bob")
        result = await router.handle(ALICE, "Alice", "!msg Bob hallo")
        assert "queued for Bob" in result.messages[0]
        assert result.inbox_notify_pubkey == BOB

    async def test_bracketed_name_with_spaces(self, store, router):
        store.upsert_user(BOB, "Peter Bosch")
        result = await router.handle(ALICE, "Alice", "!msg [Peter Bosch] hallo")
        assert "queued for Peter Bosch" in result.messages[0]

    async def test_mention_form_with_at_sign(self, store, router):
        store.upsert_user(BOB, "Peter Bosch")
        result = await router.handle(ALICE, "Alice", "!msg @[Peter Bosch] hallo")
        assert "queued for Peter Bosch" in result.messages[0]

    async def test_unterminated_bracket_is_usage_error(self, router):
        result = await router.handle(ALICE, "Alice", "!msg [Peter Bosch]")
        assert "Usage" in result.messages[0]

    async def test_self_message_is_rejected(self, store, router):
        store.upsert_user(ALICE, "Alice")
        result = await router.handle(ALICE, "Alice", "!msg Alice hallo")
        assert "yourself" in result.messages[0]


class TestReadFlow:
    async def test_deferred_commit_only_after_delivery(self, store, router):
        """!read must not advance seen-state until on_delivered runs."""
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        await router.handle(BOB, "Bob", "!post hallo welt")

        await router.handle(ALICE, "Alice", "!join lobby")
        result = await router.handle(ALICE, "Alice", "!read")
        assert any("hallo welt" in m for m in result.messages)
        assert result.on_delivered is not None

        # Simulate a FAILED radio send: commit not called -> post stays unread.
        again = await router.handle(ALICE, "Alice", "!read")
        assert any("hallo welt" in m for m in again.messages)

        # Successful delivery -> commit -> no new posts.
        again.on_delivered()
        empty = await router.handle(ALICE, "Alice", "!read")
        assert "No new posts" in empty.messages[0]

    async def test_read_requires_room(self, router):
        result = await router.handle(ALICE, "Alice", "!read")
        assert "Join a room first" in result.messages[0]


class TestOptionalCommands:
    async def test_disabled_optional_command_is_unknown(self, store):
        r = CommandRouter(store, additional_commands=[])
        result = await r.handle(ALICE, "Alice", "!ping")
        assert "Unknown command" in result.messages[0]

    async def test_enabled_ping_uses_signal_info(self, store):
        r = CommandRouter(store, additional_commands=["ping"])
        info = {"snr": 8, "rssi": -95, "hops": 2, "path": ["ab12", "cd34"]}
        result = await r.handle(ALICE, "Alice", "!ping", signal_info=info)
        joined = "\n".join(result.messages)
        assert "SNR: 8" in joined and "ab12" in joined

    async def test_enabled_ping_without_data(self, store):
        r = CommandRouter(store, additional_commands=["ping"])
        result = await r.handle(ALICE, "Alice", "!ping", signal_info=None)
        assert "No signal data" in result.messages[0]


class TestMisc:
    async def test_non_command_text_gets_help_hint(self, router):
        result = await router.handle(ALICE, "Alice", "hallo?")
        assert "!help" in result.messages[0]

    async def test_unknown_command(self, router):
        result = await router.handle(ALICE, "Alice", "!frobnicate")
        assert "Unknown command" in result.messages[0]

    def test_fmt_ago(self):
        assert CommandRouter._fmt_ago(30) == "1m"       # never "0m"
        assert CommandRouter._fmt_ago(3599) == "59m"
        assert CommandRouter._fmt_ago(7200) == "2h"
        assert CommandRouter._fmt_ago(200_000) == "2d"


class TestReadLimit:
    """Airtime guard (review point 4.1): !read without a number must not
    flood the mesh for users with a large backlog."""

    async def _fill_lobby(self, store, router, n):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        for i in range(n):
            await router.handle(BOB, "Bob", f"!post post-{i}")
        await router.handle(ALICE, "Alice", "!join lobby")

    async def test_default_limit_caps_backlog_and_hints_remainder(self, store):
        r = CommandRouter(store, read_limit=5)
        await self._fill_lobby(store, r, 8)
        result = await r.handle(ALICE, "Alice", "!read")
        joined = "\n".join(result.messages)
        assert "post-4" in joined and "post-5" not in joined   # oldest 5 only
        assert "+3 more" in joined

    async def test_commit_advances_to_the_remainder(self, store):
        r = CommandRouter(store, read_limit=5)
        await self._fill_lobby(store, r, 8)
        first = await r.handle(ALICE, "Alice", "!read")
        first.on_delivered()
        second = await r.handle(ALICE, "Alice", "!read")
        joined = "\n".join(second.messages)
        assert "post-5" in joined and "post-7" in joined
        assert "more" not in joined                            # backlog cleared

    async def test_explicit_number_overrides_default(self, store):
        r = CommandRouter(store, read_limit=5)
        await self._fill_lobby(store, r, 8)
        result = await r.handle(ALICE, "Alice", "!read 8")
        joined = "\n".join(result.messages)
        assert "post-7" in joined and "more" not in joined

    async def test_limit_zero_means_unlimited(self, store):
        r = CommandRouter(store, read_limit=0)
        await self._fill_lobby(store, r, 8)
        result = await r.handle(ALICE, "Alice", "!read")
        joined = "\n".join(result.messages)
        assert "post-7" in joined and "more" not in joined


class TestReply:
    """!reply answers the sender of the last DELIVERED inbox message."""

    async def test_reply_flow(self, store, router):
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!msg Alice hallo Alice")

        inbox = await router.handle(ALICE, "Alice", "!inbox")
        inbox.on_delivered()

        result = await router.handle(ALICE, "Alice", "!reply hallo zurück")
        assert "queued for Bob" in result.messages[0]
        assert result.inbox_notify_pubkey == BOB

        bob_inbox = await router.handle(BOB, "Bob", "!inbox")
        assert any("hallo zurück" in m for m in bob_inbox.messages)

    async def test_reply_without_prior_inbox(self, router):
        result = await router.handle(ALICE, "Alice", "!reply hi")
        assert "!inbox first" in result.messages[0]

    async def test_target_set_only_after_delivery_commit(self, store, router):
        """Deferred-commit contract: a failed inbox send must not set the
        reply target."""
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!msg Alice hallo")
        await router.handle(ALICE, "Alice", "!inbox")   # commit NOT called
        result = await router.handle(ALICE, "Alice", "!reply hi")
        assert "!inbox first" in result.messages[0]

    async def test_newest_sender_wins(self, store, router):
        carol = "cc" * 32
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Bob")
        store.upsert_user(carol, "Carol")
        await router.handle(BOB, "Bob", "!msg Alice erste")
        await router.handle(carol, "Carol", "!msg Alice zweite")
        inbox = await router.handle(ALICE, "Alice", "!inbox")
        inbox.on_delivered()
        result = await router.handle(ALICE, "Alice", "!reply an dich")
        assert "queued for Carol" in result.messages[0]

    async def test_reply_to_deleted_user_is_graceful(self, store, router):
        store.upsert_user(ALICE, "Alice")
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!msg Alice hallo")
        inbox = await router.handle(ALICE, "Alice", "!inbox")
        inbox.on_delivered()
        store.delete_user(BOB)
        result = await router.handle(ALICE, "Alice", "!reply hi")
        assert "no longer known" in result.messages[0]

    async def test_reply_without_text_is_usage_error(self, store, router):
        result = await router.handle(ALICE, "Alice", "!reply")
        assert "Usage" in result.messages[0]

    async def test_reply_listed_in_help(self, router):
        result = await router.handle(ALICE, "Alice", "!help")
        assert any("!reply" in m for m in result.messages)
