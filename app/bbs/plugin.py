"""Plugin protocol for optional, self-contained commands.

A plugin bundles everything a command needs to appear in the BBS: its
name (which doubles as the enable-switch key in `bbs.features.commands`),
its help line, and its handler. Feature modules like weather.py and
solar.py expose a `plugin(...)` factory; bbs.py calls it only when the
name is enabled in the config — an unwired plugin is indistinguishable
from an unknown command.

The handler returns plain reply LINES. Chunking to the DM byte limit,
rate limiting, and translation of the help line stay in the router, so
plugins contain feature logic only.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# (pubkey, sender_name, arg) -> reply lines
PluginHandler = Callable[[str, str, str], Awaitable[list[str]]]


@dataclass(frozen=True)
class CommandPlugin:
    name: str            # command word and key in bbs.features.commands
    help: str            # English help template, e.g. "!weather (location) — …"
    handler: PluginHandler
