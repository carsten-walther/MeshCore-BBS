"""Tests for the message catalog and the German command experience."""

import logging

import pytest

from bbs.commands import CommandRouter
from bbs.config import _valid_language
from bbs.messages import DE, Messages, placeholders
from bbs.store import BBSStore

ALICE = "aa" * 32
BOB = "bb" * 32


class TestCatalogIntegrity:
    def test_every_translation_keeps_its_placeholders(self):
        """A German template with different {fields} than its English key
        would crash at runtime — catch it structurally instead."""
        for en, de in DE.items():
            assert placeholders(de) == placeholders(en), f"Platzhalter weichen ab: {en!r}"

    def test_translations_are_actually_german(self):
        # Guard against accidentally mapping a key to itself.
        identical = [en for en, de in DE.items() if en == de]
        assert identical == [], identical


class TestMessages:
    def test_english_is_identity(self):
        assert Messages().t("No new messages.") == "No new messages."
        assert Messages("en").t("Left '{room}'.", room="lobby") == "Left 'lobby'."

    def test_german_translation(self):
        assert Messages("de").t("No new messages.") == "Keine neuen Nachrichten."
        assert Messages("de").t("Left '{room}'.", room="lobby") == "'lobby' verlassen."

    def test_unknown_template_falls_back_to_itself(self):
        assert Messages("de").t("Totally new string {x}.", x=1) == "Totally new string 1."

    def test_override_beats_catalog(self):
        msgs = Messages("de", overrides={"No new messages.": "Nix Neues!"})
        assert msgs.t("No new messages.") == "Nix Neues!"

    def test_broken_override_falls_back_to_english(self, caplog):
        msgs = Messages("en", overrides={"Left '{room}'.": "Weg von {raum}."})
        with caplog.at_level(logging.WARNING, logger="bbs.messages"):
            assert msgs.t("Left '{room}'.", room="lobby") == "Left 'lobby'."
        assert any("Broken placeholders" in r.message for r in caplog.records)


class TestExtend:
    """Plugins merge their own TRANSLATIONS into a Messages instance."""

    def test_adds_translations_for_the_active_language(self):
        m = Messages("de")
        m.extend({"de": {"Hello {name}": "Hallo {name}"}})
        assert m.t("Hello {name}", name="x") == "Hallo x"

    def test_ignores_other_languages(self):
        m = Messages("en")
        m.extend({"de": {"Hello": "Hallo"}})
        assert m.t("Hello") == "Hello"

    def test_config_overrides_beat_extended_translations(self):
        m = Messages("de", overrides={"Hello": "Servus"})
        m.extend({"de": {"Hello": "Hallo"}})
        assert m.t("Hello") == "Servus"

    def test_does_not_leak_into_other_instances(self):
        a = Messages("de")
        a.extend({"de": {"Xyz": "Zyx"}})
        assert Messages("de").t("Xyz") == "Xyz"


class TestLanguageConfig:
    def test_valid_languages_normalized(self):
        assert _valid_language("DE ") == "de"
        assert _valid_language("en") == "en"

    def test_unknown_language_falls_back(self):
        assert _valid_language("fr") == "en"


@pytest.fixture
def store(tmp_path):
    s = BBSStore(tmp_path / "test.db")
    s.connect()
    s.create_room("lobby", "config")
    yield s
    s.close()


class TestGermanRouter:
    """End-to-end: the same flows the English tests cover, auf Deutsch."""

    @pytest.fixture
    def router(self, store):
        return CommandRouter(
            store,
            messages=Messages("de"),
            additional_commands=["seen", "whoami", "stats"],
        )

    async def test_unknown_command(self, router):
        result = await router.handle(ALICE, "Alice", "!quatsch")
        assert result.messages[0] == "Unbekannter Befehl '!quatsch'. Sende !help."

    async def test_join_post_read_flow(self, store, router):
        store.upsert_user(BOB, "Bob")
        joined = await router.handle(BOB, "Bob", "!join lobby")
        assert "'lobby' betreten" in joined.messages[0]
        await router.handle(BOB, "Bob", "!post hallo")

        await router.handle(ALICE, "Alice", "!join lobby")
        read = await router.handle(ALICE, "Alice", "!read")
        assert any("hallo" in msg for msg in read.messages)
        read.on_delivered()
        empty = await router.handle(ALICE, "Alice", "!read")
        assert "Keine neuen Beiträge in 'lobby'." in empty.messages[0]

    async def test_undo_and_stats_plurals(self, store, router):
        store.upsert_user(BOB, "Bob")
        await router.handle(BOB, "Bob", "!join lobby")
        await router.handle(BOB, "Bob", "!post tippfehler")
        undo = await router.handle(BOB, "Bob", "!undo")
        assert "Dein Beitrag in 'lobby' wurde entfernt" in undo.messages[0]

        stats = await router.handle(BOB, "Bob", "!stats")
        assert "Statistik:" in stats.messages[0]
        assert "Beitrag" in stats.messages[0] or "Beiträge" in stats.messages[0]

    async def test_help_is_german(self, router):
        result = await router.handle(ALICE, "Alice", "!help")
        assert result.messages[0].startswith("Befehle: !rooms")

        detail = await router.handle(ALICE, "Alice", "!help undo")
        assert detail.messages == ["!undo — letzten Beitrag entfernen"]

    async def test_english_texts_unchanged_by_default(self, store):
        # The critical property: without config, everything stays English.
        r = CommandRouter(store)
        result = await r.handle(ALICE, "Alice", "!quatsch")
        assert result.messages[0] == "Unknown command '!quatsch'. Send !help."
