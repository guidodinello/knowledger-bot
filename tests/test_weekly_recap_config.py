import pytest

from knowledger.config import load_config


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_token_persistence.py's identical fixture: load_dotenv() resolves
    relative to config.py's own file location, not cwd, so it must be stubbed out to
    keep tests from reading the real (gitignored) project .env."""
    monkeypatch.setattr("knowledger.config.load_dotenv", lambda *a, **kw: False)
    monkeypatch.delenv("CLAUDE_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("WEEKLY_RECAP_ENABLED", raising=False)
    monkeypatch.delenv("WEEKLY_RECAP_DAY", raising=False)
    monkeypatch.delenv("WEEKLY_RECAP_HOUR", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-telegram-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("CLAUDE_SESSION_TOKEN", "fake-session-token")


def test_weekly_recap_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config()
    assert config.weekly_recap.enabled is False
    assert config.weekly_recap.day == "FRI"
    assert config.weekly_recap.hour == 18


@pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on"])
def test_weekly_recap_enabled_truthy_values(monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
    monkeypatch.setenv("WEEKLY_RECAP_ENABLED", truthy)
    assert load_config().weekly_recap.enabled is True


def test_weekly_recap_custom_day_and_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEEKLY_RECAP_ENABLED", "1")
    monkeypatch.setenv("WEEKLY_RECAP_DAY", "mon")
    monkeypatch.setenv("WEEKLY_RECAP_HOUR", "9")
    config = load_config()
    assert config.weekly_recap.day == "MON"
    assert config.weekly_recap.hour == 9


def test_weekly_recap_invalid_day_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEEKLY_RECAP_DAY", "SOMEDAY")
    with pytest.raises(ValueError, match="WEEKLY_RECAP_DAY"):
        load_config()


def test_weekly_recap_non_integer_hour_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEEKLY_RECAP_HOUR", "not-a-number")
    with pytest.raises(ValueError, match="WEEKLY_RECAP_HOUR"):
        load_config()


def test_weekly_recap_out_of_range_hour_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEEKLY_RECAP_HOUR", "24")
    with pytest.raises(ValueError, match="WEEKLY_RECAP_HOUR"):
        load_config()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
