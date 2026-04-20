import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_level = os.getenv("LOG_LEVEL", "INFO").upper()


def init_logging(log_file: Path) -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    root.setLevel(_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
