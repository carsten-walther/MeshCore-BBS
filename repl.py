"""Interactive BBS REPL for manual testing without LoRa hardware.

Usage:
    python repl.py                       # temp DB, default rooms, no weather default
    python repl.py --config config.yaml  # load rooms + weather_location from config
    python repl.py --db bbs.db           # use the live database (read/write)

Meta-commands (not sent to the BBS):
    !su <name>   — switch to a different simulated user
    !quit / !q   — exit
"""

import argparse
import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path

from bbs.commands import CommandRouter
from bbs.config import load_config
from bbs.store import BBSStore
from bbs.weather import WttrInProvider

logging.basicConfig(level=logging.WARNING)  # silence library noise during interactive use


def _fake_pubkey(name: str) -> str:
    """Derive a stable 64-char hex pubkey from a name so !msg lookups work."""
    return hashlib.sha256(name.encode()).hexdigest() * 2


def _print_reply(messages: list[str]) -> None:
    for msg in messages:
        width = min(max(len(line) for line in msg.splitlines()), 60)
        border = "─" * width
        print(f"  ┌{border}┐")
        for line in msg.splitlines():
            print(f"  │ {line}")
        print(f"  └{border}┘")
        if len(msg.encode()) > 150:
            print(f"  ⚠  {len(msg.encode())} bytes — exceeds 150-byte DM limit!")


async def run(db_path: Path, rooms: list[str], weather_location: str) -> None:
    store = BBSStore(db_path)
    store.connect()

    for room in rooms:
        store.create_room(room, created_by="config")

    router = CommandRouter(
        store,
        weather_provider=WttrInProvider(),
        weather_location=weather_location,
    )

    name = "Alice"
    pubkey = _fake_pubkey(name)

    print("BBS REPL  —  !help for BBS commands  |  !su <name> to switch user  |  !q to quit")
    print(f"Rooms:  {', '.join(rooms)}")
    print(f"User:   {name}  ({pubkey[:12]}...)")
    print()

    while True:
        try:
            text = input(f"[{name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not text:
            continue

        if text.lower() in ("!quit", "!q", "quit", "q"):
            print("Bye.")
            break

        # Switch simulated user — useful for testing !msg / !inbox.
        if text.lower().startswith("!su "):
            name = text[4:].strip()
            pubkey = _fake_pubkey(name)
            print(f"  → switched to {name!r}  ({pubkey[:12]}...)")
            continue

        result = await router.handle(pubkey, name, text)
        _print_reply(result.messages)

        if result.on_delivered:
            result.on_delivered()

    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MeshCore BBS interactive REPL")
    parser.add_argument("--config", metavar="FILE", help="load rooms/weather from config.yaml")
    parser.add_argument("--db", metavar="FILE", help="SQLite DB path (default: temporary)")
    args = parser.parse_args()

    rooms = ["lobby", "tech", "test"]
    weather_location = ""

    if args.config:
        cfg = load_config(args.config)
        rooms = cfg.bbs.rooms
        weather_location = cfg.bbs.weather_location

    if args.db:
        asyncio.run(run(Path(args.db), rooms, weather_location))
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            asyncio.run(run(Path(tmpdir) / "repl.db", rooms, weather_location))


if __name__ == "__main__":
    main()