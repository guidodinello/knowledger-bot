import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from .claude_client import Project
from .logger import get_logger
from .persistence import CorruptDataError, load_json

logger = get_logger(__name__)


def _load_persisted_token(data_dir: Path) -> str | None:
    """Read a token written by ClaudeClient.update_token() on a prior run — takes priority
    over CLAUDE_SESSION_TOKEN so a live token update survives the next restart instead of
    being silently reverted to whatever's baked into the env var.

    A missing file is a valid first-run state (falls back to CLAUDE_SESSION_TOKEN). An
    existing but corrupt/unreadable file fails closed (raises) instead of silently
    falling back — that would risk reactivating a stale, possibly-revoked env token
    without anyone noticing the persisted one couldn't be read."""
    path = data_dir / "session_token.json"
    raw = load_json(path)
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("token"), str):
        raise CorruptDataError(path, "expected a JSON object with a string 'token' field")
    token = raw["token"].strip()
    return token or None


def load_persisted_projects(data_dir: Path) -> list[Project] | None:
    """Read the project list cache written by ClaudeClient.list_projects() on its last
    successful fetch — a fallback for callers hitting AuthError before any project
    list is available in memory (e.g. right after the session token goes bad, before
    /refresh or a token update repopulates the in-process cache). The result may be
    stale (missing projects created since the last successful fetch).

    A missing file is a valid state (no successful fetch has happened yet). An
    existing but corrupt file fails closed, same rationale as `_load_persisted_token`."""
    path = data_dir / "projects_cache.json"
    raw = load_json(path)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(
        isinstance(p, dict) and isinstance(p.get("uuid"), str) and isinstance(p.get("name"), str)
        for p in raw
    ):
        raise CorruptDataError(path, "expected a JSON array of {uuid, name} project objects")
    return cast(list[Project], raw)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    url: str


@dataclass(frozen=True, slots=True)
class LoggerConfig:
    level: str = "INFO"
    file: Path = field(default_factory=lambda: Path(f"logs/knowledger_{date.today()}.log"))


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    project_whitelist: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ClaudeSettings:
    session_token: str


@dataclass(frozen=True, slots=True)
class TranscriptSettings:
    proxy: ProxyConfig | None = None
    youtube_cookies_path: Path | None = None


@dataclass(frozen=True, slots=True)
class TokenServerSettings:
    port: int | None = None
    secret: str | None = None
    personal_org_id: str | None = None
    cors_allowed_origin: str = "*"

    def __post_init__(self) -> None:
        if self.port is not None and self.secret is None:
            raise ValueError("TOKEN_UPDATE_SECRET must be set when TOKEN_SERVER_PORT is configured")


@dataclass(frozen=True, slots=True)
class PollerSettings:
    auto_transcript_project: str | None = None
    channels_path: Path = field(default_factory=lambda: Path("channels.json"))
    poll_interval: int = 3600


@dataclass(frozen=True, slots=True)
class StorageSettings:
    data_dir: Path = field(default_factory=lambda: Path("."))


@dataclass(frozen=True, slots=True)
class Config:
    telegram: TelegramSettings
    claude: ClaudeSettings
    logger: LoggerConfig
    transcript: TranscriptSettings = field(default_factory=TranscriptSettings)
    token_server: TokenServerSettings = field(default_factory=TokenServerSettings)
    poller: PollerSettings = field(default_factory=PollerSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)


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
        telegram=TelegramSettings(
            bot_token=token,
            allowed_user_ids=allowed,
            project_whitelist=frozenset(
                name.strip() for name in raw_whitelist.split(",") if name.strip()
            ),
        ),
        claude=ClaudeSettings(session_token=session),
        transcript=TranscriptSettings(
            proxy=ProxyConfig(url=proxy_url) if proxy_url else None,
            youtube_cookies_path=(
                Path(raw_cookies) if (raw_cookies := os.getenv("YOUTUBE_COOKIES_PATH")) else None
            ),
        ),
        token_server=TokenServerSettings(
            port=token_server_port,
            secret=os.getenv("TOKEN_UPDATE_SECRET") or None,
            personal_org_id=os.getenv("PERSONAL_ORG_ID") or None,
            cors_allowed_origin=os.getenv("CORS_ALLOWED_ORIGIN") or "*",
        ),
        poller=PollerSettings(
            auto_transcript_project=os.getenv("AUTO_TRANSCRIPT_PROJECT") or None,
            channels_path=Path(os.getenv("CHANNELS_PATH", "channels.json")),
            poll_interval=poll_interval,
        ),
        storage=StorageSettings(data_dir=data_dir),
        logger=logger_config,
    )
