import asyncio
import signal

from knowledger.bot import build_application
from knowledger.config import Config, load_config
from knowledger.logger import get_logger, init_logging


async def _run_polling(app) -> None:
    async with app:
        await app.updater.start_polling()
        await app.start()
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()


async def main_async(config: Config) -> None:
    logger = get_logger(__name__)
    logger.info("Starting knowledger bot")
    app = build_application(config)
    tasks = [asyncio.create_task(_run_polling(app))]

    if config.token_server_port is not None:
        if config.token_update_secret is None:
            raise ValueError("TOKEN_UPDATE_SECRET must be set when TOKEN_SERVER_PORT is configured")

        from knowledger.http_server import build_aiohttp_app, run_http_server

        aiohttp_app = build_aiohttp_app(
            client=app.bot_data["claude_client"],
            secret=config.token_update_secret,
            personal_org_id=config.personal_org_id,
            cors_allowed_origin=config.cors_allowed_origin,
        )
        tasks.append(asyncio.create_task(run_http_server(aiohttp_app, config.token_server_port)))

    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Shutdown signal received, stopping...")
        for t in tasks:
            t.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    config = load_config()
    init_logging(file=config.logger.file, level=config.logger.level)
    asyncio.run(main_async(config))


if __name__ == "__main__":
    main()
