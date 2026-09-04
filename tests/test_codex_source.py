import json
import sqlite3
from pathlib import Path

import pytest

from zcode_task_notifier.models import RuntimeState
from zcode_task_notifier.codex_source import (
    CodexSourceError,
    RolloutRef,
    backfill_codex_thread,
    discover_rollouts,
    scan_codex_events,
)


def make_codex_layout(tmp_path: Path, thread_id: str):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    state_db = codex_home / "state_example.sqlite"
    rollout = sessions / f"rollout-{thread_id}-first.jsonl"
    connection = sqlite3.connect(state_db)
    connection.execute(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            title TEXT,
            rollout_path TEXT,
            thread_source TEXT,
            cwd TEXT,
            project TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, "合成 Codex 任务", str(rollout), "user", "任意 cwd", "任意 project"),
    )
    connection.commit()
    connection.close()
    return codex_home, state_db, rollout


def append_jsonl(path: Path, record: dict[str, object], *, newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(record, ensure_ascii=False))
        if newline:
            stream.write("\n")


def complete_record(
    turn_id: str | None,
    message: str,
    timestamp: str = "2026-01-02T03:04:05Z",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "task_complete",
        "last_agent_message": message,
    }
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def started_record(turn_id: str, timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": turn_id},
    }


def mark_seen(state: RuntimeState, events: list[object]) -> None:
    state.seen_event_keys.update(event.key for event in events)  # type: ignore[attr-defined]


def make_history_layout(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-history")
    rollout.unlink(missing_ok=True)
    history_db = codex_home / "thread_history_example.sqlite"
    connection = sqlite3.connect(history_db)
    connection.execute(
        """
        CREATE TABLE thread_history (
            row_id INTEGER PRIMARY KEY,
            thread_id TEXT NOT NULL,
            turn_id TEXT,
            status TEXT NOT NULL,
            completed_at TEXT,
            final_message TEXT,
            thread_source TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO thread_history VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "thread-history", "turn-history", "completed", "2026-01-02T03:04:05Z", "历史最终答案", "user"),
    )
    connection.commit()
    connection.close()
    return codex_home, state_db, history_db


def test_task_complete_is_emitted_from_current_rollout(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(
        rollout,
        {
            "timestamp": "2026-01-02T03:04:05Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-example",
                "last_agent_message": "合成的最终结论",
            },
        },
    )

    events, offsets, _ = scan_codex_events(
        codex_home, state_db, None, RuntimeState(initialized=True), baseline=False
    )

    assert [(event.task_id, event.turn_id) for event in events] == [
        ("thread-example", "turn-example")
    ]
    assert events[0].title.startswith("[codex]")
    assert offsets[str(rollout.resolve())] == rollout.stat().st_size


def test_workspace_rollout_is_discovered_without_state_database(tmp_path: Path):
    codex_home = tmp_path / "codex"
    workspace_rollout = codex_home / "workspaces" / "workspace-example" / "rollout.jsonl"
    record = complete_record("turn-workspace", "工作区完成")
    record["payload"]["thread_id"] = "thread-example"  # type: ignore[index]
    append_jsonl(workspace_rollout, record)

    events, _, _ = scan_codex_events(
        codex_home, None, None, RuntimeState(initialized=True), baseline=False
    )

    assert [(event.task_id, event.turn_id) for event in events] == [
        ("thread-example", "turn-workspace")
    ]


def test_sessions_only_discovery_skips_malformed_files_and_reports_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    good = sessions / "rollout-thread-good-session.jsonl"
    append_jsonl(good, complete_record("turn-good-session", "合法会话"))
    (sessions / "rollout-thread-bad-utf8.jsonl").write_bytes(b"\xff\n")
    (sessions / "rollout-thread-bad-json.jsonl").write_text(
        "{not-json}\n", encoding="utf-8"
    )

    refs = discover_rollouts(codex_home, None)

    assert [ref.thread_id for ref in refs] == ["thread-good-session"]
    assert "codex session 文件处理失败" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "not-json" not in caplog.text


def test_sessions_only_discovery_isolates_unreadable_file_and_keeps_good_rollout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    good = sessions / "rollout-thread-good-readable.jsonl"
    append_jsonl(good, complete_record("turn-good-readable", "合法会话"))
    bad = sessions / "rollout-thread-bad-readable.jsonl"
    append_jsonl(bad, complete_record("turn-bad-readable", "不可读会话"))
    original_open = Path.open

    def fail_bad(path: Path, *args: object, **kwargs: object):
        if path == bad:
            raise PermissionError("private absolute path")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_bad)

    refs = discover_rollouts(codex_home, None)

    assert [ref.thread_id for ref in refs] == ["thread-good-readable"]
    assert "codex session 文件处理失败" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "private absolute path" not in caplog.text


def test_rollout_ref_is_discovered_from_state_database(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")

    refs = discover_rollouts(codex_home, state_db)

    assert refs == [RolloutRef("thread-example", "[codex] 合成 Codex 任务", rollout.resolve())]


def test_title_uses_catalog_then_single_prefix_and_thread_fallback(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET title = ?", ("[codex] 已有标题",))
    connection.execute("CREATE TABLE local_thread_catalog (thread_id TEXT, display_title TEXT)")
    connection.execute("INSERT INTO local_thread_catalog VALUES (?, ?)", ("thread-example", "目录标题"))
    connection.commit()
    connection.close()

    assert discover_rollouts(codex_home, state_db)[0].title == "[codex] 已有标题"

    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET title = ?", (None,))
    connection.commit()
    connection.close()
    assert discover_rollouts(codex_home, state_db)[0].title == "[codex] 目录标题"

    connection = sqlite3.connect(state_db)
    connection.execute("DELETE FROM local_thread_catalog")
    connection.commit()
    connection.close()
    assert discover_rollouts(codex_home, state_db)[0].title == "[codex] thread-thread-e"


def test_empty_codex_title_falls_back_to_one_prefix(tmp_path: Path):
    codex_home, state_db, _ = make_codex_layout(tmp_path, "thread-example")
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET title = ?", ("[codex]",))
    connection.commit()
    connection.close()

    assert discover_rollouts(codex_home, state_db)[0].title == "[codex] thread-thread-e"

    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET title = ?", ("[CoDeX] [codex] 合成标题",))
    connection.commit()
    connection.close()
    assert discover_rollouts(codex_home, state_db)[0].title == "[codex] 合成标题"


def test_truncated_rollout_tail_is_retried_after_newline(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(rollout, complete_record("turn-first", "第一回合"))
    state = RuntimeState(initialized=True)

    first, offsets, _ = scan_codex_events(codex_home, state_db, None, state, baseline=False)
    mark_seen(state, first)
    state.rollout_offsets = offsets
    partial = json.dumps(complete_record("turn-second", "第二回合"), ensure_ascii=False)
    with rollout.open("a", encoding="utf-8", newline="") as stream:
        stream.write(partial)

    no_event, new_offsets, _ = scan_codex_events(codex_home, state_db, None, state, baseline=False)

    assert no_event == []
    assert new_offsets[str(rollout.resolve())] == offsets[str(rollout.resolve())]
    with rollout.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n")

    second, _, _ = scan_codex_events(codex_home, state_db, None, state, baseline=False)

    assert [event.turn_id for event in second] == ["turn-second"]


def test_state_database_rollout_path_switch_is_read_as_new_stream(tmp_path: Path):
    codex_home, state_db, first_rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(first_rollout, complete_record("turn-first", "第一回合"))
    state = RuntimeState(initialized=True)
    first, offsets, _ = scan_codex_events(codex_home, state_db, None, state, baseline=False)
    mark_seen(state, first)
    state.rollout_offsets = offsets

    second_rollout = codex_home / "sessions" / "rollout-thread-example-second.jsonl"
    append_jsonl(second_rollout, complete_record("turn-second", "第二回合"))
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET rollout_path = ? WHERE thread_id = ?", (str(second_rollout), "thread-example"))
    connection.commit()
    connection.close()

    events, new_offsets, _ = scan_codex_events(codex_home, state_db, None, state, baseline=False)

    assert [event.turn_id for event in events] == ["turn-second"]
    assert new_offsets[str(second_rollout.resolve())] == second_rollout.stat().st_size


def test_same_codex_rollout_path_replaced_by_larger_file_is_read_from_start(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-replaced")
    append_jsonl(rollout, complete_record("turn-old", "旧回合"))
    state = RuntimeState(initialized=True)

    first, offsets, _ = scan_codex_events(
        codex_home, state_db, None, state, baseline=False
    )
    state.seen_event_keys.update(event.key for event in first)
    state.rollout_offsets = offsets
    old_size = rollout.stat().st_size

    replacement = json.dumps(
        complete_record("turn-new", "新文件中的新回合" + ("扩展" * old_size)),
        ensure_ascii=False,
    )
    rollout.unlink()
    rollout.write_text(replacement + "\n", encoding="utf-8")
    assert rollout.stat().st_size > old_size

    events, new_offsets, _ = scan_codex_events(
        codex_home, state_db, None, state, baseline=False
    )

    assert [event.turn_id for event in events] == ["turn-new"]
    assert new_offsets[str(rollout.resolve())] == rollout.stat().st_size


def test_codex_rollout_read_error_is_reported_without_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-io-error")
    append_jsonl(rollout, complete_record("turn-io-error", "读取失败"))
    original_open = Path.open

    def fail_rollout(path: Path, *args: object, **kwargs: object):
        if path == rollout:
            raise PermissionError("private absolute path")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_rollout)

    with pytest.raises(CodexSourceError, match="rollout") as exc_info:
        scan_codex_events(
            codex_home, state_db, None, RuntimeState(initialized=True), baseline=False
        )

    assert str(tmp_path) not in str(exc_info.value)
    assert "private absolute path" not in str(exc_info.value)


def test_duplicate_completion_is_idempotent_but_conflicting_message_has_distinct_key(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(rollout, complete_record("turn-example", "相同答案"))
    append_jsonl(rollout, complete_record("turn-example", "相同答案"))
    append_jsonl(rollout, complete_record("turn-example", "不同答案"))

    events, _, _ = scan_codex_events(codex_home, state_db, None, RuntimeState(initialized=True), baseline=False)

    assert len(events) == 2
    assert events[0].key != events[1].key
    assert all(event.key.startswith("codex:thread-example:turn-example:") for event in events)


def test_non_user_thread_is_silent_and_cwd_project_do_not_filter_user_thread(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(rollout, complete_record("turn-example", "最终答案"))
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET thread_source = ?, cwd = ?, project = ?", ("automation", "other cwd", "other project"))
    connection.commit()
    connection.close()

    assert scan_codex_events(codex_home, state_db, None, RuntimeState(initialized=True), baseline=False)[0] == []

    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET thread_source = ?, cwd = ?, project = ?", ("user", "unexpected cwd", "unexpected project"))
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(codex_home, state_db, None, RuntimeState(initialized=True), baseline=False)

    assert [event.task_id for event in events] == ["thread-example"]


def test_task_started_is_used_for_duration(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(rollout, started_record("turn-example", "2026-01-02T03:03:05Z"))
    append_jsonl(rollout, complete_record("turn-example", "最终答案"))
    state = RuntimeState(initialized=True)

    events, _, started = scan_codex_events(codex_home, state_db, None, state, baseline=False)

    assert started["thread-example:turn-example"] == 1767322985000
    assert events[0].duration_ms == 60000


def test_missing_turn_id_uses_stable_hash_key(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(rollout, complete_record(None, "无 turn 的答案"))

    first, offsets, _ = scan_codex_events(codex_home, state_db, None, RuntimeState(initialized=True), baseline=False)
    state = RuntimeState(initialized=True, seen_event_keys={first[0].key}, rollout_offsets=offsets)
    second, _, _ = scan_codex_events(codex_home, state_db, None, state, baseline=False)

    assert len(first) == 1
    assert first[0].turn_id is not None
    assert first[0].turn_id.startswith("turn-")
    assert second == []


def test_summary_is_limited_to_last_6000_characters(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    message = "前" * 7000 + "尾" * 100
    append_jsonl(rollout, complete_record("turn-example", message))

    events, _, _ = scan_codex_events(codex_home, state_db, None, RuntimeState(initialized=True), baseline=False)

    assert events[0].summary_text == message[-6000:]


def test_rollout_path_outside_codex_home_is_rejected(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET rollout_path = ?", (str(outside),))
    connection.commit()
    connection.close()

    with pytest.raises(CodexSourceError, match="rollout"):
        discover_rollouts(codex_home, state_db)


def test_first_scan_baselines_rollout_at_eof(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    append_jsonl(rollout, complete_record("turn-example", "历史完成"))

    events, offsets, _ = scan_codex_events(codex_home, state_db, None, RuntimeState(), baseline=True)

    assert events == []
    assert offsets[str(rollout.resolve())] == rollout.stat().st_size


def test_history_database_only_supplements_missing_rollout_event(tmp_path: Path):
    codex_home, state_db, history_db = make_history_layout(tmp_path)

    events, _, _ = scan_codex_events(
        codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False
    )

    assert len(events) == 1
    assert events[0].key.startswith("codex:")
    assert events[0].turn_id == "turn-history"
    assert events[0].title == "[codex] 合成 Codex 任务"


def test_rollout_and_history_missing_turn_share_identity_and_emit_once(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-cross-source")
    timestamp = "2026-01-02T03:04:05Z"
    append_jsonl(rollout, complete_record(None, "  跨源最终答案\r\n", timestamp))
    history_db = codex_home / "thread_history_example.sqlite"
    connection = sqlite3.connect(history_db)
    connection.execute(
        """
        CREATE TABLE thread_history (
            row_id INTEGER PRIMARY KEY,
            thread_id TEXT,
            status TEXT,
            completed_at TEXT,
            final_message TEXT,
            thread_source TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO thread_history VALUES (?, ?, ?, ?, ?, ?)",
        (1, "thread-cross-source", "completed", timestamp, "跨源最终答案", "user"),
    )
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(
        codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False
    )

    assert len(events) == 1
    assert events[0].turn_id is not None
    assert events[0].turn_id.startswith("turn-")


def test_history_does_not_duplicate_rollout_completion(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-history")
    append_jsonl(rollout, complete_record("turn-history", "历史最终答案"))
    history_db = codex_home / "thread_history_example.sqlite"
    connection = sqlite3.connect(history_db)
    connection.execute(
        """
        CREATE TABLE thread_history (
            row_id INTEGER PRIMARY KEY,
            thread_id TEXT,
            turn_id TEXT,
            status TEXT,
            completed_at TEXT,
            final_message TEXT,
            thread_source TEXT
        )
        """
    )
    connection.execute("INSERT INTO thread_history VALUES (?, ?, ?, ?, ?, ?, ?)", (1, "thread-history", "turn-history", "completed", "2026-01-02T03:04:05Z", "历史库不同引用", "user"))
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False)

    assert len(events) == 1
    assert events[0].turn_id == "turn-history"


def test_history_does_not_supplement_rollout_pair_already_seen(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-history")
    append_jsonl(rollout, complete_record("turn-history", "历史最终答案"))
    history_db = codex_home / "thread_history_example.sqlite"
    connection = sqlite3.connect(history_db)
    connection.execute(
        """
        CREATE TABLE thread_history (
            row_id INTEGER PRIMARY KEY,
            thread_id TEXT,
            turn_id TEXT,
            status TEXT,
            completed_at TEXT,
            final_message TEXT,
            thread_source TEXT
        )
        """
    )
    connection.execute("INSERT INTO thread_history VALUES (?, ?, ?, ?, ?, ?, ?)", (1, "thread-history", "turn-history", "completed", "2026-01-02T03:04:05Z", "历史最终答案", "user"))
    connection.commit()
    connection.close()

    state = RuntimeState(initialized=True)
    first, offsets, _ = scan_codex_events(codex_home, state_db, history_db, state, baseline=False)
    mark_seen(state, first)
    state.rollout_offsets = offsets

    events, _, _ = scan_codex_events(codex_home, state_db, history_db, state, baseline=False)

    assert events == []


def test_history_requires_user_completed_with_final_answer_reference(tmp_path: Path):
    codex_home, state_db, history_db = make_history_layout(tmp_path)
    connection = sqlite3.connect(history_db)
    connection.execute("UPDATE thread_history SET thread_source = ?, status = ?", ("automation", "completed"))
    connection.commit()
    connection.close()
    assert scan_codex_events(codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False)[0] == []

    connection = sqlite3.connect(history_db)
    connection.execute("UPDATE thread_history SET thread_source = ?, final_message = ?", ("user", None))
    connection.commit()
    connection.close()
    assert scan_codex_events(codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False)[0] == []


def test_missing_source_is_accepted_but_explicit_non_user_source_wins(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-source")
    append_jsonl(rollout, complete_record("turn-source", "来源兼容"))
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE threads SET thread_source = NULL")
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(
        codex_home, state_db, None, RuntimeState(initialized=True), baseline=False
    )
    assert [event.task_id for event in events] == ["thread-source"]

    connection = sqlite3.connect(state_db)
    connection.execute(
        "CREATE TABLE thread_metadata (thread_id TEXT, rollout_path TEXT, thread_source TEXT)"
    )
    connection.execute(
        "INSERT INTO thread_metadata VALUES (?, ?, ?)",
        ("thread-source", str(rollout), "automation"),
    )
    connection.commit()
    connection.close()

    assert scan_codex_events(
        codex_home, state_db, None, RuntimeState(initialized=True), baseline=False
    )[0] == []


def test_non_user_outside_path_is_silent_without_blocking_user_rollout(tmp_path: Path):
    codex_home, state_db, user_rollout = make_codex_layout(tmp_path, "thread-user")
    append_jsonl(user_rollout, complete_record("turn-user", "用户完成"))
    outside_rollout = tmp_path / "outside-rollout.jsonl"
    append_jsonl(outside_rollout, complete_record("turn-automation", "自动化完成"))
    connection = sqlite3.connect(state_db)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        ("thread-automation", "自动化任务", str(outside_rollout), "automation", "cwd", "project"),
    )
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(
        codex_home, state_db, None, RuntimeState(initialized=True), baseline=False
    )

    assert [(event.task_id, event.turn_id) for event in events] == [
        ("thread-user", "turn-user")
    ]


def test_history_millisecond_columns_preserve_millisecond_timestamp(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-history-ms")
    history_db = codex_home / "thread_history_example.sqlite"
    connection = sqlite3.connect(history_db)
    connection.execute(
        "CREATE TABLE history (thread_id TEXT, turn_id TEXT, status TEXT, completed_at_ms INTEGER, final_message TEXT, thread_source TEXT)"
    )
    connection.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ("thread-history-ms", "turn-ms", "completed", 1700000000000, "毫秒答案", "user"),
    )
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False)

    assert events[0].completed_at_ms == 1700000000000

def test_invalid_state_database_schema_fails_closed(tmp_path: Path):
    codex_home = tmp_path / "codex"
    (codex_home / "sessions").mkdir(parents=True)
    state_db = codex_home / "state_example.sqlite"
    connection = sqlite3.connect(state_db)
    connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(CodexSourceError, match="schema"):
        discover_rollouts(codex_home, state_db)


def test_corrupt_codex_rollout_does_not_block_other_rollouts_and_is_diagnosed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    codex_home, state_db, bad_rollout = make_codex_layout(tmp_path, "thread-bad-json")
    bad_rollout.write_text("{not-json}\n", encoding="utf-8")
    good_rollout = codex_home / "sessions" / "rollout-thread-good-json.jsonl"
    append_jsonl(good_rollout, complete_record("turn-good-json", "好文件"))
    connection = sqlite3.connect(state_db)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        (
            "thread-good-json",
            "好文件",
            str(good_rollout),
            "user",
            "cwd",
            "project",
        ),
    )
    connection.commit()
    connection.close()

    events, _, _ = scan_codex_events(
        codex_home, state_db, None, RuntimeState(initialized=True), baseline=False
    )

    assert [event.turn_id for event in events] == ["turn-good-json"]
    assert "codex rollout 文件处理失败" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_invalid_history_database_schema_fails_closed(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-example")
    history_db = codex_home / "thread_history_example.sqlite"
    connection = sqlite3.connect(history_db)
    connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(CodexSourceError, match="schema"):
        scan_codex_events(codex_home, state_db, history_db, RuntimeState(initialized=True), baseline=False)


def test_backfill_returns_latest_completion_for_exact_thread_only(tmp_path: Path):
    codex_home, state_db, rollout = make_codex_layout(tmp_path, "thread-target")
    append_jsonl(rollout, complete_record("turn-old", "旧答案", "2026-01-02T03:04:05Z"))
    append_jsonl(rollout, complete_record("turn-latest", "新答案", "2026-01-02T03:05:05Z"))
    other_rollout = codex_home / "sessions" / "rollout-thread-other.jsonl"
    append_jsonl(other_rollout, complete_record("turn-other", "其他答案", "2026-01-02T03:06:05Z"))
    connection = sqlite3.connect(state_db)
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        ("thread-other", "其他任务", str(other_rollout), "user", "cwd", "project"),
    )
    connection.commit()
    connection.close()

    event = backfill_codex_thread(codex_home, state_db, None, "thread-target")

    assert event.task_id == "thread-target"
    assert event.turn_id == "turn-latest"
    assert event.summary_text == "新答案"
    with pytest.raises(CodexSourceError, match="指定"):
        backfill_codex_thread(codex_home, state_db, None, "thread-missing")
