import json
import os
from functools import cached_property
from http import HTTPStatus
from pathlib import Path
from typing import TypedDict

from curl_cffi import requests

from .logger import get_logger

logger = get_logger(__name__)


class Project(TypedDict):
    uuid: str
    name: str


class Doc(TypedDict):
    uuid: str
    file_name: str


BASE_URL = "https://claude.ai/api"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


class AuthError(Exception):
    pass


class ClaudeClient:
    def __init__(self, session_token: str, persist_path: Path | None = None) -> None:
        self._cookie = f"sessionKey={session_token}"
        self._persist_path = persist_path

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

    @cached_property
    def _org_id(self) -> str:
        response = requests.get(
            f"{BASE_URL}/organizations",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()

        for org in response.json():
            if "chat" in org["capabilities"] or "claude_pro" in org["capabilities"]:
                return str(org["uuid"])

        raise ValueError("No organization found with 'chat' or 'claude_pro' capabilities")

    def _check_auth(self, response: requests.Response) -> None:
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AuthError(
                "Claude session token is invalid or expired. "
                "Update CLAUDE_SESSION_TOKEN with a fresh sessionKey cookie from claude.ai."
            )

    @cached_property
    def projects(self) -> list[Project]:
        response = requests.get(
            f"{BASE_URL}/organizations/{self._org_id}/projects",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()
        return response.json()

    def invalidate_projects(self) -> None:
        self.__dict__.pop("projects", None)

    def update_token(self, session_token: str) -> None:
        self._cookie = f"sessionKey={session_token}"
        self.__dict__.pop("_org_id", None)
        self.__dict__.pop("projects", None)
        if self._persist_path is not None:
            self._persist_token(self._persist_path, session_token)

    @staticmethod
    def _persist_token(path: Path, session_token: str) -> None:
        """Write-through so a fresh token survives a container restart — otherwise a
        redeploy reboots the process with whatever's baked into CLAUDE_SESSION_TOKEN,
        silently reverting any token updated live via update_token() since boot."""
        tmp = path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"token": session_token}))
            os.replace(tmp, path)
        except OSError:
            logger.exception("Failed to persist updated token to %s", path)

    def list_docs(self, project_id: str) -> list[Doc]:
        response = requests.get(
            f"{BASE_URL}/organizations/{self._org_id}/projects/{project_id}/docs",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()
        return response.json()

    def delete_doc(self, project_id: str, doc_uuid: str) -> None:
        response = requests.delete(
            f"{BASE_URL}/organizations/{self._org_id}/projects/{project_id}/docs/{doc_uuid}",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()

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

        if response.status_code != HTTPStatus.CREATED:
            logger.error("Upload failed: %d %s", response.status_code, response.text)
            response.raise_for_status()

        return response.json()


def get_org_id_for_token(token: str) -> str:
    """Return the org UUID for a session token. Raises AuthError if the token is invalid."""
    return ClaudeClient(token)._org_id
