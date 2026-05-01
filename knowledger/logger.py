import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def init_logging(file: Path, level: str) -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    file.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(file, maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
