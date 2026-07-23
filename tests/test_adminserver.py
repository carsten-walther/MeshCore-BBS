"""Tests for the admin Unix-socket RPC server and its bbs.py handlers."""

import asyncio
import json
import shutil
import socket as socket_mod
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from meshcore import EventType

import admin
from bbs.adminserver import AdminServer, socket_path
from bbs.bbs import MeshCoreBBS
from bbs.config import ChannelConfig


@pytest.fixture
def sock_dir():
    # AF_UNIX socket paths are limited to ~104 bytes on macOS — pytest's
    # tmp_path can exceed that, so use the (short) system temp dir directly.
    d = tempfile.mkdtemp(prefix="bbs-admin-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _cfg(sock_dir: Path) -> SimpleNamespace:
    """The minimal config shape admin._rpc needs to derive the socket path."""
    return SimpleNamespace(
        bbs=SimpleNamespace(storage=SimpleNamespace(db_path=str(sock_dir / "bbs.db")))
    )


async def _echo(args: dict) -> object:
    return args


async def _boom(args: dict) -> object:
    raise RuntimeError("kaputt")


def _make_server(sock_dir: Path) -> AdminServer:
    return AdminServer(socket_path(sock_dir / "bbs.db"), {"echo": _echo, "boom": _boom})


async def _call(cfg, cmd: str, args: dict | None = None):
    """Run the synchronous admin-CLI client without blocking the event loop
    that is serving the request."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: admin._rpc(cfg, cmd, args))


def _send_raw(path: Path, data: bytes) -> bytes:
    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
        sock.settimeout(5.0)
        sock.connect(str(path))
        sock.sendall(data)
        return sock.makefile("rb").readline()


class TestAdminServer:
    async def test_roundtrip(self, sock_dir):
        server = _make_server(sock_dir)
        await server.start()
        try:
            assert await _call(_cfg(sock_dir), "echo", {"x": 1}) == {"x": 1}
        finally:
            await server.stop()

    async def test_unknown_command(self, sock_dir):
        server = _make_server(sock_dir)
        await server.start()
        try:
            with pytest.raises(admin._RpcError, match="unknown command"):
                await _call(_cfg(sock_dir), "nope")
        finally:
            await server.stop()

    async def test_handler_exception_becomes_error_response(self, sock_dir):
        server = _make_server(sock_dir)
        await server.start()
        try:
            with pytest.raises(admin._RpcError, match="kaputt"):
                await _call(_cfg(sock_dir), "boom")
        finally:
            await server.stop()

    async def test_invalid_json_request(self, sock_dir):
        server = _make_server(sock_dir)
        await server.start()
        try:
            loop = asyncio.get_running_loop()
            line = await loop.run_in_executor(
                None, _send_raw, server._path, b"not json\n"
            )
            response = json.loads(line)
            assert response["ok"] is False
            assert "invalid JSON" in response["error"]
        finally:
            await server.stop()

    async def test_stale_socket_file_is_replaced(self, sock_dir):
        path = socket_path(sock_dir / "bbs.db")
        path.touch()  # leftover from a crashed run
        server = _make_server(sock_dir)
        await server.start()
        try:
            assert await _call(_cfg(sock_dir), "echo", {}) == {}
        finally:
            await server.stop()

    async def test_socket_is_private(self, sock_dir):
        server = _make_server(sock_dir)
        await server.start()
        try:
            mode = stat.S_IMODE(server._path.stat().st_mode)
            assert mode == 0o600
        finally:
            await server.stop()

    async def test_stop_removes_socket(self, sock_dir):
        server = _make_server(sock_dir)
        await server.start()
        path = server._path
        await server.stop()
        assert not path.exists()

    async def test_client_reports_bbs_not_running(self, sock_dir):
        with pytest.raises(admin._RpcError, match="is it running"):
            await _call(_cfg(sock_dir), "echo")


class _FakeResult:
    def __init__(self, payload: object, error: bool = False) -> None:
        self.type = EventType.ERROR if error else "ok"
        self.payload = payload


def _bare_bbs(mc=None, cfg=None) -> MeshCoreBBS:
    bbs = MeshCoreBBS.__new__(MeshCoreBBS)
    bbs._mc = mc
    bbs._cfg = cfg
    return bbs


class TestBbsHandlers:
    def test_handlers_cover_the_documented_commands(self):
        handlers = _bare_bbs()._admin_handlers()
        assert set(handlers) == {
            "contacts", "device-info", "advert", "advert-channels", "advert-channel",
        }

    async def test_contacts_selects_json_safe_fields(self):
        contact = {
            "public_key": "ab" * 32,
            "adv_name": "Alice",
            "type": 1,
            "flags": 0,
            "last_advert": 1700000000,
            "adv_lat": 51.34,
            "adv_lon": 12.37,
            "out_path_len": 2,
            "out_path": "ab12cd34",
            "lastmod": 1700000000,
            "raw_frame": b"\x00\x01",  # must NOT leak into the JSON reply
        }

        async def get_contacts():
            return _FakeResult({"ab12": contact})

        mc = SimpleNamespace(commands=SimpleNamespace(get_contacts=get_contacts))
        result = await _bare_bbs(mc=mc)._admin_contacts({})

        assert result == [{k: v for k, v in contact.items() if k != "raw_frame"}]
        json.dumps(result)  # the reply must be serializable as-is

    async def test_contacts_device_error_raises(self):
        async def get_contacts():
            return _FakeResult("nope", error=True)

        mc = SimpleNamespace(commands=SimpleNamespace(get_contacts=get_contacts))
        with pytest.raises(RuntimeError, match="Could not fetch contacts"):
            await _bare_bbs(mc=mc)._admin_contacts({})

    async def test_advert_passes_explicit_flood_flag(self):
        sent = {}

        async def send_advert(flood):
            sent["flood"] = flood
            return _FakeResult({})

        mc = SimpleNamespace(commands=SimpleNamespace(send_advert=send_advert))
        cfg = SimpleNamespace(bbs=SimpleNamespace(advert=SimpleNamespace(flood=False)))
        message = await _bare_bbs(mc=mc, cfg=cfg)._admin_advert({"flood": True})
        assert sent["flood"] is True
        assert "Flood" in message

    async def test_advert_defaults_to_configured_flood(self):
        sent = {}

        async def send_advert(flood):
            sent["flood"] = flood
            return _FakeResult({})

        mc = SimpleNamespace(commands=SimpleNamespace(send_advert=send_advert))
        cfg = SimpleNamespace(bbs=SimpleNamespace(advert=SimpleNamespace(flood=True)))
        await _bare_bbs(mc=mc, cfg=cfg)._admin_advert({})
        assert sent["flood"] is True

    async def test_advert_channels_requires_configured_channels(self):
        cfg = SimpleNamespace(bbs=SimpleNamespace(channels=[]))
        with pytest.raises(RuntimeError, match="No channels configured"):
            await _bare_bbs(cfg=cfg)._admin_advert_channels({})

    async def test_device_info_includes_identity(self):
        async def send_device_query():
            return _FakeResult({"model": "Heltec V3 ", "ver": "1.7.1"})

        async def stats_error():
            return _FakeResult({}, error=True)

        mc = SimpleNamespace(
            commands=SimpleNamespace(
                send_device_query=send_device_query,
                get_stats_core=stats_error,
                get_stats_radio=stats_error,
                get_stats_packets=stats_error,
            ),
            self_info={"name": "📬 BBS", "public_key": "ab" * 32},
        )
        info = await _bare_bbs(mc=mc)._admin_device_info({})
        assert info["name"] == "📬 BBS"
        assert info["public_key"] == "ab" * 32
        assert info["model"] == "Heltec V3"
        assert info["firmware_version"] == "1.7.1"

    async def test_send_channel_adverts_reports_sent_channels(self):
        calls = []

        async def send_device_query():
            return _FakeResult({"max_channels": 2})

        async def get_channel(idx):
            return _FakeResult({"channel_name": "test" if idx == 0 else ""})

        async def send_chan_msg(idx, msg):
            calls.append((idx, msg))
            return _FakeResult({})

        mc = SimpleNamespace(
            commands=SimpleNamespace(
                send_device_query=send_device_query,
                get_channel=get_channel,
                send_chan_msg=send_chan_msg,
            )
        )
        cfg = SimpleNamespace(
            bbs=SimpleNamespace(
                name="BBS",
                channels=[ChannelConfig(name="test", text="Hi from {name}")],
            )
        )
        sent = await _bare_bbs(mc=mc, cfg=cfg)._admin_advert_channels({})
        assert sent == ["test"]
        assert calls == [(0, "Hi from BBS")]

    async def test_advert_channel_sends_ad_hoc_text(self):
        calls = []

        async def send_device_query():
            return _FakeResult({"max_channels": 2})

        async def get_channel(idx):
            return _FakeResult({"channel_name": "leipzig" if idx == 0 else ""})

        async def send_chan_msg(idx, msg):
            calls.append((idx, msg))
            return _FakeResult({})

        mc = SimpleNamespace(
            commands=SimpleNamespace(
                send_device_query=send_device_query,
                get_channel=get_channel,
                send_chan_msg=send_chan_msg,
            )
        )
        cfg = SimpleNamespace(bbs=SimpleNamespace(name="BBS", channels=[]))
        name = await _bare_bbs(mc=mc, cfg=cfg)._admin_advert_channel(
            {"channel": "leipzig", "text": "Hello @[{name}]."}
        )
        assert name == "leipzig"
        assert calls == [(0, "Hello @[BBS].")]

    async def test_advert_channel_requires_channel_and_text(self):
        bbs = _bare_bbs(cfg=SimpleNamespace(bbs=SimpleNamespace(name="BBS", channels=[])))
        with pytest.raises(RuntimeError, match="channel name is required"):
            await bbs._admin_advert_channel({"text": "hi"})
        with pytest.raises(RuntimeError, match="message is required"):
            await bbs._admin_advert_channel({"channel": "leipzig"})
