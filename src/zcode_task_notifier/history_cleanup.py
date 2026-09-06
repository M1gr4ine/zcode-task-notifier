"""按自动化归属安全软删除 ZCode 任务历史。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
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
class _Candidate:
    automation_id: str
    task_id: str
    task_created_at: int
    task_updated_at: int
    task_workspace_key: str
    task_workspace_path: str


_AGENT_TAGS = frozenset({"codex", "zcode", "claudecode", "dsh"})
_NOTIFICATION_ID_PATTERNS = (
    re.compile(r"automation-tnotify-[0-9a-f]{24}\Z"),
    re.compile(r"automation-tnotify-(?:zcode|codex)-[0-9a-f]{24}\Z"),
    re.compile(r"automation-tnotify-[0-9]{13}\Z"),
)


def _normalize_notification_title(value: str) -> str:
    return re.sub(r"(?<=[。！？；：，、]) +", "", " ".join(value.split()))


_NOTIFICATION_TITLE_PREFIXES = tuple(
    _normalize_notification_title(value)
    for value in (
        "你是任务停顿通知摘要助手。 只概括下面这一个停顿事件，不扫描或混入其他任务。",
        "你是任务完成通知摘要助手。 只概括下面这一个完成事件，不扫描或混入其他任务。",
        "ZCode 任务完成通知自动化。以下任务有状态更新，请生成微信通知。",
    )
)
_AWAITING_STATUSES = frozenset({"awaiting_approval", "awaiting_input"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "error"})
_ACTIVE_DISPATCH_STATUSES = frozenset(
    {"claimed", "running", "dispatching", "pending", "queued", "retry", "retrying"}
)

_TASK_COLUMNS = frozenset(
    {
        "workspace_key",
        "workspace_path",
        "workspace_identity",
        "task_id",
        "title",
        "task_status",
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
        "workspace_path",
        "workspace_identity",
        "running",
        "claimed_at",
        "dispatch_status",
    }
)
_GROUP_MEMBER_COLUMNS = frozenset({"workspace_key", "task_id"})
_GROUP_ORDER_COLUMNS = frozenset({"node_type", "node_key"})
_SCHEMA_TABLES = frozenset(
    {"tasks", "automations", "task_group_members", "task_group_view_node_orders"}
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
    if table not in _SCHEMA_TABLES:
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
    if members_exists != orders_exists:
        raise HistorySchemaError("历史清理 schema 不兼容")
    if members_exists and not _GROUP_MEMBER_COLUMNS.issubset(
        _table_columns(connection, "task_group_members")
    ):
        raise HistorySchemaError("历史清理 schema 不兼容")
    if orders_exists and not _GROUP_ORDER_COLUMNS.issubset(
        _table_columns(connection, "task_group_view_node_orders")
    ):
        raise HistorySchemaError("历史清理 schema 不兼容")
    return members_exists and orders_exists


def _agent_tag_matches(title: Any) -> bool:
    """只接受固定 Agent 标签，避免把普通业务方括号当作归属。"""
    if not isinstance(title, str):
        return False
    normalized = title.casefold()
    return any(f"[{tag}]" in normalized for tag in _AGENT_TAGS)


def _notification_id_matches(value: Any) -> bool:
    """只接受历史通知器使用过的完整 ID 格式。"""
    return isinstance(value, str) and any(
        pattern.fullmatch(value) is not None for pattern in _NOTIFICATION_ID_PATTERNS
    )


def _notification_template_matches(title: Any) -> bool:
    """只接受固定通知模板的开头，白空格归一后仍须严格起始匹配。"""
    if not isinstance(title, str):
        return False
    normalized = _normalize_notification_title(title)
    return any(normalized.startswith(prefix) for prefix in _NOTIFICATION_TITLE_PREFIXES)


def _source_title_matches(source: str, title: Any) -> bool:
    """保留旧内部辅助函数的兼容形态；新清理不按 source 限制。"""
    return source.casefold() in _AGENT_TAGS and _agent_tag_matches(title)


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


def _awaiting_automation_ids(outbox: Mapping[str, OutboxItem]) -> set[str]:
    """仅用内存停顿事件保护等待中的自动化，不承担历史归属判定。"""
    protected: set[str] = set()
    for map_key, item in outbox.items():
        event = getattr(item, "event", None)
        if not isinstance(event, Event) or map_key != event.key:
            continue
        if event.status not in _AWAITING_STATUSES:
            continue
        value = getattr(item, "automation_id", None)
        if not isinstance(value, str) or not value:
            continue
        try:
            expected = automation_id(event.key)
        except (TypeError, ValueError):
            continue
        if value == expected:
            protected.add(value)
    return protected


def _flag_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {
            "1",
            "true",
            "yes",
            "active",
            "claimed",
            "running",
        }
    return value is not None


def _has_claim(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _candidate_hash(candidates: list[_Candidate]) -> str:
    values = sorted(
        f"{item.task_workspace_key}\0{item.task_id}" for item in candidates
    )
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def cleanup_history(
    db_path: Path,
    outbox: Mapping[str, OutboxItem],
    workspace: Path,
    *,
    keep: int = 5,
    before_delete: Callable[[CleanupReport], None] | None = None,
) -> CleanupReport:
    """按自动化关联和固定 Agent 标签清理历史，默认合计保留最新五条。

    归属不依赖 outbox 或历史账本；outbox 只用于保护当前等待审批/输入的自动化。
    发生删除时必须先在持锁事务内调用 ``before_delete``，回调失败则事务回滚。
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 5:
        raise ValueError("keep 必须大于等于 5")

    _, workspace_path = _workspace_values(workspace)
    skipped: Counter[str] = Counter()
    awaiting_ids = _awaiting_automation_ids(outbox)
    connection: sqlite3.Connection | None = None
    groups_available = False
    group_cleanup_skipped = 0
    deleted_ids: list[str] = []
    deleted_candidates: list[_Candidate] = []
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
                    t.workspace_key,
                    t.workspace_path,
                    t.workspace_identity,
                    t.task_id,
                    t.title,
                    t.task_status,
                    t.created_at,
                    t.updated_at,
                    t.cron_automation_id,
                    a.automation_id,
                    a.title,
                    a.workspace_path,
                    a.workspace_identity,
                    a.running,
                    a.claimed_at,
                    a.dispatch_status
                FROM tasks AS t
                LEFT JOIN automations AS a
                  ON a.automation_id = t.cron_automation_id
                WHERE t.deleted = 0
                  AND t.cron_automation_id IS NOT NULL
                  AND t.cron_automation_id <> ''
                  AND t.workspace_path = ?
                  AND t.workspace_identity IS NULL
                """,
                (workspace_path,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryCleanupError("历史清理查询失败") from exc

        candidates: list[_Candidate] = []
        for row in rows:
            (
                task_workspace_key,
                task_workspace_path,
                task_workspace_identity,
                task_id,
                task_title,
                task_status,
                task_created_at,
                task_updated_at,
                task_automation_id,
                parent_automation_id,
                parent_title,
                parent_workspace_path,
                parent_workspace_identity,
                parent_running,
                parent_claimed_at,
                parent_dispatch_status,
            ) = row
            if not isinstance(task_workspace_key, str) or not isinstance(
                task_workspace_path, str
            ):
                _record_skip(skipped, "workspace")
                continue
            if task_workspace_identity is not None:
                _record_skip(skipped, "workspace_identity")
                continue
            if not isinstance(task_id, str) or not task_id:
                _record_skip(skipped, "task_identity")
                continue
            if not isinstance(task_automation_id, str) or not task_automation_id:
                _record_skip(skipped, "automation_identity")
                continue
            if not isinstance(task_status, str) or task_status.casefold() not in _TERMINAL_TASK_STATUSES:
                _record_skip(skipped, "task_status")
                continue
            tagged = _agent_tag_matches(task_title) or _agent_tag_matches(parent_title)
            fallback = _notification_id_matches(
                task_automation_id
            ) and _notification_template_matches(task_title)
            if not tagged and not fallback:
                _record_skip(skipped, "agent_tag")
                continue
            if task_automation_id in awaiting_ids:
                _record_skip(skipped, "event_waiting")
                continue
            if parent_automation_id is not None:
                if parent_workspace_identity is not None:
                    _record_skip(skipped, "workspace_identity")
                    continue
                if parent_workspace_path != workspace_path:
                    _record_skip(skipped, "workspace")
                    continue
                if _flag_enabled(parent_running) or _has_claim(parent_claimed_at):
                    _record_skip(skipped, "automation_running")
                    continue
                if (
                    isinstance(parent_dispatch_status, str)
                    and parent_dispatch_status.casefold() in _ACTIVE_DISPATCH_STATUSES
                ):
                    _record_skip(skipped, "automation_running")
                    continue
            if not isinstance(task_created_at, int) or isinstance(
                task_created_at, bool
            ) or not isinstance(task_updated_at, int) or isinstance(
                task_updated_at, bool
            ):
                _record_skip(skipped, "task_timestamp")
                continue
            candidates.append(
                _Candidate(
                    automation_id=task_automation_id,
                    task_id=task_id,
                    task_created_at=task_created_at,
                    task_updated_at=task_updated_at,
                    task_workspace_key=task_workspace_key,
                    task_workspace_path=task_workspace_path,
                )
            )

        candidate_count = len(candidates)
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                candidate.task_updated_at,
                candidate.task_created_at,
                candidate.task_id,
            ),
            reverse=True,
        )
        retained_count = min(keep, len(ordered))
        to_delete = ordered[keep:]

        if not groups_available and to_delete:
            _record_skip(skipped, "groups_missing")
            group_cleanup_skipped = len(to_delete)

        if to_delete:
            if before_delete is None:
                raise HistoryCleanupError(
                    "发生历史删除时必须提供 before_delete 审计回调"
                )
            planned_report = CleanupReport(
                deleted_count=len(to_delete),
                deleted_task_ids_hash=_candidate_hash(to_delete),
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
                  AND workspace_path = ?
                  AND task_id = ?
                  AND cron_automation_id = ?
                  AND deleted = 0
                """,
                (
                    candidate.task_workspace_key,
                    candidate.task_workspace_path,
                    candidate.task_id,
                    candidate.automation_id,
                ),
            ).rowcount
            if changed != 1:
                raise HistoryCleanupError("历史清理事务中的任务行数不一致")
            deleted_ids.append(candidate.task_id)
            deleted_candidates.append(candidate)
            if not groups_available:
                continue
            connection.execute(
                "DELETE FROM task_group_members WHERE workspace_key = ? AND task_id = ?",
                (candidate.task_workspace_key, candidate.task_id),
            )
            task_node_key = json.dumps(
                [candidate.task_workspace_key, candidate.task_id],
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
                (candidate.task_workspace_key,),
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
        deleted_task_ids_hash=_candidate_hash(deleted_candidates),
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
