import json

from zcode_task_notifier.models import RuntimeState
from zcode_task_notifier.zcode_source import scan_zcode_events
from test_zcode_source import append_model_io, insert_task, make_zcode_db


def setup_task(tmp_path, summary="已完成修改，测试通过。", status="completed"):
    db = make_zcode_db(tmp_path / "tasks.sqlite")
    insert_task(db, "session-example", "任务", status, None, 1, 100, searchable_text=summary)
    return db, tmp_path / "rollout"


def test_legacy_ready_ack_is_suppressed_on_repeated_scans(tmp_path):
    db, rollout = setup_task(tmp_path, "收到 Harness 规则，已就绪，等待具体任务。")
    state = RuntimeState(initialized=True)
    for _ in range(2):
        events, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
        state.zcode_rollout_offsets, state.zcode_last_turns = offsets, turns
        assert events == []


def test_legacy_explicit_approval_final_is_not_reported_completed(tmp_path):
    db, rollout = setup_task(tmp_path, "实施计划已列出，等待你确认后再执行。")
    events, _, _ = scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
    assert len(events) == 1
    assert events[0].status == "awaiting_approval"


def test_real_model_response_wins_over_stale_index_summary(tmp_path):
    db, rollout = setup_task(tmp_path, "旧回合的结果。")
    path = append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["searchable_text"] = "旧回合的摘要：正在处理。"
    payload["request"] = {"messages": [{"role": "user", "content": "请修复问题"}]}
    payload["response"] = {"finishReason": "stop", "text": "已修复当前问题，测试通过。", "toolCalls": []}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    events, _, _ = scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
    assert [(e.summary_text, e.status) for e in events] == [("已修复当前问题，测试通过。", "completed")]


def test_intermediate_tool_call_cannot_be_reported_as_completed(tmp_path):
    db, rollout = setup_task(tmp_path)
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z",
                    searchable_text="正在读取文件，接下来继续。",
                    response={"finishReason": "tool-calls", "toolCalls": [{"toolName": "read_file"}]})
    events, _, _ = scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
    assert events == []


def test_rules_only_model_request_is_not_notified(tmp_path):
    db, rollout = setup_task(tmp_path)
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z",
                    searchable_text="收到，会遵循约定。",
                    request={"messages": [{"role": "user", "content": "<INSTRUCTIONS>修改前检查，完成后测试。</INSTRUCTIONS>"}]},
                    response={"finishReason": "stop", "text": "收到，会遵循约定。", "toolCalls": []})
    state = RuntimeState(initialized=True)
    for _ in range(2):
        events, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
        state.zcode_rollout_offsets, state.zcode_last_turns = offsets, turns
        assert events == []


def test_running_task_with_final_looking_output_is_still_silent(tmp_path):
    db, rollout = setup_task(tmp_path, status="running")
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z")
    events, _, _ = scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
    assert events == []


def test_ignored_tool_step_does_not_swallow_same_turn_later_completion(tmp_path):
    db, rollout = setup_task(tmp_path)
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:04:05Z",
                    searchable_text="正在读取文件。", response={"finishReason": "tool-calls"})
    state = RuntimeState(initialized=True)
    events, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.zcode_rollout_offsets, state.zcode_last_turns = offsets, turns
    assert events == []
    append_model_io(rollout, "session-example", "turn-example", "2026-01-02T03:05:05Z",
                    searchable_text="已完成修改，测试通过。", response={"finishReason": "stop"})
    events, _, _ = scan_zcode_events(db, rollout, state, baseline=False)
    assert [(e.turn_id, e.status) for e in events] == [("turn-example", "completed")]


def test_ignored_rules_turn_does_not_swallow_following_real_task(tmp_path):
    db, rollout = setup_task(tmp_path)
    append_model_io(rollout, "session-example", "turn-rules", "2026-01-02T03:04:05Z",
                    request={"messages": [{"role": "user", "content": "<INSTRUCTIONS>完成后测试。</INSTRUCTIONS>"}]},
                    response={"finishReason": "stop", "text": "收到规则。"})
    state = RuntimeState(initialized=True)
    events, offsets, turns = scan_zcode_events(db, rollout, state, baseline=False)
    state.zcode_rollout_offsets, state.zcode_last_turns = offsets, turns
    assert events == []
    append_model_io(rollout, "session-example", "turn-task", "2026-01-02T03:05:05Z",
                    request={"messages": [{"role": "user", "content": "请修复这个问题。"}]},
                    response={"finishReason": "stop", "text": "已完成修改，测试通过。"})
    events, _, _ = scan_zcode_events(db, rollout, state, baseline=False)
    assert [(e.turn_id, e.status) for e in events] == [("turn-task", "completed")]


def test_rules_only_input_cannot_be_overridden_by_task_like_summary(tmp_path):
    db, rollout = setup_task(tmp_path)
    append_model_io(rollout, "session-example", "turn-rules", "2026-01-02T03:04:05Z",
                    request={"messages": [{"role": "user", "content": "<INSTRUCTIONS>修改前检查，完成后测试。</INSTRUCTIONS>"}]},
                    response={"finishReason": "stop", "text": "已经完成修改并通过所有测试。"})
    events, _, _ = scan_zcode_events(db, rollout, RuntimeState(initialized=True), baseline=False)
    assert events == []
