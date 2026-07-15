import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .logger import get_logger

logger = get_logger(__name__)


def _load_persisted_token(data_dir: Path) -> str | None:
    """Read a token written by ClaudeClient.update_token() on a prior run — takes priority
    over CLAUDE_SESSION_TOKEN so a live token update survives the next restart instead of
    being silently reverted to whatever's baked into the env var."""
    path = data_dir / "session_token.json"
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("token")
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Persisted token file %s is corrupt or unreadable", path, exc_info=True)
        return None
    return token.strip() if isinstance(token, str) and token.strip() else None


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    url: str


@dataclass(frozen=True, slots=True)
class LoggerConfig:
    level: str = "INFO"
    file: Path = field(default_factory=lambda: Path(f"logs/knowledger_{date.today()}.log"))


@dataclass(frozen=True, slots=True)
class Config:
    logger: LoggerConfig
    telegram_bot_token: str
    claude_session_token: str
    allowed_user_ids: frozenset[int]
    project_whitelist: frozenset[str] = frozenset()
    proxy: ProxyConfig | None = None
    youtube_cookies_path: Path | None = None
    token_update_secret: str | None = None
    token_server_port: int | None = None
    personal_org_id: str | None = None
    cors_allowed_origin: str = "*"
    auto_transcript_project: str | None = None
    channels_path: Path = field(default_factory=lambda: Path("channels.json"))
    poll_interval: int = 3600
    data_dir: Path = field(default_factory=lambda: Path("."))


def load_config() -> Config:
    load_dotenv(override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    data_dir = Path(os.getenv("DATA_DIR", "."))
    session = _load_persisted_token(data_dir) or os.getenv("CLAUDE_SESSION_TOKEN")
    if not session:
        raise ValueError("CLAUDE_SESSION_TOKEN is required (or a persisted token in DATA_DIR)")

    raw_ids = os.getenv("ALLOWED_USER_IDS")
    if not raw_ids:
        raise ValueError("ALLOWED_USER_IDS is required")

    try:
        allowed = frozenset(int(uid.strip()) for uid in raw_ids.split(",") if uid.strip())
    except ValueError as e:
        raise ValueError(f"ALLOWED_USER_IDS must be comma-separated integers: {e}") from e

    if not allowed:
        raise ValueError("ALLOWED_USER_IDS must contain at least one user ID")

    raw_whitelist = os.getenv("PROJECT_WHITELIST", "")

    proxy_url = os.getenv("YOUTUBE_PROXY_URL")

    raw_port = os.getenv("TOKEN_SERVER_PORT")
    token_server_port: int | None = None
    if raw_port:
        try:
            token_server_port = int(raw_port)
        except ValueError as e:
            raise ValueError("TOKEN_SERVER_PORT must be an integer") from e

    raw_poll_interval = os.getenv("POLL_INTERVAL_SECONDS")
    poll_interval = 3600
    if raw_poll_interval:
        try:
            poll_interval = int(raw_poll_interval)
        except ValueError as e:
            raise ValueError("POLL_INTERVAL_SECONDS must be an integer") from e

    raw_log_file = os.getenv("LOG_FILE")
    raw_log_level = os.getenv("LOG_LEVEL")
    logger_defaults = LoggerConfig()
    logger_config = LoggerConfig(
        level=raw_log_level.upper() if raw_log_level else logger_defaults.level,
        file=Path(raw_log_file) if raw_log_file else logger_defaults.file,
    )

    return Config(
        telegram_bot_token=token,
        claude_session_token=session,
        allowed_user_ids=allowed,
        project_whitelist=frozenset(
            name.strip() for name in raw_whitelist.split(",") if name.strip()
        ),
        proxy=ProxyConfig(url=proxy_url) if proxy_url else None,
        youtube_cookies_path=(
            Path(raw_cookies) if (raw_cookies := os.getenv("YOUTUBE_COOKIES_PATH")) else None
        ),
        token_update_secret=os.getenv("TOKEN_UPDATE_SECRET") or None,
        token_server_port=token_server_port,
        personal_org_id=os.getenv("PERSONAL_ORG_ID") or None,
        auto_transcript_project=os.getenv("AUTO_TRANSCRIPT_PROJECT") or None,
        channels_path=Path(os.getenv("CHANNELS_PATH", "channels.json")),
        poll_interval=poll_interval,
        data_dir=data_dir,
        cors_allowed_origin=os.getenv("CORS_ALLOWED_ORIGIN") or "*",
        logger=logger_config,
    )
