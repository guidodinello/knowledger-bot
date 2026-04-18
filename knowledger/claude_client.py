import json

from curl_cffi import requests

from .logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://claude.ai/api"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


class AuthError(Exception):
    pass


class ClaudeClient:
    def __init__(self, session_token: str) -> None:
        self._cookie = f"sessionKey={session_token}"
        self._org_id = self._get_organization_id()

    def _get_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://claude.ai/chats",
            "Content-Type": "application/json",
            "Origin": "https://claude.ai",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": self._cookie,
        }

    def _get_organization_id(self) -> str:
        response = requests.get(
            f"{BASE_URL}/organizations",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()

        for org in response.json():
            if "chat" in org["capabilities"] or "claude_pro" in org["capabilities"]:
                return org["uuid"]

        raise ValueError("No organization found with 'chat' or 'claude_pro' capabilities")

    def _check_auth(self, response) -> None:
        if response.status_code in (401, 403):
            raise AuthError(
                "Claude session token is invalid or expired. "
                "Update CLAUDE_SESSION_TOKEN with a fresh sessionKey cookie from claude.ai."
            )

    def list_projects(self) -> list[dict]:
        response = requests.get(
            f"{BASE_URL}/organizations/{self._org_id}/projects",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()
        return response.json()

    def upload_content(self, project_id: str, content: str, file_name: str) -> dict:
        url = f"{BASE_URL}/organizations/{self._org_id}/projects/{project_id}/docs"
        payload = {"file_name": file_name, "content": content}

        response = requests.post(
            url,
            headers=self._get_headers(),
            data=json.dumps(payload),
            impersonate="chrome110",
        )
        self._check_auth(response)

        if response.status_code != 201:
            logger.error("Upload failed: %d %s", response.status_code, response.text)
            response.raise_for_status()

        return response.json()
