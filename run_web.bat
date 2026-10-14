@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Create the virtual environment first: py -3.11 -m venv .venv
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m fluke8845a.webapp
