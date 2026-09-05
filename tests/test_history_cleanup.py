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
        title = f"[{event.source}] 通知会话"
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
    allowed = {
        "workspace_key",
        "workspace_path",
        "workspace_identity",
        "running",
        "claimed_at",
        "dispatch_status",
    }
    for field, value in changes.items():
        if field not in allowed:
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
    title: str = "普通会话标题",
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
            title,
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
    task_title: str = "普通会话标题",
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
        title=task_title,
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


def _outbox_item(
    event: Event, *, status: str = "submitted", value: str | None = None
) -> OutboxItem:
    return OutboxItem(
        event=event,
        automation_id=value or automation_id(event.key),
        status=status,  # type: ignore[arg-type]
        submitted_at_ms=event.completed_at_ms if status == "submitted" else None,
    )


def _deleted_tasks(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT task_id FROM tasks WHERE deleted = 1")}


@pytest.mark.parametrize("count, expected_deleted", [(4, 0), (5, 0), (6, 1)])
def test_retention_keeps_global_five(tmp_path: Path, count: int, expected_deleted: int):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(count):
            _add_candidate(connection, workspace, index)
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == expected_deleted
    assert report.candidate_count == count
    assert report.retained_count == min(count, 5)
    assert len(_deleted_tasks(db_path)) == expected_deleted


def test_retention_is_global_across_sources_and_uses_task_time(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(11):
            _add_candidate(connection, workspace, index, source="zcode")
        for index in range(2):
            _add_candidate(connection, workspace, index, source="codex")
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 8
    assert report.retained_count == 5
    assert _deleted_tasks(db_path) == {
        *(f"notification-zcode-{index}" for index in range(6)),
        "notification-codex-0",
        "notification-codex-1",
    }


def test_agent_tagged_history_uses_global_five_without_outbox_or_parent_automation(
    tmp_path: Path,
):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        _, first_task, first_automation = _add_candidate(
            connection,
            workspace,
            0,
            automation_value="custom-schedule-0",
            title="业务调度",
            task_title="[zcode] 通知会话",
        )
        connection.execute(
            "DELETE FROM automations WHERE automation_id=?", (first_automation,)
        )
        for index in range(1, 6):
            _add_candidate(connection, workspace, index)
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 1
    assert _deleted_tasks(db_path) == {first_task}


def test_known_agent_tags_are_required_and_child_or_parent_tag_is_enough(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import _agent_tag_matches, cleanup_history

    assert all(_agent_tag_matches(f"[{tag}] task") for tag in ("codex", "zcode", "claudecode", "dsh"))
    assert not _agent_tag_matches("[business] task")

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        tagged_ids = []
        for index in range(6):
            _, task_id, _ = _add_candidate(connection, workspace, index)
            tagged_ids.append(task_id)
        _, child_tag_task, _ = _add_candidate(
            connection,
            workspace,
            20,
            title="业务调度",
            task_title="[dsh] 子会话",
        )
        _, untagged_task, _ = _add_candidate(
            connection,
            workspace,
            21,
            title="业务调度",
            task_title="普通会话",
        )
        _, no_automation_task, _ = _add_candidate(
            connection,
            workspace,
            22,
            task_id="automation-tnotify-business",
            title="业务调度",
            task_title="[codex] 但没有自动化关联",
        )
        connection.execute(
            "UPDATE tasks SET cron_automation_id=NULL WHERE task_id=?",
            (no_automation_task,),
        )
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 2
    assert tagged_ids[0] in _deleted_tasks(db_path)
    assert child_tag_task not in _deleted_tasks(db_path)
    assert untagged_task not in _deleted_tasks(db_path)
    assert no_automation_task not in _deleted_tasks(db_path)
    assert report.skipped["agent_tag"] >= 1


def test_running_waiting_and_nonterminal_tasks_are_protected(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    outbox: dict[str, OutboxItem] = {}
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
        running_event, running_task, _ = _add_candidate(
            connection, workspace, 20, running=1
        )
        claimed_event, claimed_task, _ = _add_candidate(
            connection, workspace, 21, claimed_at=123
        )
        waiting_event, waiting_task, _ = _add_candidate(
            connection,
            workspace,
            22,
            event_status="awaiting_approval",
        )
        _, nonterminal_task, _ = _add_candidate(
            connection, workspace, 23, task_status="awaiting_input"
        )
        outbox[waiting_event.key] = _outbox_item(waiting_event)
        outbox[running_event.key] = _outbox_item(running_event)
        outbox[claimed_event.key] = _outbox_item(claimed_event)
    report = cleanup_history(
        db_path, outbox, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 1
    assert running_task not in _deleted_tasks(db_path)
    assert claimed_task not in _deleted_tasks(db_path)
    assert waiting_task not in _deleted_tasks(db_path)
    assert nonterminal_task not in _deleted_tasks(db_path)
    assert report.skipped["automation_running"] == 2
    assert report.skipped["event_waiting"] == 1
    assert report.skipped["task_status"] == 1


def test_user_flags_do_not_change_two_condition_ownership(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
        connection.execute(
            "UPDATE tasks SET pinned=1, archived=1, title_overridden=1 WHERE task_id=?",
            ("notification-zcode-0",),
        )
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 1
    assert _deleted_tasks(db_path) == {"notification-zcode-0"}


def test_soft_delete_is_idempotent_and_preserves_automation_runs_and_groups(
    tmp_path: Path,
):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path, "通知工作区")
    source_file = tmp_path / "model-io-source.jsonl"
    source_file.write_text("private source content", encoding="utf-8")
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _, task_id, _ = _add_candidate(
                connection,
                workspace,
                index,
                task_id="通知任务-旧" if index == 0 else None,
            )
            if index == 0:
                workspace_key, workspace_path = _workspace_values(workspace)
                connection.execute(
                    "INSERT INTO task_group_members VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("group", workspace_key, workspace_path, None, task_id, 0, 0, 0, 0),
                )
                connection.execute(
                    "INSERT INTO task_group_view_node_orders VALUES (?, ?, ?, ?, ?)",
                    (
                        "task",
                        json.dumps([workspace_key, task_id], ensure_ascii=False, separators=(",", ":")),
                        0,
                        0,
                        0,
                    ),
                )
                connection.execute(
                    "INSERT INTO task_group_view_node_orders VALUES (?, ?, ?, ?, ?)",
                    ("task", workspace_key, 999, 999, 999),
                )
    first = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    second = cleanup_history(db_path, {}, workspace)
    assert first.deleted_count == 1
    assert second.deleted_count == 0
    assert source_file.read_text(encoding="utf-8") == "private source content"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_members WHERE task_id=?", ("通知任务-旧",)
        ).fetchone()[0] == 0
        exact_node = json.dumps(
            [_workspace_values(workspace)[0], "通知任务-旧"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders WHERE node_key=?",
            (exact_node,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders WHERE node_key=?",
            (_workspace_values(workspace)[0],),
        ).fetchone()[0] == 1
    assert first.skipped["legacy_task_node"] == 1
    assert str(tmp_path) not in repr(first)
    assert "private source content" not in repr(first)


def test_missing_group_tables_are_reported_without_blocking_soft_delete(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path, groups=False)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 1
    assert report.group_cleanup_skipped == 1
    assert report.skipped["groups_missing"] == 1


def test_partial_group_schema_fails_closed_before_soft_delete(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistorySchemaError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
        connection.execute("DROP TABLE task_group_view_node_orders")
        member_count = connection.execute(
            "SELECT COUNT(*) FROM task_group_members"
        ).fetchone()[0]

    with pytest.raises(HistorySchemaError):
        cleanup_history(db_path, {}, workspace, before_delete=lambda report: None)

    assert _deleted_tasks(db_path) == set()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_group_members").fetchone()[0] == member_count


def test_incompatible_schema_fails_closed_before_any_write(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistorySchemaError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path, broken_tasks=True)
    with pytest.raises(HistorySchemaError):
        cleanup_history(db_path, {}, workspace)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_keep_below_five_is_rejected_without_touching_database(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with pytest.raises(ValueError, match="keep"):
        cleanup_history(db_path, {}, workspace, keep=4)
    assert db_path.exists()
    assert _deleted_tasks(db_path) == set()


def test_parameterized_malicious_ids_and_paths_do_not_leak_or_break_schema(
    tmp_path: Path,
):
    from zcode_task_notifier.history_cleanup import cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path, "workspace'; DROP TABLE tasks;--")
    _create_db(db_path)
    malicious_task_id = "notification'; DROP TABLE tasks;--"
    with sqlite3.connect(db_path) as connection:
        _add_candidate(
            connection,
            workspace,
            0,
            task_id=malicious_task_id,
            automation_value="custom'; DROP TABLE automations;--",
        )
        for index in range(1, 6):
            _add_candidate(connection, workspace, index)
    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )
    assert report.deleted_count == 1
    assert malicious_task_id in _deleted_tasks(db_path)
    assert malicious_task_id not in repr(report)
    assert str(workspace) not in repr(report)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 6


def test_missing_history_database_is_not_created(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "missing-tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    with pytest.raises(HistoryCleanupError):
        cleanup_history(db_path, {}, workspace, before_delete=lambda report: None)
    assert not db_path.exists()


def test_delete_requires_before_delete_audit_callback(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
    with pytest.raises(HistoryCleanupError, match="before_delete"):
        cleanup_history(db_path, {}, workspace)
    assert _deleted_tasks(db_path) == set()


def test_before_delete_failure_rolls_back_all_soft_deletes(tmp_path: Path):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
    audits = []

    def reject(report):
        audits.append(report)
        raise RuntimeError("synthetic audit failure")

    with pytest.raises(HistoryCleanupError, match="审计"):
        cleanup_history(db_path, {}, workspace, before_delete=reject)
    assert len(audits) == 1
    assert audits[0].deleted_count == 1
    assert audits[0].candidate_count == 6
    assert _deleted_tasks(db_path) == set()


def test_duplicate_task_identifier_rolls_back_when_update_is_not_one_row(
    tmp_path: Path,
):
    from zcode_task_notifier.history_cleanup import HistoryCleanupError, cleanup_history

    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _add_candidate(connection, workspace, index)
        connection.execute("ALTER TABLE tasks RENAME TO tasks_with_primary_key")
        connection.execute(
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
                off_peak_task_id TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks SELECT * FROM tasks_with_primary_key"
        )
        connection.execute(
            """
            INSERT INTO tasks
            SELECT * FROM tasks_with_primary_key
            WHERE task_id = ?
            """,
            ("notification-zcode-0",),
        )
        connection.execute("DROP TABLE tasks_with_primary_key")

    with pytest.raises(HistoryCleanupError, match="事务"):
        cleanup_history(db_path, {}, workspace, before_delete=lambda report: None)

    assert _deleted_tasks(db_path) == set()
