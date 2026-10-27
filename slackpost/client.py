"""Minimal Slack Web API client for posting messages.

Uses slackdump's stored *client* token (``xoxc-``) together with the session
cookies.  Slack's web client authenticates client tokens this way: the token
identifies the API caller and the cookies prove the browser session.

Only read/write endpoints needed for posting are implemented.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .creds import Credentials

API_BASE = "https://slack.com/api/"
# Slack rejects requests from non-browser user agents for client tokens.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class SlackError(RuntimeError):
    """Raised when the Slack API returns ``ok: false`` or a transport error."""

    def __init__(self, error: str, *, method: str = "", detail: Optional[str] = None):
        self.error = error
        self.method = method
        self.detail = detail
        msg = f"Slack API error: {error}"
        if method:
            msg += f" (method={method})"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


@dataclass
class AuthInfo:
    url: str
    team: str
    user: str
    team_id: str
    user_id: str
    enterprise_id: Optional[str] = None


class SlackClient:
    """A tiny Slack Web API client built on :mod:`urllib`."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
    ) -> None:
        self._token = credentials.token
        self._cookie = credentials.cookie_header()
        self._user_agent = user_agent
        self._timeout = timeout

    # -- low level ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call a Slack Web API method and return the parsed JSON response."""
        payload: Dict[str, Any] = {"token": self._token}
        if params:
            payload.update({k: v for k, v in params.items() if v is not None})

        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            API_BASE + method, data=body, headers=self._headers(), method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise SlackError(f"http_{exc.code}", method=method, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise SlackError("network_error", method=method, detail=str(exc.reason)) from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SlackError("invalid_json", method=method) from exc

        if not data.get("ok"):
            raise SlackError(
                data.get("error", "unknown_error"),
                method=method,
                detail=data.get("warning") or data.get("needed"),
            )
        return data

    # -- high level --------------------------------------------------------

    def auth_test(self) -> AuthInfo:
        data = self.call("auth.test")
        return AuthInfo(
            url=data.get("url", ""),
            team=data.get("team", ""),
            user=data.get("user", ""),
            team_id=data.get("team_id", ""),
            user_id=data.get("user_id", ""),
            enterprise_id=data.get("enterprise_id"),
        )

    def list_channels(self, types: str = "public_channel,private_channel") -> List[Dict[str, Any]]:
        """Return all conversations visible to the user (paginated)."""
        channels: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            data = self.call(
                "conversations.list",
                {
                    "types": types,
                    "limit": 200,
                    "exclude_archived": "true",
                    "cursor": cursor,
                },
            )
            channels.extend(data.get("channels", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return channels

    def resolve_channel(self, name: str) -> str:
        """Resolve ``#name`` or ``name`` to a channel id when possible.

        An id (starts with ``C``/``G``/``D`` and looks like an id) is returned
        unchanged.  Otherwise we try to look the name up via
        ``conversations.list``.  On Enterprise Grid workspaces that endpoint is
        often restricted (``enterprise_is_restricted``); in that case we return
        the bare name and let ``chat.postMessage`` resolve it server-side, since
        it accepts channel names as well as ids.
        """
        candidate = name.lstrip("#")
        # Fast path: assume IDs are alphanumeric, uppercase and >= 9 chars.
        if (
            candidate[:1] in {"C", "G", "D"}
            and candidate.isalnum()
            and candidate.isupper()
            and len(candidate) >= 9
        ):
            return candidate

        try:
            for channel in self.list_channels():
                if channel.get("name") == candidate:
                    return channel["id"]
        except SlackError as exc:
            if exc.error in {"enterprise_is_restricted", "missing_scope", "not_allowed_token_type"}:
                # Let chat.postMessage resolve the name on the server side.
                return candidate
            raise

        # Listing succeeded but the name was not found; still let the API try.
        return candidate

    def list_users(self) -> List[Dict[str, Any]]:
        """Return all users in the workspace (paginated)."""
        users: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            data = self.call("users.list", {"limit": 200, "cursor": cursor})
            users.extend(data.get("members", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return users

    def open_dm(self, user_id: str) -> str:
        """Open (or fetch) the direct message channel with ``user_id``."""
        data = self.call("conversations.open", {"users": user_id})
        channel = data.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            raise SlackError("channel_not_found", method="conversations.open", detail=user_id)
        return channel_id

    def upload_file(
        self,
        path: Union[str, Path],
        *,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
        initial_comment: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a file and (optionally) share it in a channel.

        Slack deprecated ``files.upload``; the current flow is:

        1. ``files.getUploadURLExternal`` -> upload_url + file_id
        2. POST the raw bytes to that URL
        3. ``files.completeUploadExternal`` -> shares it
        """
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SlackError("file_not_found", method="files.upload", detail=str(exc)) from exc

        filename = path.name
        ticket = self.call(
            "files.getUploadURLExternal",
            {"filename": filename, "length": len(data)},
        )
        upload_url = ticket.get("upload_url")
        file_id = ticket.get("file_id")
        if not upload_url or not file_id:
            raise SlackError("invalid_upload_ticket", method="files.getUploadURLExternal")

        # Step 2: raw POST.  Slack accepts the file as the request body.
        request = urllib.request.Request(
            upload_url,
            data=data,
            method="POST",
            headers={"User-Agent": self._user_agent},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise SlackError(
                        f"upload_http_{resp.status}", method="files.upload"
                    )
        except urllib.error.HTTPError as exc:
            raise SlackError(f"upload_http_{exc.code}", method="files.upload") from exc
        except urllib.error.URLError as exc:
            raise SlackError("network_error", method="files.upload", detail=str(exc.reason)) from exc

        # Step 3: finalise and share.
        params: Dict[str, Any] = {
            "files": json.dumps([{"id": file_id, "title": title or filename}])
        }
        if channel:
            params["channel_id"] = channel
        if thread_ts:
            params["thread_ts"] = thread_ts
        if initial_comment:
            params["initial_comment"] = initial_comment
        return self.call("files.completeUploadExternal", params)

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: Optional[str] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
        reply_broadcast: bool = False,
    ) -> Dict[str, Any]:
        """Post a message via ``chat.postMessage``."""
        params: Dict[str, Any] = {
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
            "unfurl_links": "false",
            "unfurl_media": "false",
        }
        if blocks:
            params["blocks"] = json.dumps(blocks)
        if reply_broadcast:
            params["reply_broadcast"] = "true"
        return self.call("chat.postMessage", params)
