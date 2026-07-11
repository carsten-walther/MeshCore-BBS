"""Configuration loader for the MeshCore BBS."""

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from bbs.messages import SUPPORTED_LANGUAGES

_LOGGER = logging.getLogger(__name__)

# app/bbs/config.py is located in app/bbs/ → parents[2] is the repo root.
# In the container (/app/bbs/config.py), this results in "/" — and so
# the relative defaults point exactly to the volumes /config and /data.
_APP_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = _APP_ROOT / "config" / "config.yaml"

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _resolve(p: str) -> str:
    """Anchor a relative path at the repo/app root so it doesn't depend
    on the current working directory. Absolute paths and the empty
    string (= feature disabled, e.g. logging.file) pass through."""
    if not p:
        return p
    path = Path(p)
    return str(path if path.is_absolute() else _APP_ROOT / path)


def _valid_language(raw: object) -> str:
    """Normalize the UI language; unknown values fall back to English."""
    lang = str(raw).strip().lower()
    if lang in SUPPORTED_LANGUAGES:
        return lang
    _LOGGER.warning(f"bbs.language: unsupported language {raw!r} — using 'en'.")
    return "en"


def _valid_log_level(raw) -> str:
    """Normalize the log level; fall back to INFO on unknown values."""
    level = str(raw).strip().upper()
    if level in _LOG_LEVELS:
        return level
    _LOGGER.warning(f"bbs.logging.level: invalid level {raw!r} — using INFO.")
    return "INFO"


@dataclass
class TcpConfig:
    """Settings for connection.type == "tcp"."""
    host: str = "127.0.0.1"
    port: int = 5000


@dataclass
class SerialConfig:
    """Settings for connection.type == "serial"."""
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200


@dataclass
class BleConfig:
    """Settings for connection.type == "ble"."""
    address: str = "MeshCore"
    pin: str = "123456"


@dataclass
class ConnectionConfig:
    """Selects and configures how the bot talks to the MeshCore device."""
    type: str = "serial"
    tcp: TcpConfig = field(default_factory=TcpConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    ble: BleConfig = field(default_factory=BleConfig)


@dataclass
class AdvertConfig:
    """Startup and scheduled advert settings."""
    enabled: bool = True
    flood: bool = False
    times: list[str] = field(default_factory=list)
    flood_scope: str = ""


@dataclass
class ChannelsConfig:
    """Periodic channel advert settings."""
    text: str = "Store and forward messages at @[{name}]."
    names: list[str] = field(default_factory=list)
    times: list[str] = field(default_factory=list)


@dataclass
class RoomsConfig:
    """Room availability and inactivity timeout."""
    names: list[str] = field(default_factory=lambda: ["lobby"])
    timeout: int = 60
    undo_window: int = 600  # seconds a post stays !undo-able (0 = no time limit)


@dataclass
class MessagingConfig:
    """DM delivery and inbox notification settings."""
    max_len: int = 150
    inter_delay: float = 2.0
    inbox_notify_interval: int = 120
    user_list_limit: int = 5
    read_limit: int = 5


@dataclass
class StorageConfig:
    """Database path and post expiry settings."""
    db_path: str = "data/bbs.db"
    post_ttl_days: int = 14
    signal_ttl_days: int = 30


@dataclass
class LoggingConfig:
    """Log file and rotation settings."""
    file: str = "data/bbs.log"
    backup_count: int = 7
    level: str = "INFO"


@dataclass
class FeaturesConfig:
    """Optional commands and weather default location."""
    commands: list[str] = field(default_factory=lambda: ["weather", "ping"])
    weather_location: str = "Leipzig"


@dataclass
class BbsConfig:
    """Behavioral settings for the BBS."""
    name: str = "📬 BBS"
    latitude: float = 0.0
    longitude: float = 0.0
    language: str = "en"
    strings: dict[str, str] = field(default_factory=dict)
    advert: AdvertConfig = field(default_factory=AdvertConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    rooms: RoomsConfig = field(default_factory=RoomsConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)


@dataclass
class MqttBrokerConfig:
    """Settings for one MQTT broker connection."""
    enabled: bool = True
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    tls_verify: bool = True
    qos: int = 0
    retain: bool = False
    keepalive: int = 60


@dataclass
class MqttConfig:
    """MQTT publishing settings."""
    iata: str = "LOC"
    brokers: list[MqttBrokerConfig] = field(default_factory=list)


@dataclass
class RadioConfig:
    """LoRa radio parameters applied to the device on startup.

    None means: leave the device's current value unchanged. This is the
    default, so a freshly auto-created config never silently overwrites
    the settings already flashed on the radio."""
    frequency: float | None = None
    bandwidth: float | None = None
    spreading_factor: int | None = None
    coding_rate: int | None = None
    tx_power: int | None = None


@dataclass
class AppConfig:
    """Top-level configuration object."""
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    bbs: BbsConfig = field(default_factory=BbsConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)


def _valid_times(raw: list, section: str) -> list[str]:
    """Validate and normalize 'HH:MM' schedule entries.

    PyYAML (YAML 1.1) parses unquoted times like 21:00 as sexagesimal
    integers (1260) — convert those back instead of crashing later in
    _next_advert_time(). Invalid entries are dropped with a warning so a
    typo can't silently kill a background task."""
    valid: list[str] = []
    for entry in raw:
        if isinstance(entry, int) and not isinstance(entry, bool) and 0 <= entry < 1440:
            t = f"{entry // 60:02d}:{entry % 60:02d}"
            _LOGGER.warning(
                f"{section}: unquoted time {entry} was read as a YAML integer — "
                f"interpreting as '{t}'. Quote times ('09:00') to avoid this."
            )
            valid.append(t)
            continue
        s = str(entry).strip()
        try:
            h_str, m_str = s.split(":")
            h, m = int(h_str), int(m_str)
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(f"{h:02d}:{m:02d}")
                continue
        except ValueError:
            pass
        _LOGGER.warning(f"{section}: ignoring invalid time {entry!r} (expected 'HH:MM').")
    return valid


def _valid_qos(raw, label: str) -> int:
    qos = int(raw)
    if qos not in (0, 1, 2):
        _LOGGER.warning(f"{label}: invalid MQTT qos {qos} — using 0.")
        return 0
    return qos


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load configuration from a YAML file.

    Creates the file with defaults if missing. Falls back to defaults for
    any missing keys within an existing file.
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

        # The file contains relative paths; resolve these at runtime.
        default_config.bbs.storage.db_path = _resolve(default_config.bbs.storage.db_path)
        default_config.bbs.logging.file = _resolve(default_config.bbs.logging.file)
        return default_config

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    conn_raw     = raw.get("connection", {})
    tcp_raw      = conn_raw.get("tcp", {})
    serial_raw   = conn_raw.get("serial", {})
    ble_raw      = conn_raw.get("ble", {})
    radio_raw    = raw.get("radio", {})
    bbs_raw      = raw.get("bbs", {})
    mqtt_raw     = raw.get("mqtt", {})

    advert_raw   = bbs_raw.get("advert", {})
    channels_raw = bbs_raw.get("channels", {})
    rooms_raw    = bbs_raw.get("rooms", {})
    msg_raw      = bbs_raw.get("messaging", {})
    storage_raw  = bbs_raw.get("storage", {})
    logging_raw  = bbs_raw.get("logging", {})
    features_raw = bbs_raw.get("features", {})

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
        latitude=bbs_raw.get("latitude", BbsConfig.latitude),
        longitude=bbs_raw.get("longitude", BbsConfig.longitude),
        language=_valid_language(bbs_raw.get("language", BbsConfig.language)),
        strings={str(k): str(v) for k, v in (bbs_raw.get("strings") or {}).items()},
        advert=AdvertConfig(
            enabled=advert_raw.get("enabled", AdvertConfig.enabled),
            flood=advert_raw.get("flood", AdvertConfig.flood),
            times=_valid_times(advert_raw.get("times", []), "bbs.advert.times"),
            flood_scope=advert_raw.get("flood_scope", AdvertConfig.flood_scope),
        ),
        channels=ChannelsConfig(
            text=channels_raw.get("text", ChannelsConfig.text),
            names=channels_raw.get("names", []),
            times=_valid_times(channels_raw.get("times", []), "bbs.channels.times"),
        ),
        rooms=RoomsConfig(
            names=rooms_raw.get("names", ["lobby"]),
            timeout=int(rooms_raw.get("timeout", RoomsConfig.timeout)),
            undo_window=max(0, int(rooms_raw.get("undo_window", RoomsConfig.undo_window))),
        ),
        messaging=MessagingConfig(
            max_len=int(msg_raw.get("max_len", MessagingConfig.max_len)),
            inter_delay=float(msg_raw.get("inter_delay", MessagingConfig.inter_delay)),
            inbox_notify_interval=int(msg_raw.get("inbox_notify_interval", MessagingConfig.inbox_notify_interval)),
            user_list_limit=int(msg_raw.get("user_list_limit", MessagingConfig.user_list_limit)),
        ),
        storage=StorageConfig(
            db_path=_resolve(storage_raw.get("db_path", StorageConfig.db_path)),
            post_ttl_days=int(storage_raw.get("post_ttl_days", StorageConfig.post_ttl_days)),
            signal_ttl_days=int(storage_raw.get("signal_ttl_days", StorageConfig.signal_ttl_days)),
        ),
        logging=LoggingConfig(
            file=_resolve(logging_raw.get("file", LoggingConfig.file)),
            backup_count=int(logging_raw.get("backup_count", LoggingConfig.backup_count)),
            level=_valid_log_level(logging_raw.get("level", LoggingConfig.level)),
        ),
        features=FeaturesConfig(
            commands=features_raw.get("commands", ["weather", "ping"]),
            weather_location=features_raw.get("weather_location", FeaturesConfig.weather_location),
        ),
    )

    radio = RadioConfig(
        frequency=float(v) if (v := radio_raw.get("frequency")) is not None else None,
        bandwidth=float(v) if (v := radio_raw.get("bandwidth")) is not None else None,
        spreading_factor=int(v) if (v := radio_raw.get("spreading_factor")) is not None else None,
        coding_rate=int(v) if (v := radio_raw.get("coding_rate")) is not None else None,
        tx_power=int(v) if (v := radio_raw.get("tx_power")) is not None else None,
    )

    mqtt = MqttConfig(
        iata=mqtt_raw.get("iata", MqttConfig.iata),
        brokers=[
            MqttBrokerConfig(
                enabled=b.get("enabled", MqttBrokerConfig.enabled),
                host=b.get("host", MqttBrokerConfig.host),
                port=int(b.get("port", MqttBrokerConfig.port)),
                username=b.get("username", MqttBrokerConfig.username),
                password=b.get("password", MqttBrokerConfig.password),
                tls=b.get("tls", MqttBrokerConfig.tls),
                tls_verify=b.get("tls_verify", MqttBrokerConfig.tls_verify),
                qos=_valid_qos(b.get("qos", MqttBrokerConfig.qos), b.get("host", "?")),
                retain=b.get("retain", MqttBrokerConfig.retain),
                keepalive=int(b.get("keepalive", MqttBrokerConfig.keepalive)),
            )
            for b in mqtt_raw.get("brokers", [])
        ],
    )

    return AppConfig(connection=connection, radio=radio, bbs=bbs, mqtt=mqtt)
