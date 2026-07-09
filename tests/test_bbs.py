"""Tests for hardware-independent parts of the main BBS class."""

import asyncio
import datetime
import logging
import time
from collections import deque

from bbs.bbs import (
    _RX_LOG_BUFFER,
    MeshCoreBBS,
    _parse_rx_log_data,
    _render_channel_text,
)


def _bare_bbs() -> MeshCoreBBS:
    """A MeshCoreBBS instance without hardware/config wiring."""
    bbs = MeshCoreBBS.__new__(MeshCoreBBS)
    bbs._rx_log_recent = deque(maxlen=_RX_LOG_BUFFER)
    bbs._bg_tasks = []
    return bbs


def _add(bbs: MeshCoreBBS, ptype: int, snr: int, age: float = 0.0) -> None:
    bbs._rx_log_recent.append(
        (time.monotonic() - age, {"payload_type": ptype, "snr": snr})
    )


class TestRxLogMatching:
    """Regression for review point 2.1: !ping must not report a foreign
    packet's signal data."""

    def test_advert_between_dm_and_fetch_is_skipped(self):
        bbs = _bare_bbs()
        _add(bbs, ptype=2, snr=8)    # the DM packet
        _add(bbs, ptype=4, snr=-3)   # an advert arriving in between
        assert bbs._claim_rx_log_for_dm()["snr"] == 8

    def test_back_to_back_dms_pair_fifo(self):
        bbs = _bare_bbs()
        _add(bbs, ptype=2, snr=8)
        _add(bbs, ptype=2, snr=12)
        assert bbs._claim_rx_log_for_dm()["snr"] == 8
        assert bbs._claim_rx_log_for_dm()["snr"] == 12
        assert bbs._claim_rx_log_for_dm() is None

    def test_stale_entries_are_not_claimed(self):
        bbs = _bare_bbs()
        _add(bbs, ptype=2, snr=8, age=120.0)
        assert bbs._claim_rx_log_for_dm() is None

    def test_only_foreign_traffic_yields_none(self):
        bbs = _bare_bbs()
        for ptype in (4, 3, 8):  # advert, ack, path
            _add(bbs, ptype=ptype, snr=1)
        assert bbs._claim_rx_log_for_dm() is None

    def test_buffer_is_bounded(self):
        bbs = _bare_bbs()
        for i in range(3 * _RX_LOG_BUFFER):
            _add(bbs, ptype=4, snr=i)
        assert len(bbs._rx_log_recent) == _RX_LOG_BUFFER


class TestChannelText:
    """Regression for review point 2.3: literal '%' must not crash."""

    def test_name_placeholder(self):
        assert _render_channel_text("at @[{name}].", "BBS") == "at @[BBS]."

    def test_legacy_percent_s(self):
        assert _render_channel_text("at @[%s].", "BBS") == "at @[BBS]."

    def test_literal_percent_survives(self):
        assert _render_channel_text("100% frei bei @[{name}]!", "BBS") == "100% frei bei @[BBS]!"


class TestNextAdvertTime:
    def test_next_time_is_in_the_future(self):
        ts = MeshCoreBBS._next_advert_time(["00:00", "12:00"])
        assert ts > time.time()
        # And at most 24h away.
        assert ts <= time.time() + 86400 + 1

    def test_picks_the_earliest_candidate(self):
        now = datetime.datetime.now(datetime.UTC)
        soon = (now + datetime.timedelta(minutes=5)).strftime("%H:%M")
        later = (now + datetime.timedelta(hours=5)).strftime("%H:%M")
        ts = MeshCoreBBS._next_advert_time([later, soon])
        assert ts - time.time() < 6 * 60


class TestParseRxLogData:
    def test_path_is_split_into_hops(self):
        parsed = _parse_rx_log_data(
            {"path": "ab12cd34", "path_len": 2, "path_hash_size": 2,
             "snr": 8, "rssi": -95, "recv_time": 1}
        )
        assert parsed["hops"] == 2
        assert parsed["path"] == ["ab12", "cd34"]

    def test_empty_path_means_direct(self):
        parsed = _parse_rx_log_data({"path": "", "path_len": 0})
        assert parsed["hops"] == 0 and parsed["path"] == []


class TestTaskSupervision:
    """Regression for review point 2.2: crashed tasks must be logged."""

    async def test_crash_is_logged_with_task_name(self, caplog):
        bbs = _bare_bbs()

        async def boom():
            raise ValueError("kaputt")

        with caplog.at_level(logging.ERROR, logger="bbs.bbs"):
            bbs._spawn(boom(), "advert_times")
            await asyncio.sleep(0.05)

        assert any(
            "advert_times" in r.message and "crashed" in r.message
            for r in caplog.records
        )

    async def test_cancel_stays_silent(self, caplog):
        bbs = _bare_bbs()

        async def sleeper():
            await asyncio.sleep(3600)

        with caplog.at_level(logging.ERROR, logger="bbs.bbs"):
            bbs._spawn(sleeper(), "room_timeout")
            bbs._bg_tasks[0].cancel()
            await asyncio.sleep(0.05)

        assert not any("crashed" in r.message for r in caplog.records)


class TestHeartbeat:
    """Review point 3.5: the heartbeat file drives the Docker HEALTHCHECK."""

    async def test_heartbeat_touches_file_next_to_db(self, tmp_path):
        import os
        import time as _time
        from types import SimpleNamespace

        import bbs.bbs as bbs_mod

        bbs = _bare_bbs()
        bbs._cfg = SimpleNamespace(
            bbs=SimpleNamespace(storage=SimpleNamespace(db_path=str(tmp_path / "bbs.db")))
        )
        # Speed the loop up for the test.
        orig = bbs_mod._HEARTBEAT_INTERVAL
        bbs_mod._HEARTBEAT_INTERVAL = 0.05
        try:
            bbs._spawn(bbs._heartbeat_task(), "heartbeat")
            await asyncio.sleep(0.02)
            hb = tmp_path / "heartbeat"
            assert hb.exists()

            first = os.path.getmtime(hb)
            # Backdate, then wait one interval: the task must re-touch.
            os.utime(hb, (first - 100, first - 100))
            await asyncio.sleep(0.1)
            assert os.path.getmtime(hb) >= _time.time() - 5
        finally:
            bbs_mod._HEARTBEAT_INTERVAL = orig
            bbs._bg_tasks[0].cancel()

    async def test_unwritable_directory_does_not_kill_the_task(self, tmp_path, caplog):
        from types import SimpleNamespace

        import bbs.bbs as bbs_mod

        bbs = _bare_bbs()
        bbs._cfg = SimpleNamespace(
            bbs=SimpleNamespace(
                storage=SimpleNamespace(db_path=str(tmp_path / "missing" / "bbs.db"))
            )
        )
        orig = bbs_mod._HEARTBEAT_INTERVAL
        bbs_mod._HEARTBEAT_INTERVAL = 0.05
        try:
            with caplog.at_level(logging.ERROR, logger="bbs.bbs"):
                bbs._spawn(bbs._heartbeat_task(), "heartbeat")
                await asyncio.sleep(0.12)
            # Errors are logged, but the supervised task keeps running.
            assert any("heartbeat" in r.message.lower() for r in caplog.records)
            assert not bbs._bg_tasks[0].done()
        finally:
            bbs_mod._HEARTBEAT_INTERVAL = orig
            bbs._bg_tasks[0].cancel()
