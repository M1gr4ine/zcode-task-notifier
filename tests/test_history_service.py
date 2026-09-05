import json
from pathlib import Path
import sqlite3

import pytest

from test_history_cleanup import _add_candidate, _create_db, _deleted_tasks, _outbox_item, _workspace
from zcode_task_notifier import cleanup_service
from zcode_task_notifier.models import OutboxItem


def _database_with_candidates(
    tmp_path: Path, count: int = 6
) -> tuple[Path, Path, dict[str, OutboxItem]]:
    db_path = tmp_path / "tasks-index.sqlite"
    workspace = _workspace(tmp_path)
    _create_db(db_path)
    outbox: dict[str, OutboxItem] = {}
    with sqlite3.connect(db_path) as connection:
        for index in range(count):
            event, _, _ = _add_candidate(connection, workspace, index)
            outbox[event.key] = _outbox_item(event)
    return db_path, workspace, outbox


def _audit_records(state_path: Path) -> list[dict[str, object]]:
    audit_path = state_path.parent / "history-cleanup.jsonl"
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_run_history_cleanup_writes_durable_intent_and_commit_without_sensitive_data(
    tmp_path: Path,
):
    db_path, workspace, outbox = _database_with_candidates(tmp_path)
    state_path = tmp_path / "runtime" / "state.json"
    now_ms = 1_700_000_000_000

    report = cleanup_service.run_history_cleanup(
        db_path, outbox, workspace, state_path, now_ms
    )

    assert report.deleted_count == 1
    assert len(_deleted_tasks(db_path)) == 1
    records = _audit_records(state_path)
    assert [record["phase"] for record in records] == ["intent", "committed"]
    allowed = {
        "time_ms",
        "phase",
        "deleted_count",
        "deleted_task_ids_hash",
        "skipped",
    }
    assert all(set(record) == allowed for record in records)
    assert all(record["time_ms"] == now_ms for record in records)
    assert all(record["deleted_count"] == 1 for record in records)
    audit_text = (state_path.parent / "history-cleanup.jsonl").read_text(
        encoding="utf-8"
    )
    assert str(tmp_path) not in audit_text
    assert "notification-zcode-0" not in audit_text
    assert "private summary" not in audit_text
    assert not (state_path.parent / "history-ownership.json").exists()


def test_unwritable_runtime_state_skips_cleanup_before_sqlite_commit(tmp_path: Path):
    db_path, workspace, outbox = _database_with_candidates(tmp_path)
    audit_parent = tmp_path / "not-a-directory"
    audit_parent.write_text("sentinel", encoding="utf-8")
    state_path = audit_parent / "state.json"

    with pytest.raises(cleanup_service.HistoryAuditError, match="intent"):
        cleanup_service.run_history_cleanup(
            db_path, outbox, workspace, state_path, 1_700_000_000_001
        )

    assert _deleted_tasks(db_path) == set()
    assert audit_parent.read_text(encoding="utf-8") == "sentinel"


def test_zero_candidate_does_not_create_audit_or_ownership_file(tmp_path: Path):
    db_path, workspace, outbox = _database_with_candidates(tmp_path, count=5)
    state_path = tmp_path / "runtime" / "state.json"

    report = cleanup_service.run_history_cleanup(
        db_path, outbox, workspace, state_path, 1_700_000_000_002
    )

    assert report.deleted_count == 0
    assert not (state_path.parent / "history-cleanup.jsonl").exists()
    assert not (state_path.parent / "history-ownership.json").exists()


def test_repeated_cleanup_does_not_append_duplicate_audit_records(tmp_path: Path):
    db_path, workspace, outbox = _database_with_candidates(tmp_path)
    state_path = tmp_path / "runtime" / "state.json"

    first = cleanup_service.run_history_cleanup(
        db_path, outbox, workspace, state_path, 1_700_000_000_003
    )
    second = cleanup_service.run_history_cleanup(
        db_path, {}, workspace, state_path, 1_700_000_000_004
    )

    assert first.deleted_count == 1
    assert second.deleted_count == 0
    records = _audit_records(state_path)
    assert len(records) == 2
    assert [record["phase"] for record in records] == ["intent", "committed"]


def test_committed_audit_failure_is_explicit_and_keeps_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path, workspace, outbox = _database_with_candidates(tmp_path)
    state_path = tmp_path / "runtime" / "state.json"
    real_append = cleanup_service._append_audit_record

    def fail_committed(
        audit_path: Path,
        report,
        *,
        now_ms: int,
        phase: str,
    ) -> None:
        if phase == "committed":
            raise cleanup_service.HistoryAuditError("committed 审计写入失败")
        real_append(audit_path, report, now_ms=now_ms, phase=phase)

    monkeypatch.setattr(cleanup_service, "_append_audit_record", fail_committed)

    with pytest.raises(cleanup_service.HistoryAuditError, match="committed"):
        cleanup_service.run_history_cleanup(
            db_path, outbox, workspace, state_path, 1_700_000_000_005
        )

    assert len(_deleted_tasks(db_path)) == 1
    records = _audit_records(state_path)
    assert [record["phase"] for record in records] == ["intent"]


def test_corrupt_ownership_ledger_does_not_block_new_tagged_cleanup(tmp_path: Path):
    db_path, workspace, outbox = _database_with_candidates(tmp_path)
    state_path = tmp_path / "runtime" / "state.json"
    ledger_path = state_path.parent / "history-ownership.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("{not-json", encoding="utf-8")
    before = ledger_path.read_bytes()

    report = cleanup_service.run_history_cleanup(
        db_path, {}, workspace, state_path, 1_700_000_000_006
    )

    assert report.deleted_count == 1
    assert len(_deleted_tasks(db_path)) == 1
    assert ledger_path.read_bytes() == before
