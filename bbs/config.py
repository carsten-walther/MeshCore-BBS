"""Configuration loader for the MeshCore BBS."""

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

_LOGGER = logging.getLogger(__name__)


@dataclass
class TcpConfig:
    """Settings for connection.type == "tcp" (e.g. a MeshCore companion
    radio reachable over the network rather than directly via USB)."""
    host: str = "127.0.0.1"
    port: int = 30193


@dataclass
class SerialConfig:
    """Settings for connection.type == "serial" — a MeshCore device
    attached directly via USB/UART."""
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200


@dataclass
class BleConfig:
    """Settings for connection.type == "ble"."""
    address: str = "MeshCore"
    pin: str = "123456"


@dataclass
class ConnectionConfig:
    """Selects and configures how the bot talks to the MeshCore device.
    Only the section matching `type` is actually used by
    connection.create_connection(); the other two stay populated with
    their defaults but are ignored.
    """
    type: str = "serial"
    tcp: TcpConfig = field(default_factory=TcpConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    ble: BleConfig = field(default_factory=BleConfig)


@dataclass
class BbsConfig:
    """Behavioral settings for the bbs itself."""
    name: str = "📬 BBS"
    db_path: str = "bbs.db"
    advert: bool = True
    advert_flood: bool = False
    rooms: list[str] = field(default_factory=lambda: ["lobby"])
    # Minutes of inactivity before a user is auto-removed from a room.
    # Inactivity means no !join, !post, or !read in that room. Set to 0 to disable.
    room_timeout: int = 60


@dataclass
class RadioConfig:
    """LoRa radio parameters applied to the device on startup via
    set_radio() and set_tx_power(). Values use the same units that
    the MeshCore companion protocol expects (frequency in kHz,
    bandwidth in Hz, spreading factor and coding rate as integers).
    Set any field to None to leave the device's current value untouched.

    Example values: frequency=869.618, bandwidth=62.5, spreading_factor=8,
    coding_rate=8, tx_power=22.
    """
    frequency: float | None = None
    bandwidth: float | None = None
    spreading_factor: int | None = None
    coding_rate: int | None = None
    tx_power: int | None = None


@dataclass
class AppConfig:
    """Top-level configuration object, populated by load_config() and
    passed around the rest of the bot (connection.py, bbs.py, ...).
    """
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    bbs: BbsConfig = field(default_factory=BbsConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load configuration from a YAML file.

    If the file doesn't exist, it is created with default values and those
    defaults are returned. Falls back to defaults for any missing keys
    within an existing file.
    """
    config_path = Path(path)

    if not config_path.exists():
        _LOGGER.warning(
            f"Config file not found: {config_path.resolve()} — creating it with default values."
        )
        default_config = AppConfig()
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(asdict(default_config), f, sort_keys=False, allow_unicode=True)

        return default_config

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Each section is pulled out into its own dict first (defaulting to {})
    # so the nested .get() calls below don't need repeated
    # raw.get("section", {}) boilerplate or risk a KeyError/AttributeError
    # on a partially-filled config.yaml.
    conn_raw = raw.get("connection", {})
    tcp_raw = conn_raw.get("tcp", {})
    serial_raw = conn_raw.get("serial", {})
    ble_raw = conn_raw.get("ble", {})

    radio_raw = raw.get("radio", {})
    bbs_raw = raw.get("bbs", {})

    connection = ConnectionConfig(
        type=conn_raw.get("type", "serial"),
        tcp=TcpConfig(
            host=tcp_raw.get("host", TcpConfig.host),
            port=tcp_raw.get("port", TcpConfig.port),
        ),
        serial=SerialConfig(
            port=serial_raw.get("port", SerialConfig.port),
            baudrate=serial_raw.get("baudrate", SerialConfig.baudrate),
        ),
        ble=BleConfig(
            address=ble_raw.get("address", BleConfig.address),
            pin=ble_raw.get("pin", BleConfig.pin),
        ),
    )

    bbs = BbsConfig(
        name=bbs_raw.get("name", BbsConfig.name),
        db_path=bbs_raw.get("db_path", BbsConfig.db_path),
        advert=bbs_raw.get("advert", BbsConfig.advert),
        advert_flood=bbs_raw.get("advert_flood", BbsConfig.advert_flood),
        rooms=bbs_raw.get("rooms", ["lobby"]),
        room_timeout=int(bbs_raw.get("room_timeout", BbsConfig.room_timeout)),
    )

    radio = RadioConfig(
        frequency=float(v) if (v := radio_raw.get("frequency")) is not None else None,
        bandwidth=float(v) if (v := radio_raw.get("bandwidth")) is not None else None,
        spreading_factor=int(v) if (v := radio_raw.get("spreading_factor")) is not None else None,
        coding_rate=int(v) if (v := radio_raw.get("coding_rate")) is not None else None,
        tx_power=int(v) if (v := radio_raw.get("tx_power")) is not None else None,
    )

    return AppConfig(connection=connection, radio=radio, bbs=bbs)