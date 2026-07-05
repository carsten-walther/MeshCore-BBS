"""MeshCore BBS admin CLI and interactive shell.

Single-command mode:   python admin.py stats
Interactive REPL mode: python admin.py          (no arguments)
"""

import argparse
import os
import shlex
import sys
import time

from bbs.config import AppConfig, load_config
from bbs.store import BBSStore

# ANSI colours — disabled automatically when stdout is not a terminal.
_TTY   = sys.stdout.isatty()
BOLD   = "\033[1m"  if _TTY else ""
DIM    = "\033[2m"  if _TTY else ""
RESET  = "\033[0m"  if _TTY else ""
RED    = "\033[31m" if _TTY else ""
GREEN  = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
CYAN   = "\033[36m" if _TTY else ""


def _fmt_ago(secs: int) -> str:
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _col(*values: str) -> int:
    """Return the minimum column width needed to fit all values (min 4)."""
    return max(4, *(len(v) for v in values))


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

    pra = sub.add_parser("room-add", add_help=False)
    pra.add_argument("name")

    prd = sub.add_parser("room-delete", add_help=False)
    prd.add_argument("name")

    prm = sub.add_parser("room-members", add_help=False)
    prm.add_argument("name")

    prk = sub.add_parser("room-kick", add_help=False)
    prk.add_argument("name")
    prk.add_argument("pubkey")

    return p


def _help_text() -> str:
    b, r, y = BOLD, RESET, YELLOW
    return (
        f"{b}Commands:{r}\n"
        f"  {y}stats{r}                         Show user, post, and room counts\n"
        f"  {y}users{r}                         List all users with last-seen time\n"
        f"  {y}rooms{r}                         List rooms with member and post counts\n"
        f"\n"
        f"  {b}Post management:{r}\n"
        f"  {y}posts{r} <room> [-n N]           List last N posts in a room (default 20)\n"
        f"  {y}purge-posts{r} --days N          Soft-delete posts older than N days\n"
        f"  {y}purge-posts{r} --room <room>     Soft-delete all posts in a room\n"
        f"  {y}delete-post{r} <id>              Soft-delete a post by ID\n"
        f"\n"
        f"  {b}User management:{r}\n"
        f"  {y}kick{r} <pubkey>                 Remove user from all rooms\n"
        f"  {y}delete-user{r} <pubkey>          Delete user and soft-delete their posts\n"
        f"\n"
        f"  {b}Room management:{r}\n"
        f"  {y}room-add{r} <name>               Create a room in the database\n"
        f"  {y}room-delete{r} <name>            Delete room, all memberships, and posts\n"
        f"  {y}room-members{r} <name>           List current members with last-activity\n"
        f"  {y}room-kick{r} <name> <pubkey>     Remove one user from a specific room\n"
        f"\n"
        f"  {y}help{r}                          Show this help\n"
        f"  {y}quit{r}                          Exit the shell"
    )


def _resolve_pubkey(store: BBSStore, prefix: str) -> str | None:
    """Resolve a full pubkey or unique prefix to the full pubkey."""
    matches = [u for u in store.list_all_users() if u["pubkey"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["pubkey"]
    if len(matches) > 1:
        print(f"{YELLOW}Ambiguous:{RESET} '{prefix}' matches {len(matches)} users:")
        for u in matches:
            print(f"  {CYAN}{u['name']}{RESET} ({u['pubkey'][:20]}…)")
        return None
    print(f"{RED}No user found:{RESET} '{prefix}'.")
    return None


def _run(store: BBSStore, args: argparse.Namespace) -> bool:
    """Execute a parsed command. Returns False if the shell should exit."""
    now = int(time.time())
    cmd = args.command

    if cmd in (None, "help"):
        print(_help_text())

    elif cmd == "quit":
        return False

    elif cmd == "stats":
        s = store.get_stats()
        lw = 6
        print(f"{DIM}{'Users':<{lw}}{RESET}  {BOLD}{s['users']}{RESET}")
        print(f"{DIM}{'Posts':<{lw}}{RESET}  {BOLD}{s['posts']}{RESET}  {DIM}(non-deleted){RESET}")
        print(f"{DIM}{'Rooms':<{lw}}{RESET}  {BOLD}{s['rooms']}{RESET}")

    elif cmd == "users":
        users = store.list_all_users()
        if not users:
            print(f"{DIM}No users.{RESET}")
        else:
            nw = _col(*(u["name"] for u in users))
            rw = _col(*(u["current_room"] or "—" for u in users))
            for u in users:
                ago  = _fmt_ago(now - u["last_seen"]) if u["last_seen"] else "—"
                room = u["current_room"] or "—"
                print(
                    f"{CYAN}{u['name']:<{nw}}{RESET}  "
                    f"{DIM}{u['pubkey'][:16]}…{RESET}  "
                    f"seen {YELLOW}{ago:<4}{RESET}  "
                    f"room {room:<{rw}}"
                )

    elif cmd == "rooms":
        rooms = store.list_rooms_with_stats()
        if not rooms:
            print(f"{DIM}No rooms.{RESET}")
        else:
            nw = _col(*(r["name"] for r in rooms))
            mw = _col(*(str(r["member_count"]) for r in rooms))
            for r in rooms:
                ago = _fmt_ago(now - r["last_post_at"]) + " ago" if r["last_post_at"] else "no posts"
                print(
                    f"{CYAN}{r['name']:<{nw}}{RESET}  "
                    f"{BOLD}{r['member_count']:>{mw}}{RESET} member(s)  "
                    f"{DIM}{ago}{RESET}"
                )

    elif cmd == "posts":
        posts = store.list_posts(args.room, limit=args.n)
        if not posts:
            print(f"{DIM}No posts in '{args.room}'.{RESET}")
        else:
            iw = _col(*(str(p["id"]) for p in posts))
            aw = _col(*(p["author_name"] for p in posts))
            for p in posts:
                ago  = _fmt_ago(now - p["created_at"])
                text = (p["text"][:60] + "…") if len(p["text"]) > 60 else p["text"]
                print(
                    f"{DIM}#{str(p['id']):<{iw}}{RESET}  "
                    f"{CYAN}{p['author_name']:<{aw}}{RESET}  "
                    f"{YELLOW}{ago:<4}{RESET}  "
                    f"{text}"
                )

    elif cmd == "purge-posts":
        if args.days is not None:
            count = store.expire_posts(args.days * 86400)
            print(f"{GREEN}Deleted {count} post(s) older than {args.days} day(s).{RESET}")
        else:
            count = store.delete_posts_in_room(args.room)
            print(f"{GREEN}Deleted {count} post(s) in '{args.room}'.{RESET}")

    elif cmd == "delete-post":
        if store.delete_post(args.id):
            print(f"{GREEN}Post #{args.id} deleted.{RESET}")
        else:
            print(f"{YELLOW}Post #{args.id} not found or already deleted.{RESET}")

    elif cmd == "kick":
        pubkey = _resolve_pubkey(store, args.pubkey)
        if pubkey is None:
            return True
        rooms = store.kick_user(pubkey)
        user  = store.get_user(pubkey)
        name  = user["name"] if user else args.pubkey[:16]
        if rooms:
            print(f"{GREEN}'{name}' removed from {len(rooms)} room(s): {', '.join(rooms)}.{RESET}")
        else:
            print(f"{YELLOW}'{name}' was not in any room.{RESET}")

    elif cmd == "delete-user":
        pubkey = _resolve_pubkey(store, args.pubkey)
        if pubkey is None:
            return True
        user = store.get_user(pubkey)
        name = user["name"] if user else args.pubkey[:16]
        if store.delete_user(pubkey):
            print(f"{GREEN}User '{name}' deleted.{RESET}")
        else:
            print(f"{YELLOW}User '{name}' not found.{RESET}")

    elif cmd == "room-add":
        if store.room_exists(args.name):
            print(f"{YELLOW}Room '{args.name}' already exists.{RESET}")
        else:
            store.create_room(args.name, "admin")
            print(f"{GREEN}Room '{args.name}' created.{RESET}")
            print(f"{DIM}Note: add it to config.yaml → bbs.rooms to persist across restarts.{RESET}")

    elif cmd == "room-delete":
        if store.delete_room(args.name):
            print(f"{GREEN}Room '{args.name}' deleted (memberships removed, posts soft-deleted).{RESET}")
        else:
            print(f"{YELLOW}Room '{args.name}' not found.{RESET}")

    elif cmd == "room-members":
        if not store.room_exists(args.name):
            print(f"{YELLOW}Room '{args.name}' not found.{RESET}")
        else:
            members = store.room_members(args.name)
            if not members:
                print(f"{DIM}No members in '{args.name}'.{RESET}")
            else:
                nw = _col(*(m["name"] for m in members))
                for m in members:
                    ago = _fmt_ago(now - m["last_activity"]) if m["last_activity"] else "—"
                    print(f"{CYAN}{m['name']:<{nw}}{RESET}  {YELLOW}{ago}{RESET}")

    elif cmd == "room-kick":
        pubkey = _resolve_pubkey(store, args.pubkey)
        if pubkey is None:
            return True
        if not store.is_member(pubkey, args.name):
            user = store.get_user(pubkey)
            name = user["name"] if user else args.pubkey[:16]
            print(f"{YELLOW}'{name}' is not a member of '{args.name}'.{RESET}")
            return True
        store.leave_room(pubkey, args.name)
        user = store.get_user(pubkey)
        if user and user["current_room"] == args.name:
            store.set_current_room(pubkey, None)
        name = user["name"] if user else args.pubkey[:16]
        print(f"{GREEN}'{name}' removed from '{args.name}'.{RESET}")

    return True


def _print_banner(cfg: AppConfig, store: BBSStore) -> None:
    s = store.get_stats()
    rooms = ", ".join(cfg.bbs.rooms) if cfg.bbs.rooms else "—"
    lw = 8  # fixed label width for banner rows
    print(f"{BOLD}MeshCore BBS Admin{RESET}  —  type 'help' for commands, 'quit' to exit")
    print(f"Type 'help' for commands, 'quit' to exit")
    print(f"{DIM}{'BBS':<{lw}}{RESET}  {BOLD}{cfg.bbs.name}{RESET}")
    print(f"{DIM}{'Database':<{lw}}{RESET}  {cfg.bbs.db_path}")
    print(f"{DIM}{'Rooms':<{lw}}{RESET}  {CYAN}{rooms}{RESET}")
    print(
        f"{DIM}{'Stats':<{lw}}{RESET}  "
        f"{BOLD}{s['users']}{RESET} user(s)  "
        f"{BOLD}{s['posts']}{RESET} post(s)  "
        f"{BOLD}{s['rooms']}{RESET} room(s)"
    )


def _repl(cfg: AppConfig, store: BBSStore, parser: _Parser) -> None:
    _print_banner(cfg, store)
    prompt = f"{BOLD}{CYAN}bbs>{RESET} " if _TTY else "bbs> "
    while True:
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            args = parser.parse_args(shlex.split(line))
        except ValueError as e:
            print(f"{RED}Error:{RESET} {e}")
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
                print(f"{RED}Error:{RESET} {e}", file=sys.stderr)
                sys.exit(1)
            _run(store, args)
        else:
            _repl(cfg, store, parser)
    finally:
        store.close()


if __name__ == "__main__":
    main()
