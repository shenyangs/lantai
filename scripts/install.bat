@echo off
setlocal

REM Thin cmd.exe wrapper for the PowerShell installer.
set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%install.ps1"

if not exist "%PS_SCRIPT%" (
  echo [lantai] ERROR: install.ps1 not found next to install.bat
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
exit /b %ERRORLEVEL%
