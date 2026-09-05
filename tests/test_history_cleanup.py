import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from zcode_task_notifier.models import Event, OutboxItem
from zcode_task_notifier.notifier import automation_id


def _create_db(path: Path, *, groups: bool = True, broken_tasks: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            workspace_key TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            workspace_identity TEXT,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            task_status TEXT,
            provider TEXT,
            mode TEXT NOT NULL DEFAULT 'build',
            model TEXT,
            migration_source TEXT,
            forked_from_task_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            unread_at INTEGER,
            last_unread_at INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            title_overridden INTEGER NOT NULL DEFAULT 0,
            meta_json TEXT NOT NULL DEFAULT '{}',
            searchable_text TEXT NOT NULL DEFAULT '',
            cron_automation_id TEXT,
            off_peak_task_id TEXT,
            PRIMARY KEY (workspace_key, task_id)
        );

        CREATE TABLE automations (
            automation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            cron_expr TEXT NOT NULL,
            prompt TEXT NOT NULL,
            model TEXT,
            provider TEXT,
            mode TEXT,
            thought_level TEXT,
            workspace_key TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            workspace_identity TEXT,
            target_task_id TEXT,
            bot_delivery_target TEXT,
            location_kind TEXT NOT NULL DEFAULT 'local',
            recurring INTEGER NOT NULL DEFAULT 1,
            max_runs INTEGER,
            end_at INTEGER,
            schedule_rule TEXT,
            schedule_edited_by_user INTEGER NOT NULL DEFAULT 0,
            run_count INTEGER NOT NULL DEFAULT 0,
            scheduled_run_count INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            next_run_at INTEGER,
            last_run_at INTEGER,
            running INTEGER NOT NULL DEFAULT 0,
            claimed_at INTEGER,
            dispatch_status TEXT NOT NULL DEFAULT 'idle',
            dispatch_attempts INTEGER NOT NULL DEFAULT 0,
            retry_at INTEGER,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE automation_runs (
            run_id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL,
            scheduled_at INTEGER,
            trigger TEXT,
            dispatch_status TEXT,
            outcome TEXT,
            session_id TEXT,
            error TEXT,
            attempts INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    if broken_tasks:
        connection.execute("ALTER TABLE tasks RENAME TO tasks_before_break")
        connection.execute(
            """
            CREATE TABLE tasks (
                workspace_key TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                task_id TEXT NOT NULL,
                task_status TEXT,
                cron_automation_id TEXT,
                PRIMARY KEY (workspace_key, task_id)
            )
            """
        )
    if groups:
        connection.executescript(
            """
            CREATE TABLE task_group_members (
                group_id TEXT NOT NULL,
                workspace_key TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                workspace_identity TEXT,
                task_id TEXT NOT NULL,
                sort_order INTEGER,
                added_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (workspace_key, task_id)
            );

            CREATE TABLE task_group_view_node_orders (
                node_type TEXT NOT NULL,
                node_key TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (node_type, node_key)
            );
            """
        )
    connection.commit()
    connection.close()


def _workspace(tmp_path: Path, name: str = "notification-workspace") -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    return workspace


def _workspace_values(workspace: Path) -> tuple[str, str]:
    path = Path(workspace)
    path_text = str(path)
    return path.name or path_text or "workspace", path_text


def _event(
    source: str,
    key: str,
    task_id: str,
    completed_at_ms: int,
    *,
    status: str = "completed",
) -> Event:
    return Event(
        source=source,  # type: ignore[arg-type]
        key=key,
        task_id=task_id,
        title="源事件标题",
        completed_at_ms=completed_at_ms,
        duration_ms=1,
        summary_text="private summary must not appear in the report",
        status=status,  # type: ignore[arg-type]
    )


def _insert_automation(
    connection: sqlite3.Connection,
    workspace: Path,
    event: Event,
    *,
    title: str | None = None,
    automation_value: str | None = None,
    **changes: object,
) -> str:
    workspace_key, workspace_path = _workspace_values(workspace)
    identifier = automation_value or automation_id(event.key)
    if title is None:
        title = "[codex] 源事件标题" if event.source == "codex" else "[zcode] 源事件标题"
    values = (
        identifier,
        title,
        "* * * * *",
        "synthetic prompt",
        "synthetic-model",
        None,
        "yolo",
        None,
        workspace_key,
        workspace_path,
        None,
        None,
        None,
        "local",
        0,
        1,
        None,
        None,
        0,
        1,
        1,
        0,
        "completed",
        None,
        event.completed_at_ms,
        0,
        None,
        "dispatched",
        1,
        None,
        None,
        event.completed_at_ms,
        event.completed_at_ms,
    )
    connection.execute(
        """
        INSERT INTO automations (
            automation_id, title, cron_expr, prompt, model, provider, mode,
            thought_level, workspace_key, workspace_path, workspace_identity,
            target_task_id, bot_delivery_target, location_kind, recurring,
            max_runs, end_at, schedule_rule, schedule_edited_by_user, run_count,
            scheduled_run_count, enabled, lifecycle_status, next_run_at,
            last_run_at, running, claimed_at, dispatch_status, dispatch_attempts,
            retry_at, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    for field, value in changes.items():
        if field not in {
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
        }:
            raise AssertionError(f"unsupported synthetic automation field: {field}")
        connection.execute(
            f"UPDATE automations SET {field} = ? WHERE automation_id = ?",
            (value, identifier),
        )
    return identifier


def _insert_task(
    connection: sqlite3.Connection,
    workspace: Path,
    task_id: str,
    automation_value: str,
    *,
    event_time: int,
    status: str = "completed",
    deleted: int = 0,
) -> None:
    workspace_key, workspace_path = _workspace_values(workspace)
    connection.execute(
        """
        INSERT INTO tasks (
            workspace_key, workspace_path, workspace_identity, task_id, title,
            task_status, provider, mode, model, migration_source,
            forked_from_task_id, created_at, updated_at, unread_at,
            last_unread_at, pinned, archived, deleted, title_overridden,
            meta_json, searchable_text, cron_automation_id, off_peak_task_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workspace_key,
            workspace_path,
            None,
            task_id,
            "child task title may be renamed",
            status,
            "glm",
            "build",
            "synthetic-model",
            None,
            None,
            event_time,
            event_time,
            None,
            0,
            0,
            0,
            deleted,
            0,
            "{}",
            "",
            automation_value,
            None,
        ),
    )


def _add_candidate(
    connection: sqlite3.Connection,
    workspace: Path,
    index: int,
    *,
    source: str = "zcode",
    task_id: str | None = None,
    event_task_id: str | None = None,
    event_status: str = "completed",
    outbox_status: str = "submitted",
    title: str | None = None,
    automation_value: str | None = None,
    task_status: str = "completed",
    **automation_changes: object,
) -> tuple[Event, str, str]:
    event = _event(
        source,
        f"{source}:event-{index}",
        event_task_id or f"business-{source}-{index}",
        index * 1000,
        status=event_status,
    )
    identifier = _insert_automation(
        connection,
        workspace,
        event,
        title=title,
        automation_value=automation_value,
        **automation_changes,
    )
    candidate_task_id = task_id or f"notification-{source}-{index}"
    _insert_task(
        connection,
        workspace,
        candidate_task_id,
        identifier,
        event_time=index * 1000,
        status=task_status,
    )
    connection.execute(
        """
        INSERT INTO automation_runs (
            run_id, automation_id, workspace_key, scheduled_at, trigger,
            dispatch_status, outcome, session_id, error, attempts, created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"run-{source}-{index}",
            identifier,
            _workspace_values(workspace)[0],
            index * 1000,
            "schedule",
            "dispatched",
            "completed",
            f"runtime-{source}-{index}",
            None,
            1,
            index * 1000,
            index * 1000,
        ),
    )
    return event, candidate_task_id, identifier


def _outbox_item(event: Event, *, status: str = "submitted", value: str | None = None) -> OutboxItem:
    return OutboxItem(
        event=event,
        automation_id=value or automation_id(event.key),
        status=status,  # type: ignore[arg-type]
        submitted_at_ms=event.completed_at_ms if status == "submitted" else None,
    )


def _deleted_tasks(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT task_id FROM tasks WHERE deleted = 1"
            )
        }
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("count", "expected_deleted"),
    [(9, 0), (10, 0), (11, 1)],
)
def test_retention_keeps_at_least_ten_per_source(
    tmp_path: Path, count: int, expected_deleted: int
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    for index in range(count):
        event, _, _ = _add_candidate(connection, workspace, index)
        outbox[event.key] = _outbox_item(event)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    report = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )

    assert report.deleted_count == expected_deleted
    assert len(_deleted_tasks(db_path)) == expected_deleted
    assert report.deleted_task_ids_hash == hashlib.sha256(
        (
            f"{_workspace_values(workspace)[0]}\0notification-zcode-0"
            if expected_deleted
            else ""
        ).encode()
    ).hexdigest()


def test_retention_is_per_source_and_protects_original_business_task(tmp_path: Path):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    for source in ("zcode", "codex"):
        for index in range(11):
            event, _, _ = _add_candidate(connection, workspace, index, source=source)
            outbox[event.key] = _outbox_item(event)
    protected_event, _, _ = _add_candidate(
        connection,
        workspace,
        100,
        event_task_id="notification-zcode-100",
    )
    outbox[protected_event.key] = _outbox_item(protected_event)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    report = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )

    assert report.deleted_count == 2
    assert _deleted_tasks(db_path) == {
        "notification-zcode-0",
        "notification-codex-0",
    }
    assert report.skipped["business_task_id"] == 1


def test_exact_outbox_source_status_workspace_and_title_ownership_is_required(
    tmp_path: Path,
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    other_workspace = _workspace(tmp_path, "other-workspace")
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}

    valid_task = "notification-zcode-0"
    for index in range(11):
        valid_event, current_task, _ = _add_candidate(connection, workspace, index)
        outbox[valid_event.key] = _outbox_item(valid_event)
        if index == 0:
            valid_task = current_task
    pending_event, pending_task, _ = _add_candidate(connection, workspace, 20)
    outbox[pending_event.key] = _outbox_item(pending_event, status="pending")
    approval_event, approval_task, _ = _add_candidate(
        connection, workspace, 21, event_status="awaiting_approval"
    )
    outbox[approval_event.key] = _outbox_item(approval_event)
    mismatch_event, mismatch_task, mismatch_id = _add_candidate(
        connection,
        workspace,
        22,
    )
    outbox[mismatch_event.key] = _outbox_item(
        mismatch_event, value="automation-known-but-not-event-id"
    )
    title_event, title_task, _ = _add_candidate(
        connection, workspace, 23, source="codex", title="renamed without source tag"
    )
    outbox[title_event.key] = _outbox_item(title_event)
    other_event, other_task, _ = _add_candidate(
        connection, other_workspace, 24, source="codex"
    )
    outbox[other_event.key] = _outbox_item(other_event)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    report = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )

    assert report.deleted_count == 1
    assert _deleted_tasks(db_path) == {valid_task}
    assert pending_task not in _deleted_tasks(db_path)
    assert approval_task not in _deleted_tasks(db_path)
    assert mismatch_task not in _deleted_tasks(db_path)
    assert title_task not in _deleted_tasks(db_path)
    assert other_task not in _deleted_tasks(db_path)
    assert mismatch_id not in report.deleted_task_ids_hash
    assert report.skipped["outbox_automation_id"] == 1
    assert report.skipped["outbox_not_submitted"] == 1
    assert report.skipped["event_not_terminal"] == 1
    assert report.skipped["automation_title_source"] == 1
    # 查询已在 SQL 层限定当前 workspace，越界自动化不会进入候选扫描。
    assert report.skipped.get("workspace", 0) == 0


def test_running_claimed_dispatch_and_user_schedule_are_protected(tmp_path: Path):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    fields = (
        {"running": 1},
        {"claimed_at": 123},
        {"dispatch_status": "claimed"},
        {"lifecycle_status": "active", "enabled": 1},
        {"schedule_edited_by_user": 1},
    )
    task_ids = []
    for index, changes in enumerate(fields, start=1):
        event, task_id, _ = _add_candidate(
            connection, workspace, index, **changes
        )
        outbox[event.key] = _outbox_item(event)
        task_ids.append(task_id)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    report = cleanup_history(db_path, outbox, workspace)

    assert report.deleted_count == 0
    assert _deleted_tasks(db_path) == set()
    assert all(task_id not in _deleted_tasks(db_path) for task_id in task_ids)
    assert report.skipped["automation_running"] == 1
    assert report.skipped["automation_claimed"] == 1
    assert report.skipped["automation_dispatch"] == 1
    assert report.skipped["automation_lifecycle"] == 1
    assert report.skipped["automation_schedule_edited"] == 1


def test_soft_delete_is_idempotent_and_preserves_native_rows_and_source_file(
    tmp_path: Path,
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    source_file = tmp_path / "model-io-source.jsonl"
    source_file.write_text("private source content", encoding="utf-8")
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    candidate_ids: list[str] = []
    for index in range(11):
        event, task_id, identifier = _add_candidate(connection, workspace, index)
        outbox[event.key] = _outbox_item(event)
        candidate_ids.append(task_id)
        connection.execute(
            "INSERT INTO task_group_members VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "group-1",
                _workspace_values(workspace)[0],
                _workspace_values(workspace)[1],
                None,
                task_id,
                index,
                index,
                index,
                index,
            ),
        )
        connection.execute(
            "INSERT INTO task_group_view_node_orders VALUES (?, ?, ?, ?, ?)",
            (
                "task",
                json.dumps(
                    [_workspace_values(workspace)[0], task_id],
                    separators=(",", ":"),
            ),
                index,
                index,
                index,
            ),
        )
        if index == 0:
            connection.execute(
                "INSERT INTO task_group_view_node_orders VALUES (?, ?, ?, ?, ?)",
                ("task", _workspace_values(workspace)[0], 999, 999, 999),
            )
        assert identifier
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    first = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )
    second = cleanup_history(db_path, outbox, workspace)

    assert first.deleted_count == 1
    assert second.deleted_count == 0
    assert first.deleted_task_ids_hash == hashlib.sha256(
        f"{_workspace_values(workspace)[0]}\0{candidate_ids[0]}".encode()
    ).hexdigest()
    assert source_file.read_text(encoding="utf-8") == "private source content"
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 11
        assert connection.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 11
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_members WHERE task_id = ?",
            (candidate_ids[0],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders WHERE node_type = 'task' AND node_key = ?",
            (
                json.dumps(
                    [_workspace_values(workspace)[0], candidate_ids[0]],
                    separators=(",", ":"),
                ),
            ),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders WHERE node_type = 'task' AND node_key = ?",
            (_workspace_values(workspace)[0],),
        ).fetchone()[0] == 1
    finally:
        connection.close()
    assert first.skipped["legacy_task_node"] >= 1
    assert str(tmp_path) not in repr(first)
    assert "private source content" not in repr(first)


def test_missing_group_tables_are_reported_without_blocking_task_soft_delete(
    tmp_path: Path,
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path, groups=False)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    for index in range(11):
        event, _, _ = _add_candidate(connection, workspace, index)
        outbox[event.key] = _outbox_item(event)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    report = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )

    assert report.deleted_count == 1
    assert report.group_cleanup_skipped == 1
    assert report.skipped["groups_missing"] == 1


def test_incompatible_schema_fails_closed_before_any_write(tmp_path: Path):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path, broken_tasks=True)
    event = _event("zcode", "zcode:broken-schema", "business-broken", 1)
    outbox = {event.key: _outbox_item(event)}

    from zcode_task_notifier.history_cleanup import HistorySchemaError, cleanup_history

    with pytest.raises(HistorySchemaError):
        cleanup_history(db_path, outbox, workspace)
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        connection.close()


def test_keep_below_ten_is_rejected_without_touching_database(tmp_path: Path):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    event, task_id, _ = _add_candidate(connection, workspace, 1)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    with pytest.raises(ValueError, match="keep"):
        cleanup_history(db_path, {event.key: _outbox_item(event)}, workspace, keep=9)
    assert _deleted_tasks(db_path) == set()
    assert task_id not in _deleted_tasks(db_path)


def test_parameterized_malicious_ids_and_paths_do_not_leak_or_break_schema(
    tmp_path: Path,
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path, "workspace'; DROP TABLE tasks;--")
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    malicious_task_id = "notification'; DROP TABLE tasks;--"
    malicious_event, _, _ = _add_candidate(
        connection,
        workspace,
        0,
        task_id=malicious_task_id,
        event_task_id="business'; DROP TABLE automations;--",
    )
    outbox[malicious_event.key] = _outbox_item(malicious_event)
    for index in range(1, 11):
        event, _, _ = _add_candidate(connection, workspace, index)
        outbox[event.key] = _outbox_item(event)
    connection.commit()
    connection.close()

    from zcode_task_notifier.history_cleanup import cleanup_history

    report = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )

    assert report.deleted_count == 1
    assert malicious_task_id in _deleted_tasks(db_path)
    assert malicious_task_id not in repr(report)
    assert str(workspace) not in repr(report)
    assert "private summary" not in repr(report)
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 11
        assert connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 11
    finally:
        connection.close()


def test_source_title_requires_matching_source_tag():
    from zcode_task_notifier.history_cleanup import _source_title_matches

    assert _source_title_matches("codex", "[codex] 任务")
    assert not _source_title_matches("codex", "[zcode] 任务")
    assert _source_title_matches("zcode", "[zcode] 任务")
    assert not _source_title_matches("zcode", "旧版任务")
    assert not _source_title_matches("zcode", "[codex] 任务")
    assert not _source_title_matches("zcode", "[other] 任务")


def test_unlabeled_zcode_automation_is_not_a_cleanup_candidate(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    for index in range(11):
        event, _, _ = _add_candidate(
            connection,
            workspace,
            index,
            source="zcode",
            title="旧版无标签任务",
        )
        outbox[event.key] = _outbox_item(event)
    connection.commit()
    connection.close()

    report = cleanup_history(
        db_path,
        outbox,
        workspace,
        before_delete=lambda report: None,
    )

    assert report.deleted_count == 0
    assert _deleted_tasks(db_path) == set()


@pytest.mark.parametrize("field", ["pinned", "archived", "title_overridden"])
def test_user_preserved_notification_is_never_soft_deleted(tmp_path, field):
    from zcode_task_notifier.history_cleanup import cleanup_history
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    outbox = {}
    with sqlite3.connect(db_path) as connection:
        for index in range(12):
            event, _, _ = _add_candidate(connection, workspace, index)
            outbox[event.key] = _outbox_item(event)
        # 字段来自固定参数列表，正文和标识仍参数化。
        connection.execute(f"UPDATE tasks SET {field}=1 WHERE task_id=?", ("notification-zcode-0",))
    report = cleanup_history(db_path, outbox, workspace, before_delete=lambda report: None)
    assert _deleted_tasks(db_path) == {"notification-zcode-1"}
    assert report.skipped["task_user_protected"] == 1


@pytest.mark.parametrize("task_id, timestamp", [("", 1), (None, 1), ("business", -1)])
def test_direct_cleanup_rejects_invalid_source_identity_or_time(tmp_path, task_id, timestamp):
    from dataclasses import replace
    from zcode_task_notifier.history_cleanup import cleanup_history
    event = replace(_event("zcode", "zcode:invalid", "business", 1),
                    task_id=task_id, completed_at_ms=timestamp)
    report = cleanup_history(tmp_path / "unused.sqlite", {event.key: _outbox_item(event)}, tmp_path)
    assert report.candidate_count == 0
    assert report.skipped["event_invalid"] == 1


def test_missing_history_database_is_not_created(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "missing-tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    event = _event("zcode", "zcode:missing", "business-missing", 1)

    with pytest.raises(HistoryCleanupError):
        cleanup_history(
            db_path,
            {event.key: _outbox_item(event)},
            workspace,
            before_delete=lambda report: None,
        )

    assert not db_path.exists()


def test_delete_requires_before_delete_audit_callback(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    for index in range(11):
        event, _, _ = _add_candidate(connection, workspace, index)
        outbox[event.key] = _outbox_item(event)
    connection.commit()
    connection.close()

    with pytest.raises(HistoryCleanupError, match="before_delete"):
        cleanup_history(db_path, outbox, workspace)

    assert _deleted_tasks(db_path) == set()


def test_before_delete_failure_rolls_back_all_soft_deletes(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    for index in range(11):
        event, _, _ = _add_candidate(connection, workspace, index)
        outbox[event.key] = _outbox_item(event)
    connection.commit()
    connection.close()
    audits = []

    def reject(report):
        audits.append(report)
        raise RuntimeError("synthetic audit failure")

    with pytest.raises(HistoryCleanupError, match="审计"):
        cleanup_history(db_path, outbox, workspace, before_delete=reject)

    assert len(audits) == 1
    assert audits[0].deleted_count == 1
    assert audits[0].candidate_count == 11
    assert _deleted_tasks(db_path) == set()


def test_non_ascii_group_node_key_matches_native_json_and_legacy_node_remains(
    tmp_path: Path,
):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path, "通知工作区")
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    outbox: dict[str, OutboxItem] = {}
    candidate_task_id = "通知任务-零"
    for index in range(11):
        event, task_id, _ = _add_candidate(
            connection,
            workspace,
            index,
            task_id=candidate_task_id if index == 0 else None,
        )
        outbox[event.key] = _outbox_item(event)
        if index == 0:
            workspace_key, workspace_path = _workspace_values(workspace)
            connection.execute(
                "INSERT INTO task_group_members VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "group-1",
                    workspace_key,
                    workspace_path,
                    None,
                    task_id,
                    0,
                    0,
                    0,
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO task_group_view_node_orders VALUES (?, ?, ?, ?, ?)",
                (
                    "task",
                    json.dumps(
                        [workspace_key, task_id],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    0,
                    0,
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO task_group_view_node_orders VALUES (?, ?, ?, ?, ?)",
                ("task", workspace_key, 999, 999, 999),
            )
    connection.commit()
    connection.close()

    report = cleanup_history(
        db_path,
        outbox,
        workspace,
        before_delete=lambda report: None,
    )

    assert report.deleted_count == 1
    connection = sqlite3.connect(db_path)
    try:
        workspace_key, _ = _workspace_values(workspace)
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders "
            "WHERE node_type = 'task' AND node_key = ?",
            (
                json.dumps(
                    [workspace_key, candidate_task_id],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders "
            "WHERE node_type = 'task' AND node_key = ?",
            (workspace_key,),
        ).fetchone()[0] == 1
    finally:
        connection.close()
