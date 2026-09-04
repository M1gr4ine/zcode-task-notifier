"""运行路径发现和微信机器人前置校验。

该模块只检查目录结构并读取机器人配置；不会修改来源目录，也不会解密或
返回机器人凭据。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import AppConfig
from .models import DiscoveredPaths


class DiscoveryError(RuntimeError):
    """路径或通知目标不能被安全、唯一地确认。"""


def _resolve(path: Path | str) -> Path:
    """以非严格方式规范化路径，不要求候选已经存在。"""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DiscoveryError("路径无法规范化") from exc


def _deduplicate_resolved(paths: Sequence[Path]) -> list[Path]:
    """按解析后的路径去重并保留候选优先级。"""
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in paths:
        try:
            resolved = _resolve(candidate)
        except DiscoveryError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _zcode_candidates(
    configured: str,
    environ: Mapping[str, str],
    user_home: Path,
    process_hints: Sequence[Path],
) -> list[Path]:
    """按配置、环境、用户目录和进程提示收集 ZCode 候选。"""
    ordered: list[Path] = []
    if configured != "auto":
        ordered.append(Path(configured))
    if environ.get("ZCODE_HOME"):
        ordered.append(Path(environ["ZCODE_HOME"]))
    ordered.append(Path(user_home) / ".zcode")
    ordered.extend(Path(hint) for hint in process_hints)
    return _deduplicate_resolved(ordered)


def _codex_candidates(
    configured: str,
    environ: Mapping[str, str],
    user_home: Path,
) -> list[Path]:
    """按配置、环境变量和用户目录收集 Codex 候选。"""
    ordered: list[Path] = []
    if configured != "auto":
        ordered.append(Path(configured))
    if environ.get("CODEX_HOME"):
        ordered.append(Path(environ["CODEX_HOME"]))
    ordered.append(Path(user_home) / ".codex")
    return _deduplicate_resolved(ordered)


def _valid_zcode_root(path: Path) -> bool:
    """检查 ZCode 根的稳定结构标志。"""
    return (
        path.is_dir()
        and (path / "v2" / "tasks-index.sqlite").is_file()
        and _zcode_bot_layout(path) is not None
    )


def _zcode_bot_layout(path: Path) -> tuple[Path, Path, Path] | None:
    """选择完整的机器人三件套，v2 优先且不跨布局拼接。"""
    for root in (path / "v2", path):
        candidates = (
            root / "bot-config.json",
            root / "bot-state.v2.json",
            root / "credentials.json",
        )
        if all(candidate.is_file() for candidate in candidates):
            return candidates
    return None


def _codex_databases(path: Path) -> tuple[Path | None, Path | None]:
    """返回 Codex 根下稳定命名的状态库和兼容历史库。"""
    states = sorted(item for item in path.glob("state_*.sqlite") if item.is_file())
    histories = sorted(
        item for item in path.glob("thread_history_*.sqlite") if item.is_file()
    )
    return (states[0] if states else None, histories[0] if histories else None)


def _valid_codex_root(path: Path) -> bool:
    # sessions 是 Codex 当前 rollout 的稳定根标志；数据库仅为可选兼容源，
    # 不应阻断仅有 sessions 的有效安装。
    return path.is_dir() and (path / "sessions").is_dir()


def _safe_candidate_label(path: Path, environ: Mapping[str, str], user_home: Path) -> str:
    """为歧义错误生成不含原始路径的稳定候选标签。"""
    resolved = _resolve(path)
    prefixes: list[tuple[str, Path]] = []
    for name, value in (
        ("%ZCODE_HOME%", environ.get("ZCODE_HOME")),
        ("%CODEX_HOME%", environ.get("CODEX_HOME")),
        ("%USERPROFILE%", environ.get("USERPROFILE")),
        ("%LOCALAPPDATA%", environ.get("LOCALAPPDATA")),
    ):
        if value:
            prefixes.append((name, _resolve(value)))
    prefixes.append(("%USERPROFILE%", _resolve(user_home)))
    redacted = _replace_with_longest_prefix(resolved, prefixes)
    if redacted != str(resolved):
        return redacted
    digest = hashlib.sha256(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()[:10]
    return f"<候选-{digest}>"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _discover_workspace(config: AppConfig, zcode_home: Path) -> Path:
    configured = config.notification_workspace
    if configured == "auto":
        workspace_root = zcode_home / "workspace"
        default_workspace = workspace_root / "default"
        candidate = default_workspace if default_workspace.is_dir() else workspace_root
        if not candidate.is_dir():
            raise DiscoveryError("通知工作区无法自动发现")
    else:
        raw = Path(configured).expanduser()
        candidate = raw if raw.is_absolute() else zcode_home / raw
        candidate = _resolve(candidate)
        if not candidate.is_dir():
            raise DiscoveryError("通知工作区不存在或不是目录")

    candidate = _resolve(candidate)
    if not _is_relative_to(candidate, zcode_home):
        raise DiscoveryError("通知工作区必须位于已确认的 ZCode 目录内")
    return candidate


def _discover_zcode_rollout_dir(zcode_home: Path) -> Path | None:
    """解析并约束 ZCode rollout 目录，拒绝越界链接或重解析点。"""
    raw = zcode_home / "cli" / "rollout"
    try:
        present = raw.exists() or raw.is_symlink()
    except OSError as exc:
        raise DiscoveryError("ZCode rollout 目录无法检查") from exc
    if not present:
        return None
    resolved = _resolve(raw)
    if not _is_relative_to(resolved, zcode_home):
        raise DiscoveryError("ZCode rollout 目录必须位于已确认的 ZCode 目录内")
    try:
        if not resolved.is_dir():
            return None
    except OSError as exc:
        raise DiscoveryError("ZCode rollout 目录无法检查") from exc
    return resolved


def _select_zcode_home(
    config: AppConfig,
    environ: Mapping[str, str],
    user_home: Path,
    process_hints: Sequence[Path],
) -> Path:
    configured = config.zcode_home
    if configured != "auto":
        explicit = _resolve(configured)
        if not _valid_zcode_root(explicit):
            raise DiscoveryError("显式配置的 ZCode 目录无效")
        return explicit

    candidates = _zcode_candidates(configured, environ, user_home, process_hints)
    valid = [candidate for candidate in candidates if _valid_zcode_root(candidate)]
    if len(valid) > 1:
        labels = ", ".join(
            _safe_candidate_label(candidate, environ, user_home) for candidate in valid
        )
        raise DiscoveryError(f"多个有效的 ZCode 目录，拒绝自动选择: {labels}")
    if not valid:
        raise DiscoveryError("未发现有效的 ZCode 目录")
    return valid[0]


def _select_codex_home(
    config: AppConfig, environ: Mapping[str, str], user_home: Path
) -> tuple[Path, Path | None, Path | None]:
    configured = config.codex_home
    if configured != "auto":
        explicit = _resolve(configured)
        if not _valid_codex_root(explicit):
            raise DiscoveryError("显式配置的 Codex 目录无效")
        state_db, history_db = _codex_databases(explicit)
        return explicit, state_db, history_db

    candidates = _codex_candidates(configured, environ, user_home)
    valid = [candidate for candidate in candidates if _valid_codex_root(candidate)]
    if len(valid) > 1:
        labels = ", ".join(
            _safe_candidate_label(candidate, environ, user_home) for candidate in valid
        )
        raise DiscoveryError(f"多个有效的 Codex 目录，拒绝自动选择: {labels}")
    if not valid:
        raise DiscoveryError("未发现有效的 Codex 目录")
    state_db, history_db = _codex_databases(valid[0])
    return valid[0], state_db, history_db


def discover_paths(
    config: AppConfig,
    environ: Mapping[str, str],
    user_home: Path,
    process_hints: Sequence[Path] = (),
) -> DiscoveredPaths:
    """发现并验证 ZCode、可选 Codex 及通知工作区的路径。"""
    zcode_home = _select_zcode_home(config, environ, Path(user_home), process_hints)
    notification_workspace = _discover_workspace(config, zcode_home)
    rollout = _discover_zcode_rollout_dir(zcode_home)
    bot_layout = _zcode_bot_layout(zcode_home)
    if bot_layout is None:
        # _select_zcode_home 已经做过同样的检查；保留这里的显式保护，避免
        # 后续改动导致返回跨布局拼接的机器人路径。
        raise DiscoveryError("ZCode 机器人配置不完整")

    codex_home: Path | None = None
    codex_state_db: Path | None = None
    codex_history_db: Path | None = None
    if config.codex_enabled:
        codex_home, codex_state_db, codex_history_db = _select_codex_home(
            config, environ, Path(user_home)
        )

    return DiscoveredPaths(
        zcode_home=zcode_home,
        tasks_db=zcode_home / "v2" / "tasks-index.sqlite",
        zcode_logs=zcode_home / "v2" / "logs",
        bot_config=bot_layout[0],
        bot_state=bot_layout[1],
        credentials=bot_layout[2],
        notification_workspace=notification_workspace,
        zcode_rollout_dir=rollout,
        codex_home=codex_home,
        codex_state_db=codex_state_db,
        codex_history_db=codex_history_db,
    )


def discover_python(candidates: Sequence[Path]) -> Path:
    """从已收集候选中选择第一个看起来可执行的 Python 解释器。"""
    for candidate in candidates:
        try:
            path = _resolve(candidate)
        except DiscoveryError:
            continue
        if not path.is_file():
            continue
        name = path.name.casefold()
        if name in {"py", "py.exe"} or name.startswith("python"):
            return path
    raise DiscoveryError("未找到可用的 Python 解释器")


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"无法读取{label}") from exc


def _iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _bot_records(payload: Any) -> list[dict[str, Any]]:
    """兼容对象、列表及嵌套配置容器，收集带 provider 的机器人记录。"""
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    for record in _iter_dicts(payload):
        if "provider" not in record and "providerType" not in record:
            continue
        if "enabled" not in record:
            continue
        marker = id(record)
        if marker in seen:
            continue
        seen.add(marker)
        records.append(record)
    return records


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _bot_id(record: Mapping[str, Any]) -> str | None:
    return _non_empty_string(record.get("botId")) or _non_empty_string(record.get("id"))


def _bot_provider(record: Mapping[str, Any]) -> Any:
    return record.get("provider", record.get("providerType"))


def _activation_value(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        if text.isdecimal():
            try:
                return int(text) > 0
            except ValueError:
                return False
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    if isinstance(value, int):
        return value > 0
    if isinstance(value, float):
        return math.isfinite(value) and value > 0
    return False


_ACTIVATION_KEYS = (
    "activatedAt",
    "activated_at",
    "activationTime",
    "activation_time",
    "activatedAtMs",
    "weixinActivatedAt",
)


def _has_activation(payload: Any, bot_id: str) -> bool:
    """只在状态对象与机器人 ID 关联时接受激活时间。"""
    def visit(value: Any, key_hint: str | None = None) -> bool:
        if isinstance(value, dict):
            record_id = next(
                (
                    _non_empty_string(value.get(name))
                    for name in ("botId", "id", "bot_id")
                    if _non_empty_string(value.get(name))
                ),
                None,
            )
            associated = record_id == bot_id or key_hint == bot_id
            if associated and any(_activation_value(value.get(key)) for key in _ACTIVATION_KEYS):
                return True
            return any(visit(child, str(key)) for key, child in value.items())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return key_hint == bot_id and _activation_value(value)

    return visit(payload)


def _credential_value_matches(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("enc:v1:")


def _has_credential(payload: Any, credential_ref: str) -> bool:
    """在不保留凭据值的前提下验证引用对应的 enc:v1 前缀。"""
    def visit(value: Any, key_hint: str | None = None) -> bool:
        if isinstance(value, dict):
            if key_hint == credential_ref and _credential_value_matches(value):
                return True
            direct = value.get(credential_ref)
            if _credential_value_matches(direct):
                return True
            if isinstance(direct, dict):
                for key in ("value", "secret", "credential", "token"):
                    if _credential_value_matches(direct.get(key)):
                        return True
            for key in ("ref", "reference", "credentialRef", "credential_ref", "id"):
                if value.get(key) == credential_ref:
                    for value_key in (
                        "value",
                        "secret",
                        "credential",
                        "token",
                        "encrypted",
                        "encryptedValue",
                    ):
                        if _credential_value_matches(value.get(value_key)):
                            return True
            return any(visit(child, str(key)) for key, child in value.items())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(payload)


def _workspace_names(workspace: Path, zcode_home: Path) -> set[str]:
    values = {str(workspace), workspace.name}
    try:
        relative = workspace.relative_to(zcode_home)
    except ValueError:
        relative = None
    if relative is not None:
        values.add(str(relative))
        values.add(relative.as_posix())
    return {value.casefold() for value in values if value}


def _workspace_is_allowed(record: Mapping[str, Any], paths: DiscoveredPaths) -> bool:
    allowed = record.get("allowedWorkspaces")
    if isinstance(allowed, str):
        values: list[Any] = [allowed]
    elif isinstance(allowed, list):
        values = allowed
    elif isinstance(allowed, dict):
        values = [key for key, enabled in allowed.items() if enabled is True]
    else:
        return False

    names = _workspace_names(paths.notification_workspace, paths.zcode_home)
    for item in values:
        if not isinstance(item, str):
            continue
        if item == "*" or item.casefold() in names:
            return True
        if Path(item).is_absolute() and _resolve(item) == _resolve(paths.notification_workspace):
            return True
    return False


def _bot_label(record: Mapping[str, Any], ordinal: int) -> str:
    """错误信息使用稳定短哈希标签，不包含完整名称或标识。"""
    name = _non_empty_string(record.get("name")) or _bot_id(record) or f"unknown-{ordinal}"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"微信机器人-{digest}"


def load_weixin_target(paths: DiscoveredPaths) -> dict[str, str]:
    """只读校验唯一启用且已激活的微信机器人。

    返回值只包含投递所需的非凭据字段，绝不返回 credentialRef 或密文。
    """
    bot_payload = _read_json(paths.bot_config, "机器人配置")
    records = [
        record
        for record in _bot_records(bot_payload)
        if _bot_provider(record) == "weixin" and record.get("enabled") is True
    ]
    if len(records) > 1:
        labels = ", ".join(_bot_label(record, index) for index, record in enumerate(records, 1))
        raise DiscoveryError(f"多个启用的微信机器人，拒绝选择: {labels}")
    if not records:
        raise DiscoveryError("未找到已启用的微信机器人")

    record = records[0]
    bot_id = _bot_id(record)
    if not bot_id:
        raise DiscoveryError("微信机器人缺少 botId")
    provider_user_id = _non_empty_string(
        record.get("providerUserId", record.get("provider_user_id"))
    )
    if not provider_user_id:
        raise DiscoveryError("微信机器人缺少 providerUserId")
    credential_ref = _non_empty_string(record.get("credentialRef"))
    if not credential_ref:
        raise DiscoveryError("微信机器人缺少 credentialRef")
    if not _workspace_is_allowed(record, paths):
        raise DiscoveryError("微信机器人未授权通知工作区")

    credentials_payload = _read_json(paths.credentials, "机器人凭据引用")
    if not _has_credential(credentials_payload, credential_ref):
        raise DiscoveryError("微信机器人凭据值不是 enc:v1: 前缀")

    state_payload = _read_json(paths.bot_state, "机器人状态")
    if not _has_activation(state_payload, bot_id):
        raise DiscoveryError("微信机器人尚未激活")

    chat_type = _non_empty_string(record.get("chatType")) or "private"
    return {
        "provider": "weixin",
        "botId": bot_id,
        "providerUserId": provider_user_id,
        "chatType": chat_type,
    }


def _replace_with_longest_prefix(path: Path, prefixes: Sequence[tuple[str, Path]]) -> str:
    resolved = _resolve(path)
    normalized: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for label, prefix in prefixes:
        try:
            resolved_prefix = _resolve(prefix)
        except DiscoveryError:
            continue
        key = (label, os.path.normcase(str(resolved_prefix)))
        if key in seen:
            continue
        seen.add(key)
        normalized.append((label, resolved_prefix))
    normalized.sort(key=lambda pair: len(str(pair[1])), reverse=True)
    for label, prefix in normalized:
        try:
            relative = resolved.relative_to(prefix)
        except ValueError:
            continue
        if str(relative) in ("", "."):
            return label
        return label + "\\" + relative.as_posix().replace("/", "\\")
    return str(resolved)


def redact_path(path: Path, paths: DiscoveredPaths) -> str:
    """将路径按最长已知前缀折叠为语义环境变量。"""
    prefixes: list[tuple[str, Path]] = []
    if paths.codex_home is not None:
        prefixes.append(("%CODEX_HOME%", paths.codex_home))
    prefixes.append(("%ZCODE_HOME%", paths.zcode_home))
    for name, label in (
        ("ZCODE_HOME", "%ZCODE_HOME%"),
        ("CODEX_HOME", "%CODEX_HOME%"),
        ("LOCALAPPDATA", "%LOCALAPPDATA%"),
        ("USERPROFILE", "%USERPROFILE%"),
    ):
        value = os.environ.get(name)
        if value:
            prefixes.append((label, _resolve(value)))
    home = os.environ.get("USERPROFILE")
    if not home:
        try:
            prefixes.append(("%USERPROFILE%", Path.home()))
        except (OSError, RuntimeError):
            pass
    return _replace_with_longest_prefix(_resolve(path), prefixes)
