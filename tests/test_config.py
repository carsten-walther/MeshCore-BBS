"""Tests for config loading, validation, and path resolution."""

from pathlib import Path

import yaml

from bbs.config import (
    _APP_ROOT,
    _resolve,
    _valid_log_level,
    _valid_qos,
    _valid_times,
    load_config,
)


class TestTimes:
    def test_valid_times_pass_through(self):
        assert _valid_times(["09:00", "21:30"], "t") == ["09:00", "21:30"]

    def test_yaml_sexagesimal_ints_are_converted(self):
        # PyYAML (YAML 1.1) parses unquoted 21:00 as the integer 1260.
        assert _valid_times([1260, 540], "t") == ["21:00", "09:00"]

    def test_single_digit_components_are_normalized(self):
        assert _valid_times(["9:5"], "t") == ["09:05"]

    def test_invalid_entries_are_dropped(self):
        assert _valid_times(["abc", "24:00", "12:60", True, None], "t") == []

    def test_yaml_block_style_roundtrip(self):
        # End-to-end: exactly what a user writes without quotes.
        raw = yaml.safe_load("times:\n  - 09:00\n  - 21:00\n  - 9:00\n")
        assert _valid_times(raw["times"], "t") == ["09:00", "21:00", "09:00"]


class TestLogLevelAndQos:
    def test_level_is_case_insensitive(self):
        assert _valid_log_level("info") == "INFO"
        assert _valid_log_level(" DEBUG ") == "DEBUG"

    def test_unknown_level_falls_back_to_info(self):
        assert _valid_log_level("TRACE") == "INFO"
        assert _valid_log_level(None) == "INFO"

    def test_qos_is_clamped(self):
        assert _valid_qos(1, "x") == 1
        assert _valid_qos(7, "x") == 0
        assert _valid_qos(-1, "x") == 0


class TestPathResolution:
    def test_relative_paths_anchor_at_app_root(self):
        resolved = Path(_resolve("data/bbs.db"))
        assert resolved.is_absolute()
        assert resolved == _APP_ROOT / "data" / "bbs.db"

    def test_absolute_paths_pass_through(self):
        assert _resolve("/data/bbs.db") == "/data/bbs.db"

    def test_empty_string_means_disabled_and_passes_through(self):
        # logging.file == "" means stdout-only; must NOT become _APP_ROOT.
        assert _resolve("") == ""


class TestLoadConfig:
    def test_missing_file_is_created_with_portable_relative_paths(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg = load_config(cfg_path)

        # The written YAML keeps relative paths (portable) ...
        text = cfg_path.read_text()
        assert "db_path: data/bbs.db" in text
        assert "./.." not in text
        # ... while the returned config is resolved for runtime use.
        assert Path(cfg.bbs.storage.db_path).is_absolute()
        assert Path(cfg.bbs.logging.file).is_absolute()

    def test_existing_file_values_survive_and_get_validated(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "bbs:\n"
            "  name: Testbox\n"
            "  advert:\n"
            "    times:\n"
            "      - 21:00\n"          # unquoted -> YAML int 1260
            "      - 'nonsense'\n"
            "  admin:\n"               # removed section — must be ignored,
            "    pubkeys:\n"           # old configs keep loading fine
            "      - ''\n"
            "  logging:\n"
            "    level: warning\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.bbs.name == "Testbox"
        assert cfg.bbs.advert.times == ["21:00"]
        assert not hasattr(cfg.bbs, "admin")
        assert cfg.bbs.logging.level == "WARNING"

    def test_defaults_for_missing_keys(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("bbs:\n  name: OnlyName\n")
        cfg = load_config(cfg_path)
        assert cfg.bbs.messaging.max_len == 150
        assert cfg.bbs.rooms.names == ["lobby"]
        assert cfg.connection.type == "serial"


class TestRadioConfigDefaults:
    """Review point 3.3: the default must be 'leave the device unchanged'.
    Concrete radio values may only come from an explicit config."""

    def test_auto_created_config_does_not_pin_radio_values(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg = load_config(cfg_path)
        assert cfg.radio.frequency is None
        assert cfg.radio.spreading_factor is None
        # The written YAML documents the keys but pins no values.
        raw = yaml.safe_load(cfg_path.read_text())
        assert all(v is None for v in raw["radio"].values())

    def test_explicit_radio_values_are_loaded_and_typed(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "radio:\n  frequency: 869.618\n  spreading_factor: '8'\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.radio.frequency == 869.618
        assert cfg.radio.spreading_factor == 8      # str -> int coercion
        assert cfg.radio.bandwidth is None          # untouched keys stay None


class TestExampleConfig:
    """Review point 3.6: the committed example must never drift from the
    real defaults again (the '- \"\"' admin trap came from exactly such a
    drift). Comments in the example are free — values are not."""

    def test_example_matches_generated_defaults(self, tmp_path):
        generated = tmp_path / "config.yaml"
        load_config(generated)

        example = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"
        assert yaml.safe_load(example.read_text()) == yaml.safe_load(generated.read_text())
