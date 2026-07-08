"""MeshCore BBS — entry point."""

import asyncio
import logging
import logging.handlers
import os

from bbs.bbs import MeshCoreBBS
from bbs.config import load_config

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _setup_logging(log_file: str, backup_count: int) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing file handlers before (re-)adding, so a !restart
    # with a changed log_file config takes effect without accumulating handlers.
    for h in root.handlers[:]:
        if isinstance(h, logging.handlers.TimedRotatingFileHandler):
            h.close()
            root.removeHandler(h)

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)

    if log_file:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(file_handler)


async def main() -> None:
    # Config path is taken from BBS_CONFIG if set (used by the container to
    # point at /data/config.yaml), otherwise defaults to ./config.yaml.
    config_path = os.environ.get("BBS_CONFIG", "config/config.yaml")
    cfg = load_config(config_path)
    _setup_logging(cfg.bbs.logging.file, cfg.bbs.logging.backup_count)

    while True:
        bbs = MeshCoreBBS(cfg)
        restart = await bbs.start()
        if not restart:
            break
        logging.getLogger(__name__).info("Restarting with fresh config...")
        cfg = load_config(config_path)
        _setup_logging(cfg.bbs.logging.file, cfg.bbs.logging.backup_count)


if __name__ == "__main__":
    asyncio.run(main())