"""插件配置模型：由 AstrBot 配置 dict 映射而来的 dataclass 集合。"""

from dataclasses import dataclass, field
from typing import Any

REVIEW_LEVELS = ("loose", "normal", "strict")
WHITELIST_ACTIONS = ("skip", "downgrade")
CLOSE_REQUIRES = ("any", "both")


@dataclass
class GitHubConfig:
    """GitHub 接入与轮询配置。

    认证二选一：GitHub App（app_id + private_key，推荐）或 PAT（token）。
    """

    token: str = ""  # PAT，App 字段齐全时忽略
    app_id: int = 0  # GitHub App ID；0 = 未启用 App 模式
    private_key: str = ""  # App 私钥：PEM 原文（可用 \n 转义）或文件路径
    installation_id: int = 0  # 固定安装 ID；0 = 按仓库自动发现
    repositories: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 300
    guideline_files: list[str] = field(default_factory=lambda: ["CONTRIBUTING.md"])
    max_diff_files: int = 30
    max_diff_chars: int = 60_000

    @property
    def auth_mode(self) -> str:
        """认证模式：'app' | 'pat'。"""

        return "app" if self.app_id and self.private_key.strip() else "pat"


@dataclass
class WhitelistConfig:
    """白名单策略配置。"""

    users: list[str] = field(default_factory=list)
    bot_logins: list[str] = field(default_factory=lambda: ["dependabot[bot]"])
    whitelist_action: str = "skip"  # skip | downgrade


@dataclass
class ReviewConfig:
    """审查行为与 LLM 调用配置。"""

    level: str = "normal"  # loose | normal | strict
    provider_id: str = ""  # 空 = 使用当前默认 Provider
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3
    publish_review: bool = True


@dataclass
class SecurityConfig:
    """提示词注入安全策略配置。"""

    auto_close_on_injection: bool = True
    close_requires: str = "any"  # any | both
    label_on_injection: str = "prompt-injection"
    close_comment_template: str = ""


@dataclass
class PluginConfig:
    """插件顶层配置，聚合各子配置。"""

    github: GitHubConfig
    whitelist: WhitelistConfig
    review: ReviewConfig
    security: SecurityConfig

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PluginConfig":
        """从 AstrBot 配置 dict 构建配置对象，缺省字段回落到默认值。

        :param raw: AstrBot 传入的配置字典（与 _conf_schema.json 对应）。
        :raises ValueError: 必填字段缺失或取值非法时抛出。
        """

        def section(name: str) -> dict[str, Any]:
            value = raw.get(name)
            return value if isinstance(value, dict) else {}

        gh_raw = section("github")
        wl_raw = section("whitelist")
        rv_raw = section("review")
        sc_raw = section("security")

        token = str(gh_raw.get("token", "")).strip()
        app_id = _as_int(gh_raw.get("app_id"), "github.app_id")
        private_key = str(gh_raw.get("private_key", "")).strip()
        installation_id = _as_int(
            gh_raw.get("installation_id"), "github.installation_id"
        )
        if bool(app_id) != bool(private_key):
            raise ValueError("github.app_id 与 github.private_key 必须同时配置")
        if not app_id and not token:
            raise ValueError(
                "未配置 GitHub 认证：请填写 App ID + 私钥（推荐）或 token"
            )

        repositories = _as_str_list(gh_raw.get("repositories"))
        if not repositories:
            raise ValueError("github.repositories 至少需要一个 owner/repo")
        for repo in repositories:
            if repo.count("/") != 1:
                raise ValueError(f"非法仓库标识: {repo}（应为 owner/repo）")

        poll_interval = int(gh_raw.get("poll_interval_seconds", 300))
        poll_interval = max(poll_interval, 60)

        level = str(rv_raw.get("level", "normal"))
        if level not in REVIEW_LEVELS:
            raise ValueError(f"review.level 必须是 {REVIEW_LEVELS} 之一")

        wl_action = str(wl_raw.get("whitelist_action", "skip"))
        if wl_action not in WHITELIST_ACTIONS:
            raise ValueError(f"whitelist.whitelist_action 必须是 {WHITELIST_ACTIONS} 之一")

        close_requires = str(sc_raw.get("close_requires", "any"))
        if close_requires not in CLOSE_REQUIRES:
            raise ValueError(f"security.close_requires 必须是 {CLOSE_REQUIRES} 之一")

        return cls(
            github=GitHubConfig(
                token=token,
                app_id=app_id,
                private_key=private_key,
                installation_id=installation_id,
                repositories=repositories,
                poll_interval_seconds=poll_interval,
                guideline_files=_as_str_list(gh_raw.get("guideline_files"))
                or ["CONTRIBUTING.md"],
                max_diff_files=int(gh_raw.get("max_diff_files", 30)),
                max_diff_chars=int(gh_raw.get("max_diff_chars", 60_000)),
            ),
            whitelist=WhitelistConfig(
                users=_as_str_list(wl_raw.get("users")),
                bot_logins=_as_str_list(wl_raw.get("bot_logins"))
                or ["dependabot[bot]"],
                whitelist_action=wl_action,
            ),
            review=ReviewConfig(
                level=level,
                provider_id=str(rv_raw.get("provider_id", "")).strip(),
                llm_timeout_seconds=int(rv_raw.get("llm_timeout_seconds", 120)),
                llm_max_retries=max(1, int(rv_raw.get("llm_max_retries", 3))),
                publish_review=bool(rv_raw.get("publish_review", True)),
            ),
            security=SecurityConfig(
                auto_close_on_injection=bool(
                    sc_raw.get("auto_close_on_injection", True)
                ),
                close_requires=close_requires,
                label_on_injection=str(
                    sc_raw.get("label_on_injection", "prompt-injection")
                ).strip(),
                close_comment_template=str(
                    sc_raw.get("close_comment_template", "")
                ).strip(),
            ),
        )


def _as_int(value: Any, field_name: str) -> int:
    """将配置值规整为非负整数，兼容数字字符串写法。"""

    if value in (None, ""):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是整数") from None
    return max(result, 0)


def _as_str_list(value: Any) -> list[str]:
    """将配置值规整为字符串列表，兼容逗号分隔字符串写法。"""

    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
