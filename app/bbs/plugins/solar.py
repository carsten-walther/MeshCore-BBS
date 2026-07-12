"""Space-weather / solar data providers with automatic fallback.

Same design as weather.py: providers RAISE on failure (SolarError,
aiohttp.ClientError, TimeoutError) instead of returning error strings, so
ChainedSolarProvider can fall through to the next source. Only a total
chain failure produces a user-facing message.

One deliberate difference to the weather chain: solar data is global and
slow-moving (Kp updates every 3 h, solar flux daily), so the chain caches
a successful result for _CACHE_TTL seconds — replies are instant and the
free APIs are not hammered.

Note: space weather affects HF propagation (3-30 MHz), not the 868 MHz
LoRa band — !solar is a service for radio amateurs on the mesh, not a
diagnostic for the mesh itself.
"""

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any, Protocol

import aiohttp

from bbs.config import FeaturesConfig
from bbs.messages import Messages
from bbs.plugin import CommandPlugin

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_CACHE_TTL = 900.0  # seconds a fetched result stays fresh


class SolarError(Exception):
    """A provider could not produce a result."""


class SolarProvider(Protocol):
    """Any object with an async fetch() -> str method qualifies."""

    async def fetch(self) -> str: ...


def _format_hamqsl(xml_text: str) -> str:
    """Render the hamqsl.com solar XML into compact lines.
    Pure function so it is testable without any network.

    Rating words (Good/Fair/Poor) stay untranslated on purpose — like SNR
    or RSSI they are the universal vocabulary of every solar widget.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SolarError(f"unparseable XML: {exc}") from exc
    data = root.find("solardata")
    if data is None:
        raise SolarError("missing <solardata> element")

    def text_of(tag: str) -> str:
        el = data.find(tag)
        return el.text.strip() if el is not None and el.text else ""

    flux = text_of("solarflux")
    if not flux:
        raise SolarError("missing solar flux value")
    line = f"SFI {flux}  SSN {text_of('sunspots')}  A {text_of('aindex')}  K {text_of('kindex')}"
    geomag = text_of("geomagfield")
    if geomag:
        line += f" ({geomag})"
    lines = [line]

    # HF band ratings live under <calculatedconditions> — NOT the VHF
    # phenomena block. Band names like "80m-40m" are compacted to "80-40"
    # so day + night + indices still fit a single 150-byte DM.
    conditions = data.find("calculatedconditions")
    for period in ("day", "night"):
        ratings = [
            f"{band.get('name', '?').replace('m', '')} {band.text.strip()}"
            for band in (conditions.iter("band") if conditions is not None else [])
            if band.get("time") == period and band.text
        ]
        if ratings:
            lines.append(f"{period.capitalize()}: " + ", ".join(ratings))
    return "\n".join(lines)


class HamQslProvider:
    """Solar data via hamqsl.com (N0NBH) — a single XML request delivers
    the indices AND ready-made HF band condition ratings."""

    _URL = "https://www.hamqsl.com/solarxml.php"

    async def fetch(self) -> str:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(self._URL) as resp,
        ):
            resp.raise_for_status()
            return _format_hamqsl(await resp.text())


def _format_noaa(kp_rows: list, flux_rows: list) -> str:
    """Render NOAA SWPC data (indices only — NOAA publishes no band
    forecast). Pure function, testable without any network."""
    if not kp_rows or not flux_rows:
        raise SolarError("empty NOAA data")
    latest = kp_rows[-1]
    return (
        f"SFI {round(float(flux_rows[-1]['flux']))}"
        f"  A {latest['a_running']}  K {round(float(latest['Kp']))}"
    )


class NoaaSwpcProvider:
    """Official NOAA SWPC data (keyless JSON) — the fallback. Two small
    endpoints; no band forecast, indices only."""

    _KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
    _FLUX_URL = "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"

    async def fetch(self) -> str:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            kp_rows = await self._get_json(session, self._KP_URL)
            flux_rows = await self._get_json(session, self._FLUX_URL)
        return _format_noaa(kp_rows, flux_rows)

    @staticmethod
    async def _get_json(session: aiohttp.ClientSession, url: str) -> Any:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()


class ChainedSolarProvider:
    """Try providers in order; cache a success for _CACHE_TTL seconds.

    Failures are never cached — the next request tries the chain again.
    Only a total failure reaches the user."""

    def __init__(self, *providers: SolarProvider, messages: Messages | None = None) -> None:
        self._providers = providers
        self._messages = messages
        self._cached: str | None = None
        self._cached_at = 0.0

    async def fetch(self) -> str:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < _CACHE_TTL:
            return self._cached
        for provider in self._providers:
            try:
                result = await provider.fetch()
            except (TimeoutError, aiohttp.ClientError, SolarError, KeyError) as exc:
                _LOGGER.warning(f"{type(provider).__name__} failed: {exc} — trying next.")
                continue
            self._cached, self._cached_at = result, now
            return result
        t = self._messages.t if self._messages else Messages().t
        return t("Solar data unavailable.")


def plugin(provider: SolarProvider) -> CommandPlugin:
    """Bundle !solar as a self-contained optional command (see plugin.py)."""

    async def handle(pubkey: str, name: str, arg: str) -> list[str]:
        return (await provider.fetch()).splitlines()

    return CommandPlugin("solar", "!solar — solar and HF band conditions", handle)


# Merged into the shared Messages instance by the plugin loader.
TRANSLATIONS: dict[str, dict[str, str]] = {
    "de": {
        "!solar — solar and HF band conditions": "!solar — Sonnen- und HF-Bandbedingungen",
        "Solar data unavailable.": "Solardaten nicht verfügbar.",
    },
}


def create(features: FeaturesConfig, messages: Messages) -> CommandPlugin:
    """Auto-loader entry point: !solar with the default provider chain."""
    return plugin(
        ChainedSolarProvider(HamQslProvider(), NoaaSwpcProvider(), messages=messages)
    )
