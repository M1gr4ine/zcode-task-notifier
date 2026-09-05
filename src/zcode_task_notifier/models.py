"""跨组件共享的数据结构。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Source = Literal["zcode", "codex"]
EventStatus = Literal[
    "completed",
    "error",
    "awaiting_approval",
    "awaiting_input",
]


@dataclass(frozen=True)
class Event:
    source: Source
    key: str
    task_id: str
    title: str
    completed_at_ms: int
    duration_ms: int | None
    summary_text: str
    status: EventStatus = "completed"
    turn_id: str | None = None
    # 停顿分类元数据只保存稳定原因/指纹，不保存完整用户输入。
    stop_reason: str | None = None
    plan_fingerprint: str | None = None
    # task_id 是公开事件主键；以下字段用于来源适配器保留原始关联，旧事件为空。
    agent_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class TurnContext:
    """跨扫描保留的最小回合证据，不包含用户输入正文。"""

    source: Source
    task_id: str
    turn_id: str
    # None 表示旧格式/尚未观察到 user 记录，不能与“明确规则输入”混同。
    has_user_task: bool | None = None
    input_fingerprint: str | None = None
    plan_fingerprint: str | None = None
    status: str | None = None
    active: bool = True
    updated_at_ms: int = 0
    # 仅保留 request_user_input 的不透明 call_id，跨扫描识别已回答调用。
    pending_input_call_id: str | None = None


@dataclass(frozen=True)
class DiscoveredPaths:
    zcode_home: Path
    tasks_db: Path
    zcode_logs: Path
    bot_config: Path
    bot_state: Path
    credentials: Path
    notification_workspace: Path
    zcode_rollout_dir: Path | None = None
    codex_home: Path | None = None
    codex_state_db: Path | None = None
    codex_history_db: Path | None = None


@dataclass
class OutboxItem:
    event: Event
    automation_id: str | None = None
    # 旧状态文件曾保存 attempt/next_attempt_at_ms；字段暂保留为零值兼容
    # 内存构造，但当前通知器不会读取或推进它们。
    attempt: int = 0
    next_attempt_at_ms: int = 0
    status: Literal["pending", "submitted"] = "pending"
    # 旧状态没有该字段时由 state schema 恢复为 None；只有成功投递后设置。
    submitted_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pending", "submitted"}:
            raise ValueError("outbox status 无效")


@dataclass
class RuntimeState:
    schema_version: int = 1
    initialized: bool = False
    seen_event_keys: set[str] = field(default_factory=set)
    zcode_rollout_offsets: dict[str, int] = field(default_factory=dict)
    zcode_last_turns: dict[str, str] = field(default_factory=dict)
    rollout_offsets: dict[str, int] = field(default_factory=dict)
    rollout_turn_started_ms: dict[str, int] = field(default_factory=dict)
    outbox: dict[str, OutboxItem] = field(default_factory=dict)
    # 按来源记录是否已经完成静默基线；旧状态由 state schema 迁移填充。
    # 放在末尾以保留旧版本 RuntimeState 的位置参数兼容性。
    source_initialized: dict[str, bool] = field(default_factory=dict)
    # 与字节游标配套的稳定文件身份；旧状态没有这些字段时按旧游标兼容。
    zcode_rollout_identities: dict[str, str] = field(default_factory=dict)
    rollout_identities: dict[str, str] = field(default_factory=dict)
    # source:task:turn -> minimal input/stop evidence; unknown old states default empty.
    turn_contexts: dict[str, TurnContext] = field(default_factory=dict)
