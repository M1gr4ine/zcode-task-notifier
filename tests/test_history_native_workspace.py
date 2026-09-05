import json
import sqlite3

import pytest

from test_history_cleanup import _add_candidate, _create_db, _deleted_tasks, _outbox_item, _workspace
from zcode_task_notifier.history_cleanup import cleanup_history


@pytest.mark.parametrize("parent_native", [False, True])
def test_native_task_key_is_used_for_soft_delete_and_group_cleanup(tmp_path, parent_native):
    """原生完整 workspace key 参与软删除和分组清理，旧 legacy 节点不误删。"""
    workspace = _workspace(tmp_path)
    database = tmp_path / "tasks.sqlite"
    _create_db(database)
    outbox = {}
    native_key = str(workspace)
    with sqlite3.connect(database) as conn:
        for number in range(6):
            event, task_id, automation_value = _add_candidate(conn, workspace, number)
            outbox[event.key] = _outbox_item(event)
            if number == 0:
                conn.execute(
                    "INSERT INTO task_group_members VALUES ('group', ?, ?, NULL, ?, 0, 0, 0, 0)",
                    (native_key, str(workspace), task_id),
                )
                conn.execute(
                    "INSERT INTO task_group_members VALUES ('group', ?, ?, NULL, ?, 0, 0, 0, 0)",
                    (workspace.name, str(workspace), task_id + "-legacy"),
                )
                conn.execute(
                    "INSERT INTO task_group_view_node_orders VALUES ('task', ?, 0, 0, 0)",
                    (json.dumps([native_key, task_id], separators=(",", ":")),),
                )
                conn.execute(
                    "INSERT INTO task_group_view_node_orders VALUES ('task', ?, 0, 0, 0)",
                    (native_key,),
                )
            conn.execute(
                "UPDATE tasks SET workspace_key=? WHERE task_id=?",
                (native_key, task_id),
            )
            if parent_native:
                conn.execute(
                    "UPDATE automations SET workspace_key=? WHERE automation_id=?",
                    (native_key, automation_value),
                )
    report = cleanup_history(database, outbox, workspace, before_delete=lambda _: None)
    assert report.deleted_count == 1
    assert _deleted_tasks(database) == {"notification-zcode-0"}
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT task_id FROM tasks WHERE deleted=1"
        ).fetchall() == [("notification-zcode-0",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_group_members WHERE workspace_key=? AND task_id=?",
            (native_key, "notification-zcode-0"),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders WHERE node_key=?",
            (json.dumps([native_key, "notification-zcode-0"], separators=(",", ":")),),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_group_view_node_orders WHERE node_key=?",
            (native_key,),
        ).fetchone()[0] == 1


def test_same_path_accepts_key_variants_but_rejects_unknown_workspace_identity(tmp_path):
    workspace = _workspace(tmp_path)
    database = tmp_path / "tasks.sqlite"
    _create_db(database)
    outbox = {}
    with sqlite3.connect(database) as conn:
        for number in range(6):
            event, _, _ = _add_candidate(conn, workspace, number)
            outbox[event.key] = _outbox_item(event)
        conn.execute("UPDATE tasks SET workspace_key='foreign-key'")
    report = cleanup_history(database, outbox, workspace, before_delete=lambda _: None)
    assert report.deleted_count == 1

    database = tmp_path / "identity.sqlite"
    _create_db(database)
    outbox = {}
    with sqlite3.connect(database) as conn:
        for number in range(6):
            event, _, _ = _add_candidate(conn, workspace, number)
            outbox[event.key] = _outbox_item(event)
        conn.execute("UPDATE tasks SET workspace_identity='foreign-identity'")
    report = cleanup_history(database, outbox, workspace, before_delete=lambda _: None)
    assert report.deleted_count == 0
    assert _deleted_tasks(database) == set()
