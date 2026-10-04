"""GitHub 轮询器：定时发现新 PR / 新 commit，产出 PullRequestEvent。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from astrbot.api import logger

from github.client import GitHubError

if TYPE_CHECKING:
    from github.client import GitHubClient
    from github.models import PullRequestEvent
    from models import GitHubConfig

from .state_store import CursorStore

EventHandler = Callable[["PullRequestEvent"], Awaitable[None]]


class Poller:
    """按配置间隔轮询目标仓库；与游标对比后回调事件处理器。"""

    def __init__(
        self,
        github: "GitHubClient",
        config: "GitHubConfig",
        cursor: CursorStore,
        handler: EventHandler,
    ) -> None:
        self._gh = github
        self._config = config
        self._cursor = cursor
        self._handler = handler
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        """启动后台轮询循环。"""

        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """优雅停止：置标志位并等待循环退出。"""

        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            await self.scan_once()
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._config.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def scan_once(self, repo: str | None = None) -> int:
        """执行一轮扫描，返回触发的审查事件数；可被指令直接调用。"""

        repos = [repo] if repo else self._config.repositories
        dispatched = 0
        for name in repos:
            if name not in self._config.repositories:
                logger.warning(f"[gh-review] 跳过未配置仓库: {name}")
                continue
            try:
                prs = await self._gh.list_open_prs(name)
            except GitHubError as exc:
                logger.error(f"[gh-review] 拉取 PR 列表失败 {name}: {exc}")
                continue
            for event in sorted(prs, key=lambda e: e.number):
                seen = self._cursor.get(event.cursor_key)
                if seen == event.head_sha:
                    continue
                event = self._gh.fix_event_type(event, seen)
                logger.info(
                    f"[gh-review] 发现待审 PR {event.cursor_key} "
                    f"({event.event_type.value}, {event.head_sha[:7]})"
                )
                try:
                    await self._handler(event)
                    dispatched += 1
                except Exception:  # noqa: BLE001 - 单 PR 失败隔离
                    logger.exception(f"[gh-review] 处理 {event.cursor_key} 异常")
        return dispatched
