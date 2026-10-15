"""Окремий запуск воркера: `python -m worker`."""

import logging

from core.config import get_settings
from worker.runner import Worker

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    settings.ensure_dirs()
    worker = Worker(settings)
    try:
        worker.loop()
    except KeyboardInterrupt:
        pass
