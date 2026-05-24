import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .logger import get_logger

logger = get_logger(__name__)


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


def load_config() -> Config:
    load_dotenv(override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    session = os.getenv("CLAUDE_SESSION_TOKEN")
    if not session:
        raise ValueError("CLAUDE_SESSION_TOKEN is required")

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

    raw_log_file = os.getenv("LOG_FILE")
    raw_log_level = os.getenv("LOG_LEVEL")
    logger_config = LoggerConfig(
        **({"level": raw_log_level.upper()} if raw_log_level else {}),
        **({"file": Path(raw_log_file)} if raw_log_file else {}),
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
        **({"cors_allowed_origin": cors} if (cors := os.getenv("CORS_ALLOWED_ORIGIN")) else {}),
        logger=logger_config,
    )
