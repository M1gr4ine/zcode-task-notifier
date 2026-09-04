@echo off
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo PowerShell is required on Windows.
  exit /b 9009
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
