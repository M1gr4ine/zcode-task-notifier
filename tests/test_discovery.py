import json
import os
import re
from pathlib import Path
import subprocess

import pytest

from zcode_task_notifier.config import AppConfig
from zcode_task_notifier.models import DiscoveredPaths


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


def make_valid_zcode_home(root: Path, *, layout: str = "v2") -> Path:
    """创建只含合成结构标志的 ZCode 根目录。"""
    root = root.resolve()
    (root / "v2" / "logs").mkdir(parents=True)
    (root / "v2" / "tasks-index.sqlite").touch()
    (root / "workspace" / "default").mkdir(parents=True)
    if layout == "v2":
        bot_root = root / "v2"
    elif layout == "legacy":
        bot_root = root
    else:
        raise ValueError(f"unknown layout: {layout}")
    (bot_root / "bot-config.json").write_text("{}\n", encoding="utf-8")
    (bot_root / "bot-state.v2.json").write_text("{}\n", encoding="utf-8")
    (bot_root / "credentials.json").write_text("{}\n", encoding="utf-8")
    return root


def make_valid_codex_home(root: Path) -> Path:
    root = root.resolve()
    (root / "sessions").mkdir(parents=True)
    (root / "state_example.sqlite").touch()
    return root


def make_sessions_only_codex_home(root: Path) -> Path:
    root = root.resolve()
    (root / "sessions").mkdir(parents=True)
    return root


def make_paths_with_bot(
    root: Path,
    bot_id: str,
    provider_user_id: str,
    credential_ref: str,
    credential_value: str,
    activated: bool,
    allowed_workspaces: list[str] | None = None,
) -> DiscoveredPaths:
    """在临时目录写入合成机器人配置，不使用本机运行数据。"""
    zcode_home = make_valid_zcode_home(root / "zcode")
    bot_config = {
        "bots": [
            {
                "id": bot_id,
                "provider": "weixin",
                "enabled": True,
                "providerUserId": provider_user_id,
                "credentialRef": credential_ref,
                "chatType": "private",
                "allowedWorkspaces": allowed_workspaces or ["workspace/default"],
            }
        ]
    }
    credentials = {credential_ref: credential_value}
    bot_state = {bot_id: {"activatedAt": "2026-01-02T03:04:05Z" if activated else None}}
    bot_root = zcode_home / "v2"
    (bot_root / "bot-config.json").write_text(
        json.dumps(bot_config), encoding="utf-8"
    )
    (bot_root / "credentials.json").write_text(
        json.dumps(credentials), encoding="utf-8"
    )
    (bot_root / "bot-state.v2.json").write_text(
        json.dumps(bot_state), encoding="utf-8"
    )
    return DiscoveredPaths(
        zcode_home=zcode_home,
        tasks_db=zcode_home / "v2" / "tasks-index.sqlite",
        zcode_logs=zcode_home / "v2" / "logs",
        bot_config=bot_root / "bot-config.json",
        bot_state=bot_root / "bot-state.v2.json",
        credentials=bot_root / "credentials.json",
        notification_workspace=zcode_home / "workspace" / "default",
        zcode_rollout_dir=None,
    )


def test_explicit_zcode_home_wins_without_fixed_drive(tmp_path: Path):
    home = tmp_path / "用户 数据" / ".zcode"
    make_valid_zcode_home(home)
    config = AppConfig(zcode_home=str(home))

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(config, {}, tmp_path)

    assert result.zcode_home == home.resolve()


def test_ambiguous_process_hints_fail_closed(tmp_path: Path):
    first = make_valid_zcode_home(tmp_path / "one")
    second = make_valid_zcode_home(tmp_path / "two")

    from zcode_task_notifier.discovery import DiscoveryError, discover_paths

    with pytest.raises(DiscoveryError, match="多个有效的 ZCode 目录"):
        discover_paths(AppConfig(), {}, tmp_path / "empty", [first, second])


def test_invalid_explicit_zcode_home_fails_immediately(tmp_path: Path):
    from zcode_task_notifier.discovery import DiscoveryError, discover_paths

    with pytest.raises(DiscoveryError, match="显式配置的 ZCode 目录无效"):
        discover_paths(AppConfig(zcode_home=str(tmp_path / "missing")), {}, tmp_path)


def test_environment_zcode_home_is_used_before_user_candidate(tmp_path: Path):
    configured = make_valid_zcode_home(tmp_path / "env-home")
    config = AppConfig()

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(config, {"ZCODE_HOME": str(configured)}, tmp_path)

    assert result.zcode_home == configured.resolve()


def test_environment_and_user_zcode_candidates_both_valid_fail_closed(tmp_path: Path):
    configured = make_valid_zcode_home(tmp_path / "env-home")
    make_valid_zcode_home(tmp_path / ".zcode")

    from zcode_task_notifier.discovery import DiscoveryError, discover_paths

    with pytest.raises(DiscoveryError, match="多个有效的 ZCode 目录"):
        discover_paths(AppConfig(), {"ZCODE_HOME": str(configured)}, tmp_path)


def test_v2_bot_files_are_selected_before_complete_legacy_files(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode", layout="v2")
    (home / "bot-config.json").write_text("legacy-config\n", encoding="utf-8")
    (home / "bot-state.v2.json").write_text("legacy-state\n", encoding="utf-8")
    (home / "credentials.json").write_text("legacy-credentials\n", encoding="utf-8")

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)

    assert result.bot_config == home / "v2" / "bot-config.json"
    assert result.bot_state == home / "v2" / "bot-state.v2.json"
    assert result.credentials == home / "v2" / "credentials.json"


def test_complete_legacy_bot_files_are_used_when_v2_layout_is_absent(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode", layout="legacy")

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)

    assert result.bot_config == home / "bot-config.json"
    assert result.bot_state == home / "bot-state.v2.json"
    assert result.credentials == home / "credentials.json"


def test_partial_v2_layout_falls_back_as_a_whole_to_legacy_layout(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode", layout="legacy")
    (home / "v2" / "bot-config.json").write_text("partial-v2\n", encoding="utf-8")

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)

    assert result.bot_config == home / "bot-config.json"
    assert result.bot_state == home / "bot-state.v2.json"
    assert result.credentials == home / "credentials.json"


def test_partial_bot_layout_cannot_mix_v2_and_legacy_files(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode", layout="v2")
    (home / "v2" / "bot-state.v2.json").unlink()
    (home / "bot-state.v2.json").write_text("partial-legacy\n", encoding="utf-8")

    from zcode_task_notifier.discovery import DiscoveryError, discover_paths

    with pytest.raises(DiscoveryError, match="显式配置的 ZCode 目录无效"):
        discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)


def test_auto_workspace_prefers_default_workspace(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode")

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)

    assert result.notification_workspace == home / "workspace" / "default"


def test_auto_workspace_falls_back_to_workspace_when_default_is_absent(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode")
    (home / "workspace" / "default").rmdir()

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)

    assert result.notification_workspace == home / "workspace"


def test_zcode_rollout_reparse_point_outside_root_is_rejected(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode")
    outside = tmp_path / "outside-rollout"
    outside.mkdir()
    cli = home / "cli"
    cli.mkdir()
    _create_directory_reparse_point(cli / "rollout", outside)

    from zcode_task_notifier.discovery import DiscoveryError, discover_paths

    with pytest.raises(DiscoveryError, match="rollout"):
        discover_paths(AppConfig(zcode_home=str(home)), {}, tmp_path)


def test_notification_workspace_must_be_under_zcode_home(tmp_path: Path):
    home = make_valid_zcode_home(tmp_path / "zcode")
    outside = tmp_path / "outside"
    outside.mkdir()
    config = AppConfig(
        zcode_home=str(home), notification_workspace=str(outside)
    )

    from zcode_task_notifier.discovery import DiscoveryError, discover_paths

    with pytest.raises(DiscoveryError, match="通知工作区"):
        discover_paths(config, {}, tmp_path)


def test_valid_codex_home_is_selected_when_enabled(tmp_path: Path):
    zcode = make_valid_zcode_home(tmp_path / "zcode")
    codex = make_valid_codex_home(tmp_path / "codex")
    config = AppConfig(codex_enabled=True, zcode_home=str(zcode), codex_home="auto")

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(config, {"CODEX_HOME": str(codex)}, tmp_path)

    assert result.codex_home == codex.resolve()
    assert result.codex_state_db == codex / "state_example.sqlite"
    assert result.codex_history_db is None


def test_sessions_only_codex_home_is_valid_and_has_optional_databases(tmp_path: Path):
    zcode = make_valid_zcode_home(tmp_path / "zcode")
    codex = make_sessions_only_codex_home(tmp_path / "codex")
    config = AppConfig(codex_enabled=True, zcode_home=str(zcode), codex_home="auto")

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(config, {"CODEX_HOME": str(codex)}, tmp_path)

    assert result.codex_home == codex.resolve()
    assert result.codex_state_db is None
    assert result.codex_history_db is None


def test_codex_discovery_is_skipped_when_disabled(tmp_path: Path):
    zcode = make_valid_zcode_home(tmp_path / "zcode")
    config = AppConfig(zcode_home=str(zcode), codex_enabled=False, codex_home=str(tmp_path / "missing"))

    from zcode_task_notifier.discovery import discover_paths

    result = discover_paths(config, {}, tmp_path)

    assert result.codex_home is None
    assert result.codex_state_db is None


def test_discover_python_returns_first_executable_candidate(tmp_path: Path):
    first = tmp_path / "python-one.exe"
    second = tmp_path / "python-two.exe"
    first.write_bytes(b"synthetic")
    second.write_bytes(b"synthetic")

    from zcode_task_notifier.discovery import discover_python

    assert discover_python([first, second]) == first.resolve()


def test_discover_python_rejects_missing_or_non_python_candidates(tmp_path: Path):
    missing = tmp_path / "python.exe"
    not_python = tmp_path / "runner.exe"
    not_python.write_bytes(b"synthetic")

    from zcode_task_notifier.discovery import DiscoveryError, discover_python

    with pytest.raises(DiscoveryError, match="Python"):
        discover_python([missing, not_python])


def test_enabled_activated_weixin_bot_is_loaded_without_decrypting(tmp_path: Path):
    paths = make_paths_with_bot(
        tmp_path,
        bot_id="bot-example-0001",
        provider_user_id="wx-user-example",
        credential_ref="credential-example",
        credential_value="enc:v1:opaque-test-value",
        activated=True,
    )

    from zcode_task_notifier.discovery import load_weixin_target

    target = load_weixin_target(paths)

    assert target == {
        "provider": "weixin",
        "botId": "bot-example-0001",
        "providerUserId": "wx-user-example",
        "chatType": "private",
    }
    assert "credentialRef" not in target
    assert "enc:v1:" not in json.dumps(target)


@pytest.mark.parametrize("activation_value", ["disabled", 0, False, "connected"])
def test_weixin_state_leaf_values_are_not_activation_times(
    tmp_path: Path, activation_value: object
):
    paths = make_paths_with_bot(
        tmp_path,
        bot_id="bot-example-0001",
        provider_user_id="wx-user-example",
        credential_ref="credential-example",
        credential_value="enc:v1:opaque-test-value",
        activated=True,
    )
    state = {"bot-example-0001": activation_value}
    paths.bot_state.write_text(json.dumps(state), encoding="utf-8")

    from zcode_task_notifier.discovery import DiscoveryError, load_weixin_target

    with pytest.raises(DiscoveryError, match="激活"):
        load_weixin_target(paths)


def test_weixin_activated_at_convention_is_accepted(tmp_path: Path):
    paths = make_paths_with_bot(
        tmp_path,
        bot_id="bot-example-0001",
        provider_user_id="wx-user-example",
        credential_ref="credential-example",
        credential_value="enc:v1:opaque-test-value",
        activated=True,
    )
    paths.bot_state.write_text(
        json.dumps({"bot-example-0001": {"weixinActivatedAt": "2026-01-02T03:04:05Z"}}),
        encoding="utf-8",
    )

    from zcode_task_notifier.discovery import load_weixin_target

    assert load_weixin_target(paths)["provider"] == "weixin"


def test_weixin_wildcard_workspace_authorization_is_supported(tmp_path: Path):
    paths = make_paths_with_bot(
        tmp_path,
        bot_id="bot-example-0001",
        provider_user_id="wx-user-example",
        credential_ref="credential-example",
        credential_value="enc:v1:opaque-test-value",
        activated=True,
        allowed_workspaces=["*"],
    )

    from zcode_task_notifier.discovery import load_weixin_target

    assert load_weixin_target(paths)["botId"] == "bot-example-0001"


@pytest.mark.parametrize(
    "change, expected",
    [
        ("disabled", "已启用"),
        ("not_activated", "未激活"),
        ("missing_ref", "credentialRef"),
        ("missing_credential", "凭据"),
        ("bad_prefix", "enc:v1"),
        ("wrong_provider", "微信机器人"),
        ("workspace_denied", "工作区"),
    ],
)
def test_weixin_bot_precheck_rejects_incomplete_configuration(
    tmp_path: Path, change: str, expected: str
):
    paths = make_paths_with_bot(
        tmp_path,
        bot_id="bot-example-0001",
        provider_user_id="wx-user-example",
        credential_ref="credential-example",
        credential_value="enc:v1:opaque-test-value",
        activated=True,
    )
    config = json.loads(paths.bot_config.read_text(encoding="utf-8"))
    bot = config["bots"][0]
    if change == "disabled":
        bot["enabled"] = False
    elif change == "not_activated":
        state = json.loads(paths.bot_state.read_text(encoding="utf-8"))
        state["bot-example-0001"]["activatedAt"] = None
        paths.bot_state.write_text(json.dumps(state), encoding="utf-8")
    elif change == "missing_ref":
        bot.pop("credentialRef")
    elif change == "missing_credential":
        paths.credentials.write_text(json.dumps({}), encoding="utf-8")
    elif change == "bad_prefix":
        paths.credentials.write_text(
            json.dumps({"credential-example": "opaque-test-value"}), encoding="utf-8"
        )
    elif change == "wrong_provider":
        bot["provider"] = "other"
    elif change == "workspace_denied":
        bot["allowedWorkspaces"] = ["other-workspace-example"]
    paths.bot_config.write_text(json.dumps(config), encoding="utf-8")

    from zcode_task_notifier.discovery import DiscoveryError, load_weixin_target

    with pytest.raises(DiscoveryError, match=expected):
        load_weixin_target(paths)


def test_multiple_enabled_weixin_bots_fail_closed_with_redacted_names(tmp_path: Path):
    paths = make_paths_with_bot(
        tmp_path,
        bot_id="bot-example-0001",
        provider_user_id="wx-user-example",
        credential_ref="credential-example",
        credential_value="enc:v1:opaque-test-value",
        activated=True,
    )
    payload = json.loads(paths.bot_config.read_text(encoding="utf-8"))
    payload["bots"].append(
        {
            "id": "bot-example-0002",
            "provider": "weixin",
            "enabled": True,
            "providerUserId": "wx-user-example-2",
            "credentialRef": "credential-example-2",
            "allowedWorkspaces": ["workspace/default"],
        }
    )
    paths.bot_config.write_text(json.dumps(payload), encoding="utf-8")
    credentials = {"credential-example": "enc:v1:opaque-test-value", "credential-example-2": "enc:v1:opaque-test-value-2"}
    paths.credentials.write_text(json.dumps(credentials), encoding="utf-8")
    state = json.loads(paths.bot_state.read_text(encoding="utf-8"))
    state["bot-example-0002"] = {"activatedAt": "2026-01-02T03:04:05Z"}
    paths.bot_state.write_text(json.dumps(state), encoding="utf-8")

    from zcode_task_notifier.discovery import DiscoveryError, load_weixin_target

    with pytest.raises(DiscoveryError, match="多个启用的微信机器人") as exc_info:
        load_weixin_target(paths)
    message = str(exc_info.value)
    assert "bot-example-0001" not in message
    assert "bot-example-0002" not in message
    assert len(re.findall(r"微信机器人-[0-9a-f]{10}", message)) == 2


def test_redact_path_uses_longest_prefix_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    zcode = tmp_path / "zcode"
    codex = zcode / "codex"
    paths = DiscoveredPaths(
        zcode_home=zcode,
        tasks_db=zcode / "tasks.sqlite",
        zcode_logs=zcode / "logs",
        bot_config=zcode / "bot-config.json",
        bot_state=zcode / "bot-state.v2.json",
        credentials=zcode / "credentials.json",
        notification_workspace=zcode / "workspace",
        codex_home=codex,
    )
    monkeypatch.setenv("ZCODE_HOME", str(zcode))
    monkeypatch.setenv("CODEX_HOME", str(codex))

    from zcode_task_notifier.discovery import redact_path

    assert redact_path(codex / "sessions", paths) == "%CODEX_HOME%\\sessions"
    assert redact_path(zcode / "workspace", paths) == "%ZCODE_HOME%\\workspace"


def test_redact_path_folds_known_user_environment_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    user_profile = tmp_path / "profile"
    local_app_data = user_profile / "local-app-data"
    paths = DiscoveredPaths(
        zcode_home=tmp_path / "zcode",
        tasks_db=tmp_path / "tasks.sqlite",
        zcode_logs=tmp_path / "logs",
        bot_config=tmp_path / "bot.json",
        bot_state=tmp_path / "state.json",
        credentials=tmp_path / "credentials.json",
        notification_workspace=tmp_path / "workspace",
    )
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    from zcode_task_notifier.discovery import redact_path

    assert redact_path(local_app_data / "Product", paths) == "%LOCALAPPDATA%\\Product"
    assert redact_path(user_profile / "Documents", paths) == "%USERPROFILE%\\Documents"
