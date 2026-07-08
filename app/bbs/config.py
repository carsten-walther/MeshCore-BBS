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
    port: int = 5000


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
    latitude: float = 0.0
    longitude: float = 0.0
    db_path: str = "bbs.db"
    flood_scope: str = ""
    advert: bool = True
    advert_flood: bool = False
    advert_times: list[str] = field(default_factory=list)
    advert_in_channels_text: str = "Store and forward messages at @[%s]."
    advert_in_channels: list[str] = field(default_factory=lambda: [])
    advert_in_channels_times: list[str] = field(default_factory=list)
    admin_pubkeys: list[str] = field(default_factory=list)
    inbox_notify_interval: int = 120
    post_ttl_days: int = 14
    log_file: str = ""
    log_backup_count: int = 7
    rooms: list[str] = field(default_factory=lambda: ["lobby"])
    # Minutes of inactivity before a user is auto-removed from a room.
    # Inactivity means no !join, !post, or !read in that room. Set to 0 to disable.
    room_timeout: int = 60
    # Default location for !weather with no argument (e.g. "Berlin" or "52.52,13.41").
    # Leave empty to require the user to always provide a location.
    weather_location: str = "Leipzig"
    additional_commands: list[str] = field(default_factory=lambda: ["weather", "ping"])
    # Pause between consecutive DMs in a paginated reply (seconds).
    inter_msg_delay: float = 2.0
    # Maximum byte length of a single outgoing DM.
    max_msg_len: int = 150
    # Number of users shown by !users.
    user_list_limit: int = 5


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
    """MQTT publishing settings (compatible with meshcore-packet-capture topics)."""
    iata: str = "LOC"
    brokers: list[MqttBrokerConfig] = field(default_factory=list)


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
    frequency: float = 869.618
    bandwidth: float = 62.5
    spreading_factor: int = 8
    coding_rate: int = 8
    tx_power: int = 22


@dataclass
class AppConfig:
    """Top-level configuration object, populated by load_config() and
    passed around the rest of the bot (connection.py, bbs.py, ...).
    """
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    bbs: BbsConfig = field(default_factory=BbsConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)


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
    mqtt_raw = raw.get("mqtt", {})

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
        db_path=bbs_raw.get("db_path", BbsConfig.db_path),
        flood_scope=bbs_raw.get("flood_scope", BbsConfig.flood_scope),
        advert=bbs_raw.get("advert", BbsConfig.advert),
        advert_flood=bbs_raw.get("advert_flood", BbsConfig.advert_flood),
        advert_times=bbs_raw.get("advert_times", []),
        advert_in_channels_text=bbs_raw.get("advert_in_channels_text", BbsConfig.advert_in_channels_text),
        advert_in_channels=bbs_raw.get("advert_in_channels", []),
        advert_in_channels_times=bbs_raw.get("advert_in_channels_times", []),
        admin_pubkeys=bbs_raw.get("admin_pubkeys", []),
        inbox_notify_interval=int(bbs_raw.get("inbox_notify_interval", BbsConfig.inbox_notify_interval)),
        post_ttl_days=int(bbs_raw.get("post_ttl_days", BbsConfig.post_ttl_days)),
        log_file=bbs_raw.get("log_file", BbsConfig.log_file),
        log_backup_count=int(bbs_raw.get("log_backup_count", BbsConfig.log_backup_count)),
        rooms=bbs_raw.get("rooms", ["lobby"]),
        room_timeout=int(bbs_raw.get("room_timeout", BbsConfig.room_timeout)),
        weather_location=bbs_raw.get("weather_location", BbsConfig.weather_location),
        additional_commands=bbs_raw.get("additional_commands", ["weather", "ping"]),
        inter_msg_delay=float(bbs_raw.get("inter_msg_delay", BbsConfig.inter_msg_delay)),
        max_msg_len=int(bbs_raw.get("max_msg_len", BbsConfig.max_msg_len)),
        user_list_limit=int(bbs_raw.get("user_list_limit", BbsConfig.user_list_limit)),
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
                qos=int(b.get("qos", MqttBrokerConfig.qos)),
                retain=b.get("retain", MqttBrokerConfig.retain),
                keepalive=int(b.get("keepalive", MqttBrokerConfig.keepalive)),
            )
            for b in mqtt_raw.get("brokers", [])
        ],
    )

    return AppConfig(connection=connection, radio=radio, bbs=bbs, mqtt=mqtt)