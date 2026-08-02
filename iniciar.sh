#!/bin/bash
cd "$(dirname "$0")"

echo "==============================================="
echo "  Clínica Dental - iniciando servidor local..."
echo "==============================================="
echo

if ! command -v python3 &> /dev/null; then
    echo "No se encontró Python 3 instalado en esta computadora."
    echo "Descárgalo (una sola vez, necesita internet) desde https://www.python.org/downloads/"
    exit 1
fi

if [ ! -f ".deps_ok" ]; then
    echo "Instalando dependencias, esto solo pasa la primera vez..."
    python3 -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "Hubo un error instalando dependencias. Revisa tu conexión a internet."
        exit 1
    fi
    echo ok > .deps_ok
fi

echo
echo "Abre en tu navegador: http://localhost:5000"
( sleep 2 && python3 -c "import webbrowser; webbrowser.open('http://localhost:5000')" ) &
python3 app.py
