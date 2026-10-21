@echo off
rem Saves a backup (database + uploads) into the "backups" folder and keeps the newest 14.
cd /d "%~dp0"
.venv\Scripts\python.exe -m flask --app app backup --out backups --keep 14
