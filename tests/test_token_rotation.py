"""Covers the two halves of keeping a long-lived dedicated session alive: adopting the
renewed `sessionKey` claude.ai hands back on a sliding expiry, and probing whether the
current cookie is still accepted (without mistaking an outage for a revoked token)."""

import json
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledger.claude_client import AuthError, ClaudeClient, TokenStatus


class FakeCookies:
    def __init__(self, session_key: str | None = None, raises: Exception | None = None) -> None:
        self._session_key = session_key
        self._raises = raises

    def get(self, name: str) -> str | None:
        if self._raises is not None:
            raise self._raises
        return self._session_key if name == "sessionKey" else None


def _response(session_key: str | None = None, status: int = 200, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, cookies=FakeCookies(session_key, **kwargs))


def test_renewed_cookie_is_adopted_and_persisted(tmp_path: Path) -> None:
    persist_path = tmp_path / "session_token.json"
    client = ClaudeClient("original", persist_path=persist_path)

    client._adopt_renewed_token(_response("renewed"))  # type: ignore[arg-type]

    assert client._cookie == "sessionKey=renewed"
    assert json.loads(persist_path.read_text()) == {"token": "renewed"}


def test_renewal_keeps_org_and_project_caches(tmp_path: Path) -> None:
    """A renewal is the same account, so the caches update_token() clears stay valid —
    dropping them here would re-fetch the project list on every renewal for nothing."""
    client = ClaudeClient("original", persist_path=tmp_path / "session_token.json")
    client._org_id_cache = "org-uuid"
    client._projects_cache = [{"uuid": "p1", "name": "Investments"}]

    client._adopt_renewed_token(_response("renewed"))  # type: ignore[arg-type]

    assert client._org_id_cache == "org-uuid"
    assert client._projects_cache == [{"uuid": "p1", "name": "Investments"}]


def test_response_without_cookie_is_a_noop(tmp_path: Path) -> None:
    persist_path = tmp_path / "session_token.json"
    client = ClaudeClient("original", persist_path=persist_path)

    client._adopt_renewed_token(_response(None))  # type: ignore[arg-type]

    assert client._cookie == "sessionKey=original"
    assert not persist_path.exists()


def test_unchanged_cookie_is_not_rewritten(tmp_path: Path) -> None:
    """The common case: Claude echoes back the same cookie we sent, which must not cause a
    disk write on every single API call."""
    persist_path = tmp_path / "session_token.json"
    client = ClaudeClient("original", persist_path=persist_path)

    client._adopt_renewed_token(_response("original"))  # type: ignore[arg-type]

    assert not persist_path.exists()


def test_unreadable_cookies_do_not_break_the_call(tmp_path: Path) -> None:
    """Cookie parsing is best-effort — a CookieConflict must not turn an otherwise
    successful upload into a failure."""
    client = ClaudeClient("original", persist_path=tmp_path / "session_token.json")

    client._adopt_renewed_token(_response(raises=RuntimeError("cookie conflict")))  # type: ignore[arg-type]

    assert client._cookie == "sessionKey=original"


def test_persist_failure_keeps_current_cookie_active(tmp_path: Path) -> None:
    """Persist-before-activate, same rule as update_token(): if the renewal can't be made
    durable, stay on the current cookie rather than run on one a restart would revert."""
    client = ClaudeClient("original", persist_path=tmp_path / "missing-dir" / "session_token.json")

    client._adopt_renewed_token(_response("renewed"))  # type: ignore[arg-type]

    assert client._cookie == "sessionKey=original"


def test_renewal_without_persist_path_still_updates_memory() -> None:
    client = ClaudeClient("original")

    client._adopt_renewed_token(_response("renewed"))  # type: ignore[arg-type]

    assert client._cookie == "sessionKey=renewed"


def test_check_token_valid_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClaudeClient("token")
    monkeypatch.setattr(ClaudeClient, "_request", lambda *a, **kw: _response(status=200))

    assert client.check_token() is TokenStatus.VALID


def test_check_token_invalid_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args, **_kwargs):
        raise AuthError("expired")

    monkeypatch.setattr(ClaudeClient, "_request", _raise)

    assert ClaudeClient("token").check_token() is TokenStatus.INVALID


def test_check_token_unknown_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable Claude must not be reported as a dead token — callers use the
    distinction to decide whether replacing the token is safe."""

    def _raise(*_args, **_kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(ClaudeClient, "_request", _raise)

    assert ClaudeClient("token").check_token() is TokenStatus.UNKNOWN


def test_check_token_unknown_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ClaudeClient,
        "_request",
        lambda *a, **kw: _response(status=HTTPStatus.INTERNAL_SERVER_ERROR),
    )

    assert ClaudeClient("token").check_token() is TokenStatus.UNKNOWN


def test_check_token_ignores_cached_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this guards against: answering from _org_id_cache would report a revoked
    token as VALID forever, because get_org_id() never touches the network again after
    its first success."""
    client = ClaudeClient("token")
    client._org_id_cache = "org-uuid"

    def _raise(*_args, **_kwargs):
        raise AuthError("expired")

    monkeypatch.setattr(ClaudeClient, "_request", _raise)

    assert client.check_token() is TokenStatus.INVALID


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
