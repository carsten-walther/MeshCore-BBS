"""Weather provider abstraction and wttr.in implementation.

To add a new provider, implement the WeatherProvider protocol:

    class MyProvider:
        async def fetch(self, location: str) -> str:
            ...

No base class or registration needed — pass an instance to CommandRouter.
"""

import asyncio
import logging
from typing import Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)


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
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    text = (await resp.text()).strip()
                    return text or f"No weather data for '{location}'."
        except asyncio.TimeoutError:
            _LOGGER.warning(f"Weather fetch timed out for '{location}'.")
            return "Weather request timed out."
        except aiohttp.ClientError as exc:
            _LOGGER.warning(f"Weather fetch failed for '{location}': {exc}")
            return f"Weather unavailable for '{location}'."