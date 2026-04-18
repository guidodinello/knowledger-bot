from knowledger.bot import build_application
from knowledger.config import load_config
from knowledger.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    config = load_config()
    logger.info("Starting knowledger bot")
    app = build_application(config)
    app.run_polling()


if __name__ == "__main__":
    main()
