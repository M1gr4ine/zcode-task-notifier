"""按实际 rollout 消息封装构造的合成停顿回归，不包含真实会话数据。"""

import json
import sqlite3

import pytest

from zcode_task_notifier.codex_source import scan_codex_events
from zcode_task_notifier.models import RuntimeState, TurnContext
from zcode_task_notifier.state import state_from_json, state_to_json
from test_codex_source import append_jsonl, complete_record, make_codex_layout, make_history_layout, started_record


def user_message(text):
    # 实际 response_item 用户消息没有 turn_id，必须绑定当前 task_started。
    return {
        "timestamp": "2026-01-02T03:04:01Z",
        "type": "response_item",
        "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


RULES = (
    "# AGENTS.md instructions\n<INSTRUCTIONS>修改前检查源码，完成后运行测试。"
    "禁止修改用户文件。</INSTRUCTIONS>\n"
    "<environment_context>运行环境</environment_context>\n"
    "<recommended_plugins>插件清单</recommended_plugins>"
)


def test_rules_only_response_item_is_not_a_task_even_with_action_words(tmp_path):
    root, db, rollout = make_codex_layout(tmp_path, "thread-rule-only")
    append_jsonl(rollout, started_record("turn-rule-only", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message(RULES))
    append_jsonl(rollout, complete_record("turn-rule-only", "收到，会遵循这些约定。"))
    events, offsets, _ = scan_codex_events(root, db, None, RuntimeState(initialized=True), baseline=False)
    assert events == []
    assert offsets[str(rollout.resolve())] == rollout.stat().st_size


def test_actual_task_after_rules_survives_a_restart_before_completion(tmp_path):
    root, db, rollout = make_codex_layout(tmp_path, "thread-real-input")
    append_jsonl(rollout, started_record("turn-real-input", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message(RULES))
    append_jsonl(rollout, user_message("帮我修复这项检查并运行测试。"))
    state = RuntimeState(initialized=True)
    events, offsets, starts = scan_codex_events(root, db, None, state, baseline=False)
    assert events == []
    state.rollout_offsets = offsets
    state.rollout_turn_started_ms = starts
    state = state_from_json(state_to_json(state))
    append_jsonl(rollout, complete_record("turn-real-input", "已修复检查，测试通过。"))
    events, _, _ = scan_codex_events(root, db, None, state, baseline=False)
    assert [(e.turn_id, e.status) for e in events] == [("turn-real-input", "completed")]


def test_formal_proposed_plan_is_approval_not_completion(tmp_path):
    root, db, rollout = make_codex_layout(tmp_path, "thread-plan")
    append_jsonl(rollout, started_record("turn-plan", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message("先制定改造计划，等我批准。"))
    append_jsonl(rollout, complete_record(
        "turn-plan", "<proposed_plan>\n# 改造计划\n先验证输入，再修改逻辑。\n</proposed_plan>"
    ))
    events, _, _ = scan_codex_events(root, db, None, RuntimeState(initialized=True), baseline=False)
    assert len(events) == 1
    assert events[0].status == "awaiting_approval"
    assert events[0].plan_fingerprint


def test_plain_progress_at_turn_end_does_not_notify(tmp_path):
    root, db, rollout = make_codex_layout(tmp_path, "thread-progress")
    append_jsonl(rollout, started_record("turn-progress", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message("检查并修复问题。"))
    append_jsonl(rollout, complete_record("turn-progress", "我先检查相关文件，接下来继续修改。"))
    events, _, _ = scan_codex_events(root, db, None, RuntimeState(initialized=True), baseline=False)
    assert events == []


@pytest.mark.parametrize("name", ["request_user_input", "functions.request_user_input"])
def test_answered_question_is_not_notified_after_agent_resumes(tmp_path, name):
    root, db, rollout = make_codex_layout(tmp_path, "thread-answered")
    append_jsonl(rollout, started_record("turn-answered", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message("检查问题，缺少信息时先问我。"))
    append_jsonl(rollout, {
        "timestamp": "2026-01-02T03:04:02Z", "type": "response_item",
        "payload": {"type": "function_call", "name": name, "call_id": "call-example",
                    "arguments": json.dumps({"questions": [{"id": "choice", "question": "请选择 A 或 B"}]})},
    })
    append_jsonl(rollout, {
        "timestamp": "2026-01-02T03:04:03Z", "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "call-example",
                    "output": json.dumps({"answers": {"choice": {"answers": ["A"]}}})},
    })
    events, _, _ = scan_codex_events(root, db, None, RuntimeState(initialized=True), baseline=False)
    assert events == []


def test_namespaced_unanswered_question_is_a_real_stop(tmp_path):
    root, db, rollout = make_codex_layout(tmp_path, "thread-unanswered")
    append_jsonl(rollout, started_record("turn-unanswered", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message("需要我选方案时暂停。"))
    append_jsonl(rollout, {
        "timestamp": "2026-01-02T03:04:02Z", "type": "response_item",
        "payload": {"type": "function_call", "name": "functions.request_user_input", "call_id": "call-unanswered",
                    "arguments": json.dumps({"questions": [{"id": "choice", "question": "请选择 A 或 B"}]})},
    })
    events, _, _ = scan_codex_events(root, db, None, RuntimeState(initialized=True), baseline=False)
    assert len(events) == 1
    assert events[0].status == "awaiting_input"


def test_history_cannot_bypass_persisted_rules_only_evidence(tmp_path):
    root, db, history = make_history_layout(tmp_path)
    state = RuntimeState(initialized=True, turn_contexts={
        "codex:thread-history:turn-history": TurnContext(
            source="codex", task_id="thread-history", turn_id="turn-history",
            has_user_task=False, status="ignored",
        ),
    })
    events, _, _ = scan_codex_events(root, db, history, state, baseline=False)
    assert events == []


def test_history_terminal_failure_remains_a_final_stop(tmp_path):
    root, db, history = make_history_layout(tmp_path)
    with sqlite3.connect(history) as connection:
        connection.execute("UPDATE thread_history SET status='error', final_message='执行失败，无法继续。'")
    events, _, _ = scan_codex_events(root, db, history, RuntimeState(initialized=True), baseline=False)
    assert [event.status for event in events] == ["error"]


def test_foreign_thread_input_cannot_turn_rules_ack_into_task_completion(tmp_path):
    root, db, rollout = make_codex_layout(tmp_path, "thread-local")
    append_jsonl(rollout, started_record("turn-local", "2026-01-02T03:04:00Z"))
    append_jsonl(rollout, user_message(RULES))
    foreign = user_message("请执行真实修改。")
    foreign["payload"]["thread_id"] = "thread-foreign"
    append_jsonl(rollout, foreign)
    append_jsonl(rollout, complete_record("turn-local", "收到，将遵循约定。"))
    events, _, _ = scan_codex_events(root, db, None, RuntimeState(initialized=True), baseline=False)
    assert events == []
