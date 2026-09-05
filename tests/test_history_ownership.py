import json
from pathlib import Path

import pytest

from test_history_cleanup import _event, _outbox_item
from zcode_task_notifier.history_ownership import (
    HistoryOwnershipError,
    load_history_ownership,
    merge_history_ownership,
    save_history_ownership,
)


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "history-ownership.json"


def test_ownership_ledger_persists_only_minimal_terminal_identity(tmp_path: Path):
    event = _event("zcode", "zcode:ledger-1", "business-1", 1234)
    outbox = {event.key: _outbox_item(event)}
    ledger_path = _ledger_path(tmp_path)

    ownership = merge_history_ownership({}, outbox)
    save_history_ownership(ledger_path, ownership)

    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "entries"}
    assert payload["schema_version"] == 1
    assert len(payload["entries"]) == 1
    assert set(payload["entries"][0]) == {
        "automation_id",
        "event_key",
        "source",
        "task_id",
        "status",
        "completed_at_ms",
    }
    text = ledger_path.read_text(encoding="utf-8")
    assert "private summary" not in text
    assert "源事件标记" not in text
    assert str(tmp_path) not in text

    loaded = load_history_ownership(ledger_path)
    restored = loaded[event.key]
    assert restored.event.key == event.key
    assert restored.event.source == event.source
    assert restored.event.task_id == event.task_id
    assert restored.event.status == event.status
    assert restored.event.completed_at_ms == event.completed_at_ms
    assert restored.event.title == ""
    assert restored.event.summary_text == ""
    assert restored.automation_id == outbox[event.key].automation_id
    assert restored.status == "submitted"


def test_complete_outbox_item_overrides_minimal_ledger_item_for_cleanup():
    event = _event("codex", "codex:ledger-2", "business-2", 5678)
    complete = {event.key: _outbox_item(event)}
    loaded = merge_history_ownership({}, complete)
    # 传入完整 outbox 时，现有正文对象覆盖同 key 的最小重建对象。
    merged = merge_history_ownership(loaded, complete)

    assert merged[event.key].event.title == event.title
    assert merged[event.key].event.summary_text == event.summary_text


@pytest.mark.parametrize(
    "item",
    [
        _outbox_item(
            _event(
                "zcode",
                "zcode:awaiting",
                "business-awaiting",
                1,
                status="awaiting_approval",
            )
        ),
        _outbox_item(
            _event("zcode", "zcode:pending", "business-pending", 2),
            status="pending",
        ),
        _outbox_item(
            _event("zcode", "zcode:foreign-id", "business-foreign", 3),
            value="automation-foreign",
        ),
        _outbox_item(
            _event("other", "other:source", "business-other", 4),
        ),
    ],
)
def test_non_terminal_or_unowned_items_are_not_added(item):
    assert merge_history_ownership({}, {item.event.key: item}) == {}


def test_corrupt_ledger_fails_closed_without_rewriting_original(tmp_path: Path):
    ledger_path = _ledger_path(tmp_path)
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "automation_id": "automation-foreign",
                        "event_key": "zcode:corrupt",
                        "source": "zcode",
                        "task_id": "business-corrupt",
                        "status": "completed",
                        "completed_at_ms": 9,
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    before = ledger_path.read_bytes()

    with pytest.raises(HistoryOwnershipError):
        load_history_ownership(ledger_path)

    assert ledger_path.read_bytes() == before


def test_missing_or_empty_ownership_does_not_create_file(tmp_path: Path):
    ledger_path = _ledger_path(tmp_path)

    assert load_history_ownership(ledger_path) == {}
    save_history_ownership(ledger_path, {})

    assert not ledger_path.exists()
