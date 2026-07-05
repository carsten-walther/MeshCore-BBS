# MeshCore 📬 BBS

A store-and-forward bulletin board that runs on a MeshCore **companion** radio
over a LoRa mesh. Users interact entirely via direct messages (DMs) using a
simple `!command` protocol — no special app support required.

## How it works

The BBS attaches to a MeshCore device running **Companion** firmware via USB
serial, TCP, or BLE. It listens for incoming DMs, interprets `!commands`, and
replies. Messages are stored in a local SQLite database and delivered on
demand (pull-based), so the radio never has to push data unsolicited.

```
User DM → LoRa mesh → MeshCore device → USB/TCP/BLE → BBS (Python)
                                                            ↓
                                                       SQLite store
                                                            ↓
BBS reply ← LoRa mesh ← MeshCore device ← USB/TCP/BLE ← Python
```

## Requirements

- Python 3.14+
- A MeshCore device flashed with **Companion** firmware (not Room Server)
- Dependencies: `meshcore`, `pyyaml`, `aiohttp`, `aiomqtt` (see `requirements.txt`)

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

On first run, `config.yaml` is created automatically with default values.
Edit it before starting:

```yaml
# Connection type: tcp, serial, ble
connection:
  type: serial          # active transport
  tcp:
    host: 127.0.0.1
    port: 5000
  serial:
    port: /dev/ttyUSB0
    baudrate: 115200
  ble:
    address: MeshCore
    pin: "123456"

# LoRa radio (set all four or none; null = leave device value unchanged)
radio:
  frequency: 869.618        # kHz
  bandwidth: 62.500         # Hz
  spreading_factor: 8
  coding_rate: 8
  tx_power: 22              # dBm (independent of the four above)

# BBS behaviour
bbs:
  name: "📬 BBS"            # name shown to other mesh nodes
  latitude: 0.0             # GPS latitude for advert location (0.0 = disabled)
  longitude: 0.0            # GPS longitude for advert location (0.0 = disabled)
  db_path: bbs.db
  advert: true              # send an advert packet on startup
  advert_flood: false       # flood the advert across the whole mesh
  advert_times:             # UTC times to send advert each day (empty = off)
    - '09:00'
    - '21:00'
  flood_scope: ""           # restrict flood routing to a named scope, e.g. "#leipzig" (empty = no restriction)
  advert_in_channels_times: # UTC times to post channel advert each day (empty = off)
    - '09:00'
    - '21:00'
  advert_in_channels_text: "Store and forward messages at %s."  # %s = bbs.name
  advert_in_channels:       # channel names to post to (empty = disabled)
    - '#leipzig'
  admin_pubkeys:            # pubkey prefixes of admin users (grants !advert; empty list = disabled)
    - ""
  inbox_notify_interval: 120  # minutes between inbox reminders (0 = off)
  post_ttl_days: 14         # days before room posts are soft-deleted (0 = never)
  log_file: bbs.log         # path to log file (empty = stdout only)
  log_backup_count: 7       # number of daily log files to keep
  room_timeout: 60          # minutes of inactivity before auto-leave (0 = off)
  weather_location: Leipzig # default location for !weather (leave empty to require argument)
  rooms:
    - lobby
    - tech
```

> **Rooms** are defined here only. Users can join or leave rooms, but
> never create them. Rooms removed from the config stay intact in the
> database (existing posts are preserved).

## Running

```bash
python main.py
```

For production, use an external process supervisor so the BBS is restarted
automatically if the radio drops and reconnection fails:

**systemd** (`/etc/systemd/system/meshcore-bbs.service`):

```ini
[Unit]
Description=MeshCore BBS
After=network.target

[Service]
WorkingDirectory=/opt/meshcore-bbs
ExecStart=/opt/meshcore-bbs/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Docker Compose**:

```yaml
services:
  bbs:
    build: .
    restart: always
    devices:
      - /dev/ttyUSB0:/dev/ttyUSB0
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./bbs.db:/app/bbs.db
```

## Commands

Send any of these as a direct message to the BBS node:

| Command | Description |
|---------|-------------|
| `!help` | List all commands |
| `!rooms` | List available rooms with member count and last-post age |
| `!join <room>` | Enter a room |
| `!leave` | Leave your current room |
| `!post <text>` | Post a message to your current room |
| `!read [n]` | Read new posts in your current room (optional limit) |
| `!msg [name] <text>` | Send a private message |
| `!inbox` | Read your unread private messages (with sender and time) |
| `!who` | List members of your current room with last-activity time |
| `!users` | List the most recently active users (`user_list_limit`, default 5) with last-seen time |
| `!whoami` | Show how the BBS knows your name |
| `!whereami` / `!pwd` | Show your current room and unread post count |
| `!weather [location]` | Current weather (via wttr.in) — if enabled via `additional_commands` |
| `!ping` | Signal quality of your last message (SNR, RSSI, hops, path) — if enabled via `additional_commands` |
| `!advert` | Trigger an advert broadcast (secret — admin only, not shown in `!help`) |
| `!advert_channels` | Post channel advert immediately (secret — admin only, not shown in `!help`) |
| `!restart` | Restart the BBS with freshly loaded config.yaml (secret — admin only) |

### Addressing private messages

The recipient in `!msg` can be written in three ways:

```
!msg [Peter Bosch] hello           # bracket form — works with spaces/emoji
!msg @[Peter Bosch] hello          # MeshCore mention form (@ is optional)
!msg Peter hello                   # bare word — only if the name is one word
```

Use `!users` to see names in the `[name]` form ready to paste.

## Architecture

| File | Responsibility |
|------|----------------|
| `main.py` | Entry point — loads config, creates `MeshCoreBBS`, runs `asyncio` loop |
| `bbs/config.py` | Dataclass config tree + YAML loader (auto-creates file on first run) |
| `bbs/connection.py` | Connection factory: returns a `MeshCore` instance for tcp/serial/ble |
| `bbs/device.py` | Standalone async helpers: apply name/location/radio config, query device info |
| `bbs/store.py` | SQLite persistence — users, rooms, memberships, posts, private messages |
| `bbs/weather.py` | `WeatherProvider` protocol + `WttrInProvider` implementation |
| `bbs/commands.py` | Async command parser — no MeshCore/config dependency, fully unit-testable |
| `bbs/mqtt.py` | `MqttPublisher` — manages per-broker async tasks, publishes status + packet data |
| `bbs/bbs.py` | `MeshCoreBBS` — wires connection, store, router, and MQTT publisher |

### Paginated replies

When a command produces more than one DM (e.g. a long `!read` or `!inbox`),
the BBS waits `inter_msg_delay` seconds (default: **2.0**) between sends so the
radio has time to transmit each packet before the next is queued. Configurable
via `bbs.inter_msg_delay` in `config.yaml`.

### Delivery guarantee

`!read` and `!inbox` use a two-phase commit: posts/messages are fetched from
the store first, sent over the radio, and only marked as delivered after **all**
sends succeeded. A failed radio send leaves the state unchanged so the user
can retry without losing messages.

### Inbox notifications

When a user receives a private message via `!msg`, they are notified
immediately: "You have 1 new message in your inbox. Send !inbox."

If they haven't read their inbox yet, the BBS sends another reminder every
`inbox_notify_interval` minutes (default: 120). The interval clock resets
after each notification, so reminders stop once the user runs `!inbox`.
Set `inbox_notify_interval: 0` to disable all notifications.

### Room timeout (auto-leave)

When `bbs.room_timeout` is greater than zero, a background task checks every
`timeout/4` minutes for inactive room members and removes them silently.

**What counts as room activity:** `!join`, `!post`, `!read`.  
**What does not:** `!help`, `!msg`, `!inbox`, `!users`, `!whoami`, `!rooms`.

When a user is removed, they receive a DM: _"You were removed from 'lobby' after
60m inactivity. Send !join lobby to rejoin."_ — so they're not left wondering why
commands suddenly don't work.

Members that existed before this feature was added (i.e. with no
`last_activity` recorded) are exempt and will not be auto-removed until they
next join the room. Set `room_timeout: 0` to disable the feature entirely.

### Channel adverts

The BBS can post a text message to one or more MeshCore channels at fixed UTC
times each day (e.g. to announce itself on a shared channel like `#leipzig`):

```yaml
advert_in_channels_times:          # UTC times to post each day (empty = off)
  - '09:00'
  - '21:00'
advert_in_channels_text: "Store and forward messages at %s."  # %s = bbs.name
advert_in_channels:
  - '#leipzig'
```

The text `%s` is replaced with `bbs.name`. If a listed channel does not yet
exist on the device, the BBS creates it automatically in the first free slot —
for `#`-prefixed names the channel key is derived from `sha256(name)`,
which is the same convention MeshCore uses for public channels.
Leave `advert_in_channels_times` or `advert_in_channels` empty to disable.

### Weather

`!weather` fetches a compact one-line summary from [wttr.in](https://wttr.in)
(free, no API key required):

```
!weather          → uses weather_location from config.yaml
!weather Leipzig  → overrides for this request
```

Example reply: `Leipzig: ⛅️ +22°C 58% 12km/h 0.0mm 1015hPa`

The format string is set in the `WttrInProvider` constructor in `bbs/bbs.py`
using wttr.in format codes (`%c` emoji, `%t` temp, `%h` humidity, `%w` wind,
`%p` precipitation, `%P` pressure — see `https://wttr.in/:help`).

To use a different weather provider later, implement the `WeatherProvider`
protocol in `bbs/weather.py` (one async method: `fetch(location) -> str`)
and pass an instance to `CommandRouter` in `bbs/bbs.py`.

## MQTT

The BBS can publish radio data to one or more MQTT brokers:

```
meshcore/{IATA}/{PUBLIC_KEY}/status   — device online / offline (retained)
meshcore/{IATA}/{PUBLIC_KEY}/packets  — RF packet metadata per RX_LOG_DATA event
```

Add an `mqtt` section to `config.yaml`:

```yaml
mqtt:
  iata: LOC                      # 3-letter location code
  brokers:
    - enabled: true
      host: broker.example.com
      port: 1883
      username: user
      password: secret
      tls: false                 # optional
      tls_verify: true           # optional (ignored when tls: false)
      qos: 0                     # optional
      retain: false              # optional (packets topic only; status is always retained)
      keepalive: 60              # optional
    - enabled: true
      host: broker2.example.com
      port: 8883
      username: user2
      password: secret2
      tls: true
```

`PUBLIC_KEY` is the companion radio's public key, read from `MeshCore.self_info`
after connect. Each broker runs its own background task with a persistent
connection and reconnects automatically (30 s delay).

**`…/status` payload** (device info queried once at startup):
```json
{
  "status": "online",
  "timestamp": "2026-07-04T10:25:42.000000+00:00",
  "origin": "📬 BBS",
  "origin_id": "351789E2…",
  "model": "Heltec V3",
  "firmware_version": "v1.16.0-…",
  "client_version": "meshcore/v1.16.0-…",
  "radio": "869.618,62.5,8,8",
  "repeat": "off",
  "stats": {
    "battery_mv": 3807, "uptime_secs": 655574,
    "packets_sent": 48, "packets_received": 104494,
    "errors": 0, "queue_len": 0,
    "noise_floor": -113, "tx_air_secs": 58, "rx_air_secs": 70148,
    "recv_errors": 14816
  }
}
```

**`…/packets` payload** (one message per `RX_LOG_DATA` event):
```json
{
  "origin": "📬 BBS", "origin_id": "351789E2…",
  "timestamp": "2026-07-04T10:30:37.000000+00:00",
  "type": "PACKET", "direction": "rx",
  "time": "10:30:37", "date": "04/07/2026",
  "len": "101", "packet_type": "5", "route": "F", "payload_len": "83",
  "SNR": "-7.2", "RSSI": "-120", "score": 0,
  "raw": "148F93…", "hash": "0E25BD81E3A18C3C"
}
```

Routes: `F` = flood, `D` = direct (also includes `path`), `T` = transport-direct.  
`score` is set to `0` — the firmware computes a real value but does not expose it via the companion protocol.

Omit the `mqtt` section (or leave `brokers: []`) to disable MQTT entirely.

## Logging

```
2026-07-02 10:00:00 INFO     bbs.bbs: BBS name set to '📬 BBS'.
2026-07-02 10:00:00 INFO     bbs.store: BBS store opened at bbs.db
2026-07-02 10:00:01 INFO     bbs.bbs: DM from 'Alice' (a1b2c3d4e5f6): '!help'
2026-07-02 10:00:01 INFO     bbs.bbs: DM sent to 'Alice'.
```

`INFO` — lifecycle events (startup, connection, per-message).  
`DEBUG` — verbose detail.

Set `bbs.log_file` in `config.yaml` to write logs to a file with daily rotation
(midnight rollover, `bbs.log_backup_count` files retained). stdout is always
active in parallel — set `log_file: ""` to disable file logging.