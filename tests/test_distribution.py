"""Task 7 分发材料和发布隐私门禁的可重复测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def tracked_candidate_files(root: Path) -> list[Path]:
    """返回发布写集和现有源码中可安全扫描的文本文件。

    历史测试中保留了用于验证脱敏的合成路径样例；它们不是分发材料，
    因而不应被发布门禁当作待发布内容。Task 7 新增的两个测试文件仍会
    被显式纳入扫描。
    """
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, text=False
    )
    tracked = {
        Path(item.decode("utf-8"))
        for item in output.split(b"\0")
        if item
    }
    prefixes = (
        Path("README.md"),
        Path("LICENSE"),
        Path(".gitignore"),
        Path(".gitattributes"),
        Path("config.example.json"),
        Path("pyproject.toml"),
        Path("scripts"),
        Path("skills/zcode-task-notifier-install"),
        Path("tests/test_distribution.py"),
        Path("tests/test_install_skill.py"),
    )
    result: set[Path] = set()
    for relative in tracked:
        if any(relative == prefix or prefix in relative.parents for prefix in prefixes):
            result.add(root / relative)
    for relative in (
        Path("README.md"),
        Path("LICENSE"),
        Path(".gitignore"),
        Path(".gitattributes"),
        Path("config.example.json"),
        Path("scripts/install.ps1"),
        Path("scripts/install.cmd"),
        Path("scripts/uninstall.ps1"),
        Path("scripts/privacy-check.ps1"),
        Path("skills/zcode-task-notifier-install/SKILL.md"),
        Path("tests/test_distribution.py"),
        Path("tests/test_install_skill.py"),
    ):
        candidate = root / relative
        if candidate.is_file():
            result.add(candidate)
    return sorted(result)


def test_distribution_has_no_absolute_windows_paths_or_runtime_data():
    root = repo_root()
    forbidden_suffixes = {".sqlite", ".db", ".jsonl", ".log", ".lock", ".pyc"}
    drive_path = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
    user_path = re.compile(r"(?i)Users[\\/][^\\/\s]+")
    for path in tracked_candidate_files(root):
        assert path.suffix.lower() not in forbidden_suffixes
        text = path.read_text(encoding="utf-8")
        assert drive_path.search(text) is None, path
        assert user_path.search(text) is None, path


def test_distribution_has_no_realistic_credentials_or_high_entropy_assignments():
    root = repo_root()
    credential_shape = re.compile(
        r"(?i)(?:providerUserId|credentialRef|botId|token|secret)\s*[:=]\s*['\"]"
    )
    uuid_shape = re.compile(
        r"(?i)bot-(?!example(?:[-_]|$))[0-9a-f]{8}-[0-9a-f-]{27,}"
    )
    encrypted_shape = re.compile(r"enc:v1:(?!opaque-test-value|synthetic)[A-Za-z0-9+/=_-]{16,}")
    high_entropy_assignment = re.compile(
        r"(?i)(?:token|secret|password)\s*[:=]\s*['\"][A-Za-z0-9+/=_-]{32,}['\"]"
    )
    for path in tracked_candidate_files(root):
        text = path.read_text(encoding="utf-8")
        assert uuid_shape.search(text) is None, path
        assert encrypted_shape.search(text) is None, path
        assert high_entropy_assignment.search(text) is None, path
        # 字段名本身可用于实现/文档；这里只拒绝看起来像真实值的赋值。
        for match in credential_shape.finditer(text):
            value_start = match.end()
            value_fragment = text[value_start : value_start + 48]
            assert "example" in value_fragment.casefold() or "opaque" in value_fragment.casefold(), (
                path,
                match.group(0),
            )


def test_example_config_has_only_auto_paths():
    payload = json.loads((repo_root() / "config.example.json").read_text(encoding="utf-8"))
    assert payload["zcode_home"] == "auto"
    assert payload["notification_workspace"] == "auto"
    assert payload["codex_home"] == "auto"
    assert payload["codex_enabled"] is False
    assert payload["interval_seconds"] == 60
    assert payload["codex_prefix"] == "[codex]"
    assert "retry_delays_seconds" not in payload
    assert "max_retry_attempts" not in payload


def test_distribution_metadata_and_scripts_exist():
    root = repo_root()
    required = (
        "README.md",
        "LICENSE",
        ".gitignore",
        ".gitattributes",
        "config.example.json",
        "scripts/install.ps1",
        "scripts/install.cmd",
        "scripts/uninstall.ps1",
        "scripts/privacy-check.ps1",
        "skills/zcode-task-notifier-install/SKILL.md",
    )
    assert all((root / relative).is_file() for relative in required)


def test_readme_publishes_manual_skill_registration_entrypoint():
    readme = (repo_root() / "README.md").read_text(encoding="utf-8")
    repository_url = "https://github.com/M1gr4ine/zcode-task-notifier"
    assert any(repository_url in line for line in readme.splitlines()[:5])
    prompt_match = re.search(r"```text\n(?P<prompt>.*?)\n```", readme, re.DOTALL)
    assert prompt_match is not None
    prompt = prompt_match.group("prompt")
    assert repository_url in prompt
    assert "下载并注册" in prompt
    assert "用户手动输入 /zcode-task-notifier-install" in prompt
    assert "此仓库" not in prompt
    assert "PRIVACY_CHECK_VIOLATION" in readme
    assert "PRIVACY_CHECK_ERROR" in readme
    assert "输出只包含文件或对象和规则名" not in readme


def test_scripts_expose_dynamic_install_and_safe_uninstall_contract():
    root = repo_root()
    installer = (root / "scripts/install.ps1").read_text(encoding="utf-8")
    uninstaller = (root / "scripts/uninstall.ps1").read_text(encoding="utf-8")
    privacy = (root / "scripts/privacy-check.ps1").read_text(encoding="utf-8")
    for marker in (
        "$PSScriptRoot",
        "GetFolderPath('LocalApplicationData')",
        "EnableCodex",
        "DisableCodex",
        "NonInteractive",
        "New-ScheduledTaskAction",
        "Register-ScheduledTask",
        "ZCodeTaskNotifier",
        "baseline",
        "doctor",
    ):
        assert marker in installer
    for marker in ("ZCodeTaskNotifier", "Resolve-Path", "LocalApplicationData", "KeepData"):
        assert marker in uninstaller
    for marker in ("git ls-files", "git rev-list", "git cat-file", "History"):
        assert marker in privacy


def test_installer_writes_json_without_a_powershell_51_bom():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    assert "[IO.File]::WriteAllText" in installer
    assert "Text.UTF8Encoding($false)" in installer
    assert "Set-Content -LiteralPath $temporary -Encoding UTF8" not in installer


def test_legacy_watcher_matching_uses_exact_path_and_action_without_body_reads():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    start = installer.index("function Get-LegacyTaskCandidates")
    end = installer.index("function", start + len("function Get-LegacyTaskCandidates"))
    matcher = installer[start:end]
    assert "Get-Content" not in matcher
    assert "task-watch\\watch.py" in matcher
    assert "GetFullPath" in matcher
    assert "Arguments" in matcher
    assert "-Execute" in matcher
    assert "zcode_task_notifier|ZCodeTaskNotifier|task-watch" not in matcher


def test_installer_records_legacy_task_path_and_enabled_state_for_rollback():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    for marker in (
        "LegacyTaskRecords",
        "TaskPath",
        "Enabled",
        "Export-ScheduledTask",
        "Stop-ScheduledTask",
        "Disable-ScheduledTask",
        "Restore-LegacyTaskRecords",
        "TaskPathHash",
    ):
        assert marker in installer


def test_uninstaller_accepts_install_dir_and_rejects_non_container_or_reparse_target():
    uninstaller = (repo_root() / "scripts/uninstall.ps1").read_text(encoding="utf-8")
    assert "[string]$InstallDir" in uninstaller
    assert "-PathType Container" in uninstaller
    assert "ReparsePoint" in uninstaller
    assert "Resolve-InstallRoot" in uninstaller
    assert "LocalApplicationData" in uninstaller


def test_privacy_checker_fails_closed_for_unreadable_binary_and_oversize_history():
    privacy = (repo_root() / "scripts/privacy-check.ps1").read_text(encoding="utf-8")
    assert "-ErrorAction SilentlyContinue" not in privacy
    assert "binary-file" in privacy
    assert "invalid-encoding" in privacy
    assert "oversize-object" in privacy
    assert "cat-file-exit" in privacy
    assert "git cat-file failed" in privacy


def test_privacy_history_accepts_raw_git_objects_before_strict_blob_validation():
    privacy = (repo_root() / "scripts/privacy-check.ps1").read_text(encoding="utf-8")
    # tree/blob 响应可能含任意字节；只有 blob 正文进入严格 UTF-8 校验。
    assert "StandardOutputEncoding = New-Object Text.UTF8Encoding($false, $false)" in privacy
    assert "New-Object Text.UTF8Encoding($false, $true)" in privacy
    assert "PRIVACY_CHECK_VIOLATION" in privacy
    assert "PRIVACY_CHECK_ERROR" in privacy
    assert "Write-Error" not in privacy
    assert 'Write-Host ("Privacy check failed: " + $item.Location' not in privacy


def _powershell_executable() -> str | None:
    for name in ("powershell.exe", "pwsh"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None


def _init_privacy_fixture(root: Path) -> Path:
    checker = root / "scripts" / "privacy-check.ps1"
    checker.parent.mkdir(parents=True)
    checker.write_text(
        (repo_root() / "scripts/privacy-check.ps1").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return checker


def _commit_fixture(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=privacy-fixture",
            "-c",
            "user.email=privacy-fixture@example.invalid",
            "commit",
            "-qm",
            message,
        ],
        cwd=root,
        check=True,
    )


def _decode_powershell_output(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # 重定向时 Windows PowerShell 5.1 遵循活动 ANSI 代码页。
        return value.decode("mbcs", errors="replace")


def _run_privacy_fixture(
    root: Path, *arguments: str, console_code_page: int | None = None
) -> subprocess.CompletedProcess[str]:
    executable = _powershell_executable()
    if executable is None:
        if os.name == "nt":
            pytest.fail("PowerShell is required on Windows")
        pytest.skip("PowerShell unavailable on non-Windows")
    command = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(root / "scripts/privacy-check.ps1"),
        *arguments,
    ]
    if console_code_page is not None:
        command = [
            "cmd.exe",
            "/d",
            "/c",
            f"chcp {console_code_page} >NUL && {subprocess.list2cmdline(command)}",
        ]
    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=False,
        timeout=90,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        _decode_powershell_output(completed.stdout),
        _decode_powershell_output(completed.stderr),
    )


def test_privacy_worktree_rejects_oversize_file_before_reading_body(tmp_path: Path):
    root = tmp_path / "privacy-worktree"
    root.mkdir()
    _init_privacy_fixture(root)
    (root / "README.md").write_bytes(b"x" * (20 * 1024 * 1024 + 1))

    result = _run_privacy_fixture(root)

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout
    assert str(root) not in result.stdout + result.stderr


def test_privacy_runtime_suffix_family_rejects_sqlite_wal_file(tmp_path: Path):
    root = tmp_path / "privacy-runtime-family"
    root.mkdir()
    _init_privacy_fixture(root)
    runtime_file = root / "scripts" / "runtime.sqlite-wal"
    runtime_file.write_text("synthetic runtime data", encoding="utf-8")

    result = _run_privacy_fixture(root)

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout


def test_privacy_history_rejects_large_blob_without_materializing_body(tmp_path: Path):
    root = tmp_path / "privacy-history"
    root.mkdir()
    _init_privacy_fixture(root)
    readme = root / "README.md"
    readme.write_text("small\n", encoding="utf-8")
    _commit_fixture(root, "small")
    readme.write_bytes(b"x" * (20 * 1024 * 1024 + 1))
    _commit_fixture(root, "large")
    readme.write_text("small-again\n", encoding="utf-8")
    _commit_fixture(root, "small-again")

    result = _run_privacy_fixture(root, "-History")

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout
    assert "git:" not in result.stdout
    assert str(root) not in result.stdout + result.stderr


@pytest.mark.parametrize("relative", ["docs/ROADMAP.md", "tests/ordinary_test.py", "src/ordinary_source.py"])
def test_privacy_worktree_scans_all_public_nonignored_files(
    tmp_path: Path, relative: str
):
    root = tmp_path / "privacy-all-public"
    root.mkdir()
    _init_privacy_fixture(root)
    public_file = root / relative
    public_file.parent.mkdir(parents=True, exist_ok=True)
    public_file.write_text(
        "".join(("C", ":", chr(92), "private", chr(92), "not-for-release\n")),
        encoding="utf-8",
    )

    result = _run_privacy_fixture(root)

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout
    assert str(root) not in result.stdout + result.stderr


def test_privacy_history_keeps_deleted_public_path_blobs_in_scope(tmp_path: Path):
    root = tmp_path / "privacy-deleted-history"
    root.mkdir()
    _init_privacy_fixture(root)
    historical = root / "docs" / "ROADMAP.md"
    historical.parent.mkdir()
    historical.write_text(
        "".join(("C", ":", chr(92), "private", chr(92), "deleted-history\n")),
        encoding="utf-8",
    )
    _commit_fixture(root, "secret in public roadmap")
    historical.unlink()
    _commit_fixture(root, "delete historical roadmap")

    result = _run_privacy_fixture(root, "-History")

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout
    assert "git:" not in result.stdout
    assert str(root) not in result.stdout + result.stderr


def test_privacy_scans_streams_instead_of_materializing_worktree_or_history():
    privacy = (repo_root() / "scripts/privacy-check.ps1").read_text(encoding="utf-8")
    assert "$listed = @(& git" not in privacy
    assert "$objects = @(& git" not in privacy
    assert "$all = @(Get-ChildItem" not in privacy
    assert "foreach ($path in @(Get-WorkingTreeFiles))" not in privacy


def test_privacy_checker_keeps_utf8_bom_for_windows_powershell_51():
    checker = (repo_root() / "scripts/privacy-check.ps1").read_bytes()
    assert checker.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("history", [False, True])
def test_privacy_powershell_51_handles_realistic_public_file_count(
    tmp_path: Path, history: bool
):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        if os.name == "nt":
            pytest.fail("Windows PowerShell 5.1 is required on Windows")
        pytest.skip("Windows PowerShell 5.1 unavailable on non-Windows")

    root = tmp_path / ("privacy-ps51-history" if history else "privacy-ps51-current")
    root.mkdir()
    _init_privacy_fixture(root)
    for source in tracked_candidate_files(repo_root()):
        relative = source.relative_to(repo_root())
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative != Path("scripts/privacy-check.ps1"):
            target.write_bytes(source.read_bytes())
    if history:
        _commit_fixture(root, "synthetic public snapshot")

    arguments = ("-History",) if history else ()
    result = _run_privacy_fixture(root, *arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    expected = (
        "工作树与 Git 历史隐私检查通过" if history else "隐私检查通过"
    )
    assert result.stdout.strip() == expected
    assert result.stderr == ""
    assert str(root) not in result.stdout + result.stderr


def test_privacy_history_handles_utf8_console_code_page(tmp_path: Path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        if os.name == "nt":
            pytest.fail("Windows PowerShell 5.1 is required on Windows")
        pytest.skip("Windows PowerShell 5.1 unavailable on non-Windows")

    root = tmp_path / "privacy-ps51-utf8-history"
    root.mkdir()
    _init_privacy_fixture(root)
    for source in tracked_candidate_files(repo_root()):
        relative = source.relative_to(repo_root())
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative != Path("scripts/privacy-check.ps1"):
            target.write_bytes(source.read_bytes())
    _commit_fixture(root, "synthetic public snapshot")

    result = _run_privacy_fixture(root, "-History", console_code_page=65001)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "工作树与 Git 历史隐私检查通过"
    assert result.stderr == ""


def test_privacy_unc_requires_server_and_share_components(tmp_path: Path):
    root = tmp_path / "privacy-unc-shape"
    root.mkdir()
    _init_privacy_fixture(root)
    escaped = root / "src" / "escaped.py"
    escaped.parent.mkdir()
    escaped.write_text(
        "".join((chr(92), chr(92), "n", "\n")),
        encoding="utf-8",
    )

    result = _run_privacy_fixture(root)

    assert result.returncode == 0
    assert "PRIVACY_CHECK_VIOLATION" not in result.stdout

    escaped.write_text(
        "".join((chr(92), chr(92), "server", chr(92), "share", "\n")),
        encoding="utf-8",
    )
    result = _run_privacy_fixture(root)

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout
    assert str(root) not in result.stdout + result.stderr


def test_privacy_skips_ignored_tooling_dirs_but_checks_ignored_runtime_files(
    tmp_path: Path,
):
    root = tmp_path / "privacy-ignored-runtime"
    root.mkdir()
    _init_privacy_fixture(root)
    (root / ".gitignore").write_text(
        ".venv/\n__pycache__/\n.pytest_cache/\ntarget/\nignored-runtime/\n",
        encoding="utf-8",
    )
    for directory in (".venv", "__pycache__", ".pytest_cache", "target"):
        tooling = root / directory
        tooling.mkdir()
        (tooling / "cached.pyc").write_bytes(b"\0ignored tooling cache")

    result = _run_privacy_fixture(root)

    assert result.returncode == 0
    assert "PRIVACY_CHECK_VIOLATION" not in result.stdout

    runtime = root / "ignored-runtime"
    runtime.mkdir()
    (runtime / "state.json").write_text("synthetic runtime state", encoding="utf-8")
    result = _run_privacy_fixture(root)

    assert result.returncode == 1
    assert "PRIVACY_CHECK_VIOLATION" in result.stdout
    assert str(root) not in result.stdout + result.stderr


def test_installer_commits_legacy_watcher_disable_after_doctor_and_delayed_trigger():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    install_body = installer[installer.index("function Invoke-Install"):]
    doctor = install_body.index("doctor")
    suspend = install_body.index("Suspend-VerifiedLegacyTasks")
    assert doctor < suspend
    assert install_body.index("Register-NotifierTask") < suspend
    assert "AddMinutes(1)" in installer
    assert "StartBoundary" in installer
    assert "Assert-NotifierTaskTrigger" in installer


def test_installer_resolves_relative_legacy_watcher_from_working_directory():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    matcher = installer[
        installer.index("function Test-LegacyWatcherAction") : installer.index(
            "function Get-LegacyTaskCandidates"
        )
    ]
    assert "IsPathRooted" in matcher
    assert "Join-Path" in matcher
    assert "WorkingDirectory" in matcher
    assert "missing" in matcher.casefold()


def test_uninstaller_requires_task_not_found_and_action_root_match_before_unregister():
    uninstaller = (repo_root() / "scripts/uninstall.ps1").read_text(encoding="utf-8")
    assert "TaskNotFound" in uninstaller
    assert "WorkingDirectory" in uninstaller
    assert "--config" in uninstaller
    assert "--state" in uninstaller
    assert "task-root-mismatch" in uninstaller
    unregister = uninstaller.index("function Unregister-ProductTask")
    body = uninstaller[unregister:]
    assert "catch {\n        return" not in body


def test_installer_and_uninstaller_accept_windows_cim_task_not_found_shape():
    for relative in ("scripts/install.ps1", "scripts/uninstall.ps1"):
        script = (repo_root() / relative).read_text(encoding="utf-8")
        start = script.index("function Test-TaskNotFoundError")
        end = script.index("\nfunction ", start + 1)
        detector = script[start:end]
        assert "FullyQualifiedErrorId" in detector
        assert "CmdletizationQuery_NotFound" in detector


def test_uninstaller_rejects_descendant_reparse_points_before_recursive_delete():
    uninstaller = (repo_root() / "scripts/uninstall.ps1").read_text(encoding="utf-8")
    assert "Assert-NoDescendantReparsePoints" in uninstaller
    assert "Stack[string]" in uninstaller
    remove_app = uninstaller.index("function Remove-ProductApp")
    assert "Assert-NoDescendantReparsePoints" in uninstaller[remove_app:]
    assert uninstaller.index("Assert-NoDescendantReparsePoints", remove_app) < uninstaller.index(
        "Remove-Item -LiteralPath $app -Recurse", remove_app
    )


def test_installer_pauses_owned_task_before_mutation_and_restores_running_state():
    root = repo_root()
    installer = (root / "scripts/install.ps1").read_text(encoding="utf-8")
    install_body = installer[installer.index("function Invoke-Install"):]
    pause = install_body.index("Save-ExistingTaskXml")
    assert pause < install_body.index("Write-JsonAtomic")
    assert pause < install_body.index("Backup-ExistingAppPackage")
    assert pause < install_body.index("Switch-StagedAppPackage")
    assert "Test-NotifierTaskActionBelongsToRoot" in installer
    assert "Start-ScheduledTask" in installer
    assert "Running = Get-TaskRunning" in installer


def test_install_reports_doctor_and_scheduler_diagnostics_without_fake_log():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    uninstaller = (repo_root() / "scripts/uninstall.ps1").read_text(encoding="utf-8")
    assert "notifier.log" not in installer.casefold()
    assert "notifier.log" not in uninstaller.casefold()
    assert "Doctor:" in installer
    assert "Task Scheduler:" in installer
    assert "Get-ScheduledTask" in installer
    assert "local runtime data" in uninstaller


def test_install_cmd_fails_explicitly_when_windows_powershell_is_missing():
    command = (repo_root() / "scripts/install.cmd").read_text(encoding="utf-8")
    assert "where powershell.exe" in command.casefold()
    assert "exit /b 9009" in command.casefold()


def _run_powershell_harness(
    tmp_path: Path, body: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    executable = _powershell_executable()
    if executable is None:
        if os.name == "nt":
            pytest.fail("PowerShell is required on Windows")
        pytest.skip("PowerShell unavailable on non-Windows")
    harness = tmp_path / "isolated-harness.ps1"
    harness.write_text(body, encoding="utf-8")
    return subprocess.run(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            *arguments,
        ],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def test_installer_task_pause_and_rollback_are_verified_with_isolated_cmdlet_mocks(
    tmp_path: Path,
):
    source = repo_root() / "scripts/install.ps1"
    harness = r'''
param([string]$Source)
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
$definitions = [scriptblock]::Create($sourceText.Substring(0, $index))
. $definitions

$root = Join-Path ([IO.Path]::GetTempPath()) ('isolated-product-' + [Guid]::NewGuid().ToString('N'))
$script:InstallRoot = $root
$script:BackupRoot = Join-Path $root 'backup'
New-Item -ItemType Directory -Path (Join-Path $root 'app') -Force | Out-Null
New-Item -ItemType Directory -Path $script:BackupRoot -Force | Out-Null
$config = Join-Path $root 'config.json'
$state = Join-Path $root 'state.json'
$action = [pscustomobject]@{
    Execute = 'python'
    Arguments = '-m zcode_task_notifier run --config "' + $config + '" --state "' + $state + '"'
    WorkingDirectory = Join-Path $root 'app'
}
$script:fakeTask = [pscustomobject]@{
    TaskName = 'ZCodeTaskNotifier'
    TaskPath = '\Owned'
    State = 'Running'
    Actions = @($action)
}
$script:events = New-Object 'System.Collections.Generic.List[string]'
function Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    return $script:fakeTask
}
function Export-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    return '<Task>original</Task>'
}
function Stop-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    [void]$script:events.Add('stop')
    $script:fakeTask.State = 'Ready'
}
function Disable-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    [void]$script:events.Add('disable')
    $script:fakeTask.State = 'Disabled'
}
function Register-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath, [string]$Xml, [switch]$Force, [object]$Action, [object]$Trigger, [object]$Settings, [object]$Principal)
    [void]$script:events.Add('register')
    $script:fakeTask.State = 'Ready'
}
function Enable-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    [void]$script:events.Add('enable')
    $script:fakeTask.State = 'Ready'
}
function Start-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    [void]$script:events.Add('start')
    $script:fakeTask.State = 'Running'
}
function Unregister-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath, [switch]$Confirm)
    [void]$script:events.Add('unregister')
}

$backup = Save-ExistingTaskXml -Directory $script:BackupRoot -Root $root
if (($script:events -join ',') -ne 'stop,disable') { throw ('pause-order:' + ($script:events -join ',')) }
if ([IO.File]::ReadAllText($backup) -ne '<Task>original</Task>') { throw 'backup-content' }
$script:TaskRegistered = $true
$script:RegisteredTaskPath = '\Owned'
Restore-PreviousTask
if (($script:events -join ',') -ne 'stop,disable,unregister,register,enable,start') { throw ('restore-order:' + ($script:events -join ',')) }
if ($script:fakeTask.State -ne 'Running') { throw 'running-state-not-restored' }

$script:events.Clear()
$badAction = [pscustomobject]@{
    Execute = 'python'
    Arguments = '-m zcode_task_notifier run --config "' + (Join-Path ([IO.Path]::GetTempPath()) 'other-config.json') + '" --state "' + $state + '"'
    WorkingDirectory = Join-Path $root 'app'
}
$script:fakeTask = [pscustomobject]@{
    TaskName = 'ZCodeTaskNotifier'
    TaskPath = '\Other'
    State = 'Running'
    Actions = @($badAction)
}
$foreignBackupRoot = Join-Path $root 'foreign-backup'
New-Item -ItemType Directory -Path $foreignBackupRoot -Force | Out-Null
$script:PreviousTask = $null
$script:PreviousTaskXml = $null
$ownershipError = $null
try {
    $null = Save-ExistingTaskXml -Directory $foreignBackupRoot -Root $root
}
catch {
    $ownershipError = $_
}
if ($null -eq $ownershipError) { throw 'ownership-not-checked' }
if ($ownershipError.Exception.Message -ne 'The existing product task does not belong to this installation') {
    throw ('unexpected-ownership-error:' + $ownershipError.Exception.Message)
}
if ($script:events.Count -ne 0) { throw 'foreign-task-mutated' }
if ($script:fakeTask.State -ne 'Running') { throw 'foreign-task-state-changed' }
if ($script:fakeTask.TaskPath -ne '\Other') { throw 'foreign-task-path-changed' }
if (@(Get-ChildItem -LiteralPath $foreignBackupRoot -Force).Count -ne 0) {
    throw 'foreign-task-backup-created'
}
if ($null -ne $script:PreviousTask -or $null -ne $script:PreviousTaskXml) {
    throw 'foreign-task-state-recorded'
}
'PASS'
'''
    result = _run_powershell_harness(tmp_path, harness, str(source))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")


def test_installer_rollback_suspends_current_then_restores_files_and_tasks():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    rollback = installer[installer.index("try {\n    Invoke-Install") :]
    for marker in (
        "Suspend-CurrentNotifierTask",
        "Restore-InstallFiles",
        "Restore-PreviousTask",
        "Restore-LegacyTaskRecords",
    ):
        assert marker in rollback
    assert rollback.index("Suspend-CurrentNotifierTask") < rollback.index(
        "Restore-InstallFiles"
    )
    assert rollback.index("Restore-InstallFiles") < rollback.index(
        "Restore-PreviousTask"
    )
    assert rollback.index("Restore-InstallFiles") < rollback.index(
        "Restore-LegacyTaskRecords"
    )


def test_installer_reports_only_a_fixed_failure_stage_and_exception_type():
    installer = (repo_root() / "scripts/install.ps1").read_text(encoding="utf-8")
    expected_stages = (
        "preflight-python",
        "preflight-weixin",
        "preflight-codex",
        "filesystem-prepare",
        "task-backup",
        "app-backup",
        "config-backup",
        "state-backup",
        "config-write",
        "app-stage",
        "state-migrate",
        "state-baseline",
        "app-switch",
        "task-register",
        "doctor",
        "trigger-verify",
        "legacy-suspend",
        "cleanup",
    )
    for stage in expected_stages:
        assert f'$script:InstallStage = "{stage}"' in installer

    catch_body = installer[installer.index("catch {", installer.index("try {\n    Invoke-Install")) :]
    assert "$failedStage = $script:InstallStage" in catch_body
    assert "stage=" in catch_body
    assert "$_.Exception.GetType().Name" in catch_body
    assert "$_.Exception.Message" not in catch_body
    assert "[Console]::Error.WriteLine" in catch_body
    assert "Write-Error" not in catch_body

    install_body = installer[installer.index("function Invoke-Install") :]
    assert install_body.index('$script:InstallStage = "preflight-python"') < install_body.index(
        "$sourcePackage ="
    )
    assert install_body.index('$script:InstallStage = "preflight-codex"') < install_body.index(
        "$codexEnabled = Select-Codex"
    )


def test_installer_preflight_uses_requested_checks_even_when_doctor_is_unhealthy(
    tmp_path: Path,
):
    source = repo_root() / "scripts/install.ps1"
    harness = r'''
param([string]$Source, [string]$Probe)
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))

function New-ProbeConfig {
    param([bool]$CodexEnabled)
    [IO.File]::WriteAllText($Probe, '{}')
    return $Probe
}
function Invoke-PythonModule {
    param([string]$ModuleRoot, [string[]]$Arguments, [switch]$Capture)
    $payload = @{
        checks = @{
            weixin_target = $true
            codex_discovered = $true
            codex_source = $true
            state_valid = $false
        }
    } | ConvertTo-Json -Compress
    return [pscustomobject]@{ Output = $payload; ExitCode = 2 }
}
if (-not (Test-WeixinTarget -CodexEnabled $true)) { throw 'target-check-failed' }
'PASS'
'''
    probe = tmp_path / "probe.json"
    result = _run_powershell_harness(tmp_path, harness, str(source), str(probe))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")


def test_installer_rollback_suspends_current_task_with_isolated_cmdlet_mocks(
    tmp_path: Path,
):
    source = repo_root() / "scripts/install.ps1"
    harness = r'''
param([string]$Source)
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
$definitions = [scriptblock]::Create($sourceText.Substring(0, $index))
. $definitions

$root = Join-Path ([IO.Path]::GetTempPath()) ('isolated-rollback-' + [Guid]::NewGuid().ToString('N'))
$script:InstallRoot = $root
$script:TaskRegistered = $true
$script:RegisteredTaskPath = '\Owned'
$config = Join-Path $root 'config.json'
$state = Join-Path $root 'state.json'
$action = [pscustomobject]@{
    Execute = 'python'
    Arguments = '-m zcode_task_notifier run --config "' + $config + '" --state "' + $state + '"'
    WorkingDirectory = Join-Path $root 'app'
}
$script:fakeTask = [pscustomobject]@{
    TaskName = 'ZCodeTaskNotifier'
    TaskPath = '\Owned'
    State = 'Running'
    Actions = @($action)
}
$script:events = New-Object 'System.Collections.Generic.List[string]'
function Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    return $script:fakeTask
}
function Stop-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    [void]$script:events.Add('stop')
    $script:fakeTask.State = 'Ready'
}
function Disable-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    [void]$script:events.Add('disable')
    $script:fakeTask.State = 'Disabled'
}
function Unregister-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath, [switch]$Confirm)
    [void]$script:events.Add('unregister')
}

Suspend-CurrentNotifierTask
if (($script:events -join ',') -ne 'stop,disable,unregister') {
    throw ('suspend-order:' + ($script:events -join ','))
}
if ($script:TaskRegistered) { throw 'registration-flag-not-cleared' }
if ($script:fakeTask.State -ne 'Disabled') { throw 'current-task-not-disabled' }
'PASS'
'''
    result = _run_powershell_harness(tmp_path, harness, str(source))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")


def test_uninstaller_safe_app_delete_is_verified_in_isolated_temp_directory(
    tmp_path: Path,
):
    source = repo_root() / "scripts/uninstall.ps1"
    harness = r'''
param([string]$Source)
$sourceText = [IO.File]::ReadAllText($Source)
$marker = [char]10 + 'try {' + [char]10 + '    $root = Get-ProductRoot'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
$definitions = [scriptblock]::Create($sourceText.Substring(0, $index))
. $definitions

$root = Join-Path ([IO.Path]::GetTempPath()) ('isolated-uninstall-' + [Guid]::NewGuid().ToString('N'))
$app = Join-Path $root 'app'
New-Item -ItemType Directory -Path $app -Force | Out-Null
Set-Content -LiteralPath (Join-Path $app 'payload.txt') -Value 'synthetic' -NoNewline
Set-Content -LiteralPath (Join-Path $root 'state.json') -Value 'keep' -NoNewline
Remove-ProductApp -Root $root
if (Test-Path -LiteralPath $app) { throw 'app-not-removed' }
if (-not (Test-Path -LiteralPath (Join-Path $root 'state.json'))) { throw 'local-data-removed' }
'PASS'
'''
    result = _run_powershell_harness(tmp_path, harness, str(source))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")
