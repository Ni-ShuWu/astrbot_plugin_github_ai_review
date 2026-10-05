"""GitHub 认证构建与启动权限自检。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

from github.auth import GitHubAppAuth, StaticTokenAuth

if TYPE_CHECKING:
    import aiohttp

    from core.publisher import Publisher
    from github.client import GitHubClient
    from models.config import GitHubConfig

Auth = StaticTokenAuth | GitHubAppAuth


def build_auth(gh_cfg: GitHubConfig, session: aiohttp.ClientSession) -> Auth:
    """按配置构建认证策略；私钥非法时抛 GitHubAppAuthError。"""

    if gh_cfg.auth_mode == "app":
        key = GitHubAppAuth.load_private_key(gh_cfg.private_key)
        return GitHubAppAuth(
            gh_cfg.app_id,
            key,
            session,
            gh_cfg.installation_id or None,
        )
    return StaticTokenAuth(gh_cfg.token)


async def selfcheck(
    gh_cfg: GitHubConfig,
    gh: GitHubClient,
    auth: Auth,
    publisher: Publisher,
) -> None:
    """逐仓库权限自检：无写权限时降级关闭能力。

    App 模式查安装权限（pull_requests/issues: write）；
    PAT 模式查仓库 push/admin 权限。
    """

    app_mode = gh_cfg.auth_mode == "app"
    for repo in gh_cfg.repositories:
        try:
            if app_mode:
                assert isinstance(auth, GitHubAppAuth)
                perms = await auth.permissions_for(repo)
                writable = (
                    perms.get("pull_requests") == "write"
                    and perms.get("issues") == "write"
                )
            else:
                repo_perms = await gh.check_permissions(repo)
                writable = bool(repo_perms & {"push", "admin"})
        except Exception as exc:  # noqa: BLE001 - 自检失败不阻断启动
            logger.warning(f"[gh-review] 权限自检失败 {repo}: {exc}")
            continue
        if not writable:
            publisher.can_close = False
            logger.warning(
                f"[gh-review] {gh_cfg.auth_mode} 认证对 {repo} "
                "无写权限，自动关闭与 Review 能力降级为仅评论"
            )
