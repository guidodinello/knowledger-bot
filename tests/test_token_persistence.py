import json
from pathlib import Path

import pytest

from knowledger.claude_client import ClaudeClient
from knowledger.config import _load_persisted_token, load_config
from knowledger.persistence import CorruptDataError, PersistenceIOError


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_dotenv() resolves relative to config.py's own file location, not cwd — so
    chdir() alone does NOT stop it from reading the real (gitignored) project .env. Stub
    it out entirely so tests can never read real secrets off disk."""
    monkeypatch.setattr("knowledger.config.load_dotenv", lambda *a, **kw: False)
    # Belt-and-suspenders: also strip these from the real shell environment, in case
    # they're exported outside of any .env file.
    monkeypatch.delenv("CLAUDE_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-telegram-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")


def test_load_persisted_token_missing_file_returns_none(tmp_path: Path) -> None:
    assert _load_persisted_token(tmp_path) is None


def test_load_persisted_token_corrupt_json_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "session_token.json").write_text("not json")
    with pytest.raises(CorruptDataError):
        _load_persisted_token(tmp_path)


def test_load_persisted_token_wrong_schema_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "session_token.json").write_text(json.dumps({"not_token": "x"}))
    with pytest.raises(CorruptDataError):
        _load_persisted_token(tmp_path)


def test_load_persisted_token_unreadable_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "session_token.json"
    path.write_text(json.dumps({"token": "x"}))
    path.chmod(0o000)
    try:
        with pytest.raises(PersistenceIOError):
            _load_persisted_token(tmp_path)
    finally:
        path.chmod(0o600)  # restore so tmp_path cleanup can remove it


def test_load_persisted_token_reads_written_value(tmp_path: Path) -> None:
    (tmp_path / "session_token.json").write_text(json.dumps({"token": "fresh-token"}))
    assert _load_persisted_token(tmp_path) == "fresh-token"


def test_update_token_without_persist_path_writes_nothing(tmp_path: Path) -> None:
    client = ClaudeClient("initial")
    client.update_token("updated")
    assert not (tmp_path / "session_token.json").exists()


def test_update_token_with_persist_path_writes_through(tmp_path: Path) -> None:
    persist_path = tmp_path / "session_token.json"
    client = ClaudeClient("initial", persist_path=persist_path)

    client.update_token("fresh-token-123")

    assert json.loads(persist_path.read_text()) == {"token": "fresh-token-123"}


def test_persisted_token_file_is_owner_only_readable(tmp_path: Path) -> None:
    persist_path = tmp_path / "session_token.json"
    ClaudeClient("initial", persist_path=persist_path).update_token("fresh-token-123")

    assert (persist_path.stat().st_mode & 0o777) == 0o600


def test_update_token_persist_failure_leaves_old_token_active(tmp_path: Path) -> None:
    """Persist-before-activate: if durable storage can't be written, the old cookie and
    caches must remain in effect — a caller must not end up running on a token that
    only exists in memory and would be silently reverted by a restart."""
    persist_path = tmp_path / "missing-parent-dir" / "session_token.json"
    client = ClaudeClient("initial", persist_path=persist_path)

    with pytest.raises(OSError):
        client.update_token("new-token")

    assert client._cookie == "sessionKey=initial"


def test_update_token_persists_before_swapping_cookie(tmp_path: Path) -> None:
    """A successful persist must happen before the in-memory cookie changes — verified
    by checking the file is already durable by the time update_token returns."""
    persist_path = tmp_path / "session_token.json"
    client = ClaudeClient("initial", persist_path=persist_path)

    client.update_token("new-token")

    assert client._cookie == "sessionKey=new-token"
    assert json.loads(persist_path.read_text())["token"] == "new-token"


def test_load_config_falls_back_to_env_var_when_no_persisted_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_TOKEN", "stale-env-token")

    config = load_config()

    assert config.claude.session_token == "stale-env-token"


def test_load_config_prefers_persisted_token_over_stale_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario this whole feature exists for: a token updated live via the HTTP
    endpoint must survive a restart instead of reverting to CLAUDE_SESSION_TOKEN."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_TOKEN", "stale-env-token")
    persist_path = tmp_path / "session_token.json"
    ClaudeClient("whatever", persist_path=persist_path).update_token("fresh-token-123")

    config = load_config()

    assert config.claude.session_token == "fresh-token-123"


def test_load_config_fails_closed_on_corrupt_persisted_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt persisted-token file must not silently fall back to the (possibly
    stale/revoked) env var — startup should fail loudly instead."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_TOKEN", "stale-env-token")
    (tmp_path / "session_token.json").write_text("not json")

    with pytest.raises(CorruptDataError):
        load_config()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
