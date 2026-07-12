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

## Features

- **Rooms** — config-defined boards users join, post to, and read at their
  own pace (pull-based, with seen-markers and an airtime-friendly read cap)
- **Private messages** — store-and-forward DMs with inbox, `!reply`, and
  configurable new-message reminders
- **Community tools** — `!who`, `!users`, `!seen`, full-text `!search`,
  `!undo` for your own posts
- **Weather** — `!weather` via wttr.in with Open-Meteo fallback
- **Solar / space weather** — `!solar` with solar indices and HF band
  conditions for radio amateurs (hamqsl.com, NOAA SWPC fallback)
- **Signal insight** — `!ping` with SNR/RSSI, hop path, and a 24h average
  from the per-user signal history
- **Protection** — per-user rate limiting, room inactivity timeout, and
  automatic post expiry
- **Internationalization** — English and German out of the box, every
  string overridable via config
- **Scheduled adverts** — daily node adverts and channel announcements at
  configured UTC times
- **Admin CLI** — stats, moderation, and room management in a separate
  process, safe to run alongside the live BBS
- **MQTT publishing** — status and packet metadata compatible with
  meshcore-packet-capture
- **Ops-ready** — Docker image (multi-arch), healthcheck, CI with lint,
  type-check, and a hardware-free test suite

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

Copy `config/config.example.yaml` to `config/config.yaml` and adjust it — the
example documents every available option and is kept in sync with the code by
a CI test. Alternatively, on first run `config/config.yaml` is created
automatically with default values. A typical configuration:

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
  language: en              # language of user-facing replies: en or de
  strings: {}               # per-string overrides, keyed by the English template

  advert:
    enabled: true           # send an advert packet on startup
    flood: false            # flood the advert across the whole mesh
    times:                  # UTC times to send advert each day (empty = off), always use quotes
      - '09:00'
      - '21:00'
    flood_scope: ""         # restrict flood routing to a scope, e.g. "de-sn" (empty = no restriction)

  channels:
    text: "Store and forward messages at @[{name}]."  # {name} = bbs.name
    names:                  # channel names to post to (empty = disabled)
      - '#lobby'
    times:                  # UTC times to post channel advert each day (empty = off), always use quotes
      - '09:00'
      - '21:00'

  rooms:
    names:
      - lobby
      - tech
    timeout: 60             # minutes of inactivity before auto-leave (0 = off)
    undo_window: 600        # seconds a post stays !undo-able (0 = no time limit)

  messaging:
    max_len: 150            # maximum byte length of a single outgoing DM
    inter_delay: 2.0        # seconds between DMs in a paginated reply
    inbox_notify_interval: 120  # minutes between inbox reminders (0 = off)
    user_list_limit: 5      # number of users shown by !users
    read_limit: 5           # posts per !read without an explicit number (0 = unlimited)
    rate_limit: 10          # commands per user per minute (0 = no limit)

  storage:
    db_path: data/bbs.db
    post_ttl_days: 14       # days before room posts are soft-deleted (0 = never)
    signal_ttl_days: 30     # days of per-user signal history to keep (0 = forever)

  logging:
    file: data/bbs.log      # path to log file (empty = stdout only)
    backup_count: 7         # number of daily log files to keep
    level: INFO             # DEBUG, INFO, WARNING, ERROR.

  features:
    commands:               # optional commands to enable (omit one to disable it)
      - seen
      - whoami
      - stats
      - weather
      - ping
      - solar
    weather_location: Leipzig  # default location for !weather (empty = require argument)
```

> **Rooms** are defined here only. Users can join or leave rooms, but
> never create them. Rooms removed from the config stay intact in the
> database (existing posts are preserved).

### Language

All replies the BBS sends over the mesh are English by default; set
`bbs.language: de` for German. Individual strings can be reworded via
`bbs.strings`, keyed by the **English** template:

```yaml
bbs:
  language: de
  strings:
    "No new messages.": "Nix Neues!"
```

Log output and the admin CLI always stay English.

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

**Device commands** (require a *running* BBS):
```bash
python app/admin.py contacts          # contacts the radio heard via advert
python app/admin.py device-info       # firmware, radio params, device stats
python app/admin.py advert            # send an advert now
python app/admin.py advert --flood    # ... flooded
python app/admin.py advert-channels   # send the channel advert text now
```

The radio has exactly one connection — held by the BBS process — so the
admin CLI cannot talk to the device directly. Instead, the running BBS
exposes device actions on a Unix socket (`admin.sock`, created next to the
database, permissions `0600`). The CLI finds it via the same config file;
if the BBS is not running, device commands report that and everything else
keeps working against the database.

`contacts` shows what the *device* knows (everyone heard via advert,
including repeaters and room servers) — a superset of `users`, which only
lists people who have actually sent a command to the BBS.

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
chown -R 1000:1000 data          # the container runs as non-root UID 1000
# copy or create config/config.yaml
docker compose up -d
```

The image ships a `HEALTHCHECK`: the BBS touches `/data/heartbeat` every
30 s from inside its event loop, so a *hung* process (which
`restart: unless-stopped` cannot see) shows up as `unhealthy`:

```bash
docker inspect --format '{{.State.Health.Status}}' meshcore-bbs
```

For serial devices, enable the commented `group_add` block in
`docker-compose.yaml` — the non-root user needs the host's dialout GID.

To update to the latest image:

```bash
docker compose pull && docker compose up -d
```

Opening an interactive shell in the container — `docker exec -it
meshcore-bbs bash` (or `sh`), or the shell button of a UI like Portainer,
Docker Desktop, or TrueNAS — drops straight into the admin REPL. `quit`
ends the session. Single commands work as before:

```bash
docker exec -it meshcore-bbs python admin.py stats
docker exec -it meshcore-bbs python admin.py contacts
```

For a plain shell (debugging), bypass the REPL autostart:

```bash
docker exec -it -e BBS_SHELL=1 meshcore-bbs bash
```

Device commands work here too: the admin socket lives in `/data`, which
both the BBS and the `docker exec` shell see.

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

| Command | Description                                                                                        |
|---------|----------------------------------------------------------------------------------------------------|
| `!help` | Compact one-message list of all command names                                                      |
| `!help <cmd>` | Explain one command (e.g. `!help read`)                                                            |
| `!help extras` | Compact list of the enabled optional commands                                                      |
| `!rooms` | List available rooms with member count and last-post age                                           |
| `!join <room>` | Enter a room                                                                                       |
| `!leave` | Leave your current room                                                                            |
| `!post <text>` | Post a message to your current room                                                                |
| `!read [n]` | Read new posts in your current room with relative timestamp (optional limit)                       |
| `!search <text>` | Search posts in your current room (newest first, capped at `read_limit`)                           |
| `!reply <text>` | Answer your last inbox message                                                                     |
| `!msg [name] <text>` | Send a private message                                                                             |
| `!msg <keyprefix> <text>` | Send a private message                                                                             |
| `!undo` | Remove your last post (within 10 min) (`undo_window`, default 600 seconds)                         |
| `!inbox` | Read your unread private messages (with sender and time)                                           |
| `!who` | List members of your current room with last-activity time                                          |
| `!users` | List the most recently active users (`user_list_limit`, default 5) with last-seen time             |
| `!seen <name>` | Show when a user was last active (name, `[name]`, or key prefix) — if enabled via `features.commands` |
| `!whoami` | Show how the BBS knows your name — if enabled via `features.commands`                              |
| `!whereami` / `!pwd` | Show your current room and unread post count                                                       |
| `!stats` | Show total user, post, and room counts — if enabled via `features.commands`                        |
| `!weather [location]` | Current weather (wttr.in, open-meteo fallback) — if enabled via `features.commands`                |
| `!ping` | Signal quality of your last message (SNR, RSSI, hops, path) plus a 24h average — if enabled via `additional_commands` |
| `!solar` | Solar indices (SFI, SSN, A, K) and HF band conditions — if enabled via `features.commands` |

`!help` deliberately fits into a single DM (command names only) as an
airtime guard — descriptions and the optional commands cost airtime only
when asked for via `!help <cmd>` / `!help extras`.

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
| `bbs/plugin.py` | `CommandPlugin` — protocol for self-contained optional commands |
| `bbs/plugins/` | Plugin package + auto-loader — modules are loaded by their name in `features.commands` |
| `bbs/plugins/weather.py` | `WeatherProvider` protocol + wttr.in/open-meteo chain; the `!weather` plugin |
| `bbs/plugins/solar.py` | `SolarProvider` protocol + hamqsl/NOAA chain (15 min cache); the `!solar` plugin |
| `bbs/messages.py` | Message catalog — English templates as keys, German catalog, per-string overrides |
| `bbs/util.py` | Shared helpers (`fmt_ago`) used by router and admin CLI |
| `bbs/commands.py` | Async command parser — no MeshCore/config dependency, fully unit-testable |
| `bbs/adminserver.py` | Unix-socket RPC server — device commands for the admin CLI |
| `bbs/mqtt.py` | `MqttPublisher` — manages per-broker async tasks, publishes status + packet data |
| `bbs/bbs.py` | `MeshCoreBBS` — wires connection, store, router, plugins, and MQTT publisher |

### Paginated replies

When a command produces more than one DM (e.g. a long `!read` or `!inbox`),
the BBS waits `inter_delay` seconds (default: **2.0**) between sends so the
radio has time to transmit each packet before the next is queued. Configurable
via `bbs.messaging.inter_delay` in `config.yaml`.

### Rate limiting

Each user may trigger at most `bbs.messaging.rate_limit` replies per minute
(default: **10**, sliding window, `0` disables the limit). The first message
over the limit gets a short warning; everything after that is dropped
**silently** — answering every excess message would burn the very airtime
the limit protects. As soon as the window has room again, service resumes.
The limit counts incoming messages (including plain text, which gets the
`!help` hint); scheduled tasks like adverts or inbox reminders are unaffected.

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
**What does not:** `!help`, `!msg`, `!inbox`, `!users`, `!seen`, `!search`, `!whoami`, `!rooms`.

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
    text: "Store and forward messages at @[{name}]."  # {name} = bbs.name
    names:
      - '#leipzig'
    times:                   # UTC times to post each day (empty = off), always use quotes
      - '09:00'
      - '21:00'
```

The text `{name}` is replaced with `bbs.name`. If a listed channel does not yet
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
protocol in `bbs/plugins/weather.py` (one async method:
`fetch(location) -> str`) and swap it into the chain built in that
module's `create()`. Out of the box the BBS chains two providers: wttr.in
first, with open-meteo.com as automatic fallback when wttr.in is down or
rate-limited.

`!weather` and `!solar` are implemented as **plugins**: a plugin is a
module in `bbs/plugins/` whose file name equals its command name and which
exposes a `create(features, messages)` factory plus its own translations
(`TRANSLATIONS`). Listing the name under `bbs.features.commands` in
`config.yaml` loads it automatically — there is no other wiring. A new
optional command is one module in `bbs/plugins/` plus one config line;
the command parser stays untouched. Plugin tests live in `tests/plugins/`.

### Solar / space weather

`!solar` reports solar indices and HF band conditions for the radio
amateurs on the mesh:

```
SFI 107  SSN 80  A 12  K 1 (VR QUIET)
Day: 80-40 Fair, 30-20 Good, 17-15 Good, 12-10 Poor
Night: 80-40 Good, 30-20 Good, 17-15 Poor, 12-10 Poor
```

Data comes from [hamqsl.com](https://www.hamqsl.com) (N0NBH), with the
official NOAA SWPC JSON feeds as fallback (indices only — NOAA publishes
no band forecast). Results are cached for 15 minutes; solar data changes
slowly (Kp every 3 h, flux daily), so replies are instant and the free
APIs are treated politely. Note that space weather affects HF propagation,
not the 868 MHz LoRa band — this is a service *for* the mesh community,
not a diagnostic *of* the mesh.

## Development

```bash
pip install -r requirements-dev.txt   # pytest, ruff, mypy + stubs
pytest                                # 176 tests, no hardware needed
ruff check app tests                  # lint
mypy                                  # type-check (config in pyproject.toml)
```

CI (`.github/workflows/ci.yml`) runs all three on every push and pull
request; `docker.yml` builds the multi-platform image. Local hooks mirror
the pipeline:

```bash
pip install pre-commit
pre-commit install                    # ruff + hygiene + pytest on each commit
```

The test suite runs entirely without a radio — the store, command router,
message catalog, and weather chain are hardware-free by design. Many tests
are regression tests for past review findings and are commented as such.

## Roadmap & ideas

Design notes for planned and considered features — most prominently a
concept for **BBS federation** (sharing rooms between instances via MQTT) —
live in [IDEAS.md](IDEAS.md).

## Security

A few honest notes on what the BBS does and does not protect:

**Transport.** DMs between a user and the BBS are end-to-end encrypted by
MeshCore itself — nodes in between only relay ciphertext. Public channel
adverts are, by nature, public.

**Storage.** Posts and private messages are stored in **plaintext** in the
SQLite database on the host. The operator of a BBS can read everything that
passes through it, and so can anyone with access to the `data/` directory.
Treat the BBS like a postcard service, not a vault, and protect the data
directory accordingly (the Docker image runs as UID 1000 for this reason).

**Message retention.** Private messages are soft-deleted immediately after
delivery and room posts expire after `post_ttl_days`, but "deleted" rows
remain in the database file until vacuumed. `!undo` only stops *future*
delivery of a post — copies already received over the air cannot be
recalled.

**Signal history.** For every received DM the BBS stores one row of radio
metadata (SNR, RSSI, hop count) per user — this powers the `!ping` 24h
average. Rows are physically deleted after `signal_ttl_days` (default 30).

**Admin access.** There are no admin commands over the mesh — all
maintenance goes through the admin CLI (`app/admin.py`), which requires
shell access to the host (or `docker exec`).

**Identity.** Display names on the mesh are self-assigned and not unique.
The BBS treats the public key as the identity everywhere; when a name is
ambiguous, `!msg` falls back to key-prefix addressing rather than guessing.

**MQTT.** If brokers are configured, received packet metadata (signal data,
packet types, hashes) is published to them — consider who can read those
topics.

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

`INFO` — lifecycle events (startup, connection, per-message). Default level.
`DEBUG` — verbose detail, including third-party libraries; opt in via
`bbs.logging.level: DEBUG`.

Set `bbs.logging.file` in `config.yaml` to write logs to a file with daily rotation
(midnight rollover, `bbs.logging.backup_count` files retained). stdout is always
active in parallel — set `file: ""` to disable file logging.
