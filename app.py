"""
Clínica Dental — servidor con base de datos SQLite (local) o PostgreSQL
(automático si existe la variable de entorno DATABASE_URL, por ejemplo en
Render) y login.

Cómo correr localmente:
    pip install -r requirements.txt
    python app.py

Luego abre http://localhost:5000 en tu navegador.
La primera vez te pedirá un código de activación y crear el usuario
administrador.
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

from flask import (Flask, Response, g, jsonify, redirect, render_template,
                    render_template_string, request, send_from_directory,
                    session, url_for)
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
# Base de datos: SQLite en local, PostgreSQL automáticamente si existe
# DATABASE_URL (por ejemplo, en Render). El resto del código no necesita
# saber cuál de las dos está usando — ver la clase CompatConnection abajo.
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Render (y algunos otros) entregan la URL como "postgres://", pero
    # psycopg2 moderno exige el prefijo "postgresql://".
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------------------------
# Llave secreta persistente (se genera una sola vez, no se debe compartir).
# En Render, si defines la variable de entorno SECRET_KEY, se usa esa (así
# no cambia en cada reinicio del servidor, lo que invalidaría las sesiones).
# ---------------------------------------------------------------------------
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    SECRET_KEY = _env_secret
elif os.path.exists(SECRET_KEY_PATH):
    with open(SECRET_KEY_PATH, "r") as f:
        SECRET_KEY = f.read().strip()
else:
    SECRET_KEY = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with open(SECRET_KEY_PATH, "w") as f:
            f.write(SECRET_KEY)
    except OSError:
        pass  # sistema de archivos de solo lectura o efímero — no es crítico

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
# Capa de compatibilidad SQLite / PostgreSQL
# ---------------------------------------------------------------------------

def _raw_connection():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class CompatConnection:
    """Envuelve la conexión real (SQLite o PostgreSQL) para que el resto del
    código pueda seguir escribiendo db.execute("... ? ...", (valor,)) y
    leyendo row["columna"] sin importar cuál motor esté activo."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        cur = self.raw.cursor()
        if USE_POSTGRES:
            sql = sql.replace("?", "%s")
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def _insert_ignore(table, columns):
    """Genera un INSERT que no falla si la fila ya existe (para los valores
    por defecto de settings). columns[0] debe ser la clave primaria."""
    cols_sql = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    if USE_POSTGRES:
        return f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders}) ON CONFLICT ({columns[0]}) DO NOTHING"
    return f"INSERT OR IGNORE INTO {table} ({cols_sql}) VALUES ({placeholders})"


def _column_exists(db, table, column):
    if USE_POSTGRES:
        row = db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return row is not None
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


# ---------------------------------------------------------------------------
# Base de datos: conexión por petición
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = CompatConnection(_raw_connection())
        if not USE_POSTGRES:
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

EMAIL_SETTINGS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_use_tls": "1",
    "smtp_from_name": "",
}

SYNC_SETTINGS = {
    "sync_remote_url": "",
    "sync_remote_username": "",
    "sync_remote_password": "",
}

VIEW_KEYS = ["pacientes", "agenda", "calendario", "tratamientos", "odontograma", "presupuestos", "facturacion"]


def init_db():
    raw = _raw_connection()
    db = CompatConnection(raw)
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
    if USE_POSTGRES:
        # En Render (y hostings similares) el disco del servidor NO es persistente:
        # se borra en cada reinicio. Por eso, cuando se usa PostgreSQL, las fotos y
        # documentos subidos se guardan dentro de la propia base de datos en vez
        # de en el disco, para que sobrevivan los reinicios igual que el resto
        # de la información.
        db.execute(
            """CREATE TABLE IF NOT EXISTS file_blobs (
                id TEXT PRIMARY KEY,
                data BYTEA NOT NULL,
                content_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
    if not _column_exists(db, "patients", "photo"):
        db.execute("ALTER TABLE patients ADD COLUMN photo TEXT")
    if not _column_exists(db, "patients", "dui"):
        db.execute("ALTER TABLE patients ADD COLUMN dui TEXT")
    if not _column_exists(db, "users", "role"):
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
    if not _column_exists(db, "users", "permissions"):
        db.execute("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT ''")
    if not _column_exists(db, "users", "display_name"):
        db.execute("ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''")
    if not _column_exists(db, "users", "security_question"):
        db.execute("ALTER TABLE users ADD COLUMN security_question TEXT DEFAULT ''")
    if not _column_exists(db, "users", "security_answer_hash"):
        db.execute("ALTER TABLE users ADD COLUMN security_answer_hash TEXT DEFAULT ''")

    for k, v in DEFAULT_SETTINGS.items():
        db.execute(_insert_ignore("settings", ["key", "value"]), (k, v))
    for k, v in EMAIL_SETTINGS.items():
        db.execute(_insert_ignore("settings", ["key", "value"]), (k, v))
    for k, v in SYNC_SETTINGS.items():
        db.execute(_insert_ignore("settings", ["key", "value"]), (k, v))
    db.commit()
    db.close()


init_db()


def _get_setting_value(key, default=""):
    try:
        raw = _raw_connection()
        db = CompatConnection(raw)
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        db.close()
        return row["value"] if row and row["value"] else default
    except Exception:
        return default


def do_backup():
    """Copia clinica.db a backups/ (solo aplica con SQLite local; en
    PostgreSQL no hay un solo archivo que copiar — usa el respaldo de tu
    proveedor de base de datos, ej. Render Postgres)."""
    if USE_POSTGRES or not os.path.exists(DB_PATH):
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
    if USE_POSTGRES:
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    return any(f.startswith(f"clinica_{today}") for f in os.listdir(BACKUP_DIR))


def _backup_scheduler_loop():
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
        time.sleep(min(sleep_seconds, 3600))
        if datetime.now() >= target and _get_setting_value("backup_enabled", "1") == "1":
            if not _todays_backup_exists():
                do_backup()


_backup_thread_started = False


def start_backup_scheduler():
    global _backup_thread_started
    if _backup_thread_started or USE_POSTGRES:
        return
    _backup_thread_started = True
    t = threading.Thread(target=_backup_scheduler_loop, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

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


API_PERMISSION_MAP = [
    ("/api/users", None),
    ("/api/backups", None),
    ("/api/sync", None),
    ("/api/settings", None),
    ("/api/change-password", None),
    ("/api/me", None),
    ("/api/appointments", "agenda"),
    ("/api/patients", "pacientes"),
    ("/api/documents", "pacientes"),
    ("/api/budgets", "presupuestos"),
    ("/api/charges", "tratamientos"),
]


@app.before_request
def enforce_permissions():
    if not request.path.startswith("/api/"):
        return
    if not session.get("user_id"):
        return
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


_FORGOT_PASSWORD_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Recuperar contraseña · Clínica Dental</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#FAF8F4;--ink:#1E2A28;--primary:#2F6F62;--primary-dark:#204E45;--accent:#E1734F;--accent-dark:#C85E3B;--border:#DFE5E1;--muted:#7C8B87;--danger:#C0463C;--danger-light:#FBEAE8;--ok-light:#E7F3EE;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--primary-dark);font-family:'Inter',sans-serif;color:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
  .card{background:#fff;border-radius:20px;padding:34px 32px;width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,0.25);}
  h1{font-family:'Space Grotesk',sans-serif;font-size:20px;margin:0 0 4px 0;}
  p.sub{color:var(--muted);font-size:13px;margin:0 0 22px 0;}
  label{display:block;font-size:12px;font-weight:600;color:var(--primary-dark);margin-bottom:5px;}
  input{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:14px;font-size:14px;margin-bottom:14px;}
  input:focus{outline:none;border-color:var(--primary);}
  button{width:100%;background:var(--accent);color:#fff;border:none;border-radius:14px;padding:11px;font-size:14px;font-weight:700;cursor:pointer;margin-bottom:10px;}
  button:hover{background:var(--accent-dark);}
  button.secondary{background:#fff;color:var(--primary-dark);border:1px solid var(--border);}
  button.secondary:hover{background:var(--bg);}
  .msg{font-size:12.5px;padding:9px 12px;border-radius:12px;margin-bottom:14px;}
  .ok{background:var(--ok-light);color:var(--primary-dark);}
  .question-box{background:var(--bg);border-radius:14px;padding:12px 14px;margin-bottom:14px;font-size:13px;}
  a.back{display:block;text-align:center;margin-top:4px;color:var(--primary);font-size:12.5px;text-decoration:none;}
</style>
</head>
<body>
  <div class="card">
    <h1>🔑 Recuperar contraseña</h1>
    {% if message %}<div class="msg ok">{{ message }}</div>{% endif %}

    {% if question %}
      <p class="sub">Responde tu pregunta de seguridad para elegir una contraseña nueva. No necesitas internet para este paso.</p>
      <form method="POST">
        <input type="hidden" name="step" value="answer">
        <input type="hidden" name="username" value="{{ username_for_step2 }}">
        <div class="question-box"><strong>{{ question }}</strong></div>
        <label for="answer">Tu respuesta</label>
        <input type="text" id="answer" name="answer" required autofocus>
        <label for="new_password">Nueva contraseña</label>
        <input type="password" id="new_password" name="new_password" required minlength="4">
        <button type="submit">Cambiar contraseña</button>
      </form>
    {% else %}
      <p class="sub">Escribe tu usuario. Si tienes una pregunta de seguridad configurada, la usaremos aquí mismo (sin internet). Si no, se enviará una contraseña temporal por correo.</p>
      <form method="POST">
        <input type="hidden" name="step" value="username">
        <label for="username">Usuario</label>
        <input type="text" id="username" name="username" required autofocus>
        <button type="submit">Continuar</button>
      </form>
    {% endif %}
    <a class="back" href="/login">&larr; Volver a iniciar sesión</a>
  </div>
</body>
</html>
"""

_SECURITY_QUESTION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pregunta de seguridad · Clínica Dental</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#FAF8F4;--ink:#1E2A28;--primary:#2F6F62;--primary-dark:#204E45;--accent:#E1734F;--accent-dark:#C85E3B;--border:#DFE5E1;--muted:#7C8B87;--ok-light:#E7F3EE;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);font-family:'Inter',sans-serif;color:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
  .card{background:#fff;border-radius:20px;padding:34px 32px;width:100%;max-width:420px;box-shadow:0 10px 40px rgba(0,0,0,0.12);}
  h1{font-family:'Space Grotesk',sans-serif;font-size:20px;margin:0 0 4px 0;}
  p.sub{color:var(--muted);font-size:13px;margin:0 0 22px 0;}
  label{display:block;font-size:12px;font-weight:600;color:var(--primary-dark);margin-bottom:5px;}
  input{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:14px;font-size:14px;margin-bottom:14px;}
  input:focus{outline:none;border-color:var(--primary);}
  button{width:100%;background:var(--accent);color:#fff;border:none;border-radius:14px;padding:11px;font-size:14px;font-weight:700;cursor:pointer;}
  button:hover{background:var(--accent-dark);}
  .msg{font-size:12.5px;padding:9px 12px;border-radius:12px;margin-bottom:14px;background:var(--ok-light);color:var(--primary-dark);}
  a.back{display:block;text-align:center;margin-top:14px;color:var(--primary);font-size:12.5px;text-decoration:none;}
</style>
</head>
<body>
  <div class="card">
    <h1>🛡️ Tu pregunta de seguridad</h1>
    <p class="sub">Se usa para recuperar tu contraseña sin necesitar internet ni correo. Elige una pregunta que solo tú sepas responder, y no la olvides.</p>
    {% if message %}<div class="msg">{{ message }}</div>{% endif %}
    <form method="POST">
      <label for="security_question">Pregunta</label>
      <input type="text" id="security_question" name="security_question" placeholder="Ej. ¿Nombre de mi primera mascota?" value="{{ current_question }}" required>
      <label for="security_answer">Respuesta</label>
      <input type="text" id="security_answer" name="security_answer" placeholder="Escribe la respuesta (no distingue mayúsculas)" required>
      <label for="current_password">Tu contraseña actual (para confirmar que eres tú)</label>
      <input type="password" id="current_password" name="current_password" required>
      <button type="submit">Guardar pregunta de seguridad</button>
    </form>
    <a class="back" href="/">&larr; Volver a la app</a>
  </div>
</body>
</html>
"""


def _send_recovery_email(user, username):
    """Genera una contraseña temporal y la manda por correo (Brevo). Usado
    como respaldo cuando el usuario no configuró una pregunta de
    seguridad. Devuelve el mensaje a mostrar en pantalla.

    IMPORTANTE: solo cambia la contraseña en la base de datos DESPUÉS de
    confirmar que el correo se envió con éxito — así nunca se queda un
    usuario con una contraseña nueva que nunca le llegó."""
    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    recovery_email = rows.get("smtp_user", "")
    if not recovery_email:
        return (
            "No hay una pregunta de seguridad configurada para este usuario, ni "
            "un correo de recuperación configurado. Pide ayuda a quien administra "
            "el sistema, o usa el script de recuperación directa en la computadora."
        )

    import secrets

    new_password = secrets.token_urlsafe(9)
    try:
        send_email_with_attachment(
            recovery_email,
            f"Contraseña temporal - usuario {username}",
            f"Se solicitó restablecer la contraseña del usuario '{username}'.\n\n"
            f"Tu contraseña temporal es:\n\n{new_password}\n\n"
            "Inicia sesión con ella y cámbiala de inmediato desde tu perfil.\n\n"
            "Si tú no solicitaste este cambio, revisa quién tiene acceso a tu "
            "sistema — alguien más pudo haberlo pedido.",
        )
    except Exception as e:
        print(f"[recuperar contraseña] Error al enviar: {e}", flush=True)
        return (
            "No se pudo enviar el correo de recuperación en este momento "
            "(el servicio de correo puede no estar disponible todavía). Tu "
            "contraseña actual sigue siendo válida — no se hizo ningún cambio. "
            "Intenta de nuevo más tarde, o pide ayuda a quien administra el sistema."
        )

    # Solo llegamos aquí si el correo se mandó sin errores — ahora sí
    # actualizamos la contraseña.
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user["id"]),
    )
    db.commit()
    return (
        "Se envió una contraseña temporal al correo de la clínica configurado. "
        "Revisa también la carpeta de spam."
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Dos formas de recuperar la contraseña:
    1. Pregunta de seguridad — funciona sin internet (ideal para la copia local).
    2. Correo con contraseña temporal vía Brevo — necesita internet, se usa
       como respaldo si el usuario no configuró una pregunta de seguridad.
    Nunca revela si un usuario existe o no, para no dar pistas a terceros.
    """
    message = None
    question = None
    username_for_step2 = None

    if request.method == "POST":
        step = request.form.get("step", "username")
        db = get_db()

        if step == "username":
            username = request.form.get("username", "").strip()
            user = db.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
            ).fetchone()
            if user and (user["security_question"] or ""):
                question = user["security_question"]
                username_for_step2 = username
            elif user:
                message = _send_recovery_email(user, username)
            else:
                message = (
                    "Si el usuario existe, se mostrará su pregunta de seguridad o se "
                    "enviará una contraseña temporal por correo."
                )

        elif step == "answer":
            username = request.form.get("username", "").strip()
            answer = request.form.get("answer", "").strip().lower()
            new_password = request.form.get("new_password", "").strip()
            user = db.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
            ).fetchone()
            valid = (
                user
                and (user["security_answer_hash"] or "")
                and check_password_hash(user["security_answer_hash"], answer)
            )
            if valid and len(new_password) >= 4:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), user["id"]),
                )
                db.commit()
                message = "Contraseña actualizada correctamente. Ya puedes iniciar sesión."
            else:
                message = "Respuesta incorrecta o contraseña muy corta (mínimo 4 caracteres). Intenta de nuevo."
                if user:
                    question = user["security_question"]
                    username_for_step2 = username

    return render_template_string(
        _FORGOT_PASSWORD_HTML, message=message, question=question, username_for_step2=username_for_step2
    )


@app.route("/account/security-question", methods=["GET", "POST"])
@login_required
def account_security_question():
    """Permite a un usuario ya conectado configurar (o cambiar) su pregunta
    de seguridad, para poder recuperar su contraseña más adelante sin
    depender de internet ni correo."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (session.get("username"),)).fetchone()
    message = None
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        if not user or not check_password_hash(user["password_hash"], current_password):
            message = "Contraseña actual incorrecta. No se guardaron los cambios."
        else:
            question = request.form.get("security_question", "").strip()[:200]
            answer = request.form.get("security_answer", "").strip().lower()[:200]
            if not question or not answer:
                message = "Completa la pregunta y la respuesta."
            else:
                db.execute(
                    "UPDATE users SET security_question = ?, security_answer_hash = ? WHERE id = ?",
                    (question, generate_password_hash(answer), user["id"]),
                )
                db.commit()
                message = "Pregunta de seguridad guardada correctamente."
                user = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return render_template_string(
        _SECURITY_QUESTION_HTML, message=message, current_question=(user["security_question"] if user else "")
    )


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


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username", ""))


@app.route("/uploads/<path:subpath>")
@login_required
def serve_upload(subpath):
    if USE_POSTGRES:
        db = get_db()
        row = db.execute("SELECT data, content_type FROM file_blobs WHERE id = ?", (subpath,)).fetchone()
        if not row:
            return jsonify({"error": "Archivo no encontrado."}), 404
        return Response(bytes(row["data"]), mimetype=row["content_type"])
    return send_from_directory(UPLOAD_DIR, subpath)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(s):
    return (s or "").strip()


def patient_to_dict(row):
    d = dict(row)
    d["photoUrl"] = ("/uploads/" + d["photo"]) if d.get("photo") else None
    return d


def appt_to_dict(row):
    d = dict(row)
    d["patientId"] = d.pop("patient_id")
    return d


def doc_to_dict(row):
    d = dict(row)
    d["patientId"] = d.pop("patient_id")
    d["url"] = "/uploads/documents/" + d["filename"]
    return d


def charge_to_dict(row):
    d = dict(row)
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


@app.route("/api/patients", methods=["GET"])
@login_required
def list_patients():
    db = get_db()
    order_sql = "SELECT * FROM patients ORDER BY name" if USE_POSTGRES else "SELECT * FROM patients ORDER BY name COLLATE NOCASE"
    rows = db.execute(order_sql).fetchall()
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
        _delete_uploaded_file(row["photo"])
    return jsonify({"ok": True})


def _safe_remove(path, base_dir=UPLOAD_DIR):
    """Borra un archivo, pero solo si está dentro de una carpeta permitida
    (uploads/ o backups/, según se indique) — por seguridad, para no borrar
    nada fuera de esas carpetas por accidente."""
    try:
        base_abs = os.path.abspath(base_dir)
        if os.path.commonpath([os.path.abspath(path), base_abs]) == base_abs and os.path.exists(path):
            os.remove(path)
            return True
    except (OSError, ValueError):
        pass
    return False


def _resize_and_compress_image(file_storage, max_dimension=800, quality=82):
    """Redimensiona (máximo 800px de lado) y comprime a JPEG una imagen antes
    de guardarla, para no ocupar espacio de más — importante sobre todo con
    el límite de 0.5 GB del plan gratuito de Neon. Devuelve
    (bytes, content_type, extensión)."""
    from PIL import Image
    import io

    img = Image.open(file_storage.stream)
    img = img.convert("RGB")  # unifica todo a JPEG (sin canal de transparencia)
    img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    buf.seek(0)
    return buf.read(), "image/jpeg", "jpg"


def _save_uploaded_bytes(rel_path, data, content_type="application/octet-stream"):
    """Como _save_uploaded_file, pero recibe bytes ya listos en vez de un
    FileStorage (para guardar una imagen ya redimensionada/comprimida)."""
    if USE_POSTGRES:
        db = get_db()
        db.execute(
            """INSERT INTO file_blobs (id, data, content_type, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET data = excluded.data, content_type = excluded.content_type""",
            (rel_path, psycopg2.Binary(data), content_type, _now()),
        )
        db.commit()
    else:
        with open(os.path.join(UPLOAD_DIR, rel_path), "wb") as f:
            f.write(data)


def _save_uploaded_file(rel_path, file_storage):
    """Guarda un archivo subido. En PostgreSQL (Render u otro hosting sin disco
    persistente) lo guarda dentro de la base de datos; en SQLite local, en el
    disco como siempre."""
    if USE_POSTGRES:
        data = file_storage.read()
        content_type = file_storage.mimetype or "application/octet-stream"
        db = get_db()
        db.execute(
            """INSERT INTO file_blobs (id, data, content_type, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET data = excluded.data, content_type = excluded.content_type""",
            (rel_path, psycopg2.Binary(data), content_type, _now()),
        )
        db.commit()
    else:
        file_storage.save(os.path.join(UPLOAD_DIR, rel_path))


def _delete_uploaded_file(rel_path):
    if not rel_path:
        return
    if USE_POSTGRES:
        db = get_db()
        db.execute("DELETE FROM file_blobs WHERE id = ?", (rel_path,))
        db.commit()
    else:
        _safe_remove(os.path.join(UPLOAD_DIR, rel_path))


def _read_uploaded_file(rel_path):
    """Devuelve los bytes de un archivo subido, sin importar si vive en disco
    (SQLite local) o en la base de datos (PostgreSQL). None si no existe."""
    if USE_POSTGRES:
        db = get_db()
        row = db.execute("SELECT data FROM file_blobs WHERE id = ?", (rel_path,)).fetchone()
        return bytes(row["data"]) if row else None
    full_path = os.path.join(UPLOAD_DIR, rel_path)
    if not os.path.exists(full_path):
        return None
    with open(full_path, "rb") as f:
        return f.read()


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
        _delete_uploaded_file(patient["photo"])

    try:
        img_bytes, content_type, ext = _resize_and_compress_image(file)
    except Exception as e:
        return jsonify({"error": f"No se pudo procesar la imagen: {e}"}), 400

    rel_path = f"photos/{pid}_{uuid.uuid4().hex[:8]}.{ext}"
    _save_uploaded_bytes(rel_path, img_bytes, content_type)
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
        _delete_uploaded_file(patient["photo"])
    db.execute("UPDATE patients SET photo = NULL, updated_at = ? WHERE id = ?", (_now(), pid))
    db.commit()
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return jsonify(patient_to_dict(row))


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

    if ext in IMAGE_EXTS:
        # Igual que con la foto de perfil: comprimimos para ahorrar espacio.
        # Usamos un tamaño mayor (1600px) y más calidad que en la foto de
        # perfil, para que una radiografía siga siendo legible clínicamente.
        try:
            img_bytes, content_type, ext = _resize_and_compress_image(file, max_dimension=1600, quality=85)
        except Exception as e:
            return jsonify({"error": f"No se pudo procesar la imagen: {e}"}), 400
        stored_name = f"{did}.{ext}"
        _save_uploaded_bytes(f"documents/{stored_name}", img_bytes, content_type)
    else:
        # PDF: se guarda tal cual, sin comprimir.
        stored_name = f"{did}.{ext}"
        _save_uploaded_file(f"documents/{stored_name}", file)
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
        _delete_uploaded_file(f"documents/{row['filename']}")
        db.execute("DELETE FROM documents WHERE id = ?", (did,))
        db.commit()
    return jsonify({"ok": True})


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
            continue
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
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no necesita 'empujar' respaldos de archivo. Configura respaldos desde tu proveedor de base de datos."}), 400
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
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no puede recibir un respaldo de archivo SQLite."}), 400
    file = request.files.get("backup")
    if not file:
        return jsonify({"error": "No se recibió ningún archivo de respaldo."}), 400

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
        _safe_remove(tmp_path, BACKUP_DIR)

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
            _safe_remove(tmp_zip, BACKUP_DIR)

    session.clear()
    return jsonify({"ok": True})


def send_email_with_attachment(to_addr, subject, body, file_bytes=None, file_name=None):
    """Envía un correo (con o sin adjunto) usando la API de Brevo (HTTPS), en
    vez de SMTP directo. Render bloquea las conexiones salientes a los
    puertos SMTP (25, 465, 587) en el plan gratuito, así que usar SMTP
    directo (Gmail, etc.) no funciona ahí. La API de Brevo viaja por HTTPS
    normal, que sí está permitido.

    Requiere la variable de entorno BREVO_API_KEY configurada en Render, y
    un remitente verificado en Brevo (Senders). El correo de "para" y el
    nombre del remitente se siguen tomando de Configuración → Correo.
    file_bytes/file_name son opcionales — si no se pasan, se manda un
    correo de solo texto (ej. recuperación de contraseña).
    """
    import base64
    import json as json_lib
    import urllib.error
    import urllib.request

    db = get_db()
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
    sender_email = rows.get("smtp_user", "")
    from_name = rows.get("smtp_from_name") or rows.get("clinic_name", "Clínica Dental")

    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Falta configurar BREVO_API_KEY en las variables de entorno de Render.")
    if not sender_email:
        raise RuntimeError("El correo de la clínica no está configurado todavía (Configuración → Correo).")

    payload = {
        "sender": {"name": from_name, "email": sender_email},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": body,
    }
    if file_bytes is not None:
        payload["attachment"] = [
            {
                "content": base64.b64encode(file_bytes).decode("ascii"),
                "name": file_name,
            }
        ]

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json_lib.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Brevo rechazó el envío ({e.code}): {detalle}")


@app.route("/api/cron/email-backup", methods=["GET", "POST"])
def cron_email_backup():
    """Dispara el respaldo por correo desde afuera (sin necesitar sesión
    iniciada) — pensado para un servicio externo de cron (ej. cron-job.org)
    que visite esta URL una vez al día. Esto también sirve para 'despertar'
    la app en el plan gratuito de Render, donde el hilo interno no es
    confiable porque el servicio se duerme sin visitas.

    Protegido con una clave secreta: hay que definir la variable de entorno
    BACKUP_CRON_SECRET en Render (Environment) y visitar:
        https://tu-app.onrender.com/api/cron/email-backup?key=TU_CLAVE
    """
    expected = os.environ.get("BACKUP_CRON_SECRET", "").strip()
    provided = request.args.get("key") or request.headers.get("X-Backup-Key", "")
    if not expected or provided != expected:
        return jsonify({"error": "No autorizado."}), 401
    ok, detalle = do_email_backup()
    if ok:
        return jsonify({"ok": True, "message": "Respaldo enviado por correo."})
    return jsonify({"ok": False, "message": f"No se pudo enviar: {detalle}"}), 500


@app.route("/api/cron/backup-download", methods=["GET"])
def cron_backup_download():
    """Descarga el respaldo .json completo, protegido con la misma clave
    BACKUP_CRON_SECRET (sin necesitar sesión iniciada) — pensado para que
    un script en tu computadora (ej. una tarea programada de Windows) lo
    descargue automáticamente a una hora fija, guardándolo directo en tu
    PC sin que tengas que entrar a la app ni hacer nada manual.

        https://tu-app.onrender.com/api/cron/backup-download?key=TU_CLAVE
    """
    expected = os.environ.get("BACKUP_CRON_SECRET", "").strip()
    provided = request.args.get("key") or request.headers.get("X-Backup-Key", "")
    if not expected or provided != expected:
        return jsonify({"error": "No autorizado."}), 401
    db = get_db()
    payload = generate_backup_payload(db)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="respaldo_completo_{stamp}.json"'},
    )


def do_email_backup():
    """Genera el respaldo completo y lo envía por correo a la cuenta de la
    clínica configurada en Configuración → Correo. A diferencia de
    do_backup() (que solo funciona con SQLite local), esto sí funciona con
    PostgreSQL/Neon — por eso es el respaldo que corre en Render."""
    with app.app_context():
        try:
            db = get_db()
            rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
            to_addr = rows.get("smtp_user", "")
            if not to_addr:
                print("[respaldo por correo] Correo no configurado todavía, se omite el envío de hoy.", flush=True)
                return False, "No hay un correo de la clínica configurado en Configuración → Correo."
            payload = generate_backup_payload(db)
            clinic = rows.get("clinic_name") or "Clínica Dental"
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            # Brevo no acepta .json como adjunto (solo permite un listado fijo
            # de extensiones: pdf, txt, zip, docx, csv, etc.), así que lo
            # comprimimos en un .zip — adentro sigue siendo el mismo .json.
            import io

            json_name = f"respaldo_completo_{stamp}.json"
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(json_name, payload)
            fname = f"respaldo_completo_{stamp}.zip"

            send_email_with_attachment(
                to_addr,
                f"Respaldo diario - {clinic} - {datetime.now().strftime('%d/%m/%Y')}",
                "Adjunto el respaldo automático diario de la base de datos de la clínica "
                "(pacientes, citas, cargos, presupuestos y documentos), comprimido en .zip. "
                "Descomprímelo para obtener el archivo .json — es el mismo que usas para "
                "Importar respaldo. Guarda este correo o el archivo en un lugar seguro "
                "fuera de Render y Neon.",
                zip_buf.getvalue(),
                fname,
            )
            print(f"[respaldo por correo] Enviado correctamente: {fname}", flush=True)
            return True, ""
        except Exception as e:
            print(f"[respaldo por correo] Error al enviar: {e}", flush=True)
            return False, str(e)
        finally:
            close_db()


def _email_backup_scheduler_loop():
    while True:
        target_hour, target_min, enabled = 21, 0, True
        try:
            with app.app_context():
                db = get_db()
                rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}
                close_db()
            enabled = rows.get("backup_enabled", "1") == "1"
            hh, mm = (rows.get("backup_hour", "21:00").split(":") + ["0", "0"])[:2]
            target_hour, target_min = int(hh), int(mm)
        except Exception:
            pass
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        time.sleep(min(sleep_seconds, 3600))
        if datetime.now() >= target and enabled:
            do_email_backup()


_email_backup_thread_started = False


def start_email_backup_scheduler():
    """Inicia el hilo que manda el respaldo por correo una vez al día, a la
    misma hora configurada en Configuración → Copias de seguridad. Funciona
    igual con SQLite o con PostgreSQL/Neon."""
    global _email_backup_thread_started
    if _email_backup_thread_started:
        return
    _email_backup_thread_started = True
    t = threading.Thread(target=_email_backup_scheduler_loop, daemon=True)
    t.start()


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

    file_bytes = _read_uploaded_file(f"documents/{row['filename']}")
    if file_bytes is None:
        return jsonify({"error": "El archivo ya no está disponible en el servidor."}), 404

    subject = data.get("subject") or f"Tu examen de {clinic_name}"
    body = data.get("message") or (
        f"Hola {patient['name'] if patient else ''},\n\n"
        f"Te compartimos tu documento ({row['original_name']}) de {clinic_name}.\n\n"
        f"Saludos."
    )
    try:
        send_email_with_attachment(to_addr, subject, body, file_bytes, row["original_name"])
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
        _delete_uploaded_file(old["value"])

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    rel_path = f"branding/logo_{uuid.uuid4().hex[:8]}.{ext}"
    _save_uploaded_file(rel_path, file)
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
        _delete_uploaded_file(old["value"])
    db.execute("UPDATE settings SET value = '' WHERE key = 'logo_path'")
    db.commit()
    return get_settings()


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
    if USE_POSTGRES:
        return jsonify([])
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("clinica_") and f.endswith(".db")],
        reverse=True,
    )
    return jsonify([backup_to_dict(f) for f in files])


@app.route("/api/backups", methods=["POST"])
@login_required
@admin_required
def create_backup_now():
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — los respaldos de archivo no aplican. Usa el sistema de respaldos de tu proveedor de base de datos."}), 400
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


# Todas las tablas que se incluyen en la exportación completa, en el orden
# correcto para poder reconstruirlas después (padres antes que hijos).
EXPORT_TABLES = [
    "users", "patients", "appointments", "documents", "charges", "settings",
    "odontogram", "prescriptions", "budgets", "budget_items",
]


def generate_backup_payload(db):
    """Genera los bytes del respaldo completo (pacientes, citas, cargos,
    presupuestos, usuarios y — en PostgreSQL/Neon — los archivos adjuntos
    en base64). Usado tanto por la descarga manual (/api/backups/export)
    como por el envío automático diario por correo."""
    import base64
    import json as json_lib

    data = {"exported_at": _now(), "tables": {}}
    for table in EXPORT_TABLES:
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        data["tables"][table] = [dict(r) for r in rows]

    if USE_POSTGRES:
        blobs = db.execute("SELECT id, data, content_type, created_at FROM file_blobs").fetchall()
        data["files"] = [
            {
                "id": b["id"],
                "contentType": b["content_type"],
                "createdAt": b["created_at"],
                "dataBase64": base64.b64encode(bytes(b["data"])).decode("ascii"),
            }
            for b in blobs
        ]
    else:
        # En SQLite local, los archivos viven como archivos sueltos dentro
        # de uploads/. Los incluimos igual en el respaldo (leyéndolos de
        # disco) para que este mismo .json sirva para restaurar fotos y
        # documentos también al importarlo en la versión en línea
        # (PostgreSQL/Neon), y no solo al revés.
        data["files"] = []
        for root, _dirs, filenames in os.walk(UPLOAD_DIR):
            for fname in filenames:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, UPLOAD_DIR).replace(os.sep, "/")
                try:
                    with open(full_path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                content_type = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "gif": "image/gif", "pdf": "application/pdf",
                }.get(ext, "application/octet-stream")
                data["files"].append({
                    "id": rel_path,
                    "contentType": content_type,
                    "createdAt": datetime.fromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%dT%H:%M:%S"),
                    "dataBase64": base64.b64encode(raw).decode("ascii"),
                })

    return json_lib.dumps(data, default=str).encode("utf-8")


@app.route("/api/backups/export", methods=["GET"])
@login_required
@admin_required
def export_full_backup():
    """Descarga toda la información de la clínica en un solo archivo JSON,
    incluyendo las fotos/documentos guardados en la base de datos (en base64).
    Funciona igual con SQLite o PostgreSQL — pensado especialmente para cuando
    se usa PostgreSQL (Render, Neon, etc.), donde no hay un solo archivo de
    base de datos que descargar directamente como en SQLite. Guarda este
    archivo periódicamente fuera de tu hosting (en tu PC, un USB, Drive) como
    respaldo real e independiente de cualquier proveedor."""
    db = get_db()
    payload = generate_backup_payload(db)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="respaldo_completo_{stamp}.json"'},
    )


@app.route("/api/backups/import", methods=["POST"])
@login_required
@admin_required
def import_full_backup():
    """Restaura toda la información desde un archivo generado por
    /api/backups/export. Reemplaza TODO lo que haya actualmente."""
    import base64
    import json as json_lib

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No se recibió ningún archivo."}), 400
    try:
        data = json_lib.loads(file.read().decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"El archivo no es un respaldo válido: {e}"}), 400

    db = get_db()
    for table in reversed(EXPORT_TABLES):
        db.execute(f"DELETE FROM {table}")
    if USE_POSTGRES:
        db.execute("DELETE FROM file_blobs")

    for table in EXPORT_TABLES:
        for row in data.get("tables", {}).get(table, []):
            cols = list(row.keys())
            cols_sql = ", ".join(cols)
            placeholders = ", ".join(["?"] * len(cols))
            db.execute(f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})", tuple(row[c] for c in cols))

    # Restaura los archivos (fotos, documentos) sin importar si el respaldo
    # viene de PostgreSQL/Neon y se importa en SQLite local, o al revés —
    # _save_uploaded_bytes ya sabe guardar en el lugar correcto según
    # dónde se esté ejecutando esta importación.
    for f in data.get("files", []):
        rel_path = f["id"]
        content = base64.b64decode(f["dataBase64"])
        _save_uploaded_bytes(rel_path, content, f.get("contentType", "application/octet-stream"))

    db.commit()
    session.clear()
    return jsonify({"ok": True, "message": "Respaldo restaurado. Vuelve a iniciar sesión."})


@app.route("/api/backups/<path:filename>", methods=["DELETE"])
@login_required
@admin_required
def delete_backup(filename):
    safe = secure_filename(filename)
    target = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(target):
        return jsonify({"error": "Respaldo no encontrado."}), 404
    if not _safe_remove(target, BACKUP_DIR):
        return jsonify({"error": "No se pudo eliminar el archivo (puede estar en uso)."}), 500
    if safe.startswith("clinica_") and safe.endswith(".db"):
        stamp = safe[len("clinica_"):-len(".db")]
        _safe_remove(os.path.join(BACKUP_DIR, f"uploads_{stamp}.zip"), BACKUP_DIR)
    return jsonify({"ok": True})


@app.route("/api/backups/sync-folder", methods=["POST"])
@login_required
@admin_required
def sync_backup_folder_now():
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no aplica."}), 400
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
    if USE_POSTGRES:
        return jsonify({"error": "Esta versión usa PostgreSQL — no aplica restaurar un respaldo de archivo SQLite."}), 400
    safe = secure_filename(filename)
    backup_path = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(backup_path):
        return jsonify({"error": "Respaldo no encontrado."}), 404

    data = request.get_json(silent=True) or {}
    restore_uploads = bool(data.get("restoreUploads"))

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
    start_email_backup_scheduler()
    print("\nClínica Dental corriendo en http://localhost:5000\n")
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=5000, threads=4)
    except ImportError:
        print("(Sugerencia: instala 'waitress' con pip install -r requirements.txt")
        print(" para un servidor más robusto con varios usuarios a la vez.)\n")
        app.run(host="127.0.0.1", port=5000, debug=False)
