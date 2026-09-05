from __future__ import annotations

import pytest

from zcode_task_notifier.models import Event
from zcode_task_notifier.stop_policy import classify_stop


def test_rule_confirmation_waiting_for_first_task_is_ignored():
    decision = classify_stop(
        final_text="收到 Harness 规则，等待具体任务",
        structured_status="completed",
        has_user_task=False,
    )

    assert decision.status is None
    assert decision.reason == "no_actual_task"


def test_explicit_plan_approval_wait_is_not_approval_without_user_task():
    decision = classify_stop(
        final_text="我已经整理好实施计划，等待你的确认后开始。",
        structured_status="waiting",
        has_user_task=False,
    )

    assert decision.status is None


def test_explicit_plan_approval_is_distinct_from_choice_input():
    approval = classify_stop(
        final_text="计划已列出。请明确回复‘同意’后我再执行。",
        structured_status="waiting",
        has_user_task=True,
    )
    choice = classify_stop(
        final_text="有两个方案：A 保守，B 快速。请选择 A 或 B。",
        structured_status="waiting",
        has_user_task=True,
    )

    assert approval.status == "awaiting_approval"
    assert choice.status == "awaiting_input"
    assert approval.status != choice.status
    assert approval.plan_fingerprint
    assert choice.plan_fingerprint is None


def test_completed_index_with_explicit_final_wait_is_still_a_wait():
    decision = classify_stop(
        final_text="计划已列出，请明确回复同意后再执行。",
        structured_status="completed",
        has_user_task=True,
    )

    assert decision.status == "awaiting_approval"


def test_legacy_completed_plan_wait_is_approval_without_input_context():
    decision = classify_stop(
        final_text="实施计划已列出，等待你确认后再执行。",
        structured_status="completed",
        has_user_task=None,
    )

    assert decision.status == "awaiting_approval"
    assert decision.plan_fingerprint


def test_running_or_plain_progress_is_ignored():
    assert classify_stop(
        final_text="正在检查项目文件，稍后继续。",
        structured_status="running",
        has_user_task=True,
    ).status is None
    assert classify_stop(
        final_text="计划如下：先检查，再修改。",
        structured_status="completed",
        has_user_task=True,
    ).status is None


def test_explicit_final_and_failure_are_notified():
    completed = classify_stop(
        final_text="已完成修改并通过测试。",
        structured_status="completed",
        has_user_task=True,
    )
    failed = classify_stop(
        final_text="执行失败：编译命令返回错误。",
        structured_status="error",
        has_user_task=True,
    )

    assert completed.status == "completed"
    assert failed.status == "error"


def test_mentions_and_negation_do_not_turn_progress_into_failure_or_wait():
    decision = classify_stop(
        final_text="计划中提到失败场景，但当前检查尚未完成，也不需要你确认。",
        structured_status="running",
        has_user_task=True,
    )

    assert decision.status is None


def test_completed_outcome_after_quoted_failure_is_not_downgraded():
    decision = classify_stop(
        final_text="执行失败是旧方案的问题；当前已修复完成并通过测试。",
        structured_status="completed",
        has_user_task=True,
    )

    assert decision.status == "completed"


def test_event_statuses_accept_stop_metadata_and_old_constructor_defaults():
    event = Event(
        source="zcode",
        key="zcode:task:turn",
        task_id="task",
        title="任务",
        completed_at_ms=1,
        duration_ms=None,
        summary_text="已完成",
        status="awaiting_input",
        stop_reason="choice_required",
        plan_fingerprint="abc123",
        turn_id="turn",
    )

    assert event.status == "awaiting_input"
    assert Event(
        "zcode", "k", "t", "标题", 1, None, "答案"
    ).status == "completed"


@pytest.mark.parametrize("text", [
    "已完成下拉选择框修改，测试通过。",
    "已经选择方案 A，并完成实现。",
    "本次决定不改配置，问题已修复。",
    "方案已由用户选择，配置已完成。",
    "已补充缺少的信息并通过测试。",
])
def test_outcome_mentioning_choice_does_not_request_user_input(text):
    assert classify_stop(text, structured_status="completed", has_user_task=True).status == "completed"


@pytest.mark.parametrize("text", [
    "请选择方案后继续。",
    "请确认计划后再执行。",
    "测试失败，正在继续定位。",
])
def test_nonfinal_tool_step_cannot_notify_from_wording_alone(text):
    assert classify_stop(text, structured_status="completed", has_user_task=True,
                         explicit_final=False).status is None
