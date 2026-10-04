"""事件过滤器：仓库范围、bot 名单、用户白名单（skip/downgrade）。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github.models import PullRequestEvent
    from models import PluginConfig


@dataclass(frozen=True)
class FilterDecision:
    """过滤结论。"""

    allowed: bool
    downgrade_only: bool = False  # 白名单作者：仅评论，不发 Review
    reason: str = ""


class EventFilter:
    """按配置对 PR 事件做准入判定。"""

    def __init__(self, config: "PluginConfig") -> None:
        self._config = config

    def check(self, event: "PullRequestEvent") -> FilterDecision:
        """依次应用仓库范围 → bot 名单 → 用户白名单。"""

        if event.repo not in self._config.github.repositories:
            return FilterDecision(False, reason="仓库不在审查范围")

        author = event.author_login.lower()
        bots = {b.lower() for b in self._config.whitelist.bot_logins}
        if author in bots or author.endswith("[bot]"):
            return FilterDecision(False, reason="bot 作者")

        users = {u.lower() for u in self._config.whitelist.users}
        if author in users:
            if self._config.whitelist.whitelist_action == "downgrade":
                return FilterDecision(True, downgrade_only=True, reason="白名单降级")
            return FilterDecision(False, reason="白名单跳过")

        return FilterDecision(True)

    def is_whitelisted(self, login: str) -> bool:
        """供 publisher 判定：该作者是否受白名单保护（永不自动关闭）。"""

        name = login.lower()
        return (
            name in {u.lower() for u in self._config.whitelist.users}
            or name in {b.lower() for b in self._config.whitelist.bot_logins}
            or name.endswith("[bot]")
        )
