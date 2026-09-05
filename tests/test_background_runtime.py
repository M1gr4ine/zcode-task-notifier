from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys

import pytest

from test_service import IntegratedFixture


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pythonw() -> Path:
    if os.name != "nt":
        pytest.skip("pythonw.exe only exists on Windows")
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if not candidate.is_file():
        pytest.skip("the selected Python runtime has no pythonw.exe")
    return candidate


def _assert_gui_subsystem(executable: Path) -> None:
    payload = executable.read_bytes()
    pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
    assert payload[pe_offset : pe_offset + 4] == b"PE\0\0"
    optional_header = pe_offset + 24
    magic = struct.unpack_from("<H", payload, optional_header)[0]
    assert magic in {0x10B, 0x20B}
    subsystem = struct.unpack_from("<H", payload, optional_header + 68)[0]
    assert subsystem == 2


def _run_pythonw(
    executable: Path,
    root: Path,
    config_path: Path,
    state_path: Path,
    command: str,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_repo_root() / "src")
    environment["PYTHONNOUSERSITE"] = "1"
    # 只从合成配置和状态读取；cwd 也固定在仓库外的 pytest 临时目录。
    return subprocess.run(
        [
            str(executable),
            "-m",
            "zcode_task_notifier",
            command,
            "--config",
            str(config_path),
            "--state",
            str(state_path),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=False,
        timeout=30,
    )


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_pythonw_completes_baseline_and_run_without_console(tmp_path: Path):
    executable = _pythonw()
    _assert_gui_subsystem(executable)
    fixture = IntegratedFixture.create(tmp_path)

    baseline = _run_pythonw(
        executable,
        tmp_path,
        fixture.config_path,
        fixture.state_path,
        "baseline",
    )
    assert baseline.returncode == 0
    baseline_state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    assert baseline_state["initialized"] is True
    assert baseline_state["source_initialized"]["zcode"] is True
    assert baseline_state["outbox"] == {}

    fixture.complete_zcode_task("windowless-session")
    run = _run_pythonw(
        executable,
        tmp_path,
        fixture.config_path,
        fixture.state_path,
        "run",
    )
    assert run.returncode == 0
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    assert len(state["outbox"]) == 1
    item = next(iter(state["outbox"].values()))
    assert item["status"] == "submitted"
    assert item["automation_id"].startswith("automation-tnotify-")
    assert item["event"]["task_id"] == "windowless-session"
    assert item["event"]["key"] in state["seen_event_keys"]

    connection = sqlite3.connect(fixture.zcode_db)
    try:
        rows = connection.execute(
            "SELECT title, workspace_key, location_kind, recurring, lifecycle_status "
            "FROM automations"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("[zcode] 合成 ZCode 任务", "workspace-example", "local", 0, "active")]


def _run_powershell_harness(
    tmp_path: Path, body: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        pytest.skip("PowerShell harness is Windows-only")
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        pytest.fail("PowerShell is required on Windows")
    harness = tmp_path / "windowless-action-harness.ps1"
    harness.write_text(body, encoding="utf-8", newline="\n")
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
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def test_installer_selects_windowless_action_but_keeps_console_python(tmp_path: Path):
    source = _repo_root() / "scripts" / "install.ps1"
    console_python = sys.executable
    harness = r'''
param([string]$Source, [string]$ConsolePython)
$ErrorActionPreference = "Stop"
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))

$script:Python = $ConsolePython
$script:WindowlessPython = Find-WindowlessPython -ConsolePython $script:Python
if ([IO.Path]::GetFileName($script:WindowlessPython) -ine 'pythonw.exe') {
    throw 'windowless-action-not-selected'
}
if (-not ([IO.Path]::GetFullPath($script:Python)).Equals(
        [IO.Path]::GetFullPath($ConsolePython),
        [StringComparison]::OrdinalIgnoreCase)) {
    throw 'console-python-was-replaced'
}
if ([IO.Path]::GetFileName($script:Python) -ine 'python.exe') {
    throw 'diagnostic-python-is-not-console-runtime'
}

function New-ScheduledTaskAction {
    [CmdletBinding()]
    param(
        [string]$Execute,
        [string]$Argument,
        [string]$WorkingDirectory
    )
    return [pscustomobject]@{
        Execute = $Execute
        Arguments = $Argument
        WorkingDirectory = $WorkingDirectory
    }
}
$action = New-NotifierTaskAction -AppPath (Join-Path $pwd 'app') -ConfigPath (Join-Path $pwd 'config.json') -StatePath (Join-Path $pwd 'state.json')
if ([IO.Path]::GetFileName($action.Execute) -ine 'pythonw.exe') {
    throw 'action-executable-is-not-windowless'
}
if ($action.Arguments -notmatch '(?i)-m zcode_task_notifier run') {
    throw 'action-module-is-missing'
}
'PASS'
'''
    result = _run_powershell_harness(tmp_path, harness, str(source), console_python)
    assert result.returncode == 0
    assert result.stdout.strip().endswith("PASS")
