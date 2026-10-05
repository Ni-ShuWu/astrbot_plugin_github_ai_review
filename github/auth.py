"""GitHub 认证策略：静态 Token 与 GitHub App（JWT + 安装令牌）。

GitHubClient 仅依赖 ``token_for(repo)`` 接口，不关心具体模式；
GitHub App 模式自动发现 installation、缓存安装令牌并在过期前刷新。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

_API_BASE = "https://api.github.com"
_USER_AGENT = "astrbot-plugin-github-ai-review"
_JWT_TTL_SECONDS = 540  # GitHub 上限 10 分钟，留 1 分钟余量
_TOKEN_REFRESH_MARGIN = 60  # 安装令牌提前 60s 刷新


class GitHubAppAuthError(Exception):
    """GitHub App 认证失败（私钥非法、installation 不可达等）。"""


class StaticTokenAuth:
    """Personal Access Token 认证（回退模式）。"""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token_for(self, repo: str | None) -> str:
        return self._token


class GitHubAppAuth:
    """GitHub App 认证：RS256 JWT → installation access token。

    :param app_id: GitHub App ID。
    :param private_key: PEM 私钥文本（须先经 load_private_key 规整）。
    :param session: 复用的 aiohttp 会话。
    :param installation_id: 固定安装 ID；None 时按仓库自动发现。
    """

    def __init__(
        self,
        app_id: int,
        private_key: str,
        session: aiohttp.ClientSession,
        installation_id: int | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._session = session
        self._fixed_installation = installation_id
        self._jwt_cache: tuple[str, float] | None = None
        self._installation_ids: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def load_private_key(value: str) -> str:
        """规整私钥配置：支持 PEM 原文、单行 \\n 转义粘贴、文件路径。

        :raises GitHubAppAuthError: 三种形态均不合法时抛出。
        """

        text = value.strip()
        if "BEGIN" in text:
            return text.replace("\\n", "\n")
        path = Path(text)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        raise GitHubAppAuthError(
            "GitHub App 私钥无效：既非 PEM 内容也不是可读文件路径"
        )

    def _jwt(self) -> str:
        """签发（或复用）App JWT。"""

        now = time.time()
        if self._jwt_cache and self._jwt_cache[1] - 30 > now:
            return self._jwt_cache[0]
        import jwt  # PyJWT，运行期加载（requirements.txt 声明）

        expires = now + _JWT_TTL_SECONDS
        token = jwt.encode(
            {"iat": int(now) - 60, "exp": int(expires), "iss": str(self._app_id)},
            self._private_key,
            algorithm="RS256",
        )
        self._jwt_cache = (token, expires)
        return token

    async def token_for(self, repo: str | None) -> str:
        """获取仓库对应的安装令牌；过期前自动刷新。"""

        installation_id = await self._installation_id_for(repo)
        async with self._lock:
            cached = self._tokens.get(installation_id)
            now = time.time()
            if cached and cached[1] - _TOKEN_REFRESH_MARGIN > now:
                return cached[0]
            body = await self._app_request(
                "POST", f"/app/installations/{installation_id}/access_tokens"
            )
            token = body.get("token")
            expires_at = body.get("expires_at")
            if not token or not expires_at:
                raise GitHubAppAuthError(f"安装令牌响应异常: {body}")
            expiry = _parse_iso8601(expires_at)
            self._tokens[installation_id] = (token, expiry)
            logger.info(
                f"[gh-review] 已获取安装令牌 installation={installation_id}，"
                f"过期时间 {expires_at}"
            )
            return token

    async def permissions_for(self, repo: str) -> dict[str, str]:
        """查询 App 对仓库的安装权限，如 {'pull_requests': 'write'}。"""

        body = await self._app_request("GET", f"/repos/{repo}/installation")
        return {k: str(v) for k, v in (body.get("permissions") or {}).items()}

    async def _installation_id_for(self, repo: str | None) -> int:
        if self._fixed_installation:
            return self._fixed_installation
        if not repo:
            raise GitHubAppAuthError("未配置 installation_id 时请求必须携带仓库")
        cached = self._installation_ids.get(repo)
        if cached:
            return cached
        body = await self._app_request("GET", f"/repos/{repo}/installation")
        installation_id = int(body["id"])
        self._installation_ids[repo] = installation_id
        logger.info(f"[gh-review] 仓库 {repo} → installation {installation_id}")
        return installation_id

    async def _app_request(self, method: str, path: str) -> Any:
        """以 App JWT 调用 GitHub API（仅认证相关端点，不经 client 重试）。"""

        headers = {
            "Authorization": f"Bearer {self._jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
        }
        try:
            async with self._session.request(
                method,
                f"{_API_BASE}{path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = (
                    await resp.json()
                    if resp.content_type == "application/json"
                    else await resp.text()
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GitHubAppAuthError(f"GitHub App 认证请求失败: {exc}") from exc
        if resp.status >= 400:
            raise GitHubAppAuthError(
                f"GitHub App 认证接口错误({resp.status}) {method} {path}: {body}"
            )
        return body


def _parse_iso8601(value: str) -> float:
    """解析 GitHub 的 UTC ISO8601 时间（2016-07-11T22:14:10Z）。"""

    return (
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
