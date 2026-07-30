import json
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Literal, TypedDict

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


# Narrower than curl_cffi's own HttpMethod (nine verbs): only the three the Claude API
# client actually issues, so an unsupported verb is a type error rather than a runtime 405.
ApiMethod = Literal["GET", "POST", "DELETE"]

BASE_URL = "https://claude.ai/api"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


class AuthError(Exception):
    pass


class TokenStatus(StrEnum):
    """Outcome of probing the live session cookie. UNKNOWN is deliberately distinct from
    INVALID: callers deciding whether to *replace* a token must not treat "Claude was
    unreachable" as "this token is dead"."""

    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class ClaudeClient:
    def __init__(
        self,
        session_token: str,
        persist_path: Path | None = None,
        projects_persist_path: Path | None = None,
    ) -> None:
        self._session_token = session_token
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

    def _request(
        self,
        method: ApiMethod,
        url: str,
        *,
        data: str | None = None,
    ) -> requests.Response:
        """The single choke point for every claude.ai call: attaches the auth headers and
        browser impersonation, adopts a renewed session cookie if the response carries one,
        then raises AuthError on a 401/403.

        `data` is accepted for any verb, not confined to POST. A GET with content is
        discouraged (RFC 9110 §9.3.1 SHOULD NOT, and RFC 7231 gave it no defined semantics)
        but it isn't a protocol violation, and curl_cffi permitted it here already — `get()`
        takes **kwargs: Unpack[SessionRequestParams], where `data` is declared. Enforcing
        the split in the type system isn't worth the machinery for three fixed calls to one
        origin server, none of which sends a GET body.

        Callers decide whether to raise_for_status() — check_token() deliberately doesn't,
        because it reports a non-auth failure as UNKNOWN instead of raising."""
        response = requests.request(
            method,
            url,
            headers=self._get_headers(),
            impersonate="chrome110",
            data=data,
        )
        self._adopt_renewed_token(response)
        self._check_auth(response)
        return response

    def _adopt_renewed_token(self, response: requests.Response) -> None:
        """claude.ai renews `sessionKey` on a sliding expiry: responses periodically carry a
        Set-Cookie with a fresh value that supersedes the one we sent. Discarding those
        renewals means riding the original cookie until it hard-expires — tolerable while an
        external updater keeps replacing the token on every browser login, but the whole
        ballgame for a long-lived dedicated session that nothing else refreshes.

        Deliberately does NOT route through update_token(): a renewal is the same session on
        the same account, so the org and project caches stay valid and only the cookie
        changes. update_token()'s cache invalidation exists for the *different account*
        case, and firing it here would pointlessly re-fetch the project list.

        Never raises. Cookie parsing is best-effort, and on a persist failure the current
        cookie stays active (the server honours it for a grace period after renewal) rather
        than activating a token a restart would silently revert."""
        try:
            renewed = response.cookies.get("sessionKey")
        except Exception:
            logger.debug("Could not read cookies off a Claude response", exc_info=True)
            return
        if not renewed or renewed == self._session_token:
            return
        if self._persist_path is not None:
            try:
                self._persist_token(self._persist_path, renewed)
            except OSError:
                logger.exception("Failed to persist renewed session token; keeping current one")
                return
        self._session_token = renewed
        self._cookie = f"sessionKey={renewed}"
        logger.info("Adopted a renewed Claude session cookie")

    def check_token(self) -> TokenStatus:
        """Probe whether claude.ai still accepts the *current* cookie.

        Bypasses the org-id cache on purpose: get_org_id() answers from memory after its
        first success, so it would happily report a long-since-revoked token as fine."""
        try:
            response = self._request("GET", f"{BASE_URL}/organizations")
        except AuthError:
            return TokenStatus.INVALID
        except Exception:
            logger.warning("Could not verify the current session token", exc_info=True)
            return TokenStatus.UNKNOWN
        if response.status_code != HTTPStatus.OK:
            logger.warning(
                "Unexpected status %d verifying the current session token",
                response.status_code,
            )
            return TokenStatus.UNKNOWN
        return TokenStatus.VALID

    def get_org_id(self) -> str:
        org_id = self._org_id_cache
        if org_id is None:
            response = self._request("GET", f"{BASE_URL}/organizations")
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
            response = self._request(
                "GET",
                f"{BASE_URL}/organizations/{self.get_org_id()}/projects",
            )
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
        self._session_token = session_token
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
        response = self._request(
            "GET",
            f"{BASE_URL}/organizations/{self.get_org_id()}/projects/{project_id}/docs",
        )
        response.raise_for_status()
        return response.json()

    def delete_doc(self, project_id: str, doc_uuid: str) -> None:
        response = self._request(
            "DELETE",
            f"{BASE_URL}/organizations/{self.get_org_id()}/projects/{project_id}/docs/{doc_uuid}",
        )
        response.raise_for_status()

    def upload_content(self, project_id: str, content: str, file_name: str) -> None:
        url = f"{BASE_URL}/organizations/{self.get_org_id()}/projects/{project_id}/docs"
        payload = {"file_name": file_name, "content": content}

        response = self._request("POST", url, data=json.dumps(payload))

        if response.status_code != HTTPStatus.CREATED:
            logger.error("Upload failed: %d %s", response.status_code, response.text)
            response.raise_for_status()


def get_org_id_for_token(token: str) -> str:
    """Return the org UUID for a session token. Raises AuthError if the token is invalid."""
    return ClaudeClient(token).get_org_id()
