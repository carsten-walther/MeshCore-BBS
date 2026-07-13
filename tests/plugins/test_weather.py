"""Tests for the weather provider chain — no network involved."""

import aiohttp
import pytest

from bbs.plugins.weather import (
    _WMO_CODES,
    ChainedWeatherProvider,
    WeatherError,
    _format_open_meteo,
    plugin,
)


class _Ok:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, 0

    async def fetch(self, location: str) -> str:
        self.calls += 1
        return self.text


class _Fails:
    def __init__(self, exc: Exception) -> None:
        self.exc, self.calls = exc, 0

    async def fetch(self, location: str) -> str:
        self.calls += 1
        raise self.exc


class TestChain:
    async def test_first_success_wins(self):
        second = _Ok("zweiter")
        chain = ChainedWeatherProvider(_Ok("erster"), second)
        assert await chain.fetch("Leipzig") == "erster"
        assert second.calls == 0                      # not even tried

    @pytest.mark.parametrize(
        "exc",
        [
            WeatherError("kaputt"),
            aiohttp.ClientError("http"),
            TimeoutError(),
            KeyError("temperature_2m"),               # malformed API answer
        ],
    )
    async def test_falls_through_on_provider_errors(self, exc):
        chain = ChainedWeatherProvider(_Fails(exc), _Ok("fallback"))
        assert await chain.fetch("Leipzig") == "fallback"

    async def test_total_failure_yields_user_message(self):
        chain = ChainedWeatherProvider(_Fails(WeatherError("a")), _Fails(TimeoutError()))
        assert "Weather unavailable for 'Leipzig'" in await chain.fetch("Leipzig")

    async def test_unexpected_exceptions_propagate(self):
        # Programming errors must NOT be swallowed by the chain.
        with pytest.raises(ZeroDivisionError):
            await ChainedWeatherProvider(_Fails(ZeroDivisionError())).fetch("x")


class TestOpenMeteoFormatting:
    def test_compact_single_line(self):
        place = {"name": "Zwönitz"}
        current = {
            "weather_code": 61,
            "temperature_2m": 17.6,
            "relative_humidity_2m": 82,
            "wind_speed_10m": 12.3,
            "precipitation": 0.4,
        }
        line = _format_open_meteo(place, current)
        assert line == "Zwönitz: rain 18°C 82% 12km/h 0.4mm"
        assert len(line.encode()) < 150               # always a single DM

    def test_unknown_weather_code_is_omitted(self):
        current = {
            "weather_code": 1234,
            "temperature_2m": 1,
            "relative_humidity_2m": 50,
            "wind_speed_10m": 5,
        }
        line = _format_open_meteo({"name": "X"}, current)
        assert "  " not in line and "1234" not in line

    def test_wmo_map_covers_all_official_codes(self):
        official = {0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65,
                    66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
        assert official <= set(_WMO_CODES)


class TestWeatherPlugin:
    """The !weather command as a self-contained plugin (see plugin.py)."""

    async def test_argument_overrides_default_location(self):
        ok = _Ok("Berlin: sunny")
        p = plugin(ok, "Leipzig")
        assert await p.handler("aa", "Alice", "Berlin") == ["Berlin: sunny"]

    async def test_falls_back_to_default_location(self):
        class _Capture:
            async def fetch(self, location: str) -> str:
                return f"wx:{location}"

        p = plugin(_Capture(), "Leipzig")
        assert await p.handler("aa", "Alice", "") == ["wx:Leipzig"]

    async def test_usage_error_without_any_location(self):
        p = plugin(_Ok("x"), "")
        result = await p.handler("aa", "Alice", "  ")
        assert "Usage: !weather" in result[0]

    def test_plugin_identity(self):
        p = plugin(_Ok("x"), "")
        assert p.name == "weather" and "!weather" in p.help


class TestCreateDefaults:
    """create() owns the option defaults — config.py stays plugin-agnostic."""

    class _Capture:
        async def fetch(self, location: str) -> str:
            return f"wx:{location}"

    @pytest.fixture
    def captured_chain(self, monkeypatch):
        import bbs.plugins.weather as weather_mod
        monkeypatch.setattr(
            weather_mod, "ChainedWeatherProvider", lambda *a, **k: self._Capture()
        )
        return weather_mod

    async def test_missing_location_option_defaults_to_leipzig(self, captured_chain):
        p = captured_chain.create({}, None)
        assert await p.handler("aa", "Alice", "") == ["wx:Leipzig"]

    async def test_configured_location_wins(self, captured_chain):
        p = captured_chain.create({"location": "Dresden"}, None)
        assert await p.handler("aa", "Alice", "") == ["wx:Dresden"]

    async def test_explicit_empty_location_requires_an_argument(self, captured_chain):
        p = captured_chain.create({"location": ""}, None)
        result = await p.handler("aa", "Alice", "")
        assert "Usage: !weather" in result[0]
