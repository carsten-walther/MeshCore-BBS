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

On first run, `config/config.yaml` is created automatically with default values.
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

  advert:
    enabled: true           # send an advert packet on startup
    flood: false            # flood the advert across the whole mesh
    times:                  # UTC times to send advert each day (empty = off)
      - '09:00'
      - '21:00'
    flood_scope: ""         # restrict flood routing to a scope, e.g. "de-sn" (empty = no restriction)

  channels:
    text: "Store and forward messages at @[%s]."  # %s = bbs.name
    names:                  # channel names to post to (empty = disabled)
      - '#lobby'
    times:                  # UTC times to post channel advert each day (empty = off)
      - '09:00'
      - '21:00'

  rooms:
    names:
      - lobby
      - tech
    timeout: 60             # minutes of inactivity before auto-leave (0 = off)

  messaging:
    max_len: 150            # maximum byte length of a single outgoing DM
    inter_delay: 2.0        # seconds between DMs in a paginated reply
    inbox_notify_interval: 120  # minutes between inbox reminders (0 = off)
    user_list_limit: 5      # number of users shown by !users

  storage:
    db_path: data/bbs.db
    post_ttl_days: 14       # days before room posts are soft-deleted (0 = never)

  logging:
    file: data/bbs.log      # path to log file (empty = stdout only)
    backup_count: 7         # number of daily log files to keep

  admin:
    pubkeys: []             # full pubkeys of admin users (grants !restart etc.)
    # pubkeys:
    #   - "a3f2c19e8b7d5f0412…"   # 64 hex chars — get yours via the MeshCore app

  features:
    commands:               # optional commands to enable (weather, ping)
      - weather
      - ping
    weather_location: Leipzig  # default location for !weather (empty = require argument)
```

> **Rooms** are defined here only. Users can join or leave rooms, but
> never create them. Rooms removed from the config stay intact in the
> database (existing posts are preserved).

## Running

```bash
python app/main.py
```

## Admin CLI

`app/admin.py` provides a console interface for maintenance tasks. It reads
`config/config.yaml` (or `$BBS_CONFIG`) to find the database and can run
alongside a live BBS (SQLite WAL mode allows concurrent access).

**Single-command mode:**
```bash
python app/admin.py stats
python app/admin.py users
python app/admin.py rooms
python app/admin.py posts lobby
python app/admin.py posts lobby -n 50
python app/admin.py purge-posts --days 30
python app/admin.py purge-posts --room test
python app/admin.py delete-post 1234
python app/admin.py kick <pubkey>
python app/admin.py delete-user <pubkey>
python app/admin.py room-add lounge
python app/admin.py room-delete lounge
python app/admin.py room-members lobby
python app/admin.py room-kick lobby <pubkey>
```

**Interactive shell** (no arguments):
```bash
python app/admin.py
```
```
MeshCore BBS Admin  —  type 'help' for commands, 'quit' to exit
bbs> stats
Users : 12
Posts : 847  (non-deleted)
Rooms : 3
bbs> purge-posts --days 30
Deleted 42 post(s) older than 30 day(s).
bbs> quit
```

`pubkey` arguments accept a unique prefix — the shell resolves it and
reports an error if ambiguous.

> **Room management note:** `room-add` creates the room in the database only.
> To make it persistent across BBS restarts, also add the name to `config/config.yaml → bbs.rooms.names`.
> `room-delete` removes all memberships and soft-deletes all posts — this is irreversible.

For production, use an external process supervisor so the BBS is restarted
automatically if the radio drops and reconnection fails.

### Without Docker (systemd)

The BBS reads its config from `config/config.yaml` by default, or from the
path in `$BBS_CONFIG` if set.

`/etc/systemd/system/meshcore-bbs.service`:

```ini
[Unit]
Description=MeshCore BBS
After=network.target

[Service]
User=meshcore
WorkingDirectory=/opt/meshcore-bbs
Environment=BBS_CONFIG=/etc/meshcore-bbs/config.yaml
ExecStart=/opt/meshcore-bbs/.venv/bin/python app/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Set `db_path` and `log_file` in your `config.yaml` to absolute paths
(e.g. `/var/lib/meshcore-bbs/bbs.db`) so they are independent of the
working directory. Without `$BBS_CONFIG` the file must be in
`WorkingDirectory`.

### With Docker Compose

The image is published automatically to the GitHub Container Registry on every
push to `main` and on version tags:

```
ghcr.io/carsten-walther/meshcore-bbs:latest
ghcr.io/carsten-walther/meshcore-bbs:v1.2.3
```

The image uses three separate directories:

| Path in container | Purpose |
|---|---|
| `/app` | Application code (baked into the image, read-only) |
| `/config` | `config.yaml` (bind-mounted from `./config/` on the host) |
| `/data` | `bbs.db` and log files (bind-mounted from `./data/` on the host) |

Set these paths in `config/config.yaml`:

```yaml
bbs:
  storage:
    db_path: /data/bbs.db
  logging:
    file: /data/bbs.log
```

Then start with:

```bash
mkdir -p config data
# copy or create config/config.yaml
docker compose up -d
```

To update to the latest image:

```bash
docker compose pull && docker compose up -d
```

The admin CLI can be run against the live database from inside the container:

```bash
docker exec -it meshcore-bbs python admin.py
docker exec -it meshcore-bbs python admin.py stats
```

#### TrueNAS SCALE

Create datasets for config and data on your pool, then use **Apps → Custom App → Docker Compose** with:

```yaml
services:
  meshcore-bbs:
    image: ghcr.io/carsten-walther/meshcore-bbs:latest
    container_name: meshcore-bbs
    restart: unless-stopped
    environment:
      - BBS_CONFIG=/config/config.yaml
    volumes:
      - /mnt/tank/apps/meshcore-bbs/config:/config:ro
      - /mnt/tank/apps/meshcore-bbs/data:/data
```

Adjust the volume paths to match your pool and dataset layout.
To update: **Apps → meshcore-bbs → Update** or pull the image and restart the app.

## Commands

Send any of these as a direct message to the BBS node:

| Command | Description |
|---------|-------------|
| `!help` | List all commands |
| `!rooms` | List available rooms with member count and last-post age |
| `!join <room>` | Enter a room |
| `!leave` | Leave your current room |
| `!post <text>` | Post a message to your current room |
| `!read [n]` | Read new posts in your current room with relative timestamp (optional limit) |
| `!msg [name] <text>` | Send a private message |
| `!inbox` | Read your unread private messages (with sender and time) |
| `!who` | List members of your current room with last-activity time |
| `!users` | List the most recently active users (`user_list_limit`, default 5) with last-seen time |
| `!whoami` | Show how the BBS knows your name |
| `!whereami` / `!pwd` | Show your current room and unread post count |
| `!stats` | Show total user, post, and room counts |
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
| `admin.py` | Admin CLI + interactive shell — maintenance tasks on the SQLite store |
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
the BBS waits `inter_delay` seconds (default: **2.0**) between sends so the
radio has time to transmit each packet before the next is queued. Configurable
via `bbs.messaging.inter_delay` in `config.yaml`.

### Delivery guarantee

`!read` and `!inbox` use a two-phase commit: posts/messages are fetched from
the store first, sent over the radio, and only marked as delivered after **all**
sends succeeded. A failed radio send leaves the state unchanged so the user
can retry without losing messages.

Every outgoing DM is sent via `send_msg_with_retry` (up to 5 attempts). After
3 failed attempts on the direct path the BBS calls `reset_path()` and switches
to flood routing for the remaining 2 attempts, so a node that has moved or
whose cached route has expired is still reachable.

### Inbox notifications

When a user receives a private message via `!msg`, they are notified
immediately: "You have 1 new message in your inbox. Send !inbox."

If they haven't read their inbox yet, the BBS sends another reminder every
`messaging.inbox_notify_interval` minutes (default: 120). The interval clock resets
after each notification, so reminders stop once the user runs `!inbox`.
Set `inbox_notify_interval: 0` to disable all notifications.

### Room timeout (auto-leave)

When `bbs.rooms.timeout` is greater than zero, a background task checks every
`timeout/4` minutes for inactive room members and removes them silently.

**What counts as room activity:** `!join`, `!post`, `!read`.  
**What does not:** `!help`, `!msg`, `!inbox`, `!users`, `!whoami`, `!rooms`.

When a user is removed, they receive a DM: _"You were removed from 'lobby' after
60m inactivity. Send !join lobby to rejoin."_ — so they're not left wondering why
commands suddenly don't work.

Members that existed before this feature was added (i.e. with no
`last_activity` recorded) are exempt and will not be auto-removed until they
next join the room. Set `rooms.timeout: 0` to disable the feature entirely.

### Channel adverts

The BBS can post a text message to one or more MeshCore channels at fixed UTC
times each day (e.g. to announce itself on a shared channel like `#leipzig`):

```yaml
bbs:
  channels:
    text: "Store and forward messages at @[%s]."  # %s = bbs.name
    names:
      - '#leipzig'
    times:                   # UTC times to post each day (empty = off)
      - '09:00'
      - '21:00'
```

The text `%s` is replaced with `bbs.name`. If a listed channel does not yet
exist on the device, the BBS creates it automatically in the first free slot —
for `#`-prefixed names the channel key is derived from `sha256(name)`,
which is the same convention MeshCore uses for public channels.
Leave `channels.times` or `channels.names` empty to disable.

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

Set `bbs.logging.file` in `config.yaml` to write logs to a file with daily rotation
(midnight rollover, `bbs.logging.backup_count` files retained). stdout is always
active in parallel — set `file: ""` to disable file logging.