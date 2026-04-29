import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class Config:
    telegram_bot_token: str
    claude_session_token: str
    allowed_user_ids: frozenset[int]
    project_whitelist: frozenset[str] = frozenset()
    proxy: ProxyConfig | None = None
    token_update_secret: str | None = None
    token_server_port: int | None = None
    personal_org_id: str | None = None


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
    whitelist = frozenset(name.strip() for name in raw_whitelist.split(",") if name.strip())

    proxy_username = os.getenv("WEBSHARE_PROXY_USERNAME")
    proxy_password = os.getenv("WEBSHARE_PROXY_PASSWORD")
    proxy = (
        ProxyConfig(proxy_username, proxy_password) if proxy_username and proxy_password else None
    )

    token_update_secret = os.getenv("TOKEN_UPDATE_SECRET") or None
    raw_port = os.getenv("TOKEN_SERVER_PORT") or os.getenv("PORT")
    token_server_port: int | None = None
    if raw_port:
        try:
            token_server_port = int(raw_port)
        except ValueError as e:
            raise ValueError("TOKEN_SERVER_PORT must be an integer") from e
    personal_org_id = os.getenv("PERSONAL_ORG_ID") or None

    return Config(
        telegram_bot_token=token,
        claude_session_token=session,
        allowed_user_ids=allowed,
        project_whitelist=whitelist,
        proxy=proxy,
        token_update_secret=token_update_secret,
        token_server_port=token_server_port,
        personal_org_id=personal_org_id,
    )
