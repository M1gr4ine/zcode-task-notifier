import json
import os
import sqlite3
from pathlib import Path
import subprocess

import pytest

from zcode_task_notifier.models import RuntimeState
from zcode_task_notifier.zcode_source import (
    ZCodeSchemaError,
    connect_readonly,
    scan_zcode_events,
)


def _create_directory_reparse_point(link: Path, target: Path) -> None:
    """创建并校验目录重解析点；能力不可用时让安全测试失败。"""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.fail(
                "Windows junction capability probe failed: "
                f"{type(exc).__name__}: {exc}"
            )
        if result.returncode != 0:
            pytest.fail(
                "Windows junction capability probe failed "
                f"(exit={result.returncode}): {result.stdout}{result.stderr}"
            )
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(
                "directory symlink capability probe failed: "
                f"{type(exc).__name__}: {exc}"
            )

    try:
        metadata = link.lstat()
    except OSError as exc:
        pytest.fail(
            "created reparse point cannot be inspected: "
            f"{type(exc).__name__}: {exc}"
        )
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    if not link.is_symlink() and not (attributes & 0x400):
        pytest.fail("created directory link is not a symlink or Windows reparse point")


def make_zcode_db(path: Path) -> Path:
    """创建只含合成数据的 ZCode 任务索引。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            session_id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            cron_automation_id TEXT,
            created_at_ms INTEGER,
            started_at_ms INTEGER,
            completed_at_ms INTEGER,
            searchable_text TEXT,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.commit()
    connection.close()
    return path


def make_v2_like_zcode_db(path: Path) -> Path:
    """创建使用真实 v2 列名的合成 ZCode 任务索引。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            title TEXT NOT NULL,
            task_status TEXT NOT NULL,
            cron_automation_id TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            searchable_text TEXT,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.execute(
        """
        INSERT INTO tasks (
            task_id, workspace, title, task_status, cron_automation_id,
            created_at, updated_at, searchable_text, deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-v2-example",
            "workspace/default",
            "v2 合成任务",
            "completed",
            None,
            1767322985000,
            1767323045000,
            "v2 合成摘要",
            0,
        ),
    )
    connection.commit()
    connection.close()
    return path


def insert_task(
    path: Path,
    session_id: str,
    title: str,
    status: str,
    cron_automation_id: str | None,
    started_at_ms: int,
    completed_at_ms: int,
    *,
    workspace: str = "workspace-example",
    deleted: int = 0,
    searchable_text: str = "合成任务摘要",
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO tasks (
            session_id, workspace, title, status, cron_automation_id,
            created_at_ms, started_at_ms, completed_at_ms, searchable_text, deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            workspace,
            title,
            status,
            cron_automation_id,
            started_at_ms,
            started_at_ms,
            completed_at_ms,
            searchable_text,
            deleted,
        ),
    )
    connection.commit()
    connection.close()


def append_model_io(
    rollout_dir: Path,
    session_id: str,
    turn_id: str,
    completed_at: str,
    *,
    searchable_text: str = "合成最终回答",
    newline: bool = True,
    **extra: object,
) -> Path:
    rollout_dir.mkdir(parents=True, exist_ok=True)
    path = rollout_dir / f"model-io-{session_id}.jsonl"
    payload: dict[str, object] = {
        "type": "model_io",
        "querySource": "main_turn",
        "turnId": turn_id,
        "completedAt": completed_at,
        "searchable_text": searchable_text,
        "request": {"text": "synthetic request"},
        "response": {"text": searchable_text},
    }
    payload.update(extra)
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False))
        if newline:
            stream.write("\n")
    return path


def test_scans_completed_tasks_from_every_workspace(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-a", "工作区甲", "completed", None, 1000, 61000, workspace="workspace-a")
    insert_task(db, "session-b", "工作区乙", "completed", None, 2000, 122000, workspace="workspace-b")
    append_model_io(rollout, "session-a", "turn-a", "2026-01-02T03:04:05Z")
    append_model_io(rollout, "session-b", "turn-b", "2026-01-02T03:05:05Z")

    events, _, turns = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert [event.task_id for event in events] == ["session-a", "session-b"]
    assert all(event.source == "zcode" for event in events)
    assert turns == {"session-a": "turn-a", "session-b": "turn-b"}


def test_v2_task_schema_uses_task_status_and_updated_at_without_rollout_file(
    tmp_path: Path,
):
    db = make_v2_like_zcode_db(tmp_path / "tasks.sqlite")

    events, _, _ = scan_zcode_events(
        db, tmp_path / "rollout", RuntimeState(), baseline=False
    )

    assert len(events) == 1
    assert events[0].task_id == "task-v2-example"
    assert events[0].completed_at_ms == 1767323045000
    assert events[0].duration_ms == 60000
    assert events[0].summary_text == "v2 合成摘要"
    assert events[0].key.startswith("zcode:task-v2-example:")


def test_automation_sessions_are_never_notified(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    insert_task(
        db,
        "generated",
        "通知任务",
        "completed",
        "automation-tnotify-zcode-example",
        1000,
        2000,
    )

    events, _, _ = scan_zcode_events(
        db, tmp_path / "rollout", RuntimeState(initialized=True), baseline=False
    )

    assert events == []


def test_baseline_advances_cursors_without_emitting(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "历史任务", "completed", None, 1000, 2000)
    model_io = append_model_io(
        rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z"
    )

    events, offsets, turns = scan_zcode_events(
        db, rollout, RuntimeState(), baseline=True
    )

    assert events == []
    assert offsets[str(model_io.resolve())] == model_io.stat().st_size
    assert turns == {"session-example": "turn-example"}


def test_baseline_marks_existing_turn_so_next_scan_does_not_replay_it(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "历史任务", "completed", None, 1000, 61000)
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z")
    state = RuntimeState()

    events, offsets, turns = scan_zcode_events(db, rollout, state, baseline=True)
    state.zcode_rollout_offsets = offsets
    state.zcode_last_turns = turns

    assert events == []
    assert "zcode:session-example:turn-example" in state.seen_event_keys
    assert scan_zcode_events(db, rollout, state, baseline=False)[0] == []


def test_error_task_emits_error_status_and_same_key_only_once(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-error", "失败任务", "error", None, 1000, 2000)
    append_model_io(rollout, "session-error", "turn-error", "2026-01-02T03:04:05Z")
    state = RuntimeState(initialized=True)

    first, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.seen_event_keys.add(first[0].key)
    state.zcode_rollout_offsets = offsets
    state.zcode_last_turns = turns
    second, _, _ = scan_zcode_events(db, rollout, state, baseline=False)

    assert first[0].status == "error"
    assert second == []


def test_same_completed_session_emits_each_new_turn(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "连续提问", "completed", None, 1000, 2000)
    append_model_io(rollout, "session-example", "turn-first", "2026-01-02T03:04:05Z")
    state = RuntimeState(initialized=True)

    first, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.seen_event_keys.add(first[0].key)
    state.zcode_rollout_offsets = offsets
    state.zcode_last_turns = turns

    append_model_io(rollout, "session-example", "turn-second", "2026-01-02T03:10:05Z")
    second, _, _ = scan_zcode_events(db, rollout, state, baseline=False)

    assert [event.key for event in first] == ["zcode:session-example:turn-first"]
    assert [event.key for event in second] == ["zcode:session-example:turn-second"]


def test_one_incremental_scan_emits_each_distinct_turn_in_file_order(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "多个回合", "completed", None, 1000, 2000)
    append_model_io(rollout, "session-example", "turn-first", "2026-01-02T03:04:05Z")
    append_model_io(rollout, "session-example", "turn-second", "2026-01-02T03:05:05Z")

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert [event.turn_id for event in events] == ["turn-first", "turn-second"]
    assert [event.key for event in events] == [
        "zcode:session-example:turn-first",
        "zcode:session-example:turn-second",
    ]


def test_same_turn_multiple_model_calls_emits_last_record_once(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "多次调用", "completed", None, 1000, 2000)
    append_model_io(
        rollout,
        "session-example",
        "turn-same",
        "2026-01-02T03:04:05Z",
        searchable_text="第一次回答",
    )
    append_model_io(
        rollout,
        "session-example",
        "turn-same",
        "2026-01-02T03:04:06Z",
        searchable_text="最后一次回答",
    )

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert len(events) == 1
    assert events[0].completed_at_ms == 1767323046000
    assert events[0].duration_ms == 1000
    assert events[0].summary_text == "最后一次回答"


def test_running_turn_is_held_until_task_is_terminal(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "运行后完成", "running", None, 1000, 2000)
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z")
    state = RuntimeState(initialized=True)

    first, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.zcode_rollout_offsets = offsets
    state.zcode_last_turns = turns
    assert first == []

    connection = sqlite3.connect(db)
    connection.execute("UPDATE tasks SET status = 'completed' WHERE session_id = ?", ("session-example",))
    connection.commit()
    connection.close()

    second, _, _ = scan_zcode_events(db, rollout, state, baseline=False)
    assert [event.key for event in second] == ["zcode:session-example:turn-example"]


def test_running_scan_keeps_offset_so_all_accumulated_turns_emit_after_terminal(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "运行期间多回合", "running", None, 1000, 2000)
    model_io = append_model_io(
        rollout, "session-example", "turn-first", "2026-01-02T03:04:05Z"
    )
    append_model_io(
        rollout, "session-example", "turn-second", "2026-01-02T03:05:05Z"
    )
    state = RuntimeState(initialized=True)

    running_events, running_offsets, running_turns = scan_zcode_events(
        db, rollout, state, baseline=False
    )
    state.zcode_rollout_offsets = running_offsets
    state.zcode_last_turns = running_turns

    assert running_events == []
    assert running_offsets.get(str(model_io.resolve()), 0) == 0

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET status = 'completed' WHERE session_id = ?",
        ("session-example",),
    )
    connection.commit()
    connection.close()

    terminal_events, terminal_offsets, _ = scan_zcode_events(
        db, rollout, state, baseline=False
    )
    assert [event.turn_id for event in terminal_events] == [
        "turn-first",
        "turn-second",
    ]
    assert terminal_offsets[str(model_io.resolve())] == model_io.stat().st_size


def test_running_replacement_resets_offset_before_terminal_without_duplicate_seen(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-running-replaced", "运行中替换", "completed", None, 1000, 2000)
    model_io = append_model_io(
        rollout,
        "session-running-replaced",
        "turn-old",
        "2026-01-02T03:04:05Z",
    )
    state = RuntimeState(initialized=True)

    first, offsets, _ = scan_zcode_events(db, rollout, state, baseline=False)
    state.seen_event_keys.update(event.key for event in first)
    state.zcode_rollout_offsets = offsets
    old_size = model_io.stat().st_size

    model_io.unlink()
    replacement = append_model_io(
        rollout,
        "session-running-replaced",
        "turn-new",
        "2026-01-02T03:05:05Z",
        searchable_text="新文件中的新回合" + ("扩展" * old_size),
    )
    assert replacement == model_io
    assert replacement.stat().st_size > old_size
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET status = 'running' WHERE session_id = ?",
        ("session-running-replaced",),
    )
    connection.commit()
    connection.close()

    running_events, running_offsets, _ = scan_zcode_events(
        db, rollout, state, baseline=False
    )
    assert running_events == []
    assert running_offsets[str(model_io.resolve())] == 0
    state.zcode_rollout_offsets = running_offsets

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET status = 'completed' WHERE session_id = ?",
        ("session-running-replaced",),
    )
    connection.commit()
    connection.close()

    terminal_events, terminal_offsets, _ = scan_zcode_events(
        db, rollout, state, baseline=False
    )
    assert [event.turn_id for event in terminal_events] == ["turn-new"]
    assert terminal_offsets[str(model_io.resolve())] == model_io.stat().st_size
    state.seen_event_keys.update(event.key for event in terminal_events)
    state.zcode_rollout_offsets = terminal_offsets
    assert scan_zcode_events(db, rollout, state, baseline=False)[0] == []


def test_truncated_last_line_does_not_advance_cursor(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "半行", "completed", None, 1000, 2000)
    model_io = append_model_io(
        rollout, "session-example", "turn-first", "2026-01-02T03:04:05Z"
    )
    state = RuntimeState(initialized=True)
    first, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.seen_event_keys.add(first[0].key)
    state.zcode_rollout_offsets = offsets
    state.zcode_last_turns = turns
    old_offset = offsets[str(model_io.resolve())]

    append_model_io(
        rollout,
        "session-example",
        "turn-second",
        "2026-01-02T03:10:05Z",
        newline=False,
    )
    no_event, new_offsets, new_turns = scan_zcode_events(db, rollout, state, baseline=False)

    assert no_event == []
    assert new_offsets[str(model_io.resolve())] == old_offset
    assert new_turns["session-example"] == "turn-first"

    with model_io.open("ab") as stream:
        stream.write(b"\n")
    event, _, _ = scan_zcode_events(db, rollout, state, baseline=False)
    assert [item.key for item in event] == ["zcode:session-example:turn-second"]


def test_stale_negative_offset_is_clamped_and_file_is_read_from_start(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "异常游标", "completed", None, 1000, 2000)
    model_io = append_model_io(
        rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z"
    )
    state = RuntimeState(
        initialized=True,
        zcode_rollout_offsets={str(model_io.resolve()): -1},
    )

    events, offsets, _ = scan_zcode_events(db, rollout, state, baseline=False)

    assert [event.key for event in events] == ["zcode:session-example:turn-example"]
    assert offsets[str(model_io.resolve())] == model_io.stat().st_size


def test_same_model_io_path_replaced_by_larger_file_is_read_from_start(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-replaced", "替换文件", "completed", None, 1000, 2000)
    model_io = append_model_io(
        rollout, "session-replaced", "turn-old", "2026-01-02T03:04:05Z"
    )
    state = RuntimeState(initialized=True)

    first, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.seen_event_keys.update(event.key for event in first)
    state.zcode_rollout_offsets = offsets
    state.zcode_last_turns = turns
    old_size = model_io.stat().st_size

    replacement = json.dumps(
        {
            "type": "model_io",
            "querySource": "main_turn",
            "turnId": "turn-new",
            "completedAt": "2026-01-02T03:05:05Z",
            "searchable_text": "新文件中的新回合" + ("扩展" * old_size),
        },
        ensure_ascii=False,
    )
    model_io.unlink()
    model_io.write_text(replacement + "\n", encoding="utf-8")
    assert model_io.stat().st_size > old_size

    events, new_offsets, _ = scan_zcode_events(
        db, rollout, state, baseline=False
    )

    assert [event.turn_id for event in events] == ["turn-new"]
    assert new_offsets[str(model_io.resolve())] == model_io.stat().st_size


def test_model_io_read_error_is_reported_without_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-io-error", "读取失败", "completed", None, 1000, 2000)
    model_io = append_model_io(
        rollout, "session-io-error", "turn-io-error", "2026-01-02T03:04:05Z"
    )
    original_open = Path.open

    def fail_model_io(path: Path, *args: object, **kwargs: object):
        if path == model_io:
            raise PermissionError("private absolute path")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_model_io)

    with pytest.raises(ZCodeSchemaError, match="model-io") as exc_info:
        scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)

    assert str(tmp_path) not in str(exc_info.value)
    assert "private absolute path" not in str(exc_info.value)


@pytest.mark.parametrize("cron_id", ["", "   ", 0])
def test_any_non_null_cron_automation_id_is_filtered(
    tmp_path: Path, cron_id: object
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(
        db,
        "session-example",
        "定时任务",
        "completed",
        cron_id,  # type: ignore[arg-type]
        1000,
        2000,
    )
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z")

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert events == []


def test_rollout_missing_uses_compatibility_notification_only_once(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    insert_task(db, "legacy-session", "旧版任务", "completed", None, 1000, 2000)
    rollout = tmp_path / "rollout"

    first, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )
    state = RuntimeState(initialized=True, seen_event_keys={first[0].key})
    second, _, _ = scan_zcode_events(db, rollout, state, baseline=False)

    assert len(first) == 1
    assert first[0].key.startswith("zcode:legacy-session:")
    assert second == []


def test_legacy_terminal_key_upgrade_suppresses_current_version_and_registers_key(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    insert_task(
        db,
        "legacy-upgrade",
        "升级任务",
        "completed",
        None,
        1000,
        2000,
        searchable_text="当前完成摘要",
    )
    state = RuntimeState(
        initialized=True,
        seen_event_keys={"zcode:legacy-upgrade:legacy-terminal"},
    )

    first, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)

    assert first == []
    assert "zcode:legacy-upgrade:legacy-terminal" in state.seen_event_keys
    current_keys = {
        key for key in state.seen_event_keys if key.startswith("zcode:legacy-upgrade:legacy:")
    }
    assert len(current_keys) == 1
    seen_after_upgrade = set(state.seen_event_keys)

    second, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)

    assert second == []
    assert state.seen_event_keys == seen_after_upgrade

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET completed_at_ms = ?, searchable_text = ? WHERE session_id = ?",
        (3000, "下一次完成摘要", "legacy-upgrade"),
    )
    connection.commit()
    connection.close()

    changed, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)

    assert len(changed) == 1
    assert changed[0].key not in current_keys
    assert changed[0].summary_text == "下一次完成摘要"


def test_legacy_terminal_alias_does_not_suppress_modern_rollout_turn(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(
        db,
        "modern-after-upgrade",
        "现代任务",
        "completed",
        None,
        1000,
        2000,
    )
    append_model_io(
        rollout,
        "modern-after-upgrade",
        "turn-modern",
        "2026-01-02T03:04:05Z",
    )
    state = RuntimeState(
        initialized=True,
        seen_event_keys={"zcode:modern-after-upgrade:legacy-terminal"},
    )

    events, _, _ = scan_zcode_events(db, rollout, state, baseline=False)

    assert len(events) == 1
    assert events[0].key == "zcode:modern-after-upgrade:turn-modern"


def test_missing_rollout_version_ignores_title_only_change_and_tracks_summary_change(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    insert_task(
        db,
        "legacy-session",
        "旧版任务",
        "completed",
        None,
        1000,
        2000,
        searchable_text="首次摘要",
    )
    state = RuntimeState(initialized=True)
    first, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)
    state.seen_event_keys.add(first[0].key)

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET title = ? WHERE session_id = ?",
        ("只改标题", "legacy-session"),
    )
    connection.commit()
    connection.close()

    title_only, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)

    assert title_only == []

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET status = ?, completed_at_ms = ?, searchable_text = ? WHERE session_id = ?",
        ("error", 3000, "变化后的摘要", "legacy-session"),
    )
    connection.commit()
    connection.close()

    second, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)

    assert first[0].key.startswith("zcode:legacy-session:")
    assert second[0].key.startswith("zcode:legacy-session:")
    assert second[0].key != first[0].key
    assert "首次摘要" not in first[0].key
    assert "变化后的摘要" not in second[0].key


def test_missing_rollout_baseline_consumes_only_current_completion_version(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    insert_task(
        db,
        "legacy-baseline",
        "历史任务",
        "completed",
        None,
        1000,
        2000,
        searchable_text="安装前摘要",
    )
    state = RuntimeState()

    baseline, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=True)

    assert baseline == []
    assert len(state.seen_event_keys) == 1

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE tasks SET completed_at_ms = ?, searchable_text = ? WHERE session_id = ?",
        (3000, "安装后摘要", "legacy-baseline"),
    )
    connection.commit()
    connection.close()

    events, _, _ = scan_zcode_events(db, tmp_path / "rollout", state, baseline=False)

    assert len(events) == 1
    assert events[0].summary_text == "安装后摘要"


def test_summary_is_limited_to_last_6000_unicode_characters(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "长摘要", "completed", None, 1000, 2000)
    text = "前" * 7000 + "尾" * 100
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z", searchable_text=text)

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert len(events[0].summary_text) == 6000
    assert events[0].summary_text == text[-6000:]


def test_missing_database_is_not_created(tmp_path: Path):
    path = tmp_path / "missing.sqlite"

    with pytest.raises((sqlite3.OperationalError, ZCodeSchemaError)):
        with connect_readonly(path):
            pass

    assert not path.exists()


def test_missing_required_tasks_column_raises_schema_error(tmp_path: Path):
    path = tmp_path / "tasks.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tasks (session_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(ZCodeSchemaError):
        scan_zcode_events(path, None, RuntimeState(initialized=True), baseline=False)


def test_invalid_model_io_records_are_ignored(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-example", "无效记录", "completed", None, 1000, 2000)
    append_model_io(rollout, "session-example", "", "2026-01-02T03:04:05Z")
    append_model_io(
        rollout,
        "session-example",
        "turn-wrong-source",
        "2026-01-02T03:04:05Z",
        querySource="background",
    )
    append_model_io(
        rollout,
        "session-example",
        "turn-valid",
        "2026-01-02T03:04:05Z",
    )

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert [event.turn_id for event in events] == ["turn-valid"]


def test_corrupt_model_io_file_does_not_block_other_sessions_and_is_diagnosed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-bad-json", "坏文件", "completed", None, 1000, 2000)
    insert_task(db, "session-good-json", "好文件", "completed", None, 1000, 2000)
    bad_path = rollout / "model-io-session-bad-json.jsonl"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text("{not-json}\n", encoding="utf-8")
    append_model_io(
        rollout, "session-good-json", "turn-good-json", "2026-01-02T03:04:05Z"
    )

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert [event.turn_id for event in events] == ["turn-good-json"]
    assert "zcode model-io 文件处理失败" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_extreme_model_io_numbers_are_ignored_without_blocking_other_sessions(
    tmp_path: Path,
):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-bad", "异常数字", "completed", None, 1000, 2000)
    insert_task(db, "session-good", "正常任务", "completed", None, 1000, 2000)
    append_model_io(
        rollout,
        "session-bad",
        "turn-bad",
        10**1000,  # type: ignore[arg-type]
        durationMs=10**1000,
    )
    append_model_io(
        rollout,
        "session-good",
        "turn-good",
        "2026-01-02T03:04:05Z",
    )

    events, _, _ = scan_zcode_events(
        db, rollout, RuntimeState(initialized=True), baseline=False
    )

    assert [event.key for event in events] == ["zcode:session-good:turn-good"]


def test_model_io_reparse_point_outside_rollout_root_is_rejected(tmp_path: Path):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    outside = tmp_path / "outside-rollout"
    outside.mkdir()
    insert_task(db, "session-escape", "越界文件", "completed", None, 1000, 2000)
    rollout.mkdir()
    reparse_model_io = rollout / "model-io-session-escape.jsonl"
    _create_directory_reparse_point(reparse_model_io, outside)

    with pytest.raises(ZCodeSchemaError, match="rollout"):
        scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
    assert not list(outside.iterdir())


def test_model_io_reparse_point_is_rejected_by_helper_and_other_tasks_continue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    import zcode_task_notifier.zcode_source as source

    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-reparse", "重解析文件", "completed", None, 1000, 2000)
    insert_task(db, "session-safe", "安全文件", "completed", None, 1000, 2000)
    bad_path = append_model_io(
        rollout, "session-reparse", "turn-reparse", "2026-01-02T03:04:05Z"
    )
    append_model_io(rollout, "session-safe", "turn-safe", "2026-01-02T03:05:05Z")
    real_is_reparse_point = source._is_reparse_point

    def fake_is_reparse_point(path: Path) -> bool:
        if Path(path) == bad_path:
            return True
        return real_is_reparse_point(path)

    monkeypatch.setattr(source, "_is_reparse_point", fake_is_reparse_point)

    events, _, _ = scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)

    assert [event.turn_id for event in events] == ["turn-safe"]
    assert "zcode model-io 文件处理失败" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_model_io_reparse_root_is_rejected_via_lstat_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import zcode_task_notifier.zcode_source as source

    db = make_zcode_db(tmp_path / "tasks.sqlite")
    rollout = tmp_path / "rollout"
    insert_task(db, "session-reparse-root", "重解析根", "completed", None, 1000, 2000)
    append_model_io(rollout, "session-reparse-root", "turn-root", "2026-01-02T03:04:05Z")
    real_is_reparse_point = source._is_reparse_point

    def fake_is_reparse_point(path: Path) -> bool:
        if Path(path) == rollout:
            return True
        return real_is_reparse_point(path)

    monkeypatch.setattr(source, "_is_reparse_point", fake_is_reparse_point)

    with pytest.raises(ZCodeSchemaError, match="rollout"):
        scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
