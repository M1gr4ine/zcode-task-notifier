from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _powershell_executable() -> str:
    executable = shutil.which("powershell.exe")
    if executable is None:
        if os.name == "nt":
            pytest.fail("Windows PowerShell 5.1 is required")
        pytest.skip("Windows PowerShell 5.1 unavailable")
    return executable


def _run_parser_entrypoint(
    tmp_path: Path, paths: list[Path], *, code_page: int
) -> subprocess.CompletedProcess[bytes]:
    harness = tmp_path / "parse-public-ps1.ps1"
    harness.write_text(
        """param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Paths
)
$failed = $false
$parsedCount = 0
foreach ($path in $Paths) {
    $parsedCount += 1
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $path, [ref]$tokens, [ref]$errors
    )
    if ($errors.Count -ne 0) {
        $failed = $true
        foreach ($errorRecord in $errors) {
            Write-Output ($errorRecord.ErrorId + ':' + $errorRecord.Extent.StartLineNumber)
        }
    }
}
Write-Output ('PARSED_COUNT:' + $parsedCount)
if ($failed) {
    exit 1
}
""",
        encoding="ascii",
        newline="\n",
    )
    powershell = _powershell_executable()
    command = [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(harness),
        *map(str, paths),
    ]
    wrapped = [
        "cmd.exe",
        "/d",
        "/c",
        f"chcp {code_page} >NUL && {subprocess.list2cmdline(command)}",
    ]
    return subprocess.run(
        wrapped,
        cwd=tmp_path,
        capture_output=True,
        text=False,
        timeout=30,
    )


@pytest.mark.parametrize("code_page", [936, 65001])
def test_public_powershell_files_parse_from_real_file_entrypoint(
    tmp_path: Path, code_page: int
):
    synthetic = tmp_path / "合成路径" / "entry.ps1"
    synthetic.parent.mkdir()
    synthetic.write_text(
        "throw 'target body must not execute'\n",
        encoding="utf-8-sig",
        newline="\n",
    )
    public_scripts = sorted((_repo_root() / "scripts").glob("*.ps1"))

    result = _run_parser_entrypoint(
        tmp_path,
        [*public_scripts, synthetic],
        code_page=code_page,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PARSED_COUNT:{len(public_scripts) + 1}".encode() in result.stdout


@pytest.mark.parametrize("code_page", [936, 65001])
def test_parser_reports_late_bad_syntax_without_executing_target_body(
    tmp_path: Path, code_page: int
):
    valid = tmp_path / "valid.ps1"
    valid.write_text(
        "throw 'valid target body must not execute'\n",
        encoding="utf-8-sig",
        newline="\n",
    )
    bad = tmp_path / "bad.ps1"
    bad.write_text(
        "throw 'bad target body must not execute'\nif (\n",
        encoding="utf-8-sig",
        newline="\n",
    )
    public_scripts = sorted((_repo_root() / "scripts").glob("*.ps1"))
    paths = [public_scripts[0], bad, *public_scripts[1:], valid]

    result = _run_parser_entrypoint(tmp_path, paths, code_page=code_page)

    assert result.returncode == 1
    assert f"PARSED_COUNT:{len(paths)}".encode() in result.stdout
    assert b"bad target body" not in result.stdout + result.stderr
