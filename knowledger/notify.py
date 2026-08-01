from telegram.ext import Application

from .config import Config
from .logger import get_logger
from .telegram_format import NO_PREVIEW, PARSE_MODE

logger = get_logger(__name__)


async def notify(
    app: Application,
    config: Config,
    text: str,
    *,
    parse_mode: str | None = PARSE_MODE,
) -> None:
    """Broadcast to every allowed user. Defaults to HTML so callers get links and
    emphasis without restating the parse mode; pass None for text that was not built
    with the telegram_format helpers.

    Every caller must pass `text` through `cap_message` — a message over Telegram's
    4096-character limit is rejected, and the failure is swallowed below, so an
    over-long broadcast would simply never arrive."""
    for uid in config.telegram.allowed_user_ids:
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=parse_mode,
                # Broadcasts now carry video links; without this Telegram attaches a
                # preview card for the first one — a large thumbnail under the weekly
                # digest for whichever video happens to be listed first.
                link_preview_options=NO_PREVIEW,
            )
        except Exception:
            logger.warning("Failed to notify user %d", uid, exc_info=True)
