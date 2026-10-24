"""OAuth for the HTTP transport: this server is its own authorization server, and
sign-in is delegated to Google. Only allow-listed Google accounts get tokens.

The flow Claude's connectors (web, desktop, phone) and Claude Code expect:
  1. The client registers itself (dynamic client registration). Its redirect URI
     must be Claude's callback or a localhost one; anything else is refused.
  2. /authorize: we remember the request (with its PKCE challenge) and send the
     browser to Google.
  3. Google -> /oauth/google/callback: we check the Google account against the
     allowlist, mint a one-time code, and send the browser back to the client.
  4. The client swaps the code (proving PKCE) for a 1-hour access token and a
     rotating 30-day refresh token.

Tokens are random strings. Only their SHA-256 hashes are stored, in the state
directory, so they survive Cloud Run restarts and a copy of the bucket can't be
replayed. Taking an email off the allowlist cuts off its existing tokens at once.

Enabled only when HOMESTEAD_PUBLIC_URL is set; stdio and the IAM-gated setup
are unchanged.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from homestead.audit import AuditLog
from homestead.config import ConfigError
from homestead.http import HttpClient, UpstreamError

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
CALLBACK_PATH = "/oauth/google/callback"

# Claude's hosted connectors (claude.ai web, desktop and mobile apps) redirect here.
CLAUDE_CALLBACKS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}  # Claude Code, mcp-remote

SCOPE = "homestead"
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
CODE_TTL = 5 * 60
PENDING_TTL = 10 * 60
MAX_CLIENTS = 50  # registration is open to anyone; don't let it fill the bucket

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class OAuthConfig:
    public_url: str  # e.g. https://homestead-xyz-uk.a.run.app, no trailing slash
    google_client_id: str
    google_client_secret: str
    allowed_emails: frozenset[str]
    extra_redirect_uris: tuple[str, ...] = ()

    @property
    def resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.public_url}{CALLBACK_PATH}"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OAuthConfig | None:
        url = env.get("HOMESTEAD_PUBLIC_URL", "").strip().rstrip("/")
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
            raise ConfigError("HOMESTEAD_PUBLIC_URL must be https (or localhost for testing)")
        if parsed.path not in {"", "/"}:
            raise ConfigError("HOMESTEAD_PUBLIC_URL must be the bare service URL, without /mcp")
        required = {
            "HOMESTEAD_GOOGLE_CLIENT_ID": env.get("HOMESTEAD_GOOGLE_CLIENT_ID", "").strip(),
            "HOMESTEAD_GOOGLE_CLIENT_SECRET": env.get("HOMESTEAD_GOOGLE_CLIENT_SECRET", "").strip(),
            "HOMESTEAD_ALLOWED_EMAILS": env.get("HOMESTEAD_ALLOWED_EMAILS", "").strip(),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ConfigError(f"HOMESTEAD_PUBLIC_URL is set, so OAuth also needs: {missing}")
        emails = frozenset(
            e.strip().lower() for e in required["HOMESTEAD_ALLOWED_EMAILS"].split(",") if e.strip()
        )
        extra = tuple(
            u.strip()
            for u in env.get("HOMESTEAD_OAUTH_EXTRA_REDIRECTS", "").split(",")
            if u.strip()
        )
        return cls(
            public_url=url,
            google_client_id=required["HOMESTEAD_GOOGLE_CLIENT_ID"],
            google_client_secret=required["HOMESTEAD_GOOGLE_CLIENT_SECRET"],
            allowed_emails=emails,
            extra_redirect_uris=extra,
        )

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.public_url,
            resource_server_url=self.resource_url,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )


def redirect_allowed(uri: str, extra: tuple[str, ...] = ()) -> bool:
    if uri in CLAUDE_CALLBACKS or uri in extra:
        return True
    u = urlparse(uri)
    return u.scheme == "http" and u.hostname in LOOPBACK_HOSTS


def id_token_claims(id_token: str) -> dict[str, Any]:
    """The payload of a Google ID token.

    The signature isn't checked: the token comes straight from Google's token
    endpoint over TLS in exchange for our client secret, which Google documents as
    sufficient. It's never accepted from the browser.
    """
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise UpstreamError("Google returned a malformed ID token") from exc


class OAuthStore:
    """One JSON file per record, under <state>/oauth/<kind>/. Expired records vanish on read."""

    def __init__(self, root: Path, now: Callable[[], float]) -> None:
        self.root = root
        self.now = now

    def _path(self, kind: str, key: str) -> Path:
        if not _SAFE_ID.fullmatch(key):
            raise ValueError("bad key")
        return self.root / kind / f"{key}.json"

    def put(self, kind: str, key: str, data: dict[str, Any]) -> None:
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        try:
            path = self._path(kind, key)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        expires = data.get("expires_at")
        if expires is not None and expires <= self.now():
            path.unlink(missing_ok=True)
            return None
        return data

    def pop(self, kind: str, key: str) -> dict[str, Any] | None:
        """Get and delete: for one-time records (pending logins, codes, rotated tokens)."""
        data = self.get(kind, key)
        if data is not None:
            try:
                self._path(kind, key).unlink()
            except FileNotFoundError:
                return None  # someone else used it first
        return data

    def delete(self, kind: str, key: str) -> None:
        with contextlib.suppress(ValueError):
            self._path(kind, key).unlink(missing_ok=True)

    def count(self, kind: str) -> int:
        d = self.root / kind
        return sum(1 for _ in d.glob("*.json")) if d.exists() else 0


class GoogleOAuthProvider:
    """Implements the SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(
        self,
        cfg: OAuthConfig,
        state_dir: Path,
        http: HttpClient,
        audit: AuditLog,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.audit = audit
        self._now = now or (lambda: datetime.now(UTC))
        self.store = OAuthStore(state_dir / "oauth", lambda: self._now().timestamp())

    def _ts(self) -> int:
        return int(self._now().timestamp())

    def _allowed(self, email: str | None) -> bool:
        return bool(email) and email.lower() in self.cfg.allowed_emails

    # ------------------------------------------------------------------ clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not _SAFE_ID.fullmatch(client_id):
            return None
        data = self.store.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirect_allowed(str(uri), self.cfg.extra_redirect_uris):
                raise RegistrationError(
                    "invalid_redirect_uri", f"{uri} is not an allowed redirect URI for this server"
                )
        if self.store.count("clients") >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "Too many registered clients")
        self.store.put("clients", client_info.client_id, client_info.model_dump(mode="json"))

    # ------------------------------------------------------------------ authorize -> Google

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") not in {
            self.cfg.resource_url,
            self.cfg.public_url,
        }:
            raise AuthorizeError("invalid_target", "Tokens here are only for this MCP server")
        pending = secrets.token_urlsafe(32)
        self.store.put(
            "pending",
            _hash(pending),
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "expires_at": self._ts() + PENDING_TTL,
            },
        )
        query = {
            "client_id": self.cfg.google_client_id,
            "redirect_uri": self.cfg.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": pending,
            "prompt": "select_account",
        }
        if len(self.cfg.allowed_emails) == 1:
            query["login_hint"] = next(iter(self.cfg.allowed_emails))
        return f"{GOOGLE_AUTHORIZE}?{urlencode(query)}"

    def _google_email(self, code: str) -> str:
        """Swap Google's code for an ID token and return the verified email (blocking)."""
        reply = self.http.post_form(
            GOOGLE_TOKEN,
            {
                "code": code,
                "client_id": self.cfg.google_client_id,
                "client_secret": self.cfg.google_client_secret,
                "redirect_uri": self.cfg.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if not isinstance(reply, dict) or "id_token" not in reply:
            raise UpstreamError("Google did not return an ID token")
        claims = id_token_claims(reply["id_token"])
        if (
            claims.get("iss") not in GOOGLE_ISSUERS
            or claims.get("aud") != self.cfg.google_client_id
        ):
            raise UpstreamError("Google ID token is for a different issuer or client")
        if int(claims.get("exp", 0)) <= self._ts():
            raise UpstreamError("Google ID token has expired")
        if claims.get("email_verified") is not True:
            raise PermissionError("unverified email")
        return str(claims.get("email", "")).lower()

    async def google_callback(self, request: Request) -> Response:
        q = request.query_params
        pending_id = q.get("state", "")
        data = self.store.pop("pending", _hash(pending_id)) if pending_id else None
        if data is None:
            return HTMLResponse(
                "<p>This sign-in link expired or was already used. Start again from Claude.</p>",
                status_code=400,
            )

        def back(**params: str | None) -> Response:
            url = construct_redirect_uri(data["redirect_uri"], state=data["state"], **params)
            return RedirectResponse(url, status_code=302)

        if q.get("error") or not q.get("code"):
            return back(error="access_denied", error_description="Google sign-in was cancelled")
        try:
            email = await anyio.to_thread.run_sync(self._google_email, q["code"])
        except PermissionError:
            email = None
        except UpstreamError as exc:
            self.audit.record(
                tool="oauth_login", tier="AUTH", actor="unknown", outcome="error", detail=str(exc)
            )
            return back(error="server_error", error_description="Google sign-in failed")
        if not self._allowed(email):
            self.audit.record(
                tool="oauth_login", tier="AUTH", actor=email or "unverified", outcome="denied"
            )
            return back(error="access_denied", error_description="This account isn't allowed")

        code = secrets.token_urlsafe(32)
        self.store.put(
            "codes",
            _hash(code),
            {**data, "subject": email, "expires_at": self._ts() + CODE_TTL},
        )
        self.audit.record(
            tool="oauth_login",
            tier="AUTH",
            actor=email,
            outcome="ok",
            params={"client_id": data["client_id"]},
        )
        return back(code=code)

    # ------------------------------------------------------------------ codes and tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        d = self.store.get("codes", _hash(authorization_code))
        if not d or d["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=d["scopes"],
            expires_at=d["expires_at"],
            client_id=d["client_id"],
            code_challenge=d["code_challenge"],
            redirect_uri=d["redirect_uri"],
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
            resource=self.cfg.resource_url,
            subject=d["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.store.pop("codes", _hash(authorization_code.code)) is None:
            raise TokenError("invalid_grant", "Authorization code was already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject)

    def _issue(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = self._ts()
        common = {"client_id": client_id, "scopes": scopes, "subject": subject}
        self.store.put(
            "access",
            _hash(access),
            {**common, "refresh": _hash(refresh), "expires_at": now + ACCESS_TTL},
        )
        self.store.put(
            "refresh",
            _hash(refresh),
            {**common, "access": _hash(access), "expires_at": now + REFRESH_TTL},
        )
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        d = self.store.get("refresh", _hash(refresh_token))
        if not d or d["client_id"] != client.client_id or not self._allowed(d["subject"]):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=d["client_id"],
            scopes=d["scopes"],
            expires_at=d["expires_at"],
            resource=self.cfg.resource_url,
            subject=d["subject"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        old = self.store.pop("refresh", _hash(refresh_token.token))
        if old is None:
            raise TokenError("invalid_grant", "Refresh token was already used")
        self.store.delete("access", old["access"])
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        d = self.store.get("access", _hash(token))
        if not d or not self._allowed(d["subject"]):
            return None
        return AccessToken(
            token=token,
            client_id=d["client_id"],
            scopes=d["scopes"],
            expires_at=d["expires_at"],
            resource=self.cfg.resource_url,
            subject=d["subject"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind, other = (
            ("access", "refresh") if isinstance(token, AccessToken) else ("refresh", "access")
        )
        d = self.store.pop(kind, _hash(token.token))
        if d:
            self.store.delete(other, d[other])
