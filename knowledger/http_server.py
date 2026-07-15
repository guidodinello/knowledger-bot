import asyncio
import hmac

from aiohttp import web
from aiohttp.web_middlewares import middleware
from telegram.ext import Application

from .bot import drain_queue
from .claude_client import AuthError, ClaudeClient, get_org_id_for_token
from .config import Config
from .logger import get_logger
from .queue import Queue

logger = get_logger(__name__)

# Keep references so these fire-and-forget drains can't be garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


def _run_in_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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

    client.update_token(new_token)
    logger.info("Token updated via HTTP endpoint")

    telegram_app: Application = request.app["telegram_app"]
    config: Config = request.app["config"]
    queue: Queue = request.app["queue"]
    _run_in_background(drain_queue(telegram_app, config, client, queue))

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
    queue: Queue,
    telegram_app: Application,
    config: Config,
    secret: str,
    personal_org_id: str | None,
    cors_allowed_origin: str = "*",
) -> web.Application:
    app = web.Application(middlewares=[_make_cors_middleware(cors_allowed_origin)])
    app["claude_client"] = client
    app["queue"] = queue
    app["telegram_app"] = telegram_app
    app["config"] = config
    app["token_update_secret"] = secret
    app["personal_org_id"] = personal_org_id
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
        await runner.cleanup()
