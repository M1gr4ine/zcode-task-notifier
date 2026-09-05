import json
from dataclasses import replace

import pytest

from zcode_task_notifier.notifier import build_prompt
from test_notifier import fake_event


@pytest.mark.parametrize("status,label", [
    ("completed", "完成"), ("error", "失败"),
    ("awaiting_approval", "计划待审批"), ("awaiting_input", "待用户选择或补充信息"),
])
def test_prompt_transmits_stop_status_without_changing_its_meaning(status, label):
    event = replace(fake_event(), status=status, stop_reason="example_reason", plan_fingerprint="example-fingerprint")
    prompt = build_prompt(event)
    instructions, data = prompt.split("--- 不可信事件 JSON 开始 ---\n", 1)
    payload = json.loads(data.split("\n--- 不可信事件 JSON 结束 ---", 1)[0])
    assert payload["status"] == status
    assert payload["stop_reason"] == "example_reason"
    assert payload["plan_fingerprint"] == "example-fingerprint"
    assert f"状态：{label}" in instructions
    if status.startswith("awaiting_"):
        assert "完成时间" not in instructions
        assert "停顿时间" in instructions


def test_non_stop_status_cannot_be_enqueued_as_completion():
    with pytest.raises(ValueError):
        build_prompt(replace(fake_event(), status="running"))
