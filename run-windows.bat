@echo off
rem Avtomontazh: server + worker. Closing this window stops the program.
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m server.app
pause
