import json
from http import HTTPStatus
from pathlib import Path
from typing import TypedDict

from curl_cffi import requests

from .logger import get_logger
from .persistence import PersistenceIOError, atomic_write_json

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
    def __init__(
        self,
        session_token: str,
        persist_path: Path | None = None,
        projects_persist_path: Path | None = None,
    ) -> None:
        self._cookie = f"sessionKey={session_token}"
        self._persist_path = persist_path
        self._projects_persist_path = projects_persist_path
        self._org_id_cache: str | None = None
        self._projects_cache: list[Project] | None = None

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

    def get_org_id(self) -> str:
        org_id = self._org_id_cache
        if org_id is None:
            response = requests.get(
                f"{BASE_URL}/organizations",
                headers=self._get_headers(),
                impersonate="chrome110",
            )
            self._check_auth(response)
            response.raise_for_status()

            for org in response.json():
                if "chat" in org["capabilities"] or "claude_pro" in org["capabilities"]:
                    org_id = str(org["uuid"])
                    break
            else:
                raise ValueError("No organization found with 'chat' or 'claude_pro' capabilities")
            self._org_id_cache = org_id
        return org_id

    def _check_auth(self, response: requests.Response) -> None:
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AuthError(
                "Claude session token is invalid or expired. "
                "Update CLAUDE_SESSION_TOKEN with a fresh sessionKey cookie from claude.ai.",
            )

    def list_projects(self) -> list[Project]:
        projects = self._projects_cache
        if projects is None:
            response = requests.get(
                f"{BASE_URL}/organizations/{self.get_org_id()}/projects",
                headers=self._get_headers(),
                impersonate="chrome110",
            )
            self._check_auth(response)
            response.raise_for_status()
            projects = response.json()
            self._projects_cache = projects
            if self._projects_persist_path is not None:
                self._persist_projects(self._projects_persist_path, projects)
        return projects

    @staticmethod
    def _persist_projects(path: Path, projects: list[Project]) -> None:
        """Best-effort cache, not a durability guarantee like the session token — a
        write failure here must not break the caller's already-successful fetch, so
        it's logged rather than raised."""
        try:
            atomic_write_json(path, projects)
        except PersistenceIOError:
            logger.exception("Failed to persist project list cache to %s", path)

    def invalidate_projects(self) -> None:
        self._projects_cache = None

    def update_token(self, session_token: str) -> None:
        """Persist-before-activate: a fresh token is written to durable storage FIRST.
        Only once that succeeds do the in-memory cookie and caches change — so a
        persistence failure (raised to the caller) leaves the old token fully active
        instead of running live on a token that a restart would silently revert."""
        if self._persist_path is not None:
            self._persist_token(self._persist_path, session_token)
        self._cookie = f"sessionKey={session_token}"
        self._org_id_cache = None
        self._projects_cache = None

    @staticmethod
    def _persist_token(path: Path, session_token: str) -> None:
        """Delegates to the shared atomic-JSON writer (owner-only permissions, since this
        is a credential) rather than reimplementing the temp-file-plus-replace pattern —
        one place owns the durable-write mechanics. Re-raised as OSError, matching the
        contract update_token() callers (e.g. the HTTP token endpoint) already expect."""
        try:
            atomic_write_json(path, {"token": session_token}, mode=0o600)
        except PersistenceIOError as e:
            raise OSError(str(e)) from e

    def list_docs(self, project_id: str) -> list[Doc]:
        response = requests.get(
            f"{BASE_URL}/organizations/{self.get_org_id()}/projects/{project_id}/docs",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()
        return response.json()

    def delete_doc(self, project_id: str, doc_uuid: str) -> None:
        response = requests.delete(
            f"{BASE_URL}/organizations/{self.get_org_id()}/projects/{project_id}/docs/{doc_uuid}",
            headers=self._get_headers(),
            impersonate="chrome110",
        )
        self._check_auth(response)
        response.raise_for_status()

    def upload_content(self, project_id: str, content: str, file_name: str) -> None:
        url = f"{BASE_URL}/organizations/{self.get_org_id()}/projects/{project_id}/docs"
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


def get_org_id_for_token(token: str) -> str:
    """Return the org UUID for a session token. Raises AuthError if the token is invalid."""
    return ClaudeClient(token).get_org_id()
