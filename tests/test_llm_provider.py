"""LLMClient Provider 解析测试。

AstrBot v4.27+ 将 get_using_provider() 标记为过时并新增
get_using_provider_async()；插件需优先走异步接口，同时兼容旧版同步接口。
"""

import pytest

from llm.client import LLMClient, LLMError
from models.config import ReviewConfig


class _Provider:
    def __init__(self, model: str = "m") -> None:
        self.model = model


class _AsyncContext:
    """模拟 v4.27+ Context：同步接口已废弃，仍保留以防调用。"""

    def __init__(self, provider, by_id=None) -> None:
        self._provider = provider
        self._by_id = by_id or {}
        self.sync_called = False

    def get_provider_by_id(self, provider_id):
        return self._by_id.get(provider_id)

    async def get_using_provider_async(self):
        return self._provider

    def get_using_provider(self):
        self.sync_called = True
        return self._provider


class _LegacyContext:
    """模拟旧版 Context：只有同步接口。"""

    def __init__(self, provider) -> None:
        self._provider = provider

    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self):
        return self._provider


def _client(context, provider_id: str = "") -> LLMClient:
    return LLMClient(context, ReviewConfig(provider_id=provider_id))


async def test_resolve_prefers_async_interface():
    provider = _Provider()
    context = _AsyncContext(provider)
    resolved = await _client(context)._resolve_provider()
    assert resolved is provider
    assert context.sync_called is False, "不应调用已废弃的同步接口"


async def test_resolve_falls_back_to_legacy_sync_interface():
    provider = _Provider()
    resolved = await _client(_LegacyContext(provider))._resolve_provider()
    assert resolved is provider


async def test_resolve_prefers_configured_provider_id():
    chosen = _Provider("configured")
    context = _AsyncContext(_Provider("default"), by_id={"pid": chosen})
    resolved = await _client(context, provider_id="pid")._resolve_provider()
    assert resolved is chosen


async def test_resolve_raises_without_provider():
    with pytest.raises(LLMError, match="无可用 LLM Provider"):
        await _client(_AsyncContext(None))._resolve_provider()
