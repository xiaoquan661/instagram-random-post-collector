@echo off
if not exist "%~dp0start-ui.cmd" (
  echo.
  echo [ERROR] start-ui.cmd is missing.
  echo Extract every file from the ZIP before running this launcher.
  echo.
  pause
  exit /b 1
)
call "%~dp0start-ui.cmd" %*
