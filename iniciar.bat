@echo off
title Clinica Dental
cd /d "%~dp0"

echo ===============================================
echo   Clinica Dental - iniciando servidor local...
echo ===============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python instalado en esta PC.
    echo Descargalo -necesita internet, una sola vez- desde https://www.python.org/downloads/
    echo Al instalarlo, marca la casilla "Add Python to PATH".
    pause
    exit /b
)

netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo El servidor ya estaba corriendo. Abriendo tu navegador...
    start "" http://localhost:5000
    echo.
    echo Puedes cerrar esta ventana.
    pause
    exit /b
)

if not exist ".deps_ok" (
    echo Instalando dependencias, esto solo pasa la primera vez...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Hubo un error instalando dependencias. Revisa tu conexion a internet
        echo la primera vez que instales, o pide ayuda.
        pause
        exit /b
    )
    echo ok > .deps_ok
)

echo.
echo Abriendo en tu navegador... si no se abre solo, entra a http://localhost:5000
start "" http://localhost:5000
python app.py

pause
