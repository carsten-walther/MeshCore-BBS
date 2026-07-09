"""MeshCore connection factory."""

import logging

from meshcore import MeshCore

from bbs.config import AppConfig

_LOGGER = logging.getLogger(__name__)


async def create_connection(cfg: AppConfig) -> MeshCore:
    """Create and return a MeshCore connection based on the configured type.

    Each branch wraps its create_* call in try/except purely for logging:
    the underlying meshcore library already raises on failure (rather than
    returning None), so this doesn't change control flow — it just attaches
    a clear, transport-specific error message before the exception
    propagates to the caller.
    """
    conn = cfg.connection

    if conn.type == "tcp":
        _LOGGER.info(
            f"Connecting via TCP to {conn.tcp.host}:{conn.tcp.port}"
        )
        try:
            mc = await MeshCore.create_tcp(
                host=conn.tcp.host,
                port=conn.tcp.port,
                debug=False,
                auto_reconnect=True
            )
        except Exception as exc:
            _LOGGER.error(
                f"TCP connection to {conn.tcp.host}:{conn.tcp.port} failed: {exc}"
            )
            raise

    elif conn.type == "serial":
        _LOGGER.info(
            f"Connecting via Serial on {conn.serial.port} @ {conn.serial.baudrate} baud"
        )
        try:
            mc = await MeshCore.create_serial(
                port=conn.serial.port,
                baudrate=conn.serial.baudrate,
                debug=False,
                auto_reconnect=True
            )
        except Exception as exc:
            _LOGGER.error(
                f"Serial connection on {conn.serial.port} failed: {exc}"
            )
            raise

    elif conn.type == "ble":
        _LOGGER.info(
            f"Connecting via BLE to device '{conn.ble.address}'"
        )
        try:
            mc = await MeshCore.create_ble(
                address=conn.ble.address,
                pin=conn.ble.pin,
                debug=False,
                auto_reconnect=True
            )
        except Exception as exc:
            _LOGGER.error(
                f"BLE connection to '{conn.ble.address}' failed: {exc}"
            )
            raise

    else:
        # Reached if cfg.connection.type in config.yaml is misspelled or
        # uses an unsupported transport — fail fast with a clear message
        # instead of silently doing nothing.
        _LOGGER.error(
            f"Unknown connection type: '{conn.type}'. Valid options: tcp, serial, ble."
        )
        raise ValueError(f"Unknown connection type: '{conn.type}'. Valid options: tcp, serial, ble.")

    # Kept as a safety net: some MeshCore versions/transports may still
    # return None on failure instead of raising.
    if mc is None:
        _LOGGER.error(
            f"Failed to connect via {conn.type} — device did not respond."
        )
        raise RuntimeError(f"Failed to connect via {conn.type} — device did not respond.")

    return mc
