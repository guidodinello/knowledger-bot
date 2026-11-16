"""The token endpoint serves HTTPS when given a certificate, so the shared secret and the
posted session token never cross the network in clear text. These tests start the real
run_http_server with a throwaway self-signed certificate and talk to it as the browser
extension would."""

import asyncio
import contextlib
import socket
import ssl
from pathlib import Path
from typing import cast

import aiohttp
import pytest
import trustme
from aiohttp import web
from telegram.ext import Application

from knowledger.claude_client import ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    TelegramSettings,
    TokenServerSettings,
)
from knowledger.http_server import build_aiohttp_app, run_http_server
from knowledger.queue import Queue
from knowledger.queue_processor import QueueProcessor


def _throwaway_cert(directory: Path) -> tuple[Path, Path, Path]:
    """Server cert and key for 127.0.0.1, plus the CA that signed them for the client to
    trust. Generated in-process: the CI test image has no openssl binary."""
    ca = trustme.CA()
    leaf = ca.issue_cert("127.0.0.1")
    cert, key, ca_cert = directory / "cert.pem", directory / "key.pem", directory / "ca.pem"
    leaf.cert_chain_pems[0].write_to_path(str(cert))
    leaf.private_key_pem.write_to_path(str(key))
    ca.cert_pem.write_to_path(str(ca_cert))
    return cert, key, ca_cert


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_listening(port: int) -> None:
    for _ in range(100):
        with contextlib.suppress(OSError):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"server never listened on {port}")


def _app(settings: TokenServerSettings) -> web.Application:
    config = Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        token_server=settings,
    )
    # The telegram app is never used: a wrong secret is rejected before anything else runs.
    return build_aiohttp_app(
        ClaudeClient("initial-token"), QueueProcessor(Queue()), cast(Application, object()), config
    )


async def _post_wrong_secret(settings: TokenServerSettings, url: str, **request_kwargs) -> int:
    server = asyncio.create_task(run_http_server(_app(settings), settings))
    try:
        await _wait_until_listening(cast(int, settings.port))
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, json={"secret": "wrong", "token": "t"}, **request_kwargs) as resp,
        ):
            return resp.status
    finally:
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server


def test_serves_https_when_cert_and_key_are_set(tmp_path: Path) -> None:
    cert, key, ca_cert = _throwaway_cert(tmp_path)
    port = _free_port()
    settings = TokenServerSettings(port=port, secret="s3cr3t", tls_cert_path=cert, tls_key_path=key)
    trust_our_cert = ssl.create_default_context(cafile=ca_cert)

    status = asyncio.run(
        _post_wrong_secret(settings, f"https://127.0.0.1:{port}/update-token", ssl=trust_our_cert)
    )

    # 403 is the endpoint's own answer, so the request made it through the TLS handshake.
    assert status == 403


def test_plain_http_is_refused_when_tls_is_configured(tmp_path: Path) -> None:
    cert, key, _ = _throwaway_cert(tmp_path)
    port = _free_port()
    settings = TokenServerSettings(port=port, secret="s3cr3t", tls_cert_path=cert, tls_key_path=key)

    with pytest.raises(aiohttp.ClientError):
        asyncio.run(_post_wrong_secret(settings, f"http://127.0.0.1:{port}/update-token"))


def test_cert_without_key_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be set together"):
        TokenServerSettings(port=8443, secret="s3cr3t", tls_cert_path=tmp_path / "cert.pem")


def test_key_without_cert_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be set together"):
        TokenServerSettings(port=8443, secret="s3cr3t", tls_key_path=tmp_path / "key.pem")


def test_missing_cert_file_fails_at_startup(tmp_path: Path) -> None:
    settings = TokenServerSettings(
        port=_free_port(),
        secret="s3cr3t",
        tls_cert_path=tmp_path / "absent-cert.pem",
        tls_key_path=tmp_path / "absent-key.pem",
    )
    with pytest.raises(FileNotFoundError):
        asyncio.run(run_http_server(_app(settings), settings))
