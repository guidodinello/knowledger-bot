"""Contract test for the /update-token endpoint consumed by the browser extension at
https://github.com/guidodinello/knowledger-token-updater. The extension POSTs
{"secret": ..., "token": ...} and branches on status code — if this endpoint's request/
response shape drifts, that repo breaks silently with no signal here, so this test pins
the exact payload and status codes the extension relies on. The extension is updated in
lockstep when this contract changes — both repos are ours, so the endpoint gets the shape
it should have rather than one carrying compatibility shims.

Every test stubs ClaudeClient.check_token: the endpoint now probes the live token before
adopting a posted one, and these tests must never reach the real claude.ai."""

import asyncio
from typing import cast

import pytest
from aiohttp.test_utils import TestClient, TestServer
from telegram.ext import Application

from knowledger.claude_client import ClaudeClient, TokenStatus
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


async def _post(
    config: Config,
    payload: dict,
    client: ClaudeClient | None = None,
) -> tuple[int, dict]:
    client = client if client is not None else ClaudeClient("initial-token")
    processor = QueueProcessor(Queue())
    app = build_aiohttp_app(client, processor, cast(Application, FakeTelegramApp()), config)
    server = TestServer(app)
    async with (
        TestClient(server) as test_client,
        test_client.post("/update-token", json=payload) as resp,
    ):
        return resp.status, await resp.json()


@pytest.fixture(autouse=True)
def _stub_token_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to INVALID — i.e. the bot's token is dead, so a posted one gets adopted.
    That's the pre-existing behaviour the extension was written against; tests that care
    about the live-token gate override this explicitly."""
    monkeypatch.setattr(ClaudeClient, "check_token", lambda self: TokenStatus.INVALID)


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
    client = ClaudeClient("initial-token")

    status, body = asyncio.run(
        _post(_config(), {"secret": "s3cr3t", "token": "new-token"}, client),
    )

    assert status == 200
    assert body == {"outcome": "adopted"}
    assert client._cookie == "sessionKey=new-token"


def test_live_token_is_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the dedicated-session setup: logging into Claude anywhere must
    not clobber a token that still works with a browser cookie that dies on logout."""
    monkeypatch.setattr(ClaudeClient, "check_token", lambda self: TokenStatus.VALID)
    client = ClaudeClient("dedicated-token")

    status, body = asyncio.run(
        _post(_config(), {"secret": "s3cr3t", "token": "browser-token"}, client),
    )

    assert status == 200
    assert body == {"outcome": "ignored", "reason": "current token still valid"}
    assert client._cookie == "sessionKey=dedicated-token"


def test_unverifiable_token_is_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: Claude being unreachable is not evidence the current token is dead."""
    monkeypatch.setattr(ClaudeClient, "check_token", lambda self: TokenStatus.UNKNOWN)
    client = ClaudeClient("dedicated-token")

    status, body = asyncio.run(
        _post(_config(), {"secret": "s3cr3t", "token": "browser-token"}, client),
    )

    assert status == 503
    assert body == {"error": "could not verify current token"}
    assert client._cookie == "sessionKey=dedicated-token"


def test_force_replaces_a_live_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual escape hatch — without it, a working-but-unwanted token could never be
    rotated by hand."""
    monkeypatch.setattr(ClaudeClient, "check_token", lambda self: TokenStatus.VALID)
    client = ClaudeClient("dedicated-token")

    status, body = asyncio.run(
        _post(_config(), {"secret": "s3cr3t", "token": "chosen-token", "force": True}, client),
    )

    assert status == 200
    assert body == {"outcome": "adopted"}
    assert client._cookie == "sessionKey=chosen-token"


def test_live_token_short_circuits_before_org_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordering matters: a token we've already decided not to adopt must never be forwarded
    to Claude just to identify which account it belongs to."""
    monkeypatch.setattr(ClaudeClient, "check_token", lambda self: TokenStatus.VALID)
    calls: list[str] = []
    monkeypatch.setattr(
        "knowledger.http_server.get_org_id_for_token",
        lambda token: calls.append(token) or "some-other-org",
    )

    status, body = asyncio.run(
        _post(
            _config(personal_org_id="personal-org"),
            {"secret": "s3cr3t", "token": "work-account-token"},
        ),
    )

    assert status == 200
    assert body["outcome"] == "ignored"
    assert calls == []


def test_force_does_not_skip_the_secret_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ClaudeClient, "check_token", lambda self: TokenStatus.VALID)
    client = ClaudeClient("dedicated-token")

    status, _ = asyncio.run(
        _post(_config(), {"secret": "wrong", "token": "attacker-token", "force": True}, client),
    )

    assert status == 403
    assert client._cookie == "sessionKey=dedicated-token"
