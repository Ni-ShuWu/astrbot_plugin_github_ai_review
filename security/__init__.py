"""安全层：提示词注入检测与不可信内容包裹。"""

from .injection import (
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    InjectionFinding,
    InjectionScanner,
    wrap_untrusted,
)

__all__ = [
    "UNTRUSTED_BEGIN",
    "UNTRUSTED_END",
    "InjectionFinding",
    "InjectionScanner",
    "wrap_untrusted",
]
