"""跨组件共享的数据结构。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Source = Literal["zcode", "codex"]


@dataclass(frozen=True)
class Event:
    source: Source
    key: str
    task_id: str
    title: str
    completed_at_ms: int
    duration_ms: int | None
    summary_text: str
    status: Literal["completed", "error"] = "completed"
    turn_id: str | None = None


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
