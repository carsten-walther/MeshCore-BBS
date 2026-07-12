"""Plugin package and auto-loader.

A plugin is a module in THIS package whose file name equals its command
name (bbs/plugins/weather.py → `!weather`) and which exposes:

    def create(features: FeaturesConfig, messages: Messages) -> CommandPlugin
    TRANSLATIONS: dict[str, dict[str, str]]      # optional, language → catalog

`load_plugins()` imports exactly the modules named in
`bbs.features.commands` — listing a name in the config IS the loading
mechanism. Built-in optional commands (seen, whoami, stats, ping) are
skipped; any other name without a module here is logged and ignored, and
a plugin that fails to import or initialize never takes the BBS down.
Deliberately no directory scanning: what runs is always visible in the
config.

Plugin TRANSLATIONS are merged into the shared Messages instance, so each
plugin ships its own strings instead of extending the central catalog.
"""

import importlib
import logging
from collections.abc import Iterable

from bbs.commands import BUILTIN_OPTIONAL_COMMANDS
from bbs.config import FeaturesConfig
from bbs.messages import Messages
from bbs.plugin import CommandPlugin

_LOGGER = logging.getLogger(__name__)


def load_plugins(
    names: Iterable[str], features: FeaturesConfig, messages: Messages
) -> list[CommandPlugin]:
    plugins: list[CommandPlugin] = []
    for name in names:
        if name in BUILTIN_OPTIONAL_COMMANDS:
            continue
        module_name = f"{__name__}.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            if e.name == module_name:
                _LOGGER.warning(
                    f"bbs.features.commands: no built-in command or plugin "
                    f"named {name!r} — ignored."
                )
            else:
                _LOGGER.exception(f"Plugin {name!r} failed to import — ignored.")
            continue
        create = getattr(module, "create", None)
        if create is None:
            _LOGGER.warning(f"Plugin {name!r} has no create() factory — ignored.")
            continue
        try:
            plugin = create(features, messages)
        except Exception:
            _LOGGER.exception(f"Plugin {name!r} failed to initialize — ignored.")
            continue
        if plugin.name != name:
            _LOGGER.warning(
                f"Plugin module {name!r} created command {plugin.name!r} — ignored "
                f"(file name and command name must match)."
            )
            continue
        messages.extend(getattr(module, "TRANSLATIONS", {}))
        plugins.append(plugin)
        _LOGGER.info(f"Plugin loaded: !{plugin.name}")
    return plugins
