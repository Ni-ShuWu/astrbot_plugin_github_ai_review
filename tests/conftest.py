"""测试基础设施：astrbot 依赖桩与公共 fixture。

插件在 AstrBot 运行时内运行，测试环境无 astrbot 包，
此处以最小桩模块满足 import（仅 logger 被实际使用）。
"""

import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return

    class _CommandGroup:
        """filter.command_group 返回的指令组桩，支持 .command 子装饰器。"""

        def __init__(self, func):
            self.func = func

        def command(self, name: str):
            return lambda fn: fn

    class _FilterStub:
        PermissionType = types.SimpleNamespace(ADMIN="admin", MEMBER="member")

        @staticmethod
        def command_group(name: str):
            return lambda fn: _CommandGroup(fn)

        @staticmethod
        def permission_type(perm):
            # 模拟 AstrBot 真实实现：读取被装饰对象 __name__（RegisteringCommandable 无此属性）
            def deco(fn):
                _ = fn.__name__
                return fn

            return deco

    class _Star:
        def __init__(self, context=None, config=None):
            # 模拟 AstrBot v4.27.5：Star.__init__ 不写 self.config，
            # 插件必须自行保存构造期传入的配置
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(name: str) -> Path:
            return Path(name)

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("gh-review-test")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = _FilterStub()
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = _Star
    star.StarTools = _StarTools
    astrbot.api = api
    api.event = event
    api.star = star
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star


_install_astrbot_stub()

import pytest

from github.models import PullRequestEvent, PullRequestEventType
from models import PluginConfig


@pytest.fixture()
def config_dict() -> dict:
    """最小合法配置字典。"""

    return {
        "github": {"token": "t", "repositories": ["octo/demo"]},
        "whitelist": {},
        "review": {},
        "security": {},
    }


@pytest.fixture()
def config(config_dict) -> PluginConfig:
    return PluginConfig.from_dict(config_dict)


def make_event(
    *,
    repo: str = "octo/demo",
    number: int = 42,
    author: str = "alice",
    head_sha: str = "abc123",
    title: str = "Fix bug",
    body: str = "修复空指针",
) -> PullRequestEvent:
    """构造测试用 PR 事件。"""

    return PullRequestEvent(
        repo=repo,
        number=number,
        title=title,
        body=body,
        author_login=author,
        head_sha=head_sha,
        base_branch="main",
        event_type=PullRequestEventType.OPENED,
        html_url=f"https://github.com/{repo}/pull/{number}",
    )


@pytest.fixture()
def event() -> PullRequestEvent:
    return make_event()
