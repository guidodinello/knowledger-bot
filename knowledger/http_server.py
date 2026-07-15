import asyncio
import hmac

from aiohttp import web
from aiohttp.web_middlewares import middleware
from telegram.ext import Application

from .claude_client import AuthError, ClaudeClient, get_org_id_for_token
from .config import Config
from .logger import get_logger
from .queue_processor import QueueProcessor

logger = get_logger(__name__)


def _log_background_task_result(task: asyncio.Task) -> None:
    """Observe a background task's outcome as soon as it finishes, rather than only at
    server shutdown — an unobserved exception here would otherwise be silently discarded."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background queue-drain task failed", exc_info=exc)


def _run_in_background(app: web.Application, coro) -> None:
    """Fire-and-forget, but owned by the app: kept alive against GC while running, and
    awaited on shutdown so cleanup mid-drain doesn't abandon it. QueueProcessor.drain()
    itself is safe to cancel — each entry is claimed (and durably persisted as in_flight)
    right before it's processed, so at most the single entry in flight is at risk."""
    task = asyncio.create_task(coro)
    app["background_tasks"].add(task)
    task.add_done_callback(app["background_tasks"].discard)
    task.add_done_callback(_log_background_task_result)


async def _await_background_tasks(app: web.Application) -> None:
    tasks = app["background_tasks"]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _handle_update_token(request: web.Request) -> web.Response:
    secret: str = request.app["token_update_secret"]
    client: ClaudeClient = request.app["claude_client"]
    personal_org_id: str | None = request.app["personal_org_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "JSON body must be an object"}, status=400)

    raw_secret = body.get("secret")
    if not isinstance(raw_secret, str) or not hmac.compare_digest(raw_secret, secret):
        return web.json_response({"error": "forbidden"}, status=403)

    raw_token = body.get("token")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return web.json_response({"error": "'token' must be a non-empty string"}, status=400)
    new_token = raw_token.strip()

    if personal_org_id is not None:
        try:
            org_id = await asyncio.to_thread(get_org_id_for_token, new_token)
        except AuthError:
            return web.json_response({"error": "token is invalid"}, status=401)
        except Exception:
            logger.exception("Org validation error")
            return web.json_response({"error": "validation failed"}, status=500)
        if org_id != personal_org_id:
            logger.warning("Rejected token for org %s (expected %s)", org_id, personal_org_id)
            return web.json_response({"error": "token belongs to wrong account"}, status=403)

    try:
        client.update_token(new_token)
    except OSError:
        logger.exception("Failed to persist updated token")
        return web.json_response({"error": "failed to persist token"}, status=500)
    logger.info("Token updated via HTTP endpoint")

    telegram_app: Application = request.app["telegram_app"]
    config: Config = request.app["config"]
    processor: QueueProcessor = request.app["queue_processor"]
    _run_in_background(request.app, processor.drain(telegram_app, config, client))

    return web.json_response({"status": "ok"})


def _make_cors_middleware(allowed_origin: str):
    @middleware
    async def _cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": allowed_origin,
                    "Access-Control-Allow-Methods": "POST",
                    "Access-Control-Allow-Headers": "Content-Type",
                }
            )
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        return response

    return _cors_middleware


def build_aiohttp_app(
    client: ClaudeClient,
    processor: QueueProcessor,
    telegram_app: Application,
    config: Config,
    secret: str,
    personal_org_id: str | None,
    cors_allowed_origin: str = "*",
) -> web.Application:
    app = web.Application(middlewares=[_make_cors_middleware(cors_allowed_origin)])
    app["claude_client"] = client
    app["queue_processor"] = processor
    app["telegram_app"] = telegram_app
    app["config"] = config
    app["token_update_secret"] = secret
    app["personal_org_id"] = personal_org_id
    app["background_tasks"] = set()
    app.on_cleanup.append(_await_background_tasks)
    app.router.add_post("/update-token", _handle_update_token)
    app.router.add_route("OPTIONS", "/update-token", _handle_update_token)
    return app


async def run_http_server(aiohttp_app: web.Application, port: int) -> None:
    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Token update server listening on port %d", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()  # runs app.on_cleanup, including _await_background_tasks
