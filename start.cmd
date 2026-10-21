@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запускаю слежение за Авито...
echo Остановить: Ctrl+C
".venv\Scripts\python.exe" -u avito_watch.py
pause
