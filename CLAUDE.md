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

- `bbs/config.py` — dataclass config + YAML loader. Auto-creates
  `config.yaml` with defaults if missing. Sections: connection (tcp/serial/
  ble), radio (freq/bw/sf/cr/tx_power in MeshCore units, None = leave as-is),
  bbs (name, rooms, db_path). NOTE: `field(default_factory=...)` fields have
  no class attribute, so the loader must inline their default (that bit us
  with `rooms`).
- `bbs/connection.py` — connection factory (tcp/serial/ble), try/except only
  for logging (meshcore raises on failure).
- `bbs/store.py` — SQLite persistence. Tables: users, rooms, memberships,
  posts, private_messages. Users keyed by FULL public key (never the 12-char
  pubkey_prefix — prefixes can collide). WAL mode. Read and mark-seen are
  deliberately separate so a failed radio send can't drop messages.
- `bbs/commands.py` — pure prefix-command parser (depends only on the store,
  no meshcore/config import → unit-testable). Returns `CommandResult`
  (messages + optional `on_delivered` commit callback).
- `bbs/bbs.py` — `MeshCoreBBS`: connects, applies name/radio, syncs
  config rooms into the store, subscribes to CONTACT_MSG_RECV, resolves the
  sender's pubkey_prefix → full contact, dispatches to the router, sends
  replies, and only runs `on_delivered` if ALL sends succeeded.

## Model

Pull-based: `!post` only stores; others see it when they `!read`. `!msg`
queues a private message pulled via `!inbox`. Every command replies only to
the sender — nothing is pushed to other users.

## Commands

`!help`, `!rooms`, `!join <room>`, `!leave`, `!post <text>`, `!read`,
`!msg [name] <text>`, `!inbox`, `!users`, `!whoami`.

- Rooms come from config only; users join, never create.
- `!msg` recipient: `[Name With Spaces]` or the mention form `@[Name]`
  (the `@` is optional) or a bare single word. User-facing text shows the
  plain `[name]` form because the MeshCore client renders a literal `@[` as
  a mention and mangles it.
- `!users` lists the 5 most-recently-active users (excluding the caller),
  names in `[name]` form for pasting into `!msg`.

## Constraints / gotchas

- Reply length: `_DEFAULT_MAX_LEN = 150` bytes in `commands.py`. Contact
  messages don't carry a sender-name prefix (unlike channel messages), but
  staying at 150 keeps replies inside the firmware limit regardless of
  firmware specifics. `commands._chunk()` packs lines greedily and splits
  across multiple DMs when needed.
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

## Open ideas / next steps

- Per-room member listing (`!who`), activity/last-seen in `!users`.
- Make `_USER_LIST_LIMIT` a config field if per-deployment tuning is wanted.
- Contact-list pruning for large meshes (finite device contact list).