"""GitHub REST API 封装：认证、分页、ETag 条件请求、限流退避。

core 层只面对本类的方法语义，不接触 URL/header/分页细节。
写操作（review/comment/label/close）仅允许 publisher 调用。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import aiohttp
from astrbot.api import logger

from .models import PullRequestEvent, PullRequestEventType, PullRequestFile

if TYPE_CHECKING:
    from .auth import GitHubAppAuth, StaticTokenAuth

_API_BASE = "https://api.github.com"
_USER_AGENT = "astrbot-plugin-github-ai-review"
_MAX_ATTEMPTS = 3
_PER_PAGE = 100


class GitHubError(Exception):
    """GitHub API 调用失败。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class GitHubPermissionError(GitHubError):
    """Token 权限不足（401/403 非限流）。"""


class GitHubClient:
    """GitHub REST 客户端；会话与认证策略由外部注入。

    auth 为 StaticTokenAuth 或 GitHubAppAuth；令牌按请求现取，
    GitHub App 模式下自动携带对应仓库的安装令牌。
    """

    def __init__(
        self,
        auth: StaticTokenAuth | GitHubAppAuth,
        session: aiohttp.ClientSession,
    ) -> None:
        self._session = session
        self._auth = auth

    async def _request(
        self,
        method: str,
        path: str,
        *,
        repo: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, aiohttp.typedefs.LooseHeaders]:
        """带退避重试的底层请求；返回 (状态码, JSON 体, 响应头)。"""

        token = await self._auth.token_for(repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
        }
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with self._session.request(
                    method,
                    f"{_API_BASE}{path}",
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (429,) or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        wait = (
                            float(retry_after)
                            if retry_after and retry_after.isdigit()
                            else min(2**attempt * 2, 30) + random.random()
                        )
                        logger.warning(
                            f"[gh-review] GitHub {resp.status}，{wait:.1f}s 后重试 "
                            f"({attempt + 1}/{_MAX_ATTEMPTS}): {method} {path}"
                        )
                        await asyncio.sleep(wait)
                        continue
                    body = (
                        await resp.json()
                        if resp.content_type == "application/json"
                        else await resp.text()
                    )
                    if resp.status in (401, 403):
                        raise GitHubPermissionError(
                            f"GitHub 鉴权/权限失败({resp.status}): {body}", resp.status
                        )
                    if resp.status >= 400 and resp.status != 304:
                        raise GitHubError(
                            f"GitHub API 错误({resp.status}): {body}", resp.status
                        )
                    return resp.status, body, resp.headers
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise GitHubError(f"GitHub 网络错误: {exc}") from exc
                await asyncio.sleep(min(2**attempt * 2, 30) + random.random())
        raise GitHubError(f"GitHub 请求重试耗尽: {method} {path}")

    async def check_permissions(self, repo: str) -> set[str]:
        """自检 token 对仓库的权限集合，如 {'admin','push','pull'}。"""

        _, body, _ = await self._request("GET", f"/repos/{repo}", repo=repo)
        perms = body.get("permissions") or {}
        return {name for name, granted in perms.items() if granted}

    async def list_open_prs(self, repo: str) -> list[PullRequestEvent]:
        """列出开放 PR（按最近更新排序，全部分页）。

        event_type 统一先标记 OPENED，由 poller 对比游标后修正。
        """

        events: list[PullRequestEvent] = []
        page = 1
        while True:
            _, body, _ = await self._request(
                "GET",
                f"/repos/{repo}/pulls",
                repo=repo,
                params={
                    "state": "open",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": _PER_PAGE,
                    "page": page,
                },
            )
            for pr in body:
                events.append(
                    PullRequestEvent(
                        repo=repo,
                        number=pr["number"],
                        title=pr.get("title") or "",
                        body=pr.get("body") or "",
                        author_login=(pr.get("user") or {}).get("login", ""),
                        head_sha=pr["head"]["sha"],
                        base_branch=pr["base"]["ref"],
                        event_type=PullRequestEventType.OPENED,
                        html_url=pr.get("html_url", ""),
                    )
                )
            if len(body) < _PER_PAGE:
                return events
            page += 1

    def fix_event_type(
        self, event: PullRequestEvent, seen_sha: str | None
    ) -> PullRequestEvent:
        """根据游标修正事件类型：已见过=新 commit，否则为新 PR。"""

        if seen_sha is not None and seen_sha != event.head_sha:
            return replace(event, event_type=PullRequestEventType.SYNCHRONIZE)
        return event

    async def get_pr_files(self, repo: str, number: int) -> list[PullRequestFile]:
        """分页拉取 PR 文件变更列表。"""

        files: list[PullRequestFile] = []
        page = 1
        while True:
            _, body, _ = await self._request(
                "GET",
                f"/repos/{repo}/pulls/{number}/files",
                repo=repo,
                params={"per_page": _PER_PAGE, "page": page},
            )
            for item in body:
                files.append(
                    PullRequestFile(
                        filename=item["filename"],
                        status=item.get("status", "modified"),
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                        patch=item.get("patch") or "",
                    )
                )
            if len(body) < _PER_PAGE:
                return files
            page += 1

    async def get_file_content(
        self, repo: str, path: str, etag: str | None = None
    ) -> tuple[str | None, str | None]:
        """获取仓库文件文本；携带 ETag 时 304 返回 (None, 原 etag)。

        文件不存在返回 (None, None)。
        """

        headers = {"If-None-Match": etag} if etag else None
        try:
            status, body, resp_headers = await self._request(
                "GET",
                f"/repos/{repo}/contents/{path}",
                repo=repo,
                params={"ref": "HEAD"},
                extra_headers=headers,
            )
        except GitHubError as exc:
            if exc.status == 404:
                return None, None
            raise
        new_etag = resp_headers.get("ETag", etag)
        if status == 304:
            return None, etag
        import base64

        raw = base64.b64decode(body.get("content") or b"").decode(
            "utf-8", errors="replace"
        )
        return raw, new_etag

    async def create_review(
        self,
        repo: str,
        number: int,
        commit_id: str,
        event: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> None:
        """发表 PR Review；comments 为 [{path, line, side, body}]。"""

        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "event": event,
            "body": body,
        }
        if comments:
            payload["comments"] = comments
        await self._request(
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            repo=repo,
            json_body=payload,
        )

    async def create_comment(self, repo: str, number: int, body: str) -> None:
        """发表 issue 区评论（PR 汇总评论）。"""

        await self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            repo=repo,
            json_body={"body": body},
        )

    async def add_label(self, repo: str, number: int, label: str) -> None:
        """为 PR 打标签；标签不存在时静默忽略（不阻断主流程）。"""

        try:
            await self._request(
                "POST",
                f"/repos/{repo}/issues/{number}/labels",
                repo=repo,
                json_body={"labels": [label]},
            )
        except GitHubError as exc:
            logger.warning(f"[gh-review] 打标签失败（忽略）: {exc}")

    async def close_pr(self, repo: str, number: int) -> None:
        """关闭 PR（不合并）。"""

        await self._request(
            "PATCH",
            f"/repos/{repo}/pulls/{number}",
            repo=repo,
            json_body={"state": "closed"},
        )
