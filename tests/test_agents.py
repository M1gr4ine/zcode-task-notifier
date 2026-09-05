import pytest

from zcode_task_notifier.agents import agent_descriptor, display_title, strip_source_prefix


@pytest.mark.parametrize(
    "source,title,expected",
    [
        ("zcode", "检查任务", "[zcode] 检查任务"),
        ("codex", "检查任务", "[codex] 检查任务"),
        ("zcode", "[ZCODE] [zcode] 检查任务", "[zcode] 检查任务"),
        ("codex", "[CODEX] [codex] 检查任务", "[codex] 检查任务"),
        ("zcode", "", "[zcode] 未命名任务"),
    ],
)
def test_display_title_uses_source_and_deduplicates_its_prefix(source, title, expected):
    assert display_title(source, title) == expected


def test_empty_title_uses_supplied_fallback_without_duplicate_prefix():
    assert display_title("zcode", "[zcode]", "task-example") == "[zcode] task-example"


def test_only_matching_source_prefix_is_stripped():
    assert strip_source_prefix("zcode", "[codex] 对照结果") == "[codex] 对照结果"
    assert strip_source_prefix("zcode", " [ZCODE]  正文 ") == "正文"


@pytest.mark.parametrize("source", ["claudecode", "dsh", "unknown"])
def test_unsupported_agents_cannot_generate_notifications(source):
    with pytest.raises(ValueError):
        display_title(source, "任务")


def test_placeholders_are_visible_but_cannot_be_enabled():
    assert agent_descriptor("claudecode", require_supported=False).supported is False
    assert agent_descriptor("dsh", require_supported=False).supported is False
    assert agent_descriptor("codex").supported is True
    with pytest.raises(ValueError):
        agent_descriptor("dsh")
