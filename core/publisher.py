"""发表器：将审查结果与注入处置发布到 GitHub。

GitHubClient 的写操作仅允许经本模块调用；LLM 输出只作为数据，
动作决策（发 Review / 评论 / 关闭）全部由本模块按配置做出。
"""

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger

from github.client import GitHubError, GitHubPermissionError
from models.review import ReviewResult, Verdict

if TYPE_CHECKING:
    from github.client import GitHubClient
    from github.models import PullRequestEvent
    from models import PluginConfig
    from security.injection import InjectionFinding

_WRITE_RETRIES = 2


class Publisher:
    """GitHub 侧发表与注入关闭流程。"""

    def __init__(self, github: "GitHubClient", config: "PluginConfig") -> None:
        self._gh = github
        self._config = config
        self.can_close = True  # 启动自检无写权限时置 False，自动降级

    async def publish(self, result: ReviewResult) -> None:
        """发表审查结果：Review + 行内评论，或按策略降级为评论。"""

        event = result.event
        if (
            result.degraded
            or result.downgrade_only
            or not self._config.review.publish_review
        ):
            await self._comment_with_retry(event, self._render_body(result))
            return

        comments = [
            {
                "path": issue.file,
                "line": issue.line,
                "side": "RIGHT",
                "body": self._render_issue(issue),
            }
            for issue in result.issues
            if issue.file and issue.line
        ]
        try:
            await self._gh.create_review(
                event.repo,
                event.number,
                event.head_sha,
                result.verdict.value,
                self._render_body(result),
                comments,
            )
        except (GitHubError, GitHubPermissionError) as exc:
            logger.warning(
                f"[gh-review] Review 发表失败，降级为评论 {event.cursor_key}: {exc}"
            )
            await self._comment_with_retry(event, self._render_body(result))

    async def injection_block(
        self,
        event: "PullRequestEvent",
        findings: list["InjectionFinding"],
        *,
        llm_flagged: bool,
        close: bool,
    ) -> None:
        """注入处置：警告评论 → REQUEST_CHANGES → 标签 → （可选）关闭。

        任一前置失败仍继续后续步骤（尽力留证）。
        """

        body = self._render_injection_notice(event, findings, llm_flagged, close)
        await self._comment_with_retry(event, body)

        if self._config.review.publish_review:
            try:
                await self._gh.create_review(
                    event.repo,
                    event.number,
                    event.head_sha,
                    Verdict.REQUEST_CHANGES.value,
                    "⚠️ 检测到疑似提示词注入内容，详见评论。",
                    [],
                )
            except (GitHubError, GitHubPermissionError) as exc:
                logger.warning(f"[gh-review] 注入 Review 发表失败: {exc}")

        label = self._config.security.label_on_injection
        if label:
            await self._gh.add_label(event.repo, event.number, label)

        if close:
            if not self.can_close:
                logger.warning(
                    f"[gh-review] 无写权限，跳过关闭 {event.cursor_key}（已评论留证）"
                )
                return
            for attempt in range(_WRITE_RETRIES + 1):
                try:
                    await self._gh.close_pr(event.repo, event.number)
                    logger.info(
                        f"[gh-review] 已自动关闭注入 PR {event.cursor_key} "
                        f"(llm_flagged={llm_flagged})"
                    )
                    return
                except (GitHubError, GitHubPermissionError) as exc:
                    if attempt == _WRITE_RETRIES:
                        logger.error(
                            f"[gh-review] 关闭 {event.cursor_key} 失败: {exc}"
                        )
                    else:
                        await asyncio.sleep(1.5 * (attempt + 1))

    async def notify_unavailable(self, event: "PullRequestEvent", reason: str) -> None:
        """LLM 不可用时的兜底评论。"""

        await self._comment_with_retry(
            event,
            f"🤖 AI 审查暂时不可用（{reason}）。"
            "维护者可在修复后使用 `/pr_review recheck` 重新触发审查。",
        )

    async def _comment_with_retry(
        self, event: "PullRequestEvent", body: str
    ) -> None:
        for attempt in range(_WRITE_RETRIES + 1):
            try:
                await self._gh.create_comment(event.repo, event.number, body)
                return
            except (GitHubError, GitHubPermissionError) as exc:
                if attempt == _WRITE_RETRIES:
                    logger.error(
                        f"[gh-review] 评论发表失败 {event.cursor_key}: {exc}"
                    )
                else:
                    await asyncio.sleep(1.5 * (attempt + 1))

    def _render_body(self, result: ReviewResult) -> str:
        icon = {
            Verdict.APPROVE: "✅",
            Verdict.COMMENT: "💬",
            Verdict.REQUEST_CHANGES: "🔧",
        }[result.verdict]
        parts = [
            f"## {icon} AI 审查（{self._config.review.level} 强度）",
            "",
            result.summary,
        ]
        if result.guideline_refs:
            parts += ["", "**依据规范**: " + "、".join(result.guideline_refs)]
        if result.degraded:
            parts += ["", "> ⚠️ 模型输出未遵循结构化格式，已降级为原文展示。"]
        parts += ["", "---", "🤖 Powered by AstrBot GitHub AI Review"]
        return "\n".join(parts)

    def _render_issue(self, issue) -> str:
        badge = {
            "blocker": "🔴 BLOCKER",
            "major": "🟠 MAJOR",
            "minor": "🟡 MINOR",
            "nit": "⚪ NIT",
        }[issue.severity.value]
        parts = [f"**{badge}** {issue.title}", "", issue.detail]
        if issue.suggestion:
            parts += ["", f"**建议**: {issue.suggestion}"]
        return "\n".join(parts)

    def _render_injection_notice(
        self,
        event: "PullRequestEvent",
        findings: list["InjectionFinding"],
        llm_flagged: bool,
        closed: bool,
    ) -> str:
        template = self._config.security.close_comment_template
        lines = [
            f"## 🚨 检测到疑似提示词注入 @{event.author_login}",
            "",
            (
                "本 PR 的内容中包含疑似针对 AI 审查器的提示词注入指令，"
                "审查流程已中止。"
            ),
        ]
        if findings:
            lines += ["", "**命中模式**:"]
            lines += [
                f"- `{f.pattern_name}`（{f.source}）: `{f.matched_text}`"
                for f in findings[:10]
            ]
        if llm_flagged:
            lines += ["", "AI 审查器亦在内容中识别到可疑注入标记。"]
        lines += [
            "",
            "**处置**: " + ("本 PR 已被自动关闭。" if closed else "已要求修改。"),
            "如属误判，请联系仓库维护者人工复核后重新打开。",
        ]
        if template:
            lines += ["", template]
        return "\n".join(lines)
