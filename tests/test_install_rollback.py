"""安装器早期失败回滚边界测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _powershell() -> str:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 unavailable")
    return executable


def _run_harness(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    source = _repo_root() / "scripts" / "install.ps1"
    harness = tmp_path / "rollback-harness.ps1"
    harness.write_text(
        "param([string]$Source)\n" + body,
        encoding="utf-8",
        newline="",
    )
    return subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            str(source),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


_LOAD_DEFINITIONS = r'''
$ErrorActionPreference = 'Stop'
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))
'''


def test_missing_completed_backup_reports_failure_and_preserves_current_file(tmp_path: Path) -> None:
    body = _LOAD_DEFINITIONS + r'''
$current = Join-Path $PSScriptRoot 'current.json'
[IO.File]::WriteAllText($current, 'current-content')
$failed = $false
try {
    Restore-FileBackup -Path $current -BackupPath (Join-Path $PSScriptRoot 'missing.json') -Existed $true
}
catch { $failed = $true }
if (-not $failed) { throw 'missing-backup-must-not-claim-success' }
if ([IO.File]::ReadAllText($current) -ne 'current-content') { throw 'current-file-mutated' }
'PASS'
'''
    result = _run_harness(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")


def test_early_task_backup_failures_do_not_delete_unowned_files(tmp_path: Path) -> None:
    body = _LOAD_DEFINITIONS + r'''
$scenarios = @(
    'task-backup-failure',
    'config-backup-failure',
    'state-backup-failure'
)
foreach ($scenario in $scenarios) {
    $root = Join-Path ([IO.Path]::GetTempPath()) ('rollback-early-' + [Guid]::NewGuid().ToString('N'))
    $backup = Join-Path $root 'backup'
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    $config = Join-Path $root 'config.json'
    $state = Join-Path $root 'state.json'
    [IO.File]::WriteAllText($config, 'old-config')
    [IO.File]::WriteAllText($state, 'old-state')
    $script:InstallRoot = $root
    $script:BackupRoot = $backup
    $script:AppBackupPath = $null
    $script:ConfigBackup = $null
    $script:StateBackup = $null
    $script:ConfigExisted = $false
    $script:StateExisted = $false
    $script:ConfigBackupReady = $false
    $script:StateBackupReady = $false
    if ($scenario -eq 'config-backup-failure') {
        $script:ConfigExisted = $true
        $script:ConfigBackup = Join-Path $backup 'config.json'
        [IO.File]::WriteAllText($script:ConfigBackup, 'old-config')
        $script:ConfigBackupReady = $true
    }
    elseif ($scenario -eq 'state-backup-failure') {
        $script:StateExisted = $true
        $script:StateBackup = Join-Path $backup 'state.json'
        [IO.File]::WriteAllText($script:StateBackup, 'old-state')
        $script:StateBackupReady = $true
    }
    Restore-InstallFiles
    if ([IO.File]::ReadAllText($config) -ne 'old-config') { throw ($scenario + ':config-mutated') }
    if ([IO.File]::ReadAllText($state) -ne 'old-state') { throw ($scenario + ':state-mutated') }
    Remove-Item -LiteralPath $root -Recurse -Force
}
'PASS'
'''
    result = _run_harness(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")


def test_rollback_restores_only_files_reached_by_the_recovery_checkpoint(
    tmp_path: Path,
) -> None:
    body = _LOAD_DEFINITIONS + r'''
$root = Join-Path ([IO.Path]::GetTempPath()) ('rollback-ready-' + [Guid]::NewGuid().ToString('N'))
$backup = Join-Path $root 'backup'
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$config = Join-Path $root 'config.json'
$state = Join-Path $root 'state.json'
$configBackup = Join-Path $backup 'config.json'
$stateBackup = Join-Path $backup 'state.json'
[IO.File]::WriteAllText($configBackup, 'old-config')
[IO.File]::WriteAllText($stateBackup, 'old-state')
[IO.File]::WriteAllText($config, 'new-config')
[IO.File]::WriteAllText($state, 'new-state')
$script:InstallRoot = $root
$script:BackupRoot = $backup
$script:AppBackupPath = $null
$script:ConfigBackup = $configBackup
$script:StateBackup = $stateBackup
$script:ConfigExisted = $true
$script:StateExisted = $true
$script:ConfigBackupReady = $true
$script:StateBackupReady = $true
Restore-InstallFiles
if ([IO.File]::ReadAllText($config) -ne 'old-config') { throw 'overwrite-config-not-restored' }
if ([IO.File]::ReadAllText($state) -ne 'old-state') { throw 'overwrite-state-not-restored' }

$absentRoot = Join-Path ([IO.Path]::GetTempPath()) ('rollback-absent-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $absentRoot -Force | Out-Null
$absentConfig = Join-Path $absentRoot 'config.json'
$absentState = Join-Path $absentRoot 'state.json'
[IO.File]::WriteAllText($absentConfig, 'new-config')
[IO.File]::WriteAllText($absentState, 'new-state')
$script:InstallRoot = $absentRoot
$script:BackupRoot = Join-Path $absentRoot 'backup'
$script:AppBackupPath = $null
$script:ConfigBackup = $null
$script:StateBackup = $null
$script:ConfigExisted = $false
$script:StateExisted = $false
$script:ConfigBackupReady = $true
$script:StateBackupReady = $true
Restore-InstallFiles
if (Test-Path -LiteralPath $absentConfig) { throw 'new-config-not-removed' }
if (Test-Path -LiteralPath $absentState) { throw 'new-state-not-removed' }
Remove-Item -LiteralPath $root -Recurse -Force
Remove-Item -LiteralPath $absentRoot -Recurse -Force
'PASS'
'''
    result = _run_harness(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")
