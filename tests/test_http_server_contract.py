"""Contract test for the /update-token endpoint consumed by the browser extension at
https://github.com/guidodinello/knowledger-token-updater. The extension POSTs
{"secret": ..., "token": ...} and branches on status code — if this endpoint's request/
response shape drifts, that repo breaks silently with no signal here, so this test pins
the exact payload and status codes the extension relies on."""

import asyncio
from typing import cast

from aiohttp.test_utils import TestClient, TestServer
from telegram.ext import Application

from knowledger.claude_client import ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    TelegramSettings,
    TokenServerSettings,
)
from knowledger.http_server import build_aiohttp_app
from knowledger.queue import Queue
from knowledger.queue_processor import QueueProcessor


def _config(**token_server_overrides) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        token_server=TokenServerSettings(port=8080, secret="s3cr3t", **token_server_overrides),
    )


class FakeTelegramApp:
    """Never actually invoked in these tests — drain() only runs after a successful
    token update, and its background task result isn't awaited here."""


async def _post(config: Config, payload: dict) -> tuple[int, dict]:
    client = ClaudeClient("initial-token")
    processor = QueueProcessor(Queue())
    app = build_aiohttp_app(client, processor, cast(Application, FakeTelegramApp()), config)
    server = TestServer(app)
    async with (
        TestClient(server) as test_client,
        test_client.post("/update-token", json=payload) as resp,
    ):
        return resp.status, await resp.json()


def test_missing_secret_is_forbidden() -> None:
    status, body = asyncio.run(_post(_config(), {"token": "abc"}))
    assert status == 403
    assert body == {"error": "forbidden"}


def test_wrong_secret_is_forbidden() -> None:
    status, _ = asyncio.run(_post(_config(), {"secret": "wrong", "token": "abc"}))
    assert status == 403


def test_empty_token_is_rejected() -> None:
    status, body = asyncio.run(_post(_config(), {"secret": "s3cr3t", "token": "  "}))
    assert status == 400
    assert body == {"error": "'token' must be a non-empty string"}


def test_valid_secret_and_token_updates() -> None:
    status, body = asyncio.run(_post(_config(), {"secret": "s3cr3t", "token": "new-token"}))
    assert status == 200
    assert body == {"status": "ok"}
