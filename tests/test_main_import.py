"""main.py 模块级导入冒烟测试。

回归场景：REVIEW_LEVELS 未从 models 包导出导致 AstrBot 运行时
ImportError——此前测试从不导入 main.py，覆盖不到模块级代码。
"""

import importlib


def test_main_module_imports():
    module = importlib.import_module("main")
    assert hasattr(module, "GitHubAIReview")


def test_review_levels_exported():
    from models import REVIEW_LEVELS

    assert set(REVIEW_LEVELS) == {"loose", "normal", "strict"}


def test_plugin_class_instantiates():
    import main

    plugin = main.GitHubAIReview(context=None, config={})
    assert plugin._poller is None


def test_plugin_saves_config_dropped_by_star_init():
    """回归：AstrBot v4.27.5 的 Star.__init__ 丢弃 config 参数，
    插件需自行保存，否则 initialize 时 'GitHubAIReview' object has no
    attribute 'config'。"""

    import main

    class _Probe(main.Star):
        def __init__(self, config=None):
            super().__init__(context=None, config=config)

    assert not hasattr(_Probe(), "config"), "模拟的 Star 不应写入 self.config"

    cfg = {"github": {"repositories": ["o/r"]}}
    plugin = main.GitHubAIReview(context=None, config=cfg)
    assert plugin.config is cfg


async def test_initialize_with_invalid_config_returns_gracefully():
    """配置无效时 initialize 记录日志后返回，不抛出异常。"""

    import main

    plugin = main.GitHubAIReview(context=None, config={})
    assert await plugin.initialize() is None


def test_save_config_tolerates_plain_dict():
    import main

    plugin = main.GitHubAIReview(context=None, config={})
    plugin._save_config()


def test_save_config_delegates_to_config_object():
    import main

    class _Cfg(dict):
        saved = 0

        def save_config(self):
            self.saved += 1

    cfg = _Cfg()
    plugin = main.GitHubAIReview(context=None, config=cfg)
    plugin._save_config()
    assert cfg.saved == 1


def test_plugin_config_falls_back_to_empty_dict():
    import main

    plugin = main.GitHubAIReview(context=None)
    assert plugin.config == {}


def test_plugin_config_is_keyword_constructible():
    """AstrBot 以关键字参数实例化插件类。"""

    import main

    plugin = main.GitHubAIReview(context=None, config={"review": {"level": "strict"}})
    assert plugin.config["review"]["level"] == "strict"
