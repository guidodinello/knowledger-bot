from pathlib import Path

import pytest

from knowledger.config import load_config


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_weekly_recap_config.py / test_token_persistence.py's identical fixture:
    load_dotenv() resolves relative to config.py's own file location, not cwd, so it
    must be stubbed out to keep tests from reading the real (gitignored) project .env."""
    monkeypatch.setattr("knowledger.config.load_dotenv", lambda *a, **kw: False)
    monkeypatch.delenv("CLAUDE_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("CHANNELS_PATH", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-telegram-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("CLAUDE_SESSION_TOKEN", "fake-session-token")


def test_channels_path_defaults_to_cwd_relative_when_data_dir_unset() -> None:
    config = load_config()
    assert config.poller.channels_path == Path("channels.json")


def test_channels_path_is_derived_from_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config = load_config()
    assert config.poller.channels_path == tmp_path / "channels.json"


def test_channels_path_ignores_channels_path_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CHANNELS_PATH is deleted in favor of deriving the path from DATA_DIR — a stray
    value left in the environment (e.g. an un-updated .env.oracle) must not resurrect
    the old override behaviour."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHANNELS_PATH", "/some/other/path.json")
    config = load_config()
    assert config.poller.channels_path == tmp_path / "channels.json"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
