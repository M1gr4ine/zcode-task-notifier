"""按通知器归属安全软删除 ZCode 任务历史。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .models import Event, OutboxItem
from .notifier import automation_id


class HistoryCleanupError(RuntimeError):
    """历史清理失败；由上层决定是否忽略本轮清理。"""


class HistorySchemaError(HistoryCleanupError):
    """任务索引 schema 不满足清理所需契约。"""


@dataclass(frozen=True)
class CleanupReport:
    """不包含任务正文、路径或真实 ID 的清理结果。"""

    deleted_count: int
    deleted_task_ids_hash: str
    skipped: Mapping[str, int]
    candidate_count: int
    retained_count: int
    group_cleanup_skipped: int


@dataclass(frozen=True)
class _OwnedEvent:
    source: str
    automation_id: str
    event_task_id: str
    completed_at_ms: int


@dataclass(frozen=True)
class _Candidate:
    source: str
    automation_id: str
    event_task_id: str
    event_completed_at_ms: int
    task_id: str
    task_created_at: int
    task_updated_at: int


_TASK_COLUMNS = frozenset(
    {
        "workspace_key",
        "workspace_path",
        "task_id",
        "task_status",
        "pinned",
        "archived",
        "title_overridden",
        "created_at",
        "updated_at",
        "deleted",
        "cron_automation_id",
    }
)
_AUTOMATION_COLUMNS = frozenset(
    {
        "automation_id",
        "title",
        "workspace_key",
        "workspace_path",
        "target_task_id",
        "recurring",
        "max_runs",
        "schedule_edited_by_user",
        "enabled",
        "lifecycle_status",
        "next_run_at",
        "running",
        "claimed_at",
        "dispatch_status",
        "created_at",
        "updated_at",
    }
)
_GROUP_MEMBER_COLUMNS = frozenset({"workspace_key", "task_id"})
_GROUP_ORDER_COLUMNS = frozenset({"node_type", "node_key"})
_TERMINAL_STATUSES = frozenset({"completed", "error"})
_TERMINAL_LIFECYCLES = frozenset({"completed", "failed"})
_TERMINAL_DISPATCHES = frozenset(
    {"idle", "dispatched", "failed_to_dispatch", "skipped"}
)


def _workspace_values(workspace: Path) -> tuple[str, str]:
    path = Path(workspace)
    path_text = str(path)
    return path.name or path_text or "workspace", path_text


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in {
        "tasks",
        "automations",
        "task_group_members",
        "task_group_view_node_orders",
    }:
        raise HistorySchemaError("历史清理 schema 不兼容")
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        raise HistorySchemaError("历史清理 schema 不兼容") from exc
    return {str(row[1]) for row in rows if len(row) > 1}


def _validate_schema(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "tasks") or not _table_exists(
        connection, "automations"
    ):
        raise HistorySchemaError("历史清理 schema 不兼容")
    if not _TASK_COLUMNS.issubset(_table_columns(connection, "tasks")):
        raise HistorySchemaError("历史清理 schema 不兼容")
    if not _AUTOMATION_COLUMNS.issubset(_table_columns(connection, "automations")):
        raise HistorySchemaError("历史清理 schema 不兼容")

    members_exists = _table_exists(connection, "task_group_members")
    orders_exists = _table_exists(connection, "task_group_view_node_orders")
    if members_exists and not _GROUP_MEMBER_COLUMNS.issubset(
        _table_columns(connection, "task_group_members")
    ):
        raise HistorySchemaError("历史清理 schema 不兼容")
    if orders_exists and not _GROUP_ORDER_COLUMNS.issubset(
        _table_columns(connection, "task_group_view_node_orders")
    ):
        raise HistorySchemaError("历史清理 schema 不兼容")
    return members_exists and orders_exists


def _source_title_matches(source: str, title: Any) -> bool:
    if not isinstance(title, str):
        return False
    normalized = title.strip().casefold()
    if source == "codex":
        return normalized.startswith("[codex]")
    return source == "zcode" and normalized.startswith("[zcode]")


def _connect_rw(path: Path) -> sqlite3.Connection:
    """以 SQLite ``mode=rw`` 打开已有数据库，禁止意外创建空库。"""
    try:
        database = Path(path).expanduser().resolve(strict=False)
        uri = database.as_uri() + "?mode=rw"
    except (OSError, RuntimeError, ValueError) as exc:
        raise HistoryCleanupError("历史清理数据库路径无效") from exc
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
    except sqlite3.Error as exc:
        raise HistoryCleanupError("历史清理数据库不可用") from exc
    connection.execute("PRAGMA busy_timeout = 200")
    return connection


def _record_skip(skipped: Counter[str], reason: str) -> None:
    skipped[reason] += 1


def _owned_events(
    outbox: Mapping[str, OutboxItem], skipped: Counter[str]
) -> dict[str, _OwnedEvent]:
    owned: dict[str, _OwnedEvent] = {}
    for map_key, item in outbox.items():
        event = getattr(item, "event", None)
        if not isinstance(event, Event):
            _record_skip(skipped, "outbox_invalid")
            continue
        if map_key != event.key:
            _record_skip(skipped, "outbox_key")
            continue
        if item.status != "submitted":
            _record_skip(skipped, "outbox_not_submitted")
            continue
        if event.source not in {"zcode", "codex"}:
            _record_skip(skipped, "source")
            continue
        if event.status not in _TERMINAL_STATUSES:
            _record_skip(skipped, "event_not_terminal")
            continue
        if not isinstance(event.completed_at_ms, int) or isinstance(
            event.completed_at_ms, bool
        ) or event.completed_at_ms < 0 or not isinstance(event.task_id, str) or not event.task_id.strip():
            _record_skip(skipped, "event_invalid")
            continue
        try:
            expected_id = automation_id(event.key)
        except (TypeError, ValueError):
            _record_skip(skipped, "outbox_automation_id")
            continue
        if item.automation_id != expected_id:
            _record_skip(skipped, "outbox_automation_id")
            continue
        if expected_id in owned:
            _record_skip(skipped, "outbox_duplicate")
            continue
        owned[expected_id] = _OwnedEvent(
            source=event.source,
            automation_id=expected_id,
            event_task_id=event.task_id,
            completed_at_ms=event.completed_at_ms,
        )
    return owned


def _report_hash(workspace_key: str, task_ids: list[str]) -> str:
    values = sorted(f"{workspace_key}\0{task_id}" for task_id in task_ids)
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def cleanup_history(
    db_path: Path,
    outbox: Mapping[str, OutboxItem],
    workspace: Path,
    *,
    keep: int = 10,
    before_delete: Callable[[CleanupReport], None] | None = None,
) -> CleanupReport:
    """只软删除本产品已提交且已结束的通知 task 历史。

    发生删除时必须先在持锁事务内调用 ``before_delete``，由上层持久化
    删除审计；回调失败则整个事务回滚。
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 10:
        raise ValueError("keep 必须大于等于 10")

    workspace_key, workspace_path = _workspace_values(workspace)
    skipped: Counter[str] = Counter()
    owned = _owned_events(outbox, skipped)
    if not owned:
        return CleanupReport(
            deleted_count=0,
            deleted_task_ids_hash=_report_hash(workspace_key, []),
            skipped=MappingProxyType(dict(sorted(skipped.items()))),
            candidate_count=0,
            retained_count=0,
            group_cleanup_skipped=0,
        )

    connection: sqlite3.Connection | None = None
    groups_available = False
    group_cleanup_skipped = 0
    deleted_ids: list[str] = []
    candidate_count = 0
    retained_count = 0
    try:
        try:
            connection = _connect_rw(Path(db_path))
            groups_available = _validate_schema(connection)
        except HistoryCleanupError:
            raise
        except sqlite3.Error as exc:
            raise HistoryCleanupError("历史清理数据库不可用") from exc

        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT
                    t.workspace_key AS task_workspace_key,
                    t.workspace_path AS task_workspace_path,
                    t.task_id,
                    t.task_status,
                    t.pinned,
                    t.archived,
                    t.title_overridden,
                    t.created_at AS task_created_at,
                    t.updated_at AS task_updated_at,
                    t.cron_automation_id,
                    a.automation_id,
                    a.title AS automation_title,
                    a.workspace_key AS automation_workspace_key,
                    a.workspace_path AS automation_workspace_path,
                    a.target_task_id,
                    a.recurring,
                    a.max_runs,
                    a.schedule_edited_by_user,
                    a.enabled,
                    a.lifecycle_status,
                    a.next_run_at,
                    a.running,
                    a.claimed_at,
                    a.dispatch_status
                FROM tasks AS t
                JOIN automations AS a
                  ON a.automation_id = t.cron_automation_id
                WHERE t.deleted = 0
                  AND t.workspace_key = ?
                  AND t.workspace_path = ?
                  AND a.workspace_key = ?
                  AND a.workspace_path = ?
                """,
                (workspace_key, workspace_path, workspace_key, workspace_path),
            ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryCleanupError("历史清理查询失败") from exc

        seen_automation_ids: set[str] = set()
        candidates_by_source: dict[str, list[_Candidate]] = defaultdict(list)
        for row in rows:
            (
                task_workspace_key,
                task_workspace_path,
                task_id,
                task_status,
                task_pinned,
                task_archived,
                task_title_overridden,
                task_created_at,
                task_updated_at,
                task_cron_id,
                automation_value,
                automation_title,
                automation_workspace_key,
                automation_workspace_path,
                target_task_id,
                recurring,
                max_runs,
                schedule_edited_by_user,
                enabled,
                lifecycle_status,
                next_run_at,
                running,
                claimed_at,
                dispatch_status,
            ) = row
            owner = owned.get(automation_value)
            if owner is None:
                continue
            seen_automation_ids.add(automation_value)
            if (
                task_workspace_key != workspace_key
                or task_workspace_path != workspace_path
                or automation_workspace_key != workspace_key
                or automation_workspace_path != workspace_path
            ):
                _record_skip(skipped, "workspace")
                continue
            if task_id == owner.event_task_id:
                _record_skip(skipped, "business_task_id")
                continue
            if any(value != 0 for value in (task_pinned, task_archived, task_title_overridden)):
                _record_skip(skipped, "task_user_protected")
                continue
            if not isinstance(task_status, str) or task_status.casefold() not in _TERMINAL_STATUSES:
                _record_skip(skipped, "task_status")
                continue
            if not _source_title_matches(owner.source, automation_title):
                _record_skip(skipped, "automation_title_source")
                continue
            if target_task_id is not None:
                _record_skip(skipped, "automation_target_task")
                continue
            if recurring != 0 or max_runs != 1:
                _record_skip(skipped, "automation_schedule")
                continue
            if schedule_edited_by_user != 0:
                _record_skip(skipped, "automation_schedule_edited")
                continue
            if running != 0:
                _record_skip(skipped, "automation_running")
                continue
            if claimed_at is not None:
                _record_skip(skipped, "automation_claimed")
                continue
            if lifecycle_status not in _TERMINAL_LIFECYCLES:
                _record_skip(skipped, "automation_lifecycle")
                continue
            if enabled != 0 or next_run_at is not None:
                _record_skip(skipped, "automation_enabled")
                continue
            if dispatch_status not in _TERMINAL_DISPATCHES:
                _record_skip(skipped, "automation_dispatch")
                continue
            if not isinstance(task_created_at, int) or not isinstance(
                task_updated_at, int
            ):
                _record_skip(skipped, "task_timestamp")
                continue
            candidates_by_source[owner.source].append(
                _Candidate(
                    source=owner.source,
                    automation_id=owner.automation_id,
                    event_task_id=owner.event_task_id,
                    event_completed_at_ms=owner.completed_at_ms,
                    task_id=task_id,
                    task_created_at=task_created_at,
                    task_updated_at=task_updated_at,
                )
            )

        for automation_value in owned:
            if automation_value not in seen_automation_ids:
                _record_skip(skipped, "task_join_missing")

        all_candidates = [
            candidate
            for source_candidates in candidates_by_source.values()
            for candidate in source_candidates
        ]
        candidate_count = len(all_candidates)
        to_delete: list[_Candidate] = []
        for source_candidates in candidates_by_source.values():
            ordered = sorted(
                source_candidates,
                key=lambda candidate: (
                    candidate.event_completed_at_ms,
                    candidate.task_updated_at,
                    candidate.task_created_at,
                    candidate.task_id,
                ),
                reverse=True,
            )
            retained_count += min(keep, len(ordered))
            to_delete.extend(ordered[keep:])

        if not groups_available and to_delete:
            _record_skip(skipped, "groups_missing")

        if to_delete:
            if before_delete is None:
                raise HistoryCleanupError(
                    "发生历史删除时必须提供 before_delete 审计回调"
                )
            planned_report = CleanupReport(
                deleted_count=len(to_delete),
                deleted_task_ids_hash=_report_hash(
                    workspace_key, [candidate.task_id for candidate in to_delete]
                ),
                skipped=MappingProxyType(dict(sorted(skipped.items()))),
                candidate_count=candidate_count,
                retained_count=retained_count,
                group_cleanup_skipped=group_cleanup_skipped,
            )
            try:
                before_delete(planned_report)
            except Exception as exc:
                raise HistoryCleanupError("删除前审计回调失败") from exc

        for candidate in to_delete:
            changed = connection.execute(
                """
                UPDATE tasks
                SET deleted = 1
                WHERE workspace_key = ?
                  AND task_id = ?
                  AND cron_automation_id = ?
                  AND deleted = 0
                """,
                (workspace_key, candidate.task_id, candidate.automation_id),
            ).rowcount
            if changed != 1:
                _record_skip(skipped, "task_changed")
                continue
            deleted_ids.append(candidate.task_id)
            if not groups_available:
                group_cleanup_skipped += 1
                continue
            connection.execute(
                "DELETE FROM task_group_members WHERE workspace_key = ? AND task_id = ?",
                (workspace_key, candidate.task_id),
            )
            task_node_key = json.dumps(
                [workspace_key, candidate.task_id],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                """
                DELETE FROM task_group_view_node_orders
                WHERE node_type = 'task' AND node_key = ?
                """,
                (task_node_key,),
            )
            legacy_exists = connection.execute(
                """
                SELECT 1 FROM task_group_view_node_orders
                WHERE node_type = 'task' AND node_key = ?
                LIMIT 1
                """,
                (workspace_key,),
            ).fetchone()
            if legacy_exists is not None:
                _record_skip(skipped, "legacy_task_node")

        connection.commit()
    except HistoryCleanupError:
        if connection is not None:
            connection.rollback()
        raise
    except sqlite3.OperationalError as exc:
        if connection is not None:
            connection.rollback()
        raise HistoryCleanupError("历史清理数据库繁忙或事务失败") from exc
    except sqlite3.Error as exc:
        if connection is not None:
            connection.rollback()
        raise HistoryCleanupError("历史清理事务失败") from exc
    finally:
        if connection is not None:
            connection.close()

    return CleanupReport(
        deleted_count=len(deleted_ids),
        deleted_task_ids_hash=_report_hash(workspace_key, deleted_ids),
        skipped=MappingProxyType(dict(sorted(skipped.items()))),
        candidate_count=candidate_count,
        retained_count=retained_count,
        group_cleanup_skipped=group_cleanup_skipped,
    )


__all__ = [
    "CleanupReport",
    "HistoryCleanupError",
    "HistorySchemaError",
    "cleanup_history",
]
