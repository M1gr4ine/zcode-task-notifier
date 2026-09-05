"""安装器 Python 预检的原生 stderr 回归测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _powershell() -> str:
    for candidate in ("powershell.exe", "pwsh"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    pytest.skip("PowerShell unavailable on non-Windows")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_invoke_python_module_capture_keeps_native_stderr_as_data(
    tmp_path: Path, exit_code: int
) -> None:
    installer = _repo_root() / "scripts" / "install.ps1"
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_text(
        "@echo off\r\n"
        "echo {\"checks\":{\"weixin_target\":true}}\r\n"
        "echo codex state warning: synthetic>&2\r\n"
        f"exit /b {exit_code}\r\n",
        encoding="ascii",
        newline="",
    )
    harness = tmp_path / "invoke-python.ps1"
    harness.write_text(
        r'''
param([string]$Source, [string]$FakePython, [int]$ExpectedExitCode)
$ErrorActionPreference = 'Stop'
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))
$tempRoot = [IO.Path]::GetTempPath()
$beforeStdout = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'zcode-task-notifier-stdout-*.tmp' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
$beforeStderr = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'zcode-task-notifier-stderr-*.tmp' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
$env:PYTHONPATH = 'sentinel-python-path'
$script:Python = $FakePython
$result = Invoke-PythonModule -ModuleRoot (Split-Path -Parent $Source) -Arguments @('--synthetic') -Capture
if ($result.ExitCode -ne $ExpectedExitCode) { throw ('exit-code:' + $result.ExitCode) }
$payload = $result.Output | ConvertFrom-Json
if (-not [bool]$payload.checks.weixin_target) { throw 'stdout-not-captured' }
if ([string]$result.ErrorOutput -notmatch 'codex state warning: synthetic') { throw 'stderr-not-captured' }
if ($env:PYTHONPATH -ne 'sentinel-python-path') { throw 'python-path-not-restored' }
if ($ErrorActionPreference -ne 'Stop') { throw 'error-action-not-restored' }
$afterStdout = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'zcode-task-notifier-stdout-*.tmp' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
$afterStderr = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'zcode-task-notifier-stderr-*.tmp' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
if (@(Compare-Object $beforeStdout $afterStdout).Count -ne 0) { throw 'stdout-temp-not-cleaned' }
if (@(Compare-Object $beforeStderr $afterStderr).Count -ne 0) { throw 'stderr-temp-not-cleaned' }
'PASS'
''',
        encoding="utf-8",
        newline="",
    )
    result = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            str(installer),
            str(fake_python),
            str(exit_code),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")
    assert result.stderr == ""


def test_invoke_python_module_non_capture_allows_warning_stderr(tmp_path: Path) -> None:
    installer = _repo_root() / "scripts" / "install.ps1"
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_text(
        "@echo off\r\n"
        "echo baseline-stdout\r\n"
        "echo codex state warning: synthetic>&2\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
        newline="",
    )
    harness = tmp_path / "invoke-python-non-capture.ps1"
    harness.write_text(
        r'''
param([string]$Source, [string]$FakePython)
$ErrorActionPreference = 'Stop'
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))
$tempRoot = [IO.Path]::GetTempPath()
$beforeStderr = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'zcode-task-notifier-stderr-*.tmp' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
$env:PYTHONPATH = 'sentinel-python-path'
$script:Python = $FakePython
Invoke-PythonModule -ModuleRoot (Split-Path -Parent $Source) -Arguments @('--synthetic')
if ($env:PYTHONPATH -ne 'sentinel-python-path') { throw 'python-path-not-restored' }
if ($ErrorActionPreference -ne 'Stop') { throw 'error-action-not-restored' }
$afterStderr = @(Get-ChildItem -LiteralPath $tempRoot -Filter 'zcode-task-notifier-stderr-*.tmp' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
if (@(Compare-Object $beforeStderr $afterStderr).Count -ne 0) { throw 'stderr-temp-not-cleaned' }
'PASS'
''',
        encoding="utf-8",
        newline="",
    )
    result = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            str(installer),
            str(fake_python),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "baseline-stdout" in result.stdout
    assert result.stdout.strip().endswith("PASS")
    assert result.stderr == ""


def test_invoke_python_module_rejects_launch_failure_instead_of_stale_exit_code(
    tmp_path: Path,
) -> None:
    installer = _repo_root() / "scripts" / "install.ps1"
    harness = tmp_path / "invoke-python-launch-failure.ps1"
    harness.write_text(
        r'''
param([string]$Source)
$ErrorActionPreference = 'Stop'
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))
$script:Python = Join-Path $env:TEMP ('zcode-missing-python-' + [Guid]::NewGuid().ToString('N') + '.cmd')
$global:LASTEXITCODE = 7
try {
    $null = Invoke-PythonModule -ModuleRoot (Split-Path -Parent $Source) -Arguments @('--synthetic') -Capture
    throw 'launch-failure-not-rejected'
}
catch {
    if ($_.Exception.Message -notmatch 'could not be started') { throw }
}
'PASS'
''',
        encoding="utf-8",
        newline="",
    )
    result = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            str(installer),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")
