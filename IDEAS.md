# Ideas & Roadmap

Design notes for features that are planned or under consideration but not
yet implemented. Nothing in this document describes current behaviour —
see [README.md](README.md) for that.

---

## BBS federation (concept)

**Status: concept — not implemented.**

### Goal

Two or more MeshCore-BBS instances — say Leipzig and Dresden, each serving
its own LoRa mesh — share selected rooms. A `!post` into the shared room
`saxony` on BBS A shows up for readers on BBS B on their next `!read`.

The key insight: **the pull model extends naturally.** Remote posts are
simply rows in the local `posts` table — `!read`, seen-markers,
`read_limit`, and the post TTL all work unchanged. No user-facing push,
and therefore no extra airtime cost from federation itself.

### Transport: MQTT, not the mesh

- **Over the mesh** sounds romantic but is the wrong tool: federation is
  most valuable when the meshes are *separate* (different cities) — then
  there is no radio path between them. And if they shared a mesh, syncing
  would burn the very airtime all users share.
- **MQTT** is nearly free in this codebase: `aiomqtt` is already a
  dependency, `MqttPublisher` provides the pattern (reconnect loop, queue,
  TLS/auth via `MqttBrokerConfig`), and the BBS host already assumes
  internet access (weather providers).

Topology: a **star over one shared broker**. Every BBS publishes only its
*locally created* posts and subscribes to the shared topic. Posts received
via federation are never re-published, so forwarding loops are impossible
by construction — yet every instance still sees everything.

### Message flow

```
Topic: meshcore-bbs/{network}/{room}

BBS A (LEJ)                    broker                     BBS B (DD)
!post "hello" ──> publish ──> saxony ──> subscribe ──> add_federated_post()
                                                       └─> visible via !read:
                                                           "Alice@LEJ 5m: hello"
```

Payload (JSON): `origin` (the BBS's public key — already available via
`self_info`), `origin_id` (local post ID), `room`, `author_name`, `text`,
`created_at`. Display names get an origin tag (`Alice@LEJ`); the short
code already exists in the config as `mqtt.iata`.

### The three core technical points

1. **Deduplication.** QoS 1 means at-least-once delivery — duplicates are
   possible. The `posts` table gains two nullable columns
   `origin`/`origin_id` (via the usual `ALTER TABLE` migration) plus a
   UNIQUE index; `INSERT OR IGNORE` does the rest. `origin IS NULL` marks
   a local post.
2. **Offline backlog.** If BBS B is down, posts must not be lost. MQTT
   covers this with a persistent session (fixed client ID,
   `clean_start=False`, QoS 1) — the broker buffers until the subscriber
   returns.
3. **Router integration.** Following the `inbox_notify_pubkey` pattern,
   `CommandResult` gains an optional field (e.g. `federate_post`) that
   `bbs.py` hands to the federation link — the router stays free of MQTT
   dependencies and thus purely unit-testable.

### Config sketch

```yaml
bbs:
  federation:
    enabled: true
    network: saxony-mesh      # shared namespace on the broker
    rooms: [saxony]           # only explicitly listed rooms are shared
    tag: LEJ                  # origin tag (default: mqtt.iata)
    broker:                   # own broker block, same format as mqtt.brokers
      host: broker.example.org
      username: bbs-leipzig
      tls: true
```

### Deliberate v1 exclusions

- **No PM federation** — identity and routing across BBS boundaries (who
  is `Alice@DD`, how do I reply to her?) is a separate, much harder
  problem.
- **No federated `!undo`** — v1 is append-only; a delete event type could
  be added later.
- **Trust = broker access.** Anyone allowed to publish on the broker can
  inject posts. V1 relies on broker auth/ACLs + TLS (all supported);
  HMAC-signed payloads would be the later hardening step. Must be
  documented in the README security section.

### Effort and slicing

The largest feature so far, but it splits into three increments:

1. Schema migration + store methods (`add_federated_post`, dedup).
2. New module `federation.py` (connection, publish/subscribe, payload
   parsing — the pure parts testable without a network).
3. Wiring in `bbs.py` + config + docs.

Most of it is testable without a broker; an end-to-end test could run two
store instances against a local Mosquitto, or the payload handling can be
covered purely by unit tests.

### Open decisions

- Own broker block in the config (as sketched) vs. reusing an entry from
  `mqtt.brokers`?
- Is broker ACL sufficient as the v1 trust model?

---

## Further ideas (backlog)

Briefly discussed, in no particular order:

- **Admin command queue** — let the admin CLI trigger runtime actions
  (advert, channel advert, restart) in the running BBS process via a small
  `admin_commands` table in SQLite: the CLI enqueues, a background task in
  the BBS polls every few seconds and executes. Stale commands (older than
  ~60 s) are discarded so a restart queued while the BBS was down doesn't
  fire hours later. Replaces the removed `!advert`/`!advert_channels`/
  `!restart` DM commands.
- **`!subscribe <room>`** — opt-in DM notification on new posts. Breaks
  the pull model deliberately, but only per user request; needs airtime
  discipline (batched hints like the inbox reminder, not one DM per post).
- **Read-only web dashboard** — small FastAPI/aiohttp page against the
  SQLite DB (WAL already allows parallel reads): users, posts, rooms,
  RX log, uptime, signal history graphs. Far more comfortable than the
  admin CLI for a quick glance.
- **Polls** — `!poll <question>` / `!vote <n>`; classic BBS feature, good
  for the mesh community feel.
- **Banlist** — `admin.py kick` exists, but the user can rejoin
  immediately. A `banned` column on `users` plus an admin CLI command
  (`ban`/`unban`) would make it stick; banned senders get silence (no
  reply costs airtime), consistent with the rate limiter.
- **Automatic DB backup** — daily `VACUUM INTO` snapshot with rotation
  into `data/backups/`, as another supervised background task.
- **Multi-day weather** — `!weather tomorrow` / a compact multi-day
  forecast; Open-Meteo already returns the data, only the formatting is
  missing. (Space-weather/solar indices are implemented — see `!solar`.)
- **More languages** — the i18n catalog in `messages.py` makes adding
  FR/ES/NL/PL pure translation work.
- **BBS games / fortune** — a quote of the day (e.g. shown with `!help`),
  number guessing, Wordle-like; retro charm, technically trivial, no
  external dependencies.
