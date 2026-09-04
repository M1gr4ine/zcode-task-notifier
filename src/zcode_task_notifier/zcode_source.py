"""读取 ZCode 全工作区完成回合的只读事件源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping

from .models import Event, RuntimeState


class ZCodeSchemaError(RuntimeError):
    """ZCode 任务索引缺少事件源所需的 schema。"""


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TaskColumns:
    session_id: str
    title: str
    status: str
    cron_automation_id: str
    deleted: str
    started_at: str | None
    completed_at: str | None
    searchable_text: str | None
    started_at_is_ms: bool
    completed_at_is_ms: bool


@dataclass(frozen=True)
class _ModelIO:
    turn_id: str
    completed_at_ms: int
    duration_ms: int | None
    summary_text: str
    sequence: int


def _mapping_value(mapping: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """兼容普通映射和 sqlite3.Row。"""
    try:
        return mapping[key]
    except (KeyError, IndexError):
        return default


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "session_id": ("session_id", "sessionId", "task_id", "taskId", "id"),
    "title": ("title", "name", "subject"),
    "status": ("task_status", "taskStatus", "status", "state"),
    "cron_automation_id": (
        "cron_automation_id",
        "cronAutomationId",
        "automation_id",
        "automationId",
    ),
    "deleted": ("deleted", "is_deleted", "isDeleted"),
    "started_at": (
        "started_at_ms",
        "startedAtMs",
        "started_at",
        "startedAt",
        "created_at_ms",
        "createdAtMs",
        "created_at",
        "createdAt",
    ),
    "completed_at": (
        "completed_at_ms",
        "completedAtMs",
        "completed_at",
        "completedAt",
        "updated_at_ms",
        "updatedAtMs",
        "updated_at",
        "updatedAt",
        "finished_at_ms",
        "finishedAtMs",
        "finished_at",
        "finishedAt",
    ),
    "searchable_text": ("searchable_text", "searchableText"),
}


def connect_readonly(path: Path) -> sqlite3.Connection:
    """以 SQLite ``mode=ro`` 打开数据库，绝不为缺失路径创建文件。"""
    database = Path(path).expanduser().resolve(strict=False)
    # ``as_uri`` 对 Windows 盘符、空格和非 ASCII 路径都能生成有效 URI。
    uri = database.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("PRAGMA table_info(tasks)").fetchall()
    except sqlite3.Error as exc:
        raise ZCodeSchemaError("无法读取 tasks schema") from exc
    result: dict[str, str] = {}
    for row in rows:
        name = row[1]
        if isinstance(name, str):
            result[name.casefold()] = name
    return result


def _pick_column(columns: Mapping[str, str], logical_name: str) -> str | None:
    for candidate in _COLUMN_ALIASES[logical_name]:
        actual = columns.get(candidate.casefold())
        if actual is not None:
            return actual
    return None


def _validate_schema(connection: sqlite3.Connection) -> _TaskColumns:
    columns = _table_columns(connection)
    required_names = ("session_id", "title", "status", "cron_automation_id", "deleted")
    selected: dict[str, str | None] = {
        name: _pick_column(columns, name)
        for name in _COLUMN_ALIASES
    }
    missing = [name for name in required_names if selected[name] is None]
    if missing:
        raise ZCodeSchemaError(f"tasks schema 缺少字段: {', '.join(missing)}")
    return _TaskColumns(
        session_id=selected["session_id"],  # type: ignore[arg-type]
        title=selected["title"],  # type: ignore[arg-type]
        status=selected["status"],  # type: ignore[arg-type]
        cron_automation_id=selected["cron_automation_id"],  # type: ignore[arg-type]
        deleted=selected["deleted"],  # type: ignore[arg-type]
        started_at=selected["started_at"],
        completed_at=selected["completed_at"],
        searchable_text=selected["searchable_text"],
        started_at_is_ms=_column_is_milliseconds(selected["started_at"]),
        completed_at_is_ms=_column_is_milliseconds(selected["completed_at"]),
    )


def _column_is_milliseconds(name: str | None) -> bool:
    if name is None:
        return False
    normalized = name.casefold()
    return normalized.endswith("_ms") or normalized.endswith("ms")


def _select_tasks(connection: sqlite3.Connection, columns: _TaskColumns) -> list[sqlite3.Row]:
    optional = {
        "started_at": columns.started_at,
        "completed_at": columns.completed_at,
        "searchable_text": columns.searchable_text,
    }
    expressions = [
        f"{_quote_identifier(columns.session_id)} AS __session_id",
        f"{_quote_identifier(columns.title)} AS __title",
        f"{_quote_identifier(columns.status)} AS __status",
        f"{_quote_identifier(columns.cron_automation_id)} AS __cron_automation_id",
        f"{_quote_identifier(columns.deleted)} AS __deleted",
    ]
    for logical_name, actual_name in optional.items():
        if actual_name is None:
            expressions.append(f"NULL AS __{logical_name}")
        else:
            expressions.append(
                f"{_quote_identifier(actual_name)} AS __{logical_name}"
            )
        if logical_name == "started_at":
            expressions.append(
                f"{1 if columns.started_at_is_ms else 0} AS __started_at_is_ms"
            )
        elif logical_name == "completed_at":
            expressions.append(
                f"{1 if columns.completed_at_is_ms else 0} AS __completed_at_is_ms"
            )
    query = (
        "SELECT "
        + ", ".join(expressions)
        + f" FROM {_quote_identifier('tasks')}"
        + f" WHERE {_quote_identifier(columns.deleted)} = 0"
    )
    try:
        return connection.execute(query).fetchall()
    except sqlite3.Error as exc:
        raise ZCodeSchemaError("无法读取 tasks 数据") from exc


def _event_key(task_id: str, turn_id: str) -> str:
    return f"zcode:{task_id}:{turn_id}"


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    """在不跟随链接的前提下识别 symlink 和 Windows 重解析点。"""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    # FILE_ATTRIBUTE_REPARSE_POINT；非 Windows 平台没有该字段时为 0。
    try:
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OSError("无法读取 ZCode 路径属性") from exc
    return bool(attributes & 0x400)


def _safe_model_io_path(rollout_dir: Path, session_id: str) -> Path | None:
    if not session_id or "\x00" in session_id:
        return None
    filename = f"model-io-{session_id}.jsonl"
    # 会话 ID 只应是文件名的一部分；拒绝分隔符，防止数据库内容构成越界路径。
    if Path(filename).name != filename:
        return None
    root = Path(rollout_dir).expanduser()
    try:
        # 发现阶段会返回已规范化的目录；直接调用扫描器时也拒绝把
        # rollout 根替换为链接，避免后续文件检查脱离受信任根。
        if _is_reparse_point(root):
            raise ZCodeSchemaError("ZCode model-io rollout 路径越界")
        root = root.resolve(strict=False)
        candidate = root / filename
        if _is_reparse_point(candidate):
            raise ZCodeSchemaError("ZCode model-io rollout 路径越界")
        candidate = candidate.resolve(strict=False)
    except ZCodeSchemaError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ZCodeSchemaError("ZCode model-io rollout 路径无法规范化") from exc
    if not _is_relative_to(candidate, root):
        raise ZCodeSchemaError("ZCode model-io rollout 路径越界")
    return candidate


def _file_identity(path: Path) -> str:
    """返回不读取文件内容的稳定身份指纹。"""
    try:
        metadata = path.stat()
    except (OSError, ValueError) as exc:
        raise ZCodeSchemaError("无法读取 ZCode model-io 文件") from exc
    try:
        device = int(getattr(metadata, "st_dev", 0) or 0)
        inode = int(getattr(metadata, "st_ino", 0) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ZCodeSchemaError("无法读取 ZCode model-io 文件身份") from exc
    if device or inode:
        return f"stat:{device}:{inode}"

    # Windows 某些文件系统可能把 st_dev/st_ino 暴露为 0；ctime 在该平台
    # 表示创建时间，追加内容不会改变它，可作为不含正文的安全回退。
    try:
        created = int(
            getattr(metadata, "st_birthtime_ns", 0)
            or getattr(metadata, "st_ctime_ns", 0)
            or 0
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ZCodeSchemaError("无法读取 ZCode model-io 文件身份") from exc
    if created:
        return f"created:{created}"

    # 极少数平台没有任何创建时间；元数据回退仍能检测典型的替换，且
    # 不把内容写入状态。若追加导致变化，安全地从头重扫而不会漏报。
    try:
        size = int(getattr(metadata, "st_size", 0) or 0)
        modified = int(getattr(metadata, "st_mtime_ns", 0) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ZCodeSchemaError("无法读取 ZCode model-io 文件身份") from exc
    return f"metadata:{size}:{modified}"


def _read_complete_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """读取从字节游标开始的完整行，截断尾行时不推进游标。"""
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError, OverflowError):
        offset = 0
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        with path.open("rb") as stream:
            stream.seek(offset)
            start = stream.tell()
            data = stream.read()
    except OSError as exc:
        raise ZCodeSchemaError("无法读取 ZCode model-io 文件") from exc
    except ValueError as exc:
        raise ZCodeSchemaError("无法定位 ZCode model-io 文件游标") from exc
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], start
    complete = data[: last_newline + 1]
    return complete.splitlines(), start + len(complete)


_MAX_TIMESTAMP_MS = 10**15
_MAX_DURATION_MS = 10**15


def _parse_timestamp_ms(value: Any, *, numeric_is_ms: bool = False) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        milliseconds = (
            number
            if numeric_is_ms or number >= 1_000_000_000_000
            else number * 1000
        )
        if not math.isfinite(milliseconds) or milliseconds > _MAX_TIMESTAMP_MS:
            return None
        try:
            return int(round(milliseconds))
        except (OverflowError, ValueError, TypeError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return _parse_timestamp_ms(float(text), numeric_is_ms=numeric_is_ms)
    except (ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        milliseconds = parsed.timestamp() * 1000
    except (OverflowError, OSError, ValueError):
        return None
    if not math.isfinite(milliseconds) or milliseconds < 0:
        return None
    if milliseconds > _MAX_TIMESTAMP_MS:
        return None
    try:
        return int(round(milliseconds))
    except (OverflowError, ValueError, TypeError):
        return None


def _non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _tail_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text[-6000:]


def _duration_ms(payload: Mapping[str, Any], task_row: Mapping[str, Any]) -> int | None:
    for key in ("durationMs", "duration_ms"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except (OverflowError, TypeError, ValueError):
                continue
            if not math.isfinite(number) or number < 0 or number > _MAX_DURATION_MS:
                continue
            try:
                return int(round(number))
            except (OverflowError, ValueError, TypeError):
                continue
    started = _parse_timestamp_ms(
        payload.get("startedAt", payload.get("started_at", payload.get("startAt")))
    )
    completed = _parse_timestamp_ms(payload.get("completedAt"))
    if started is not None and completed is not None and completed >= started:
        return completed - started
    row_started = _parse_timestamp_ms(
        _mapping_value(task_row, "__started_at"),
        numeric_is_ms=bool(_mapping_value(task_row, "__started_at_is_ms")),
    )
    row_completed = _parse_timestamp_ms(
        _mapping_value(task_row, "__completed_at"),
        numeric_is_ms=bool(_mapping_value(task_row, "__completed_at_is_ms")),
    )
    if row_started is not None and row_completed is not None and row_completed >= row_started:
        return row_completed - row_started
    return None


def _model_io_from_line(raw: bytes, sequence: int, task_row: Mapping[str, Any]) -> _ModelIO | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ZCodeSchemaError("ZCode model-io 记录不是有效 UTF-8") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ZCodeSchemaError("ZCode model-io 记录不是有效 JSON") from exc
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "model_io" or payload.get("querySource") != "main_turn":
        return None
    turn_id = _non_empty_text(payload.get("turnId"))
    completed_at_ms = _parse_timestamp_ms(payload.get("completedAt"))
    if turn_id is None or completed_at_ms is None:
        return None
    searchable = payload.get("searchable_text")
    if not isinstance(searchable, str):
        searchable = payload.get("searchableText")
    if not isinstance(searchable, str):
        searchable = _mapping_value(task_row, "__searchable_text")
    return _ModelIO(
        turn_id=turn_id,
        completed_at_ms=completed_at_ms,
        duration_ms=_duration_ms(payload, task_row),
        summary_text=_tail_text(searchable),
        sequence=sequence,
    )


def _legacy_key(session_id: str, task_row: Mapping[str, Any]) -> str:
    """无回合文件时按终态版本生成键，不把标题变化误报为新回合。"""
    raw_completed = _mapping_value(task_row, "__completed_at")
    completed_at_ms = _parse_timestamp_ms(
        raw_completed,
        numeric_is_ms=bool(_mapping_value(task_row, "__completed_at_is_ms")),
    )
    if completed_at_ms is None:
        # 时间缺失时保留旧版固定键，只能保证一次性兼容通知。
        return _legacy_terminal_key(session_id)
    raw_summary = _mapping_value(task_row, "__searchable_text")
    summary = raw_summary if isinstance(raw_summary, str) else ""
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:24]
    # 终态时间和摘要指纹共同构成内容版本；标题不参与，避免只改标题重复通知。
    return f"zcode:{session_id}:legacy:{completed_at_ms}:{digest}"


def _legacy_terminal_key(session_id: str) -> str:
    """返回旧版无回合文件实现使用的固定终态键。"""
    return f"zcode:{session_id}:legacy-terminal"


def _legacy_version_prefix(session_id: str) -> str:
    """返回带完成版本的兼容事件键前缀。"""
    return f"zcode:{session_id}:legacy:"


def _row_text(row: Mapping[str, Any], name: str, default: str = "") -> str:
    value = _mapping_value(row, name)
    if isinstance(value, str):
        return value.strip() or default
    if value is None:
        return default
    return str(value)


def _row_time(row: Mapping[str, Any], name: str) -> int:
    parsed = _parse_timestamp_ms(
        _mapping_value(row, name),
        numeric_is_ms=bool(_mapping_value(row, f"{name}_is_ms")),
    )
    return parsed if parsed is not None else 0


def _make_event(
    session_id: str,
    title: str,
    status: str,
    task_row: Mapping[str, Any],
    model_io: _ModelIO | None,
) -> Event:
    status_value = "error" if status.casefold() == "error" else "completed"
    row_summary = _tail_text(_mapping_value(task_row, "__searchable_text"))
    if model_io is None:
        completed_at_ms = _row_time(task_row, "__completed_at")
        summary_text = row_summary
        key = _legacy_key(session_id, task_row)
        turn_id = None
        duration_ms = _duration_ms({}, task_row)
    else:
        completed_at_ms = model_io.completed_at_ms
        summary_text = model_io.summary_text
        key = _event_key(session_id, model_io.turn_id)
        turn_id = model_io.turn_id
        duration_ms = model_io.duration_ms
    return Event(
        source="zcode",
        key=key,
        task_id=session_id,
        title=title,
        completed_at_ms=completed_at_ms,
        duration_ms=duration_ms,
        summary_text=summary_text,
        status=status_value,  # type: ignore[arg-type]
        turn_id=turn_id,
    )


def _scan_model_io(
    path: Path,
    offset: int,
    task_row: Mapping[str, Any],
) -> tuple[list[_ModelIO], int]:
    lines, new_offset = _read_complete_lines(path, offset)
    records: dict[str, _ModelIO] = {}
    for sequence, line in enumerate(lines):
        record = _model_io_from_line(line, sequence, task_row)
        if record is not None:
            records[record.turn_id] = record
    return sorted(records.values(), key=lambda record: record.sequence), new_offset


def scan_zcode_events(
    db_path: Path,
    rollout_dir: Path | None,
    state: RuntimeState,
    baseline: bool,
) -> tuple[list[Event], dict[str, int], dict[str, str]]:
    """扫描全部未删除任务，并按完成回合生成稳定、可去重的事件。"""
    events: list[Event] = []
    offsets = dict(state.zcode_rollout_offsets)
    identities = dict(state.zcode_rollout_identities)
    turns = dict(state.zcode_last_turns)
    diagnostics: list[ZCodeSchemaError] = []
    successful_file_reads = False

    with connect_readonly(Path(db_path)) as connection:
        columns = _validate_schema(connection)
        rows = _select_tasks(connection, columns)

        for row in rows:
            session_id = _row_text(row, "__session_id")
            if not session_id:
                continue
            cron_id = row["__cron_automation_id"]
            if cron_id is not None:
                continue

            status = _row_text(row, "__status").casefold()
            if status not in {"completed", "error"}:
                # 即使任务仍在运行，也必须消费完整 model-io 行并记录最新 turn，
                # 这样下一轮进入终态时不依赖观察到 running 的时机。
                terminal = False
            else:
                terminal = True

            title = _row_text(row, "__title", session_id)
            model_path: Path | None = None
            model_path_error = False
            if rollout_dir is not None:
                try:
                    model_path = _safe_model_io_path(Path(rollout_dir), session_id)
                except ZCodeSchemaError as exc:
                    model_path_error = True
                    diagnostics.append(exc)
                    _LOGGER.warning(
                        "zcode model-io 文件处理失败: %s", type(exc).__name__
                    )
            model_io_records: list[_ModelIO] = []
            if model_path is not None:
                key = _path_key(model_path)
                try:
                    file_exists = model_path.is_file()
                except OSError as exc:
                    diagnostics.append(
                        ZCodeSchemaError("无法检查 ZCode model-io 文件")
                    )
                    _LOGGER.warning(
                        "zcode model-io 文件处理失败: %s", type(exc).__name__
                    )
                    file_exists = False
                if file_exists:
                    try:
                        current_identity = _file_identity(model_path)
                        previous_identity = identities.get(key)
                        # 只有明确保存过身份且发现变化时才重置游标；旧状态
                        # 没有身份字段时继续沿用旧 path->offset 兼容行为。
                        old_offset = offsets.get(key, offsets.get(session_id, 0))
                        if previous_identity is not None and current_identity != previous_identity:
                            old_offset = 0
                            # running 任务不会推进完整行游标，但身份变化必须
                            # 把安全的重扫位置返回给下一轮终态扫描。
                            offsets[key] = 0
                        model_io_records, new_offset = _scan_model_io(
                            model_path, old_offset, row
                        )
                        successful_file_reads = True
                        identities[key] = current_identity
                        # 只有终态任务的事件已返回给调用方，或作为基线消费时，
                        # 才能提交本次完整行游标。running 期间保留旧游标，防止
                        # 尚未可通知的 turn 被吞掉。
                        if terminal:
                            offsets[key] = new_offset
                        if model_io_records:
                            turns[session_id] = model_io_records[-1].turn_id
                    except ZCodeSchemaError as exc:
                        diagnostics.append(exc)
                        _LOGGER.warning(
                            "zcode model-io 文件处理失败: %s", type(exc).__name__
                        )

            if not terminal:
                continue

            if model_path_error:
                continue

            # 若本轮没有新增 model-io，使用上轮已观察到的 turn；这覆盖了
            # “先看到 running，下一轮才看到 completed”的轮询间隔。
            if not model_io_records:
                previous_turn = _non_empty_text(turns.get(session_id))
                if previous_turn is not None:
                    model_io_records = [
                        _ModelIO(
                            turn_id=previous_turn,
                            completed_at_ms=_row_time(row, "__completed_at"),
                            duration_ms=_duration_ms({}, row),
                            summary_text=_tail_text(
                                _mapping_value(row, "__searchable_text")
                            ),
                            sequence=-1,
                        )
                    ]

            if not model_io_records:
                # 没有 rollout 文件时的旧版兼容路径只允许一次性终态通知；
                # 有文件但没有有效完整记录则等待下次补齐，避免误报。
                if model_path is not None and model_path.exists():
                    continue
                event = _make_event(session_id, title, status, row, None)
                if baseline:
                    state.seen_event_keys.add(event.key)
                elif event.key not in state.seen_event_keys:
                    # 旧版本只记录固定的任务级终态键。升级后不能把同一
                    # 完成版本重新入队，但要登记新版本键；之后时间或摘要
                    # 变化会自然生成新的兼容键并继续通知。
                    has_registered_version = any(
                        key.startswith(_legacy_version_prefix(session_id))
                        for key in state.seen_event_keys
                    )
                    if (
                        _legacy_terminal_key(session_id) in state.seen_event_keys
                        and not has_registered_version
                    ):
                        state.seen_event_keys.add(event.key)
                    else:
                        events.append(event)
                continue

            for model_io in model_io_records:
                event = _make_event(session_id, title, status, row, model_io)
                if baseline:
                    # 基线扫描不投递，但要把当前终态视作已观察，防止下一轮
                    # 因为只有 last_turn 游标而重新生成历史事件。
                    state.seen_event_keys.add(event.key)
                elif event.key not in state.seen_event_keys:
                    events.append(event)

    state.zcode_rollout_identities = identities
    if diagnostics and not (successful_file_reads or events):
        raise diagnostics[0]
    return events, offsets, turns
