import pytest

from zcode_task_notifier.notifier import build_prompt, enqueue_automation
from test_notifier import fake_event, fake_bot_target, make_automations_db, read_automation, count_automations


@pytest.mark.parametrize("source", ["zcode", "codex"])
def test_source_prefix_is_once_in_title_and_required_on_message_first_line(tmp_path, source):
    db = make_automations_db(tmp_path / "tasks.sqlite")
    event = fake_event(source=source, title=f"[{source}] [{source}] 合成任务")
    enqueue_automation(db, tmp_path / "workspace", fake_bot_target(), event, "model", 1000)
    row = read_automation(db)
    assert row["title"] == f"[{source}] 合成任务"
    assert f"最终通知正文的第一行必须以 `[{source}] ` 开始" in row["prompt"]
    assert '"title":"合成任务"' in row["prompt"]


@pytest.mark.parametrize("source", ["claudecode", "dsh", "unknown"])
def test_reserved_or_unknown_source_cannot_create_notification(tmp_path, source):
    db = make_automations_db(tmp_path / "tasks.sqlite")
    event = fake_event(source=source)
    with pytest.raises(ValueError, match="来源"):
        build_prompt(event)
    with pytest.raises(ValueError, match="来源"):
        enqueue_automation(db, tmp_path / "workspace", fake_bot_target(), event, "model", 1000)
    assert count_automations(db) == 0
