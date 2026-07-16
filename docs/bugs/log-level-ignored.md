# Bug: LOG_LEVEL from .env Is Silently Ignored

**Severity:** Low
**Files:** `knowledger/logger.py:5`, `main.py`

## Description

`logger.py` reads the log level at module import time:

```python
# logger.py:5
_level = os.getenv("LOG_LEVEL", "INFO").upper()
```

`main.py` imports `logger` at the top of the file, which triggers this line before `main()` is ever called:

```python
# main.py
from knowledger.logger import get_logger, init_logging  # _level is set here

def main() -> None:
    config = load_config()  # load_dotenv() is called here — too late
```

`load_dotenv()` (inside `load_config`) populates `LOG_LEVEL` from `.env`, but `_level` was already evaluated before `load_dotenv()` ran. Only environment variables set in the OS shell before startup take effect; `.env`-defined `LOG_LEVEL` is always silently ignored.

## Impact

Setting `LOG_LEVEL=DEBUG` in `.env` has no effect. Developers and operators trying to increase verbosity for troubleshooting will be confused when it doesn't work.

## Fix

Move the level read inside `init_logging` so it runs after `load_dotenv()`:

```python
def init_logging(log_file: Path) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    ...
    root.setLevel(level)
```

Remove the module-level `_level` variable.
