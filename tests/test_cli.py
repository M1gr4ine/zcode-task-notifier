import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from test_service import IntegratedFixture


def _create_directory_reparse_point(link: Path, target: Path) -> None:
    """创建并校验目录重解析点；能力不可用时让安全测试失败。"""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.fail(
                "Windows junction capability probe failed: "
                f"{type(exc).__name__}: {exc}"
            )
        if result.returncode != 0:
            pytest.fail(
                "Windows junction capability probe failed "
                f"(exit={result.returncode}): {result.stdout}{result.stderr}"
            )
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(
                "directory symlink capability probe failed: "
                f"{type(exc).__name__}: {exc}"
            )

    try:
        metadata = link.lstat()
    except OSError as exc:
        pytest.fail(
            "created reparse point cannot be inspected: "
            f"{type(exc).__name__}: {exc}"
        )
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    if not link.is_symlink() and not (attributes & 0x400):
        pytest.fail("created directory link is not a symlink or Windows reparse point")


def test_backfill_requires_exact_codex_thread_id(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    fixture.complete_codex_turn("thread-new", "turn-latest")

    from zcode_task_notifier.cli import main

    code = main(
        [
            "backfill",
            "--config",
            str(fixture.config_path),
            "--state",
            str(fixture.state_path),
            "--codex-thread",
            "thread-new",
        ]
    )

    assert code == 0
    assert fixture.automation_titles() == ["[codex] 合成 Codex 任务"]


def test_backfill_does_not_fallback_to_another_thread(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    fixture.complete_codex_turn("thread-new", "turn-latest")

    from zcode_task_notifier.cli import main

    assert (
        main(
            [
                "backfill",
                "--config",
                str(fixture.config_path),
                "--state",
                str(fixture.state_path),
                "--codex-thread",
                "thread-missing",
            ]
        )
        == 2
    )
    assert fixture.automation_titles() == []


def test_backfill_schema_error_returns_schema_exit_code(tmp_path: Path):
    fixture = IntegratedFixture.create(tmp_path, codex_enabled=True)
    connection = sqlite3.connect(fixture.codex_home / "state_example.sqlite")
    connection.execute("DROP TABLE threads")
    connection.commit()
    connection.close()

    from zcode_task_notifier.cli import main

    assert (
        main(
            [
                "backfill",
                "--config",
                str(fixture.config_path),
                "--state",
                str(fixture.state_path),
                "--codex-thread",
                "thread-new",
            ]
        )
        == 3
    )


def test_doctor_redacts_all_expanded_paths(tmp_path: Path, capsys):
    fixture = IntegratedFixture.create(tmp_path)

    from zcode_task_notifier.cli import main

    assert main(["doctor", "--config", str(fixture.config_path)]) == 0
    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    assert "%ZCODE_HOME%" in output


def test_doctor_json_contains_only_health_counts_and_redacted_paths(
    tmp_path: Path, capsys
):
    fixture = IntegratedFixture.create(tmp_path)

    from zcode_task_notifier.cli import main

    assert main(["doctor", "--config", str(fixture.config_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "healthy",
        "checks",
        "source_counts",
        "redacted_paths",
        "warnings",
        "degraded",
    }
    assert all(isinstance(value, bool) for value in payload["checks"].values())
    assert str(tmp_path) not in json.dumps(payload)


def test_baseline_json_explicitly_reports_initialized(tmp_path: Path, capsys):
    fixture = IntegratedFixture.create(tmp_path)

    from zcode_task_notifier.cli import main

    assert (
        main(
            [
                "baseline",
                "--config",
                str(fixture.config_path),
                "--state",
                str(fixture.state_path),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["initialized"] is True


def test_baseline_json_does_not_claim_initialized_after_source_failure(
    tmp_path: Path, monkeypatch, capsys
):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier import service
    from zcode_task_notifier.cli import main

    def fail_source(*args, **kwargs):
        raise RuntimeError("private source path and task content")

    monkeypatch.setattr(service, "scan_zcode_events", fail_source)

    code = main(
        [
            "baseline",
            "--config",
            str(fixture.config_path),
            "--state",
            str(fixture.state_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert payload["initialized"] is False
    assert payload["source_errors"]
    assert str(tmp_path) not in json.dumps(payload)


def test_baseline_lock_skip_keeps_zero_exit_and_reports_uninitialized_state(
    tmp_path: Path, capsys
):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.cli import main
    from zcode_task_notifier.state import ProcessLock

    lock = ProcessLock(fixture.state_path.parent / "notifier.lock")
    assert lock.acquire() is True
    try:
        code = main(
            [
                "baseline",
                "--config",
                str(fixture.config_path),
                "--state",
                str(fixture.state_path),
                "--json",
            ]
        )
    finally:
        lock.release()
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["initialized"] is False
    assert payload["skipped_due_lock"] is True


def test_baseline_source_error_cannot_report_stale_initialized_state(
    tmp_path: Path, monkeypatch, capsys
):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier import service
    from zcode_task_notifier.cli import main
    from zcode_task_notifier.models import RuntimeState
    from zcode_task_notifier.state import StateStore

    StateStore(fixture.state_path).save(RuntimeState(initialized=True))

    def fail_source(*args, **kwargs):
        raise RuntimeError("synthetic baseline failure")

    def fail_state_save(self, state):
        raise OSError("synthetic state persistence failure")

    monkeypatch.setattr(service, "scan_zcode_events", fail_source)
    monkeypatch.setattr(StateStore, "save", fail_state_save)

    code = main(
        [
            "baseline",
            "--config",
            str(fixture.config_path),
            "--state",
            str(fixture.state_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert payload["initialized"] is False
    assert payload["source_errors"]


def test_doctor_reads_explicit_state_after_legacy_retry_migration(tmp_path: Path, capsys):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.cli import main
    from zcode_task_notifier.models import Event, OutboxItem, RuntimeState
    from zcode_task_notifier.state import StateStore

    event = Event(
        source="zcode",
        key="zcode:doctor-state",
        task_id="session-doctor-state",
        title="状态检查",
        completed_at_ms=1000,
        duration_ms=1,
        summary_text="摘要",
    )
    fixture.state_path.write_text(
        json.dumps(
            {
                "initialized": True,
                "seen_event_keys": ["keep-me"],
                "log_offsets": "ignored",
                "failure_fingerprints": {"ignored": True},
                "outbox": {
                    "retry": {
                        "event": {
                            "source": event.source,
                            "key": event.key,
                            "task_id": event.task_id,
                            "title": event.title,
                            "completed_at_ms": event.completed_at_ms,
                            "duration_ms": event.duration_ms,
                            "summary_text": event.summary_text,
                        },
                        "automation_id": "automation-legacy-retry",
                        "attempt": "ignored",
                        "next_attempt_at_ms": "ignored",
                        "status": "retry_wait",
                        "submitted_at_ms": 1000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "doctor",
            "--config",
            str(fixture.config_path),
            "--state",
            str(fixture.state_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["checks"]["state_valid"] is True
    assert payload["source_counts"]["outbox_submitted"] == 1
    assert "outbox_retry_wait" not in payload["source_counts"]
    assert "outbox_exhausted" not in payload["source_counts"]
    assert "outbox_retry_wait" not in payload["checks"]
    assert "outbox_exhausted" not in payload["checks"]
    assert str(tmp_path) not in json.dumps(payload)


def test_doctor_reports_corrupt_explicit_state(tmp_path: Path, capsys):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.cli import main
    fixture.state_path.write_text("{not-json", encoding="utf-8")

    code = main(
        [
            "doctor",
            "--config",
            str(fixture.config_path),
            "--state",
            str(fixture.state_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["checks"]["state_valid"] is False
    assert fixture.state_path.exists()
    assert str(tmp_path) not in json.dumps(payload)


def test_doctor_rollout_fallback_is_healthy_but_degraded(tmp_path: Path, capsys):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.cli import main
    rollout_dir = fixture.zcode_home / "cli" / "rollout"
    rollout_dir.rmdir()

    code = main(
        [
            "doctor",
            "--config",
            str(fixture.config_path),
            "--state",
            str(fixture.state_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["healthy"] is True
    assert payload["degraded"] is True
    assert any("逐回合不可用" in warning for warning in payload["warnings"])
    assert any("仅一次终态兼容" in warning for warning in payload["warnings"])


def test_doctor_text_reports_each_degraded_warning_without_paths(tmp_path: Path, capsys):
    fixture = IntegratedFixture.create(tmp_path)
    from zcode_task_notifier.cli import main

    (fixture.zcode_home / "cli" / "rollout").rmdir()

    assert main(["doctor", "--config", str(fixture.config_path)]) == 0
    output = capsys.readouterr().out

    assert "降级：是" in output
    assert "逐回合不可用" in output
    assert "仅一次终态兼容" in output
    assert str(tmp_path) not in output


def test_doctor_and_run_share_default_state_path_for_custom_config(
    tmp_path: Path, monkeypatch, capsys
):
    fixture = IntegratedFixture.create(tmp_path)
    runtime_root = tmp_path / "runtime-default"
    runtime_root.mkdir()
    monkeypatch.setattr("zcode_task_notifier.cli._default_root", lambda: runtime_root)

    from zcode_task_notifier.cli import main
    from zcode_task_notifier.models import Event, OutboxItem, RuntimeState
    from zcode_task_notifier.state import StateStore

    event = Event(
        source="zcode",
        key="zcode:default-state",
        task_id="session-default-state",
        title="默认状态",
        completed_at_ms=1000,
        duration_ms=1,
        summary_text="摘要",
    )
    StateStore(runtime_root / "state.json").save(
        RuntimeState(
            initialized=True,
            outbox={"pending": OutboxItem(event=event, status="pending")},
        )
    )

    captured: list[Path] = []

    def fake_run(config_path: Path, state_path: Path):
        captured.append(Path(state_path))
        from zcode_task_notifier.service import RunReport

        return RunReport(0, False, [])

    monkeypatch.setattr("zcode_task_notifier.cli.run_once", fake_run)
    assert main(["run", "--config", str(fixture.config_path), "--json"]) == 0
    capsys.readouterr()
    assert captured == [runtime_root / "state.json"]

    code = main(["doctor", "--config", str(fixture.config_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["source_counts"]["outbox_pending"] == 1


def test_migrate_imports_legacy_snapshot_with_explicit_paths(tmp_path: Path):
    legacy_path = tmp_path / "snapshot.json"
    state_path = tmp_path / "state.json"
    legacy_path.write_text(
        json.dumps(
            {
                "tasks": {"session-example": {"status": "completed"}},
                "codex_turns": {"thread-example:turn-example": {"status": "completed"}},
            }
        ),
        encoding="utf-8",
    )

    from zcode_task_notifier.cli import main
    from zcode_task_notifier.state import StateStore

    assert (
        main(
            [
                "migrate",
                "--snapshot",
                str(legacy_path),
                "--state",
                str(state_path),
            ]
        )
        == 0
    )
    state = StateStore(state_path).load()
    assert "legacy-zcode:session-example:completed" in state.seen_event_keys
    assert "legacy-codex:thread-example:turn-example" in state.seen_event_keys


def test_migrate_rejects_same_snapshot_and_state_without_touching_source(
    tmp_path: Path,
):
    same_path = tmp_path / "snapshot.json"
    original = json.dumps(
        {"tasks": {"session-example": {"status": "completed"}}},
        ensure_ascii=False,
    ).encode("utf-8")
    same_path.write_bytes(original)

    from zcode_task_notifier.cli import main

    code = main(
        [
            "migrate",
            "--snapshot",
            str(same_path),
            "--state",
            str(same_path),
        ]
    )

    assert code != 0
    assert same_path.read_bytes() == original


def test_migrate_keeps_a_verified_backup_of_existing_state(tmp_path: Path):
    legacy_path = tmp_path / "snapshot.json"
    state_path = tmp_path / "state.json"
    legacy_path.write_text(
        json.dumps({"tasks": {"session-example": {"status": "completed"}}}),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"initialized": True}), encoding="utf-8")

    from zcode_task_notifier.cli import main

    assert (
        main(
            [
                "migrate",
                "--snapshot",
                str(legacy_path),
                "--state",
                str(state_path),
            ]
        )
        == 0
    )
    backups = sorted(tmp_path.glob("state.json.migrate-backup-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"initialized": True}


def test_migrate_fails_when_notifier_process_lock_is_busy(tmp_path: Path):
    legacy_path = tmp_path / "snapshot.json"
    state_path = tmp_path / "state.json"
    legacy_path.write_text(
        json.dumps({"tasks": {"session-example": {"status": "completed"}}}),
        encoding="utf-8",
    )

    from zcode_task_notifier.cli import main
    from zcode_task_notifier.state import ProcessLock

    lock = ProcessLock(tmp_path / "notifier.lock")
    assert lock.acquire() is True
    try:
        assert (
            main(
                [
                    "migrate",
                    "--snapshot",
                    str(legacy_path),
                    "--state",
                    str(state_path),
                ]
            )
            != 0
        )
        assert not state_path.exists()
    finally:
        lock.release()


def test_restore_migration_target_uses_verified_same_directory_atomic_replace(
    tmp_path: Path, monkeypatch
):
    """回滚必须先校验临时文件，再用同目录 replace 提交。"""
    import zcode_task_notifier.cli as cli

    state_path = tmp_path / "state.json"
    backup_path = tmp_path / "state.json.migrate-backup.json"
    state_path.write_text("corrupt", encoding="utf-8")
    backup_path.write_text('{"initialized": true}\n', encoding="utf-8")
    replacements: list[tuple[Path, Path]] = []
    real_replace = cli.os.replace

    def record_replace(source: str | bytes, destination: str | bytes) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", record_replace)

    cli._restore_migration_target(state_path, backup_path, target_existed=True)

    assert state_path.read_text(encoding="utf-8") == '{"initialized": true}\n'
    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == state_path.parent
    assert destination == state_path
    assert not temporary.exists()


def test_restore_migration_target_rejects_reparse_target_without_touching_link(
    tmp_path: Path,
):
    """恢复不能跟随文件或目录重解析点把备份写入目标目录外。"""
    import zcode_task_notifier.cli as cli

    outside = tmp_path / "outside"
    outside.mkdir()
    state_path = tmp_path / "state.json"
    backup_path = tmp_path / "backup.json"
    backup_path.write_text("safe", encoding="utf-8")
    _create_directory_reparse_point(state_path, outside)

    with pytest.raises(ValueError, match="重解析点"):
        cli._restore_migration_target(state_path, backup_path, target_existed=True)
    assert cli._is_reparse_point(state_path)
    assert not list(outside.iterdir())
    assert backup_path.read_text(encoding="utf-8") == "safe"


def test_restore_migration_target_rejects_reparse_ancestor_without_touching_target(
    tmp_path: Path, monkeypatch
):
    """祖先目录即使不是直接 parent，也不能把回滚写出安全边界。"""
    import pytest

    import zcode_task_notifier.cli as cli

    ancestor = tmp_path / "ancestor"
    parent = ancestor / "normal" / "tail"
    parent.mkdir(parents=True)
    state_path = parent / "state.json"
    backup_path = tmp_path / "backup.json"
    state_path.write_text("corrupt", encoding="utf-8")
    backup_path.write_text("safe", encoding="utf-8")
    real_is_reparse_point = cli._is_reparse_point

    def fake_is_reparse_point(path: Path) -> bool:
        if Path(path) == ancestor:
            return True
        return real_is_reparse_point(path)

    monkeypatch.setattr(cli, "_is_reparse_point", fake_is_reparse_point)

    with pytest.raises(ValueError):
        cli._restore_migration_target(state_path, backup_path, target_existed=True)
    assert state_path.read_text(encoding="utf-8") == "corrupt"


def test_restore_migration_target_checks_existing_ancestors_for_missing_tail(
    tmp_path: Path, monkeypatch
):
    """目标尾部尚不存在时，仍须沿现有父链检查重解析点。"""
    import pytest

    import zcode_task_notifier.cli as cli

    ancestor = tmp_path / "ancestor"
    parent = ancestor / "normal"
    parent.mkdir(parents=True)
    state_path = parent / "new" / "state.json"
    backup_path = tmp_path / "backup.json"
    backup_path.write_text("safe", encoding="utf-8")
    real_is_reparse_point = cli._is_reparse_point

    def fake_is_reparse_point(path: Path) -> bool:
        if Path(path) == ancestor:
            return True
        return real_is_reparse_point(path)

    monkeypatch.setattr(cli, "_is_reparse_point", fake_is_reparse_point)

    with pytest.raises(ValueError):
        cli._restore_migration_target(state_path, backup_path, target_existed=False)
    assert not state_path.exists()
