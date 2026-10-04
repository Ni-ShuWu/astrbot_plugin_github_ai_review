"""审查结果解析：JSON 提取、schema 校验、按强度档位裁决。"""

import json
import re
from typing import Any

from astrbot.api import logger

from github.models import PullRequestEvent
from models.review import IssueSeverity, ReviewIssue, ReviewResult, Verdict

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_FALLBACK_SUMMARY = 4000


class ResultParser:
    """将 LLM 原始输出解析为 ReviewResult；失败降级为纯文本评论。"""

    def parse(self, event: PullRequestEvent, raw: str, level: str) -> ReviewResult:
        """解析并裁决；任何解析失败都不会抛出，而是降级。"""

        data = self._extract_json(raw)
        if data is None:
            logger.warning(f"[gh-review] LLM 输出非 JSON，降级处理 {event.cursor_key}")
            return ReviewResult(
                event=event,
                verdict=Verdict.COMMENT,
                summary=raw[:_MAX_FALLBACK_SUMMARY],
                raw_llm_output=raw,
                degraded=True,
            )

        issues = [self._parse_issue(item) for item in data.get("issues") or []]
        issues = [i for i in issues if i is not None]
        verdict = self._adjust_verdict(
            level, str(data.get("verdict", "")).upper(), issues
        )
        refs = data.get("guideline_refs")
        return ReviewResult(
            event=event,
            verdict=verdict,
            summary=str(data.get("summary", "")).strip() or "（模型未给出汇总）",
            issues=issues,
            prompt_injection_suspected=bool(
                data.get("prompt_injection_suspected", False)
            ),
            guideline_refs=[str(r) for r in refs] if isinstance(refs, list) else [],
            raw_llm_output=raw,
        )

    def _extract_json(self, raw: str) -> dict[str, Any] | None:
        """依次尝试：整体解析 → 去围栏 → 首个大括号块。"""

        text = raw.strip()
        for candidate in (
            text,
            (m.group(1) if (m := _FENCE_RE.search(text)) else ""),
            (m.group(0) if (m := _BRACE_RE.search(text)) else ""),
        ):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def _parse_issue(self, item: Any) -> ReviewIssue | None:
        if not isinstance(item, dict):
            return None
        title = str(item.get("title", "")).strip()
        detail = str(item.get("detail", "")).strip()
        if not title and not detail:
            return None
        line = item.get("line")
        file = item.get("file")
        suggestion = item.get("suggestion")
        return ReviewIssue(
            severity=IssueSeverity.from_str(str(item.get("severity", ""))),
            file=str(file) if file else None,
            line=line if isinstance(line, int) and line > 0 else None,
            title=title or detail[:50],
            detail=detail or title,
            suggestion=str(suggestion) if suggestion else None,
        )

    def _adjust_verdict(
        self, level: str, llm_verdict: str, issues: list[ReviewIssue]
    ) -> Verdict:
        """以问题清单为准确定裁决（不让 LLM 在有问题时自批 APPROVE）。

        loose : 仅 BLOCKER → REQUEST_CHANGES；有问题 → COMMENT；无 → APPROVE
        normal: BLOCKER/MAJOR → REQUEST_CHANGES；其余问题 → COMMENT
        strict: BLOCKER/MAJOR → REQUEST_CHANGES；任何问题 → COMMENT
        """

        severities = {i.severity for i in issues}
        if IssueSeverity.BLOCKER in severities:
            return Verdict.REQUEST_CHANGES
        if IssueSeverity.MAJOR in severities:
            return Verdict.COMMENT if level == "loose" else Verdict.REQUEST_CHANGES
        if issues:
            return Verdict.COMMENT
        if llm_verdict == Verdict.REQUEST_CHANGES.value:
            return Verdict.COMMENT  # 无问题但模型要求改：降级为评论
        return Verdict.APPROVE
