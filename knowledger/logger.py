import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.setLevel(logging.INFO)
    return logger
