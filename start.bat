@echo off
REM ============================================================
REM  EdgeAgent one-click launcher (Windows)
REM  Starts the desktop client: self-check -> config bootstrap ->
REM  local server -> tray icon / window + browser UI.
REM
REM  Options:
REM    start.bat              desktop client (tray / window)
REM    start.bat --headless   server only (no GUI), for scripting
REM    start.bat --check      self-check only, prints JSON
REM ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found. Install Python 3.11+ and add it to PATH.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] failed to create virtual environment.
    pause
    exit /b 1
  )
) else (
  echo [1/3] Virtual environment found.
)

echo [2/3] Installing / verifying dependencies ...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo [ERROR] dependency installation failed. Check your network or pip mirror.
  pause
  exit /b 1
)

echo [3/3] Starting EdgeAgent desktop client ...
echo        tray icon - right click for menu: open panel / logs / quit
".venv\Scripts\python.exe" launcher.py %*

echo.
echo EdgeAgent stopped.
if "%~1"=="" pause
