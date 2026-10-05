"""统一 LLM 调用封装：Provider 解析、超时、重试、并发限流。

仅通过 AstrBot Provider 抽象调用模型，不实现任何厂商 SDK；
Provider 实例每次调用时现取，支持 AstrBot 侧热切换模型。
"""

import asyncio
import random
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

    from models import ReviewConfig

_LLM_CONCURRENCY = 2


def _retryable_errors() -> tuple[type[Exception], ...]:
    """可重试的网络类错误集合（不强制依赖 aiohttp）。"""

    errors: list[type[Exception]] = [TimeoutError, ConnectionError, OSError]
    try:
        import aiohttp

        errors.append(aiohttp.ClientError)
    except ImportError:
        pass
    return tuple(errors)


_RETRYABLE_ERRORS = _retryable_errors()


class LLMError(Exception):
    """LLM 调用最终失败（重试耗尽或不可重试错误）。"""


class LLMClient:
    """面向 core 层的纯文本对话接口。"""

    def __init__(self, context: "Context", config: "ReviewConfig") -> None:
        self._context = context
        self._config = config
        self._semaphore = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _resolve_provider(self):
        """按优先级解析 Provider：指定 provider_id → 默认 provider。

        默认 Provider 优先使用 AstrBot 的异步接口
        （get_using_provider_async，v4.27+ 推荐），旧版本回退同步接口。

        :raises LLMError: 无可用 Provider 时抛出。
        """

        provider = None
        if self._config.provider_id:
            provider = self._context.get_provider_by_id(self._config.provider_id)
            if provider is None:
                logger.warning(
                    f"[gh-review] 未找到 provider_id="
                    f"{self._config.provider_id}，回退默认 Provider"
                )
        if provider is None:
            get_async = getattr(self._context, "get_using_provider_async", None)
            if callable(get_async):
                provider = await get_async()
            else:
                get_sync = getattr(self._context, "get_using_provider", None)
                provider = get_sync() if callable(get_sync) else None
        if provider is None:
            raise LLMError("无可用 LLM Provider，请在 AstrBot 中接入模型或配置 provider_id")
        return provider

    async def chat(self, system: str, user: str) -> str:
        """发起一次文本对话，返回模型原始文本输出。

        :param system: system prompt（含输出契约）。
        :param user: 已装配并包裹的用户内容。
        :raises LLMError: 超时/网络错误重试耗尽，或 Provider 业务错误。
        """

        async with self._semaphore:
            return await self._chat_with_retry(system, user)

    async def _chat_with_retry(self, system: str, user: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self._config.llm_max_retries):
            try:
                provider = await self._resolve_provider()
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=user, system_prompt=system),
                    timeout=self._config.llm_timeout_seconds,
                )
                text = getattr(resp, "completion_text", None) or ""
                if not text.strip():
                    raise LLMError("LLM 返回空响应")
                return text
            except _RETRYABLE_ERRORS as exc:
                last_error = exc
            except LLMError as exc:
                # 空响应可重试；无 Provider 立即失败
                if "无可用" in str(exc):
                    raise
                last_error = exc
            except Exception as exc:  # Provider 业务异常（配额/鉴权等）不重试
                raise LLMError(f"LLM 调用失败（不可重试）: {exc}") from exc

            delay = min(2**attempt * 2, 30) + random.random()
            logger.warning(
                f"[gh-review] LLM 调用失败，{delay:.1f}s 后重试 "
                f"({attempt + 1}/{self._config.llm_max_retries}): {last_error}"
            )
            await asyncio.sleep(delay)
        raise LLMError(f"LLM 调用重试耗尽: {last_error}")
