@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run_venv

where py >nul 2>&1
if errorlevel 1 goto check_python_command
py -3.12 --version >nul 2>&1
if errorlevel 1 goto check_py311
py -3.12 "scripts\launch_windows.py" web %*
goto finish

:check_py311
py -3.11 --version >nul 2>&1
if errorlevel 1 goto check_python_command
py -3.11 "scripts\launch_windows.py" web %*
goto finish

:check_python_command
where python >nul 2>&1
if errorlevel 1 goto python_missing
python -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
python "scripts\launch_windows.py" web %*
goto finish

:run_venv
".venv\Scripts\python.exe" "scripts\launch_windows.py" web %*

:finish
set "APP_EXIT=%ERRORLEVEL%"
exit /b %APP_EXIT%

:python_missing
echo 64-bit Python 3.11 or 3.12 is required to run this source package.
echo Install 64-bit Python, then double-click this file again.
pause
exit /b 1
