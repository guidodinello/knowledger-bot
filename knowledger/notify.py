from telegram.ext import Application

from .config import Config
from .logger import get_logger
from .telegram_format import NO_PREVIEW, PARSE_MODE, cap_message, cap_plain_message

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
    with the telegram_format helpers. Messages are capped here because send failures
    are intentionally best-effort and an over-long broadcast must not disappear."""
    text = cap_plain_message(text) if parse_mode is None else cap_message(text)
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
