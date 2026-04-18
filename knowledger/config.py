import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .logger import get_logger

load_dotenv(override=True)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Config:
    telegram_bot_token: str
    claude_session_token: str
    allowed_user_ids: frozenset[int]


def load_config() -> Config:
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

    return Config(
        telegram_bot_token=token,
        claude_session_token=session,
        allowed_user_ids=allowed,
    )
