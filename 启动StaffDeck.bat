@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "REPO_DIR=%~dp0"
set "VENV_PYTHON=%REPO_DIR%backend\.venv\Scripts\python.exe"
set "NODE_DIR="
set "STAFFDECK_NODE="
set "STAFFDECK_NPM="
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%POWERSHELL%" set "POWERSHELL=powershell.exe"

if not exist "%REPO_DIR%scripts\dev_up.ps1" (
  echo [ERROR] scripts\dev_up.ps1 was not found.
  echo Please run this file from the StaffDeck project directory.
  pause
  exit /b 1
)

if not exist "%VENV_PYTHON%" (
  echo [ERROR] backend\.venv is missing.
  echo Please create backend\.venv with Python 3.11 or newer first.
  pause
  exit /b 1
)

if not exist "%REPO_DIR%backend\.env" (
  echo [ERROR] backend\.env is missing.
  echo Copy backend\.env.example to backend\.env and configure the model settings first.
  pause
  exit /b 1
)

"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] backend\.venv must use Python 3.11 or newer.
  "%VENV_PYTHON%" --version
  pause
  exit /b 1
)

rem Locate a real system-installed Node.js runtime.
for /f "delims=" %%P in ('where node.exe 2^>nul') do if not defined NODE_DIR set "NODE_DIR=%%~dpP"
if not defined NODE_DIR if exist "%ProgramFiles%\nodejs\node.exe" set "NODE_DIR=%ProgramFiles%\nodejs\"
if not defined NODE_DIR if exist "%ProgramFiles(x86)%\nodejs\node.exe" set "NODE_DIR=%ProgramFiles(x86)%\nodejs\"
if not defined NODE_DIR if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" set "NODE_DIR=%LOCALAPPDATA%\Programs\nodejs\"
if not defined NODE_DIR if defined NVM_SYMLINK if exist "%NVM_SYMLINK%\node.exe" set "NODE_DIR=%NVM_SYMLINK%\"
if not defined NODE_DIR if exist "C:\nvm4w\nodejs\node.exe" set "NODE_DIR=C:\nvm4w\nodejs\"

if not defined NODE_DIR (
  echo [ERROR] Node.js 20 or newer was not found.
  echo Install Node.js and reopen PowerShell or Windows Terminal before retrying.
  pause
  exit /b 1
)

set "PATH=!NODE_DIR!;!PATH!"
set "STAFFDECK_NODE=!NODE_DIR!node.exe"
for /f "delims=" %%V in ('node.exe --version 2^>nul') do set "NODE_VERSION=%%V"
for /f "tokens=1 delims=." %%V in ("!NODE_VERSION:~1!") do set "NODE_MAJOR=%%V"
if not defined NODE_MAJOR (
  echo [ERROR] Unable to determine the Node.js version.
  pause
  exit /b 1
)
if !NODE_MAJOR! LSS 20 (
  echo [ERROR] Node.js 20 or newer is required. Found !NODE_VERSION!.
  pause
  exit /b 1
)
set "NPM_CMD="
for /f "delims=" %%P in ('where npm.cmd 2^>nul') do if not defined NPM_CMD set "NPM_CMD=%%~fP"
if not defined NPM_CMD (
  echo [ERROR] npm.cmd was not found with Node.js.
  echo Repair or reinstall Node.js 20 or newer, then retry.
  pause
  exit /b 1
)
set "STAFFDECK_NPM=!NPM_CMD!"

echo Starting StaffDeck with the documented Windows command...
"%POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%REPO_DIR%scripts\dev_up.ps1" --detach
if errorlevel 1 goto :start_failed

echo Waiting for StaffDeck health check...
set "PORT=5173"
for /l %%N in (1,1,45) do (
  if exist "%REPO_DIR%.dev\app.port" set /p PORT=<"%REPO_DIR%.dev\app.port"
  curl.exe --fail --silent --max-time 2 "http://127.0.0.1:!PORT!/api/health" >nul 2>&1
  if not errorlevel 1 goto :ready
  timeout /t 1 /nobreak >nul
)

echo [ERROR] StaffDeck did not become healthy within 45 seconds.
echo Check logs in "%REPO_DIR%.dev\logs"
pause
exit /b 1

:start_failed
echo [ERROR] StaffDeck failed to start.
echo Check logs in "%REPO_DIR%.dev\logs"
pause
exit /b 1

:ready
echo StaffDeck is ready at http://127.0.0.1:!PORT!
start "" "http://127.0.0.1:!PORT!/workspace/gallery"
exit /b 0
