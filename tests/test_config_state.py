import json
from pathlib import Path

import pytest

from zcode_task_notifier.config import AppConfig, ConfigError, load_config, save_config
from zcode_task_notifier.models import Event, OutboxItem, RuntimeState
from zcode_task_notifier.state import ProcessLock, StateError, StateStore, state_from_json


def test_default_config_is_zcode_only_and_uses_auto_paths(tmp_path: Path):
    path = tmp_path / "config.json"
    save_config(path, AppConfig())
    loaded = load_config(path)
    assert loaded.zcode_home == "auto"
    assert loaded.notification_workspace == "auto"
    assert loaded.codex_enabled is False
    assert loaded.codex_home == "auto"
    assert loaded.codex_prefix == "[codex]"
    assert loaded.outbox_retention_days == 7


def test_config_preserves_user_paths_and_ignores_unknown_fields(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "zcode_home": "custom/zcode-home",
                "notification_workspace": "custom/notification-workspace",
                "unknown_future_option": "ignored",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_config(path)

    assert loaded.zcode_home == "custom/zcode-home"
    assert loaded.notification_workspace == "custom/notification-workspace"


def test_task_one_does_not_register_cli_before_cli_module_exists():
    project_path = Path(__file__).parents[1] / "pyproject.toml"
    project_text = project_path.read_text(encoding="utf-8")

    assert "[project.scripts]" not in project_text
    assert "zcode-task-notifier = \"zcode_task_notifier.cli:main\"" not in project_text


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"interval_seconds": 59},
        {"codex_prefix": "codex"},
    ],
)
def test_invalid_config_values_are_rejected(tmp_path: Path, changes: dict[str, object]):
    path = tmp_path / "config.json"
    payload = {"schema_version": 1, **changes}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_state_schema_version_rejects_boolean_true():
    with pytest.raises(StateError):
        state_from_json({"schema_version": True})


def test_state_save_is_atomic_and_round_trips_sets_and_nested_outbox(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    event = Event(
        source="zcode",
        key="zcode:session:1",
        task_id="session-1",
        title="完成任务",
        completed_at_ms=1700000000000,
        duration_ms=1200,
        summary_text="任务摘要",
        turn_id="turn-1",
    )
    state = RuntimeState(
        initialized=True,
        seen_event_keys={"zcode:session:1"},
        zcode_rollout_offsets={"rollout.jsonl": 12},
        zcode_last_turns={"session-1": "turn-1"},
        rollout_offsets={"codex.jsonl": 8},
        rollout_turn_started_ms={"turn-1": 1699999999000},
        outbox={
            event.key: OutboxItem(
                event=event,
                automation_id="automation-1",
                submitted_at_ms=1700000004000,
                status="submitted",
            )
        },
    )

    store.save(state)
    loaded = store.load()

    assert loaded == state
    assert isinstance(loaded.outbox[event.key], OutboxItem)
    assert isinstance(loaded.outbox[event.key].event, Event)
    assert list(tmp_path.glob("state.json.tmp-*")) == []


def test_old_outbox_without_submitted_at_is_preserved_safely(tmp_path: Path):
    payload = _valid_state_payload()
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = StateStore(path).load()

    assert loaded.outbox["event-key"].submitted_at_ms is None


def test_submitted_at_is_validated_and_round_tripped(tmp_path: Path):
    payload = _valid_state_payload()
    payload["outbox"]["event-key"]["submitted_at_ms"] = 1700000004000  # type: ignore[index]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = StateStore(path).load()

    assert loaded.outbox["event-key"].submitted_at_ms == 1700000004000


def test_invalid_submitted_at_quarantines_state(tmp_path: Path):
    payload = _valid_state_payload()
    payload["outbox"]["event-key"]["submitted_at_ms"] = "late"  # type: ignore[index]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = StateStore(path).load()

    assert loaded == RuntimeState()
    assert not path.exists()


def test_legacy_initialized_state_migrates_to_initialized_sources(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_valid_state_payload()), encoding="utf-8")

    loaded = StateStore(path).load()

    assert loaded.initialized is True
    assert loaded.source_initialized == {"zcode": True, "codex": True}


def test_source_initialized_flags_round_trip_independently(tmp_path: Path):
    path = tmp_path / "state.json"
    state = RuntimeState(
        initialized=False,
        source_initialized={"zcode": True, "codex": False},
    )

    StateStore(path).save(state)

    assert StateStore(path).load().source_initialized == {
        "zcode": True,
        "codex": False,
    }


def test_rollout_file_identities_round_trip_with_offsets(tmp_path: Path):
    path = tmp_path / "state.json"
    state = RuntimeState(
        zcode_rollout_offsets={"zcode.jsonl": 12},
        zcode_rollout_identities={"zcode.jsonl": "zcode-file-identity"},
        rollout_offsets={"codex.jsonl": 34},
        rollout_identities={"codex.jsonl": "codex-file-identity"},
    )

    StateStore(path).save(state)

    loaded = StateStore(path).load()

    assert loaded.zcode_rollout_offsets == {"zcode.jsonl": 12}
    assert loaded.zcode_rollout_identities == {"zcode.jsonl": "zcode-file-identity"}
    assert loaded.rollout_offsets == {"codex.jsonl": 34}
    assert loaded.rollout_identities == {"codex.jsonl": "codex-file-identity"}


def test_corrupt_state_is_quarantined_and_returns_uninitialized_state(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    loaded = StateStore(path).load()

    assert loaded == RuntimeState()
    assert not path.exists()
    corrupt_files = list(tmp_path.glob("state.corrupt-*.json"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{not-json"


def _valid_state_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "initialized": True,
        "outbox": {
            "event-key": {
                "event": {
                    "source": "zcode",
                    "key": "event-key",
                    "task_id": "task-1",
                    "title": "任务",
                    "completed_at_ms": 1700000000000,
                    "duration_ms": 1000,
                    "summary_text": "摘要",
                    "status": "completed",
                    "turn_id": "turn-1",
                },
                "automation_id": "automation-1",
                "attempt": 0,
                "next_attempt_at_ms": 0,
                "status": "pending",
            }
        },
    }


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("event", "source", "other"),
        ("event", "status", "bogus"),
        ("event", "completed_at_ms", "1700000000000"),
        ("event", "duration_ms", True),
        ("outbox", "status", "bogus"),
    ],
)
def test_semantically_invalid_nested_state_is_quarantined(
    tmp_path: Path, section: str, field: str, value: object
):
    payload = _valid_state_payload()
    outbox_item = payload["outbox"]["event-key"]  # type: ignore[index]
    target = outbox_item["event"] if section == "event" else outbox_item  # type: ignore[index]
    target[field] = value  # type: ignore[index]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = StateStore(path).load()

    assert loaded == RuntimeState()
    assert not path.exists()
    assert len(list(tmp_path.glob("state.corrupt-*.json"))) == 1


def test_state_io_error_is_not_treated_as_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_valid_state_payload()), encoding="utf-8")

    def deny_open(*args: object, **kwargs: object):
        raise PermissionError("simulated state read failure")

    monkeypatch.setattr(Path, "open", deny_open)

    with pytest.raises(StateError) as exc_info:
        StateStore(path).load()

    assert isinstance(exc_info.value.__cause__, PermissionError)
    assert path.exists()
    assert list(tmp_path.glob("state.corrupt-*.json")) == []


def test_process_lock_rejects_second_instance(tmp_path: Path):
    first = ProcessLock(tmp_path / "notifier.lock")
    second = ProcessLock(tmp_path / "notifier.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()


def test_process_lock_is_a_context_manager(tmp_path: Path):
    path = tmp_path / "notifier.lock"
    with ProcessLock(path):
        assert ProcessLock(path).acquire() is False


def test_legacy_retry_config_fields_are_ignored_even_when_invalid(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "retry_delays_seconds": ["not-a-delay"],
                "max_retry_attempts": "not-a-count",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_config(path)

    assert loaded == AppConfig()
    assert not hasattr(loaded, "retry_delays_seconds")
    save_config(path, loaded)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "retry_delays_seconds" not in saved
    assert "max_retry_attempts" not in saved


@pytest.mark.parametrize("legacy_status", ["retry_wait", "exhausted"])
def test_legacy_retry_state_is_submitted_without_losing_seen_keys(
    tmp_path: Path, legacy_status: str
):
    payload = _valid_state_payload()
    payload["seen_event_keys"] = ["keep-me"]
    outbox_item = payload["outbox"]["event-key"]  # type: ignore[index]
    outbox_item["automation_id"] = "automation-legacy-retry"  # type: ignore[index]
    outbox_item["attempt"] = {"unexpected": "value"}  # type: ignore[index]
    outbox_item["next_attempt_at_ms"] = "unexpected"  # type: ignore[index]
    outbox_item["status"] = legacy_status  # type: ignore[index]
    outbox_item["submitted_at_ms"] = 1700000004000  # type: ignore[index]
    payload["log_offsets"] = "ignored"  # type: ignore[index]
    payload["failure_fingerprints"] = {"ignored": True}  # type: ignore[index]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = StateStore(path).load()
    item = loaded.outbox["event-key"]

    assert loaded.seen_event_keys == {"keep-me"}
    assert item.status == "submitted"
    assert item.automation_id == "automation-legacy-retry"
    assert item.attempt == 0
    assert item.next_attempt_at_ms == 0
    assert item.submitted_at_ms == 1700000004000

    StateStore(path).save(loaded)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "log_offsets" not in saved
    assert "failure_fingerprints" not in saved
    assert "attempt" not in saved["outbox"]["event-key"]
    assert "next_attempt_at_ms" not in saved["outbox"]["event-key"]
