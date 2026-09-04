"""通知器配置的默认值、JSON 编解码和校验。"""

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """配置文件无法解析或未通过约束。"""


@dataclass(frozen=True)
class AppConfig:
    zcode_home: str = "auto"
    notification_workspace: str = "auto"
    codex_enabled: bool = False
    codex_home: str = "auto"
    interval_seconds: int = 60
    model: str = "builtin:bigmodel-coding-plan/GLM-5-Turbo"
    codex_prefix: str = "[codex]"
    outbox_retention_days: int = 7

    def __post_init__(self) -> None:
        _validate_config(self)


_CONFIG_FIELDS = frozenset(field.name for field in fields(AppConfig))


def _validate_config(config: AppConfig) -> None:
    """校验公共配置约束，阻止不安全或不可执行的值。"""
    if not isinstance(config.zcode_home, str):
        raise ConfigError("zcode_home 必须是字符串")
    if not isinstance(config.notification_workspace, str):
        raise ConfigError("notification_workspace 必须是字符串")
    if not isinstance(config.codex_home, str):
        raise ConfigError("codex_home 必须是字符串")
    if not isinstance(config.codex_enabled, bool):
        raise ConfigError("codex_enabled 必须是布尔值")
    if not isinstance(config.interval_seconds, int) or isinstance(config.interval_seconds, bool):
        raise ConfigError("interval_seconds 必须是整数")
    if config.interval_seconds < 60:
        raise ConfigError("interval_seconds 必须至少为 60 秒")
    if not isinstance(config.model, str):
        raise ConfigError("model 必须是字符串")
    if config.codex_prefix != "[codex]":
        raise ConfigError("codex_prefix 必须为 [codex]")

    if not isinstance(config.outbox_retention_days, int) or isinstance(
        config.outbox_retention_days, bool
    ):
        raise ConfigError("outbox_retention_days 必须是整数")
    if config.outbox_retention_days < 0:
        raise ConfigError("outbox_retention_days 不能为负数")


def save_config(path: Path, config: AppConfig) -> None:
    """将配置保存为不展开本机路径的 UTF-8 JSON。"""
    if not isinstance(config, AppConfig):
        raise ConfigError("config 必须是 AppConfig")
    _validate_config(config)
    payload = asdict(config)
    payload["schema_version"] = 1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def load_config(path: Path) -> AppConfig:
    """读取配置；未知字段（包括旧版重试字段）安全忽略。"""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload: Any = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("配置根节点必须是对象")
    schema_version = payload.get("schema_version", 1)
    if type(schema_version) is not int or schema_version != 1:
        raise ConfigError("不支持的配置 schema_version")

    values = {name: payload[name] for name in _CONFIG_FIELDS if name in payload}
    try:
        config = AppConfig(**values)
    except (TypeError, ValueError) as exc:
        raise ConfigError("配置字段类型不正确") from exc
    _validate_config(config)
    return config
