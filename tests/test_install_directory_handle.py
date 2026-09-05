"""用真实 Windows 目录句柄回归稳定根目录的包切换与恢复。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 目录共享语义")
def test_package_upgrade_and_rollback_with_real_root_directory_handle(tmp_path: Path) -> None:
    shell = shutil.which("powershell.exe")
    if not shell:
        pytest.skip("需要 Windows PowerShell")
    app = tmp_path / "app"
    old = app / "zcode_task_notifier"
    stage = tmp_path / "stage"
    new = stage / "zcode_task_notifier"
    backup = tmp_path / "backup"
    backup.mkdir()
    for package, payload in ((old, "old-package"), (new, "new-package")):
        package.mkdir(parents=True)
        for marker in ("__init__.py", "cli.py"):
            (package / marker).write_text("", encoding="utf-8")
        (package / "payload.txt").write_text(payload, encoding="utf-8")
    (app / "other.txt").write_text("keep-other-file", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"
    harness = tmp_path / "swap-with-held-directory.ps1"
    harness.write_text(r'''
param([string]$Source, [string]$ProductRoot)
$ErrorActionPreference = 'Stop'
$sourceText = [IO.File]::ReadAllText($Source)
$sourceDir = Split-Path -Parent $Source
$sourceText = $sourceText.Replace('$PSScriptRoot', "'" + $sourceDir.Replace("'", "''") + "'")
$marker = [char]10 + 'try {' + [char]10 + '    Invoke-Install'
$index = $sourceText.LastIndexOf($marker)
if ($index -lt 0) { throw 'definitions-not-found' }
. ([scriptblock]::Create($sourceText.Substring(0, $index)))
$script:InstallRoot = $ProductRoot
$script:BackupRoot = Join-Path $ProductRoot 'backup'
$app = Join-Path $ProductRoot 'app'
Backup-ExistingAppPackage -AppPath $app -Directory $script:BackupRoot
Switch-StagedAppPackage -StageAppPath (Join-Path $ProductRoot 'stage') -AppPath $app
if ([IO.File]::ReadAllText((Join-Path $app 'zcode_task_notifier\payload.txt')) -ne 'new-package') { throw 'new-package-missing' }
Restore-InstallFiles
if ([IO.File]::ReadAllText((Join-Path $app 'zcode_task_notifier\payload.txt')) -ne 'old-package') { throw 'old-package-not-restored' }
'PASS'
''', encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(app), 0x80000000, 3, None, 3, 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with pytest.raises(OSError) as original:
            app.rename(backup / "whole-app")
        assert original.value.winerror == 32
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(source), str(tmp_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip().endswith("PASS")
        assert app.is_dir()
        assert (old / "payload.txt").read_text(encoding="utf-8") == "old-package"
        assert (app / "other.txt").read_text(encoding="utf-8") == "keep-other-file"
    finally:
        kernel.CloseHandle(handle)
