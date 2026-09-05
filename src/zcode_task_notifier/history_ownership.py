"""通知历史的最小归属账本。

账本只保存能够重新定位通知自动化的稳定字段，不保存标题、摘要、路径或
其他来源正文。它与运行时 outbox 分开保存，使 outbox 过期后仍能执行历史
保留策略。
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import uuid

from .models import Event, OutboxItem
from .notifier import automation_id


HISTORY_OWNERSHIP_FILENAME = "history-ownership.json"
_SCHEMA_VERSION = 1
_TERMINAL_STATUSES = frozenset({"completed", "error"})
_SOURCES = frozenset({"zcode", "codex"})
_ENTRY_FIELDS = frozenset(
    {
        "automation_id",
        "event_key",
        "source",
        "task_id",
        "status",
        "completed_at_ms",
    }
)


class HistoryOwnershipError(RuntimeError):
    """历史归属账本损坏、不兼容或无法安全持久化。"""


def _record_from_item(
    item: object,
    *,
    mapping_key: object | None = None,
) -> tuple[str, str, str, str, str, int] | None:
    """提取并校验可写入账本的六个稳定字段；不合格输入直接丢弃。"""
    if not isinstance(item, OutboxItem) or item.status != "submitted":
        return None
    event = item.event
    if not isinstance(event, Event):
        return None
    if mapping_key is not None and mapping_key != event.key:
        return None
    if (
        not isinstance(event.source, str)
        or event.source not in _SOURCES
        or not isinstance(event.key, str)
        or not event.key
        or not isinstance(event.task_id, str)
        or not event.task_id
        or not isinstance(event.status, str)
        or event.status not in _TERMINAL_STATUSES
        or not isinstance(event.completed_at_ms, int)
        or isinstance(event.completed_at_ms, bool)
        or event.completed_at_ms < 0
        or not isinstance(item.automation_id, str)
        or not item.automation_id
    ):
        return None
    try:
        expected_id = automation_id(event.key)
    except (TypeError, ValueError):
        return None
    if item.automation_id != expected_id:
        return None
    return (
        item.automation_id,
        event.key,
        event.source,
        event.task_id,
        event.status,
        event.completed_at_ms,
    )


def _record_to_item(record: tuple[str, str, str, str, str, int]) -> OutboxItem:
    automation_value, event_key, source, task_id, status, completed_at_ms = record
    event = Event(
        source=source,  # type: ignore[arg-type]
        key=event_key,
        task_id=task_id,
        title="",
        completed_at_ms=completed_at_ms,
        duration_ms=None,
        summary_text="",
        status=status,  # type: ignore[arg-type]
    )
    return OutboxItem(
        event=event,
        automation_id=automation_value,
        status="submitted",
        submitted_at_ms=completed_at_ms,
    )


def _parse_record(value: object) -> tuple[str, str, str, str, str, int]:
    if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
        raise HistoryOwnershipError("历史归属账本条目字段不兼容")
    automation_value = value["automation_id"]
    event_key = value["event_key"]
    source = value["source"]
    task_id = value["task_id"]
    status = value["status"]
    completed_at_ms = value["completed_at_ms"]
    if (
        not isinstance(automation_value, str)
        or not automation_value
        or not isinstance(event_key, str)
        or not event_key
        or not isinstance(source, str)
        or source not in _SOURCES
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(status, str)
        or status not in _TERMINAL_STATUSES
        or not isinstance(completed_at_ms, int)
        or isinstance(completed_at_ms, bool)
        or completed_at_ms < 0
    ):
        raise HistoryOwnershipError("历史归属账本条目值无效")
    try:
        expected_id = automation_id(event_key)
    except (TypeError, ValueError) as exc:
        raise HistoryOwnershipError("历史归属账本事件键无效") from exc
    if automation_value != expected_id:
        raise HistoryOwnershipError("历史归属账本自动化归属不匹配")
    return (
        automation_value,
        event_key,
        source,
        task_id,
        status,
        completed_at_ms,
    )


def _item_record(item: object, *, mapping_key: object | None = None) -> tuple[str, str, str, str, str, int] | None:
    return _record_from_item(item, mapping_key=mapping_key)


def _record_payload(record: tuple[str, str, str, str, str, int]) -> dict[str, object]:
    automation_value, event_key, source, task_id, status, completed_at_ms = record
    return {
        "automation_id": automation_value,
        "event_key": event_key,
        "source": source,
        "task_id": task_id,
        "status": status,
        "completed_at_ms": completed_at_ms,
    }


def _valid_records(
    ownership: Mapping[str, OutboxItem],
) -> dict[str, tuple[str, str, str, str, str, int]]:
    records: dict[str, tuple[str, str, str, str, str, int]] = {}
    for key, item in ownership.items():
        record = _item_record(item, mapping_key=key)
        if record is not None:
            records[record[1]] = record
    return records


def merge_history_ownership(
    existing: Mapping[str, OutboxItem],
    outbox: Mapping[str, OutboxItem],
) -> dict[str, OutboxItem]:
    """合并已持久化归属与当前 outbox，完整 outbox 优先覆盖同一事件。"""
    merged: dict[str, OutboxItem] = {
        key: _record_to_item(record)
        for key, record in _valid_records(existing).items()
    }
    for key, item in outbox.items():
        record = _item_record(item, mapping_key=key)
        if record is not None:
            merged[key] = item
    return merged


def ownership_signature(
    ownership: Mapping[str, OutboxItem],
) -> frozenset[tuple[str, str, str, str, str, int]]:
    """返回不含正文的稳定签名，供 wrapper 判断是否需要重写账本。"""
    return frozenset(_valid_records(ownership).values())


def load_history_ownership(path: Path) -> dict[str, OutboxItem]:
    """严格加载账本；文件不存在表示尚无历史归属。"""
    ledger_path = Path(path)
    if not ledger_path.exists():
        return {}
    if not ledger_path.is_file():
        raise HistoryOwnershipError("历史归属账本不是普通文件")
    try:
        with ledger_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoryOwnershipError("历史归属账本读取失败") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "entries"}:
        raise HistoryOwnershipError("历史归属账本根结构不兼容")
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != _SCHEMA_VERSION
    ):
        raise HistoryOwnershipError("历史归属账本版本不兼容")
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise HistoryOwnershipError("历史归属账本 entries 无效")
    loaded: dict[str, OutboxItem] = {}
    automation_values: set[str] = set()
    for value in entries:
        record = _parse_record(value)
        if record[1] in loaded or record[0] in automation_values:
            raise HistoryOwnershipError("历史归属账本包含重复归属")
        automation_values.add(record[0])
        loaded[record[1]] = _record_to_item(record)
    return loaded


def save_history_ownership(
    path: Path,
    ownership: Mapping[str, OutboxItem],
) -> None:
    """原子保存最小账本；无有效归属时不创建无用文件。"""
    records = _valid_records(ownership)
    ledger_path = Path(path)
    if not records:
        return
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "entries": [
            _record_payload(records[key])
            for key in sorted(records, key=lambda value: (records[value][5], value))
        ],
    }
    temporary_path = ledger_path.with_name(
        f".{ledger_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, ledger_path)
    except (OSError, TypeError, ValueError) as exc:
        raise HistoryOwnershipError("历史归属账本保存失败") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


__all__ = [
    "HISTORY_OWNERSHIP_FILENAME",
    "HistoryOwnershipError",
    "load_history_ownership",
    "merge_history_ownership",
    "ownership_signature",
    "save_history_ownership",
]
