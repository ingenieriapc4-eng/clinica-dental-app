# Clínica Dental — App local con base de datos y login

Sistema para gestionar pacientes y agenda de citas de una clínica dental.
Corre en tu propia computadora, guarda todo en una base de datos SQLite real
(`clinica.db`) y pide usuario y contraseña para entrar.

## 1. Requisitos

- Tener Python 3.9 o superior instalado (revisa con `python3 --version` o `python --version`).
  Se descarga una sola vez (necesita internet) desde https://www.python.org/downloads/ —
  en Windows, marca la casilla "Add Python to PATH" al instalar.

## 2. Código de activación (solo la primera vez)

La primera vez que se instala, la pantalla de "Configura el administrador" va a
pedir un **código de activación** además del usuario y contraseña. Sin el
código correcto, no se puede crear la cuenta ni usar la app.

- Ese código lo define quien entregó este ZIP (revisa `app.py`, busca
  `ACTIVATION_CODE_HASH` — ahí están las instrucciones de cómo generar uno
  nuevo con `hashlib.sha256(...)` antes de entregarlo a alguien más).
- Si eres tú quien recibió este ZIP de otra persona, pídele el código a quien
  te lo entregó.

## 3. Forma fácil: doble clic

- **Windows**: haz doble clic en `iniciar.bat`.
- **Mac / Linux**: doble clic en `iniciar.sh` (o `bash iniciar.sh` desde la terminal).

La primera vez instalará las dependencias (necesita internet solo esa vez) y abrirá
la app en tu navegador automáticamente. Las siguientes veces arranca directo, sin internet.

## 4. Ícono de escritorio en Windows (sin ventana negra de cmd)

Para no tener que entrar a la carpeta cada vez, crea un acceso directo en el escritorio:

1. Ejecuta `iniciar.bat` **una sola vez** con doble clic (esto instala lo necesario).
2. Ve al archivo `Abrir Clinica Dental.vbs` dentro de esta carpeta.
3. Clic derecho sobre él → **Enviar a** → **Escritorio (crear acceso directo)**.
4. Listo: ahora tienes un ícono en el escritorio. Doble clic ahí y la app arranca
   sola y abre el navegador — sin mostrar ninguna ventana negra de cmd.

Opcional, para que el ícono se vea mejor:
- Clic derecho sobre el acceso directo del escritorio → **Propiedades** → **Cambiar ícono**
  → elige cualquier ícono de diente/salud que tengas, o deja el que Windows asigne por defecto.
- También puedes renombrar el acceso directo a "Clínica Dental".

Si algún día la app no abre o se ve rara, ejecuta `iniciar.bat` directamente (el normal,
no el del ícono) para ver si aparece algún mensaje de error en la ventana negra.

**Si el ícono no abre nada y la ventana se cierra muy rápido**, casi siempre es porque el
servidor ya estaba corriendo desde antes en segundo plano (por ejemplo, de un intento
anterior). Los lanzadores ya detectan esto automáticamente y solo abren el navegador —
pero si aun así no ves nada, entra a `http://localhost:5000` directo en tu navegador,
seguramente ya está funcionando.

Para cerrar el servidor por completo (por ejemplo, antes de apagar la PC si lo iniciaste
con el ícono silencioso), usa **`Cerrar Clinica Dental.bat`**.

## 5. Instalación manual (alternativa)

Abre una terminal dentro de esta carpeta y ejecuta:

```bash
# (opcional pero recomendado) crear un entorno virtual
python3 -m venv venv
source venv/bin/activate      # en Windows: venv\Scripts\activate

# instalar dependencias
pip install -r requirements.txt
```

## 6. Ejecutar el servidor

```bash
python app.py
```

Verás un mensaje como:

```
Clínica Dental corriendo en http://localhost:5000
```

Abre esa dirección en tu navegador (Chrome, Firefox, etc.).

- **La primera vez** te pedirá crear el usuario y contraseña de administrador.
- Las siguientes veces, esa misma pantalla te pedirá iniciar sesión.

## 7. Todo funciona 100% local, sin internet

Esta app no depende de ningún servicio en la nube: el servidor corre en tu propia
computadora (`http://localhost:5000` solo es accesible desde esta PC) y todos los
datos —pacientes, citas, tratamientos, presupuestos, odontogramas, fotos y
documentos— se guardan en archivos dentro de esta misma carpeta:

- `clinica.db` → toda la información (pacientes, citas, tratamientos, etc.)
- `uploads/` → fotos de pacientes, radiografías, documentos y el logo
- `backups/` → copias de seguridad automáticas (ver siguiente sección)

Internet solo hace falta **una vez**, para instalar Python y las dependencias.
Después de eso puedes desconectar el wifi/cable y la app sigue funcionando igual.
Para usarla desde otra computadora de la misma clínica, tendrías que dejar el
servidor corriendo en una PC y que las demás entren por red local — eso ya
implicaría configuración adicional de red, avísame si te interesa.

## 8. Respaldo automático de la información

La app crea una copia de seguridad de la base de datos (y de las fotos/documentos)
todos los días, sin que tengas que hacer nada:

- Se configura desde **Configuración → Respaldo de datos** (solo el administrador
  la ve): ahí eliges la hora del respaldo diario y puedes activarlo/desactivarlo.
- Si un día no dejaste la app abierta a esa hora, se hace un respaldo apenas
  vuelvas a abrirla.
- También puedes presionar **"Respaldar ahora"** en cualquier momento.
- Los respaldos quedan guardados en la carpeta `backups/` de esta misma PC, y
  puedes descargarlos o borrarlos desde esa misma pantalla.
- Se conservan los últimos 60 respaldos automáticamente; los más viejos se
  eliminan solos para no llenar el disco.

**Recomendación importante:** además de estos respaldos automáticos (que están en
la misma computadora), de vez en cuando copia la carpeta `backups/` a un USB, disco
externo o carpeta de Google Drive/Dropbox. Así, si la computadora falla por completo
(se daña el disco, se moja, la roban, etc.), no pierdes la información — un respaldo
que vive solo en la misma PC no protege contra ese caso.


Para detener el servidor, vuelve a la terminal y presiona `Ctrl + C`.
Para volver a usarlo más adelante, repite solo el paso 3 (no hace falta reinstalar nada).

## 9. Dónde quedan tus datos

- Toda la información (pacientes, citas, usuario y contraseña cifrada) se guarda
  en el archivo **`clinica.db`**, que se crea automáticamente en esta misma carpeta.
- Ese archivo es tu base de datos real. Para respaldarla, simplemente copia
  `clinica.db` a otro lugar (USB, nube, etc.) — idealmente hazlo con el servidor apagado.
- Las contraseñas nunca se guardan en texto plano: se almacenan con un hash
  (`werkzeug.security`), que es el estándar recomendado para este tipo de apps.

## 10. Usar la app desde otras computadoras de la clínica (red local)

Por defecto el servidor solo responde en tu propia computadora (`localhost`).
Si quieres que otras computadoras de la misma red (ej. recepción y consultorio)
accedan desde su navegador:

1. Abre `app.py` y cambia la última línea:
   ```python
   app.run(host="127.0.0.1", port=5000, debug=False)
   ```
   por:
   ```python
   app.run(host="0.0.0.0", port=5000, debug=False)
   ```
2. En la computadora donde corre el servidor, busca su dirección IP local
   (ej. `192.168.1.15`).
3. Desde las otras computadoras, entra a `http://192.168.1.15:5000`.

**Importante:** esto solo es seguro dentro de tu red local/privada (ej. el wifi
de la clínica). No expongas este servidor directamente a internet sin agregar
HTTPS y medidas de seguridad adicionales — los datos de pacientes son
información sensible.

## 11. Cuentas de usuario

Este sistema está configurado con **una sola cuenta** de acceso (uso general de
la clínica). Si más adelante necesitas cuentas separadas por persona (ej.
recepción y doctor/a, cada una con su propio usuario), es un cambio sencillo
de agregar — solo pídemelo.

## 12. Funcionalidad incluida

- **Pacientes**: alta, edición, baja, búsqueda, ficha con datos personales,
  alergias, antecedentes médicos, notas y **foto de perfil**.
- **Exámenes y radiografías**: sube imágenes o PDFs a la ficha de cada
  paciente, categorizados como radiografía, examen u otro.
- **Agenda**: vista diaria de citas, con estado (confirmada / pendiente / cancelada).
- **Calendario**: vista mensual con conteo de citas por día.
- **Tratamientos**: registra los tratamientos realizados a cada paciente, con
  costo y fecha.
- **Facturación**: ve el total facturado, cobrado y pendiente, y registra
  pagos contra el saldo de cada tratamiento.
- **Configuración**: cambia el nombre de la clínica, sube tu logo, y ajusta
  el color principal, el color de acento y la tipografía de toda la app —
  todo desde la interfaz, sin tocar código.
- **Seguridad**: pantalla de login, contraseñas con hash, bloqueo temporal tras
  varios intentos fallidos, opción de cambiar contraseña dentro de la app.

## 13. Cómo actualizar una instalación que ya tenías corriendo

Si ya tenías esta app funcionando (por ejemplo en PythonAnywhere) y solo
quieres agregar las funciones nuevas:

1. Reemplaza tu `app.py` y tu carpeta `templates/` por los de esta nueva
   versión (sube los archivos nuevos sobre los viejos).
2. No necesitas borrar `clinica.db` — la base de datos se actualiza sola
   (agrega automáticamente lo necesario para las fotos, la tabla de
   documentos, la de tratamientos y la de configuración) sin perder tus
   pacientes ni citas existentes.
3. Recarga la aplicación (en PythonAnywhere: pestaña **Web** → botón
   **Reload**; en tu PC: vuelve a correr `python app.py`).

Los archivos que subas (fotos, radiografías, logo) se guardan en una carpeta
nueva llamada `uploads/`, dentro de la misma carpeta del proyecto — inclúyela
también en tus respaldos junto con `clinica.db`.
