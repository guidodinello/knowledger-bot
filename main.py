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
    # config.token_server's secret-requires-port invariant is validated at construction
    # time (TokenServerSettings.__post_init__), not here.

    app = build_application(config)

    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_run_polling(app))]

            if config.poller.auto_transcript_project is not None:
                from knowledger.poller import run_poller

                tasks.append(tg.create_task(run_poller(app, config)))

            if config.token_server.port is not None:
                from knowledger.http_server import build_aiohttp_app, run_http_server

                aiohttp_app = build_aiohttp_app(
                    client=app.bot_data["claude_client"],
                    processor=app.bot_data["queue_processor"],
                    telegram_app=app,
                    config=config,
                )
                tasks.append(tg.create_task(run_http_server(aiohttp_app, config.token_server.port)))

            loop = asyncio.get_running_loop()

            def _shutdown() -> None:
                # Cancelling every sibling task here raises CancelledError inside each —
                # TaskGroup does not treat that as a failure (it's excluded from the
                # ExceptionGroup below), so a signal-triggered shutdown exits main_async
                # cleanly. A genuine subsystem failure, by contrast, is a real exception
                # that TaskGroup itself turns into cross-cancellation of the siblings and
                # an ExceptionGroup — no manual handling needed for that case.
                logger.info("Shutdown signal received, stopping...")
                for t in tasks:
                    t.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, _shutdown)
    except* Exception as eg:
        logger.error("Essential subsystem failed — shutting down", exc_info=eg)
        raise


def main() -> None:
    config = load_config()
    init_logging(file=config.logger.file, level=config.logger.level)
    asyncio.run(main_async(config))


if __name__ == "__main__":
    main()
