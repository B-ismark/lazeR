@echo off
rem LazeR — double-click to start the server window.
rem Uses pythonw so no black console window appears.
cd /d "%~dp0server"
start "" pythonw remote_server.py
if errorlevel 1 (
  rem pythonw missing? fall back to python (shows a console).
  start "" python remote_server.py
)
