"""GitHub 领域模型：轮询产出的不可变事件与文件变更。"""

from dataclasses import dataclass
from enum import Enum


class PullRequestEventType(Enum):
    """触发审查的 PR 事件类型。"""

    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    REOPENED = "reopened"


@dataclass(frozen=True)
class PullRequestEvent:
    """一次待审查的 PR 事件；(repo, number, head_sha) 为去重键。"""

    repo: str  # "owner/repo"
    number: int
    title: str
    body: str
    author_login: str
    head_sha: str
    base_branch: str
    event_type: PullRequestEventType
    html_url: str

    @property
    def cursor_key(self) -> str:
        """游标存储键。"""

        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class PullRequestFile:
    """PR 中单个文件的变更摘要与 patch。"""

    filename: str
    status: str  # added/modified/removed/renamed
    additions: int
    deletions: int
    patch: str  # 大文件时 GitHub 可能不返回，置空字符串
