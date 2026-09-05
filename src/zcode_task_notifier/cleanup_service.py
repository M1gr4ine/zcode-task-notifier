"""历史清理服务包装：在软删除前后持久化脱敏审计。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .history_cleanup import CleanupReport, HistoryCleanupError, cleanup_history
from .history_ownership import (
    HISTORY_OWNERSHIP_FILENAME,
    HistoryOwnershipError,
    load_history_ownership,
    merge_history_ownership,
    ownership_signature,
    save_history_ownership,
)
from .models import OutboxItem


class HistoryAuditError(HistoryCleanupError):
    """历史清理审计无法持久化，或删除已提交但提交审计失败。"""


_AUDIT_FILENAME = "history-cleanup.jsonl"
_AUDIT_PHASES = frozenset({"intent", "committed"})


def _audit_payload(report: CleanupReport, *, now_ms: int, phase: str) -> dict[str, object]:
    if phase not in _AUDIT_PHASES:
        raise ValueError("历史清理审计 phase 无效")
    return {
        "time_ms": now_ms,
        "phase": phase,
        "deleted_count": report.deleted_count,
        "deleted_task_ids_hash": report.deleted_task_ids_hash,
        "skipped": dict(report.skipped),
    }


def _append_audit_record(
    audit_path: Path,
    report: CleanupReport,
    *,
    now_ms: int,
    phase: str,
) -> None:
    """追加一条白名单审计记录并确保写入操作完成。"""
    payload = _audit_payload(report, now_ms=now_ms, phase=phase)
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise HistoryAuditError(f"{phase} 审计写入失败") from exc


def run_history_cleanup(
    db_path: Path,
    outbox: Mapping[str, OutboxItem],
    workspace: Path,
    state_path: Path,
    now_ms: int,
) -> CleanupReport:
    """执行保留十条规则，并为实际软删除持久化 intent/committed 审计。

    intent 由 ``cleanup_history`` 在持锁事务内调用，因此该记录无法写入时
    不会发生数据库删除。数据库提交后再写 committed；若该后置写入失败，
    直接报告已提交而不尝试宣称回滚。
    """
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("now_ms 必须是非负整数")

    audit_path = Path(state_path).parent / _AUDIT_FILENAME
    ownership_path = Path(state_path).parent / HISTORY_OWNERSHIP_FILENAME

    def skip_cleanup() -> CleanupReport:
        # 归属账本损坏或无法持久化时保持 fail-closed；空 outbox 不会打开或
        # 写入数据库，只构造一个可由调用方忽略的零删除报告。
        return cleanup_history(Path(db_path), {}, Path(workspace), keep=10)

    try:
        existing_ownership = load_history_ownership(ownership_path)
        merged_ownership = merge_history_ownership(existing_ownership, outbox)
        if ownership_signature(merged_ownership) != ownership_signature(
            existing_ownership
        ):
            save_history_ownership(ownership_path, merged_ownership)
    except HistoryOwnershipError:
        return skip_cleanup()

    def write_intent(report: CleanupReport) -> None:
        _append_audit_record(audit_path, report, now_ms=now_ms, phase="intent")

    try:
        report = cleanup_history(
            Path(db_path),
            merged_ownership,
            Path(workspace),
            keep=10,
            before_delete=write_intent,
        )
    except HistoryCleanupError as exc:
        if isinstance(exc.__cause__, HistoryAuditError):
            raise HistoryAuditError("intent 审计写入失败，历史删除已阻止") from exc
        raise

    if report.deleted_count == 0:
        return report

    try:
        _append_audit_record(
            audit_path,
            report,
            now_ms=now_ms,
            phase="committed",
        )
    except HistoryAuditError as exc:
        raise HistoryAuditError("历史删除已提交，但 committed 审计写入失败") from exc
    return report


__all__ = ["HistoryAuditError", "run_history_cleanup"]
