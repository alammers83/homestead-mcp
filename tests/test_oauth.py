"""OAuth over the real HTTP app: registration, Google sign-in, PKCE, tokens, and the gate on /mcp."""

import base64
import hashlib
import secrets
import unittest
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from helpers import Clock, FakeHttp, make_app
from homestead.auth import OAuthConfig, id_token_claims, redirect_allowed
from homestead.config import ConfigError

BASE = "https://hs.test"
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
ME = "me@example.com"
CFG = OAuthConfig(
    public_url=BASE,
    google_client_id="gid.apps.googleusercontent.com",
    google_client_secret="gsecret",
    allowed_emails=frozenset({ME}),
)
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def google_claims(email=ME, verified=True, aud=CFG.google_client_id, exp_offset=600):
    return {
        "iss": "https://accounts.google.com",
        "aud": aud,
        "email": email,
        "email_verified": verified,
        "exp": int(datetime.now(UTC).timestamp()) + exp_offset,
    }


class Flow(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.http.google_claims = google_claims()
        # The SDK checks code and token expiry against the real clock, so start there.
        self.clock = Clock(datetime.now(UTC))
        app, *_ = make_app(self.http, self.clock, oauth=CFG)
        self.app = app
        asgi = app.server.streamable_http_app(stateless_http=True, host="0.0.0.0")  # noqa: S104
        self.client = TestClient(asgi, base_url=BASE, follow_redirects=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ---------------------------------------------------------------- helpers

    def register(self, redirect=CLAUDE):
        return self.client.post(
            "/register",
            json={
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "Claude",
            },
        )

    def login(self, redirect=CLAUDE):
        client_id = self.register(redirect).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        r = self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "claude-state",
                "resource": f"{BASE}/mcp",
            },
        )
        self.assertEqual(r.status_code, 302, r.text)
        google = urlparse(r.headers["location"])
        self.assertEqual(google.netloc, "accounts.google.com")
        gq = parse_qs(google.query)
        self.assertEqual(gq["redirect_uri"], [f"{BASE}/oauth/google/callback"])
        self.assertEqual(gq["login_hint"], [ME])
        back = self.client.get(
            "/oauth/google/callback", params={"state": gq["state"][0], "code": "google-code"}
        )
        self.assertEqual(back.status_code, 302)
        return client_id, verifier, urlparse(back.headers["location"])

    def tokens(self, redirect=CLAUDE):
        client_id, verifier, back = self.login(redirect)
        q = parse_qs(back.query)
        self.assertEqual(q["state"], ["claude-state"])
        r = self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"][0],
                "redirect_uri": redirect,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": f"{BASE}/mcp",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        return client_id, r.json(), q["code"][0], verifier

    def mcp(self, token=None):
        headers = dict(MCP_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/mcp", json=INIT, headers=headers)

    # ---------------------------------------------------------------- tests

    def test_discovery_points_at_this_server(self):
        meta = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(meta["authorization_servers"], [BASE])
        asm = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(asm["registration_endpoint"], f"{BASE}/register")

    def test_mcp_without_a_token_is_refused(self):
        r = self.mcp()
        self.assertEqual(r.status_code, 401)
        self.assertIn("resource_metadata", r.headers["www-authenticate"])
        self.assertEqual(self.mcp("made-up").status_code, 401)

    def test_full_login_then_mcp_works(self):
        _, tok, *_ = self.tokens()
        self.assertEqual(tok["token_type"], "Bearer")
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 200)
        login = [e for e in self.app.audit.tail(20) if e["tool"] == "oauth_login"]
        self.assertEqual((login[-1]["actor"], login[-1]["outcome"]), (ME, "ok"))

    def test_claude_code_loopback_redirect_works(self):
        _, tok, *_ = self.tokens(redirect="http://localhost:53682/callback")
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 200)

    def test_other_google_accounts_are_refused(self):
        self.http.google_claims = google_claims(email="someone@else.com")
        _, _, back = self.login()
        q = parse_qs(back.query)
        self.assertEqual(q["error"], ["access_denied"])
        self.assertNotIn("code", q)
        self.assertEqual(self.app.audit.tail(1)[0]["outcome"], "denied")

    def test_unverified_or_wrong_audience_is_refused(self):
        for claims in (google_claims(verified=False), google_claims(aud="someone-elses-app")):
            self.http.google_claims = claims
            _, _, back = self.login()
            self.assertIn("error", parse_qs(back.query))

    def test_unknown_redirect_uris_cannot_register(self):
        r = self.register("https://evil.example/callback")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_redirect_uri")

    def test_codes_are_single_use_and_need_pkce(self):
        client_id, _, code, verifier = self.tokens()
        again = self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        self.assertEqual(again.status_code, 400)

        client_id, _, back = self.login()
        wrong = self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": parse_qs(back.query)["code"][0],
                "redirect_uri": CLAUDE,
                "client_id": client_id,
                "code_verifier": "x" * 50,
            },
        )
        self.assertEqual(wrong.status_code, 400)

    def test_google_callback_state_is_single_use(self):
        client_id = self.register().json()["client_id"]
        r = self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLAUDE,
                "code_challenge": "a" * 43,
                "code_challenge_method": "S256",
                "state": "s",
            },
        )
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        first = self.client.get("/oauth/google/callback", params={"state": state, "code": "c"})
        second = self.client.get("/oauth/google/callback", params={"state": state, "code": "c"})
        self.assertEqual((first.status_code, second.status_code), (302, 400))

    def test_refresh_rotates_and_old_tokens_die(self):
        client_id, tok, *_ = self.tokens()
        refresh = lambda rt: self.client.post(  # noqa: E731
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id},
        )
        new = refresh(tok["refresh_token"])
        self.assertEqual(new.status_code, 200, new.text)
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 401)
        self.assertEqual(self.mcp(new.json()["access_token"]).status_code, 200)
        self.assertEqual(refresh(tok["refresh_token"]).status_code, 400)

    def test_access_tokens_expire(self):
        _, tok, *_ = self.tokens()
        self.clock.advance(hours=2)
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 401)

    def test_revocation(self):
        client_id, tok, *_ = self.tokens()
        r = self.client.post(
            # The SDK's revoke handler wants the client_secret field even when it's empty.
            "/revoke",
            data={"token": tok["access_token"], "client_id": client_id, "client_secret": ""},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 401)

    def test_tokens_are_not_stored_in_the_clear(self):
        _, tok, *_ = self.tokens()
        state = self.app.audit.path.parent
        stored = "".join(p.read_text() for p in (state / "oauth").rglob("*.json"))
        self.assertNotIn(tok["access_token"], stored)
        self.assertNotIn(tok["refresh_token"], stored)

    def test_removing_an_email_cuts_off_its_tokens(self):
        import dataclasses

        _, tok, *_ = self.tokens()
        provider = self.app.server._auth_server_provider
        provider.cfg = dataclasses.replace(CFG, allowed_emails=frozenset({"new@example.com"}))
        self.assertEqual(self.mcp(tok["access_token"]).status_code, 401)


class WithoutOAuth(unittest.TestCase):
    def test_iam_gated_setup_is_unchanged(self):
        app, *_ = make_app()
        asgi = app.server.streamable_http_app(stateless_http=True, host="0.0.0.0")  # noqa: S104
        with TestClient(asgi, base_url=BASE) as client:
            r = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(client.get("/oauth/google/callback").status_code, 404)


class Config(unittest.TestCase):
    ENV = {
        "HOMESTEAD_PUBLIC_URL": "https://x.run.app/",
        "HOMESTEAD_GOOGLE_CLIENT_ID": "id",
        "HOMESTEAD_GOOGLE_CLIENT_SECRET": "secret",
        "HOMESTEAD_ALLOWED_EMAILS": "Me@Example.com, you@example.com",
    }

    def test_off_without_a_public_url(self):
        self.assertIsNone(OAuthConfig.from_env({}))

    def test_parses_and_normalizes(self):
        cfg = OAuthConfig.from_env(self.ENV)
        self.assertEqual(cfg.public_url, "https://x.run.app")
        self.assertEqual(cfg.allowed_emails, {"me@example.com", "you@example.com"})

    def test_missing_pieces_fail_loudly(self):
        for key in ("HOMESTEAD_GOOGLE_CLIENT_SECRET", "HOMESTEAD_ALLOWED_EMAILS"):
            with self.assertRaises(ConfigError):
                OAuthConfig.from_env({**self.ENV, key: ""})
        with self.assertRaises(ConfigError):
            OAuthConfig.from_env({**self.ENV, "HOMESTEAD_PUBLIC_URL": "http://x.run.app"})
        with self.assertRaises(ConfigError):
            OAuthConfig.from_env({**self.ENV, "HOMESTEAD_PUBLIC_URL": "https://x.run.app/mcp"})

    def test_redirect_allowlist(self):
        self.assertTrue(redirect_allowed(CLAUDE))
        self.assertTrue(redirect_allowed("http://127.0.0.1:9999/cb"))
        self.assertFalse(redirect_allowed("https://claude.ai.evil.com/api/mcp/auth_callback"))
        self.assertFalse(redirect_allowed("https://localhost/cb"))  # loopback must be http
        self.assertTrue(redirect_allowed("https://x/cb", extra=("https://x/cb",)))

    def test_id_token_payload(self):
        from helpers import fake_jwt

        self.assertEqual(id_token_claims(fake_jwt({"email": ME}))["email"], ME)


if __name__ == "__main__":
    unittest.main()
