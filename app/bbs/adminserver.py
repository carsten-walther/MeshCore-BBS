"""Unix-socket RPC server exposing device actions to the admin CLI.

The admin CLI (app/admin.py) reads the SQLite database directly, but the
radio has exactly one connection — held by the running BBS process. Device
actions (contact list, adverts, device info) therefore go through this
server: a Unix domain socket next to the database, i.e. inside the shared
/data volume, reachable both via `docker exec` and from the host.

Protocol: one request per connection, newline-delimited JSON.

    -> {"cmd": "contacts", "args": {}}
    <- {"ok": true, "data": [...]}      or      {"ok": false, "error": "..."}

Handlers are plain async callables injected by bbs.py, so this module has
no MeshCore dependency and is unit-testable with fakes.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SOCKET_NAME = "admin.sock"

_REQUEST_TIMEOUT = 30.0   # seconds to wait for the client's request line
_HANDLER_TIMEOUT = 60.0   # seconds a handler may take (device round-trips)

Handler = Callable[[dict], Awaitable[object]]


def socket_path(db_path: str | Path) -> Path:
    """The admin socket lives next to the database, so both the BBS and
    the admin CLI derive the same path from config alone."""
    return Path(db_path).parent / SOCKET_NAME


class AdminServer:
    def __init__(self, path: str | Path, handlers: dict[str, Handler]) -> None:
        self._path = Path(path)
        self._handlers = handlers
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        # A stale socket file from a crashed run would make bind() fail.
        self._path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._path)
        )
        os.chmod(self._path, 0o600)
        _LOGGER.info(f"Admin socket listening on {self._path}.")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._path.unlink(missing_ok=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            response = await self._respond(reader)
        except Exception:
            # The server must survive any request — a broken admin call
            # must never take the BBS down with it.
            _LOGGER.exception("Admin request failed unexpectedly.")
            response = {"ok": False, "error": "internal error (see BBS log)"}
        try:
            raw = json.dumps(response, ensure_ascii=False)
        except (TypeError, ValueError):
            _LOGGER.exception("Admin handler returned non-JSON-serializable data.")
            raw = json.dumps({"ok": False, "error": "handler returned unserializable data"})
        try:
            writer.write(raw.encode() + b"\n")
            await writer.drain()
        except OSError:
            _LOGGER.debug("Admin client disconnected before the response was sent.")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _respond(self, reader: asyncio.StreamReader) -> dict:
        try:
            line = await asyncio.wait_for(reader.readline(), _REQUEST_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "error": "timed out waiting for request"}
        except ValueError:  # StreamReader line-length limit exceeded
            return {"ok": False, "error": "request too large"}

        try:
            request = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"invalid JSON request: {e}"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}

        cmd = request.get("cmd")
        handler = self._handlers.get(cmd) if isinstance(cmd, str) else None
        if handler is None:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        args = request.get("args")
        if not isinstance(args, dict):
            args = {}

        try:
            data = await asyncio.wait_for(handler(args), _HANDLER_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "error": f"'{cmd}' timed out after {_HANDLER_TIMEOUT:.0f}s"}
        except Exception as e:
            _LOGGER.warning(f"Admin command '{cmd}' failed: {e}")
            return {"ok": False, "error": str(e) or type(e).__name__}
        return {"ok": True, "data": data}
