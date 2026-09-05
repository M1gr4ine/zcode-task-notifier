"""安装器稳定 app 根目录与产品包切换测试。"""

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
    harness = tmp_path / "app-swap-harness.ps1"
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


def test_package_swap_keeps_locked_app_root_and_restores_old_package(
    tmp_path: Path,
) -> None:
    body = _LOAD_DEFINITIONS + r'''
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NativeDirectoryLock {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateFileW(
        string path, uint desiredAccess, uint shareMode, IntPtr security,
        uint creationDisposition, uint flags, IntPtr templateHandle);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr handle);
}
'@
function Write-ProductPackage {
    param([string]$Path, [string]$Payload)
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $Path '__init__.py'), '')
    [IO.File]::WriteAllText((Join-Path $Path 'cli.py'), '')
    [IO.File]::WriteAllText((Join-Path $Path 'payload.txt'), $Payload)
}
$root = Join-Path ([IO.Path]::GetTempPath()) ('app-swap-' + [Guid]::NewGuid().ToString('N'))
$app = Join-Path $root 'app'
$oldPackage = Join-Path $app 'zcode_task_notifier'
$backup = Join-Path $root 'backup'
$stageApp = Join-Path $root 'stage-app'
$stagePackage = Join-Path $stageApp 'zcode_task_notifier'
New-Item -ItemType Directory -Path $app -Force | Out-Null
New-Item -ItemType Directory -Path $backup -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $app 'other.txt'), 'must-remain')
Write-ProductPackage -Path $oldPackage -Payload 'old-package'
$directoryHandle = [NativeDirectoryLock]::CreateFileW(
    $app, [uint32]2147483648, [uint32]3, [IntPtr]::Zero,
    [uint32]3, [uint32]33554432, [IntPtr]::Zero
)
if ($directoryHandle -eq [IntPtr](-1)) { throw 'directory-lock-not-created' }
try {
    $rootMoveFailed = $false
    try {
        Move-Item -LiteralPath $app -Destination ($root + '-whole-backup') -Force -ErrorAction Stop
    }
    catch {
        $rootMoveFailed = $true
        if (([int64]$_.Exception.HResult -ne -2147024864) -and
            -not ($_.Exception -is [IO.IOException])) {
            throw
        }
    }
    if (-not $rootMoveFailed) { throw 'whole-app-rename-was-not-blocked' }
    Backup-ExistingAppPackage -AppPath $app -Directory $backup
    if (-not (Test-Path -LiteralPath $app -PathType Container)) { throw 'app-root-moved' }
    if (-not (Test-Path -LiteralPath (Join-Path $app 'other.txt') -PathType Leaf)) { throw 'other-file-removed' }
    if (Test-Path -LiteralPath $oldPackage) { throw 'old-package-not-backed-up' }
    if (-not (Test-Path -LiteralPath $script:AppBackupPath -PathType Container)) { throw 'package-backup-missing' }
    Write-ProductPackage -Path $stagePackage -Payload 'new-package'
    Switch-StagedAppPackage -StageAppPath $stageApp -AppPath $app
    if ([IO.File]::ReadAllText((Join-Path $app 'zcode_task_notifier\payload.txt')) -ne 'new-package') { throw 'new-package-not-installed' }
    if ([IO.File]::ReadAllText((Join-Path $app 'other.txt')) -ne 'must-remain') { throw 'other-file-changed' }
    $script:InstallRoot = $root
    $script:BackupRoot = $backup
    Restore-InstallFiles
    if ([IO.File]::ReadAllText((Join-Path $app 'zcode_task_notifier\payload.txt')) -ne 'old-package') { throw 'old-package-not-restored' }
    if ([IO.File]::ReadAllText((Join-Path $app 'other.txt')) -ne 'must-remain') { throw 'other-file-lost-on-rollback' }
    if (-not (Test-Path -LiteralPath $app -PathType Container)) { throw 'app-root-lost-on-rollback' }
}
finally {
    [void][NativeDirectoryLock]::CloseHandle($directoryHandle)
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
'PASS'
'''
    result = _run_harness(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")


def test_package_swap_rejects_unknown_or_missing_targets(tmp_path: Path) -> None:
    body = _LOAD_DEFINITIONS + r'''
$root = Join-Path ([IO.Path]::GetTempPath()) ('app-swap-reject-' + [Guid]::NewGuid().ToString('N'))
$app = Join-Path $root 'app'
$backup = Join-Path $root 'backup'
$stageApp = Join-Path $root 'stage-app'
New-Item -ItemType Directory -Path $app -Force | Out-Null
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$unknown = Join-Path $app 'zcode_task_notifier'
New-Item -ItemType Directory -Path $unknown -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $unknown 'unknown.txt'), 'foreign')
$caught = $false
try { Backup-ExistingAppPackage -AppPath $app -Directory $backup }
catch { $caught = $true }
if (-not $caught) { throw 'unknown-package-accepted' }
if ([IO.File]::ReadAllText((Join-Path $unknown 'unknown.txt')) -ne 'foreign') { throw 'unknown-package-mutated' }

$emptyStage = Join-Path $root 'empty-stage'
New-Item -ItemType Directory -Path $emptyStage -Force | Out-Null
$caught = $false
try { Switch-StagedAppPackage -StageAppPath $emptyStage -AppPath $app }
catch { $caught = $true }
if (-not $caught) { throw 'missing-stage-package-accepted' }
if ([IO.File]::ReadAllText((Join-Path $unknown 'unknown.txt')) -ne 'foreign') { throw 'missing-stage-mutated-target' }
Remove-Item -LiteralPath $root -Recurse -Force
'PASS'
'''
    result = _run_harness(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("PASS")
