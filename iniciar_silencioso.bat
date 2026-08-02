@echo off
cd /d "%~dp0"

netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if not errorlevel 1 (
    start "" http://localhost:5000
    exit /b
)

start "" http://localhost:5000
python app.py
