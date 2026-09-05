import sqlite3

from zcode_task_notifier import service
from zcode_task_notifier.models import RuntimeState
from zcode_task_notifier.state import StateStore
import test_service as fixtures
from test_history_cleanup import _create_db, _add_candidate, _outbox_item, _deleted_tasks


def native_fixture(tmp_path, monkeypatch):
    def create_native_db(path):
        _create_db(path)
        return path
    monkeypatch.setattr(fixtures, "_make_zcode_db", create_native_db)
    fixture = fixtures.IntegratedFixture.create(tmp_path)
    state = RuntimeState(initialized=True)
    workspace = fixture.zcode_home / "workspace-example"
    with sqlite3.connect(fixture.zcode_db) as connection:
        for index in range(11):
            event, _, _ = _add_candidate(connection, workspace, index)
            state.outbox[event.key] = _outbox_item(event)
            state.seen_event_keys.add(event.key)
    StateStore(fixture.state_path).save(state)
    return fixture


def test_main_loop_cleans_owned_history_before_outbox_expiry(tmp_path, monkeypatch):
    fixture = native_fixture(tmp_path, monkeypatch)
    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=8 * 86400000)
    assert report.cleanup_deleted == 1
    assert report.cleanup_warnings == []
    assert report.enqueued == 0
    assert len(_deleted_tasks(fixture.zcode_db)) == 1
    assert StateStore(fixture.state_path).load().outbox == {}
    assert (tmp_path / "history-ownership.json").exists()
    assert service.run_once(fixture.config_path, fixture.state_path, now_ms=9 * 86400000).cleanup_deleted == 0


def test_explicit_baseline_never_cleans_existing_history(tmp_path, monkeypatch):
    fixture = native_fixture(tmp_path, monkeypatch)
    report = service.initialize_baseline(fixture.config_path, fixture.state_path)
    assert report.cleanup_deleted == 0
    assert _deleted_tasks(fixture.zcode_db) == set()
    assert not (tmp_path / "history-cleanup.jsonl").exists()


def test_first_run_baseline_defers_cleanup_until_next_incremental_run(tmp_path, monkeypatch):
    fixture = native_fixture(tmp_path, monkeypatch)
    store = StateStore(fixture.state_path)
    state = store.load()
    state.initialized = False
    state.source_initialized = {"zcode": False, "codex": False}
    store.save(state)
    assert service.run_once(fixture.config_path, fixture.state_path, now_ms=100000).cleanup_deleted == 0
    assert _deleted_tasks(fixture.zcode_db) == set()
    assert service.run_once(fixture.config_path, fixture.state_path, now_ms=160000).cleanup_deleted == 1


def test_cleanup_failure_is_reported_without_blocking_notifications(tmp_path, monkeypatch):
    fixture = fixtures.IntegratedFixture.create(tmp_path)
    service.initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("business-new")

    def fail_cleanup(*args, **kwargs):
        raise OSError("synthetic private details must not escape")

    monkeypatch.setattr(service, "run_history_cleanup", fail_cleanup)
    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    assert report.enqueued == 1
    assert report.source_errors == []
    assert report.cleanup_warnings == ["history_cleanup:OSError"]
    assert "private details" not in repr(report)
