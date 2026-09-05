import json
import sqlite3
from pathlib import Path

import pytest

from zcode_task_notifier.config import AppConfig, save_config
from zcode_task_notifier.models import Event, OutboxItem, RuntimeState
from zcode_task_notifier.state import StateStore, state_to_json


def _make_zcode_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE tasks (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            cron_automation_id TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            started_at_ms INTEGER,
            completed_at_ms INTEGER,
            searchable_text TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE automations (
            automation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            prompt TEXT NOT NULL,
            model TEXT,
            provider TEXT,
            mode TEXT,
            thought_level TEXT,
            workspace_key TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            workspace_identity TEXT,
            target_task_id TEXT,
            bot_delivery_target TEXT,
            location_kind TEXT NOT NULL,
            recurring INTEGER NOT NULL,
            max_runs INTEGER,
            end_at INTEGER,
            schedule_rule TEXT,
            schedule_edited_by_user INTEGER NOT NULL,
            run_count INTEGER NOT NULL,
            scheduled_run_count INTEGER NOT NULL,
            enabled INTEGER NOT NULL,
            lifecycle_status TEXT NOT NULL,
            next_run_at INTEGER,
            last_run_at INTEGER,
            running INTEGER NOT NULL,
            claimed_at INTEGER,
            dispatch_status TEXT NOT NULL,
            dispatch_attempts INTEGER NOT NULL,
            retry_at INTEGER,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()
    return path


def _make_zcode_home(root: Path) -> Path:
    home = root / "zcode-home"
    (home / "v2" / "logs").mkdir(parents=True)
    (home / "cli" / "rollout").mkdir(parents=True)
    (home / "workspace-example").mkdir()
    db = _make_zcode_db(home / "v2" / "tasks-index.sqlite")
    (home / "v2" / "bot-config.json").write_text(
        json.dumps(
            {
                "bots": [
                    {
                        "id": "bot-example-0001",
                        "provider": "weixin",
                        "enabled": True,
                        "providerUserId": "wx-user-example",
                        "credentialRef": "credential-example",
                        "allowedWorkspaces": ["workspace-example"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (home / "v2" / "credentials.json").write_text(
        json.dumps({"credential-example": "enc:v1:synthetic"}), encoding="utf-8"
    )
    (home / "v2" / "bot-state.v2.json").write_text(
        json.dumps({"bot-example-0001": {"activatedAt": "2026-01-02T03:04:05Z"}}),
        encoding="utf-8",
    )
    return home


def _make_codex_home(root: Path) -> tuple[Path, Path, Path]:
    home = root / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    state_db = home / "state_example.sqlite"
    rollout = sessions / "rollout-thread-new.jsonl"
    connection = sqlite3.connect(state_db)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, rolloutPath TEXT, source TEXT, cwd TEXT, project TEXT)"
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        (
            "thread-new",
            "合成 Codex 任务",
            str(rollout),
            "user",
            "synthetic-cwd",
            "synthetic-project",
        ),
    )
    connection.commit()
    connection.close()
    return home, state_db, rollout


class IntegratedFixture:
    def __init__(
        self,
        root: Path,
        config_path: Path,
        state_path: Path,
        zcode_db: Path,
        zcode_home: Path,
        codex_home: Path | None,
        codex_rollout: Path | None,
    ):
        self.root = root
        self.config_path = config_path
        self.state_path = state_path
        self.zcode_db = zcode_db
        self.zcode_home = zcode_home
        self.codex_home = codex_home
        self.codex_rollout = codex_rollout

    @classmethod
    def create(cls, root: Path, codex_enabled: bool = False) -> "IntegratedFixture":
        zcode_home = _make_zcode_home(root)
        codex_home = codex_rollout = None
        if codex_enabled:
            codex_home, _, codex_rollout = _make_codex_home(root)
        config_path = root / "config.json"
        state_path = root / "state.json"
        save_config(
            config_path,
            AppConfig(
                zcode_home=str(zcode_home),
                notification_workspace="workspace-example",
                codex_enabled=codex_enabled,
                codex_home=str(codex_home) if codex_home is not None else "auto",
            ),
        )
        return cls(
            root,
            config_path,
            state_path,
            zcode_home / "v2" / "tasks-index.sqlite",
            zcode_home,
            codex_home,
            codex_rollout,
        )

    def complete_zcode_task(self, session_id: str) -> None:
        connection = sqlite3.connect(self.zcode_db)
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "合成 ZCode 任务",
                "completed",
                None,
                0,
                99000,
                100000,
                "合成 ZCode 摘要",
            ),
        )
        connection.commit()
        connection.close()
        rollout = self.zcode_home / "cli" / "rollout" / f"model-io-{session_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "model_io",
                    "querySource": "main_turn",
                    "turnId": "turn-new",
                    "completedAt": "1970-01-01T00:01:40Z",
                    "searchable_text": "合成 ZCode 摘要",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def complete_codex_turn(self, thread_id: str, turn_id: str) -> None:
        assert self.codex_rollout is not None
        self.codex_rollout.write_text(
            json.dumps(
                {
                    "timestamp": "1970-01-01T00:01:40Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "last_agent_message": "合成 Codex 摘要",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def automation_titles(self) -> list[str]:
        connection = sqlite3.connect(self.zcode_db)
        try:
            rows = connection.execute(
                "SELECT title FROM automations ORDER BY rowid"
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            connection.close()


def test_baseline_is_silent_then_zcode_and_codex_complete_once(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)

    from zcode_task_notifier.service import initialize_baseline, run_once

    baseline = initialize_baseline(fixture.config_path, fixture.state_path)
    assert baseline.enqueued == 0
    fixture.complete_zcode_task("session-new")
    fixture.complete_codex_turn("thread-new", "turn-new")

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 2
    assert fixture.automation_titles() == [
        "[zcode] 合成 ZCode 任务",
        "[codex] 合成 Codex 任务",
    ]
    assert run_once(fixture.config_path, fixture.state_path, now_ms=160000).enqueued == 0


def test_v2_bot_bundle_reaches_native_automation_schema_without_bot_secret(
    tmp_path: Path,
):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)

    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-native")
    fixture.complete_codex_turn("thread-new", "turn-native")

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 2
    connection = sqlite3.connect(fixture.zcode_db)
    try:
        rows = connection.execute(
            "SELECT automation_id, title, prompt, workspace_key, workspace_path, "
            "bot_delivery_target, location_kind, recurring, lifecycle_status "
            "FROM automations ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 2
    assert all(row[0].startswith("automation-tnotify-") for row in rows)
    assert rows[0][1] == "[zcode] 合成 ZCode 任务"
    assert rows[1][1] == "[codex] 合成 Codex 任务"
    assert all("不可信事件 JSON" in row[2] for row in rows)
    assert all(row[3] == str(fixture.zcode_home / "workspace-example") for row in rows)
    assert all(row[4].endswith("workspace-example") for row in rows)
    assert all(json.loads(row[5]) == {
        "provider": "weixin", "botId": "bot-example-0001",
        "providerUserId": "wx-user-example", "chatType": "private",
    } for row in rows)
    assert all(row[6:] == ("local", 0, "active") for row in rows)


def test_failed_baseline_source_does_not_mark_installation_initialized(
    tmp_path: Path, monkeypatch
):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    from zcode_task_notifier.service import initialize_baseline

    def fail_zcode(*args, **kwargs):
        raise RuntimeError("synthetic baseline source failure")

    monkeypatch.setattr("zcode_task_notifier.service.scan_zcode_events", fail_zcode)

    report = initialize_baseline(fixture.config_path, fixture.state_path)

    assert report.source_errors
    assert StateStore(fixture.state_path).load().initialized is False


def test_legacy_snapshot_imports_seen_keys_without_private_fields(tmp_path: Path):
    legacy = tmp_path / "snapshot.json"
    legacy.write_text(
        json.dumps(
            {
                "tasks": {"session-example": {"title": "合成任务", "status": "completed"}},
                "codex_turns": {"thread-example:turn-example": {"status": "completed"}},
                "ignored_private_path": "must-not-copy",
            }
        ),
        encoding="utf-8",
    )

    from zcode_task_notifier.migration import import_legacy_snapshot

    migrated = import_legacy_snapshot(legacy, RuntimeState())
    assert "legacy-zcode:session-example:completed" in migrated.seen_event_keys
    assert "legacy-codex:thread-example:turn-example" in migrated.seen_event_keys
    assert "must-not-copy" not in json.dumps(state_to_json(migrated))


def test_single_source_error_does_not_block_other_source(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_codex_turn("thread-new", "turn-new")

    def fail_zcode(*args, **kwargs):
        raise RuntimeError("synthetic zcode failure")

    monkeypatch.setattr("zcode_task_notifier.service.scan_zcode_events", fail_zcode)
    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 1
    assert report.source_errors
    assert fixture.automation_titles() == ["[codex] 合成 Codex 任务"]


def test_failed_enqueue_does_not_mark_seen(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-failed")

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("synthetic insert failure")

    monkeypatch.setattr("zcode_task_notifier.service.enqueue_automation", fail_enqueue)
    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    assert report.enqueued == 0
    state = StateStore(fixture.state_path).load()
    assert "zcode:session-failed:turn-new" not in state.seen_event_keys
    assert "zcode:session-failed:turn-new" in state.outbox


def test_outbox_save_failure_blocks_external_automation_write(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-outbox-save-failure")

    def fail_save(self, state):
        raise OSError("synthetic outbox save failure")

    monkeypatch.setattr(StateStore, "save", fail_save)

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 0
    assert report.source_errors
    assert fixture.automation_titles() == []


def test_source_scan_failure_does_not_create_delivery_log_cursor(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    log = fixture.zcode_home / "v2" / "logs" / "service.log"
    log.write_text("ordinary zcode log line\n", encoding="utf-8")

    def fail_zcode(*args, **kwargs):
        raise RuntimeError("synthetic source failure")

    monkeypatch.setattr("zcode_task_notifier.service.scan_zcode_events", fail_zcode)

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.source_errors
    persisted = StateStore(fixture.state_path).load()
    assert "log_offsets" not in state_to_json(persisted)


def test_state_save_failure_stops_following_automation_writes(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-first")
    fixture.complete_zcode_task("session-second")
    real_save = StateStore.save
    calls = {"count": 0}

    def fail_after_first_submission(self, state):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("synthetic post-submit save failure")
        return real_save(self, state)

    monkeypatch.setattr(StateStore, "save", fail_after_first_submission)

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 1
    assert len(fixture.automation_titles()) == 1


def test_sendmessage_failure_is_ignored_without_automatic_followup(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.notifier import automation_id
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-association")
    assert run_once(fixture.config_path, fixture.state_path, now_ms=100000).enqueued == 1

    identifier = automation_id("zcode:session-association:turn-new")
    log = fixture.zcode_home / "v2" / "logs" / "service.log"
    log.write_text(
        f"{identifier} /sendmessage failed: missing session association\n",
        encoding="utf-8",
    )

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert not hasattr(report, "retried")
    item = StateStore(fixture.state_path).load().outbox[
        "zcode:session-association:turn-new"
    ]
    assert item.attempt == 0
    assert item.status == "submitted"


def test_context_token_or_sendmessage_failure_never_creates_followup(
    tmp_path: Path,
):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.notifier import automation_id
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-no-followup")
    assert run_once(fixture.config_path, fixture.state_path, now_ms=100000).enqueued == 1

    identifier = automation_id("zcode:session-no-followup:turn-new")
    log = fixture.zcode_home / "v2" / "logs" / "service.log"
    log.write_text(
        f"{identifier} session_id=session-no-followup /sendmessage failed: context_token invalid\n",
        encoding="utf-8",
    )

    first = run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    second = run_once(fixture.config_path, fixture.state_path, now_ms=130000)
    assert first.enqueued == 0
    assert second.enqueued == 0
    assert fixture.automation_titles() == ["[zcode] 合成 ZCode 任务"]


def test_state_save_failure_keeps_stable_automation_id(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-save-failure")
    real_save = StateStore.save
    calls = {"count": 0}

    def fail_once(self, state):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("synthetic save failure")
        return real_save(self, state)

    monkeypatch.setattr(StateStore, "save", fail_once)
    first = run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    assert first.enqueued == 1
    monkeypatch.setattr(StateStore, "save", real_save)
    second = run_once(fixture.config_path, fixture.state_path, now_ms=160000)
    assert second.enqueued == 1
    assert fixture.automation_titles() == ["[zcode] 合成 ZCode 任务"]


def test_lock_competition_returns_skipped(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import run_once
    from zcode_task_notifier.state import ProcessLock

    lock = ProcessLock(fixture.state_path.parent / "notifier.lock")
    assert lock.acquire() is True
    try:
        report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)
    finally:
        lock.release()
    assert report.skipped_locked is True


def test_disabled_codex_does_not_touch_codex_path(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=False)
    from zcode_task_notifier.service import initialize_baseline

    def fail_codex(*args, **kwargs):
        raise AssertionError("disabled Codex must not be scanned")

    monkeypatch.setattr("zcode_task_notifier.service.scan_codex_events", fail_codex)
    report = initialize_baseline(fixture.config_path, fixture.state_path)
    assert report.enqueued == 0


def test_prune_uses_submitted_at_not_completion_time():
    from zcode_task_notifier.service import _prune_outbox

    day_ms = 24 * 60 * 60 * 1000
    event = Event(
        source="zcode",
        key="zcode:old-completion",
        task_id="session-old-completion",
        title="早完成晚投递",
        completed_at_ms=0,
        duration_ms=1,
        summary_text="摘要",
    )
    state = RuntimeState(
        outbox={
            event.key: OutboxItem(
                event=event,
                automation_id="automation-tnotify-old",
                status="submitted",
                submitted_at_ms=7 * day_ms,
            )
        }
    )

    assert _prune_outbox(state, AppConfig(outbox_retention_days=7), 8 * day_ms) is False
    assert event.key in state.outbox


def test_successful_submission_persists_submitted_at(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-submitted-at")

    assert run_once(fixture.config_path, fixture.state_path, now_ms=100000).enqueued == 1
    item = StateStore(fixture.state_path).load().outbox[
        "zcode:session-submitted-at:turn-new"
    ]
    assert item.submitted_at_ms == 100000


def test_codex_discovery_failure_keeps_zcode_running(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    from zcode_task_notifier import service

    service.initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-codex-discovery-error")
    original_discover = service.discover_paths

    def fail_codex(config, environ, user_home):
        if config.codex_enabled:
            raise RuntimeError("private Codex path and token")
        return original_discover(config, environ, user_home)

    monkeypatch.setattr(service, "discover_paths", fail_codex)

    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 1
    assert any(error.startswith("codex:") for error in report.source_errors)
    assert str(tmp_path) not in repr(report)


def test_codex_baseline_failure_preserves_source_flag_and_recovery_is_silent(
    tmp_path: Path, monkeypatch
):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    from zcode_task_notifier import service
    from zcode_task_notifier.state import StateStore
    original_scan_codex_events = service.scan_codex_events

    state = RuntimeState(
        initialized=False,
        source_initialized={"zcode": True, "codex": False},
    )
    StateStore(fixture.state_path).save(state)
    fixture.complete_codex_turn("thread-new", "turn-old")
    fixture.complete_zcode_task("session-after-codex-failure")

    def fail_codex(*args, **kwargs):
        raise RuntimeError("synthetic Codex baseline failure")

    monkeypatch.setattr(service, "scan_codex_events", fail_codex)
    first = service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert first.enqueued == 1
    failed_state = StateStore(fixture.state_path).load()
    assert failed_state.source_initialized == {"zcode": True, "codex": False}
    assert failed_state.initialized is False

    monkeypatch.setattr(service, "scan_codex_events", original_scan_codex_events)
    fixture.complete_zcode_task("session-after-codex-recovery")
    second = service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert second.enqueued == 1
    assert fixture.automation_titles() == [
        "[zcode] 合成 ZCode 任务",
        "[zcode] 合成 ZCode 任务",
    ]
    recovered_state = StateStore(fixture.state_path).load()
    assert recovered_state.source_initialized == {"zcode": True, "codex": True}
    assert recovered_state.initialized is True


def test_reverse_source_baseline_keeps_codex_incremental_and_zcode_silent(
    tmp_path: Path,
):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    from zcode_task_notifier.service import run_once
    from zcode_task_notifier.state import StateStore

    StateStore(fixture.state_path).save(
        RuntimeState(
            initialized=False,
            source_initialized={"zcode": False, "codex": True},
        )
    )
    fixture.complete_zcode_task("session-zcode-baseline")
    fixture.complete_codex_turn("thread-new", "turn-codex-incremental")

    report = run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.enqueued == 1
    assert fixture.automation_titles() == ["[codex] 合成 Codex 任务"]
    state = StateStore(fixture.state_path).load()
    assert "zcode:session-zcode-baseline:turn-new" in state.seen_event_keys
    assert state.source_initialized == {"zcode": True, "codex": True}
    assert state.initialized is True


def test_state_save_failure_after_existing_submit_stops_new_source_writes(
    tmp_path: Path, monkeypatch
):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.notifier import automation_id
    from zcode_task_notifier.service import initialize_baseline, run_once

    initialize_baseline(fixture.config_path, fixture.state_path)
    fixture.complete_zcode_task("session-existing-retry")
    assert run_once(fixture.config_path, fixture.state_path, now_ms=100000).enqueued == 1
    fixture.complete_zcode_task("session-new-after-save-failure")

    def fail_save(self, state):
        raise OSError("private state path and session")

    monkeypatch.setattr(StateStore, "save", fail_save)

    report = run_once(fixture.config_path, fixture.state_path, now_ms=130000)

    assert report.enqueued == 0
    assert len(fixture.automation_titles()) == 1


def test_report_errors_do_not_include_exception_details(tmp_path: Path, monkeypatch):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier import service

    service.initialize_baseline(fixture.config_path, fixture.state_path)

    def fail_zcode(*args, **kwargs):
        raise RuntimeError(f"{tmp_path} target=session-secret")

    monkeypatch.setattr(service, "scan_zcode_events", fail_zcode)

    report = service.run_once(fixture.config_path, fixture.state_path, now_ms=100000)

    assert report.source_errors
    assert str(tmp_path) not in repr(report)
    assert "session-secret" not in repr(report)


@pytest.mark.parametrize(
    "identity",
    [
        "".join(("C", ":", chr(92), "Users", chr(92), "alice", chr(92), "session")),
        "".join((chr(92), chr(92), "server", chr(92), "share")),
        "session/child",
        "session\\child",
        "enc:v1:secret",
        "credential-ref",
        "token-abc",
        "secret-value",
        "line\nbreak",
        "x" * 129,
        "",
        "session:turn",
    ],
)
def test_legacy_migration_rejects_unsafe_zcode_identity_and_collects_error(
    tmp_path: Path, identity: str
):
    legacy = tmp_path / "snapshot.json"
    legacy.write_text(
        json.dumps({"tasks": {identity: {"status": "completed"}}}),
        encoding="utf-8",
    )
    current = RuntimeState(seen_event_keys={"existing"})
    errors = []

    from zcode_task_notifier.migration import import_legacy_snapshot

    migrated = import_legacy_snapshot(legacy, current, errors=errors)

    assert migrated is current
    assert migrated.seen_event_keys == {"existing"}
    assert errors
    assert errors[0].code == "unsafe_identity"
    assert str(tmp_path) not in repr(errors)
    if identity:
        assert identity not in repr(errors)


def test_legacy_migration_requires_two_safe_codex_identity_components(tmp_path: Path):
    legacy = tmp_path / "snapshot.json"
    legacy.write_text(
        json.dumps(
            {
                "codex_turns": {
                    "thread-example:turn-example": {"status": "completed"}
                }
            }
        ),
        encoding="utf-8",
    )
    current = RuntimeState()
    errors = []

    from zcode_task_notifier.migration import import_legacy_snapshot

    migrated = import_legacy_snapshot(legacy, current, errors=errors)

    assert migrated.seen_event_keys == {
        "legacy-codex:thread-example:turn-example"
    }
    assert errors == []

    for invalid in ("thread-only", "thread:turn:extra", "thread:/turn", "thread:token"):
        legacy.write_text(
            json.dumps({"codex_turns": {invalid: {"status": "completed"}}}),
            encoding="utf-8",
        )
        errors = []
        before = set(current.seen_event_keys)
        migrated = import_legacy_snapshot(legacy, current, errors=errors)
        assert migrated is current
        assert migrated.seen_event_keys == before
        assert errors[0].code == "unsafe_identity"


def test_legacy_migration_parse_failure_keeps_state_and_reports_code(tmp_path: Path):
    legacy = tmp_path / "snapshot.json"
    legacy.write_text("{not-json", encoding="utf-8")
    current = RuntimeState(seen_event_keys={"existing"})
    errors = []

    from zcode_task_notifier.migration import import_legacy_snapshot

    migrated = import_legacy_snapshot(legacy, current, errors=errors)

    assert migrated is current
    assert migrated.seen_event_keys == {"existing"}
    assert [error.code for error in errors] == ["invalid_json"]
    assert str(tmp_path) not in repr(errors)
