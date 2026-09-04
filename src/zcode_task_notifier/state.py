"""可靠状态的原子保存、坏文件隔离和单实例锁。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from .models import Event, OutboxItem, RuntimeState

try:
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows 开发环境的兼容路径
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 不提供 fcntl
    fcntl = None


class StateError(ValueError):
    """状态文件结构不符合当前 schema。"""


def _event_to_json(event: Event) -> dict[str, Any]:
    return asdict(event)


def _outbox_to_json(item: OutboxItem) -> dict[str, Any]:
    return {
        "event": _event_to_json(item.event),
        "automation_id": item.automation_id,
        "status": item.status,
        "submitted_at_ms": item.submitted_at_ms,
    }


def state_to_json(state: RuntimeState) -> dict[str, Any]:
    """将运行态转换为 JSON 可编码对象。"""
    return {
        "schema_version": state.schema_version,
        "initialized": state.initialized,
        "source_initialized": dict(state.source_initialized),
        "seen_event_keys": sorted(state.seen_event_keys),
        "zcode_rollout_offsets": state.zcode_rollout_offsets,
        "zcode_last_turns": state.zcode_last_turns,
        "rollout_offsets": state.rollout_offsets,
        "zcode_rollout_identities": state.zcode_rollout_identities,
        "rollout_identities": state.rollout_identities,
        "rollout_turn_started_ms": state.rollout_turn_started_ms,
        "outbox": {key: _outbox_to_json(item) for key, item in state.outbox.items()},
    }


def _event_from_json(payload: Any) -> Event:
    if not isinstance(payload, dict):
        raise StateError("outbox event 必须是对象")
    allowed = {
        "source",
        "key",
        "task_id",
        "title",
        "completed_at_ms",
        "duration_ms",
        "summary_text",
        "status",
        "turn_id",
    }
    values = {name: payload[name] for name in allowed if name in payload}
    required = {"source", "key", "task_id", "title", "completed_at_ms", "duration_ms", "summary_text"}
    if not required.issubset(values):
        raise StateError("outbox event 缺少字段")
    source = values["source"]
    if not isinstance(source, str) or source not in {"zcode", "codex"}:
        raise StateError("outbox event source 无效")
    if not isinstance(values["key"], str):
        raise StateError("outbox event key 必须是字符串")
    if not isinstance(values["task_id"], str):
        raise StateError("outbox event task_id 必须是字符串")
    if not isinstance(values["title"], str):
        raise StateError("outbox event title 必须是字符串")
    if not isinstance(values["summary_text"], str):
        raise StateError("outbox event summary_text 必须是字符串")
    status = values.get("status", "completed")
    if not isinstance(status, str) or status not in {"completed", "error"}:
        raise StateError("outbox event status 无效")
    if not isinstance(values["completed_at_ms"], int) or isinstance(values["completed_at_ms"], bool):
        raise StateError("outbox event completed_at_ms 必须是非负整数")
    if values["completed_at_ms"] < 0:
        raise StateError("outbox event completed_at_ms 必须是非负整数")
    duration_ms = values["duration_ms"]
    if duration_ms is not None and (
        not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0
    ):
        raise StateError("outbox event duration_ms 必须是非负整数或 null")
    turn_id = values.get("turn_id")
    if turn_id is not None and not isinstance(turn_id, str):
        raise StateError("outbox event turn_id 必须是字符串或 null")
    try:
        return Event(**values)
    except (TypeError, ValueError) as exc:
        raise StateError("outbox event 字段无效") from exc


def _outbox_from_json(payload: Any) -> OutboxItem:
    if not isinstance(payload, dict) or "event" not in payload:
        raise StateError("outbox 项无效")
    allowed = {
        "automation_id",
        "status",
        "submitted_at_ms",
    }
    values = {name: payload[name] for name in allowed if name in payload}
    automation_id = values.get("automation_id")
    if automation_id is not None and not isinstance(automation_id, str):
        raise StateError("outbox automation_id 必须是字符串或 null")
    status = values.get("status", "pending")
    if not isinstance(status, str) or status not in {
        "pending",
        "submitted",
        # 旧版失败补偿状态不再等待或重发；它们已完成首发尝试，
        # 升级后必须视为 submitted，绝不能重新进入 pending。
        "retry_wait",
        "exhausted",
    }:
        raise StateError("outbox status 无效")
    if status in {"retry_wait", "exhausted"}:
        status = "submitted"
    values["status"] = status
    submitted_at_ms = values.get("submitted_at_ms")
    if submitted_at_ms is not None and (
        not isinstance(submitted_at_ms, int)
        or isinstance(submitted_at_ms, bool)
        or submitted_at_ms < 0
    ):
        raise StateError("outbox submitted_at_ms 必须是非负整数或 null")
    try:
        # attempt/next_attempt_at_ms 是旧版兼容输入，故意不读取；新状态
        # 只保存首发 pending/submitted 与已提交时间。
        return OutboxItem(event=_event_from_json(payload["event"]), **values)
    except (TypeError, ValueError) as exc:
        raise StateError("outbox 项字段无效") from exc


def state_from_json(payload: Any) -> RuntimeState:
    """从 JSON 对象恢复声明的集合、映射和嵌套数据类。"""
    if not isinstance(payload, dict):
        raise StateError("状态根节点必须是对象")
    schema_version = payload.get("schema_version", 1)
    if type(schema_version) is not int or schema_version != 1:
        raise StateError("不支持的状态 schema_version")

    def _set(name: str) -> set[str]:
        value = payload.get(name, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise StateError(f"{name} 必须是字符串数组")
        return set(value)

    def _int_map(name: str) -> dict[str, int]:
        value = payload.get(name, {})
        if not isinstance(value, dict):
            raise StateError(f"{name} 必须是对象")
        result: dict[str, int] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, int) or isinstance(item, bool):
                raise StateError(f"{name} 的值必须是整数")
            result[key] = item
        return result

    def _str_map(name: str) -> dict[str, str]:
        value = payload.get(name, {})
        if not isinstance(value, dict):
            raise StateError(f"{name} 必须是对象")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise StateError(f"{name} 的值必须是字符串")
            result[key] = item
        return result

    raw_outbox = payload.get("outbox", {})
    if not isinstance(raw_outbox, dict):
        raise StateError("outbox 必须是对象")
    outbox = {key: _outbox_from_json(item) for key, item in raw_outbox.items() if isinstance(key, str)}
    if len(outbox) != len(raw_outbox):
        raise StateError("outbox 键必须是字符串")
    initialized = payload.get("initialized", False)
    if not isinstance(initialized, bool):
        raise StateError("initialized 必须是布尔值")
    raw_source_initialized = payload.get("source_initialized")
    if raw_source_initialized is None:
        # 老版本只有一个 overall initialized；为避免升级后把历史重新洪泛，
        # 将它安全迁移为两个来源都已完成（或都未完成）的兼容基线。
        source_initialized = {"zcode": initialized, "codex": initialized}
    else:
        if not isinstance(raw_source_initialized, dict):
            raise StateError("source_initialized 必须是对象")
        source_initialized = {}
        for key, value in raw_source_initialized.items():
            if key not in {"zcode", "codex"} or not isinstance(value, bool):
                raise StateError("source_initialized 的值必须是布尔值")
            source_initialized[key] = value
    return RuntimeState(
        schema_version=1,
        initialized=initialized,
        source_initialized=source_initialized,
        seen_event_keys=_set("seen_event_keys"),
        zcode_rollout_offsets=_int_map("zcode_rollout_offsets"),
        zcode_last_turns=_str_map("zcode_last_turns"),
        rollout_offsets=_int_map("rollout_offsets"),
        zcode_rollout_identities=_str_map("zcode_rollout_identities"),
        rollout_identities=_str_map("rollout_identities"),
        rollout_turn_started_ms=_int_map("rollout_turn_started_ms"),
        outbox=outbox,
    )


class StateStore:
    """以原子替换方式持久化运行态。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> RuntimeState:
        try:
            if not self.path.exists():
                return RuntimeState()
        except OSError as exc:
            raise StateError(f"无法检查状态文件: {self.path}") from exc
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                return state_from_json(json.load(stream))
        except OSError as exc:
            raise StateError(f"无法读取状态文件: {self.path}") from exc
        except (UnicodeError, json.JSONDecodeError, StateError, TypeError, ValueError) as exc:
            corrupt_path = self._corrupt_path()
            try:
                os.replace(self.path, corrupt_path)
            except OSError as quarantine_error:
                raise StateError(f"无法隔离损坏状态文件: {self.path}") from quarantine_error
            return RuntimeState()

    def load_strict(self) -> RuntimeState:
        """只读校验状态，不隔离或替换损坏文件，供 doctor 使用。"""
        try:
            if not self.path.exists():
                return RuntimeState()
        except OSError as exc:
            raise StateError("无法检查状态文件") from exc
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                return state_from_json(json.load(stream))
        except OSError as exc:
            raise StateError("无法读取状态文件") from exc
        except (UnicodeError, json.JSONDecodeError, StateError, TypeError, ValueError) as exc:
            raise StateError("状态文件结构无效") from exc

    def save(self, state: RuntimeState) -> None:
        if not isinstance(state, RuntimeState):
            raise StateError("state 必须是 RuntimeState")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(state_to_json(state), stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _corrupt_path(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        if not candidate.exists():
            return candidate
        index = 2
        while True:
            candidate = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}-{index}{self.path.suffix}")
            if not candidate.exists():
                return candidate
            index += 1


class ProcessLock:
    """使用 Windows 单字节锁保证同一通知器只有一个进程运行。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream: Any | None = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if msvcrt is not None:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:  # pragma: no cover - Linux/macOS 兼容路径
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - 极少数无锁平台
                stream.close()
                return False
        except (OSError, IOError):
            stream.close()
            return False
        self._stream = stream
        self._acquired = True
        return True

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        self._acquired = False
        if stream is None:
            return
        try:
            stream.seek(0)
            if msvcrt is not None:
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:  # pragma: no cover - Windows 不提供 fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"无法获取进程锁: {self.path}")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()
