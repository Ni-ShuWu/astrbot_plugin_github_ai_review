"""审查流水线编排：filter → prescan → review → parse → publish → 游标落盘。

单 PR 失败隔离；注入处置路径不经过 LLM（预扫描命中时）。
"""

from typing import TYPE_CHECKING

from astrbot.api import logger

from github.client import GitHubError
from llm.client import LLMError

if TYPE_CHECKING:
    from github.client import GitHubClient
    from github.models import PullRequestEvent
    from models import PluginConfig

    from .context_builder import ContextBuilder
    from .filter import EventFilter
    from .publisher import Publisher
    from .result_parser import ResultParser
    from .review_engine import ReviewEngine
    from .state_store import CursorStore


class ReviewPipeline:
    """单 PR 的完整审查流程编排器。"""

    def __init__(
        self,
        config: "PluginConfig",
        github: "GitHubClient",
        event_filter: "EventFilter",
        builder: "ContextBuilder",
        engine: "ReviewEngine",
        parser: "ResultParser",
        publisher: "Publisher",
        cursor: "CursorStore",
    ) -> None:
        self._config = config
        self._gh = github
        self._filter = event_filter
        self._builder = builder
        self._engine = engine
        self._parser = parser
        self._publisher = publisher
        self._cursor = cursor

    async def handle(self, event: "PullRequestEvent") -> None:
        """处理一个 PR 事件；异常只在边界记录，不向上抛出。"""

        decision = self._filter.check(event)
        if not decision.allowed:
            logger.info(
                f"[gh-review] 跳过 {event.cursor_key}: {decision.reason}"
            )
            await self._cursor.mark(event.cursor_key, event.head_sha)
            return

        try:
            context = await self._builder.build(event)
        except GitHubError as exc:
            logger.error(f"[gh-review] 上下文构建失败 {event.cursor_key}: {exc}")
            return  # 不标记游标，下轮重试

        findings = self._engine.prescan(context)
        if findings:
            close = self._should_close(prescan_hit=True, llm_flagged=False, event=event)
            logger.warning(
                f"[gh-review] 预扫描命中注入 {event.cursor_key}: "
                f"{[f.pattern_name for f in findings]}"
            )
            await self._publisher.injection_block(
                event, findings, llm_flagged=False, close=close
            )
            await self._cursor.mark(event.cursor_key, event.head_sha)
            return

        try:
            raw = await self._engine.review(context)
        except LLMError as exc:
            logger.error(f"[gh-review] LLM 审查失败 {event.cursor_key}: {exc}")
            await self._publisher.notify_unavailable(event, str(exc))
            # 标记游标防止每轮刷屏；恢复后可用 recheck 指令重审
            await self._cursor.mark(event.cursor_key, event.head_sha)
            return

        result = self._parser.parse(event, raw, self._config.review.level)
        result.downgrade_only = decision.downgrade_only

        if result.prompt_injection_suspected:
            close = self._should_close(prescan_hit=False, llm_flagged=True, event=event)
            logger.warning(
                f"[gh-review] LLM 标记注入 {event.cursor_key} (close={close})"
            )
            await self._publisher.injection_block(
                event, [], llm_flagged=True, close=close
            )
        else:
            await self._publisher.publish(result)

        await self._cursor.mark(event.cursor_key, event.head_sha)

    def _should_close(
        self, *, prescan_hit: bool, llm_flagged: bool, event: "PullRequestEvent"
    ) -> bool:
        """按 security 策略判定是否自动关闭；白名单作者永不关闭。"""

        sec = self._config.security
        if not sec.auto_close_on_injection:
            return False
        if self._filter.is_whitelisted(event.author_login):
            return False
        if sec.close_requires == "both":
            # both 需要双通道确认；预扫描命中时 LLM 未执行，故不关闭
            return prescan_hit and llm_flagged
        return prescan_hit or llm_flagged
