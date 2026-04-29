import asyncio

from aiohttp import web
from aiohttp.web_middlewares import middleware

from .claude_client import AuthError, ClaudeClient, get_org_id_for_token
from .logger import get_logger

logger = get_logger(__name__)


async def _handle_update_token(request: web.Request) -> web.Response:
    secret: str | None = request.app["token_update_secret"]
    client: ClaudeClient = request.app["claude_client"]
    personal_org_id: str | None = request.app["personal_org_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "JSON body must be an object"}, status=400)

    if secret is not None and body.get("secret") != secret:
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
    return web.json_response({"status": "ok"})


@middleware
async def _cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def build_aiohttp_app(
    client: ClaudeClient,
    secret: str | None,
    personal_org_id: str | None,
) -> web.Application:
    middlewares = [_cors_middleware] if secret is not None else []
    app = web.Application(middlewares=middlewares)
    app["claude_client"] = client
    app["token_update_secret"] = secret
    app["personal_org_id"] = personal_org_id
    app.router.add_post("/update-token", _handle_update_token)
    return app


async def run_http_server(aiohttp_app: web.Application, port: int) -> None:
    runner = web.AppRunner(aiohttp_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Token update server listening on port %d", port)
    if aiohttp_app["token_update_secret"] is None:
        logger.warning("TOKEN_UPDATE_SECRET not set — /update-token is unauthenticated")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
