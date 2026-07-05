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

- `bbs/mqtt.py` — `MqttPublisher`: one background task per enabled broker (asyncio.Queue
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
- `bbs/config.py` — dataclass config + YAML loader. Auto-creates
  `config.yaml` with defaults if missing. Sections: connection (tcp/serial/
  ble), radio (freq/bw/sf/cr/tx_power in MeshCore units, None = leave as-is),
  bbs (name, latitude, longitude, db_path, advert, advert_flood, advert_times,
  flood_scope, advert_in_channels_times, advert_in_channels_text, advert_in_channels,
  admin_pubkeys, inbox_notify_interval, post_ttl_days, log_file, log_backup_count,
  rooms, room_timeout, weather_location, additional_commands). NOTE: `field(default_factory=...)` fields have no class
  attribute, so the loader must inline their default (that bit us with `rooms`).
- `bbs/device.py` — standalone async helpers for device setup: `apply_device_name`,
  `apply_device_loc`, `apply_radio_config`, `apply_flood_scope`, `query_device_info`.
  Depend only on `MeshCore` + config values — no store, router, or MQTT dependency,
  so independently testable. Called from `bbs.py` during `start()`.
  `apply_flood_scope(mc, flood_scope)` calls `set_flood_scope` (runtime) and
  `set_default_flood_scope` (persisted) on the device; empty string skips both.
  Both methods live in `MessagingCommands` (accessible via `mc.commands`).
- `bbs/connection.py` — connection factory (tcp/serial/ble), try/except only
  for logging (meshcore raises on failure).
- `bbs/store.py` — SQLite persistence. Tables: users, rooms, memberships,
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
  Admin-only methods (used by `admin.py`): `list_all_users()` (all users, no
  limit), `list_posts(room, limit)` (newest first), `delete_post(id)` (soft,
  returns bool), `delete_posts_in_room(room)` (soft, returns count),
  `kick_user(pubkey)` (leave all rooms, returns room list), `delete_user(pubkey)`
  (physical DELETE from users + soft-delete posts/PMs, returns bool),
  `delete_room(name)` (DELETE memberships + room row, soft-delete posts,
  reset `current_room=NULL` for affected users; returns bool).
  `get_stats()` — single SELECT with sub-selects for user, post, room counts.
- `admin.py` — standalone admin CLI + interactive shell. Single-command mode
  (`python admin.py stats`) and REPL mode (`python admin.py`, no args). Uses
  `BBSStore` directly; safe to run alongside a live BBS (WAL). Commands:
  `stats`, `users`, `rooms`, `posts <room> [-n N]`, `purge-posts --days N`,
  `purge-posts --room <room>`, `delete-post <id>`, `kick <pubkey>`,
  `delete-user <pubkey>`, `room-add <name>`, `room-delete <name>`,
  `room-members <name>`, `room-kick <name> <pubkey>`.
  `pubkey` accepts a unique prefix; `_resolve_pubkey()` resolves it.
  `_Parser` subclasses `ArgumentParser` to raise `ValueError` instead of
  calling `sys.exit()`, so REPL errors are caught gracefully. ANSI colours
  (auto-disabled when not a TTY); column widths computed dynamically from data.
  Startup banner shows BBS name, db path, configured rooms, and live stats.
- `bbs/weather.py` — `WeatherProvider` Protocol (structural: any class with
  `async def fetch(location) -> str` qualifies) + `WttrInProvider` as the
  default implementation. Format string passed to the constructor maps to
  wttr.in format codes; default is `"%l: %c %t %h %w %p %P"` (location,
  emoji, temp, humidity, wind, precipitation, pressure). To swap providers,
  implement the protocol and pass an instance to `CommandRouter`.
- `bbs/commands.py` — async command parser. `handle()` is async; sync
  handlers are dispatched transparently via `asyncio.iscoroutine`. Depends
  only on `BBSStore` and the `WeatherProvider` protocol (no meshcore/config
  import → unit-testable). Returns `CommandResult` (messages + optional
  `on_delivered` commit callback). `!join`, `!post`, and `!read` call
  `update_room_activity`; other commands do not count as room activity.
- `bbs/bbs.py` — `MeshCoreBBS`: connects, applies name/location/radio, syncs
  config rooms into the store, subscribes to CONTACT_MSG_RECV, resolves the
  sender's pubkey_prefix → full contact, dispatches to the router, sends
  replies, and only runs `on_delivered` if ALL sends succeeded. When
  `bbs.room_timeout > 0`, starts `_room_timeout_task` — a background
  coroutine that polls every `timeout/4` minutes (min. 1 min) and calls
  `leave_room` + `set_current_room(None)` for each expired membership.
  When `bbs.advert_times` is non-empty, starts `_advert_times_task` — sends
  `send_advert(flood=advert_flood)` at each configured UTC time (HH:MM) daily.
  `_next_advert_time(times)` finds the nearest upcoming slot across the list
  (today or tomorrow); tasks sleep until that timestamp and recalculate after each fire.
  When `bbs.advert_in_channels_times` is non-empty and `bbs.advert_in_channels` is non-empty,
  starts `_advert_in_channels_times_task` — sends `advert_in_channels_text % bbs.name`
  to each named channel at the configured UTC times. `_resolve_channel(name)` queries
  the device via `send_device_query()` (for `max_channels`) and `get_channel(idx)` per
  slot; creates the channel in the first empty slot via `set_channel()` if not found
  (key auto-derived from name hash for `#` channels). Raises `RuntimeError` on failure;
  `_send_channel_adverts()` catches it, logs a warning, and skips that channel.
  When `bbs.post_ttl_days > 0`, starts `_post_cleanup_task_fn` — soft-deletes
  posts older than `post_ttl_days` days, checks every `ttl/4` days (min. 1h).
  When `bbs.inbox_notify_interval > 0`, starts `_inbox_notify_interval_task`
  — polls every `inbox_notify_interval` minutes and sends a reminder DM to
  each user with undelivered PMs whose last notification is older than the
  interval. Immediate notification on `!msg` is triggered via
  `CommandResult.inbox_notify_pubkey` → `_notify_inbox()` in `bbs.py`.
  `_inbox_notify_last: dict[str, float]` tracks the last notification time
  per pubkey (monotonic clock) so the interval is respected across both paths.

## Model

Pull-based: `!post` only stores; others see it when they `!read`. `!msg`
queues a private message pulled via `!inbox`. Every command replies only to
the sender — nothing is pushed to other users.

Auto-leave: if `bbs.room_timeout > 0`, users who have not sent `!join`,
`!post`, or `!read` in a room for that many minutes are removed from it.
`last_activity` is set on those three commands only — other commands
(`!help`, `!msg`, `!inbox`, etc.) do not count as room activity. On removal,
the user receives a DM explaining what happened and how to rejoin.

## Commands

`!help`, `!rooms`, `!join <room>`, `!leave`, `!post <text>`, `!read`,
`!msg [name] <text>`, `!inbox`, `!who`, `!users`, `!whoami`, `!whereami` / `!pwd`,
`!stats`, `!weather [location]`, `!ping`, `!advert` (secret), `!advert_channels` (secret), `!restart` (secret).

- Rooms come from config only; users join, never create.
- `bbs.additional_commands` controls which optional commands are available.
  Currently: `weather`, `ping`. Commands not listed behave as unknown —
  `_OPTIONAL_COMMANDS` in `commands.py` maps name → help string; `handle()`
  checks membership before dispatching; `_cmd_help` only lists enabled ones.
- `!rooms` — lists rooms with member count and last-post age (`2h ago`).
  Uses `store.list_rooms_with_stats()` (LEFT JOIN rooms/memberships/posts).
- `!read [n]` — optional numeric argument limits how many unseen posts are
  returned. Each post is shown as `author Xm: text` (relative timestamp via
  `_fmt_ago`). The seen-marker advances only to the last fetched post, so the
  remainder stays unread and can be retrieved with another `!read`.
- `!msg` recipient: `[Name With Spaces]` or the mention form `@[Name]`
  (the `@` is optional) or a bare single word. User-facing text shows the
  plain `[name]` form because the MeshCore client renders a literal `@[` as
  a mention and mangles it. Sending to yourself is rejected.
- `!inbox` — shows sender name, relative time (`5m`), and message text.
- `!who` — lists all current members of the user's room with their last-activity
  time (`5m`, `2h`, `3d`). Uses `store.room_members()` (JOIN memberships + users,
  sorted by last_activity DESC). Shows `—` for members with no activity recorded.
- `!users` lists the `bbs.user_list_limit` most-recently-active users (default 5,
  excluding the caller), names in `[name]` form for pasting into `!msg`, with
  last-seen time appended (`5m`, `2h`, `3d`).
- `!ping` — returns SNR, RSSI, hop count, and path of the user's last received
  packet. Data comes from `RX_LOG_DATA` events (subscribed in `bbs.py`), parsed
  by `_parse_rx_log_data()` and stored as `_last_rx_log`. The value is consumed
  and cleared on each `CONTACT_MSG_RECV`, then passed as `signal_info` to
  `CommandRouter.handle()`. Unavailable for messages fetched via
  `start_auto_message_fetching()` (no associated radio event).
- `!whereami` / `!pwd` — aliases for the same handler; show the user's
  current room with unread post count, or prompt to `!join` if not in one.
- `!stats` — shows total user, post (non-deleted), and room counts via
  `store.get_stats()` (single SELECT with three sub-selects). Visible in `!help`.
- `!advert` — secret admin-only command (not in `!help`). Triggers `send_advert(flood=advert_flood)`
  via an `advert_callback` passed to `CommandRouter` from `bbs.py`. Only the user whose
  pubkey starts with any entry in `bbs.admin_pubkeys` (config list) may invoke it; everyone
  else gets the generic "Unknown command" response. Empty list disables the command entirely.
- `!advert_channels` — secret admin-only command (not in `!help`). Immediately calls
  `_send_channel_adverts()` in `bbs.py` via `advert_channels_callback`, posting
  `advert_in_channels_text % bbs.name` to all configured channels (same logic as the
  periodic task). Non-admins get the generic "Unknown command" response.
- `!restart` — secret admin-only command (not in `!help`). Sets `_restart_requested=True`
  and cancels `_main_task` for an orderly shutdown. `start()` returns `True`, and the
  `while True` loop in `main.py` reloads `config.yaml` and starts a fresh `MeshCoreBBS`
  instance. Non-admins get the generic "Unknown command" response.
- `!weather [location]` — fetches a weather summary via wttr.in. Uses
  `bbs.weather_location` from config if no argument is given. Default format
  `"%l: %c %t %h %w %p %P"` gives e.g. `Berlin: ⛅️ +18°C 65% 15km/h 0.0mm 1013hPa`.
  Format is set in the `WttrInProvider` constructor in `bbs/bbs.py`.

## Constraints / gotchas

- Reply length: `bbs.max_msg_len` (default 150) bytes per DM, configurable in
  `config.yaml`. Contact messages don't carry a sender-name prefix (unlike channel
  messages), but staying at 150 keeps replies inside the firmware limit regardless of
  firmware specifics. `commands._chunk()` packs lines greedily and splits
  across multiple DMs when needed.
- Paginated replies (multiple DMs) are sent with a `bbs.inter_msg_delay` seconds
  pause between each message (default 2.0, configurable in `config.yaml`), so the
  radio has time to transmit before the next packet is queued.
- Contacts auto-add on advert by default, so senders are usually already
  resolvable; ambiguous/unknown prefixes are handled, never guessed.
- Disconnect with `max_attempts_exceeded` cancels the main task for an
  orderly shutdown (no `sys.exit()` inside a callback). An external
  supervisor (systemd/Docker) is expected to restart.

## Conventions

- Python 3.14, async throughout, clean/minimal code and comments.
- Log/error messages in English. INFO for once-per-startup/lifecycle events,
  DEBUG for per-message detail.
- After changing a module, sanity-check it (py_compile) and, for store/
  commands, run a quick functional check — they're testable without hardware.

