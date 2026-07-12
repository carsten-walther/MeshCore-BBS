# MeshCore BBS

A store-and-forward bulletin board that runs on a MeshCore **companion**
radio over a LoRa mesh, driven by the `meshcore` Python library
(`meshcore==2.3.7`). It replaces the idea of a firmware "Room Server":
instead the BBS logic lives entirely in Python and the companion radio is
just the modem.

## Why a companion, not a Room Server

The MeshCore role (Companion / Repeater / Room Server) is fixed by the
flashed firmware and can't be switched at runtime. `meshcore_py` only speaks
the companion protocol. Room Server firmware has poor reachability and a
fixed feature set, so we run a Companion and implement the BBS ourselves.
Trade-off: users interact via normal DMs and a custom `!command` protocol,
not the app's native Room Server UI.

## Architecture

- `app/bbs/mqtt.py` — `MqttPublisher`: one background task per enabled broker (asyncio.Queue
  + persistent `aiomqtt.Client`). Publishes to two topics compatible with
  meshcore-packet-capture: `meshcore/{IATA}/{PUBLIC_KEY}/status` (online/offline,
  retained) and `meshcore/{IATA}/{PUBLIC_KEY}/packets` (one message per `RX_LOG_DATA`
  event). `packets` payload fields: `origin`, `origin_id`, `timestamp`, `type`,
  `direction`, `time` (HH:MM:SS), `date` (DD/MM/YYYY), `len`, `packet_type` (int as
  string), `route` (F/D/T), `payload_len`, `SNR`, `RSSI`, `score` (always 0 — firmware
  computes the real value but does not expose it via the companion protocol), `raw`
  (uppercase hex), `hash` (SHA256 first 8 bytes, uppercase hex), `path` (direct routes
  only). Route map: TC_FLOOD/FLOOD→"F", DIRECT→"D", TC_DIRECT→"T". Reconnects
  automatically on MQTT errors (30 s delay). `stop()` sends offline status before
  closing. Configured via `AppConfig.mqtt` (`MqttConfig` + list of `MqttBrokerConfig`).
  PUBLIC_KEY retrieved at startup via `self._mc.self_info.get("public_key", "")`
  (populated from `SELF_INFO` event during `send_appstart()`). Device info for the
  `status` payload queried once at startup by `_query_device_info()` in `bbs.py`
  (DEVICE_INFO, self_info radio, STATS_CORE, STATS_RADIO, STATS_PACKETS).
- `app/bbs/config.py` — dataclass config + YAML loader. Auto-creates the config
  file at the given path if missing (default `config/config.yaml`). Sections: connection (tcp/serial/ble),
  radio (freq/bw/sf/cr/tx_power in MeshCore units, None = leave as-is),
  bbs (name, latitude, longitude, language, strings + 7 nested sub-sections):
  `advert` (enabled, flood, times, flood_scope),
  `channels` (text with `{name}` placeholder, names, times),
  `rooms` (names, timeout, undo_window),
  `messaging` (max_len, inter_delay, inbox_notify_interval, user_list_limit, read_limit, rate_limit),
  `storage` (db_path, post_ttl_days, signal_ttl_days),
  `logging` (file, backup_count, level),
  `features` (commands, weather_location).
  Everything user-supplied is validated on load via `_valid_*` helpers
  (`_valid_times` also converts YAML-1.1 sexagesimal ints like unquoted
  `21:00`→1260 back to "21:00";
  `_valid_language`, `_valid_log_level`, `_valid_qos` clamp with warnings).
  A stale `admin:` section in an existing config is ignored (the former
  DM admin commands are gone — admin actions live in `app/admin.py`).
  Relative paths are anchored at the repo/app root via `_APP_ROOT`
  (`Path(__file__).resolve().parents[2]`) so behaviour is cwd-independent;
  in the container that root is `/`, mapping straight onto the volumes.
  `config/config.example.yaml` is the commented twin of the auto-created
  defaults; `tests/test_config.py::TestExampleConfig` compares PARSED values
  so the example can never drift again (comments are free, values are not).
  NOTE: `field(default_factory=...)` fields have no class attribute, so the
  loader must inline their default.
- `app/bbs/device.py` — standalone async helpers for device setup: `apply_device_name`,
  `apply_device_loc`, `apply_radio_config`, `apply_flood_scope`, `query_device_info`.
  Depend only on `MeshCore` + config values — no store, router, or MQTT dependency,
  so independently testable. Called from `bbs.py` during `start()`.
  `apply_flood_scope(mc, flood_scope)` calls `set_flood_scope` (runtime) and
  `set_default_flood_scope` (persisted) on the device; empty string skips both.
  Both methods live in `MessagingCommands` (accessible via `mc.commands`).
- `app/bbs/connection.py` — connection factory (tcp/serial/ble), try/except only
  for logging (meshcore raises on failure).
- `app/bbs/store.py` — SQLite persistence. Tables: users, rooms, memberships,
  posts, private_messages. Users keyed by FULL public key (never the 12-char
  pubkey_prefix — prefixes can collide). WAL mode. Read and mark-seen are
  deliberately separate so a failed radio send can't drop messages.
  `memberships` has a `last_activity` column; `posts` and `private_messages`
  have a `deleted` column (soft-delete, never physical DELETE). Schema
  migrations in `connect()` add these via `ALTER TABLE … ADD COLUMN`
  (OperationalError ignored if already present).
  `expire_posts(ttl_secs)` soft-deletes posts older than TTL.
  `mark_private_delivered()` sets both `delivered=1` and `deleted=1`.
  `unseen_posts()`, `undelivered_private()`, `recipients_with_undelivered_private()`
  all filter `deleted=0`.
  `search_posts(room, term, limit)` / `count_search_posts(room, term)` —
  case-insensitive LIKE substring search (wildcards escaped via
  `_like_escape`), non-deleted posts only, newest first; powers `!search`.
  `signal_history` table (pubkey, snr, rssi, hops, created_at — one row per
  received DM): `add_signal_record(..., ttl_secs)` inserts and prunes rows
  older than the TTL in the same commit (no extra cleanup task needed at
  LoRa rates; physical DELETE, not soft); `signal_stats(pubkey,
  window_secs=86400)` returns avg SNR/RSSI + count or None — powers the
  `!ping` 24h trend line.
  Admin-only methods (used by `app/admin.py`): `list_all_users()` (all users, no
  limit), `list_posts(room, limit)` (newest first), `delete_post(id)` (soft,
  returns bool), `delete_posts_in_room(room)` (soft, returns count),
  `kick_user(pubkey)` (leave all rooms, returns room list), `delete_user(pubkey)`
  (physical DELETE from users + soft-delete posts/PMs, returns bool),
  `delete_room(name)` (DELETE memberships + room row, soft-delete posts,
  reset `current_room=NULL` for affected users; returns bool).
  `get_stats()` — single SELECT with sub-selects for user, post, room counts.
  `busy_timeout=5000` is set alongside WAL so a write collision with a
  parallel admin.py waits instead of raising "database is locked".
  `users.last_pm_from` (added via migration) stores the `!reply` target —
  set ONLY in the deferred commit of `!inbox`, i.e. after proven delivery.
  `last_post_by(pubkey)` returns the newest non-deleted own post (`!undo`).
  Name lookups: `find_users_by_name` (exact, CI), `find_users_by_name_prefix`
  (LIKE with ESCAPE — a user named "100%" must not act as a wildcard),
  `find_users_by_pubkey_prefix` (router validates hex before calling).
- `app/admin.py` — standalone admin CLI + interactive shell. Single-command mode
  (`python app/admin.py stats`) and REPL mode (`python app/admin.py`, no args). Uses
  `BBSStore` directly; safe to run alongside a live BBS (WAL). DB commands:
  `stats`, `users`, `rooms`, `posts <room> [-n N]`, `purge-posts --days N`,
  `purge-posts --room <room>`, `delete-post <id>`, `kick <pubkey>`,
  `delete-user <pubkey>`, `room-add <name>`, `room-delete <name>`,
  `room-members <name>`, `room-kick <name> <pubkey>`.
  Device commands (need a RUNNING BBS; served via the admin socket, see
  adminserver.py): `contacts` (device contact list — everyone heard via
  advert incl. repeaters/room servers, sorted by last_advert, with type
  label, route hops/flood/direct, position), `device-info` (name/pubkey +
  `query_device_info()` output), `advert [--flood]`, `advert-channels`.
  Client side is the sync `_rpc()` (stdlib `socket`, `_RPC_TIMEOUT` 90 s >
  server handler timeout); socket missing → clear "is it running?" error,
  DB commands keep working. `_CONTACT_TYPES` maps advert type ints to labels.
  `pubkey` accepts a unique prefix; `_resolve_pubkey()` resolves it.
  `_Parser` subclasses `ArgumentParser` to raise `ValueError` instead of
  calling `sys.exit()`, so REPL errors are caught gracefully. ANSI colours
  (auto-disabled when not a TTY); column widths computed dynamically from data.
  Startup banner shows BBS name, db path, configured rooms, and live stats.
- `app/bbs/adminserver.py` — `AdminServer`: Unix-domain-socket RPC server
  for the admin CLI's device commands. The radio has ONE connection (held
  by the BBS process), so admin.py cannot open its own — it delegates.
  Socket at `socket_path(db_path)` = `dirname(db_path)/admin.sock` (both
  sides derive it from config alone; in Docker that's /data, so
  `docker exec` and the host both reach it), chmod 600, stale file
  unlinked on start. Protocol: one request per connection, one JSON line
  each way (`{"cmd", "args"}` → `{"ok", "data"|"error"}`). Handlers are
  injected async callables (`Handler = Callable[[dict], Awaitable[object]]`)
  built in `bbs.py::_admin_handlers()` — this module has no meshcore
  import and is fully unit-testable. Handler exceptions/timeouts (60 s)
  become error responses, never server crashes; unserializable handler
  results are caught. Socket start failure in bbs.py is non-fatal
  (logged; admin.py degrades to DB-only). Handlers in bbs.py:
  `_admin_contacts` (fresh `get_contacts()`, explicit `_CONTACT_FIELDS`
  selection keeps the reply JSON-safe), `_admin_device_info`,
  `_admin_advert` (flood arg defaults to `bbs.advert.flood`),
  `_admin_advert_channels` (errors if no channels configured;
  `_send_channel_adverts()` returns the list of channels actually sent).
- `app/bbs/weather.py` — `WeatherProvider` Protocol (structural: any class with
  `async def fetch(location) -> str` qualifies). Providers RAISE on failure
  (`WeatherError`/`ClientError`/`TimeoutError`) — never return error strings —
  so `ChainedWeatherProvider` can fall through. Default chain (wired in
  `bbs.py`): `WttrInProvider` (format `"%l: %c %t %h %w %p %P"`) first,
  `OpenMeteoProvider` as fallback (geocoding + forecast, WMO code map,
  compact single-line output via the pure `_format_open_meteo()` — network-free
  testable). Only a total chain failure produces a user-facing message
  (translated via `Messages`). Unexpected exceptions propagate on purpose.
- `app/bbs/solar.py` — same pattern as weather.py: `SolarProvider` Protocol
  (`async def fetch() -> str`, no arguments — solar data is global),
  providers RAISE (`SolarError`/`ClientError`/`TimeoutError`). Chain:
  `HamQslProvider` (one XML from hamqsl.com with indices AND ready-made HF
  band ratings; parsed by the pure `_format_hamqsl()`) first,
  `NoaaSwpcProvider` (official JSON, indices only via `_format_noaa()`) as
  fallback. One deliberate difference to weather: `ChainedSolarProvider`
  CACHES a success for 15 min (`_CACHE_TTL`, monotonic clock) — solar data
  moves slowly (Kp 3-hourly, flux daily); failures are never cached.
  Rating words (Good/Fair/Poor) stay untranslated like SNR/RSSI.
- `app/bbs/commands.py` — async command parser. `handle()` is async; sync
  handlers are dispatched transparently via `asyncio.iscoroutine`. Depends
  only on `BBSStore`, the `WeatherProvider` protocol, and `Messages`
  (no meshcore/config import → unit-testable). Returns `CommandResult`
  (messages + optional `on_delivered` commit callback). `!join`, `!post`,
  and `!read` call `update_room_activity`; other commands do not count as
  room activity. `_chunk()` packs replies by UTF-8 BYTES (not characters —
  umlauts are 2 bytes, emoji up to 4; `_btrunc()` never splits a multibyte
  sequence). `_resolve_msg_target()` resolves `!msg` targets: exact name →
  pubkey prefix (≥4 hex chars) → name prefix; ambiguity returns candidates
  with 8-char key prefixes instead of guessing. All user-facing text goes
  through `self._t(...)` (see messages.py).
- `app/bbs/messages.py` — gettext-style catalog: the ENGLISH template is the
  key, `DE` maps it to German, unknown keys fall back to themselves. Plurals
  are template PAIRS chosen by the caller (`"{n} posts" if n != 1 else
  "{n} post"`) because German plurals don't follow +s. `bbs.language`
  selects the catalog, `bbs.strings` overrides single strings (keyed by the
  English template). Broken placeholders in a translation/override fall back
  to the English original with a warning. A unit test enforces identical
  placeholders between every EN key and its DE value. Strings without
  natural language (post lines, SNR/RSSI) stay plain f-strings; the admin
  CLI stays English (operator tool).
- `app/bbs/util.py` — small shared helpers; currently `fmt_ago()` (compact
  relative time, floors at "1m"), used by both the router and the admin CLI.
- `app/bbs/bbs.py` — `MeshCoreBBS`: connects, applies name/location/radio, syncs
  config rooms into the store, subscribes to CONTACT_MSG_RECV, resolves the
  sender's pubkey_prefix → full contact, dispatches to the router, sends
  replies, and only runs `on_delivered` if ALL sends succeeded. When
  `bbs.rooms.timeout > 0`, starts `_room_timeout_task` — a background
  coroutine that polls every `timeout/4` minutes (min. 1 min) and calls
  `leave_room` + `set_current_room(None)` for each expired membership.
  When `bbs.advert.times` is non-empty, starts `_advert_times_task` — sends
  `send_advert(flood=advert.flood)` at each configured UTC time (HH:MM) daily.
  `_next_advert_time(times)` finds the nearest upcoming slot across the list
  (today or tomorrow); tasks sleep until that timestamp and recalculate after each fire.
  When `bbs.channels.times` is non-empty and `bbs.channels.names` is non-empty,
  starts `_advert_in_channels_times_task` — sends `_render_channel_text(channels.text, bbs.name)` (supports `{name}` and legacy `%s`; a literal `%` cannot crash)
  to each named channel at the configured UTC times. `_resolve_channel(name)` queries
  the device via `send_device_query()` (for `max_channels`) and `get_channel(idx)` per
  slot; creates the channel in the first empty slot via `set_channel()` if not found
  (key auto-derived from name hash for `#` channels). Raises `RuntimeError` on failure;
  `_send_channel_adverts()` catches it, logs a warning, and skips that channel.
  When `bbs.storage.post_ttl_days > 0`, starts `_post_cleanup_task_fn` — soft-deletes
  posts older than `post_ttl_days` days, checks every `ttl/4` days (min. 1h).
  When `bbs.messaging.inbox_notify_interval > 0`, starts `_inbox_notify_interval_task`
  — polls every `inbox_notify_interval` minutes and sends a reminder DM to
  each user with undelivered PMs whose last notification is older than the
  interval. Immediate notification on `!msg` is triggered via
  `CommandResult.inbox_notify_pubkey` → `_notify_inbox()` in `bbs.py`.
  `_inbox_notify_last: dict[str, float]` tracks the last notification time
  per pubkey (monotonic clock) so the interval is respected across both paths.
  ALL background tasks are spawned via `_spawn()` with a done-callback that
  logs crashes loudly (a silent task death was the historic failure mode);
  the schedule-driven tasks additionally wrap their ACTION (not the sleep)
  in try/except so a transient radio error costs one cycle, not the day.
  An unconditional `_heartbeat_task` touches `dirname(db_path)/heartbeat`
  every 30 s; the Docker HEALTHCHECK watches its mtime (a hung event loop
  is invisible to `restart: unless-stopped`).
  `_mc`/`_router` are Optional until `start()`; access goes through
  `_require_mc()`/`_require_router()` (mypy-provable, clear RuntimeError).
  RX log: `RX_LOG_DATA` payloads land in a `deque(maxlen=8)` ring buffer;
  `_claim_rx_log_for_dm()` claims the oldest FRESH (≤5 s) entry with
  `payload_type == 2` (TXT_MSG) FIFO — adverts (type 4) between the packet
  and the fetch-delayed CONTACT_MSG_RECV no longer misattribute `!ping`
  data. Residual limit: an overheard third-party DM in the same window is
  indistinguishable without a firmware correlation ID.
  `Messages` is built once in `__init__` from `cfg.bbs.language`/`strings`
  and shared with the router, the weather chain, and the two DMs sent
  directly from bbs.py (room-timeout eviction, inbox notification).

## Model

Pull-based: `!post` only stores; others see it when they `!read`. `!msg`
queues a private message pulled via `!inbox`. Every command replies only to
the sender — nothing is pushed to other users.

Auto-leave: if `bbs.rooms.timeout > 0`, users who have not sent `!join`,
`!post`, or `!read` in a room for that many minutes are removed from it.
`last_activity` is set on those three commands only — other commands
(`!help`, `!msg`, `!inbox`, etc.) do not count as room activity. On removal,
the user receives a DM explaining what happened and how to rejoin.

## Commands

`!help`, `!rooms`, `!join <room>`, `!leave`, `!post <text>`, `!read`,
`!search <text>`, `!undo`, `!msg [name] <text>`, `!reply <text>`, `!inbox`,
`!who`, `!users`, `!seen <name>`, `!whoami`,
`!whereami` / `!pwd`, `!stats`, `!weather [location]`, `!ping`, `!solar`.

There are NO admin commands over the mesh — maintenance and privileged
actions go through the admin CLI (`app/admin.py`). The former secret
commands `!advert`, `!advert_channels`, and `!restart` were removed and
now answer with the generic "Unknown command" response; `advert` and
`advert-channels` live on as admin CLI device commands (via the admin
socket, see adminserver.py).

- Rooms come from config only; users join, never create.
- `!help` — ONE-DM summary of command names only (airtime guard; a test
  enforces ≤150 bytes in EN and DE, with and without extras). Descriptions
  are sent per request: `!help <cmd>` (leading `!` tolerated; `pwd` maps to
  the `whereami` entry via `_HELP_ALIASES`), optional commands only via
  `!help extras`. `_COMMAND_HELP` maps cmd → detail line, `_HELP_ORDER`
  fixes the summary (shows `!pwd`, omits `help` itself); the
  `!help extras` hint is appended only when at least one optional command
  is enabled. `!help <cmd>` for a DISABLED optional command answers
  "Unknown command" (consistent with dispatching); `!help extras` with
  none enabled says so. Consistency tests enforce that every dispatched
  command has a detail line and every core command appears in the summary.
- `bbs.features.commands` controls which optional commands are available.
  Currently: `seen`, `whoami`, `stats`, `weather`, `ping`, `solar`
  (all in the default list — an existing config with an explicit
  `commands:` list must add the DB-backed three to keep them).
  Commands not listed behave as unknown —
  `_OPTIONAL_COMMANDS` in `commands.py` maps name → help string; `handle()`
  checks membership before dispatching; they appear only in `!help extras`,
  never in the `!help` summary.
- `!rooms` — lists rooms with member count and last-post age (`2h ago`).
  Uses `store.list_rooms_with_stats()` (LEFT JOIN rooms/memberships/posts).
- `!read [n]` — without a number, capped at `messaging.read_limit` (default 5,
  0 = unlimited) as an airtime guard; a trailing "+N more — send !read again"
  hint is appended. An explicit number overrides the cap. Each post is shown as `author Xm: text` (relative timestamp via
  `_fmt_ago`). The seen-marker advances only to the last fetched post, so the
  remainder stays unread and can be retrieved with another `!read`.
- `!search <text>` — searches non-deleted posts in the current room
  (case-insensitive substring, newest first, `store.search_posts()` with
  the shared `_like_escape`). Capped at `messaging.read_limit` like `!read`
  (0 = unlimited) with a "+N more — refine your search" hint via
  `count_search_posts()`. Minimum 2 characters. Deliberately NOT room
  activity and does not move the seen-marker — searching is browsing
  history, not reading new posts.
- `!seen <name>` — shows a user's last activity (`users.last_seen` +
  `fmt_ago`). Target forms like `!msg`: bare name (spaces allowed — the
  whole argument is the name), `[name]`/`@[name]`, or a pubkey prefix;
  resolution via `_resolve_msg_target(token, hint=...)` — the `hint`
  parameter makes the ambiguity message teach `!seen <keyprefix>` instead
  of the `!msg` form.
- `!undo` — soft-deletes the caller's newest own post if younger than
  `rooms.undo_window` (default 600 s, 0 = no limit); repeatable
  (newest-first). Only stops FUTURE delivery — copies already received
  over the air are gone.
- `!reply <text>` — answers the sender of the last DELIVERED inbox message
  (`users.last_pm_from`, set in the `!inbox` commit). No target until the
  first successful `!inbox`; deleted senders are handled gracefully.
- `!msg` recipient: `[Name With Spaces]` or the mention form `@[Name]`
  (the `@` is optional) or a bare single word — or a pubkey prefix
  (≥4 hex chars, case-insensitive). Resolution: exact name → key prefix →
  name prefix; ambiguity lists candidates with 8-char key prefixes and
  teaches `!msg <keyprefix> <text>`. `!users` shows the prefix per user. User-facing text shows the
  plain `[name]` form because the MeshCore client renders a literal `@[` as
  a mention and mangles it. Sending to yourself is rejected.
- `!inbox` — shows sender name, relative time (`5m`), and message text.
- `!who` — lists all current members of the user's room with their last-activity
  time (`5m`, `2h`, `3d`). Uses `store.room_members()` (JOIN memberships + users,
  sorted by last_activity DESC). Shows `—` for members with no activity recorded.
- `!users` lists the `bbs.messaging.user_list_limit` most-recently-active users (default 5,
  excluding the caller), names in `[name]` form for pasting into `!msg`, with
  last-seen time appended (`5m`, `2h`, `3d`).
- `!ping` — returns SNR, RSSI, hop count, and path of the user's last received
  packet. Data comes from `RX_LOG_DATA` events (subscribed in `bbs.py`), parsed
  by `_parse_rx_log_data()` after `_claim_rx_log_for_dm()` picks the matching
  ring-buffer entry (fresh, TXT_MSG type, FIFO), then passed as `signal_info`
  to `CommandRouter.handle()`. Unavailable for messages fetched via
  `start_auto_message_fetching()` (no associated radio event).
  Every parsed `signal_info` is also recorded per user via
  `store.add_signal_record()` in `_on_contact_msg_recv` (regardless of
  whether `ping` is enabled; TTL from `bbs.storage.signal_ttl_days`,
  default 30 days, 0 = keep forever). With >1 sample in the last 24h,
  `!ping` appends a trend line: `24h: avg SNR 6.2 dB, -95 dBm (12 packets)`.
- `!whereami` / `!pwd` — aliases for the same handler; show the user's
  current room with unread post count, or prompt to `!join` if not in one.
- `!stats` — shows total user, post (non-deleted), and room counts via
  `store.get_stats()` (single SELECT with three sub-selects). Optional
  command (like `!seen` and `!whoami`) — listed via `!help extras`.
- `!weather [location]` — fetches a weather summary via the provider chain
  (wttr.in, then open-meteo on failure). Uses
  `bbs.features.weather_location` from config if no argument is given. Default format
  `"%l: %c %t %h %w %p %P"` gives e.g. `Berlin: ⛅️ +18°C 65% 15km/h 0.0mm 1013hPa`.
  Format is set in the `WttrInProvider` constructor in `bbs/bbs.py`.
- `!solar` — solar indices + HF band conditions via the solar chain
  (hamqsl.com, NOAA SWPC fallback), cached 15 min. Output packs indices,
  day AND night band lines into a single 150-byte DM (band names
  compacted: `80m-40m`→`80-40`); a test enforces the byte budget.
  Space weather affects HF, not 868 MHz LoRa — it's a ham service, not
  mesh diagnostics.

## Constraints / gotchas

- Reply length: `bbs.messaging.max_len` (default 150) BYTES per DM — chunking
  measures UTF-8 bytes, never characters (umlauts 2 B, emoji up to 4 B). Contact messages don't carry a sender-name prefix (unlike channel
  messages), but staying at 150 keeps replies inside the firmware limit regardless of
  firmware specifics. `commands._chunk()` packs lines greedily and splits
  across multiple DMs when needed.
- Paginated replies (multiple DMs) are sent with a `bbs.messaging.inter_delay` seconds
  pause between each message (default 2.0, configurable in `config.yaml`), so the
  radio has time to transmit before the next packet is queued.
- Outgoing DMs use `send_msg_with_retry(max_attempts=5, max_flood_attempts=2,
  flood_after=3)`: 3 direct-path attempts, then `reset_path()` + 2 flood
  attempts. Returns `None` on total failure; `_send_dm()` maps this to `False`
  so the two-phase commit in `_on_contact_msg_recv` does not advance the
  seen-marker on a failed send.
- Rate limit: `bbs.messaging.rate_limit` (default 10, 0 = off) replies per
  user per sliding 60 s window, checked at the TOP of `handle()` before any
  work (even the upsert). First excess message → one warning DM; after
  that, EMPTY `CommandResult([])` (silent drop — replying to spam would
  burn the airtime the limit protects). State is in-memory in the router
  (`_rate_events`/`_rate_warned`), deliberately not persisted; scheduled
  tasks and room-timeout DMs are unaffected.
- Contacts auto-add on advert by default, so senders are usually already
  resolvable; ambiguous/unknown prefixes are handled, never guessed.
- Disconnect with `max_attempts_exceeded` cancels the main task for an
  orderly shutdown (no `sys.exit()` inside a callback). An external
  supervisor (systemd/Docker) is expected to restart.
- Docker layout: `/app` (code, read-only), `/config` (config.yaml, bind-mount
  `:ro`), `/data` (bbs.db + logs, writable volume). `BBS_CONFIG=/config/config.yaml`
  is set in the image. `.dockerignore` excludes `.venv`, `config/`, `data/`.
  The image runs as non-root UID 1000 ("bbs") — host `data/` must be
  chowned accordingly; serial access via `group_add` (dialout GID) in
  compose. INTERACTIVE shells in the container (docker exec -it bash/sh,
  UI shell buttons) exec straight into the admin REPL via `~/.shinit`
  (sourced from `~/.bashrc` for bash, via the `ENV` env var for dash;
  both only apply to interactive shells, so main.py, `sh -c`, and the
  HEALTHCHECK are unaffected). `BBS_SHELL=1` bypasses it for a plain
  shell. HEALTHCHECK watches `/data/heartbeat` (90 s staleness = three
  missed beats); note: "unhealthy" marks only, compose does not restart on it.
  Without Docker: `BBS_CONFIG` env var or `DEFAULT_CONFIG_PATH`
  (repo-root-anchored, cwd-independent).
- YAML time fields MUST be quoted ('09:00'): PyYAML (YAML 1.1) parses
  unquoted `21:00` as the sexagesimal int 1260. `_valid_times` converts
  ints back with a warning, but don't rely on it in examples/docs.
- CI: `.github/workflows/ci.yml` runs `ruff check app tests`, `mypy`, and
  `pytest` (210 tests) on every push/PR. `.pre-commit-config.yaml` mirrors
  it locally (plus file hygiene); the pytest hook is `language: system` so
  it uses the active venv. mypy config lives in `pyproject.toml`
  (`check_untyped_defs`, missing-stub ignores for meshcore/aiomqtt).
- CI/CD: `.github/workflows/docker.yml` builds and pushes a multi-platform image
  (`linux/amd64`, `linux/arm64`) to `ghcr.io/carsten-walther/meshcore-bbs` on every
  push to `main` (`:latest`) and on `v*` tags (`:v1.2.3`). Uses `GITHUB_TOKEN` —
  no additional secrets required.

## Conventions

- Python 3.14, async throughout, clean/minimal code and comments.
- Log/error messages in English. INFO for once-per-startup/lifecycle events,
  DEBUG for per-message detail.
- User-facing text: route through `self._t("English template", **kwargs)`.
  The English string IS the catalog key — add the German translation to
  `DE` in `messages.py` with IDENTICAL placeholders (enforced by test).
  Plurals via template pairs. Never translate technical-only strings or
  the admin CLI.
- After changing a module, run the real checks: `ruff check app tests`,
  `mypy`, `pytest` (all three are what CI runs; store/commands/messages
  are fully testable without hardware). New behaviour needs a test —
  the suite doubles as the regression record of past review findings.
