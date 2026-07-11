"""Tests for the solar provider chain — no network involved."""

import aiohttp
import pytest

from bbs.solar import (
    ChainedSolarProvider,
    SolarError,
    _format_hamqsl,
    _format_noaa,
)

_SAMPLE_XML = """<?xml version="1.0"?>
<solar>
 <solardata>
  <solarflux>107</solarflux>
  <aindex>12</aindex>
  <kindex>1</kindex>
  <sunspots>80</sunspots>
  <geomagfield>VR QUIET</geomagfield>
  <calculatedconditions>
   <band name="80m-40m" time="day">Fair</band>
   <band name="30m-20m" time="day">Good</band>
   <band name="17m-15m" time="day">Good</band>
   <band name="12m-10m" time="day">Poor</band>
   <band name="80m-40m" time="night">Good</band>
   <band name="30m-20m" time="night">Good</band>
   <band name="17m-15m" time="night">Poor</band>
   <band name="12m-10m" time="night">Poor</band>
  </calculatedconditions>
  <calculatedvhfconditions>
   <phenomenon name="vhf-aurora" location="northern_hemi">Band Closed</phenomenon>
  </calculatedvhfconditions>
 </solardata>
</solar>
"""


class _Ok:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, 0

    async def fetch(self) -> str:
        self.calls += 1
        return self.text


class _Fails:
    def __init__(self, exc: Exception) -> None:
        self.exc, self.calls = exc, 0

    async def fetch(self) -> str:
        self.calls += 1
        raise self.exc


class TestChain:
    async def test_first_success_wins(self):
        second = _Ok("zweiter")
        chain = ChainedSolarProvider(_Ok("erster"), second)
        assert await chain.fetch() == "erster"
        assert second.calls == 0

    @pytest.mark.parametrize(
        "exc",
        [
            SolarError("kaputt"),
            aiohttp.ClientError("http"),
            TimeoutError(),
            KeyError("Kp"),                            # malformed API answer
        ],
    )
    async def test_falls_through_on_provider_errors(self, exc):
        chain = ChainedSolarProvider(_Fails(exc), _Ok("fallback"))
        assert await chain.fetch() == "fallback"

    async def test_total_failure_yields_user_message(self):
        chain = ChainedSolarProvider(_Fails(SolarError("a")), _Fails(TimeoutError()))
        assert "Solar data unavailable" in await chain.fetch()

    async def test_unexpected_exceptions_propagate(self):
        with pytest.raises(ZeroDivisionError):
            await ChainedSolarProvider(_Fails(ZeroDivisionError())).fetch()


class TestCache:
    """Solar data is global and slow-moving — a success is served from
    cache for the TTL, a failure is never cached."""

    async def test_second_fetch_is_served_from_cache(self):
        provider = _Ok("daten")
        chain = ChainedSolarProvider(provider)
        await chain.fetch()
        await chain.fetch()
        assert provider.calls == 1

    async def test_expired_cache_refetches(self):
        provider = _Ok("daten")
        chain = ChainedSolarProvider(provider)
        await chain.fetch()
        chain._cached_at -= 901          # age the entry past the TTL
        await chain.fetch()
        assert provider.calls == 2

    async def test_failure_is_not_cached(self):
        failing = _Fails(SolarError("down"))
        chain = ChainedSolarProvider(failing)
        await chain.fetch()
        await chain.fetch()
        assert failing.calls == 2        # tried again, no cached error


class TestHamQslFormatting:
    def test_compact_output(self):
        out = _format_hamqsl(_SAMPLE_XML)
        assert out.splitlines() == [
            "SFI 107  SSN 80  A 12  K 1 (VR QUIET)",
            "Day: 80-40 Fair, 30-20 Good, 17-15 Good, 12-10 Poor",
            "Night: 80-40 Good, 30-20 Good, 17-15 Poor, 12-10 Poor",
        ]

    def test_fits_one_dm(self):
        # Indices + day + night must pack into a single 150-byte message.
        assert len(_format_hamqsl(_SAMPLE_XML).encode()) <= 150

    def test_vhf_phenomena_are_ignored(self):
        assert "aurora" not in _format_hamqsl(_SAMPLE_XML).lower()

    def test_unparseable_xml_raises(self):
        with pytest.raises(SolarError):
            _format_hamqsl("<solar><oops")

    def test_missing_solardata_raises(self):
        with pytest.raises(SolarError):
            _format_hamqsl("<solar></solar>")

    def test_missing_flux_raises(self):
        with pytest.raises(SolarError):
            _format_hamqsl("<solar><solardata><kindex>1</kindex></solardata></solar>")

    def test_missing_band_conditions_yields_indices_only(self):
        out = _format_hamqsl(
            "<solar><solardata><solarflux>99</solarflux></solardata></solar>"
        )
        assert out.startswith("SFI 99")
        assert "\n" not in out


class TestNoaaFormatting:
    def test_indices_line(self):
        kp_rows = [
            {"time_tag": "t1", "Kp": 6.0, "a_running": 80, "station_count": 7},
            {"time_tag": "t2", "Kp": 1.33, "a_running": 12, "station_count": 8},
        ]
        flux_rows = [{"flux": 107, "time_tag": "t"}]
        assert _format_noaa(kp_rows, flux_rows) == "SFI 107  A 12  K 1"

    def test_uses_latest_kp_row(self):
        kp_rows = [
            {"Kp": 9.0, "a_running": 300},
            {"Kp": 2.67, "a_running": 15},
        ]
        assert "K 3" in _format_noaa(kp_rows, [{"flux": 100}])

    def test_empty_data_raises(self):
        with pytest.raises(SolarError):
            _format_noaa([], [{"flux": 100}])
        with pytest.raises(SolarError):
            _format_noaa([{"Kp": 1, "a_running": 5}], [])
