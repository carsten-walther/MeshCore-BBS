"""MeshCore BBS — entry point."""

import asyncio
import logging

from bbs.bbs import MeshCoreBBS
from bbs.config import load_config


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
logging.getLogger(__name__).setLevel(logging.DEBUG)


async def main() -> None:
    cfg = load_config("config.yaml")
    bbs = MeshCoreBBS(cfg)
    await bbs.start()


if __name__ == "__main__":
    asyncio.run(main())