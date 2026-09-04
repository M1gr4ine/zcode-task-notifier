"""zcode-task-notifier 的命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import sys
from typing import Any, Sequence
from uuid import uuid4

from .migration import import_legacy_snapshot
from .service import DoctorReport, RunReport, backfill_codex, doctor, initialize_baseline, run_once
from .state import ProcessLock, StateStore


def _default_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value) / "ZCodeTaskNotifier"
    return Path.home() / ".zcode-task-notifier"


def _path(value: str | None, default: Path) -> Path:
    if value is None:
        return default
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zcode-task-notifier")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "baseline"):
        command = subparsers.add_parser(name)
        command.add_argument("--config")
        command.add_argument("--state")
        command.add_argument("--json", action="store_true", dest="as_json")
    command = subparsers.add_parser("doctor")
    command.add_argument("--config")
    command.add_argument("--state")
    command.add_argument("--json", action="store_true", dest="as_json")
    command = subparsers.add_parser("backfill")
    command.add_argument("--config")
    command.add_argument("--state")
    command.add_argument("--codex-thread")
    command.add_argument("--json", action="store_true", dest="as_json")
    command = subparsers.add_parser("migrate")
    command.add_argument("--snapshot", required=True)
    command.add_argument("--state", required=True)
    command.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _print_run(
    report: RunReport,
    as_json: bool,
    *,
    initialized: bool | None = None,
) -> None:
    payload = asdict(report)
    if initialized is not None:
        payload["initialized"] = initialized
    if report.skipped_locked:
        payload["skipped_due_lock"] = True
    if as_json:
        _print_json(payload)
        return
    if report.skipped_locked:
        print("已跳过：已有通知轮次正在运行")
    else:
        print(f"已入队 {report.enqueued}")
    for error in report.source_errors:
        print(f"错误：{error}")


def _print_doctor(report: DoctorReport, as_json: bool) -> None:
    if as_json:
        _print_json(asdict(report))
        return
    print("健康：" + ("是" if report.healthy else "否"))
    print("检查：" + json.dumps(report.checks, ensure_ascii=False, sort_keys=True))
    print("计数：" + json.dumps(report.source_counts, ensure_ascii=False, sort_keys=True))
    print("路径：" + json.dumps(report.redacted_paths, ensure_ascii=False, sort_keys=True))
    print("降级：" + ("是" if report.degraded else "否"))
    for warning in report.warnings:
        print("警告：" + warning)


def _canonical_path(path: Path) -> Path:
    """规范化迁移路径，跟随已有链接并允许尚不存在的目标。"""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except OSError as exc:
        raise ValueError("迁移路径无法规范化") from exc


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    """在不跟随链接的前提下识别 Windows 重解析点及各平台 symlink。"""
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    # FILE_ATTRIBUTE_REPARSE_POINT；Windows 之外没有该字段时保持 0。
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _iter_lexical_path_ancestors(path: Path):
    """按词法逐级返回路径，避免用 resolve 先跟随中间链接。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    current = target
    while True:
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


def _assert_safe_restore_path(path: Path, *, allow_missing: bool = False) -> None:
    """恢复前拒绝目标及任一祖先通过链接把写入导出到产品目录外。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target_exists = False
    for ancestor in _iter_lexical_path_ancestors(target):
        try:
            if _is_reparse_point(ancestor):
                raise ValueError("迁移恢复路径不能包含重解析点")
            if ancestor == target:
                target_exists = True
        except FileNotFoundError:
            continue
    if not target_exists and not allow_missing:
        raise FileNotFoundError(target)


def _verified_migration_backup(state_path: Path) -> Path | None:
    """在读取目标状态前创建可核验的旁路备份。"""
    if not state_path.exists():
        return None
    _assert_safe_restore_path(state_path)
    if not state_path.is_file():
        raise ValueError("迁移目标状态不是普通文件")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source_digest = _file_digest(state_path)
    backup = state_path.with_name(
        f"{state_path.name}.migrate-backup-{stamp}-{source_digest[:12]}-{uuid4().hex[:12]}.json"
    )
    if os.path.lexists(str(backup)):
        raise FileExistsError("迁移备份已存在")
    try:
        shutil.copy2(state_path, backup)
        if not backup.is_file() or _file_digest(backup) != source_digest:
            raise OSError("迁移备份校验失败")
    except Exception:
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup


def _restore_migration_target(
    state_path: Path,
    backup: Path | None,
    target_existed: bool,
) -> None:
    if backup is not None:
        backup_path = Path(backup)
        _assert_safe_restore_path(state_path, allow_missing=True)
        _assert_safe_restore_path(backup_path)
        if not backup_path.is_file():
            raise ValueError("迁移备份不是普通文件")
        expected_digest = _file_digest(backup_path)
        temporary = state_path.with_name(
            f".{state_path.name}.restore-{os.getpid()}-{uuid4().hex}"
        )
        try:
            with backup_path.open("rb") as source, temporary.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if _file_digest(temporary) != expected_digest:
                raise OSError("迁移备份摘要校验失败")
            os.replace(temporary, state_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    elif not target_existed and state_path.exists():
        _assert_safe_restore_path(state_path)
        if not state_path.is_file():
            raise ValueError("迁移新状态不是普通文件")
        state_path.unlink()


class _MigrationValidationError(ValueError):
    """保留迁移校验的稳定错误码，同时允许调用方安全恢复目标。"""

    def __init__(self, errors: Sequence[Any]):
        super().__init__("快照校验失败")
        self.errors = tuple(errors)


def _migrate_snapshot(snapshot_path: Path, state_path: Path) -> Any:
    """在通知器同一进程锁内安全完成旧快照迁移。"""
    snapshot = _canonical_path(snapshot_path)
    _assert_safe_restore_path(Path(state_path), allow_missing=True)
    state = _canonical_path(state_path)
    if snapshot == state:
        raise ValueError("迁移源与目标不能相同")
    state.parent.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(state.parent / "notifier.lock")
    if not lock.acquire():
        raise RuntimeError("通知器正在运行")
    target_existed = state.exists()
    backup: Path | None = None
    try:
        backup = _verified_migration_backup(state)
        store = StateStore(state)
        current = store.load()
        migration_errors: list[Any] = []
        migrated = import_legacy_snapshot(snapshot, current, errors=migration_errors)
        if migration_errors:
            raise _MigrationValidationError(migration_errors)
        store.save(migrated)
        return backup
    except Exception:
        try:
            _restore_migration_target(state, backup, target_existed)
        except Exception as restore_error:
            raise RuntimeError("迁移失败且无法恢复目标状态") from restore_error
        raise
    finally:
        lock.release()


def _exit_for_report(report: RunReport) -> int:
    if report.skipped_locked:
        return 0
    if not report.source_errors:
        return 0
    if any(error.startswith(("config:", "discovery:")) for error in report.source_errors):
        return 2
    return 3


def _read_baseline_initialized(state_path: Path) -> bool:
    """只在 baseline 输出前读取持久状态，避免虚报初始化成功。"""
    try:
        return bool(StateStore(state_path).load_strict().initialized)
    except Exception:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """运行命令并返回约定的 0/2/3 状态码。"""
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    root = _default_root()
    config_path = _path(getattr(args, "config", None), root / "config.json")
    state_path = _path(getattr(args, "state", None), root / "state.json")
    try:
        if args.command == "run":
            report = run_once(config_path, state_path)
            _print_run(report, args.as_json)
            return _exit_for_report(report)
        if args.command == "baseline":
            report = initialize_baseline(config_path, state_path)
            initialized = _read_baseline_initialized(state_path) and not report.source_errors
            _print_run(report, args.as_json, initialized=initialized)
            status = _exit_for_report(report)
            if not initialized and status == 0 and not report.skipped_locked:
                return 3
            return status
        if args.command == "doctor":
            doctor_state_path = _path(getattr(args, "state", None), state_path)
            report = doctor(config_path, doctor_state_path)
            _print_doctor(report, args.as_json)
            return 0 if report.healthy else 2
        if args.command == "backfill":
            if not args.codex_thread or not args.codex_thread.strip():
                print("错误：必须指定 --codex-thread", file=sys.stderr)
                return 2
            identifier = backfill_codex(config_path, state_path, args.codex_thread)
            if args.as_json:
                _print_json({"automation_id": identifier})
            else:
                print("已补发指定 Codex 任务")
            return 0
        if args.command == "migrate":
            state_path = _path(args.state, root / "state.json")
            backup = _migrate_snapshot(
                _path(args.snapshot, root / "snapshot.json"),
                state_path,
            )
            if args.as_json:
                _print_json({"migrated": True, "backup_created": backup is not None})
            else:
                print("旧快照迁移完成")
            return 0
    except _MigrationValidationError as exc:
        if args.command == "migrate":
            if args.as_json:
                _print_json(
                    {
                        "migrated": False,
                        "errors": [str(error.code) for error in exc.errors],
                    }
                )
            else:
                print("迁移未执行：快照校验失败")
            return 2
        raise
    except Exception as exc:
        # 命令行不打印异常正文，避免路径、目标、提示词或会话内容泄露。
        kind = "discovery" if exc.__class__.__name__ in {"DiscoveryError", "ConfigError"} else "schema"
        print(f"错误：{kind}:{type(exc).__name__}", file=sys.stderr)
        return 2 if kind == "discovery" else 3
    return 2


__all__ = ["main"]
