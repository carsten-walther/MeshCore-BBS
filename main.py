"""MeshCore BBS — entry point."""

import asyncio
import logging
import os

from bbs.bbs import MeshCoreBBS
from bbs.config import load_config


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
logging.getLogger(__name__).setLevel(logging.DEBUG)


async def main() -> None:
    # Config path is taken from BBS_CONFIG if set (used by the container to
    # point at /data/config.yaml), otherwise defaults to ./config.yaml.
    config_path = os.environ.get("BBS_CONFIG", "config.yaml")
    cfg = load_config(config_path)
    bbs = MeshCoreBBS(cfg)
    await bbs.start()


if __name__ == "__main__":
    asyncio.run(main())