from __future__ import annotations

import os
import logging
import threading

from dotenv import load_dotenv

from app import ENV_FILE, dispatcher_loop, ensure_project_files

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    load_dotenv(ENV_FILE, override=True)
    os.environ["DISABLE_INLINE_WORKERS"] = "false"
    ensure_project_files()
    logger.info("Worker started. Press Ctrl+C to stop.")
    try:
        dispatcher_loop()
    except KeyboardInterrupt:
        logger.info("Worker stopped.")
        return 0
    finally:
        threading.Event().set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
