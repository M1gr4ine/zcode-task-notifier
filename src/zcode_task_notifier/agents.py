"""通知来源与未适配来源的边界。"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class AgentDescriptor:
    key: str
    prefix: str
    supported: bool


# 占位只声明边界，不注册扫描器，也不改变其他 Agent 的协议或配置。
_AGENTS = MappingProxyType({
    "zcode": AgentDescriptor("zcode", "[zcode]", True),
    "codex": AgentDescriptor("codex", "[codex]", True),
    "claudecode": AgentDescriptor("claudecode", "[claudecode]", False),
    "dsh": AgentDescriptor("dsh", "[dsh]", False),
})


def agent_descriptor(source: str, *, require_supported: bool = True) -> AgentDescriptor:
    descriptor = _AGENTS.get(source) if isinstance(source, str) else None
    if descriptor is None or (require_supported and not descriptor.supported):
        raise ValueError("来源尚未支持")
    return descriptor


def strip_source_prefix(source: str, title: str) -> str:
    prefix = agent_descriptor(source).prefix
    value = str(title).strip()
    while value.casefold().startswith(prefix):
        value = value[len(prefix):].lstrip()
    return value


def display_title(source: str, title: str, fallback: str = "未命名任务") -> str:
    descriptor = agent_descriptor(source)
    value = strip_source_prefix(source, title)
    if not value:
        value = strip_source_prefix(source, fallback) or "未命名任务"
    return f"{descriptor.prefix} {value}"
