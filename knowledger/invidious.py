import re
from http import HTTPStatus

from curl_cffi import requests as curl_requests

from .logger import get_logger

logger = get_logger(__name__)

_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.privacyredirect.com",
    "https://yt.cdaut.de",
    "https://invidious.nerdvpn.de",
]

_INSTANCES_API = "https://api.invidious.io/instances.json"

_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}[\d:,.]*\s*-->\s*\d{2}:\d{2}")


def _parse_vtt(vtt: str) -> str:
    lines = []
    for line in vtt.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if stripped.isdigit():
            continue
        if _TIMESTAMP_RE.search(stripped):
            continue
        if lines and lines[-1] == stripped:
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _fetch_dynamic_instances() -> list[str]:
    try:
        resp = curl_requests.get(_INSTANCES_API, impersonate="chrome110", timeout=10)
        if resp.status_code != HTTPStatus.OK:
            return []
        # Each entry is [domain, {...}]; filter for API-enabled instances over HTTPS
        return [
            f"https://{entry[0]}"
            for entry in resp.json()
            if entry[1].get("api") and entry[1].get("uri", "").startswith("https")
        ]
    except Exception:
        logger.debug("Failed to fetch Invidious instance list from %s", _INSTANCES_API)
        return []


def _try_instances(video_id: str, instances: list[str]) -> str | None:
    for instance in instances:
        try:
            resp = curl_requests.get(
                f"{instance}/api/v1/captions/{video_id}",
                impersonate="chrome110",
                timeout=10,
            )
            if resp.status_code != HTTPStatus.OK:
                continue
            data = resp.json()
            tracks = data.get("captions", [])
            if not tracks:
                continue
            en_track = next((t for t in tracks if "en" in t.get("languageCode", "").lower()), None)
            track = en_track if en_track is not None else tracks[0]
            track_url = track.get("url")
            if not isinstance(track_url, str) or not track_url.startswith("/"):
                logger.warning("Unexpected non-relative track URL from %s: %s", instance, track_url)
                continue
            vtt_resp = curl_requests.get(
                f"{instance}{track_url}",
                impersonate="chrome110",
                timeout=10,
            )
            if vtt_resp.status_code != HTTPStatus.OK:
                continue
            text = _parse_vtt(vtt_resp.text)
            if text:
                logger.info("Fetched transcript via Invidious instance %s", instance)
                return text
        except Exception:
            logger.debug("Invidious instance %s failed for video %s", instance, video_id)
            continue
    return None


def fetch_transcript(video_id: str) -> str | None:
    result = _try_instances(video_id, _INSTANCES)
    if result is not None:
        return result
    logger.warning("All hardcoded Invidious instances failed, fetching dynamic instance list")
    dynamic = _fetch_dynamic_instances()
    fallback = [i for i in dynamic if i not in _INSTANCES]
    return _try_instances(video_id, fallback)
