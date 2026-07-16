import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SUPPRESSED_SUBSTRINGS = ("getUpdates",)

# httpx logs the full request URL at INFO level (e.g. "HTTP Request: POST
# https://api.telegram.org/bot123456:AAE.../sendMessage ..."), which embeds the Telegram
# bot token in plaintext. Matches any logger, not just httpx's, since the token could
# surface via a connection-error message too.
_BOT_TOKEN_RE = re.compile(r"/bot\d+:[\w-]+")


class _HttpxFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.levelno == logging.INFO and any(s in msg for s in _SUPPRESSED_SUBSTRINGS):
            return False
        if _BOT_TOKEN_RE.search(msg):
            record.msg = _BOT_TOKEN_RE.sub("/bot<redacted>", msg)
            record.args = ()
        return True


def init_logging(file: Path, level: str) -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    # Attached to the handlers (not the root logger) since a Logger's own filters only
    # run for records logged directly on it — records from child loggers (httpx,
    # telegram) reach these handlers via propagation without re-running root's filters.
    httpx_filter = _HttpxFilter()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(httpx_filter)
    root.addHandler(sh)

    file.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(file, maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    fh.addFilter(httpx_filter)
    root.addHandler(fh)

    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
