"""审查引擎：预扫描、Prompt 装配渲染、调用 LLM。"""

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.client import LLMClient
    from models import ReviewConfig
    from security.injection import InjectionFinding, InjectionScanner

    from .context_builder import ReviewContext
    from .prompt_store import PromptStore


class ReviewEngine:
    """按强度档位渲染 Prompt 并调用 LLMClient；预扫描命中时不调用 LLM。"""

    def __init__(
        self,
        llm: "LLMClient",
        prompts: "PromptStore",
        scanner: "InjectionScanner",
        config: "ReviewConfig",
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._scanner = scanner
        self._config = config

    def prescan(self, context: "ReviewContext") -> list["InjectionFinding"]:
        """对送入 Prompt 前的各段不可信内容做注入预扫描。"""

        sections: dict[str, str] = {"pr_meta": context.pr_meta_text}
        for path, content in context.guidelines.items():
            sections[f"guidelines:{path}"] = content
        sections["diff"] = context.diff_text
        return self._scanner.scan_all(sections)

    async def review(self, context: "ReviewContext") -> str:
        """渲染并调用 LLM，返回原始文本输出。"""

        from security.injection import wrap_untrusted

        system = self._prompts.get(f"system_{self._config.level}")
        template = self._prompts.get("user_template")

        guidelines_text = "\n\n".join(
            f"# {path}\n{content}" for path, content in context.guidelines.items()
        ) or "（未找到规范文档，按通用工程规范审查）"

        values: defaultdict[str, str] = defaultdict(str)
        values.update(
            {
                "guidelines": wrap_untrusted("guidelines", guidelines_text),
                "pr_meta": wrap_untrusted("pr_meta", context.pr_meta_text),
                "diff": wrap_untrusted("diff", context.diff_text),
                "level": self._config.level,
            }
        )
        user = template.format_map(values)
        return await self._llm.chat(system, user)
