@echo off
title Cerrar Clinica Dental
echo Buscando el servidor de Clinica Dental...

set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
    echo Cerrando proceso %%p...
    taskkill /F /PID %%p >nul 2>nul
    set FOUND=1
)

if "%FOUND%"=="1" (
    echo.
    echo Listo, el servidor se cerro correctamente.
) else (
    echo.
    echo No encontre ningun servidor de Clinica Dental corriendo.
)

pause
