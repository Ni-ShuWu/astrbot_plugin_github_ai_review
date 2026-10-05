"""GitHub App 认证与客户端认证注入测试。"""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from github.auth import (
    GitHubAppAuth,
    GitHubAppAuthError,
    StaticTokenAuth,
    _parse_iso8601,
)
from github.client import GitHubClient
from models import PluginConfig

_FAR_FUTURE = "2999-01-01T00:00:00Z"
_PAST = "2000-01-01T00:00:00Z"


def _make_rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


_PEM = _make_rsa_pem()


def _public_key():
    private = serialization.load_pem_private_key(_PEM.encode(), password=None)
    return private.public_key()


class _FakeResp:
    def __init__(self, status: int, body):
        self.status = status
        self.content_type = "application/json"
        self.headers: dict = {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class FakeSession:
    """按 (method, path) 路由的假 aiohttp 会话。"""

    def __init__(self, routes: dict):
        self._routes = routes
        self.requests: list[tuple] = []

    def request(self, method, url, headers=None, **kwargs):
        path = url.replace("https://api.github.com", "")
        self.requests.append((method, path, headers or {}))
        result = self._routes.get((method, path))
        if result is None:
            return _FakeResp(404, {"message": "not found"})
        if isinstance(result, Exception):
            raise result
        return _FakeResp(200, result)

    def calls(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.requests if (m, p) == (method, path))


def _app_routes(**overrides):
    routes = {
        ("GET", "/repos/octo/demo/installation"): {
            "id": 777,
            "permissions": {"pull_requests": "write", "issues": "write"},
        },
        ("POST", "/app/installations/777/access_tokens"): {
            "token": "inst-token-1",
            "expires_at": _FAR_FUTURE,
        },
    }
    routes.update(overrides)
    return routes


class TestLoadPrivateKey:
    def test_pem_passthrough(self):
        assert GitHubAppAuth.load_private_key(_PEM).strip() == _PEM.strip()

    def test_escaped_newlines(self):
        escaped = _PEM.replace("\n", "\\n")
        assert GitHubAppAuth.load_private_key(escaped).strip() == _PEM.strip()

    def test_file_path(self, tmp_path):
        path = tmp_path / "app.pem"
        path.write_text(_PEM, encoding="utf-8")
        assert GitHubAppAuth.load_private_key(str(path)) == _PEM

    def test_invalid_raises(self):
        with pytest.raises(GitHubAppAuthError):
            GitHubAppAuth.load_private_key("not-a-key-not-a-path")


class TestJwt:
    def test_jwt_claims(self):
        import jwt

        auth = GitHubAppAuth(123, _PEM, None)  # 本用例不触网
        claims = jwt.decode(
            auth._jwt(), _public_key(), algorithms=["RS256"]
        )
        assert claims["iss"] == "123"
        assert claims["exp"] - claims["iat"] == 600  # TTL 540 + iat 回拨 60

    def test_jwt_cached(self):
        auth = GitHubAppAuth(123, _PEM, None)
        assert auth._jwt() == auth._jwt()


class TestInstallationToken:
    async def test_auto_discovery_and_cache(self):
        session = FakeSession(_app_routes())
        auth = GitHubAppAuth(123, _PEM, session)

        t1 = await auth.token_for("octo/demo")
        t2 = await auth.token_for("octo/demo")

        assert t1 == t2 == "inst-token-1"
        assert session.calls("GET", "/repos/octo/demo/installation") == 1
        assert session.calls("POST", "/app/installations/777/access_tokens") == 1

    async def test_fixed_installation_skips_discovery(self):
        session = FakeSession(_app_routes())
        auth = GitHubAppAuth(123, _PEM, session, installation_id=777)

        assert await auth.token_for("octo/demo") == "inst-token-1"
        assert session.calls("GET", "/repos/octo/demo/installation") == 0

    async def test_refresh_when_expired(self):
        routes = _app_routes()
        routes[("POST", "/app/installations/777/access_tokens")] = {
            "token": "stale",
            "expires_at": _PAST,
        }
        session = FakeSession(routes)
        auth = GitHubAppAuth(123, _PEM, session)

        await auth.token_for("octo/demo")
        await auth.token_for("octo/demo")
        assert session.calls("POST", "/app/installations/777/access_tokens") == 2

    async def test_missing_installation_raises(self):
        session = FakeSession({})
        auth = GitHubAppAuth(123, _PEM, session)
        with pytest.raises(GitHubAppAuthError, match="404"):
            await auth.token_for("octo/demo")

    async def test_jwt_sent_in_headers(self):
        import jwt

        session = FakeSession(_app_routes())
        auth = GitHubAppAuth(123, _PEM, session)
        await auth.token_for("octo/demo")

        _, _, headers = session.requests[0]
        assert headers["Authorization"].startswith("Bearer ey")
        token = headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, _public_key(), algorithms=["RS256"])
        assert claims["iss"] == "123"

    async def test_permissions_for(self):
        session = FakeSession(_app_routes())
        auth = GitHubAppAuth(123, _PEM, session)
        perms = await auth.permissions_for("octo/demo")
        assert perms == {"pull_requests": "write", "issues": "write"}


class TestParseIso8601:
    def test_github_format(self):
        assert _parse_iso8601("2016-07-11T22:14:10Z") == 1468275250.0


class TestClientAuthInjection:
    async def test_client_uses_auth_token(self):
        session = FakeSession(
            {("GET", "/repos/octo/demo"): {"permissions": {"push": True}}}
        )
        client = GitHubClient(StaticTokenAuth("pat-xyz"), session)

        perms = await client.check_permissions("octo/demo")

        assert perms == {"push"}
        _, _, headers = session.requests[0]
        assert headers["Authorization"] == "Bearer pat-xyz"

    async def test_static_auth_ignores_repo(self):
        auth = StaticTokenAuth("same")
        assert await auth.token_for("a/b") == await auth.token_for(None) == "same"


class TestAppModeConfig:
    def test_app_mode_valid(self, config_dict):
        config_dict["github"] = {
            "app_id": "12345",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----",
            "repositories": ["octo/demo"],
        }
        cfg = PluginConfig.from_dict(config_dict)
        assert cfg.github.auth_mode == "app"
        assert cfg.github.app_id == 12345
        assert cfg.github.installation_id == 0

    def test_partial_app_config_raises(self, config_dict):
        config_dict["github"] = {"app_id": "123", "repositories": ["octo/demo"]}
        with pytest.raises(ValueError, match="同时配置"):
            PluginConfig.from_dict(config_dict)

    def test_no_auth_at_all_raises(self, config_dict):
        config_dict["github"] = {"repositories": ["octo/demo"]}
        with pytest.raises(ValueError, match="未配置 GitHub 认证"):
            PluginConfig.from_dict(config_dict)

    def test_pat_mode_still_works(self, config):
        assert config.github.auth_mode == "pat"

    def test_bad_app_id_raises(self, config_dict):
        config_dict["github"]["app_id"] = "abc"
        config_dict["github"]["private_key"] = "BEGIN"
        with pytest.raises(ValueError, match="app_id"):
            PluginConfig.from_dict(config_dict)

    def test_installation_id_parsed(self, config_dict):
        config_dict["github"].update(
            {"app_id": 1, "private_key": "BEGIN", "installation_id": "777"}
        )
        cfg = PluginConfig.from_dict(config_dict)
        assert cfg.github.installation_id == 777
