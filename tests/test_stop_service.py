"""停顿事实到首发自动化的集成回归，全部使用合成数据。"""

import json
import sqlite3

import pytest

from zcode_task_notifier import service
from zcode_task_notifier.state import StateStore
from test_codex_source import append_jsonl, started_record
from test_codex_stop_shape import user_message
from test_service import IntegratedFixture


def append_wait(fixture):
    append_jsonl(fixture.codex_rollout, started_record("turn-choice", "2026-01-02T03:04:00Z"))
    append_jsonl(fixture.codex_rollout, user_message("请检查问题，需要我选择时暂停。"))
    append_jsonl(fixture.codex_rollout, {
        "timestamp": "2026-01-02T03:04:02Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "functions.request_user_input",
                    "call_id": "call-choice", "arguments": json.dumps({"questions": [{"question": "请选择 A 或 B"}]})},
    })


def test_waiting_state_reaches_native_prompt_and_roundtrips_state(tmp_path):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    service.initialize_baseline(fixture.config_path, fixture.state_path)
    append_wait(fixture)
    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    assert report.enqueued == 1
    state = StateStore(fixture.state_path).load_strict()
    item = next(iter(state.outbox.values()))
    assert item.status == "submitted"
    assert item.event.status == "awaiting_input"
    with sqlite3.connect(fixture.zcode_db) as connection:
        prompt = connection.execute("SELECT prompt FROM automations").fetchone()[0]
    assert '"status":"awaiting_input"' in prompt
    assert "停顿时间" in prompt


def test_unsubmitted_wait_is_cancelled_before_delivery_after_answer(tmp_path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    service.initialize_baseline(fixture.config_path, fixture.state_path)
    append_wait(fixture)
    original_target = service._load_target
    monkeypatch.setattr(service, "_load_target", lambda paths: (None, ["notification:Unavailable"]))
    assert service.run_once(fixture.config_path, fixture.state_path, now_ms=100000).enqueued == 0
    assert len(StateStore(fixture.state_path).load().outbox) == 1
    append_jsonl(fixture.codex_rollout, {
        "timestamp": "2026-01-02T03:04:03Z", "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "call-choice",
                    "output": json.dumps({"answers": {"choice": {"answers": ["A"]}}})},
    })
    monkeypatch.setattr(service, "_load_target", original_target)
    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=160000)
    assert report.enqueued == 0
    assert fixture.automation_titles() == []
    assert StateStore(fixture.state_path).load().outbox == {}


def test_pending_wait_is_not_submitted_when_source_refresh_fails(tmp_path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    service.initialize_baseline(fixture.config_path, fixture.state_path)
    append_wait(fixture)
    original_target = service._load_target
    monkeypatch.setattr(service, "_load_target", lambda paths: (None, ["notification:Unavailable"]))
    service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    monkeypatch.setattr(service, "_load_target", original_target)

    def unavailable(*args, **kwargs):
        raise OSError("synthetic source unavailable")

    monkeypatch.setattr(service, "scan_codex_events", unavailable)
    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=160000)
    assert report.enqueued == 0
    assert fixture.automation_titles() == []
    assert len(StateStore(fixture.state_path).load().outbox) == 1


@pytest.mark.parametrize("terminal_status", ["completed", "error"])
def test_history_terminal_supersedes_unsubmitted_wait(tmp_path, monkeypatch, terminal_status):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    service.initialize_baseline(fixture.config_path, fixture.state_path)
    append_wait(fixture)
    original_target = service._load_target
    monkeypatch.setattr(service, "_load_target", lambda paths: (None, ["notification:Unavailable"]))
    service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    monkeypatch.setattr(service, "_load_target", original_target)
    history = fixture.codex_home / "thread_history_example.sqlite"
    with sqlite3.connect(history) as connection:
        connection.execute("CREATE TABLE thread_history (thread_id TEXT, turn_id TEXT, status TEXT, completed_at TEXT, final_message TEXT, thread_source TEXT)")
        connection.execute("INSERT INTO thread_history VALUES (?, ?, ?, ?, ?, ?)",
                           ("thread-new", "turn-choice", terminal_status, "2026-01-02T03:05:05Z",
                            "已完成修改。" if terminal_status == "completed" else "执行失败，无法继续。", "user"))
    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=160000)
    assert report.enqueued == 1
    state = StateStore(fixture.state_path).load()
    assert [item.event.status for item in state.outbox.values()] == [terminal_status]
