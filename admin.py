"""MeshCore BBS admin CLI and interactive shell.

Single-command mode:   python admin.py stats
Interactive REPL mode: python admin.py          (no arguments)
"""

import argparse
import os
import shlex
import sys
import time

from bbs.config import load_config
from bbs.store import BBSStore


def _fmt_ago(secs: int) -> str:
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that raises ValueError instead of calling sys.exit() on errors."""
    def error(self, message: str) -> None:
        raise ValueError(message)
    def exit(self, status: int = 0, message: str | None = None) -> None:
        pass


def _build_parser() -> _Parser:
    p = _Parser(prog="", add_help=False)
    sub = p.add_subparsers(dest="command")

    sub.add_parser("stats",  add_help=False)
    sub.add_parser("users",  add_help=False)
    sub.add_parser("rooms",  add_help=False)
    sub.add_parser("help",   add_help=False)
    sub.add_parser("quit",   add_help=False)

    pp = sub.add_parser("posts", add_help=False)
    pp.add_argument("room")
    pp.add_argument("-n", type=int, default=20, metavar="N")

    pu = sub.add_parser("purge-posts", add_help=False)
    g = pu.add_mutually_exclusive_group(required=True)
    g.add_argument("--days", type=int, metavar="N")
    g.add_argument("--room", metavar="ROOM")

    pd = sub.add_parser("delete-post", add_help=False)
    pd.add_argument("id", type=int)

    pk = sub.add_parser("kick", add_help=False)
    pk.add_argument("pubkey")

    pdu = sub.add_parser("delete-user", add_help=False)
    pdu.add_argument("pubkey")

    return p


_HELP = """\
Commands:
  stats                         Show user, post, and room counts
  users                         List all users with last-seen time
  rooms                         List rooms with member and post counts
  posts <room> [-n N]           List last N posts in a room (default 20)
  purge-posts --days N          Soft-delete posts older than N days
  purge-posts --room <room>     Soft-delete all posts in a room
  delete-post <id>              Soft-delete a post by ID
  kick <pubkey>                 Remove user from all rooms
  delete-user <pubkey>          Delete user and soft-delete their posts
  help                          Show this help
  quit                          Exit the shell"""


def _resolve_pubkey(store: BBSStore, prefix: str) -> str | None:
    """Resolve a full pubkey or unique prefix to the full pubkey."""
    matches = [u for u in store.list_all_users() if u["pubkey"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["pubkey"]
    if len(matches) > 1:
        print(f"Ambiguous: '{prefix}' matches {len(matches)} users:")
        for u in matches:
            print(f"  {u['name']} ({u['pubkey'][:20]}…)")
        return None
    print(f"No user found: '{prefix}'.")
    return None


def _run(store: BBSStore, args: argparse.Namespace) -> bool:
    """Execute a parsed command. Returns False if the shell should exit."""
    now = int(time.time())
    cmd = args.command

    if cmd in (None, "help"):
        print(_HELP)

    elif cmd == "quit":
        return False

    elif cmd == "stats":
        s = store.get_stats()
        print(f"Users : {s['users']}")
        print(f"Posts : {s['posts']}  (non-deleted)")
        print(f"Rooms : {s['rooms']}")

    elif cmd == "users":
        users = store.list_all_users()
        if not users:
            print("No users.")
        for u in users:
            ago  = _fmt_ago(now - u["last_seen"]) if u["last_seen"] else "—"
            room = u["current_room"] or "—"
            print(f"{u['name']:<24}  {u['pubkey'][:16]}…  seen {ago:<5}  room {room}")

    elif cmd == "rooms":
        rooms = store.list_rooms_with_stats()
        if not rooms:
            print("No rooms.")
        for r in rooms:
            ago = _fmt_ago(now - r["last_post_at"]) + " ago" if r["last_post_at"] else "no posts"
            print(f"{r['name']:<20}  {r['member_count']:>3} member(s)  {ago}")

    elif cmd == "posts":
        posts = store.list_posts(args.room, limit=args.n)
        if not posts:
            print(f"No posts in '{args.room}'.")
        for p in posts:
            ago  = _fmt_ago(now - p["created_at"])
            text = (p["text"][:60] + "…") if len(p["text"]) > 60 else p["text"]
            print(f"#{p['id']:<6}  {p['author_name']:<16}  {ago:<5}  {text}")

    elif cmd == "purge-posts":
        if args.days is not None:
            count = store.expire_posts(args.days * 86400)
            print(f"Deleted {count} post(s) older than {args.days} day(s).")
        else:
            count = store.delete_posts_in_room(args.room)
            print(f"Deleted {count} post(s) in '{args.room}'.")

    elif cmd == "delete-post":
        if store.delete_post(args.id):
            print(f"Post #{args.id} deleted.")
        else:
            print(f"Post #{args.id} not found or already deleted.")

    elif cmd == "kick":
        pubkey = _resolve_pubkey(store, args.pubkey)
        if pubkey is None:
            return True
        rooms = store.kick_user(pubkey)
        user  = store.get_user(pubkey)
        name  = user["name"] if user else args.pubkey[:16]
        if rooms:
            print(f"'{name}' removed from {len(rooms)} room(s): {', '.join(rooms)}.")
        else:
            print(f"'{name}' was not in any room.")

    elif cmd == "delete-user":
        pubkey = _resolve_pubkey(store, args.pubkey)
        if pubkey is None:
            return True
        user = store.get_user(pubkey)
        name = user["name"] if user else args.pubkey[:16]
        if store.delete_user(pubkey):
            print(f"User '{name}' deleted.")
        else:
            print(f"User '{name}' not found.")

    return True


def _repl(store: BBSStore, parser: _Parser) -> None:
    print("MeshCore BBS Admin  —  type 'help' for commands, 'quit' to exit")
    while True:
        try:
            line = input("bbs> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            args = parser.parse_args(shlex.split(line))
        except ValueError as e:
            print(f"Error: {e}")
            continue
        if not _run(store, args):
            break


def main() -> None:
    config_path = os.environ.get("BBS_CONFIG", "config.yaml")
    cfg   = load_config(config_path)
    store = BBSStore(cfg.bbs.db_path)
    store.connect()
    parser = _build_parser()
    try:
        if len(sys.argv) > 1:
            try:
                args = parser.parse_args(sys.argv[1:])
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            _run(store, args)
        else:
            _repl(store, parser)
    finally:
        store.close()


if __name__ == "__main__":
    main()
