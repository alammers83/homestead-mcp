"""Tiny JSON-over-HTTP client (stdlib only), injectable so modules are testable offline."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

DEFAULT_USER_AGENT = "homestead-mcp/0.1 (https://github.com/alammers83/homestead-mcp)"


class UpstreamError(RuntimeError):
    """An external API was unreachable or returned something unusable."""


class HttpClient(Protocol):
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any: ...

    def post_json(self, url: str, body: dict[str, Any]) -> int: ...


class UrllibClient:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 10.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def _open(self, req: urllib.request.Request) -> Any:
        if not req.full_url.startswith("https://"):
            raise UpstreamError("Only https URLs are allowed")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310 (scheme checked)
        except urllib.error.HTTPError as exc:
            raise UpstreamError(f"{req.full_url} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise UpstreamError(f"Could not reach {req.full_url}: {exc}") from exc

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/geo+json, application/json",
            },
        )
        with self._open(req) as resp:
            try:
                return json.load(resp)
            except json.JSONDecodeError as exc:
                raise UpstreamError(f"{url} returned invalid JSON") from exc

    def post_json(self, url: str, body: dict[str, Any]) -> int:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"User-Agent": self.user_agent, "Content-Type": "application/json"},
        )
        with self._open(req) as resp:
            return resp.status
