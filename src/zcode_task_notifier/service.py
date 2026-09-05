"""通知监控器的单轮编排和运维服务。

每一轮只在获得进程锁后读取配置、状态和动态路径；来源扫描相互隔离，
新事件先落入本地 outbox，再逐个写入 ZCode 自动化表。首次写入或状态保存
失败时保留 pending 事件，下一轮使用同一个首发自动化 ID 幂等恢复；微信
发送失败不会触发本程序的后续自动化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Sequence

from .codex_source import (
    CodexSourceError,
    backfill_codex_thread,
    scan_codex_events,
)
from .config import AppConfig, ConfigError, load_config
from .discovery import DiscoveryError, discover_paths, load_weixin_target, redact_path
from .models import Event, OutboxItem, RuntimeState
from .notifier import (
    AutomationSchemaError,
    AutomationWriteError,
    _validate_automations_schema,
    enqueue_automation,
)
from .state import ProcessLock, StateError, StateStore
from .zcode_source import ZCodeSchemaError, connect_readonly, scan_zcode_events


@dataclass(frozen=True)
class RunReport:
    enqueued: int
    skipped_locked: bool
    source_errors: list[str]


@dataclass(frozen=True)
class DoctorReport:
    healthy: bool
    checks: dict[str, bool]
    source_counts: dict[str, int]
    redacted_paths: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False


_DAY_MS = 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_path_label(path: Path) -> str:
    """仅用于内部错误分类，绝不返回用户路径。"""
    return path.name or "path"


def _error(label: str, exc: BaseException) -> str:
    """生成不含路径、凭据、提示词或会话内容的错误摘要。"""
    return f"{label}:{type(exc).__name__}"


def _lock_path(state_path: Path) -> Path:
    # 设计中的运行态文件名固定为 state.json / notifier.lock。
    return Path(state_path).parent / "notifier.lock"


def _invalid_now(now_ms: int | None) -> int:
    if now_ms is None:
        return _now_ms()
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("now_ms 必须是非负整数")
    return now_ms


def _source_error(label: str, exc: BaseException) -> str:
    if isinstance(exc, ConfigError):
        return _error("config", exc)
    if isinstance(exc, DiscoveryError):
        category = label if label in {"codex", "zcode", "zcode-log"} else "discovery"
        return _error(category, exc)
    if isinstance(exc, (StateError, OSError)):
        return _error("state", exc)
    if isinstance(exc, (ZCodeSchemaError, CodexSourceError, AutomationSchemaError, AutomationWriteError)):
        return _error("schema", exc)
    return _error(label, exc)


def _empty_report(*, skipped_locked: bool = False, errors: Sequence[str] = ()) -> RunReport:
    return RunReport(0, skipped_locked, list(errors))


def _load_config_and_state(
    config_path: Path, state_path: Path
) -> tuple[AppConfig, RuntimeState, StateStore] | RunReport:
    try:
        config = load_config(config_path)
    except Exception as exc:
        return _empty_report(errors=[_source_error("config", exc)])
    store = StateStore(state_path)
    try:
        state = store.load()
    except Exception as exc:
        return _empty_report(errors=[_source_error("state", exc)])
    return config, state, store


def _discover(config: AppConfig) -> tuple[Any, list[str]] | RunReport:
    try:
        # 只有 discover_paths 内部在 Codex 启用时才会解析 Codex 候选。
        return discover_paths(config, os.environ, Path.home()), []
    except Exception as exc:
        if not config.codex_enabled:
            return _empty_report(errors=[_source_error("discovery", exc)])
        # Codex 是可选来源：重新只发现 ZCode，避免可选来源故障放大为主源停摆。
        try:
            zcode_only = replace(config, codex_enabled=False)
            paths = discover_paths(zcode_only, os.environ, Path.home())
        except Exception as zcode_exc:
            return _empty_report(errors=[_source_error("discovery", zcode_exc)])
        return paths, [_source_error("codex", exc)]


def _prune_outbox(state: RuntimeState, config: AppConfig, now_ms: int) -> bool:
    cutoff = now_ms - config.outbox_retention_days * _DAY_MS
    removed = False
    for key, item in list(state.outbox.items()):
        if item.status != "submitted":
            continue
        submitted_at = item.submitted_at_ms
        # 旧状态缺少 submitted_at_ms 时安全保留，不能用完成时间误删。
        if isinstance(submitted_at, int) and submitted_at < cutoff:
            del state.outbox[key]
            removed = True
    return removed


def _item_due(item: OutboxItem, now_ms: int) -> bool:
    return item.status == "pending"


def _scan_sources(
    paths: Any,
    state: RuntimeState,
    config: AppConfig,
    baseline: bool | Mapping[str, bool],
) -> tuple[list[Event], bool, list[str], set[str]]:
    events: list[Event] = []
    dirty = False
    errors: list[str] = []
    refreshed_sources: set[str] = set()
    zcode_baseline = (
        baseline.get("zcode", False) if isinstance(baseline, Mapping) else baseline
    )

    try:
        zcode_events, zcode_offsets, zcode_turns = scan_zcode_events(
            paths.tasks_db,
            paths.zcode_rollout_dir,
            state,
            baseline=zcode_baseline,
        )
        state.zcode_rollout_offsets = zcode_offsets
        state.zcode_last_turns = zcode_turns
        events.extend(zcode_events)
        refreshed_sources.add("zcode")
        dirty = True
        if zcode_baseline:
            state.source_initialized["zcode"] = True
    except Exception as exc:
        if zcode_baseline:
            state.source_initialized["zcode"] = False
        errors.append(_source_error("zcode", exc))

    if config.codex_enabled:
        codex_baseline = (
            baseline.get("codex", False) if isinstance(baseline, Mapping) else baseline
        )
        try:
            if paths.codex_home is None:
                raise DiscoveryError("Codex 目录未发现")
            codex_events, rollout_offsets, turn_starts = scan_codex_events(
                paths.codex_home,
                paths.codex_state_db,
                paths.codex_history_db,
                state,
                baseline=codex_baseline,
            )
            state.rollout_offsets = rollout_offsets
            state.rollout_turn_started_ms = turn_starts
            events.extend(codex_events)
            refreshed_sources.add("codex")
            dirty = True
            if codex_baseline:
                state.source_initialized["codex"] = True
        except Exception as exc:
            if codex_baseline:
                state.source_initialized["codex"] = False
            errors.append(_source_error("codex", exc))
    return events, dirty, errors, refreshed_sources


def _ensure_source_initialized(state: RuntimeState) -> None:
    """为直接构造的旧内存状态补齐分源标志，避免升级后重复洪泛。"""
    if not state.source_initialized:
        state.source_initialized.update(
            {"zcode": state.initialized, "codex": state.initialized}
        )
    else:
        state.source_initialized.setdefault("zcode", False)
        state.source_initialized.setdefault("codex", False)


def _overall_initialized(config: AppConfig, state: RuntimeState) -> bool:
    required = ["zcode"]
    if config.codex_enabled:
        required.append("codex")
    return all(state.source_initialized.get(source, False) for source in required)


def _save_state(store: StateStore, state: RuntimeState, errors: list[str]) -> bool:
    try:
        store.save(state)
    except Exception as exc:
        errors.append(_source_error("state", exc))
        return False
    return True


def _load_target(paths: Any) -> tuple[Mapping[str, str] | None, list[str]]:
    try:
        return load_weixin_target(paths), []
    except Exception as exc:
        return None, [_source_error("notification", exc)]


def _submit_due_items(
    paths: Any,
    state: RuntimeState,
    config: AppConfig,
    store: StateStore,
    now_ms: int,
    errors: list[str],
    *,
    waiting_sources: set[str] | None = None,
) -> tuple[int, bool]:
    due = [
        (key, item) for key, item in state.outbox.items()
        if _item_due(item, now_ms)
        and (item.event.status not in {"awaiting_approval", "awaiting_input"}
             or waiting_sources is None or item.event.source in waiting_sources)
    ]
    if not due:
        return 0, True
    target, target_errors = _load_target(paths)
    errors.extend(target_errors)
    if target is None:
        return 0, True

    enqueued = 0
    for key, item in due:
        # 使用快照引用前再次确认仍为待处理项；前一项成功后保存失败也不
        # 影响本轮内存状态的顺序，稳定 ID 会在下轮恢复。
        current = state.outbox.get(key)
        if current is None or not _item_due(current, now_ms):
            continue
        try:
            identifier = enqueue_automation(
                paths.tasks_db,
                paths.notification_workspace,
                target,
                current.event,
                config.model,
                now_ms,
            )
        except Exception as exc:
            errors.append(_source_error("automation", exc))
            continue

        current.automation_id = identifier
        current.status = "submitted"
        current.next_attempt_at_ms = 0
        current.submitted_at_ms = now_ms
        state.seen_event_keys.add(current.event.key)
        enqueued += 1
        # 每个成功插入之后立即持久化；保存失败不会回滚已插入的自动化，
        # 但下一轮会以同一事件首发 ID 再次幂等查询。
        if not _save_state(store, state, errors):
            # 当前项的自动化已经存在，但新的 seen/状态未可靠落盘；
            # 停止后续外部写入，避免在状态不可持久化时继续扩大不一致。
            return enqueued, False
    return enqueued, True


def _discard_obsolete_waits(state: RuntimeState, refreshed_sources: set[str]) -> bool:
    """等待首发前重新核对来源；已恢复或被新停顿取代的不再投递。"""
    changed = False
    for key, item in list(state.outbox.items()):
        event = item.event
        if (item.status != "pending" or event.source not in refreshed_sources
                or event.status not in {"awaiting_approval", "awaiting_input"}):
            continue
        contexts = [context for context in state.turn_contexts.values()
                    if context.source == event.source and context.task_id == event.task_id]
        obsolete = any(
            (context.turn_id == event.turn_id and context.status != event.status)
            or context.updated_at_ms > event.completed_at_ms
            for context in contexts
            if context.updated_at_ms >= event.completed_at_ms
        )
        if obsolete:
            del state.outbox[key]
            state.seen_event_keys.add(key)
            changed = True
    return changed


def _execute_once(
    config_path: Path,
    state_path: Path,
    now_ms: int,
    *,
    baseline: bool,
) -> RunReport:
    lock = ProcessLock(_lock_path(state_path))
    if not lock.acquire():
        return _empty_report(skipped_locked=True)
    try:
        loaded = _load_config_and_state(config_path, state_path)
        if isinstance(loaded, RunReport):
            return loaded
        config, state, store = loaded
        _ensure_source_initialized(state)
        discovered = _discover(config)
        if isinstance(discovered, RunReport):
            return discovered
        paths, discovery_errors = discovered
        scan_config = config
        if config.codex_enabled and paths.codex_home is None:
            scan_config = replace(config, codex_enabled=False)

        # 每个来源独立决定是否静默基线；可用来源的恢复不能把另一来源
        # 的历史事件带入增量通知，且已经完成基线的 ZCode 仍可正常出新事件。
        source_baseline: dict[str, bool] = {
            "zcode": baseline or not state.source_initialized.get("zcode", False)
        }
        if config.codex_enabled and paths.codex_home is not None:
            source_baseline["codex"] = (
                baseline or not state.source_initialized.get("codex", False)
            )
        errors: list[str] = list(discovery_errors)
        needs_source_baseline = any(source_baseline.values())
        all_available_baseline = needs_source_baseline and all(
            source_baseline.get(source, False)
            for source in (
                ["zcode"]
                + (["codex"] if config.codex_enabled and paths.codex_home is not None else [])
            )
        )
        if all_available_baseline:
            _, source_dirty, source_errors, _ = _scan_sources(
                paths, state, scan_config, baseline=source_baseline
            )
            errors.extend(source_errors)
            state.initialized = _overall_initialized(config, state)
            if source_dirty or needs_source_baseline:
                _save_state(store, state, errors)
            return RunReport(0, False, errors)

        dirty = _prune_outbox(state, config, now_ms)

        # 已有 pending 项可以在扫描新来源前投递，避免来源读取延迟阻塞
        # 首发状态保存失败后的幂等恢复。
        if dirty:
            if not _save_state(store, state, errors):
                return RunReport(0, False, errors)
        # 完成/失败首发仍可先恢复；等待类必须先读取本轮恢复信号。
        enqueued, persisted = _submit_due_items(
            paths, state, config, store, now_ms, errors, waiting_sources=set()
        )
        if not persisted:
            return RunReport(enqueued, False, errors)

        events, source_dirty, source_errors, refreshed_sources = _scan_sources(
            paths,
            state,
            scan_config,
            baseline=source_baseline if needs_source_baseline else False,
        )
        errors.extend(source_errors)
        dirty = dirty or source_dirty
        state.initialized = _overall_initialized(config, state)
        for event in events:
            if event.key in state.seen_event_keys or event.key in state.outbox:
                continue
            state.outbox[event.key] = OutboxItem(
                event=event,
                status="pending",
            )
            dirty = True

        dirty = _discard_obsolete_waits(state, refreshed_sources) or dirty

        # outbox 与来源游标必须先保存，之后才允许外部写入。
        if dirty:
            if not _save_state(store, state, errors):
                # 新事件尚未可靠落盘时，不能先写入 ZCode
                # 自动化；否则外部写入成功后状态丢失会造成不可恢复的重复。
                return RunReport(enqueued, False, errors)

        submitted, persisted = _submit_due_items(
            paths, state, config, store, now_ms, errors, waiting_sources=refreshed_sources
        )
        enqueued += submitted
        if not persisted:
            return RunReport(enqueued, False, errors)
        return RunReport(enqueued, False, errors)
    except Exception as exc:
        # 锁释放仍在 finally 执行；对意外错误只返回脱敏摘要。
        return _empty_report(errors=[_source_error("service", exc)])
    finally:
        lock.release()


def run_once(
    config_path: Path,
    state_path: Path,
    now_ms: int | None = None,
) -> RunReport:
    """执行一轮扫描与投递；锁竞争安全地返回 skipped 报告。"""
    try:
        current_ms = _invalid_now(now_ms)
    except ValueError as exc:
        return _empty_report(errors=[_error("config", exc)])
    return _execute_once(Path(config_path), Path(state_path), current_ms, baseline=False)


def initialize_baseline(config_path: Path, state_path: Path) -> RunReport:
    """初始化静默基线，不补发安装前已经完成的事件。"""
    return _execute_once(Path(config_path), Path(state_path), _now_ms(), baseline=True)


def _default_doctor_paths() -> dict[str, str]:
    return {
        "config": "%LOCALAPPDATA%\\ZCodeTaskNotifier\\config.json",
        "state": "%LOCALAPPDATA%\\ZCodeTaskNotifier\\state.json",
        "zcode_home": "%ZCODE_HOME%",
        "codex_home": "%CODEX_HOME%",
    }


def _count_zcode(paths: Any) -> tuple[int, int, bool]:
    try:
        with connect_readonly(paths.tasks_db) as connection:
            row = connection.execute("SELECT COUNT(*) FROM tasks WHERE deleted = 0").fetchone()
            count = int(row[0]) if row is not None else 0
        try:
            workspace_count = sum(1 for child in paths.zcode_home.iterdir() if child.is_dir())
        except OSError:
            workspace_count = 0
        return count, workspace_count, True
    except Exception:
        return 0, 0, False


def _check_automations_schema(path: Path) -> bool:
    try:
        uri = Path(path).resolve(strict=False).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            _validate_automations_schema(connection)
        finally:
            connection.close()
        return True
    except Exception:
        return False


def _doctor_redacted(paths: Any | None) -> dict[str, str]:
    if paths is None:
        return _default_doctor_paths()
    result = _default_doctor_paths()
    # 所有值都折叠到已确认的语义根下；不放入 config_path 等外部原路径。
    result.update(
        {
            "zcode_tasks_db": redact_path(paths.tasks_db, paths),
            "zcode_logs": redact_path(paths.zcode_logs, paths),
            "notification_workspace": redact_path(paths.notification_workspace, paths),
        }
    )
    if paths.zcode_rollout_dir is not None:
        result["zcode_rollout"] = redact_path(paths.zcode_rollout_dir, paths)
    else:
        result["zcode_rollout"] = "%ZCODE_HOME%\\cli\\rollout"
    if paths.codex_home is not None:
        result["codex_home"] = redact_path(paths.codex_home, paths)
        if paths.codex_state_db is not None:
            result["codex_state_db"] = redact_path(paths.codex_state_db, paths)
        if paths.codex_history_db is not None:
            result["codex_history_db"] = redact_path(paths.codex_history_db, paths)
    return result


def doctor(config_path: Path, state_path: Path | None = None) -> DoctorReport:
    """执行只读健康检查，结果仅包含布尔值、计数和脱敏路径。"""
    checks: dict[str, bool] = {
        "config_valid": False,
        "state_valid": False,
        "zcode_discovered": False,
        "zcode_tasks_schema": False,
        "automations_schema": False,
        "weixin_target": False,
        "zcode_rollout": False,
        "codex_discovered": True,
        "codex_source": True,
    }
    counts = {
        "zcode_tasks": 0,
        "zcode_workspaces": 0,
        "codex_rollouts": 0,
        "outbox_pending": 0,
        "outbox_submitted": 0,
    }
    warnings: list[str] = []
    degraded = False
    paths: Any | None = None
    effective_state_path = (
        Path(state_path) if state_path is not None else Path(config_path).with_name("state.json")
    )
    try:
        state = StateStore(effective_state_path).load_strict()
        checks["state_valid"] = True
    except Exception:
        state = None
    if state is not None:
        for item in state.outbox.values():
            status = item.status
            if status in {"pending", "submitted"}:
                counts[f"outbox_{status}"] += 1
    try:
        config = load_config(config_path)
        checks["config_valid"] = True
    except Exception:
        config = None
    if config is not None:
        try:
            paths = discover_paths(config, os.environ, Path.home())
            checks["zcode_discovered"] = True
        except Exception:
            paths = None
            if config.codex_enabled:
                checks["codex_discovered"] = False
                checks["codex_source"] = False
    if paths is not None:
        zcode_count, workspace_count, zcode_ok = _count_zcode(paths)
        counts["zcode_tasks"] = zcode_count
        counts["zcode_workspaces"] = workspace_count
        checks["zcode_tasks_schema"] = zcode_ok
        checks["automations_schema"] = _check_automations_schema(paths.tasks_db)
        try:
            load_weixin_target(paths)
            checks["weixin_target"] = True
        except Exception:
            checks["weixin_target"] = False
        if paths.zcode_rollout_dir is not None:
            checks["zcode_rollout"] = True
        else:
            # 没有 rollout 时仍可进行终态兼容检查，但无法提供逐回合事件。
            checks["zcode_rollout"] = True
            degraded = True
            warnings.append("ZCode rollout 缺失：逐回合不可用，仅一次终态兼容")
        if config.codex_enabled:
            checks["codex_discovered"] = paths.codex_home is not None
            if paths.codex_home is not None:
                try:
                    from .codex_source import discover_rollouts

                    counts["codex_rollouts"] = len(
                        discover_rollouts(paths.codex_home, paths.codex_state_db)
                    )
                    checks["codex_source"] = True
                except Exception:
                    checks["codex_source"] = False
    healthy = all(checks.values())
    return DoctorReport(
        healthy,
        checks,
        counts,
        _doctor_redacted(paths),
        warnings=warnings,
        degraded=degraded,
    )


def backfill_codex(config_path: Path, state_path: Path, thread_id: str) -> str:
    """只补发指定 Codex thread 的最后一个完成事件，并保持稳定幂等 ID。"""
    target_thread = thread_id.strip() if isinstance(thread_id, str) else ""
    if not target_thread:
        raise ValueError("Codex thread ID 不能为空")
    lock = ProcessLock(_lock_path(Path(state_path)))
    if not lock.acquire():
        raise RuntimeError("通知器正在运行")
    try:
        config = load_config(config_path)
        if not config.codex_enabled:
            raise DiscoveryError("Codex 未启用")
        state_store = StateStore(state_path)
        state = state_store.load()
        paths = discover_paths(config, os.environ, Path.home())
        if paths.codex_home is None:
            raise DiscoveryError("Codex 目录未发现")
        try:
            event = backfill_codex_thread(
                paths.codex_home,
                paths.codex_state_db,
                paths.codex_history_db,
                target_thread,
            )
        except CodexSourceError as exc:
            # “指定 thread 不存在/没有完成事件”是运维目标发现失败，
            # 而状态库 schema 损坏仍须保留为 schema 错误（CLI 退出 3）。
            message = str(exc)
            if message.startswith(
                ("未找到指定 Codex thread", "指定 Codex thread 没有")
            ):
                raise DiscoveryError("指定 Codex thread 不可补发") from exc
            raise
        if event.key not in state.outbox:
            state.outbox[event.key] = OutboxItem(
                event=event,
                status="pending",
            )
        state_store.save(state)
        target = load_weixin_target(paths)
        item = state.outbox[event.key]
        if item.status != "submitted":
            identifier = enqueue_automation(
                paths.tasks_db,
                paths.notification_workspace,
                target,
                item.event,
                config.model,
                _now_ms(),
            )
            item.automation_id = identifier
            item.status = "submitted"
            item.submitted_at_ms = _now_ms()
            state.seen_event_keys.add(event.key)
            state_store.save(state)
        if item.automation_id is None:
            # 理论上只有损坏外部状态才能到这里，禁止返回不可重建 ID。
            raise RuntimeError("补发自动化 ID 缺失")
        return item.automation_id
    finally:
        lock.release()


__all__ = [
    "DoctorReport",
    "RunReport",
    "backfill_codex",
    "doctor",
    "initialize_baseline",
    "run_once",
]
