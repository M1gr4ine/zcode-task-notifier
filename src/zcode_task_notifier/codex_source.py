"""读取 Codex rollout 完成事件，并兼容补充旧版历史库。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from .models import Event, RuntimeState, TurnContext
from .stop_policy import StopDecision, classify_stop, user_task_evidence


class CodexSourceError(RuntimeError):
    """Codex 数据源不能被安全、唯一地确认。"""


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RolloutRef:
    """经状态库确认的一个 Codex rollout 文件。"""

    thread_id: str
    title: str
    path: Path


@dataclass(frozen=True)
class _ThreadRow:
    thread_id: str
    title: str
    source: str | None
    path: Path


@dataclass(frozen=True)
class _CodexMetadata:
    refs: tuple[RolloutRef, ...]
    titles: dict[str, str]
    sources: dict[str, str | None]


@dataclass(frozen=True)
class _HistoryRow:
    thread_id: str
    turn_id: str | None
    status: str
    completed_at_ms: int
    final_message: str
    title: str
    source: str | None
    started_at_ms: int | None
    identity: str


def _context_key(thread_id: str, turn_id: str) -> str:
    return f"codex:{thread_id}:{turn_id}"


def _structured_text(value: Any) -> str:
    """只提取记录中的文本供本回合判断，不把正文写入 RuntimeState。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(part for part in (_structured_text(item) for item in value) if part)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in (
            "text",
            "content",
            "input_text",
            "inputText",
            "user_input",
            "userInput",
            "message",
        ):
            if key in value:
                part = _structured_text(value[key])
                if part:
                    parts.append(part)
        return "\n".join(parts)
    return ""


def _input_fingerprint(text: str) -> str | None:
    normalized = " ".join(text.replace("\r", "\n").split())
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _record_user_evidence(
    ref: RolloutRef,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    contexts: dict[str, TurnContext],
    fallback_turn_id: str | None = None,
) -> tuple[str | None, bool]:
    """按记录角色提取真实用户任务证据，忽略 system/developer 环境内容。"""
    explicit_thread = _event_thread_id(record, payload)
    if explicit_thread is not None and explicit_thread != ref.thread_id:
        return None, False
    record_type = _text(record.get("type"))
    payload_type = _text(payload.get("type"))
    role = _text(payload.get("role")) or _text(record.get("role"))
    is_user = role is not None and role.casefold() == "user"
    if payload_type in {"user_message", "user_input", "turn_context"}:
        is_user = True
    if record_type in {"user_message", "user_input"} or (
        record_type == "turn_context"
        and any(key in payload for key in ("user_input", "userInput", "input"))
    ):
        is_user = True
    if not is_user:
        return None, False
    turn_id = _event_turn_id(record, payload) or fallback_turn_id
    if turn_id is None:
        return None, False
    text = _structured_text(payload)
    if not text:
        text = _structured_text(record)
    fingerprint = _input_fingerprint(text)
    task_evidence = user_task_evidence(text)
    current = contexts.get(_context_key(ref.thread_id, turn_id))
    if fingerprint is None:
        return turn_id, bool(current and current.has_user_task)
    key = _context_key(ref.thread_id, turn_id)
    # 同一回合可能先出现 Harness 封装、随后才出现真实用户请求；真实证据
    # 一旦观察到不可被后续规则封装降级。
    has_user_task = bool(task_evidence or (current and current.has_user_task is True))
    contexts[key] = TurnContext(
        source="codex",
        task_id=ref.thread_id,
        turn_id=turn_id,
        has_user_task=has_user_task,
        input_fingerprint=fingerprint,
        plan_fingerprint=current.plan_fingerprint if current else None,
        pending_input_call_id=current.pending_input_call_id if current else None,
        status=current.status if current else None,
        active=True,
        updated_at_ms=current.updated_at_ms if current else 0,
    )
    return turn_id, True


def _context_task_flag(
    ref: RolloutRef, turn_id: str | None, contexts: Mapping[str, TurnContext]
) -> bool | None:
    if not turn_id:
        return None
    context = contexts.get(_context_key(ref.thread_id, turn_id))
    return None if context is None else context.has_user_task


def _remember_decision(
    ref: RolloutRef,
    turn_id: str | None,
    contexts: dict[str, TurnContext],
    decision: StopDecision,
    *,
    active: bool,
    timestamp_ms: int,
    pending_input_call_id: str | None = None,
) -> None:
    if not turn_id:
        return
    key = _context_key(ref.thread_id, turn_id)
    current = contexts.get(key)
    contexts[key] = TurnContext(
        source="codex",
        task_id=ref.thread_id,
        turn_id=turn_id,
        has_user_task=current.has_user_task if current else None,
        input_fingerprint=current.input_fingerprint if current else None,
        plan_fingerprint=(
            decision.plan_fingerprint
            if decision.status == "awaiting_approval"
            else None
        ),
        pending_input_call_id=pending_input_call_id,
        status=decision.status,
        active=active,
        updated_at_ms=timestamp_ms,
    )


_MAX_TIMESTAMP_MS = 10**15
_MAX_SUMMARY_CHARS = 6000

_THREAD_ID_ALIASES = (
    "thread_id",
    "threadId",
    "conversation_id",
    "conversationId",
    "thread",
    "thread_uuid",
    "threadUUID",
    "session_id",
    "sessionId",
    "task_id",
    "taskId",
)
_PATH_ALIASES = (
    "rollout_path",
    "rolloutPath",
    "rollout_file",
    "rolloutFile",
    "rollout",
    "jsonl_path",
    "jsonlPath",
    "file_path",
    "filePath",
    "session_path",
    "sessionPath",
    "path",
)
_TITLE_ALIASES = (
    "title",
    "display_title",
    "displayTitle",
    "name",
    "subject",
)
_SOURCE_ALIASES = ("thread_source", "threadSource", "source")
_TURN_ALIASES = ("turn_id", "turnId", "turn", "turnID")
_STATUS_ALIASES = (
    "status",
    "state",
    "task_status",
    "turn_status",
    "completion_status",
)
_COMPLETED_ALIASES = (
    "completed_at_ms",
    "completedAtMs",
    "completed_at",
    "completedAt",
    "finished_at_ms",
    "finishedAtMs",
    "finished_at",
    "finishedAt",
    "completed_timestamp",
    "completedTimestamp",
)
_STARTED_ALIASES = (
    "started_at_ms",
    "startedAtMs",
    "started_at",
    "startedAt",
    "created_at_ms",
    "createdAtMs",
    "created_at",
    "createdAt",
)
_FINAL_ALIASES = (
    "last_agent_message",
    "lastAgentMessage",
    "final_message",
    "finalMessage",
    "final_answer",
    "finalAnswer",
    "final_message_ref",
    "finalMessageRef",
    "final_answer_ref",
    "finalAnswerRef",
    "message_ref",
    "messageRef",
    "message_id",
    "messageId",
    "final_message_id",
    "finalMessageId",
    "final_answer_id",
    "finalAnswerId",
    "answer_id",
    "answerId",
    "last_message",
    "lastMessage",
    "answer",
    "output",
    "output_ref",
    "outputRef",
)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connect_readonly(path: Path) -> sqlite3.Connection:
    database = Path(path).expanduser().resolve(strict=False)
    try:
        uri = database.as_uri() + "?mode=ro"
    except (ValueError, OSError) as exc:
        raise CodexSourceError("Codex 数据库路径无法规范化") from exc
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise CodexSourceError(f"无法只读打开 Codex 数据库: {database.name}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        connection.close()
        raise CodexSourceError("无法启用 Codex 数据库只读模式") from exc
    return connection


def _table_infos(connection: sqlite3.Connection) -> list[tuple[str, dict[str, str], list[str]]]:
    try:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise CodexSourceError("无法读取 Codex 数据库表目录") from exc

    result: list[tuple[str, dict[str, str], list[str]]] = []
    for raw_name in names:
        table_name = raw_name[0]
        if not isinstance(table_name, str):
            continue
        try:
            rows = connection.execute(
                f"PRAGMA table_info({_quote_identifier(table_name)})"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CodexSourceError("无法读取 Codex 数据库 schema") from exc
        columns: dict[str, str] = {}
        primary_keys: list[str] = []
        for row in rows:
            name = row[1]
            if not isinstance(name, str):
                continue
            columns[name.casefold()] = name
            try:
                if int(row[5]) > 0:
                    primary_keys.append(name)
            except (TypeError, ValueError):
                pass
        result.append((table_name, columns, primary_keys))
    return result


def _pick_column(columns: Mapping[str, str], aliases: Iterable[str]) -> str | None:
    for alias in aliases:
        actual = columns.get(alias.casefold())
        if actual is not None:
            return actual
    return None


def _thread_column(table_name: str, columns: Mapping[str, str]) -> str | None:
    """选择明确的 thread 列；兼容线程表将主键命名为 id 的版本。"""
    explicit = _pick_column(columns, _THREAD_ID_ALIASES)
    if explicit is not None:
        return explicit
    table = table_name.casefold()
    if any(
        token in table
        for token in ("thread", "session", "rollout", "conversation", "task", "history", "turn", "state")
    ):
        return columns.get("id")
    return None


def _row_value(row: Mapping[str, Any], column: str | None) -> Any:
    if column is None:
        return None
    try:
        return row[column]
    except (KeyError, IndexError):
        return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if value is None or isinstance(value, bool):
        return None
    return str(value).strip() or None


def _path_values(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # 某些状态版本会在一个 JSON 字段中保存多个 rollout 路径。
        if text[:1] in "[{":
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if decoded is not None and not isinstance(decoded, str):
                return _path_values(decoded)
        return [text]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_path_values(item))
        return result
    if isinstance(value, dict):
        result = []
        for key in ("path", "rollout_path", "rolloutPath", "file", "file_path", "filePath"):
            if key in value:
                result.extend(_path_values(value[key]))
        return result
    return []


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_rollout_path(codex_home: Path, raw_path: str) -> Path:
    root = Path(codex_home).expanduser().resolve(strict=False)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodexSourceError("rollout 路径无法规范化") from exc
    if not _is_relative_to(resolved, root):
        raise CodexSourceError("rollout 路径越出已确认的 Codex 目录")
    return resolved


def _source_is_user(source: str | None) -> bool:
    # 老版本没有 thread_source；在没有该字段时只能按历史兼容约定接受。
    return source is None or source.casefold() == "user"


def _normalise_title(title: str | None, thread_id: str) -> str:
    value = (title or "").strip()
    prefix = "[codex]"
    while value.casefold().startswith(prefix):
        value = value[len(prefix) :].lstrip()
    value = value or f"thread-{thread_id[:8]}"
    return "[codex] " + value


def _catalog_titles(
    connection: sqlite3.Connection,
    table_infos: list[tuple[str, dict[str, str], list[str]]],
) -> dict[str, str]:
    titles: dict[str, str] = {}
    for table_name, columns, _ in table_infos:
        if table_name.casefold() != "local_thread_catalog":
            continue
        thread_column = _pick_column(columns, _THREAD_ID_ALIASES) or columns.get("id")
        title_column = _pick_column(columns, ("display_title", "displayTitle", "title"))
        if thread_column is None or title_column is None:
            continue
        try:
            rows = connection.execute(
                f"SELECT {_quote_identifier(thread_column)}, {_quote_identifier(title_column)} "
                f"FROM {_quote_identifier(table_name)}"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CodexSourceError("无法读取 Codex thread catalog") from exc
        for row in rows:
            thread_id = _text(row[0])
            title = _text(row[1])
            if thread_id and title and thread_id not in titles:
                titles[thread_id] = title
    return titles


def _load_state_metadata(codex_home: Path, state_db: Path | None) -> _CodexMetadata:
    root = Path(codex_home).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise CodexSourceError("Codex 根目录不存在")
    if state_db is None:
        refs = _discover_session_files(root)
        return _CodexMetadata(tuple(refs), {}, {})

    connection = _connect_readonly(Path(state_db))
    try:
        table_infos = _table_infos(connection)
        catalog_titles = _catalog_titles(connection, table_infos)
        rows: list[_ThreadRow] = []
        titles: dict[str, str] = {}
        sources: dict[str, str | None] = {}
        source_observations: dict[str, list[str | None]] = {}
        matched_state_schema = False
        for table_name, columns, _ in table_infos:
            thread_column = _thread_column(table_name, columns)
            path_column = _pick_column(columns, _PATH_ALIASES)
            if thread_column is None:
                continue
            title_column = _pick_column(columns, _TITLE_ALIASES)
            source_column = _pick_column(columns, _SOURCE_ALIASES)
            try:
                table_rows = connection.execute(
                    f"SELECT * FROM {_quote_identifier(table_name)}"
                ).fetchall()
            except sqlite3.Error as exc:
                raise CodexSourceError("无法读取 Codex thread 状态") from exc
            if path_column is None:
                for row in table_rows:
                    thread_id = _text(_row_value(row, thread_column))
                    if not thread_id:
                        continue
                    title = _text(_row_value(row, title_column))
                    source = _text(_row_value(row, source_column))
                    if title and thread_id not in titles:
                        titles[thread_id] = title
                    source_observations.setdefault(thread_id, []).append(source)
                continue
            matched_state_schema = True
            for row in table_rows:
                thread_id = _text(_row_value(row, thread_column))
                if not thread_id:
                    continue
                title = _text(_row_value(row, title_column))
                row_source = _text(_row_value(row, source_column))
                source_observations.setdefault(thread_id, []).append(row_source)
                if title and thread_id not in titles:
                    titles[thread_id] = title
                raw_paths = _path_values(_row_value(row, path_column))
                for raw_path in raw_paths:
                    # 先保留原始候选，待所有来源信息汇总后再做路径规范化。
                    # 非 user 行即使越界，也必须静默跳过而不能触发 confinement 错误。
                    rows.append(_ThreadRow(thread_id, title or "", row_source, Path(raw_path)))

        for thread_id, observed_sources in source_observations.items():
            explicit_sources = [source for source in observed_sources if source is not None]
            non_user = next(
                (source for source in explicit_sources if source.casefold() != "user"),
                None,
            )
            if non_user is not None:
                sources[thread_id] = non_user
            elif explicit_sources:
                sources[thread_id] = "user"
            else:
                sources[thread_id] = None

        refs_by_key: dict[tuple[str, str], RolloutRef] = {}
        unsafe_paths: list[CodexSourceError] = []
        unsafe_thread_ids: set[str] = set()
        for row in rows:
            if not _source_is_user(sources.get(row.thread_id)):
                continue
            try:
                path = _safe_rollout_path(root, str(row.path))
            except CodexSourceError as exc:
                # 状态库可能保留已迁移安装或旧版本的绝对路径。绝不读取越界
                # 文件；有受限根内来源时仅跳过坏行，全部越界时仍失败关闭。
                unsafe_paths.append(exc)
                unsafe_thread_ids.add(row.thread_id)
                continue
            title = row.title or titles.get(row.thread_id, "") or catalog_titles.get(row.thread_id, "")
            ref = RolloutRef(row.thread_id, _normalise_title(title, row.thread_id), path)
            key = (row.thread_id, str(path))
            previous = refs_by_key.get(key)
            if previous is None or previous.title == _normalise_title("", row.thread_id):
                refs_by_key[key] = ref

        if unsafe_thread_ids:
            fallback_titles = dict(catalog_titles)
            fallback_titles.update(titles)
            for ref in _discover_session_files(
                root,
                fallback_titles,
                sources,
                thread_ids=unsafe_thread_ids,
            ):
                refs_by_key.setdefault((ref.thread_id, str(ref.path)), ref)

        if unsafe_paths and not refs_by_key:
            raise unsafe_paths[0]
        if unsafe_paths:
            _LOGGER.warning("codex state 越界路径已跳过: count=%d", len(unsafe_paths))

        if not matched_state_schema:
            raise CodexSourceError("Codex 状态库 schema 缺少 thread 与 rollout 路径字段")

        refs = list(refs_by_key.values())
        refs.sort(key=lambda item: (item.thread_id, str(item.path)))
        for thread_id, title in catalog_titles.items():
            titles.setdefault(thread_id, title)
        return _CodexMetadata(tuple(refs), titles, sources)
    finally:
        connection.close()


def _session_thread_id(path: Path) -> str | None:
    lines, _ = _read_complete_lines(path, 0)
    thread_id: str | None = None
    for raw in lines[:64]:
        try:
            record = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CodexSourceError("Codex session 记录不是有效 UTF-8") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise CodexSourceError("Codex session 记录不是有效 JSON") from exc
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = record
        for key in ("thread_id", "threadId", "session_id", "sessionId", "id"):
            value = _text(payload.get(key))
            if value:
                thread_id = thread_id or value
                break
    if thread_id:
        return thread_id
    stem = path.stem
    for prefix in ("rollout-", "session-"):
        if stem.startswith(prefix):
            stem = stem.removeprefix(prefix)
            break
    return stem or None


def _discover_session_files(
    codex_home: Path,
    catalog_titles: Mapping[str, str] | None = None,
    sources: Mapping[str, str | None] | None = None,
    thread_ids: set[str] | None = None,
) -> list[RolloutRef]:
    refs: dict[tuple[str, str], RolloutRef] = {}
    diagnostics: list[CodexSourceError] = []
    candidates: list[Path] = []
    selected_ids = {
        value.casefold() for value in (thread_ids or set()) if value.strip()
    }
    for directory_name in ("sessions", "workspaces"):
        directory = codex_home / directory_name
        if not directory.is_dir():
            continue
        try:
            candidates.extend(
                item
                for item in directory.rglob("*.jsonl")
                if item.is_file()
                and (
                    not selected_ids
                    or any(value in item.name.casefold() for value in selected_ids)
                )
            )
        except OSError as exc:
            raise CodexSourceError(f"无法遍历 Codex {directory_name} 目录") from exc
    candidates.sort()
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CodexSourceError("rollout 路径无法规范化") from exc
        if not _is_relative_to(path, codex_home):
            raise CodexSourceError("rollout 路径越出已确认的 Codex 目录")
        try:
            thread_id = _session_thread_id(path)
        except CodexSourceError as exc:
            diagnostics.append(exc)
            _LOGGER.warning("codex session 文件处理失败: %s", type(exc).__name__)
            continue
        if not thread_id:
            continue
        if selected_ids and thread_id.casefold() not in selected_ids:
            continue
        source = sources.get(thread_id) if sources is not None else None
        if not _source_is_user(source):
            continue
        title = catalog_titles.get(thread_id, "") if catalog_titles is not None else ""
        refs[(thread_id, str(path))] = RolloutRef(
            thread_id, _normalise_title(title, thread_id), path
        )
    if diagnostics and not refs:
        raise diagnostics[0]
    return sorted(refs.values(), key=lambda item: (item.thread_id, str(item.path)))


def discover_rollouts(codex_home: Path, state_db: Path | None) -> list[RolloutRef]:
    """返回状态库确认的全部用户 rollout；路径越界时失败关闭。"""
    return list(_load_state_metadata(Path(codex_home), state_db).refs)


def _read_complete_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """只返回完整换行记录，截断尾行保留原游标。"""
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
        raise CodexSourceError("无法读取 Codex rollout 文件") from exc
    except ValueError as exc:
        raise CodexSourceError("无法定位 Codex rollout 文件游标") from exc
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], start
    complete = data[: last_newline + 1]
    return complete.splitlines(), start + len(complete)


def _file_identity(path: Path) -> str:
    """返回不读取 rollout 正文的稳定文件身份指纹。"""
    try:
        metadata = path.stat()
    except (OSError, ValueError) as exc:
        raise CodexSourceError("无法读取 Codex rollout 文件") from exc
    try:
        device = int(getattr(metadata, "st_dev", 0) or 0)
        inode = int(getattr(metadata, "st_ino", 0) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodexSourceError("无法读取 Codex rollout 文件身份") from exc
    if device or inode:
        return f"stat:{device}:{inode}"

    # Windows 某些文件系统把 st_dev/st_ino 暴露为 0；ctime 表示创建时间，
    # 追加内容不会改变它，可作为不含正文的安全回退。
    try:
        created = int(
            getattr(metadata, "st_birthtime_ns", 0)
            or getattr(metadata, "st_ctime_ns", 0)
            or 0
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodexSourceError("无法读取 Codex rollout 文件身份") from exc
    if created:
        return f"created:{created}"

    # 极少数平台没有任何创建时间；元数据回退仍能检测典型替换，且不保存
    # rollout 正文。追加导致变化时从头重扫是安全的，最多产生已去重事件。
    try:
        size = int(getattr(metadata, "st_size", 0) or 0)
        modified = int(getattr(metadata, "st_mtime_ns", 0) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodexSourceError("无法读取 Codex rollout 文件身份") from exc
    return f"metadata:{size}:{modified}"


def _column_is_milliseconds(name: str | None) -> bool:
    if name is None:
        return False
    normalized = name.casefold()
    return normalized.endswith("_ms") or normalized.endswith("ms")


def _timestamp_ms(value: Any, *, numeric_is_ms: bool = False) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if number < 0 or number != number or number in (float("inf"), float("-inf")):
            return None
        milliseconds = (
            number
            if numeric_is_ms or number >= 1_000_000_000_000
            else number * 1000
        )
        if milliseconds > _MAX_TIMESTAMP_MS:
            return None
        return int(round(milliseconds))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        try:
            return _timestamp_ms(float(text), numeric_is_ms=numeric_is_ms)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        milliseconds = parsed.timestamp() * 1000
    except (OSError, OverflowError, ValueError):
        return None
    if milliseconds < 0 or milliseconds > _MAX_TIMESTAMP_MS:
        return None
    return int(round(milliseconds))


def _iso_timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        milliseconds = parsed.timestamp() * 1000
    except (OSError, OverflowError, ValueError):
        return None
    if milliseconds < 0 or milliseconds > _MAX_TIMESTAMP_MS:
        return None
    return int(round(milliseconds))


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _tail_text(value: str) -> str:
    return value[-_MAX_SUMMARY_CHARS:]


def _normalise_message(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _completion_fingerprint(
    thread_id: str,
    completed_at_ms: int,
    normalized_message: str,
) -> str:
    identity = f"{thread_id}\0{completed_at_ms}\0{normalized_message}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _event_thread_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    for source in (payload, record):
        for key in _THREAD_ID_ALIASES:
            value = _text(source.get(key))
            if value:
                return value
    return None


def _event_turn_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    for source in (payload, record):
        for key in _TURN_ALIASES:
            value = _text(source.get(key))
            if value:
                return value
    return None


def _request_input_call_id(payload: Mapping[str, Any]) -> str | None:
    """仅接受 Codex 已观察到的 request_user_input 工具名称。"""
    if _text(payload.get("type")) != "function_call":
        return None
    name = _text(payload.get("name"))
    if name not in {"request_user_input", "functions.request_user_input"}:
        return None
    return _text(payload.get("call_id"))


def _function_call_output_id(payload: Mapping[str, Any]) -> str | None:
    if _text(payload.get("type")) != "function_call_output":
        return None
    return _text(payload.get("call_id"))


def _clear_pending_input(
    ref: RolloutRef,
    turn_id: str | None,
    contexts: dict[str, TurnContext],
    timestamp_ms: int,
) -> None:
    """用户已回答工具提问后，将回合从 waiting 恢复为执行中。"""
    if not turn_id:
        return
    key = _context_key(ref.thread_id, turn_id)
    current = contexts.get(key)
    if current is None:
        return
    contexts[key] = TurnContext(
        source="codex",
        task_id=ref.thread_id,
        turn_id=turn_id,
        has_user_task=current.has_user_task,
        input_fingerprint=current.input_fingerprint,
        plan_fingerprint=current.plan_fingerprint,
        pending_input_call_id=None,
        status="running",
        active=True,
        updated_at_ms=timestamp_ms,
    )


def _pending_input_turn(
    ref: RolloutRef,
    call_id: str,
    contexts: Mapping[str, TurnContext],
) -> str | None:
    for context in contexts.values():
        if (
            context.source == "codex"
            and context.task_id == ref.thread_id
            and context.pending_input_call_id == call_id
        ):
            return context.turn_id
    return None


def _stable_missing_turn(
    thread_id: str,
    completed_at_ms: int,
    normalized_message: str,
) -> str:
    return f"turn-{_completion_fingerprint(thread_id, completed_at_ms, normalized_message)[:24]}"


def _record_event(
    ref: RolloutRef,
    record: Mapping[str, Any],
    starts: dict[str, int],
    contexts: dict[str, TurnContext],
    fallback_turn_id: str | None = None,
) -> Event | None:
    if record.get("type") not in {
        "event_msg",
        "task_waiting",
        "task_complete",
        "response_item",
    }:
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    _record_user_evidence(ref, record, payload, contexts, fallback_turn_id)
    event_type = _text(payload.get("type"))
    request_input_call_id = _request_input_call_id(payload)
    if request_input_call_id is not None:
        event_type = "request_user_input"
    explicit_thread = _event_thread_id(record, payload)
    if explicit_thread is not None and explicit_thread != ref.thread_id:
        return None
    turn_id = _event_turn_id(record, payload) or fallback_turn_id
    if event_type == "task_started":
        started_at_ms = _iso_timestamp_ms(record.get("timestamp"))
        if turn_id and started_at_ms is not None:
            starts.setdefault(f"{ref.thread_id}:{turn_id}", started_at_ms)
        if turn_id:
            current = contexts.get(_context_key(ref.thread_id, turn_id))
            contexts[_context_key(ref.thread_id, turn_id)] = TurnContext(
                source="codex",
                task_id=ref.thread_id,
                turn_id=turn_id,
                has_user_task=current.has_user_task if current else None,
                input_fingerprint=current.input_fingerprint if current else None,
                plan_fingerprint=current.plan_fingerprint if current else None,
                pending_input_call_id=None,
                status="running",
                active=True,
                updated_at_ms=started_at_ms or (current.updated_at_ms if current else 0),
            )
        return None
    if event_type not in {
        "task_complete",
        "task_waiting",
        "awaiting_input",
        "awaiting_approval",
        "task_paused",
        "request_user_input",
    }:
        return None
    completed_at_ms = _iso_timestamp_ms(record.get("timestamp"))
    if completed_at_ms is None:
        return None
    message_value = payload.get("last_agent_message", payload.get("lastAgentMessage", ""))
    if event_type == "request_user_input" and not message_value:
        message_value = payload.get("arguments", payload.get("question", ""))
    message = _message_text(message_value)
    normalized_message = _normalise_message(message)
    if turn_id is None:
        turn_id = _stable_missing_turn(ref.thread_id, completed_at_ms, normalized_message)
    structured_status = _text(payload.get("status"))
    if event_type in {
        "task_waiting",
        "awaiting_input",
        "awaiting_approval",
        "task_paused",
        "request_user_input",
    }:
        structured_status = structured_status or "waiting"
    decision = classify_stop(
        message,
        structured_status=structured_status,
        has_user_task=_context_task_flag(ref, turn_id, contexts),
        event_type=event_type,
        explicit_final=event_type == "task_complete",
    )
    _remember_decision(
        ref,
        turn_id,
        contexts,
        decision,
        active=(
            decision.status in {"awaiting_approval", "awaiting_input"}
            or event_type
            in {
                "task_waiting",
                "awaiting_input",
                "awaiting_approval",
                "task_paused",
                "request_user_input",
            }
        ),
        timestamp_ms=completed_at_ms,
        pending_input_call_id=(
            request_input_call_id if event_type == "request_user_input" else None
        ),
    )
    if decision.status is None:
        return None
    final_hash = hashlib.sha256(normalized_message.encode("utf-8")).hexdigest()
    if decision.status == "completed":
        # 兼容 V1 完成事件键，避免升级后重复首发。
        key = f"codex:{ref.thread_id}:{turn_id}:{final_hash}"
    else:
        key = f"codex:{ref.thread_id}:{turn_id}:{decision.status}:{final_hash}"
    started_at_ms = starts.get(f"{ref.thread_id}:{turn_id}")
    duration_ms = (
        completed_at_ms - started_at_ms
        if started_at_ms is not None and completed_at_ms >= started_at_ms
        else None
    )
    return Event(
        source="codex",
        key=key,
        task_id=ref.thread_id,
        title=ref.title,
        completed_at_ms=completed_at_ms,
        duration_ms=duration_ms,
        summary_text=_tail_text(message),
        status=decision.status,
        stop_reason=decision.stop_reason,
        plan_fingerprint=decision.plan_fingerprint,
        turn_id=turn_id,
    )


def _scan_rollout(
    ref: RolloutRef,
    offset: int,
    starts: dict[str, int],
    contexts: dict[str, TurnContext],
) -> tuple[list[Event], int]:
    lines, new_offset = _read_complete_lines(ref.path, offset)
    events_by_key: dict[str, Event] = {}
    pending_call_events: dict[str, tuple[str | None, str | None]] = {}
    answered_call_ids: set[str] = set()
    active_turn_id: str | None = None
    known_active = [
        context
        for context in contexts.values()
        if context.source == "codex"
        and context.task_id == ref.thread_id
        and context.active
    ]
    if known_active:
        active_turn_id = max(known_active, key=lambda context: context.updated_at_ms).turn_id
    for raw in lines:
        try:
            record = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CodexSourceError("Codex rollout 记录不是有效 UTF-8") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise CodexSourceError("Codex rollout 记录不是有效 JSON") from exc
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            explicit_thread = _event_thread_id(record, payload)
            if explicit_thread is not None and explicit_thread != ref.thread_id:
                continue
        payload_type = _text(payload.get("type")) if isinstance(payload, dict) else None
        record_turn_id = (
            _event_turn_id(record, payload)
            if isinstance(payload, dict)
            else None
        )
        if payload_type == "task_started" and record_turn_id:
            active_turn_id = record_turn_id
        if isinstance(payload, dict):
            _record_user_evidence(ref, record, payload, contexts, active_turn_id)
            output_call_id = _function_call_output_id(payload)
            if output_call_id is not None:
                answered_call_ids.add(output_call_id)
                pending = pending_call_events.pop(output_call_id, None)
                turn_id = pending[0] if pending is not None else None
                if turn_id is None:
                    turn_id = _pending_input_turn(ref, output_call_id, contexts)
                if turn_id is None and record_turn_id:
                    turn_id = record_turn_id
                timestamp_ms = _iso_timestamp_ms(record.get("timestamp"))
                if timestamp_ms is not None:
                    _clear_pending_input(ref, turn_id, contexts, timestamp_ms)
                if pending is not None and pending[1] is not None:
                    events_by_key.pop(pending[1], None)
                elif turn_id is not None:
                    for key, event in list(events_by_key.items()):
                        if (
                            event.task_id == ref.thread_id
                            and event.turn_id == turn_id
                            and event.status == "awaiting_input"
                        ):
                            events_by_key.pop(key, None)
                continue
            request_call_id = _request_input_call_id(payload)
            if request_call_id is not None and request_call_id in answered_call_ids:
                # 仅防止异常乱序记录把已经返回的工具调用重新变成 waiting。
                continue
        event = _record_event(ref, record, starts, contexts, active_turn_id)
        if event is not None:
            events_by_key[event.key] = event
            if event.status == "awaiting_input":
                request_call_id = (
                    _request_input_call_id(payload)
                    if isinstance(payload, dict)
                    else None
                )
                if request_call_id is not None:
                    pending_call_events[request_call_id] = (event.turn_id, event.key)
        elif isinstance(payload, dict):
            request_call_id = _request_input_call_id(payload)
            if request_call_id is not None:
                turn_id = _event_turn_id(record, payload) or active_turn_id
                pending_call_events[request_call_id] = (turn_id, None)
    return list(events_by_key.values()), new_offset


def _history_final_value(row: Mapping[str, Any], column: str | None) -> Any:
    return _row_value(row, column)


def _load_history_rows(
    history_db: Path,
    title_by_thread: Mapping[str, str],
    source_by_thread: Mapping[str, str | None],
) -> list[_HistoryRow]:
    connection = _connect_readonly(Path(history_db))
    try:
        table_infos = _table_infos(connection)
        catalog_titles = _catalog_titles(connection, table_infos)
        result: list[_HistoryRow] = []
        matched_history_schema = False
        for table_name, columns, primary_keys in table_infos:
            thread_column = _thread_column(table_name, columns)
            status_column = _pick_column(columns, _STATUS_ALIASES)
            completed_column = _pick_column(columns, _COMPLETED_ALIASES)
            final_column = _pick_column(columns, _FINAL_ALIASES)
            if (
                thread_column is None
                or status_column is None
                or completed_column is None
                or final_column is None
            ):
                continue
            matched_history_schema = True
            turn_column = _pick_column(columns, _TURN_ALIASES)
            title_column = _pick_column(columns, _TITLE_ALIASES)
            source_column = _pick_column(columns, _SOURCE_ALIASES)
            started_column = _pick_column(columns, _STARTED_ALIASES)
            identity_column = primary_keys[0] if primary_keys else turn_column
            try:
                rows = connection.execute(
                    f"SELECT * FROM {_quote_identifier(table_name)}"
                ).fetchall()
            except sqlite3.Error as exc:
                raise CodexSourceError("无法读取 Codex 历史记录") from exc
            for row in rows:
                thread_id = _text(_row_value(row, thread_column))
                status = _text(_row_value(row, status_column))
                completed_at_ms = _timestamp_ms(
                    _row_value(row, completed_column),
                    numeric_is_ms=_column_is_milliseconds(completed_column),
                )
                final_value = _history_final_value(row, final_column)
                final_text = _text(final_value)
                if not thread_id or status is None or status.casefold() not in {
                    "completed",
                    "complete",
                    "success",
                    "succeeded",
                    "error",
                    "failed",
                    "failure",
                    "waiting",
                    "awaiting",
                    "awaiting_input",
                    "awaiting_approval",
                    "paused",
                    "blocked",
                }:
                    continue
                if completed_at_ms is None or final_text is None:
                    continue
                row_source = _text(_row_value(row, source_column))
                source = row_source if row_source is not None else source_by_thread.get(thread_id)
                if not _source_is_user(source):
                    continue
                turn_id = _text(_row_value(row, turn_column))
                identity = _text(_row_value(row, identity_column)) if identity_column else None
                if not identity:
                    identity = f"{thread_id}:{completed_at_ms}:{final_text}"
                title = (
                    title_by_thread.get(thread_id)
                    or catalog_titles.get(thread_id)
                    or _text(_row_value(row, title_column))
                    or ""
                )
                started_at_ms = _timestamp_ms(
                    _row_value(row, started_column),
                    numeric_is_ms=_column_is_milliseconds(started_column),
                )
                result.append(
                    _HistoryRow(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        status=status,
                        completed_at_ms=completed_at_ms,
                        final_message=final_text,
                        title=_normalise_title(title, thread_id),
                        source=source,
                        started_at_ms=started_at_ms,
                        identity=f"{table_name}:{identity}",
                    )
                )
        if not matched_history_schema:
            raise CodexSourceError("Codex 历史库 schema 缺少完成状态、时间或最终答案引用")
        return result
    finally:
        connection.close()


def _history_event(
    row: _HistoryRow,
    starts: Mapping[str, int],
    contexts: Mapping[str, TurnContext] | None = None,
) -> Event | None:
    turn_id = row.turn_id
    normalized_message = _normalise_message(row.final_message)
    if turn_id is None:
        turn_id = _stable_missing_turn(row.thread_id, row.completed_at_ms, normalized_message)
    context = (contexts or {}).get(_context_key(row.thread_id, turn_id))
    decision = classify_stop(
        row.final_message,
        structured_status=row.status,
        has_user_task=context.has_user_task if context else None,
        explicit_final=row.status.casefold() in {
            "completed", "complete", "success", "succeeded", "error", "failed", "failure"
        },
    )
    if decision.status is None:
        return None
    final_hash = hashlib.sha256(normalized_message.encode("utf-8")).hexdigest()
    if decision.status == "completed":
        key = f"codex:{row.thread_id}:{turn_id}:{final_hash}"
    else:
        key = f"codex:{row.thread_id}:{turn_id}:{decision.status}:{final_hash}"
    started_at_ms = starts.get(f"{row.thread_id}:{turn_id}") or row.started_at_ms
    duration_ms = (
        row.completed_at_ms - started_at_ms
        if started_at_ms is not None and row.completed_at_ms >= started_at_ms
        else None
    )
    return Event(
        source="codex",
        key=key,
        task_id=row.thread_id,
        title=row.title,
        completed_at_ms=row.completed_at_ms,
        duration_ms=duration_ms,
        summary_text=_tail_text(row.final_message),
        status=decision.status,
        stop_reason=decision.stop_reason,
        plan_fingerprint=decision.plan_fingerprint,
        turn_id=turn_id,
    )


def _codex_pairs_from_seen(keys: Iterable[str]) -> set[tuple[str, str]]:
    """从已持久化的 Codex 事件键提取 thread/turn 对。"""
    result: set[tuple[str, str]] = set()
    for key in keys:
        parts = key.split(":")
        if len(parts) < 4 or parts[0] != "codex":
            continue
        # waiting 只是可撤销的中间停顿；同一回合随后可能已收到答案，
        # 不能用它压掉历史库中的最终完成记录。
        if len(parts) >= 5 and parts[3] in {"awaiting_input", "awaiting_approval"}:
            continue
        if parts[1] and parts[2]:
            result.add((parts[1], parts[2]))
    return result


def scan_codex_events(
    codex_home: Path,
    state_db: Path | None,
    history_db: Path | None,
    state: RuntimeState,
    baseline: bool,
) -> tuple[list[Event], dict[str, int], dict[str, int]]:
    """增量读取 rollout 权威完成事件，并以历史库作兼容补充。"""
    metadata = _load_state_metadata(Path(codex_home), state_db)
    offsets = dict(state.rollout_offsets)
    identities = dict(state.rollout_identities)
    starts = dict(state.rollout_turn_started_ms)
    contexts = dict(state.turn_contexts)
    events: list[Event] = []
    emitted_keys: set[str] = set()
    rollout_pairs: set[tuple[str, str]] = _codex_pairs_from_seen(state.seen_event_keys)
    scanned_paths: set[str] = set()
    diagnostics: list[CodexSourceError] = []
    successful_file_reads = False

    for ref in metadata.refs:
        path = ref.path.resolve(strict=False)
        path_key = str(path)
        if path_key in scanned_paths:
            continue
        scanned_paths.add(path_key)
        try:
            file_exists = path.is_file()
        except OSError as exc:
            diagnostics.append(CodexSourceError("无法检查 Codex rollout 文件"))
            _LOGGER.warning(
                "codex rollout 文件处理失败: %s", type(exc).__name__
            )
            continue
        if not file_exists:
            continue
        try:
            current_identity = _file_identity(path)
            previous_identity = identities.get(path_key)
            # 旧状态没有身份字段时继续沿用 path->offset；只有确认身份变化
            # 才从头读取，避免同一路径替换成更大文件时跳过新开头记录。
            old_offset = offsets.get(path_key, 0)
            if previous_identity is not None and current_identity != previous_identity:
                old_offset = 0
            rollout_events, new_offset = _scan_rollout(ref, old_offset, starts, contexts)
            successful_file_reads = True
            identities[path_key] = current_identity
            offsets[path_key] = new_offset
        except CodexSourceError as exc:
            diagnostics.append(exc)
            _LOGGER.warning(
                "codex rollout 文件处理失败: %s", type(exc).__name__
            )
            continue
        for event in rollout_events:
            if event.turn_id is not None and event.status in {"completed", "error"}:
                rollout_pairs.add((event.task_id, event.turn_id))
            if event.key in emitted_keys or event.key in state.seen_event_keys:
                continue
            emitted_keys.add(event.key)
            if baseline:
                state.seen_event_keys.add(event.key)
            else:
                events.append(event)

    if history_db is not None:
        try:
            history_rows = _load_history_rows(
                Path(history_db), metadata.titles, metadata.sources
            )
        except CodexSourceError as exc:
            diagnostics.append(exc)
            history_rows = []
            _LOGGER.warning("codex 可选历史库处理失败: %s", type(exc).__name__)
        history_keys: set[str] = set()
        for row in history_rows:
            event = _history_event(row, starts, contexts)
            if event is None:
                continue
            if event.turn_id and event.status in {"completed", "error"}:
                context_key = _context_key(event.task_id, event.turn_id)
                previous = contexts.get(context_key)
                if previous is None or previous.updated_at_ms <= event.completed_at_ms:
                    # 历史补全也推进终态，撤销此前还未首发的等待通知。
                    contexts[context_key] = TurnContext(
                        source="codex", task_id=event.task_id, turn_id=event.turn_id,
                        has_user_task=previous.has_user_task if previous else None,
                        input_fingerprint=previous.input_fingerprint if previous else None,
                        status=event.status, active=False, updated_at_ms=event.completed_at_ms,
                    )
            if (event.task_id, event.turn_id) in rollout_pairs:
                continue
            if event.key in history_keys or event.key in emitted_keys or event.key in state.seen_event_keys:
                continue
            history_keys.add(event.key)
            if baseline:
                state.seen_event_keys.add(event.key)
            else:
                events.append(event)

    state.rollout_identities = identities
    state.turn_contexts = contexts
    events = [
        event for event in events
        if event.status not in {"awaiting_approval", "awaiting_input"}
        or not any(
            context.task_id == event.task_id and context.turn_id == event.turn_id
            and context.status in {"completed", "error"}
            and context.updated_at_ms >= event.completed_at_ms
            for context in contexts.values()
        )
    ]
    if diagnostics and not (successful_file_reads or events):
        raise diagnostics[0]
    return events, offsets, starts


def backfill_codex_thread(
    codex_home: Path,
    state_db: Path | None,
    history_db: Path | None,
    thread_id: str,
) -> Event:
    """只从指定用户 thread 的 rollout 返回最后一个完成事件。"""
    target = _text(thread_id)
    if target is None:
        raise CodexSourceError("指定 Codex thread ID 不能为空")
    metadata = _load_state_metadata(Path(codex_home), state_db)
    refs = [ref for ref in metadata.refs if ref.thread_id == target]
    if not refs:
        raise CodexSourceError("未找到指定 Codex thread 的 rollout")

    starts: dict[str, int] = {}
    contexts: dict[str, TurnContext] = {}
    all_events: list[tuple[int, Event]] = []
    scanned_paths: set[str] = set()
    sequence = 0
    for ref in refs:
        path = ref.path.resolve(strict=False)
        path_key = str(path)
        if path_key in scanned_paths:
            continue
        scanned_paths.add(path_key)
        if not path.is_file():
            continue
        rollout_events, _ = _scan_rollout(ref, 0, starts, contexts)
        for event in rollout_events:
            all_events.append((sequence, event))
            sequence += 1
    if not all_events:
        raise CodexSourceError("指定 Codex thread 没有可补发的 task_complete")
    _, event = max(all_events, key=lambda item: (item[1].completed_at_ms, item[0]))
    return event


__all__ = [
    "CodexSourceError",
    "RolloutRef",
    "backfill_codex_thread",
    "discover_rollouts",
    "scan_codex_events",
]
