"""Tests for the plugin auto-loader (bbs/plugins/__init__.py)."""

import logging
import string as stringlib
import sys
import types

from bbs.config import FeaturesConfig
from bbs.messages import Messages
from bbs.plugin import CommandPlugin
from bbs.plugins import load_plugins, solar, weather


def _fake_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(f"bbs.plugins.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class TestLoader:
    def test_loads_the_shipped_plugins_from_the_default_config(self):
        features = FeaturesConfig()  # default commands list
        plugins = load_plugins(features.commands, features, Messages())
        assert [p.name for p in plugins] == ["weather", "solar"]

    def test_builtin_optionals_are_skipped_silently(self, caplog):
        with caplog.at_level(logging.WARNING, logger="bbs.plugins"):
            plugins = load_plugins(
                ["seen", "whoami", "stats", "ping"], FeaturesConfig(), Messages()
            )
        assert plugins == []
        assert not caplog.records

    def test_unknown_name_warns_and_is_ignored(self, caplog):
        with caplog.at_level(logging.WARNING, logger="bbs.plugins"):
            plugins = load_plugins(["frobnicate", "solar"], FeaturesConfig(), Messages())
        assert [p.name for p in plugins] == ["solar"]
        assert any("frobnicate" in r.message for r in caplog.records)

    def test_broken_create_is_ignored(self, caplog, monkeypatch):
        def create(features, messages):
            raise RuntimeError("kaputt")

        monkeypatch.setitem(
            sys.modules, "bbs.plugins.broken", _fake_module("broken", create=create)
        )
        with caplog.at_level(logging.ERROR, logger="bbs.plugins"):
            assert load_plugins(["broken"], FeaturesConfig(), Messages()) == []
        assert any("broken" in r.message for r in caplog.records)

    def test_module_without_create_is_ignored(self, caplog, monkeypatch):
        monkeypatch.setitem(sys.modules, "bbs.plugins.nocreate", _fake_module("nocreate"))
        with caplog.at_level(logging.WARNING, logger="bbs.plugins"):
            assert load_plugins(["nocreate"], FeaturesConfig(), Messages()) == []
        assert any("create()" in r.message for r in caplog.records)

    def test_command_name_must_match_module_name(self, caplog, monkeypatch):
        async def handler(pubkey, name, arg):
            return []

        def create(features, messages):
            return CommandPlugin("other", "!other — x", handler)

        monkeypatch.setitem(
            sys.modules, "bbs.plugins.misnamed", _fake_module("misnamed", create=create)
        )
        with caplog.at_level(logging.WARNING, logger="bbs.plugins"):
            assert load_plugins(["misnamed"], FeaturesConfig(), Messages()) == []
        assert any("misnamed" in r.message for r in caplog.records)

    def test_translations_are_merged_into_messages(self):
        messages = Messages("de")
        load_plugins(["weather", "solar"], FeaturesConfig(), messages)
        assert messages.t("Usage: !weather <location>") == "Nutzung: !weather <ort>"
        assert messages.t("Solar data unavailable.") == "Solardaten nicht verfügbar."

    def test_plugin_receives_its_own_options(self, monkeypatch):
        seen: dict = {}

        async def handler(pubkey, name, arg):
            return []

        def create(options, messages):
            seen.update(options)
            return CommandPlugin("optioned", "!optioned — x", handler)

        monkeypatch.setitem(
            sys.modules, "bbs.plugins.optioned", _fake_module("optioned", create=create)
        )
        features = FeaturesConfig(plugins={"optioned": {"speed": 7}, "other": {"x": 1}})
        plugins = load_plugins(["optioned"], features, Messages())
        assert [p.name for p in plugins] == ["optioned"]
        assert seen == {"speed": 7}


class TestShippedPluginModules:
    """Every plugin in the package must satisfy the loader contract."""

    _SHIPPED = (weather, solar)

    def test_translation_placeholders_match_their_english_keys(self):
        def placeholders(s: str) -> list[str]:
            return sorted(fn for _, fn, _, _ in stringlib.Formatter().parse(s) if fn)

        for module in self._SHIPPED:
            for catalog in module.TRANSLATIONS.values():
                for en, translated in catalog.items():
                    assert placeholders(en) == placeholders(translated), (module.__name__, en)
                    assert en != translated, (module.__name__, en)

    def test_create_returns_a_matching_plugin_with_translated_help(self):
        for module in self._SHIPPED:
            plugin = module.create({}, Messages())
            assert plugin.name == module.__name__.rsplit(".", 1)[-1]
            assert plugin.help in module.TRANSLATIONS["de"]
