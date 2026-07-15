from telegram.ext import Application

from .config import Config
from .logger import get_logger

logger = get_logger(__name__)


async def notify(app: Application, config: Config, text: str) -> None:
    for uid in config.allowed_user_ids:
        try:
            await app.bot.send_message(chat_id=uid, text=text)
        except Exception:
            logger.warning("Failed to notify user %d", uid, exc_info=True)
