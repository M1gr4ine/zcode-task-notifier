import sqlite3
from pathlib import Path

from zcode_task_notifier.history_cleanup import cleanup_history

from test_history_cleanup import (
    _add_candidate,
    _create_db,
    _deleted_tasks,
    _outbox_item,
    _workspace,
)


_TEMPLATE_1 = "你是任务停顿通知摘要助手。 只概括下面这一个停顿事件，不扫描或混入其他任务。"
_TEMPLATE_2 = "你是任务完成通知摘要助手。 只概括下面这一个完成事件，不扫描或混入其他任务。"
_TEMPLATE_3 = "ZCode 任务完成通知自动化。以下任务有状态更新，请生成微信通知。"


def test_fallback_accepts_fixed_templates_and_all_notification_id_formats_after_parent_removed(
    tmp_path: Path,
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    rows = [
        (
            "automation-tnotify-000000000000000000000001",
            "你是任务停顿通知摘要助手。\r\n\t只概括下面这一个停顿事件，不扫描或混入其他任务。 附加内容",
        ),
        (
            "automation-tnotify-zcode-000000000000000000000002",
            "你是任务完成通知摘要助手。\n 只概括下面这一个完成事件，不扫描或混入其他任务。 附加内容",
        ),
        (
            "automation-tnotify-codex-000000000000000000000003",
            "ZCode 任务完成通知自动化。\t以下任务有状态更新，请生成微信通知。 附加内容",
        ),
        (
            "automation-tnotify-1234567890123",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-000000000000000000000004",
            _TEMPLATE_2 + " 附加内容",
        ),
        (
            "automation-tnotify-zcode-000000000000000000000005",
            _TEMPLATE_3 + " 附加内容",
        ),
        (
            "automation-tnotify-codex-000000000000000000000006",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-1234567890124",
            _TEMPLATE_2 + " 附加内容",
        ),
        (
            "automation-tnotify-000000000000000000000007",
            _TEMPLATE_3 + " 附加内容",
        ),
        (
            "automation-tnotify-zcode-000000000000000000000008",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-codex-000000000000000000000009",
            _TEMPLATE_2 + " 附加内容",
        ),
        (
            "automation-tnotify-1234567890125",
            _TEMPLATE_3 + " 附加内容",
        ),
    ]
    with sqlite3.connect(db_path) as connection:
        for index, (automation_value, task_title) in enumerate(rows):
            _add_candidate(
                connection,
                workspace,
                index,
                task_id=f"fallback-{index}",
                title="普通自动化",
                task_title=task_title,
                automation_value=automation_value,
            )
        connection.execute("DELETE FROM automations")

    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )

    assert report.candidate_count == 12
    assert report.retained_count == 5
    assert report.deleted_count == 7
    assert _deleted_tasks(db_path) == {f"fallback-{index}" for index in range(7)}


def test_agent_tags_and_fallback_share_one_global_five_retention(tmp_path: Path):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    fallback_ids = [
        "automation-tnotify-000000000000000000000011",
        "automation-tnotify-zcode-000000000000000000000012",
        "automation-tnotify-codex-000000000000000000000013",
    ]
    with sqlite3.connect(db_path) as connection:
        for index in range(3):
            _add_candidate(
                connection,
                workspace,
                index,
                task_id=f"tagged-{index}",
                task_title="[zcode] 历史通知会话",
            )
        for index, automation_value in enumerate(fallback_ids, 3):
            _add_candidate(
                connection,
                workspace,
                index,
                task_id=f"fallback-{index}",
                title="普通自动化",
                task_title=_TEMPLATE_1 + " 附加内容",
                automation_value=automation_value,
            )
        connection.execute(
            "DELETE FROM automations WHERE automation_id IN (?, ?, ?)",
            fallback_ids,
        )

    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )

    assert report.candidate_count == 6
    assert report.retained_count == 5
    assert report.deleted_count == 1
    assert _deleted_tasks(db_path) == {"tagged-0"}


def test_fallback_rejects_bad_ids_missing_cron_and_nonprefix_titles(tmp_path: Path):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    accepted_ids = [
        "automation-tnotify-000000000000000000000021",
        "automation-tnotify-zcode-000000000000000000000022",
        "automation-tnotify-codex-000000000000000000000023",
        "automation-tnotify-1234567890126",
        "automation-tnotify-000000000000000000000024",
    ]
    invalid_rows = [
        (
            "automation-tnotify-abcdef",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-00000000000000000000000A",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-zcode-00000000000000000000002",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-codex-0000000000000000000000000",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-12345678901234",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-000000000000000000000025",
            "引用：" + _TEMPLATE_1,
        ),
        (
            "automation-tnotify-zcode-000000000000000000000026",
            "普通任务引用通知模板但不是模板起始",
        ),
        (
            "automation-tnotify-codex-000000000000000000000027",
            "普通任务引用通知模板但不是模板起始",
        ),
        (
            "zcode-000000000000000000000029",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "codex-000000000000000000000030",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "1234567890127",
            _TEMPLATE_1 + " 附加内容",
        ),
        (
            "automation-tnotify-000000000000000000000031",
            "你是任务停顿通知摘要助手。",
        ),
        (
            "automation-tnotify-zcode-000000000000000000000032",
            "你是任务完成通知摘要助手。",
        ),
        (
            "automation-tnotify-codex-000000000000000000000033",
            "ZCode 任务完成通知自动化。",
        ),
    ]
    with sqlite3.connect(db_path) as connection:
        for index, automation_value in enumerate(accepted_ids):
            _add_candidate(
                connection,
                workspace,
                index,
                task_id=f"accepted-{index}",
                title="普通自动化",
                task_title=_TEMPLATE_1 + " 附加内容",
                automation_value=automation_value,
            )
        for index, (automation_value, task_title) in enumerate(invalid_rows, 10):
            _add_candidate(
                connection,
                workspace,
                index,
                task_id=f"invalid-{index}",
                title="普通自动化",
                task_title=task_title,
                automation_value=automation_value,
            )
        _add_candidate(
            connection,
            workspace,
            30,
            task_id="invalid-no-cron",
            title="普通自动化",
            task_title=_TEMPLATE_1 + " 附加内容",
            automation_value="automation-tnotify-000000000000000000000028",
        )
        connection.execute("UPDATE tasks SET cron_automation_id = NULL WHERE task_id = ?", ("invalid-no-cron",))
        connection.execute("DELETE FROM automations")

    report = cleanup_history(
        db_path, {}, workspace, before_delete=lambda report: None
    )

    assert report.candidate_count == 5
    assert report.retained_count == 5
    assert report.deleted_count == 0
    assert _deleted_tasks(db_path) == set()


def test_fallback_keeps_running_waiting_and_foreign_workspace_rows_protected(
    tmp_path: Path,
):
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    foreign_workspace = _workspace(tmp_path, "foreign-workspace")
    _create_db(db_path)
    waiting_outbox = {}
    fallback_task_ids = []
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            _, task_id, automation_value = _add_candidate(
                connection,
                workspace,
                index,
                task_id=f"fallback-{index}",
                title="普通自动化",
                task_title=_TEMPLATE_1 + " 附加内容",
            )
            fallback_task_ids.append(task_id)
            connection.execute(
                "DELETE FROM automations WHERE automation_id = ?", (automation_value,)
            )
        running_event, running_task, _ = _add_candidate(
            connection,
            workspace,
            20,
            task_id="running-fallback",
            title="业务调度",
            task_title=_TEMPLATE_1 + " 附加内容",
            running=1,
        )
        claimed_event, claimed_task, _ = _add_candidate(
            connection,
            workspace,
            21,
            task_id="claimed-fallback",
            title="业务调度",
            task_title=_TEMPLATE_1 + " 附加内容",
            claimed_at=123,
        )
        waiting_event, waiting_task, _ = _add_candidate(
            connection,
            workspace,
            22,
            task_id="waiting-fallback",
            title="业务调度",
            task_title=_TEMPLATE_1 + " 附加内容",
            event_status="awaiting_approval",
        )
        waiting_outbox[waiting_event.key] = _outbox_item(waiting_event)
        _, identity_task, _ = _add_candidate(
            connection,
            workspace,
            23,
            task_id="identity-fallback",
            title="普通自动化",
            task_title=_TEMPLATE_1 + " 附加内容",
        )
        connection.execute(
            "UPDATE tasks SET workspace_identity = ? WHERE task_id = ?",
            ("foreign-identity", identity_task),
        )
        _, foreign_task, _ = _add_candidate(
            connection,
            foreign_workspace,
            24,
            task_id="foreign-workspace-fallback",
            title="普通自动化",
            task_title=_TEMPLATE_1 + " 附加内容",
        )

    report = cleanup_history(
        db_path, waiting_outbox, workspace, before_delete=lambda report: None
    )

    assert report.candidate_count == 6
    assert report.retained_count == 5
    assert report.deleted_count == 1
    assert _deleted_tasks(db_path) == {fallback_task_ids[0]}
    assert running_task not in _deleted_tasks(db_path)
    assert claimed_task not in _deleted_tasks(db_path)
    assert waiting_task not in _deleted_tasks(db_path)
    assert identity_task not in _deleted_tasks(db_path)
    assert foreign_task not in _deleted_tasks(db_path)
    assert report.skipped["automation_running"] == 2
    assert report.skipped["event_waiting"] == 1
