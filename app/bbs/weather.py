"""Weather providers with automatic fallback.

To add a new provider, implement the WeatherProvider protocol:

    class MyProvider:
        async def fetch(self, location: str) -> str:
            ...

Providers RAISE on failure (WeatherError, aiohttp.ClientError,
TimeoutError) instead of returning error strings — that is what lets
ChainedWeatherProvider fall through to the next source. Only the chain
itself turns a total failure into a user-facing message.
"""

import logging
from typing import Any, Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)


class WeatherError(Exception):
    """A provider could not produce a result for this location."""


class WeatherProvider(Protocol):
    """Any object with an async fetch(location) -> str method qualifies."""

    async def fetch(self, location: str) -> str: ...


class WttrInProvider:
    """Weather via wttr.in — free, no API key required.

    fmt follows the wttr.in format string syntax:
      "3"                → "Berlin: ⛅️ +18°C"  (default, very compact)
      "%l: %c %t %h %w" → adds humidity and wind speed
    See https://wttr.in/:help for all format codes.
    """

    def __init__(self, fmt: str = "%l: %c %t %h %w %p %P") -> None:
        self._fmt = fmt

    async def fetch(self, location: str) -> str:
        url = f"https://wttr.in/{location}"
        params = {"format": self._fmt}
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(url, params=params) as resp,
        ):
            resp.raise_for_status()
            text = (await resp.text()).strip()
            if not text:
                raise WeatherError(f"empty response for '{location}'")
            return text


# Compact WMO weather-code descriptions (open-meteo uses WMO codes).
_WMO_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "frz drizzle", 57: "frz drizzle",
    61: "rain", 63: "rain", 65: "heavy rain",
    66: "frz rain", 67: "frz rain",
    71: "snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}


def _format_open_meteo(place: dict[str, Any], current: dict[str, Any]) -> str:
    """Render open-meteo data in a compact, wttr-like single line.
    Pure function so it is testable without any network."""
    desc = _WMO_CODES.get(current.get("weather_code", -1), "")
    parts = [
        f"{place.get('name', '?')}:",
        desc,
        f"{round(current['temperature_2m'])}°C",
        f"{round(current['relative_humidity_2m'])}%",
        f"{round(current['wind_speed_10m'])}km/h",
        f"{current.get('precipitation', 0)}mm",
    ]
    return " ".join(p for p in parts if p)


class OpenMeteoProvider:
    """Weather via open-meteo.com — keyless and very reliable.

    Needs two requests (geocoding, then forecast) because the API takes
    coordinates, not place names."""

    _GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    _WX_URL = "https://api.open-meteo.com/v1/forecast"

    async def fetch(self, location: str) -> str:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            geo = await self._get_json(session, self._GEO_URL, {"name": location, "count": 1})
            results = geo.get("results") or []
            if not results:
                raise WeatherError(f"unknown location '{location}'")
            place = results[0]

            wx = await self._get_json(
                session,
                self._WX_URL,
                {
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,"
                    "wind_speed_10m,precipitation,weather_code",
                },
            )
            current = wx.get("current")
            if not current:
                raise WeatherError(f"no current weather for '{location}'")
            return _format_open_meteo(place, current)

    @staticmethod
    async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any]) -> Any:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


class ChainedWeatherProvider:
    """Try providers in order; only a total failure reaches the user.

    wttr.in is kept first for its charming emoji one-liners, but it is
    notoriously flaky — open-meteo covers its outages."""

    def __init__(self, *providers: WeatherProvider) -> None:
        self._providers = providers

    async def fetch(self, location: str) -> str:
        for provider in self._providers:
            try:
                return await provider.fetch(location)
            except (TimeoutError, aiohttp.ClientError, WeatherError, KeyError) as exc:
                _LOGGER.warning(
                    f"{type(provider).__name__} failed for '{location}': {exc} — trying next."
                )
        return f"Weather unavailable for '{location}'."
