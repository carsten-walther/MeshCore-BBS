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
- Dependencies: `meshcore`, `pyyaml`, `aiohttp` (see `requirements.txt`)

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
  db_path: bbs.db
  advert: true              # send an advert packet on startup
  advert_flood: false       # flood the advert across the whole mesh
  advert_interval: 180      # resend advert every N minutes (0 = off)
  admin_pubkeys:            # pubkey prefixes of admin users (grants !advert; empty list = disabled)
    - ""
  room_timeout: 60          # minutes of inactivity before auto-leave (0 = off)
  weather_location: Berlin  # default location for !weather (leave empty to require argument)
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
| `!rooms` | List available rooms |
| `!join <room>` | Enter a room |
| `!leave` | Leave your current room |
| `!post <text>` | Post a message to your current room |
| `!read` | Read new posts in your current room |
| `!msg [name] <text>` | Send a private message |
| `!inbox` | Read your unread private messages |
| `!users` | List the 5 most recently active users |
| `!whoami` | Show how the BBS knows your name |
| `!whereami` / `!pwd` | Show your current room |
| `!weather [location]` | Current weather (via wttr.in) |
| `!advert` | Trigger an advert broadcast (secret — admin only, not shown in `!help`) |

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
|------|---------------|
| `main.py` | Entry point — loads config, creates `MeshCoreBBS`, runs `asyncio` loop |
| `bbs/config.py` | Dataclass config tree + YAML loader (auto-creates file on first run) |
| `bbs/connection.py` | Connection factory: returns a `MeshCore` instance for tcp/serial/ble |
| `bbs/store.py` | SQLite persistence — users, rooms, memberships, posts, private messages |
| `bbs/weather.py` | `WeatherProvider` protocol + `WttrInProvider` implementation |
| `bbs/commands.py` | Async command parser — no MeshCore/config dependency, fully unit-testable |
| `bbs/bbs.py` | `MeshCoreBBS` — wires connection, store, and router; handles delivery guarantees |

### Paginated replies

When a command produces more than one DM (e.g. a long `!read` or `!inbox`),
the BBS waits **1 second** between sends so the radio has time to transmit
each packet before the next is queued (`_INTER_MSG_DELAY_SECS` in `bbs/bbs.py`).

### Delivery guarantee

`!read` and `!inbox` use a two-phase commit: posts/messages are fetched from
the store first, sent over the radio, and only marked as delivered after **all**
sends succeeded. A failed radio send leaves the state unchanged so the user
can retry without losing messages.

### Room timeout (auto-leave)

When `bbs.room_timeout` is greater than zero, a background task checks every
`timeout/4` minutes for inactive room members and removes them silently.

**What counts as room activity:** `!join`, `!post`, `!read`.  
**What does not:** `!help`, `!msg`, `!inbox`, `!users`, `!whoami`, `!rooms`.

Members that existed before this feature was added (i.e. with no
`last_activity` recorded) are exempt and will not be auto-removed until they
next join the room. Set `room_timeout: 0` to disable the feature entirely.

### Weather

`!weather` fetches a compact one-line summary from [wttr.in](https://wttr.in)
(free, no API key required):

```
!weather          → uses weather_location from config.yaml
!weather München  → overrides for this request
```

Example reply: `München: ⛅️ +22°C 58% 12km/h 0.0mm 1015hPa`

The format string is set in the `WttrInProvider` constructor in `bbs/bbs.py`
using wttr.in format codes (`%c` emoji, `%t` temp, `%h` humidity, `%w` wind,
`%p` precipitation, `%P` pressure — see `https://wttr.in/:help`).

To use a different weather provider later, implement the `WeatherProvider`
protocol in `bbs/weather.py` (one async method: `fetch(location) -> str`)
and pass an instance to `CommandRouter` in `bbs/bbs.py`.

## Logging

```
2026-07-02 10:00:00 INFO     bbs.bbs: BBS name set to '📬 BBS'.
2026-07-02 10:00:00 INFO     bbs.store: BBS store opened at bbs.db
2026-07-02 10:00:01 INFO     bbs.bbs: DM from 'Alice' (a1b2c3d4e5f6): '!help'
2026-07-02 10:00:01 INFO     bbs.bbs: DM sent to 'Alice'.
```

`INFO` — lifecycle events (startup, connection, per-message).  
`DEBUG` — verbose detail (enable in `main.py` by setting `logging.DEBUG`).