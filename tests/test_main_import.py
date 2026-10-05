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
