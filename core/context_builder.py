"""上下文构建：拉取 PR diff 与规范文档，组装 ReviewContext。

内含 GuidelineCache：TTL + ETag 条件请求 + per-key 锁防并发回源。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from github.client import GitHubClient
    from github.models import PullRequestEvent, PullRequestFile
    from models import GitHubConfig

_GUIDELINE_TTL = 600.0  # 秒
_GUIDELINE_MAX = 200


@dataclass
class ReviewContext:
    """单次审查的全部输入材料。"""

    event: "PullRequestEvent"
    files: list["PullRequestFile"]
    guidelines: dict[str, str]  # {路径: 内容}
    diff_text: str
    pr_meta_text: str
    diff_truncated: bool = False
    total_additions: int = 0
    total_deletions: int = 0
    missing_guidelines: list[str] = field(default_factory=list)


class GuidelineCache:
    """规范文档缓存：TTL 惰性过期，ETag 304 续期，FIFO 上限。"""

    def __init__(self, github: "GitHubClient") -> None:
        self._gh = github
        self._entries: dict[str, tuple[str, str | None, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def get(self, repo: str, path: str) -> str | None:
        """取规范文档内容；不存在返回 None。"""

        key = f"{repo}:{path}"
        async with await self._lock_for(key):
            entry = self._entries.get(key)
            etag = None
            if entry:
                content, etag, fetched_at = entry
                if time.monotonic() - fetched_at < _GUIDELINE_TTL:
                    return content
            content, new_etag = await self._gh.get_file_content(repo, path, etag)
            if content is None and new_etag is not None and entry:
                # 304：续期
                self._entries[key] = (entry[0], new_etag, time.monotonic())
                return entry[0]
            if content is None:
                self._entries.pop(key, None)
                return None
            while len(self._entries) >= _GUIDELINE_MAX:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = (content, new_etag, time.monotonic())
            return content

    def clear(self) -> None:
        """reload 指令时强制失效。"""

        self._entries.clear()


class ContextBuilder:
    """组装审查上下文；diff 超限按字符截断。"""

    def __init__(self, github: "GitHubClient", config: "GitHubConfig") -> None:
        self._gh = github
        self._config = config
        self.guideline_cache = GuidelineCache(github)

    async def build(self, event: "PullRequestEvent") -> ReviewContext:
        """拉取文件列表与规范文档，生成 diff 文本与 PR 元信息文本。"""

        files = (await self._gh.get_pr_files(event.repo, event.number))[
            : self._config.max_diff_files
        ]
        guidelines, missing = await self._fetch_guidelines(event.repo)
        diff_text, truncated = self._render_diff(files)
        pr_meta = (
            f"标题: {event.title}\n作者: {event.author_login}\n"
            f"目标分支: {event.base_branch}\n描述:\n{event.body}"
        )
        return ReviewContext(
            event=event,
            files=files,
            guidelines=guidelines,
            diff_text=diff_text,
            pr_meta_text=pr_meta,
            diff_truncated=truncated,
            total_additions=sum(f.additions for f in files),
            total_deletions=sum(f.deletions for f in files),
            missing_guidelines=missing,
        )

    async def _fetch_guidelines(
        self, repo: str
    ) -> tuple[dict[str, str], list[str]]:
        guidelines: dict[str, str] = {}
        missing: list[str] = []
        for path in self._config.guideline_files:
            try:
                content = await self.guideline_cache.get(repo, path)
            except Exception as exc:  # noqa: BLE001 - 规范拉取失败不阻断审查
                logger.warning(f"[gh-review] 规范拉取失败 {repo}:{path}: {exc}")
                content = None
            if content is None:
                missing.append(path)
            else:
                guidelines[path] = content
        return guidelines, missing

    def _render_diff(self, files: list["PullRequestFile"]) -> tuple[str, bool]:
        parts: list[str] = []
        budget = self._config.max_diff_chars
        used = 0
        truncated = False
        for f in files:
            header = f"--- {f.filename} ({f.status}, +{f.additions}/-{f.deletions})\n"
            body = f.patch or "（该文件 diff 过大，GitHub 未提供 patch）"
            chunk = header + body + "\n"
            if used + len(chunk) > budget:
                parts.append(header + "（后续内容因长度截断）\n")
                truncated = True
                break
            parts.append(chunk)
            used += len(chunk)
        return "".join(parts), truncated
