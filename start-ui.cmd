@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Ins Posts Local UI
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto :bad_folder

if not exist "pyproject.toml" goto :not_extracted
if not exist "src\ins_posts\webui\server.py" goto :not_extracted

if defined LOCALAPPDATA (
  set "INS_POSTS_HOME=%LOCALAPPDATA%\InsPosts"
) else (
  set "INS_POSTS_HOME=%TEMP%\InsPosts"
)
set "INS_POSTS_RUNTIME=!INS_POSTS_HOME!\runtime-v0.4.0"
set "INS_POSTS_DATA=!INS_POSTS_HOME!\data\ui-jobs"

if not exist "!INS_POSTS_RUNTIME!\Scripts\python.exe" (
  echo [1/3] Creating a local Python environment for first use...
  where py >nul 2>&1
  if errorlevel 1 (
    where python >nul 2>&1
    if errorlevel 1 goto :no_python
    set "INS_POSTS_PYTHON=python"
  ) else (
    set "INS_POSTS_PYTHON=py -3"
  )
  !INS_POSTS_PYTHON! -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if errorlevel 1 goto :old_python
  !INS_POSTS_PYTHON! -m venv "!INS_POSTS_RUNTIME!"
  if errorlevel 1 goto :venv_failed
) else (
  echo [1/3] Local Python environment found.
)

echo [2/3] Checking dependencies...
"!INS_POSTS_RUNTIME!\Scripts\python.exe" -c "import importlib.metadata as m; import ins_posts.webui.server; assert m.version('ins-posts') == '0.4.1'; assert m.version('gallery-dl') == '1.32.10'" >nul 2>&1
if errorlevel 1 (
  echo       Installing dependencies. The first run needs internet access...
  "!INS_POSTS_RUNTIME!\Scripts\python.exe" -m pip install --upgrade .
  if errorlevel 1 goto :install_failed
  if exist "build" rmdir /s /q "build"
  for /d %%D in ("src\*.egg-info") do if exist "%%~fD" rmdir /s /q "%%~fD"
)

if /i "%~1"=="--check" (
  echo [OK] Launcher, Python, and dependencies are ready.
  goto :success
)

echo [3/3] Opening the browser interface...
echo       Keep this window open. Closing it stops the local service.
"!INS_POSTS_RUNTIME!\Scripts\python.exe" -m ins_posts.webui.server --output-root "!INS_POSTS_DATA!" %*
if errorlevel 1 goto :run_failed
goto :success

:not_extracted
echo.
echo [ERROR] Project files are missing next to this launcher.
echo Do not run the launcher inside the ZIP preview window.
echo Right-click the ZIP, choose "Extract All", then run start-ui.cmd.
goto :failed

:bad_folder
echo.
echo [ERROR] Cannot enter the launcher folder.
goto :failed_no_popd

:no_python
echo.
echo [ERROR] Python was not found.
echo Install Python 3.10 or newer and enable "Add Python to PATH".
goto :failed

:old_python
echo.
echo [ERROR] Python 3.10 or newer is required.
goto :failed

:venv_failed
echo.
echo [ERROR] Could not create the local application runtime.
echo Check disk space and folder permissions for: !INS_POSTS_RUNTIME!
goto :failed

:install_failed
echo.
echo [ERROR] Dependency installation failed. Check the pip error above.
goto :failed

:run_failed
echo.
echo [ERROR] The local web service did not start. Check the error above.
goto :failed

:failed
popd >nul 2>&1
:failed_no_popd
echo.
pause
exit /b 1

:success
popd >nul 2>&1
endlocal
exit /b 0
