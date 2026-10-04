"""审查结果模型：LLM 输出的结构化表示。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github.models import PullRequestEvent


class Verdict(Enum):
    """审查结论，直接映射 GitHub Review 事件类型。"""

    APPROVE = "APPROVE"
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class IssueSeverity(Enum):
    """问题严重级别。"""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"

    @classmethod
    def from_str(cls, value: str) -> "IssueSeverity":
        """容错解析：非法值映射为 MINOR。"""

        try:
            return cls(value.strip().lower())
        except (ValueError, AttributeError):
            return cls.MINOR


@dataclass
class ReviewIssue:
    """单条审查问题；file/line 为 None 表示整体性问题。"""

    severity: IssueSeverity
    file: str | None
    line: int | None
    title: str
    detail: str
    suggestion: str | None = None


@dataclass
class ReviewResult:
    """一次完整审查的结果，供 publisher 消费。"""

    event: "PullRequestEvent"
    verdict: Verdict
    summary: str
    issues: list[ReviewIssue] = field(default_factory=list)
    prompt_injection_suspected: bool = False
    injection_prescan_hit: bool = False
    guideline_refs: list[str] = field(default_factory=list)
    raw_llm_output: str = ""
    degraded: bool = False
    downgrade_only: bool = False  # 白名单 downgrade：仅评论，不发 Review
