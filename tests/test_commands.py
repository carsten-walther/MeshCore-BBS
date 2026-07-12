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
    # The DB-backed optional commands are enabled like in the default
    # config; the network-backed ones (weather, ping, solar) are enabled
    # per-test together with their fakes.
    return CommandRouter(
        store, max_message_length=150, additional_commands=["seen", "whoami", "stats"]
    )


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


class TestRemovedAdminCommands:
    """The former DM admin commands are gone — they must behave like any
    other unknown command (admin actions move to the admin interface)."""

    @pytest.mark.parametrize("cmd", ["!restart", "!advert", "!advert_channels"])
    async def test_removed_commands_are_unknown(self, router, cmd):
        result = await router.handle(ALICE, "Alice", cmd)
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

    @pytest.mark.parametrize("cmd", ["!seen Bob", "!whoami", "!stats"])
    async def test_disabled_db_backed_commands_are_unknown(self, store, cmd):
        r = CommandRouter(store, additional_commands=[])
        result = await r.handle(ALICE, "Alice", cmd)
        assert "Unknown command" in result.messages[0]

    @pytest.mark.parametrize("cmd,expect", [("!whoami", "Alice"), ("!stats", "1")])
    async def test_enabled_db_backed_commands_work(self, store, cmd, expect):
        r = CommandRouter(store, additional_commands=["seen", "whoami", "stats"])
        result = await r.handle(ALICE, "Alice", cmd)
        assert expect in result.messages[0]

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

    async def test_ping_shows_24h_trend_with_history(self, store):
        r = CommandRouter(store, additional_commands=["ping"])
        store.add_signal_record(ALICE, snr=4.0, rssi=-90, hops=0)
        store.add_signal_record(ALICE, snr=8.0, rssi=-100, hops=0)
        info = {"snr": 8, "rssi": -100, "hops": 0, "path": []}
        result = await r.handle(ALICE, "Alice", "!ping", signal_info=info)
        joined = "\n".join(result.messages)
        assert "24h: avg SNR 6.0 dB, -95 dBm (2 packets)" in joined

    async def test_ping_hides_trend_with_single_sample(self, store):
        # One sample IS the current packet — a "trend" would be noise.
        r = CommandRouter(store, additional_commands=["ping"])
        store.add_signal_record(ALICE, snr=8.0, rssi=-100, hops=0)
        info = {"snr": 8, "rssi": -100, "hops": 0, "path": []}
        result = await r.handle(ALICE, "Alice", "!ping", signal_info=info)
        assert "24h" not in "\n".join(result.messages)

    async def test_enabled_solar_uses_provider(self, store):
        class _FakeSolar:
            async def fetch(self) -> str:
                return "SFI 107  SSN 80  A 12  K 1\nDay: 80-40 Fair"

        r = CommandRouter(store, additional_commands=["solar"], solar_provider=_FakeSolar())
        result = await r.handle(ALICE, "Alice", "!solar")
        joined = "\n".join(result.messages)
        assert "SFI 107" in joined and "Day:" in joined

    async def test_disabled_solar_is_unknown(self, store):
        r = CommandRouter(store, additional_commands=[])
        result = await r.handle(ALICE, "Alice", "!solar")
        assert "Unknown command" in result.messages[0]

    async def test_enabled_solar_without_provider(self, store):
        r = CommandRouter(store, additional_commands=["solar"])
        result = await r.handle(ALICE, "Alice", "!solar")
        assert "not configured" in result.messages[0]

    async def test_ping_trend_is_per_user(self, store):
        r = CommandRouter(store, additional_commands=["ping"])
        store.add_signal_record(BOB, snr=4.0, rssi=-90, hops=0)
        store.add_signal_record(BOB, snr=8.0, rssi=-100, hops=0)
        info = {"snr": 8, "rssi": -100, "hops": 0, "path": []}
        result = await r.handle(ALICE, "Alice", "!ping", signal_info=info)
        assert "24h" not in "\n".join(result.messages)


class TestHelp:
    """Airtime redesign: bare !help is ONE DM of command names; descriptions
    and the optional commands are sent per request only."""

    _ALL_EXTRAS = ["seen", "whoami", "stats", "weather", "ping", "solar"]

    async def test_summary_is_a_single_dm_in_every_language(self, store):
        from bbs.messages import Messages
        for lang in ("en", "de"):
            for extras in ([], self._ALL_EXTRAS):
                r = CommandRouter(
                    store, messages=Messages(lang), additional_commands=extras
                )
                result = await r.handle(ALICE, "Alice", "!help")
                assert len(result.messages) == 1, (lang, extras)
                assert len(result.messages[0].encode()) <= 150, (lang, extras)

    async def test_summary_omits_optional_commands(self, store):
        r = CommandRouter(store, additional_commands=self._ALL_EXTRAS)
        text = (await r.handle(ALICE, "Alice", "!help")).messages[0]
        for cmd in self._ALL_EXTRAS:
            assert f"!{cmd}" not in text
        assert "!help extras" in text

    async def test_summary_hides_extras_hint_when_none_enabled(self, store):
        r = CommandRouter(store, additional_commands=[])
        text = (await r.handle(ALICE, "Alice", "!help")).messages[0]
        assert "extras" not in text
        assert "!help <cmd>" in text

    async def test_per_command_detail(self, router):
        result = await router.handle(ALICE, "Alice", "!help read")
        assert result.messages == ["!read (n) — read new posts"]

    async def test_detail_accepts_bang_prefix(self, router):
        result = await router.handle(ALICE, "Alice", "!help !msg")
        assert "private message" in result.messages[0]

    async def test_pwd_alias_shares_whereami_detail(self, router):
        result = await router.handle(ALICE, "Alice", "!help pwd")
        assert "!whereami" in result.messages[0]

    async def test_extras_lists_only_enabled(self, store):
        r = CommandRouter(store, additional_commands=["ping"])
        joined = "\n".join((await r.handle(ALICE, "Alice", "!help extras")).messages)
        assert "!ping" in joined and "!weather" not in joined and "!solar" not in joined

    async def test_extras_is_a_names_only_single_dm(self, store):
        """!help extras uses the same compact format as the bare summary."""
        from bbs.messages import Messages
        for lang in ("en", "de"):
            r = CommandRouter(
                store, messages=Messages(lang), additional_commands=self._ALL_EXTRAS
            )
            result = await r.handle(ALICE, "Alice", "!help extras")
            assert len(result.messages) == 1, lang
            text = result.messages[0]
            assert len(text.encode()) <= 150, lang
            assert "!help <cmd>" in text
            for cmd in self._ALL_EXTRAS:
                assert f"!{cmd}" in text
            # Names only — the description lines stay behind !help <cmd>.
            assert "signal quality" not in text and "Signalqualität" not in text

    async def test_extras_with_none_enabled(self, store):
        r = CommandRouter(store, additional_commands=[])
        result = await r.handle(ALICE, "Alice", "!help extras")
        assert "No extra commands" in result.messages[0]

    async def test_detail_for_enabled_optional(self, store):
        r = CommandRouter(store, additional_commands=["weather"])
        result = await r.handle(ALICE, "Alice", "!help weather")
        assert "current weather" in result.messages[0]

    async def test_detail_for_disabled_optional_is_unknown(self, store):
        r = CommandRouter(store, additional_commands=[])
        result = await r.handle(ALICE, "Alice", "!help weather")
        assert "Unknown command" in result.messages[0]

    async def test_unknown_topic(self, router):
        result = await router.handle(ALICE, "Alice", "!help frobnicate")
        assert "Unknown command '!frobnicate'" in result.messages[0]

    def test_every_command_has_a_detail_line(self):
        """A new command without a !help <cmd> entry is a doc bug."""
        from bbs.commands import _COMMAND_HELP, _HELP_ALIASES, _OPTIONAL_COMMANDS
        documented = set(_COMMAND_HELP) | set(_OPTIONAL_COMMANDS) | set(_HELP_ALIASES)
        assert set(CommandRouter._COMMANDS) <= documented

    def test_summary_lists_every_core_command(self):
        """A new core command must also appear in the summary line."""
        from bbs.commands import _HELP_ALIASES, _HELP_ORDER, _OPTIONAL_COMMANDS
        summary = {_HELP_ALIASES.get(c, c) for c in _HELP_ORDER}
        core = {
            c for c in CommandRouter._COMMANDS
            if c not in _OPTIONAL_COMMANDS and c not in _HELP_ALIASES and c != "help"
        }
        assert core <= summary


class TestMisc:
    async def test_non_command_text_gets_help_hint(self, router):
        result = await router.handle(ALICE, "Alice", "hallo?")
        assert "!help" in result.messages[0]

    async def test_unknown_command(self, router):
        result = await router.handle(ALICE, "Alice", "!frobnicate")
        assert "Unknown command" in result.messages[0]

    def test_fmt_ago(self):
        from bbs.util import fmt_ago

        assert fmt_ago(30) == "1m"       # never "0m" — also in the admin CLI now
        assert fmt_ago(3599) == "59m"
        assert fmt_ago(7200) == "2h"
        assert fmt_ago(200_000) == "2d"


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


class TestMsgDisambiguation:
    """Review point 4.3: distinct unknown/ambiguous errors, pubkey-prefix
    addressing, and name-prefix convenience."""

    async def test_unknown_user_message(self, router):
        result = await router.handle(ALICE, "Alice", "!msg Nobody hi")
        assert "No user 'Nobody' known" in result.messages[0]

    async def test_ambiguous_name_lists_candidates_with_prefixes(self, store, router):
        store.upsert_user(BOB, "Peter")
        carol = "cc" * 32
        store.upsert_user(carol, "Peter")
        result = await router.handle(ALICE, "Alice", "!msg Peter hi")
        joined = "\n".join(result.messages)
        assert "ambiguous" in joined
        assert BOB[:8] in joined and carol[:8] in joined
        assert "!msg <keyprefix>" in joined

    async def test_pubkey_prefix_addresses_uniquely(self, store, router):
        store.upsert_user(BOB, "Peter")
        carol = "cc" * 32
        store.upsert_user(carol, "Peter")           # same name, key still works
        result = await router.handle(ALICE, "Alice", f"!msg {carol[:8]} hi")
        assert "queued for Peter" in result.messages[0]
        assert result.inbox_notify_pubkey == carol

    async def test_pubkey_prefix_is_case_insensitive(self, store, router):
        store.upsert_user(BOB, "Bob")
        result = await router.handle(ALICE, "Alice", f"!msg {BOB[:8].upper()} hi")
        assert result.inbox_notify_pubkey == BOB

    async def test_ambiguous_pubkey_prefix(self, store, router):
        k1, k2 = "abcd11" + "00" * 29, "abcd22" + "00" * 29
        store.upsert_user(k1, "Eins")
        store.upsert_user(k2, "Zwei")
        result = await router.handle(ALICE, "Alice", "!msg abcd hi")
        joined = "\n".join(result.messages)
        assert "ambiguous" in joined and "Eins" in joined and "Zwei" in joined

    async def test_short_hex_word_is_treated_as_name(self, store, router):
        # "ed" is 2 hex chars — below the 4-char threshold, so it must be
        # looked up as a name, not a key prefix.
        store.upsert_user(BOB, "Ed")
        result = await router.handle(ALICE, "Alice", "!msg ed hi")
        assert result.inbox_notify_pubkey == BOB

    async def test_exact_name_beats_pubkey_prefix(self, store, router):
        # A user literally named "abcd" wins over someone whose KEY starts
        # with abcd — exact names have priority.
        keyuser = "abcd" + "00" * 30
        store.upsert_user(keyuser, "KeyGuy")
        store.upsert_user(BOB, "abcd")
        result = await router.handle(ALICE, "Alice", "!msg abcd hi")
        assert result.inbox_notify_pubkey == BOB

    async def test_exact_name_beats_name_prefix(self, store, router):
        store.upsert_user(BOB, "Bo")
        carol = "cc" * 32
        store.upsert_user(carol, "Bob2")
        result = await router.handle(ALICE, "Alice", "!msg Bo hi")
        assert result.inbox_notify_pubkey == BOB

    async def test_unique_name_prefix_resolves(self, store, router):
        store.upsert_user(BOB, "Peter Bosch")
        result = await router.handle(ALICE, "Alice", "!msg Pet hi")
        assert "queued for Peter Bosch" in result.messages[0]

    async def test_users_listing_shows_key_prefix(self, store, router):
        store.upsert_user(BOB, "Bob")
        result = await router.handle(ALICE, "Alice", "!users")
        assert BOB[:8] in "\n".join(result.messages)


class TestUndo:
    """Review point 4.5: users can retract their own recent posts."""

    async def test_undo_removes_post_from_future_reads(self, store, router):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        await router.handle(BOB, "Bob", "!post oops typo")
        result = await router.handle(BOB, "Bob", "!undo")
        assert "Removed your post in 'lobby'" in result.messages[0]
        assert "oops typo" in result.messages[0]

        await router.handle(ALICE, "Alice", "!join lobby")
        read = await router.handle(ALICE, "Alice", "!read")
        assert "No new posts" in read.messages[0]

    async def test_undo_is_repeatable_newest_first(self, store, router):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        await router.handle(BOB, "Bob", "!post erster")
        await router.handle(BOB, "Bob", "!post zweiter")
        first = await router.handle(BOB, "Bob", "!undo")
        assert "zweiter" in first.messages[0]
        second = await router.handle(BOB, "Bob", "!undo")
        assert "erster" in second.messages[0]
        third = await router.handle(BOB, "Bob", "!undo")
        assert "Nothing to undo" in third.messages[0]

    async def test_undo_only_touches_own_posts(self, store, router):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        await router.handle(BOB, "Bob", "!post von Bob")
        result = await router.handle(ALICE, "Alice", "!undo")
        assert "Nothing to undo" in result.messages[0]

    async def test_undo_respects_time_window(self, store):
        r = CommandRouter(store, undo_window=600)
        store.upsert_user(BOB, "Bob")
        await r.handle(BOB, "Bob", "!join lobby")
        await r.handle(BOB, "Bob", "!post alt")
        store._db.execute("UPDATE posts SET created_at = created_at - 700")
        store._db.commit()
        result = await r.handle(BOB, "Bob", "!undo")
        assert "Too late" in result.messages[0] and "10m" in result.messages[0]

    async def test_window_zero_means_no_time_limit(self, store):
        r = CommandRouter(store, undo_window=0)
        store.upsert_user(BOB, "Bob")
        await r.handle(BOB, "Bob", "!join lobby")
        await r.handle(BOB, "Bob", "!post uralt")
        store._db.execute("UPDATE posts SET created_at = created_at - 999999")
        store._db.commit()
        result = await r.handle(BOB, "Bob", "!undo")
        assert "Removed your post" in result.messages[0]

    async def test_long_text_is_snipped_in_confirmation(self, store, router):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        await router.handle(BOB, "Bob", "!post " + "x" * 80)
        result = await router.handle(BOB, "Bob", "!undo")
        assert "…" in result.messages[0]
        assert len(result.messages[0].encode()) <= 150

    async def test_undo_listed_in_help(self, router):
        result = await router.handle(ALICE, "Alice", "!help")
        assert any("!undo" in m for m in result.messages)


class TestRateLimit:
    """Airtime guard: a sender over the limit gets ONE warning, then
    silence — replying to every excess message would burn the very
    airtime the limit protects."""

    def _age_events(self, r, pubkey, seconds=61):
        # Slide all recorded events out of the window (no sleeps in tests).
        from collections import deque
        r._rate_events[pubkey] = deque(t - seconds for t in r._rate_events[pubkey])

    async def test_allows_up_to_limit_then_warns_once_then_mutes(self, store):
        r = CommandRouter(store, rate_limit=3)
        for _ in range(3):
            result = await r.handle(ALICE, "Alice", "!help")
            assert result.messages
        warned = await r.handle(ALICE, "Alice", "!help")
        assert "Too many commands" in warned.messages[0]
        muted = await r.handle(ALICE, "Alice", "!help")
        assert muted.messages == []

    async def test_non_command_text_counts_too(self, store):
        # Plain text gets the !help hint reply — that costs airtime as well.
        r = CommandRouter(store, rate_limit=2)
        await r.handle(ALICE, "Alice", "hallo?")
        await r.handle(ALICE, "Alice", "hallo??")
        warned = await r.handle(ALICE, "Alice", "!help")
        assert "Too many commands" in warned.messages[0]

    async def test_window_expiry_restores_service_and_rewarns(self, store):
        r = CommandRouter(store, rate_limit=2)
        await r.handle(ALICE, "Alice", "!help")
        await r.handle(ALICE, "Alice", "!help")
        assert "Too many commands" in (await r.handle(ALICE, "Alice", "!help")).messages[0]

        self._age_events(r, ALICE)
        ok = await r.handle(ALICE, "Alice", "!help")
        assert any("Commands:" in m for m in ok.messages)   # service restored
        await r.handle(ALICE, "Alice", "!help")
        again = await r.handle(ALICE, "Alice", "!help")
        assert "Too many commands" in again.messages[0]     # warned anew, not muted

    async def test_zero_disables_the_limit(self, store):
        r = CommandRouter(store, rate_limit=0)
        for _ in range(30):
            assert (await r.handle(ALICE, "Alice", "!help")).messages

    async def test_limits_are_per_user(self, store):
        r = CommandRouter(store, rate_limit=1)
        await r.handle(ALICE, "Alice", "!help")
        assert "Too many commands" in (await r.handle(ALICE, "Alice", "!help")).messages[0]
        bob = await r.handle(BOB, "Bob", "!help")
        assert any("Commands:" in m for m in bob.messages)


class TestSeen:
    async def test_shows_last_activity(self, store, router):
        store.upsert_user(BOB, "Bob")
        store._db.execute("UPDATE users SET last_seen = last_seen - 7200 WHERE pubkey=?", (BOB,))
        store._db.commit()
        result = await router.handle(ALICE, "Alice", "!seen Bob")
        assert "[Bob]" in result.messages[0] and "2h" in result.messages[0]

    async def test_bracketed_name_with_spaces(self, store, router):
        store.upsert_user(BOB, "Peter Bosch")
        result = await router.handle(ALICE, "Alice", "!seen @[Peter Bosch]")
        assert "[Peter Bosch]" in result.messages[0]

    async def test_bare_name_with_spaces_works_without_brackets(self, store, router):
        # Unlike !msg there is no message body, so the whole argument is the name.
        store.upsert_user(BOB, "Peter Bosch")
        result = await router.handle(ALICE, "Alice", "!seen Peter Bosch")
        assert "[Peter Bosch]" in result.messages[0]

    async def test_unknown_user(self, router):
        result = await router.handle(ALICE, "Alice", "!seen Nobody")
        assert "No user 'Nobody' known" in result.messages[0]

    async def test_ambiguous_name_teaches_seen_keyprefix_form(self, store, router):
        store.upsert_user(BOB, "Peter")
        carol = "cc" * 32
        store.upsert_user(carol, "Peter")
        result = await router.handle(ALICE, "Alice", "!seen Peter")
        joined = "\n".join(result.messages)
        assert "ambiguous" in joined
        assert "!seen <keyprefix>" in joined and "!msg" not in joined

    async def test_pubkey_prefix_target(self, store, router):
        store.upsert_user(BOB, "Bob")
        result = await router.handle(ALICE, "Alice", f"!seen {BOB[:8]}")
        assert "[Bob]" in result.messages[0]

    async def test_without_argument_is_usage_error(self, router):
        result = await router.handle(ALICE, "Alice", "!seen")
        assert "Usage" in result.messages[0]

    async def test_listed_in_help_extras(self, router):
        # !seen is an optional command: not in the !help summary, but in extras.
        summary = await router.handle(ALICE, "Alice", "!help")
        assert "!seen" not in summary.messages[0]
        extras = await router.handle(ALICE, "Alice", "!help extras")
        assert any("!seen" in m for m in extras.messages)


class TestSearch:
    async def _fill_lobby(self, store, router, texts):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        for t in texts:
            await router.handle(BOB, "Bob", f"!post {t}")
        await router.handle(ALICE, "Alice", "!join lobby")

    async def test_finds_matching_posts(self, store, router):
        await self._fill_lobby(store, router, ["Antenne steht", "anderes Thema"])
        result = await router.handle(ALICE, "Alice", "!search antenne")
        joined = "\n".join(result.messages)
        assert "Antenne steht" in joined and "anderes Thema" not in joined

    async def test_no_match_message(self, store, router):
        await self._fill_lobby(store, router, ["hallo"])
        result = await router.handle(ALICE, "Alice", "!search xyz")
        assert "No posts matching 'xyz'" in result.messages[0]

    async def test_requires_room(self, router):
        result = await router.handle(ALICE, "Alice", "!search test")
        assert "Join a room first" in result.messages[0]

    async def test_without_argument_is_usage_error(self, router):
        result = await router.handle(ALICE, "Alice", "!search")
        assert "Usage" in result.messages[0]

    async def test_single_character_term_is_rejected(self, router):
        result = await router.handle(ALICE, "Alice", "!search a")
        assert "too short" in result.messages[0]

    async def test_limit_caps_results_and_hints_remainder(self, store):
        r = CommandRouter(store, read_limit=2)
        await self._fill_lobby(store, r, [f"treffer {i}" for i in range(5)])
        result = await r.handle(ALICE, "Alice", "!search treffer")
        joined = "\n".join(result.messages)
        # Newest first, capped at read_limit, remainder hinted.
        assert "treffer 4" in joined and "treffer 2" not in joined
        assert "+3 more" in joined

    async def test_does_not_advance_seen_marker(self, store, router):
        """Searching is browsing history, not reading — the posts must
        still arrive via !read afterwards."""
        await self._fill_lobby(store, router, ["wichtiger treffer"])
        await router.handle(ALICE, "Alice", "!search treffer")
        read = await router.handle(ALICE, "Alice", "!read")
        assert any("wichtiger treffer" in m for m in read.messages)

    async def test_listed_in_help(self, router):
        result = await router.handle(ALICE, "Alice", "!help")
        assert any("!search" in m for m in result.messages)
