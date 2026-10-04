"""提示词注入防护：预扫描、定界包裹、伪造定界符中和。

双通道设计：
- 预扫描（本模块）：送入 LLM 前的正则检测，命中即阻断审查流程。
- LLM 通道：模型自行识别后回传 prompt_injection_suspected 标记。
"""

import re
from dataclasses import dataclass

UNTRUSTED_BEGIN = "<<<UNTRUSTED_BEGIN"
UNTRUSTED_END = "<<<UNTRUSTED_END>>>"

# 注入模式库：(模式名, 正则)。不区分大小写，覆盖中英常见写法。
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.DOTALL))
    for name, pattern in (
        (
            "override_instructions",
            (
                r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+"
                r"(instructions?|prompts?|rules?|directions?)"
            ),
        ),
        (
            "new_instructions",
            (
                r"(from\s+now\s+on|henceforth)\s*,?\s*(you\s+(are|will|must)|"
                r"follow\s+these\s+instructions?)"
            ),
        ),
        (
            "role_hijack",
            (
                r"(you\s+are\s+now|act\s+as|pretend\s+to\s+be|assume\s+the\s+role)\s+"
                r"(?:an?\s+)?(?!code\s+reviewer)[\w\s]{0,20}?"
                r"(assistant|bot|ai|system|admin)\b"
            ),
        ),
        (
            "system_prompt_probe",
            (
                r"(reveal|print|show|output|repeat|disclose)\s+(your|the)\s+"
                r"(system\s+)?(prompt|instructions?|rules?)"
            ),
        ),
        (
            "delimiter_escape",
            r"<<<\s*/?\s*UNTRUSTED_(BEGIN|END)",
        ),
        (
            "authority_claim",
            (
                r"(as\s+the\s+(repo\s+)?(owner|maintainer)|i\s+am\s+the\s+"
                r"(owner|maintainer|admin))\s*,?\s*(approve|merge|close|skip)"
            ),
        ),
        (
            "auto_action_lure",
            (
                r"(approve|merge|lgtm)\s+(this|the)\s+(pr|pull\s+request)\s+"
                r"(immediately|automatically|without\s+(review|checking))"
            ),
        ),
        (
            # 须为明确指令语境：忽略/无视 + 范围量词 + 指令类宾语；
            # 不含「跳过」「提示」「要求」等正常叙述高频词。
            "cn_override",
            (
                r"(忽略|无视)(之前|以上|前面|先前|上述)(的)?"
                r"(所有|全部|一切)(指令|提示词|规则)"
            ),
        ),
        (
            # 须为直接的角色指派祈使句；不含「假设你是」「如果你是」等假设语境。
            "cn_role_hijack",
            (
                r"(你现在是|请你?扮演|请你?充当|从现在开始你是?)"
                r"(一个|一名)?\w{0,10}(助手|机器人|模型|管理员|审查员)"
            ),
        ),
        (
            # 须为免审查的批准指令；「自动」一词单独出现属正常叙述，不命中。
            "cn_auto_action",
            (
                r"((直接|立即)(通过|批准|合并)(此|该|本)(PR|拉取请求|合并请求)"
                r"|(无需|不需要|不用)(进行?)?(审查|审核|检查)[,，]?"
                r"(直接|立即)?(通过|批准|合并))"
            ),
        ),
    )
)


@dataclass(frozen=True)
class InjectionFinding:
    """一条注入命中记录。"""

    pattern_name: str
    matched_text: str  # 截断留证，最长 80 字符
    source: str  # 命中来源：guidelines / pr_meta / diff


class InjectionScanner:
    """注入预扫描器：对送入 Prompt 的各段不可信内容做正则检测。"""

    def scan(self, source: str, content: str) -> list[InjectionFinding]:
        """扫描单段内容，返回全部命中（空列表 = 干净）。

        :param source: 内容来源标识，用于留证与日志。
        :param content: 待扫描文本。
        """

        findings: list[InjectionFinding] = []
        for name, pattern in _PATTERNS:
            match = pattern.search(content)
            if match:
                findings.append(
                    InjectionFinding(
                        pattern_name=name,
                        matched_text=match.group(0)[:80],
                        source=source,
                    )
                )
        return findings

    def scan_all(self, sections: dict[str, str]) -> list[InjectionFinding]:
        """扫描多段内容（如 guidelines/pr_meta/diff），聚合并命中。"""

        findings: list[InjectionFinding] = []
        for source, content in sections.items():
            findings.extend(self.scan(source, content))
        return findings


def wrap_untrusted(source: str, content: str) -> str:
    """将不可信内容包裹进定界块，并中和内容内的伪造定界符。

    伪造定界符通过插入零宽空格使其失配，既保留可见文本又不构成闭合。
    """

    neutralized = re.sub(
        r"<<<(\s*/?\s*UNTRUSTED_(?:BEGIN|END))",
        "<<<\u200b\\1",
        content,
        flags=re.IGNORECASE,
    )
    return (
        f'{UNTRUSTED_BEGIN} source="{source}">\n'
        f"{neutralized}\n"
        f"{UNTRUSTED_END}"
    )
