"""AstrBot GitHub PR AI 审查插件入口。

轮询目标仓库 PR，按仓库规范文档进行 AI 审查并发表 Review；
支持白名单、审查强度调节与提示词注入防护（可自动关闭注入 PR）。
"""

# AstrBot 按包路径导入插件，插件目录不在 sys.path，需先引导再导入本地模块
import sys
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from core.context_builder import ContextBuilder
from core.filter import EventFilter
from core.pipeline import ReviewPipeline
from core.poller import Poller
from core.prompt_store import PromptStore
from core.publisher import Publisher
from core.result_parser import ResultParser
from core.review_engine import ReviewEngine
from core.selfcheck import Auth, build_auth, selfcheck
from core.state_store import CursorStore
from github.auth import GitHubAppAuthError
from github.client import GitHubClient
from llm.client import LLMClient
from models.config import REVIEW_LEVELS, PluginConfig
from security.injection import InjectionScanner

_HELP = """📋 GitHub PR AI 审查
/pr_review status            查看运行状态
/pr_review scan [仓库]        立即扫描
/pr_review recheck <仓库> <PR号>  强制重审
/pr_review level [loose|normal|strict]  查看/设置强度
/pr_review wl <add|del|list> [用户]     白名单管理
/pr_review close_inj [on|off] 注入自动关闭开关
/pr_review reload            重载配置与模板"""


class GitHubAIReview(Star):
    """GitHub PR AI 审查插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        # AstrBot v4.27.5 起 Star.__init__ 不再写入 self.config（配置改由
        # StarMetadata 持有），插件需自行保存构造期传入的配置对象。
        self.config: dict = config if config is not None else {}
        self._session: aiohttp.ClientSession | None = None
        self._poller: Poller | None = None
        self._pipeline: ReviewPipeline | None = None
        self._publisher: Publisher | None = None
        self._prompts: PromptStore | None = None
        self._builder: ContextBuilder | None = None
        self._gh: GitHubClient | None = None
        self._auth: Auth | None = None
        self._cursor: CursorStore | None = None
        self._plugin_config: PluginConfig | None = None

    async def initialize(self) -> None:
        """构建组件、自检权限、启动轮询。"""

        try:
            plugin_config = PluginConfig.from_dict(self.config)
        except ValueError as exc:
            logger.error(f"[gh-review] 配置无效，插件未启动: {exc}")
            return
        self._session = aiohttp.ClientSession()
        data_dir = StarTools.get_data_dir("astrbot_plugin_github_ai_review")
        data_dir.mkdir(parents=True, exist_ok=True)
        self._cursor = CursorStore(data_dir / "seen_prs.json")
        self._prompts = PromptStore(Path(__file__).parent / "prompts")
        try:
            self._build_components(plugin_config)
        except GitHubAppAuthError as exc:
            logger.error(f"[gh-review] GitHub App 认证配置无效，插件未启动: {exc}")
            return
        assert self._gh and self._auth and self._publisher
        await selfcheck(plugin_config.github, self._gh, self._auth, self._publisher)
        assert self._poller is not None
        self._poller.start()
        logger.info(
            f"[gh-review] 已启动：仓库 {plugin_config.github.repositories}，"
            f"间隔 {plugin_config.github.poll_interval_seconds}s，"
            f"强度 {plugin_config.review.level}，"
            f"认证 {plugin_config.github.auth_mode}"
        )

    def _save_config(self) -> None:
        """持久化插件配置；配置对象未必实现 save_config。"""

        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _build_components(self, plugin_config: PluginConfig) -> None:
        """由配置重建全部业务组件（reload 热替换）。"""

        assert self._session and self._cursor and self._prompts
        self._plugin_config = plugin_config
        self._auth = build_auth(plugin_config.github, self._session)
        self._gh = GitHubClient(self._auth, self._session)
        builder = ContextBuilder(self._gh, plugin_config.github)
        if self._builder:  # 复用规范缓存并重绑新认证
            builder.guideline_cache = self._builder.guideline_cache
            builder.guideline_cache.rebind(self._gh)
        self._builder = builder
        llm = LLMClient(self.context, plugin_config.review)
        engine = ReviewEngine(
            llm, self._prompts, InjectionScanner(), plugin_config.review
        )
        self._publisher = Publisher(self._gh, plugin_config)
        self._pipeline = ReviewPipeline(
            plugin_config,
            self._gh,
            EventFilter(plugin_config),
            builder,
            engine,
            ResultParser(),
            self._publisher,
            self._cursor,
        )
        old_poller = self._poller
        self._poller = Poller(
            self._gh,
            plugin_config.github,
            self._cursor,
            self._pipeline.handle,
        )
        self._poller_restart_needed = old_poller is not None

    async def terminate(self) -> None:
        """停止轮询并释放会话。"""

        if self._poller:
            await self._poller.stop()
        if self._session:
            await self._session.close()

    async def _reload(self) -> str:
        """重载配置与模板，返回结果描述。"""

        try:
            plugin_config = PluginConfig.from_dict(self.config)
        except ValueError as exc:
            return f"❌ 配置无效: {exc}"
        try:
            self._build_components(plugin_config)
        except GitHubAppAuthError as exc:
            return f"❌ GitHub App 认证配置无效: {exc}"
        self._prompts.reload()
        self._builder.guideline_cache.clear()
        if getattr(self, "_poller_restart_needed", False):
            await self._poller.stop()
        self._poller.start()
        await selfcheck(plugin_config.github, self._gh, self._auth, self._publisher)
        return "✅ 已重载配置、Prompt 模板与规范缓存"

    @filter.command_group("pr_review")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def pr_review(self):
        """GitHub PR AI 审查管理指令组。"""

    @pr_review.command("help")
    async def help_cmd(self, event: AstrMessageEvent):
        """显示帮助。"""
        yield event.plain_result(_HELP)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看运行状态。"""
        if not self._plugin_config:
            yield event.plain_result("❌ 插件未启动（配置无效），请检查配置")
            return
        cfg = self._plugin_config
        provider = cfg.review.provider_id or "（默认模型）"
        lines = [
            "📊 GitHub PR AI 审查状态",
            f"仓库: {', '.join(cfg.github.repositories)}",
            f"轮询间隔: {cfg.github.poll_interval_seconds}s",
            f"审查强度: {cfg.review.level}",
            f"Provider: {provider}",
            f"已审游标: {len(self._cursor)} 条",
            (
                f"注入自动关闭: "
                f"{'开' if cfg.security.auto_close_on_injection else '关'}"
                f"（{cfg.security.close_requires}）"
            ),
            f"写权限: {'正常' if self._publisher.can_close else '缺失（已降级）'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("scan")
    async def scan(self, event: AstrMessageEvent, repo: str = ""):
        """立即扫描全部或指定仓库。"""
        if not self._poller:
            yield event.plain_result("❌ 插件未启动")
            return
        target = repo.strip() or None
        count = await self._poller.scan_once(target)
        yield event.plain_result(f"✅ 扫描完成，触发 {count} 个审查任务")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("recheck")
    async def recheck(self, event: AstrMessageEvent, repo: str = "", number: str = ""):
        """强制重审指定 PR（忽略游标）。"""
        if not repo or not number.isdigit():
            yield event.plain_result(
                "用法: /pr_review recheck <owner/repo> <PR号>"
            )
            return
        assert self._gh and self._pipeline
        try:
            prs = await self._gh.list_open_prs(repo.strip())
        except Exception as exc:  # noqa: BLE001 - 指令级错误回聊报告
            yield event.plain_result(f"❌ GitHub 请求失败: {exc}")
            return
        target = next((p for p in prs if p.number == int(number)), None)
        if target is None:
            yield event.plain_result(f"❌ 未找到开放中的 PR {repo}#{number}")
            return
        yield event.plain_result(f"🔍 已触发重审 {repo}#{number}")
        try:
            await self._pipeline.handle(target)
        except Exception as exc:  # noqa: BLE001 - 单 PR 失败隔离
            logger.exception(f"[gh-review] recheck {repo}#{number} 失败")
            yield event.plain_result(f"❌ 审查过程出错: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("level")
    async def level(self, event: AstrMessageEvent, value: str = ""):
        """查看或设置审查强度。"""
        value = value.strip().lower()
        if not value:
            yield event.plain_result(
                f"当前强度: {self._plugin_config.review.level}，"
                f"可选: {'/'.join(REVIEW_LEVELS)}"
            )
            return
        if value not in REVIEW_LEVELS:
            yield event.plain_result(
                f"❌ 非法强度，可选: {'/'.join(REVIEW_LEVELS)}"
            )
            return
        self.config["review"]["level"] = value
        self._save_config()
        await self._reload()
        yield event.plain_result(f"✅ 审查强度已切换为 {value}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("wl")
    async def whitelist(self, event: AstrMessageEvent, action: str = "", user: str = ""):
        """管理用户白名单。"""
        wl = self.config.setdefault("whitelist", {})
        users = wl.setdefault("users", [])
        action = action.strip().lower()
        user = user.strip()
        if action == "list" or not action:
            yield event.plain_result(
                "白名单用户: " + (", ".join(users) if users else "（空）")
            )
            return
        if not user:
            yield event.plain_result("用法: /pr_review wl <add|del|list> [用户名]")
            return
        if action == "add" and user not in users:
            users.append(user)
        elif action == "del" and user in users:
            users.remove(user)
        elif action not in ("add", "del"):
            yield event.plain_result("❌ 未知操作，支持 add/del/list")
            return
        self._save_config()
        await self._reload()
        yield event.plain_result(f"✅ 白名单: {', '.join(users) or '（空）'}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("close_inj")
    async def close_inj(self, event: AstrMessageEvent, value: str = ""):
        """开关注入自动关闭。"""
        value = value.strip().lower()
        if value not in ("on", "off"):
            cur = self._plugin_config.security.auto_close_on_injection
            yield event.plain_result(
                f"注入自动关闭: {'开' if cur else '关'}（/pr_review close_inj on|off）"
            )
            return
        self.config["security"]["auto_close_on_injection"] = value == "on"
        self._save_config()
        await self._reload()
        yield event.plain_result(
            f"✅ 注入自动关闭已{'开启' if value == 'on' else '关闭'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pr_review.command("reload")
    async def reload(self, event: AstrMessageEvent):
        """重载配置与 Prompt 模板。"""
        yield event.plain_result(await self._reload())
