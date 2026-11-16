"""YouTube's RSS endpoint randomly answers ~10-15% of requests with 404/500, so a direct
fetch is retried before falling back to the proxy, instead of skipping the channel until
the next hourly tick on the first blip."""

from dataclasses import dataclass, field

import pytest
from curl_cffi.requests.exceptions import HTTPError

from knowledger import poller
from knowledger.config import ProxyConfig


class _FakeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"HTTP Error {self.status_code}")


@dataclass
class _FakeYouTube:
    """Serves `responses` in order to requests.get and records each call's kwargs."""

    responses: list[_FakeResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.fixture
def youtube(monkeypatch: pytest.MonkeyPatch) -> _FakeYouTube:
    fake = _FakeYouTube()
    monkeypatch.setattr(poller.requests, "get", fake.get)
    monkeypatch.setattr(poller.time, "sleep", lambda _seconds: None)
    return fake


def test_a_transient_404_is_retried_directly(youtube: _FakeYouTube) -> None:
    youtube.responses = [_FakeResponse(404), _FakeResponse(200, "<feed/>")]

    body = poller._http_get(poller.FEED_URL, {"channel_id": "UC1"}, ProxyConfig(url="http://p"))

    assert body == "<feed/>"
    assert len(youtube.calls) == 2
    assert all("proxies" not in c for c in youtube.calls)  # the paid proxy was not needed


def test_persistent_failure_without_proxy_raises_after_all_attempts(youtube: _FakeYouTube) -> None:
    youtube.responses = [_FakeResponse(404)] * poller.DIRECT_FETCH_ATTEMPTS

    with pytest.raises(HTTPError):
        poller._http_get(poller.FEED_URL, {"channel_id": "UC1"}, None)

    assert len(youtube.calls) == poller.DIRECT_FETCH_ATTEMPTS


def test_persistent_direct_failure_falls_back_to_the_proxy(youtube: _FakeYouTube) -> None:
    youtube.responses = [_FakeResponse(500)] * poller.DIRECT_FETCH_ATTEMPTS + [
        _FakeResponse(200, "<feed/>"),
    ]

    body = poller._http_get(poller.FEED_URL, {"channel_id": "UC1"}, ProxyConfig(url="http://p"))

    assert body == "<feed/>"
    assert len(youtube.calls) == poller.DIRECT_FETCH_ATTEMPTS + 1
    assert "proxies" in youtube.calls[-1]
