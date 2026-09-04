"""将旧版快照中的终态身份最小迁移到当前运行态。

迁移只读取 ``tasks`` 与 ``codex_turns`` 两个映射，并把已完成身份加入
``seen_event_keys``。旧快照中的路径、提示词、投递目标和日志等字段不会被
复制到新状态。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .models import RuntimeState


_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "done",
        "success",
        "succeeded",
        "error",
        "failed",
        "failure",
        "cancelled",
        "canceled",
        "exhausted",
    }
)

_MAX_IDENTITY_COMPONENT_LENGTH = 128
_IDENTITY_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_IDENTITY_MARKERS = (
    "credential",
    "token",
    "secret",
    "password",
    "passwd",
    "enc:v1",
    "control",
)


@dataclass(frozen=True)
class MigrationError:
    """不包含原始路径或身份值的可收集迁移错误。"""

    code: str
    source: str


@dataclass(frozen=True)
class MigrationReport:
    """兼容旧返回值之外的结构化迁移结果。"""

    state: RuntimeState
    errors: tuple[MigrationError, ...] = ()


def _is_terminal(record: Any) -> bool:
    if isinstance(record, str):
        return record.strip().casefold() in _TERMINAL_STATUSES
    if isinstance(record, Mapping):
        for key in ("status", "state", "outcome"):
            value = record.get(key)
            if isinstance(value, str) and value.strip().casefold() in _TERMINAL_STATUSES:
                return True
    return False


def _safe_identity_component(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    identity = value.strip()
    if not identity or len(identity) > _MAX_IDENTITY_COMPONENT_LENGTH:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in identity):
        return None
    if any(marker in identity.casefold() for marker in _FORBIDDEN_IDENTITY_MARKERS):
        return None
    if _IDENTITY_COMPONENT_RE.fullmatch(identity) is None:
        return None
    return identity


def _mapping_records(
    payload: Any,
    source: str,
    errors: list[MigrationError],
) -> list[tuple[str, Any]] | None:
    if not isinstance(payload, Mapping):
        errors.append(MigrationError("invalid_shape", source))
        return None
    result: list[tuple[str, Any]] = []
    for raw_key, value in payload.items():
        if source == "codex_turns":
            components = _codex_identity_components(raw_key)
            key = ":".join(components) if components is not None else None
        else:
            key = _safe_identity_component(raw_key)
        if key is None:
            errors.append(MigrationError("unsafe_identity", source))
            return None
        result.append((key, value))
    return result


def _codex_identity_components(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    thread_id = _safe_identity_component(parts[0])
    turn_id = _safe_identity_component(parts[1])
    if thread_id is None or turn_id is None:
        return None
    return thread_id, turn_id


def import_legacy_snapshot_report(
    legacy_path: Path, current: RuntimeState
) -> MigrationReport:
    """安全地把旧快照终态转换为 seen 基线并返回结构化错误。

    快照不存在、无法解析或结构不符合约定时返回原状态，不会清空或部分
    改写当前状态。成功迁移时只增加两个命名空间下的事件键。错误只包含
    稳定类别和来源，不包含快照路径、身份值或旧记录正文。
    """
    if not isinstance(current, RuntimeState):
        raise TypeError("current 必须是 RuntimeState")
    errors: list[MigrationError] = []
    try:
        raw = Path(legacy_path).read_text(encoding="utf-8")
        payload: Any = json.loads(raw)
    except OSError:
        errors.append(MigrationError("unreadable", "snapshot"))
        return MigrationReport(current, tuple(errors))
    except UnicodeError:
        errors.append(MigrationError("invalid_encoding", "snapshot"))
        return MigrationReport(current, tuple(errors))
    except (json.JSONDecodeError, TypeError, ValueError):
        errors.append(MigrationError("invalid_json", "snapshot"))
        return MigrationReport(current, tuple(errors))
    if not isinstance(payload, Mapping):
        errors.append(MigrationError("invalid_shape", "snapshot"))
        return MigrationReport(current, tuple(errors))

    tasks = payload.get("tasks")
    codex_turns = payload.get("codex_turns")
    task_records = (
        _mapping_records(tasks, "tasks", errors) if tasks is not None else []
    )
    codex_records = (
        _mapping_records(codex_turns, "codex_turns", errors)
        if codex_turns is not None
        else []
    )
    # 任一映射结构或身份不安全时全量保持原状态，避免部分迁移。
    if errors or task_records is None or codex_records is None:
        return MigrationReport(current, tuple(errors))

    new_keys: set[str] = set()
    for identity, record in task_records:
        if _is_terminal(record):
            new_keys.add(f"legacy-zcode:{identity}:completed")
    for identity, record in codex_records:
        if _is_terminal(record):
            components = _codex_identity_components(identity)
            if components is None:
                errors.append(MigrationError("unsafe_identity", "codex_turns"))
                continue
            thread_id, turn_id = components
            new_keys.add(f"legacy-codex:{thread_id}:{turn_id}")
    if errors:
        return MigrationReport(current, tuple(errors))
    current.seen_event_keys.update(new_keys)
    return MigrationReport(current, ())


def import_legacy_snapshot(
    legacy_path: Path,
    current: RuntimeState,
    errors: list[MigrationError] | None = None,
) -> RuntimeState:
    """兼容旧接口；可选 ``errors`` 收集受控迁移诊断。"""
    report = import_legacy_snapshot_report(legacy_path, current)
    if errors is not None:
        if not isinstance(errors, list):
            raise TypeError("errors 必须是 list")
        errors.extend(report.errors)
    return report.state


# 让调用方可按“with report”语义读取而不破坏既有导入名。
import_legacy_snapshot_with_report = import_legacy_snapshot_report


__all__ = [
    "MigrationError",
    "MigrationReport",
    "import_legacy_snapshot",
    "import_legacy_snapshot_report",
    "import_legacy_snapshot_with_report",
]
