"""
Clínica Dental — servidor local con base de datos real (SQLite) y login.

Cómo correr:
    pip install -r requirements.txt
    python app.py

Luego abre http://localhost:5000 en tu navegador.
La primera vez te pedirá crear el usuario administrador.
"""

import hashlib
import os
import re
import shutil
import smtplib
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

from flask import (Flask, g, jsonify, redirect, render_template, request,
                    send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "clinica.db")
SECRET_KEY_PATH = os.path.join(BASE_DIR, ".secret_key")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
PHOTOS_DIR = os.path.join(UPLOAD_DIR, "photos")
DOCS_DIR = os.path.join(UPLOAD_DIR, "documents")
BRANDING_DIR = os.path.join(UPLOAD_DIR, "branding")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
MAX_BACKUPS = 60
for d in (UPLOAD_DIR, PHOTOS_DIR, DOCS_DIR, BRANDING_DIR, BACKUP_DIR):
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------------------
# Llave secreta persistente (se genera una sola vez, no se debe compartir)
# ---------------------------------------------------------------------------
if os.path.exists(SECRET_KEY_PATH):
    with open(SECRET_KEY_PATH, "r") as f:
        SECRET_KEY = f.read().strip()
else:
    SECRET_KEY = uuid.uuid4().hex + uuid.uuid4().hex
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(SECRET_KEY)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB por archivo subido
app.permanent_session_lifetime = timedelta(hours=12)

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
DOCUMENT_EXTS = IMAGE_EXTS | {"pdf"}


def _ext_ok(filename, allowed):
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in allowed


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Código de activación: se pide una sola vez, en la pantalla de "Configura el
# administrador" (primera instalación). Sin el código correcto, no se puede
# crear la cuenta de administrador ni usar la app.
#
# PARA CAMBIAR EL CÓDIGO (hazlo tú, antes de entregar el ZIP a alguien más):
# 1. Elige tu propio código, por ejemplo "ClinicaGarcia2026"
# 2. En una terminal, genera su hash con:
#      python3 -c "import hashlib; print(hashlib.sha256('TU_CODIGO_AQUI'.encode()).hexdigest())"
# 3. Copia el resultado (una cadena larga de letras y números) y pégalo abajo,
#    reemplazando el valor de ACTIVATION_CODE_HASH.
# 4. Guarda app.py. La próxima persona que instale este ZIP necesitará saber
#    "TU_CODIGO_AQUI" para poder crear el administrador.
#
# El código de ejemplo que viene puesto ahora es: ClinicaDemo2026
ACTIVATION_CODE_HASH = "626286af113f9d26817985694fa4c15927338e14488ac11ea593d396c214e611"


def _valid_activation_code(code):
    return hashlib.sha256((code or "").strip().encode()).hexdigest() == ACTIVATION_CODE_HASH


DEFAULT_SETTINGS = {
    "clinic_name": "Clínica Dental",
    "doctor_name": "",
    "logo_path": "",
    "primary_color": "#2F6F62",
    "accent_color": "#E1734F",
    "bg_color": "#FAF8F4",
    "font_family": "Space Grotesk",
    "font_size": "15",
    "welcome_message": "",
    "backup_hour": "21:00",
    "backup_enabled": "1",
    "backup_folder": "",
}

# Claves de configuración de correo: nunca se exponen por /api/settings (endpoint general),
# solo por /api/settings/email, que es admin-only y no revela la contraseña guardada.
EMAIL_SETTINGS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_use_tls": "1",
    "smtp_from_name": "",
}

# Claves de sincronización con una versión en línea (PythonAnywhere, etc.): tampoco
# se exponen por /api/settings, solo por /api/settings/sync (admin-only, sin revelar contraseña).
SYNC_SETTINGS = {
    "sync_remote_url": "",
    "sync_remote_username": "",
    "sync_remote_password": "",
}

VIEW_KEYS = ["pacientes", "agenda", "calendario", "tratamientos", "odontograma", "presupuestos", "facturacion"]



def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            permissions TEXT NOT NULL DEFAULT '',
            display_name TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS patients (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            dob TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            allergies TEXT,
            history TEXT,
            notes TEXT,
            photo TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS appointments (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration INTEGER DEFAULT 30,
            status TEXT DEFAULT 'pendiente',
            reason TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            category TEXT DEFAULT 'otro',
            uploaded_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS charges (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            treatment TEXT NOT NULL,
            cost REAL NOT NULL DEFAULT 0,
            paid REAL NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS odontogram (
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            tooth INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'sano',
            notes TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (patient_id, tooth)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS prescriptions (
            patient_id TEXT PRIMARY KEY REFERENCES patients(id) ON DELETE CASCADE,
            medications TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS budgets (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendiente',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS budget_items (
            id TEXT PRIMARY KEY,
            budget_id TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            cost REAL NOT NULL DEFAULT 0
        )"""
    )
    # Migración suave: si la tabla patients ya existía sin la columna photo, agrégala.
    cols = [r[1] for r in db.execute("PRAGMA table_info(patients)").fetchall()]
    if "photo" not in cols:
        db.execute("ALTER TABLE patients ADD COLUMN photo TEXT")
    if "dui" not in cols:
        db.execute("ALTER TABLE patients ADD COLUMN dui TEXT")
    ucols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if "role" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
    if "permissions" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT ''")
    if "display_name" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''")
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    for k, v in EMAIL_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    for k, v in SYNC_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()
    db.close()


# Se llama al importar el módulo, para que funcione tanto con `python app.py`
# como cuando un servidor WSGI (PythonAnywhere, gunicorn, etc.) importa `app`
# directamente sin pasar por el bloque `if __name__ == "__main__":`.
init_db()


# ---------------------------------------------------------------------------
# Respaldo automático de la base de datos
# ---------------------------------------------------------------------------

def _get_setting_value(key, default=""):
    try:
        db = sqlite3.connect(DB_PATH)
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        db.close()
        return row[0] if row and row[0] else default
    except Exception:
        return default


def do_backup():
    """Copia clinica.db a backups/ de forma segura (aunque el servidor esté escribiendo)
    usando la API de respaldo de SQLite, y elimina respaldos antiguos si hay demasiados."""
    if not os.path.exists(DB_PATH):
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest_path = os.path.join(BACKUP_DIR, f"clinica_{stamp}.db")
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest_path)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        print(f"[respaldo] Error al respaldar: {e}")
        return None

    # También copiamos los archivos subidos (fotos, documentos, logo) la primera vez del día,
    # comprimidos junto al respaldo, para no perder radiografías/documentos si falla el disco.
    try:
        if os.path.isdir(UPLOAD_DIR) and os.listdir(UPLOAD_DIR):
            shutil.make_archive(os.path.join(BACKUP_DIR, f"uploads_{stamp}"), "zip", UPLOAD_DIR)
    except Exception as e:
        print(f"[respaldo] No se pudieron respaldar los archivos subidos: {e}")

    _prune_backups()
    _sync_to_folder([dest_path] + ([f"{os.path.join(BACKUP_DIR, f'uploads_{stamp}.zip')}"] if os.path.exists(os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")) else []))
    print(f"[respaldo] Copia de seguridad creada: {os.path.basename(dest_path)}")
    return dest_path


def _sync_to_folder(paths):
    """Copia archivos de respaldo a una carpeta local sincronizada con Drive/OneDrive/Dropbox,
    si el administrador configuró una en Ajustes. No requiere internet: solo copia el archivo
    a esa carpeta; es la app de Drive/OneDrive la que luego lo sube cuando haya conexión."""
    folder = _get_setting_value("backup_folder", "")
    if not folder:
        return False
    if not os.path.isdir(folder):
        print(f"[respaldo] La carpeta de sincronización no existe: {folder}")
        return False
    ok = True
    for p in paths:
        try:
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(folder, os.path.basename(p)))
        except Exception as e:
            print(f"[respaldo] No se pudo copiar {p} a la carpeta sincronizada: {e}")
            ok = False
    return ok


def _prune_backups():
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")]
    )
    excess = len(files) - MAX_BACKUPS
    for old in files[:max(excess, 0)]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            stamp = old[len("clinica_"):-len(".db")]
            zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
            if os.path.exists(zpath):
                os.remove(zpath)
        except Exception:
            pass


def _todays_backup_exists():
    today = datetime.now().strftime("%Y-%m-%d")
    return any(f.startswith(f"clinica_{today}") for f in os.listdir(BACKUP_DIR))


def _backup_scheduler_loop():
    # Si no existe ningún respaldo de hoy al iniciar la app, hace uno de una vez
    # (cubre el caso de "cerré la app antes de la hora programada ayer").
    if _get_setting_value("backup_enabled", "1") == "1" and not _todays_backup_exists():
        do_backup()
    while True:
        try:
            hour_str = _get_setting_value("backup_hour", "21:00")
            hh, mm = (hour_str.split(":") + ["0", "0"])[:2]
            target_hour, target_min = int(hh), int(mm)
        except Exception:
            target_hour, target_min = 21, 0
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=now.day) + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        time.sleep(min(sleep_seconds, 3600))  # revisa cada hora como máximo, por si cambian la hora configurada
        if datetime.now() >= target and _get_setting_value("backup_enabled", "1") == "1":
            if not _todays_backup_exists():
                do_backup()


_backup_thread_started = False


def start_backup_scheduler():
    global _backup_thread_started
    if _backup_thread_started:
        return
    _backup_thread_started = True
    t = threading.Thread(target=_backup_scheduler_loop, daemon=True)
    t.start()


# El respaldo automático programado (el hilo en segundo plano) solo se arranca
# cuando corres `python app.py` directamente en tu propia PC (ver el bloque
# `if __name__ == "__main__":` al final del archivo). NO se arranca aquí al
# nivel del módulo, porque en un hosting con WSGI (PythonAnywhere, gunicorn,
# etc.) la aplicación se importa y luego el proceso se "clona" (fork) para
# atender peticiones — un hilo en segundo plano iniciado antes de ese clonado
# puede quedar trabado para siempre, dejando la página cargando sin fin y sin
# ningún error en el log. Puedes seguir haciendo respaldos manuales desde
# Configuración → Respaldo de datos sin importar dónde esté corriendo la app;
# para respaldos automáticos en un hosting, usa la función de "tareas
# programadas" (Scheduled tasks) que ofrezca ese hosting.


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

# Throttling simple en memoria contra intentos de fuerza bruta al login.
_failed_attempts = {}
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 60


def any_user_exists():
    db = get_db()
    row = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return row["c"] > 0


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/") or request.path.startswith("/uploads/"):
                return jsonify({"error": "No autenticado"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "No autenticado"}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "Solo el administrador puede hacer esto."}), 403
        return view(*args, **kwargs)

    return wrapped


def has_permission(view_key):
    if session.get("role") == "admin":
        return True
    perms = (session.get("permissions") or "")
    if perms == "all":
        return True
    return view_key in [p for p in perms.split(",") if p]


# Mapea prefijos de rutas de la API a la sección/permiso que controlan.
# Se revisa en orden; la primera coincidencia gana.
API_PERMISSION_MAP = [
    ("/api/users", None),  # admin_required se aplica en la propia ruta
    ("/api/backups", None),  # admin_required se aplica en la propia ruta
    ("/api/sync", None),  # admin_required se aplica en la propia ruta
    ("/api/settings", None),  # GET libre para todos, escritura protegida en la propia ruta
    ("/api/change-password", None),
    ("/api/me", None),
    ("/api/appointments", "agenda"),
    ("/api/patients", "pacientes"),  # incluye /api/patients/<id>/odontogram y /documents, ver abajo
    ("/api/documents", "pacientes"),
    ("/api/budgets", "presupuestos"),
    ("/api/charges", "tratamientos"),
]


@app.before_request
def enforce_permissions():
    if not request.path.startswith("/api/"):
        return
    if not session.get("user_id"):
        return  # lo maneja login_required
    if session.get("role") == "admin":
        return
    if request.path.endswith("/odontogram") or request.path.endswith("/prescription"):
        if not has_permission("odontograma"):
            return jsonify({"error": "No tienes permiso para ver los exámenes."}), 403
        return
    for prefix, key in API_PERMISSION_MAP:
        if request.path.startswith(prefix):
            if key is None:
                return
            if not has_permission(key) and not (prefix == "/api/charges" and has_permission("facturacion")):
                return jsonify({"error": "No tienes permiso para acceder a esta sección."}), 403
            return


@app.before_request
def enforce_setup():
    # Si todavía no hay ningún usuario, todo redirige a /setup (excepto la propia ruta y estáticos).
    if request.path in ("/setup", "/static") or request.path.startswith("/static/"):
        return
    if not any_user_exists() and request.path != "/setup":
        if request.path.startswith("/api/"):
            return jsonify({"error": "Configura el usuario administrador primero"}), 403
        return redirect(url_for("setup"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if any_user_exists():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        activation_code = request.form.get("activation_code", "")

        rec = _failed_attempts.get("_setup_activation")
        if rec and rec["count"] >= MAX_ATTEMPTS and (time.time() - rec["ts"]) < LOCKOUT_SECONDS:
            error = "Demasiados intentos fallidos. Espera un minuto e intenta de nuevo."
        elif not _valid_activation_code(activation_code):
            rec = _failed_attempts.get("_setup_activation", {"count": 0, "ts": 0})
            rec["count"] += 1
            rec["ts"] = time.time()
            _failed_attempts["_setup_activation"] = rec
            error = "Código de activación incorrecto."
        elif len(username) < 3:
            error = "El usuario debe tener al menos 3 caracteres."
        elif len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif password != confirm:
            error = "Las contraseñas no coinciden."
        else:
            _failed_attempts.pop("_setup_activation", None)
            db = get_db()
            db.execute(
                "INSERT INTO users (id, username, password_hash, role, permissions, created_at) VALUES (?, ?, ?, 'admin', 'all', ?)",
                (uuid.uuid4().hex, username, generate_password_hash(password), _now()),
            )
            db.commit()
            return redirect(url_for("login"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not any_user_exists():
        return redirect(url_for("setup"))
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        key = username.lower()
        rec = _failed_attempts.get(key)
        if rec and rec["count"] >= MAX_ATTEMPTS and (time.time() - rec["ts"]) < LOCKOUT_SECONDS:
            error = "Demasiados intentos fallidos. Espera un minuto e intenta de nuevo."
        else:
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                _failed_attempts.pop(key, None)
                session.clear()
                session.permanent = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["permissions"] = user["permissions"]
                return redirect(url_for("index"))
            else:
                rec = _failed_attempts.get(key, {"count": 0, "ts": 0})
                rec["count"] += 1
                rec["ts"] = time.time()
                _failed_attempts[key] = rec
                error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(force=True)
    current = data.get("current", "")
    new = data.get("new", "")
    if len(new) < 8:
        return jsonify({"error": "La nueva contraseña debe tener al menos 8 caracteres."}), 400
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current):
        return jsonify({"error": "La contraseña actual no es correcta."}), 400
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new), user["id"]),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Página principal
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username", ""))


@app.route("/uploads/<path:subpath>")
@login_required
def serve_upload(subpath):
    return send_from_directory(UPLOAD_DIR, subpath)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(s):
    return (s or "").strip()


def patient_to_dict(row):
    d = {k: row[k] for k in row.keys()}
    d["photoUrl"] = ("/uploads/" + d["photo"]) if d.get("photo") else None
    return d


def appt_to_dict(row):
    d = {k: row[k] for k in row.keys()}
    d["patientId"] = d.pop("patient_id")
    return d


def doc_to_dict(row):
    d = {k: row[k] for k in row.keys()}
    d["patientId"] = d.pop("patient_id")
    d["url"] = "/uploads/documents/" + d["filename"]
    return d


def charge_to_dict(row):
    d = {k: row[k] for k in row.keys()}
    d["patientId"] = d.pop("patient_id")
    cost = d["cost"] or 0
    paid = d["paid"] or 0
    if paid <= 0:
        d["status"] = "pendiente"
    elif paid >= cost:
        d["status"] = "pagado"
    else:
        d["status"] = "parcial"
    d["balance"] = round(cost - paid, 2)
    return d


# ---------------------------------------------------------------------------
# API: pacientes
# ---------------------------------------------------------------------------

@app.route("/api/patients", methods=["GET"])
@login_required
def list_patients():
    db = get_db()
    rows = db.execute("SELECT * FROM patients ORDER BY name COLLATE NOCASE").fetchall()
    return jsonify([patient_to_dict(r) for r in rows])


@app.route("/api/patients", methods=["POST"])
@login_required
def create_patient():
    data = request.get_json(force=True)
    name = _clean(data.get("name"))
    phone = _clean(data.get("phone"))
    if not name or not phone:
        return jsonify({"error": "Nombre y teléfono son obligatorios."}), 400
    email = _clean(data.get("email"))
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": "El email no parece válido."}), 400

    pid = uuid.uuid4().hex
    now = _now()
    db = get_db()
    db.execute(
        """INSERT INTO patients (id, name, dob, phone, email, address, allergies, history, notes, dui, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, name, _clean(data.get("dob")), phone, email, _clean(data.get("address")),
         _clean(data.get("allergies")), _clean(data.get("history")), _clean(data.get("notes")),
         _clean(data.get("dui")), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row)), 201


@app.route("/api/patients/<pid>", methods=["PUT"])
@login_required
def update_patient(pid):
    db = get_db()
    existing = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not existing:
        return jsonify({"error": "Paciente no encontrado."}), 404
    data = request.get_json(force=True)
    name = _clean(data.get("name"))
    phone = _clean(data.get("phone"))
    if not name or not phone:
        return jsonify({"error": "Nombre y teléfono son obligatorios."}), 400
    email = _clean(data.get("email"))
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": "El email no parece válido."}), 400

    db.execute(
        """UPDATE patients SET name=?, dob=?, phone=?, email=?, address=?, allergies=?, history=?, notes=?, dui=?, updated_at=?
           WHERE id=?""",
        (name, _clean(data.get("dob")), phone, email, _clean(data.get("address")),
         _clean(data.get("allergies")), _clean(data.get("history")), _clean(data.get("notes")),
         _clean(data.get("dui")), _now(), pid),
    )
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


@app.route("/api/patients/<pid>", methods=["DELETE"])
@login_required
def delete_patient(pid):
    db = get_db()
    row = db.execute("SELECT photo FROM patients WHERE id = ?", (pid,)).fetchone()
    db.execute("DELETE FROM patients WHERE id = ?", (pid,))
    db.commit()
    if row and row["photo"]:
        _safe_remove(os.path.join(UPLOAD_DIR, row["photo"]))
    return jsonify({"ok": True})


def _safe_remove(path):
    try:
        if os.path.commonpath([os.path.abspath(path), UPLOAD_DIR]) == UPLOAD_DIR and os.path.exists(path):
            os.remove(path)
    except (OSError, ValueError):
        pass


@app.route("/api/patients/<pid>/photo", methods=["POST"])
@login_required
def upload_patient_photo(pid):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        return jsonify({"error": "Paciente no encontrado."}), 404
    file = request.files.get("photo")
    if not file or file.filename == "":
        return jsonify({"error": "No se recibió ninguna imagen."}), 400
    if not _ext_ok(file.filename, IMAGE_EXTS):
        return jsonify({"error": "Formato no permitido. Usa JPG, PNG, WEBP o GIF."}), 400

    if patient["photo"]:
        _safe_remove(os.path.join(UPLOAD_DIR, patient["photo"]))

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    rel_path = f"photos/{pid}_{uuid.uuid4().hex[:8]}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, rel_path))
    db.execute("UPDATE patients SET photo = ?, updated_at = ? WHERE id = ?", (rel_path, _now(), pid))
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


@app.route("/api/patients/<pid>/photo", methods=["DELETE"])
@login_required
def delete_patient_photo(pid):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        return jsonify({"error": "Paciente no encontrado."}), 404
    if patient["photo"]:
        _safe_remove(os.path.join(UPLOAD_DIR, patient["photo"]))
    db.execute("UPDATE patients SET photo = NULL, updated_at = ? WHERE id = ?", (_now(), pid))
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


# ---------------------------------------------------------------------------
# API: exámenes / radiografías (documentos por paciente)
# ---------------------------------------------------------------------------

VALID_DOC_CATEGORIES = {"radiografia", "examen", "otro"}


@app.route("/api/patients/<pid>/documents", methods=["GET"])
@login_required
def list_documents(pid):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM documents WHERE patient_id = ? ORDER BY uploaded_at DESC", (pid,)
    ).fetchall()
    return jsonify([doc_to_dict(r) for r in rows])


@app.route("/api/patients/<pid>/documents", methods=["POST"])
@login_required
def upload_document(pid):
    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone()
    if not patient:
        return jsonify({"error": "Paciente no encontrado."}), 404
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "No se recibió ningún archivo."}), 400
    if not _ext_ok(file.filename, DOCUMENT_EXTS):
        return jsonify({"error": "Formato no permitido. Usa JPG, PNG, WEBP, GIF o PDF."}), 400
    category = request.form.get("category", "otro")
    if category not in VALID_DOC_CATEGORIES:
        category = "otro"

    original = secure_filename(file.filename)
    ext = original.rsplit(".", 1)[1].lower()
    did = uuid.uuid4().hex
    stored_name = f"{did}.{ext}"
    file.save(os.path.join(DOCS_DIR, stored_name))
    db.execute(
        """INSERT INTO documents (id, patient_id, filename, original_name, category, uploaded_at)
           VALUES (?,?,?,?,?,?)""",
        (did, pid, stored_name, original, category, _now()),
    )
    db.commit()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    return jsonify(doc_to_dict(row)), 201


@app.route("/api/documents/<did>", methods=["DELETE"])
@login_required
def delete_document(did):
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    if row:
        _safe_remove(os.path.join(DOCS_DIR, row["filename"]))
        db.execute("DELETE FROM documents WHERE id = ?", (did,))
        db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: citas
# ---------------------------------------------------------------------------

VALID_STATUS = {"confirmada", "pendiente", "cancelada"}


@app.route("/api/appointments", methods=["GET"])
@login_required
def list_appointments():
    db = get_db()
    date = request.args.get("date")
    if date:
        rows = db.execute("SELECT * FROM appointments WHERE date = ? ORDER BY time", (date,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM appointments ORDER BY date, time").fetchall()
    return jsonify([appt_to_dict(r) for r in rows])


@app.route("/api/appointments", methods=["POST"])
@login_required
def create_appointment():
    data = request.get_json(force=True)
    patient_id = _clean(data.get("patientId"))
    date = _clean(data.get("date"))
    time_ = _clean(data.get("time"))
    if not patient_id or not date or not time_:
        return jsonify({"error": "Paciente, fecha y hora son obligatorios."}), 400
    status = data.get("status") if data.get("status") in VALID_STATUS else "pendiente"

    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return jsonify({"error": "El paciente no existe."}), 400

    aid = uuid.uuid4().hex
    now = _now()
    db.execute(
        """INSERT INTO appointments (id, patient_id, date, time, duration, status, reason, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (aid, patient_id, date, time_, int(data.get("duration") or 30), status,
         _clean(data.get("reason")), _clean(data.get("notes")), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    return jsonify(appt_to_dict(row)), 201


@app.route("/api/appointments/<aid>", methods=["PUT"])
@login_required
def update_appointment(aid):
    db = get_db()
    existing = db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    if not existing:
        return jsonify({"error": "Cita no encontrada."}), 404
    data = request.get_json(force=True)
    patient_id = _clean(data.get("patientId"))
    date = _clean(data.get("date"))
    time_ = _clean(data.get("time"))
    if not patient_id or not date or not time_:
        return jsonify({"error": "Paciente, fecha y hora son obligatorios."}), 400
    status = data.get("status") if data.get("status") in VALID_STATUS else "pendiente"

    db.execute(
        """UPDATE appointments SET patient_id=?, date=?, time=?, duration=?, status=?, reason=?, notes=?, updated_at=?
           WHERE id=?""",
        (patient_id, date, time_, int(data.get("duration") or 30), status,
         _clean(data.get("reason")), _clean(data.get("notes")), _now(), aid),
    )
    db.commit()
    row = db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    return jsonify(appt_to_dict(row))


@app.route("/api/appointments/<aid>", methods=["DELETE"])
@login_required
def delete_appointment(aid):
    db = get_db()
    db.execute("DELETE FROM appointments WHERE id = ?", (aid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: tratamientos / facturación (charges)
# ---------------------------------------------------------------------------

@app.route("/api/charges", methods=["GET"])
@login_required
def list_charges():
    db = get_db()
    patient_id = request.args.get("patient_id")
    if patient_id:
        rows = db.execute(
            "SELECT * FROM charges WHERE patient_id = ? ORDER BY date DESC", (patient_id,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM charges ORDER BY date DESC").fetchall()
    return jsonify([charge_to_dict(r) for r in rows])


@app.route("/api/charges", methods=["POST"])
@login_required
def create_charge():
    data = request.get_json(force=True)
    patient_id = _clean(data.get("patientId"))
    treatment = _clean(data.get("treatment"))
    date = _clean(data.get("date")) or time.strftime("%Y-%m-%d")
    if not patient_id or not treatment:
        return jsonify({"error": "Paciente y tratamiento son obligatorios."}), 400
    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return jsonify({"error": "El paciente no existe."}), 400
    try:
        cost = float(data.get("cost") or 0)
        paid = float(data.get("paid") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Costo y monto pagado deben ser números."}), 400

    cid = uuid.uuid4().hex
    now = _now()
    db.execute(
        """INSERT INTO charges (id, patient_id, date, treatment, cost, paid, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (cid, patient_id, date, treatment, cost, paid, _clean(data.get("notes")), now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM charges WHERE id = ?", (cid,)).fetchone()
    return jsonify(charge_to_dict(row)), 201


@app.route("/api/charges/<cid>", methods=["PUT"])
@login_required
def update_charge(cid):
    db = get_db()
    existing = db.execute("SELECT * FROM charges WHERE id = ?", (cid,)).fetchone()
    if not existing:
        return jsonify({"error": "Registro no encontrado."}), 404
    data = request.get_json(force=True)
    treatment = _clean(data.get("treatment"))
    date = _clean(data.get("date"))
    if not treatment or not date:
        return jsonify({"error": "Tratamiento y fecha son obligatorios."}), 400
    try:
        cost = float(data.get("cost") or 0)
        paid = float(data.get("paid") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Costo y monto pagado deben ser números."}), 400

    db.execute(
        """UPDATE charges SET date=?, treatment=?, cost=?, paid=?, notes=?, updated_at=? WHERE id=?""",
        (date, treatment, cost, paid, _clean(data.get("notes")), _now(), cid),
    )
    db.commit()
    row = db.execute("SELECT * FROM charges WHERE id = ?", (cid,)).fetchone()
    return jsonify(charge_to_dict(row))


@app.route("/api/charges/<cid>", methods=["DELETE"])
@login_required
def delete_charge(cid):
    db = get_db()
    db.execute("DELETE FROM charges WHERE id = ?", (cid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: configuración / marca (nombre, logo, colores, tipografía)
# ---------------------------------------------------------------------------

ALLOWED_FONTS = {
    "Space Grotesk": "'Space Grotesk', sans-serif",
    "Inter": "'Inter', sans-serif",
    "Poppins": "'Poppins', sans-serif",
    "Merriweather": "'Merriweather', serif",
    "IBM Plex Mono": "'IBM Plex Mono', monospace",
}

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@app.route("/api/settings", methods=["GET"])
@login_required
def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    result = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] in EMAIL_SETTINGS or r["key"] in SYNC_SETTINGS:
            continue  # nunca se exponen por este endpoint general
        result[r["key"]] = r["value"]
    result["logoUrl"] = ("/uploads/" + result["logo_path"]) if result.get("logo_path") else None
    result["fonts"] = list(ALLOWED_FONTS.keys())
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
@login_required
@admin_required
def update_settings():
    data = request.get_json(force=True)
    db = get_db()

    clinic_name = _clean(data.get("clinic_name"))
    if clinic_name:
        db.execute("UPDATE settings SET value = ? WHERE key = 'clinic_name'", (clinic_name[:80],))

    if "doctor_name" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'doctor_name'", (_clean(data.get("doctor_name"))[:80],))

    for color_key in ("primary_color", "accent_color", "bg_color"):
        val = _clean(data.get(color_key))
        if val and HEX_RE.match(val):
            db.execute("UPDATE settings SET value = ? WHERE key = ?", (val, color_key))

    font = data.get("font_family")
    if font in ALLOWED_FONTS:
        db.execute("UPDATE settings SET value = ? WHERE key = 'font_family'", (font,))

    if "font_size" in data:
        try:
            size = int(data.get("font_size"))
            if 12 <= size <= 20:
                db.execute("UPDATE settings SET value = ? WHERE key = 'font_size'", (str(size),))
        except (TypeError, ValueError):
            pass

    if "welcome_message" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'welcome_message'", (_clean(data.get("welcome_message"))[:200],))

    if "backup_hour" in data:
        val = _clean(data.get("backup_hour"))
        if re.match(r"^\d{1,2}:\d{2}$", val or ""):
            db.execute("UPDATE settings SET value = ? WHERE key = 'backup_hour'", (val,))
    if "backup_enabled" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'backup_enabled'", ("1" if data.get("backup_enabled") else "0",))
    if "backup_folder" in data:
        db.execute("UPDATE settings SET value = ? WHERE key = 'backup_folder'", (_clean(data.get("backup_folder"))[:400],))

    db.commit()
    return get_settings()


@app.route("/api/settings/email", methods=["GET"])
@login_required
@admin_required
def get_email_settings():
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    return jsonify({
        "smtp_host": rows.get("smtp_host", ""),
        "smtp_port": rows.get("smtp_port", "587"),
        "smtp_user": rows.get("smtp_user", ""),
        "smtp_use_tls": rows.get("smtp_use_tls", "1") == "1",
        "smtp_from_name": rows.get("smtp_from_name", ""),
        "hasPassword": bool(rows.get("smtp_password")),
    })


@app.route("/api/settings/email", methods=["POST"])
@login_required
@admin_required
def update_email_settings():
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_host'", (_clean(data.get("smtp_host"))[:200],))
    port = _clean(data.get("smtp_port")) or "587"
    if port.isdigit():
        db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_port'", (port,))
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_user'", (_clean(data.get("smtp_user"))[:200],))
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_use_tls'", ("1" if data.get("smtp_use_tls") else "0",))
    db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_from_name'", (_clean(data.get("smtp_from_name"))[:120],))
    if data.get("smtp_password"):
        db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_password'", (data["smtp_password"],))
    db.commit()
    return get_email_settings()


# ---------------------------------------------------------------------------
# API: sincronización con una versión en línea (ej. PythonAnywhere)
# ---------------------------------------------------------------------------

@app.route("/api/settings/sync", methods=["GET"])
@login_required
@admin_required
def get_sync_settings():
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    return jsonify({
        "sync_remote_url": rows.get("sync_remote_url", ""),
        "sync_remote_username": rows.get("sync_remote_username", ""),
        "hasPassword": bool(rows.get("sync_remote_password")),
    })


@app.route("/api/settings/sync", methods=["POST"])
@login_required
@admin_required
def update_sync_settings():
    data = request.get_json(force=True)
    db = get_db()
    url = _clean(data.get("sync_remote_url")).rstrip("/")
    db.execute("UPDATE settings SET value = ? WHERE key = 'sync_remote_url'", (url[:300],))
    db.execute("UPDATE settings SET value = ? WHERE key = 'sync_remote_username'", (_clean(data.get("sync_remote_username"))[:200],))
    if data.get("sync_remote_password"):
        db.execute("UPDATE settings SET value = ? WHERE key = 'sync_remote_password'", (data["sync_remote_password"],))
    db.commit()
    return get_sync_settings()


@app.route("/api/sync/push", methods=["POST"])
@login_required
@admin_required
def push_to_remote():
    try:
        import requests
    except ImportError:
        return jsonify({"error": "Falta instalar la librería 'requests' (pip install -r requirements.txt) para poder usar esta función."}), 500

    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    remote_url = rows.get("sync_remote_url", "")
    remote_user = rows.get("sync_remote_username", "")
    remote_pass = rows.get("sync_remote_password", "")
    if not remote_url or not remote_user or not remote_pass:
        return jsonify({"error": "Primero configura tu versión en línea en Configuración → Sincronización."}), 400

    # Siempre empuja el estado más reciente: genera un respaldo fresco antes de enviarlo.
    backup_path = do_backup()
    if not backup_path:
        return jsonify({"error": "No se pudo generar el respaldo a enviar."}), 500
    stamp = os.path.basename(backup_path)[len("clinica_"):-len(".db")]
    zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")

    session_r = requests.Session()
    try:
        login_resp = session_r.post(
            remote_url + "/login",
            data={"username": remote_user, "password": remote_pass},
            allow_redirects=False,
            timeout=25,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"No se pudo conectar con tu versión en línea: {e}"}), 502

    if login_resp.status_code != 302:
        return jsonify({"error": "No se pudo iniciar sesión en tu versión en línea. Revisa la URL, usuario y contraseña."}), 400

    files = {"backup": (os.path.basename(backup_path), open(backup_path, "rb"), "application/octet-stream")}
    if os.path.exists(zpath):
        files["uploads"] = (os.path.basename(zpath), open(zpath, "rb"), "application/zip")

    try:
        resp = session_r.post(remote_url + "/api/sync/receive-backup", files=files, timeout=120)
    except requests.RequestException as e:
        return jsonify({"error": f"No se pudo enviar el respaldo: {e}"}), 502
    finally:
        for f in files.values():
            f[1].close()

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", "")
        except Exception:
            pass
        return jsonify({"error": f"Tu versión en línea rechazó el envío. {detail}".strip()}), 502

    return jsonify({"ok": True, "message": "Tu versión en línea ya quedó actualizada con los datos de esta PC."})


@app.route("/api/sync/receive-backup", methods=["POST"])
@login_required
@admin_required
def receive_backup():
    file = request.files.get("backup")
    if not file:
        return jsonify({"error": "No se recibió ningún archivo de respaldo."}), 400

    # Respaldo de seguridad del estado actual de ESTE servidor antes de sobrescribir.
    do_backup()

    tmp_path = os.path.join(BACKUP_DIR, "_incoming_sync.db")
    file.save(tmp_path)
    try:
        src = sqlite3.connect(tmp_path)
        dst = sqlite3.connect(DB_PATH)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        return jsonify({"error": f"El archivo recibido no es una base de datos válida: {e}"}), 400
    finally:
        _safe_remove(tmp_path)

    uploads_file = request.files.get("uploads")
    if uploads_file:
        tmp_zip = os.path.join(BACKUP_DIR, "_incoming_sync_uploads.zip")
        uploads_file.save(tmp_zip)
        try:
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(UPLOAD_DIR)
        except Exception as e:
            print(f"[sync] No se pudieron aplicar los archivos subidos recibidos: {e}")
        finally:
            _safe_remove(tmp_zip)

    session.clear()
    return jsonify({"ok": True})


def send_email_with_attachment(to_addr, subject, body, file_path, file_name):
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    host = rows.get("smtp_host", "")
    port = int(rows.get("smtp_port") or 587)
    user = rows.get("smtp_user", "")
    password = rows.get("smtp_password", "")
    use_tls = rows.get("smtp_use_tls", "1") == "1"
    from_name = rows.get("smtp_from_name") or rows.get("clinic_name", "Clínica Dental")

    if not host or not user or not password:
        raise RuntimeError("El correo de la clínica no está configurado todavía (Configuración → Correo).")

    msg = MIMEMultipart()
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(file_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=file_name)
    part["Content-Disposition"] = f'attachment; filename="{file_name}"'
    msg.attach(part)

    with smtplib.SMTP(host, port, timeout=20) as server:
        if use_tls:
            server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


@app.route("/api/documents/<did>/send-email", methods=["POST"])
@login_required
def send_document_email(did):
    data = request.get_json(force=True)
    to_addr = _clean(data.get("to"))
    if not to_addr or "@" not in to_addr:
        return jsonify({"error": "Escribe un correo válido."}), 400

    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    if not row:
        return jsonify({"error": "Documento no encontrado."}), 404
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (row["patient_id"],)).fetchone()
    clinic_name = _get_setting_value("clinic_name", "Clínica Dental")

    file_path = os.path.join(DOCS_DIR, row["filename"])
    if not os.path.exists(file_path):
        return jsonify({"error": "El archivo ya no está disponible en el servidor."}), 404

    subject = data.get("subject") or f"Tu examen de {clinic_name}"
    body = data.get("message") or (
        f"Hola {patient['name'] if patient else ''},\n\n"
        f"Te compartimos tu documento ({row['original_name']}) de {clinic_name}.\n\n"
        f"Saludos."
    )
    try:
        send_email_with_attachment(to_addr, subject, body, file_path, row["original_name"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/settings/logo", methods=["POST"])
@login_required
@admin_required
def upload_logo():
    file = request.files.get("logo")
    if not file or file.filename == "":
        return jsonify({"error": "No se recibió ninguna imagen."}), 400
    if not _ext_ok(file.filename, IMAGE_EXTS):
        return jsonify({"error": "Formato no permitido. Usa JPG, PNG, WEBP o GIF."}), 400

    db = get_db()
    old = db.execute("SELECT value FROM settings WHERE key = 'logo_path'").fetchone()
    if old and old["value"]:
        _safe_remove(os.path.join(UPLOAD_DIR, old["value"]))

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    rel_path = f"branding/logo_{uuid.uuid4().hex[:8]}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, rel_path))
    db.execute("UPDATE settings SET value = ? WHERE key = 'logo_path'", (rel_path,))
    db.commit()
    return get_settings()


@app.route("/api/settings/logo", methods=["DELETE"])
@login_required
@admin_required
def delete_logo():
    db = get_db()
    old = db.execute("SELECT value FROM settings WHERE key = 'logo_path'").fetchone()
    if old and old["value"]:
        _safe_remove(os.path.join(UPLOAD_DIR, old["value"]))
    db.execute("UPDATE settings SET value = '' WHERE key = 'logo_path'")
    db.commit()
    return get_settings()


# ---------------------------------------------------------------------------
# API: respaldos
# ---------------------------------------------------------------------------

def backup_to_dict(filename):
    path = os.path.join(BACKUP_DIR, filename)
    stamp = filename[len("clinica_"):-len(".db")]
    zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
    size = os.path.getsize(path)
    if os.path.exists(zpath):
        size += os.path.getsize(zpath)
    return {
        "filename": filename,
        "createdAt": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
        "sizeKb": round(size / 1024, 1),
        "hasUploads": os.path.exists(zpath),
    }


@app.route("/api/backups", methods=["GET"])
@login_required
@admin_required
def list_backups():
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")],
        reverse=True,
    )
    return jsonify([backup_to_dict(f) for f in files])


@app.route("/api/backups", methods=["POST"])
@login_required
@admin_required
def create_backup_now():
    path = do_backup()
    if not path:
        return jsonify({"error": "No se pudo crear el respaldo."}), 500
    return list_backups()


@app.route("/api/backups/<path:filename>/download", methods=["GET"])
@login_required
@admin_required
def download_backup(filename):
    safe = secure_filename(filename)
    if safe != filename or not os.path.exists(os.path.join(BACKUP_DIR, safe)):
        return jsonify({"error": "Respaldo no encontrado."}), 404
    return send_from_directory(BACKUP_DIR, safe, as_attachment=True)


@app.route("/api/backups/<path:filename>", methods=["DELETE"])
@login_required
@admin_required
def delete_backup(filename):
    safe = secure_filename(filename)
    target = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(target):
        return jsonify({"error": "Respaldo no encontrado."}), 404
    _safe_remove(target)
    if safe.startswith("clinica_") and safe.endswith(".db"):
        stamp = safe[len("clinica_"):-len(".db")]
        _safe_remove(os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip"))
    return jsonify({"ok": True})


@app.route("/api/backups/sync-folder", methods=["POST"])
@login_required
@admin_required
def sync_backup_folder_now():
    folder = _get_setting_value("backup_folder", "")
    if not folder:
        return jsonify({"error": "Primero configura una carpeta de sincronización."}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"La carpeta no existe en esta computadora: {folder}"}), 400
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")],
        reverse=True,
    )
    if not files:
        return jsonify({"error": "Todavía no hay ningún respaldo que copiar."}), 400
    latest = files[0]
    stamp = latest[len("clinica_"):-len(".db")]
    zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
    paths = [os.path.join(BACKUP_DIR, latest)] + ([zpath] if os.path.exists(zpath) else [])
    ok = _sync_to_folder(paths)
    if not ok:
        return jsonify({"error": "No se pudo copiar a la carpeta. Revisa que la ruta sea correcta y tengas permiso de escritura."}), 500
    return jsonify({"ok": True, "folder": folder, "file": latest})


@app.route("/api/backups/<path:filename>/restore", methods=["POST"])
@login_required
@admin_required
def restore_backup(filename):
    safe = secure_filename(filename)
    backup_path = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(backup_path):
        return jsonify({"error": "Respaldo no encontrado."}), 404

    data = request.get_json(silent=True) or {}
    restore_uploads = bool(data.get("restoreUploads"))

    # Respaldo de seguridad del estado actual antes de sobrescribir, por si algo sale mal.
    do_backup()

    try:
        src = sqlite3.connect(backup_path)
        dst = sqlite3.connect(DB_PATH)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        return jsonify({"error": f"No se pudo restaurar la base de datos: {e}"}), 500

    if restore_uploads and safe.startswith("clinica_") and safe.endswith(".db"):
        stamp = safe[len("clinica_"):-len(".db")]
        zpath = os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip")
        if os.path.exists(zpath):
            try:
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(UPLOAD_DIR)
            except Exception as e:
                print(f"[respaldo] No se pudieron restaurar los archivos subidos: {e}")

    session.clear()
    return jsonify({"ok": True, "message": "Respaldo restaurado. Vuelve a iniciar sesión."})


# ---------------------------------------------------------------------------
# API: odontograma
# ---------------------------------------------------------------------------

ODONTO_STATUSES = {"sano", "caries", "obturado", "corona", "ausente", "extraccion", "implante", "endodoncia"}


@app.route("/api/patients/<pid>/odontogram", methods=["GET"])
@login_required
def get_odontogram(pid):
    db = get_db()
    rows = db.execute(
        "SELECT tooth, status, notes, updated_at FROM odontogram WHERE patient_id = ?", (pid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/patients/<pid>/odontogram", methods=["POST"])
@login_required
def upsert_odontogram(pid):
    data = request.get_json(force=True)
    tooth = data.get("tooth")
    status = _clean(data.get("status")) or "sano"
    notes = _clean(data.get("notes"))
    quadrant, pos = (tooth // 10, tooth % 10) if isinstance(tooth, int) else (0, 0)
    if not isinstance(tooth, int) or quadrant not in (1, 2, 3, 4) or not (1 <= pos <= 8):
        return jsonify({"error": "Número de diente inválido."}), 400
    if status not in ODONTO_STATUSES:
        return jsonify({"error": "Estado de diente inválido."}), 400

    db = get_db()
    if not db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone():
        return jsonify({"error": "Paciente no encontrado."}), 404

    db.execute(
        """INSERT INTO odontogram (patient_id, tooth, status, notes, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(patient_id, tooth) DO UPDATE SET
             status = excluded.status, notes = excluded.notes, updated_at = excluded.updated_at""",
        (pid, tooth, status, notes, _now()),
    )
    db.commit()
    rows = db.execute(
        "SELECT tooth, status, notes, updated_at FROM odontogram WHERE patient_id = ?", (pid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# API: receta de medicamentos
# ---------------------------------------------------------------------------

@app.route("/api/patients/<pid>/prescription", methods=["GET"])
@login_required
def get_prescription(pid):
    db = get_db()
    row = db.execute(
        "SELECT medications, instructions, updated_at FROM prescriptions WHERE patient_id = ?", (pid,)
    ).fetchone()
    return jsonify(dict(row) if row else {"medications": "", "instructions": "", "updated_at": ""})


@app.route("/api/patients/<pid>/prescription", methods=["POST"])
@login_required
def save_prescription(pid):
    data = request.get_json(force=True)
    medications = _clean(data.get("medications"))
    instructions = _clean(data.get("instructions"))
    db = get_db()
    if not db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone():
        return jsonify({"error": "Paciente no encontrado."}), 404
    now = _now()
    db.execute(
        """INSERT INTO prescriptions (patient_id, medications, instructions, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(patient_id) DO UPDATE SET
             medications = excluded.medications, instructions = excluded.instructions, updated_at = excluded.updated_at""",
        (pid, medications, instructions, now),
    )
    db.commit()
    return jsonify({"medications": medications, "instructions": instructions, "updated_at": now})


# ---------------------------------------------------------------------------
# API: presupuestos
# ---------------------------------------------------------------------------

BUDGET_STATUSES = {"pendiente", "aprobado", "rechazado"}


def budget_to_dict(row, items):
    d = dict(row)
    d["items"] = [{"id": i["id"], "description": i["description"], "cost": i["cost"]} for i in items]
    d["total"] = round(sum(i["cost"] for i in items), 2)
    return d


@app.route("/api/budgets", methods=["GET"])
@login_required
def list_budgets():
    db = get_db()
    pid = request.args.get("patient_id")
    if pid:
        rows = db.execute(
            "SELECT * FROM budgets WHERE patient_id = ? ORDER BY created_at DESC", (pid,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM budgets ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        items = db.execute("SELECT * FROM budget_items WHERE budget_id = ?", (r["id"],)).fetchall()
        result.append(budget_to_dict(r, items))
    return jsonify(result)


@app.route("/api/budgets", methods=["POST"])
@login_required
def create_budget():
    data = request.get_json(force=True)
    pid = data.get("patient_id")
    title = _clean(data.get("title"))
    items = data.get("items") or []
    if not pid or not title:
        return jsonify({"error": "Paciente y título son obligatorios."}), 400

    db = get_db()
    if not db.execute("SELECT id FROM patients WHERE id = ?", (pid,)).fetchone():
        return jsonify({"error": "Paciente no encontrado."}), 404

    bid = uuid.uuid4().hex
    now = _now()
    db.execute(
        """INSERT INTO budgets (id, patient_id, title, status, notes, created_at, updated_at)
           VALUES (?, ?, ?, 'pendiente', ?, ?, ?)""",
        (bid, pid, title[:120], _clean(data.get("notes")), now, now),
    )
    for it in items:
        desc = _clean(it.get("description"))
        try:
            cost = float(it.get("cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        if desc:
            db.execute(
                "INSERT INTO budget_items (id, budget_id, description, cost) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, bid, desc[:200], cost),
            )
    db.commit()
    row = db.execute("SELECT * FROM budgets WHERE id = ?", (bid,)).fetchone()
    items_rows = db.execute("SELECT * FROM budget_items WHERE budget_id = ?", (bid,)).fetchall()
    return jsonify(budget_to_dict(row, items_rows)), 201


@app.route("/api/budgets/<bid>", methods=["PUT"])
@login_required
def update_budget(bid):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT * FROM budgets WHERE id = ?", (bid,)).fetchone()
    if not row:
        return jsonify({"error": "Presupuesto no encontrado."}), 404

    title = _clean(data.get("title")) or row["title"]
    status = data.get("status") or row["status"]
    if status not in BUDGET_STATUSES:
        return jsonify({"error": "Estado inválido."}), 400
    notes = data.get("notes", row["notes"])

    db.execute(
        "UPDATE budgets SET title = ?, status = ?, notes = ?, updated_at = ? WHERE id = ?",
        (title[:120], status, _clean(notes), _now(), bid),
    )

    if data.get("items") is not None:
        db.execute("DELETE FROM budget_items WHERE budget_id = ?", (bid,))
        for it in data["items"]:
            desc = _clean(it.get("description"))
            try:
                cost = float(it.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0
            if desc:
                db.execute(
                    "INSERT INTO budget_items (id, budget_id, description, cost) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex, bid, desc[:200], cost),
                )
    db.commit()
    row = db.execute("SELECT * FROM budgets WHERE id = ?", (bid,)).fetchone()
    items_rows = db.execute("SELECT * FROM budget_items WHERE budget_id = ?", (bid,)).fetchall()
    return jsonify(budget_to_dict(row, items_rows))


@app.route("/api/budgets/<bid>", methods=["DELETE"])
@login_required
def delete_budget(bid):
    db = get_db()
    db.execute("DELETE FROM budgets WHERE id = ?", (bid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: sesión actual y gestión de usuarios (solo administrador)
# ---------------------------------------------------------------------------

def user_to_dict(row):
    perms = row["permissions"] or ""
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "permissions": [] if row["role"] == "admin" else [p for p in perms.split(",") if p],
        "displayName": row["display_name"] or "",
        "createdAt": row["created_at"],
    }


@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado"}), 404
    return jsonify(user_to_dict(row))


@app.route("/api/users", methods=["GET"])
@login_required
@admin_required
def list_users():
    db = get_db()
    rows = db.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return jsonify([user_to_dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@login_required
@admin_required
def create_user():
    data = request.get_json(force=True)
    username = _clean(data.get("username"))
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ("admin", "secretaria") else "secretaria"
    perms = data.get("permissions") or []
    perms = [p for p in perms if p in VIEW_KEYS]
    display_name = _clean(data.get("displayName"))

    if len(username) < 3:
        return jsonify({"error": "El usuario debe tener al menos 3 caracteres."}), 400
    if len(password) < 8:
        return jsonify({"error": "La contraseña debe tener al menos 8 caracteres."}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        return jsonify({"error": "Ese usuario ya existe."}), 400

    uid_ = uuid.uuid4().hex
    db.execute(
        """INSERT INTO users (id, username, password_hash, role, permissions, display_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uid_, username, generate_password_hash(password), role,
         "all" if role == "admin" else ",".join(perms), display_name, _now()),
    )
    db.commit()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid_,)).fetchone()
    return jsonify(user_to_dict(row)), 201


@app.route("/api/users/<uid>", methods=["PUT"])
@login_required
@admin_required
def update_user(uid):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return jsonify({"error": "Usuario no encontrado."}), 404

    role = data.get("role") if data.get("role") in ("admin", "secretaria") else row["role"]
    perms = data.get("permissions")
    perms_val = row["permissions"]
    if role == "admin":
        perms_val = "all"
    elif perms is not None:
        perms_val = ",".join([p for p in perms if p in VIEW_KEYS])
    display_name = data.get("displayName", row["display_name"])

    db.execute(
        "UPDATE users SET role = ?, permissions = ?, display_name = ? WHERE id = ?",
        (role, perms_val, _clean(display_name), uid),
    )
    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"error": "La contraseña debe tener al menos 8 caracteres."}), 400
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(data["password"]), uid))
    db.commit()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return jsonify(user_to_dict(row))


@app.route("/api/users/<uid>", methods=["DELETE"])
@login_required
@admin_required
def delete_user(uid):
    db = get_db()
    if uid == session.get("user_id"):
        return jsonify({"error": "No puedes eliminar tu propia cuenta."}), 400
    admins_left = db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND id != ?", (uid,)).fetchone()
    target = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
    if target and target["role"] == "admin" and admins_left["c"] == 0:
        return jsonify({"error": "Debe quedar al menos un administrador."}), 400
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_backup_scheduler()
    print("\nClínica Dental corriendo en http://localhost:5000\n")
    try:
        from waitress import serve
        # waitress maneja varias peticiones a la vez de forma segura (útil si
        # 2 personas usan la app al mismo tiempo desde esta misma PC/red),
        # a diferencia del servidor de desarrollo simple de Flask.
        serve(app, host="127.0.0.1", port=5000, threads=4)
    except ImportError:
        print("(Sugerencia: instala 'waitress' con pip install -r requirements.txt")
        print(" para un servidor más robusto con varios usuarios a la vez.)\n")
        app.run(host="127.0.0.1", port=5000, debug=False)
