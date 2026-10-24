"""
DermaPlanet - dermatology clinic website with online appointment booking.

Stack: Flask + SQLite (sqlite3 ships with Python, so there is nothing to install
or configure for the database - the file is created automatically on first run).
"""
import csv
import io
import os
import re
import hashlib
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
    session, send_from_directory, url_for,
)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:  # Pillow is optional: used only to shrink uploaded doctor photos
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_env_file(path):
    """Tiny .env loader so no extra dependency is needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(os.path.join(BASE_DIR, ".env"))

# Demo hosting on Vercel: the project folder is read-only there, only /tmp is writable.
# The SQLite file and uploads live in /tmp, so the demo starts fresh (with the sample
# doctors and the admin account) whenever Vercel starts a new instance.
ON_VERCEL = bool(os.environ.get("VERCEL"))
WRITABLE_DIR = "/tmp/dermaplanet" if ON_VERCEL else BASE_DIR

INSTANCE_DIR = os.path.join(WRITABLE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)


def get_secret_key():
    """Use SECRET_KEY from the environment, else create one and keep it on disk."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    if ON_VERCEL:  # stable across instances without any setup (demo only)
        seed = "dermaplanet-demo|" + os.environ.get("VERCEL_PROJECT_ID", "")
        return hashlib.sha256(seed.encode()).hexdigest()
    key_file = os.path.join(INSTANCE_DIR, "secret_key")
    if os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


app = Flask(__name__)
if ON_VERCEL:  # trust Vercel's proxy headers (real client IP, https)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=get_secret_key(),
    DATABASE=os.environ.get("DATABASE_PATH", os.path.join(INSTANCE_DIR, "dermaplanet.db")),
    UPLOAD_FOLDER=os.path.join(WRITABLE_DIR, "uploads"),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # 4 MB uploads (Vercel rejects bodies over 4.5 MB)
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1" if ON_VERCEL else "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@dermaplanet.com").strip().lower()
DEFAULT_ADMIN_PASSWORD = "Admin@12345"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)

CLINIC = {
    "name": "DermaPlanet",
    "address": "DermaPlanet Skin Clinic, Pune, Maharashtra, India",
    "phone": "+91 98765 43210",
    "email": "support@dermaplanet.com",
    "hours": "Monday to Saturday, 10:00 AM to 6:00 PM",
    "closed": "Closed on Sundays",
}

SERVICES = [
    ("Acne treatment", "Acne and breakouts",
     "A skin assessment, a treatment plan matched to your acne type, and follow-ups to adjust it."),
    ("Hair treatment", "Hair fall and scalp problems",
     "Scalp and hair analysis, dandruff and hair-fall care, and therapies to support regrowth."),
    ("Skin care", "Dull, dry or sensitive skin",
     "Routine planning, peels and facials supervised by a dermatologist, not a salon."),
    ("Laser treatment", "Hair removal, scars and marks",
     "Laser procedures performed by specialists, with a consultation before every course."),
    ("Pigmentation and scars", "Dark spots, melasma, acne marks",
     "Targeted creams, peels and procedures to even out skin tone and soften scars."),
    ("Allergies and eczema", "Rashes, itching, psoriasis",
     "Finding the trigger, calming flare-ups and building a routine to keep them away."),
]

# Booking rules
SLOT_START = (10, 0)
SLOT_END = (18, 0)          # last slot starts 17:30
SLOT_MINUTES = 30
LUNCH_BREAK = {"13:00", "13:30"}
BOOKING_WINDOW_DAYS = 60
ACTIVE_STATUSES = ("Pending", "Approved")
ALL_STATUSES = ("Pending", "Approved", "Rejected", "Completed", "Cancelled")

try:
    CLINIC_TZ = ZoneInfo(os.environ.get("CLINIC_TIMEZONE", "Asia/Kolkata")) if ZoneInfo else None
except Exception:  # tzdata missing (some Windows installs)
    CLINIC_TZ = None


def now():
    """Current local time in the clinic's timezone (naive datetime)."""
    if CLINIC_TZ:
        return datetime.now(CLINIC_TZ).replace(tzinfo=None)
    return datetime.now()


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    phone         TEXT,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctors(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    specialization TEXT NOT NULL,
    experience     TEXT NOT NULL,
    bio            TEXT,
    image          TEXT,
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS appointments(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    doctor_id        INTEGER NOT NULL REFERENCES doctors(id),
    patient_name     TEXT NOT NULL,
    email            TEXT NOT NULL,
    phone            TEXT NOT NULL,
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    problem          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'Pending'
                     CHECK(status IN ('Pending','Approved','Rejected','Completed','Cancelled')),
    admin_note       TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A doctor can only have one live appointment per slot (prevents double booking).
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_slot
    ON appointments(doctor_id, appointment_date, appointment_time)
    WHERE status IN ('Pending','Approved');

CREATE INDEX IF NOT EXISTS ix_appt_user ON appointments(user_id);
CREATE INDEX IF NOT EXISTS ix_appt_date ON appointments(appointment_date);

CREATE TABLE IF NOT EXISTS messages(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,
    message    TEXT NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect_db():
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = connect_db()
    if not ON_VERCEL:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)

    # Small migration so databases created by an earlier version keep working.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(appointments)")}
    if "admin_note" not in cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN admin_note TEXT")

    # Seed the two original doctors on a brand-new database.
    if conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO doctors(name, specialization, experience, bio, image) VALUES (?,?,?,?,?)",
            [
                ("Dr. Ananya Sharma", "Skin and laser treatment", "7+ years",
                 "Focuses on acne, pigmentation and laser procedures, with an emphasis on "
                 "explaining every option before starting treatment.",
                 "images/doctor1.jpg"),
                ("Dr. Rahul Patil", "Hair and laser treatment", "8+ years",
                 "Treats hair fall, scalp conditions and laser hair reduction, and follows "
                 "each patient's progress closely.",
                 "images/doctor2.jpg"),
            ],
        )

    # Create the first admin account (change the password after the first login).
    if conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] == 0:
        conn.execute(
            "INSERT OR IGNORE INTO users(name, email, phone, password_hash, is_admin) VALUES (?,?,?,?,1)",
            ("Admin", ADMIN_EMAIL, "", generate_password_hash(ADMIN_PASSWORD)),
        )
        if ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
            print(f"[DermaPlanet] Admin created: {ADMIN_EMAIL} / {DEFAULT_ADMIN_PASSWORD}  "
                  "(change this password after logging in)")
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Security helpers: CSRF, auth, headers, rate limiting
# --------------------------------------------------------------------------- #
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.context_processor
def inject_globals():
    def csrf_field():
        return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')
    return {"csrf_field": csrf_field, "clinic": CLINIC, "current_year": now().year}


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if g.user is None:
            session.clear()

    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, description="Your session expired. Please go back, refresh the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    if request.endpoint in ("login", "admin_login", "register", "profile"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Please log in as admin to continue.", "info")
            return redirect(url_for("admin_login"))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def safe_next(target):
    """Only allow redirects to paths on this site."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


_failed_logins = {}  # (ip, email) -> [timestamps]
MAX_ATTEMPTS, WINDOW_SECONDS = 5, 600


def too_many_attempts(key):
    cutoff = time.time() - WINDOW_SECONDS
    attempts = [t for t in _failed_logins.get(key, []) if t > cutoff]
    _failed_logins[key] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def record_failed_attempt(key):
    _failed_logins.setdefault(key, []).append(time.time())


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def clean_phone(raw):
    """Return a 10-digit Indian mobile number, or None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def password_problem(password):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one number."
    return None


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Template filters
# --------------------------------------------------------------------------- #
@app.template_filter("fdate")
def fdate(value):
    d = parse_date(value) if isinstance(value, str) else value
    return f"{d:%a}, {d.day} {d:%b %Y}" if d else value


@app.template_filter("ftime")
def ftime(value):
    try:
        t = datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError):
        return value
    return f"{t.hour % 12 or 12}:{t:%M} {t:%p}"


@app.template_filter("initials")
def initials(name):
    parts = [p for p in re.sub(r"^Dr\.?\s+", "", name or "").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "D"


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def all_slot_times():
    times, cur = [], datetime(2000, 1, 1, *SLOT_START)
    end = datetime(2000, 1, 1, *SLOT_END)
    while cur < end:
        label = cur.strftime("%H:%M")
        if label not in LUNCH_BREAK:
            times.append(label)
        cur += timedelta(minutes=SLOT_MINUTES)
    return times


def date_problem(d):
    """Return a message if the clinic can't take bookings on date d, else None."""
    today = now().date()
    if d is None:
        return "Please choose a valid date."
    if d < today:
        return "That date has passed. Please choose today or a later date."
    if d > today + timedelta(days=BOOKING_WINDOW_DAYS):
        return f"Bookings open up to {BOOKING_WINDOW_DAYS} days ahead."
    if d.weekday() == 6:
        return "The clinic is closed on Sundays. Please choose another day."
    return None


def available_slots(db, doctor_id, d, exclude_id=None):
    """Free HH:MM slots for a doctor on date d (a date object).

    exclude_id lets the admin reschedule an appointment without its own slot counting as taken.
    """
    if date_problem(d):
        return []
    taken = {
        row["appointment_time"] for row in db.execute(
            "SELECT appointment_time FROM appointments "
            "WHERE doctor_id = ? AND appointment_date = ? AND status IN ('Pending','Approved') "
            "AND id != ?",
            (doctor_id, d.isoformat(), exclude_id or 0),
        )
    }
    slots = [t for t in all_slot_times() if t not in taken]
    current = now()
    if d == current.date():
        cutoff = (current + timedelta(minutes=30)).strftime("%H:%M")
        slots = [t for t in slots if t > cutoff]
    return slots


def next_available(db):
    """The soonest open slot across all active doctors (shown on the home page)."""
    doctors = db.execute("SELECT * FROM doctors WHERE active = 1 ORDER BY id").fetchall()
    today = now().date()
    for offset in range(0, 15):
        d = today + timedelta(days=offset)
        for doc in doctors:
            slots = available_slots(db, doc["id"], d)
            if slots:
                return {"doctor": doc, "date": d.isoformat(), "time": slots[0]}
    return None


# --------------------------------------------------------------------------- #
# Public pages
# --------------------------------------------------------------------------- #
@app.route("/")
def home():
    db = get_db()
    doctors = db.execute("SELECT * FROM doctors WHERE active = 1 ORDER BY id LIMIT 3").fetchall()
    return render_template("index.html", doctors=doctors, services=SERVICES[:4],
                           next_slot=next_available(db))


@app.route("/static/uploads/<path:filename>")
def uploaded_file(filename):
    """Uploaded doctor photos (stored outside /static so the folder can be /tmp on Vercel)."""
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, max_age=3600)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/services")
def services():
    return render_template("services.html", services=SERVICES)


@app.route("/doctors")
def doctors():
    rows = get_db().execute("SELECT * FROM doctors WHERE active = 1 ORDER BY id").fetchall()
    return render_template("doctors.html", doctors=rows)


@app.route("/contact", methods=["GET", "POST"])
def contact():
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "email", "message")}
        error = None
        if not form["name"] or len(form["name"]) > 80:
            error = "Please enter your name."
        elif not EMAIL_RE.match(form["email"]):
            error = "Please enter a valid email address."
        elif not 10 <= len(form["message"]) <= 1500:
            error = "Your message should be between 10 and 1500 characters."
        if error:
            flash(error, "error")
        else:
            db = get_db()
            db.execute("INSERT INTO messages(name, email, message) VALUES (?,?,?)",
                       (form["name"], form["email"].lower(), form["message"]))
            db.commit()
            flash("Thanks for getting in touch. We will reply by email soon.", "success")
            return redirect(url_for("contact"))
    return render_template("contact.html", form=form)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "email", "phone")}
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        phone = clean_phone(form["phone"])
        error = None
        if not form["name"] or len(form["name"]) > 80:
            error = "Please enter your full name."
        elif not EMAIL_RE.match(form["email"]):
            error = "Please enter a valid email address."
        elif not phone:
            error = "Enter a valid 10-digit mobile number."
        elif password_problem(password):
            error = password_problem(password)
        elif password != confirm:
            error = "The two passwords do not match."

        if error:
            flash(error, "error")
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users(name, email, phone, password_hash) VALUES (?,?,?,?)",
                    (form["name"], form["email"].lower(), phone, generate_password_hash(password)),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("An account with this email already exists. Try logging in.", "error")
            else:
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))
    return render_template("register.html", form=form)


def handle_login(admin_only):
    if g.user and (g.user["is_admin"] or not admin_only):
        return redirect(url_for("admin_dashboard" if admin_only else "dashboard"))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        key = (request.remote_addr, email)
        if too_many_attempts(key):
            flash("Too many failed attempts. Please wait 10 minutes and try again.", "error")
        else:
            user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], password) \
                    and (user["is_admin"] or not admin_only):
                _failed_logins.pop(key, None)
                session.clear()                      # prevents session fixation
                session["user_id"] = user["id"]
                session.permanent = True
                flash(f"Welcome back, {user['name'].split()[0]}.", "success")
                if user["is_admin"] and admin_only:
                    return redirect(url_for("admin_dashboard"))
                return redirect(safe_next(request.args.get("next"))
                                or url_for("admin_dashboard" if user["is_admin"] else "dashboard"))
            record_failed_attempt(key)
            flash("Incorrect email or password.", "error")
    return render_template("admin_login.html" if admin_only else "login.html", email=email)


@app.route("/login", methods=["GET", "POST"])
def login():
    return handle_login(admin_only=False)


@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    return handle_login(admin_only=True)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = clean_phone(request.form.get("phone", "")) if not g.user["is_admin"] \
            else request.form.get("phone", "").strip()
        if not name or len(name) > 80:
            flash("Please enter your name.", "error")
        elif not g.user["is_admin"] and not phone:
            flash("Enter a valid 10-digit mobile number.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name, phone, g.user["id"]))
            db.commit()
            flash("Profile updated.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html")


@app.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current", "")
    new = request.form.get("new", "")
    confirm = request.form.get("confirm", "")
    if not check_password_hash(g.user["password_hash"], current):
        flash("Your current password is incorrect.", "error")
    elif password_problem(new):
        flash(password_problem(new), "error")
    elif new != confirm:
        flash("The new passwords do not match.", "error")
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(new), g.user["id"]))
        db.commit()
        flash("Password changed.", "success")
    return redirect(url_for("profile"))


# --------------------------------------------------------------------------- #
# Patient area
# --------------------------------------------------------------------------- #
APPT_SELECT = (
    "SELECT a.*, d.name AS doctor_name, d.specialization AS doctor_spec "
    "FROM appointments a JOIN doctors d ON d.id = a.doctor_id "
)


@app.route("/dashboard")
@login_required
def dashboard():
    if g.user["is_admin"]:
        return redirect(url_for("admin_dashboard"))
    db = get_db()
    today = now().date().isoformat()
    upcoming = db.execute(
        APPT_SELECT + "WHERE a.user_id = ? AND a.status IN ('Pending','Approved') "
        "AND a.appointment_date >= ? ORDER BY a.appointment_date, a.appointment_time",
        (g.user["id"], today),
    ).fetchall()
    counts = {row["status"]: row["n"] for row in db.execute(
        "SELECT status, COUNT(*) AS n FROM appointments WHERE user_id = ? GROUP BY status",
        (g.user["id"],))}
    return render_template("dashboard.html", upcoming=upcoming, counts=counts,
                           total=sum(counts.values()), today=today)


@app.route("/appointment", methods=["GET", "POST"])
@login_required
def appointment():
    db = get_db()
    doctors_list = db.execute("SELECT * FROM doctors WHERE active = 1 ORDER BY name").fetchall()
    today = now().date()
    form = {
        "name": g.user["name"], "email": g.user["email"], "phone": g.user["phone"] or "",
        "doctor": request.args.get("doctor", ""), "date": "", "time": "", "problem": "",
    }

    if request.method == "POST":
        form = {k: request.form.get(k, "").strip()
                for k in ("name", "email", "phone", "doctor", "date", "time", "problem")}
        phone = clean_phone(form["phone"])
        doctor = db.execute("SELECT * FROM doctors WHERE id = ? AND active = 1",
                            (form["doctor"],)).fetchone() if form["doctor"].isdigit() else None
        d = parse_date(form["date"])
        error = None
        if not form["name"] or len(form["name"]) > 80:
            error = "Please enter the patient's name."
        elif not EMAIL_RE.match(form["email"]):
            error = "Please enter a valid email address."
        elif not phone:
            error = "Enter a valid 10-digit mobile number."
        elif doctor is None:
            error = "Please choose a doctor."
        elif date_problem(d):
            error = date_problem(d)
        elif form["time"] not in available_slots(db, doctor["id"], d):
            error = "That time is no longer available. Please pick another slot."
        elif not 5 <= len(form["problem"]) <= 1000:
            error = "Describe your concern in 5 to 1000 characters."

        if error:
            flash(error, "error")
        else:
            try:
                db.execute(
                    "INSERT INTO appointments(user_id, doctor_id, patient_name, email, phone, "
                    "appointment_date, appointment_time, problem) VALUES (?,?,?,?,?,?,?,?)",
                    (g.user["id"], doctor["id"], form["name"], form["email"].lower(), phone,
                     d.isoformat(), form["time"], form["problem"]),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("Someone just booked that slot. Please pick another time.", "error")
            else:
                flash(f"Request sent for {fdate(d)} at {ftime(form['time'])}. "
                      "You will see the status here once the clinic confirms.", "success")
                return redirect(url_for("my_appointments"))

    return render_template(
        "appointment.html", doctors=doctors_list, form=form,
        min_date=today.isoformat(),
        max_date=(today + timedelta(days=BOOKING_WINDOW_DAYS)).isoformat(),
    )


@app.route("/api/slots")
def api_slots():
    doctor_id = request.args.get("doctor", "")
    d = parse_date(request.args.get("date", ""))
    if not doctor_id.isdigit():
        return jsonify(slots=[], message="Choose a doctor first.")
    problem = date_problem(d)
    if problem:
        return jsonify(slots=[], message=problem)
    exclude = request.args.get("exclude", "")
    exclude_id = int(exclude) if exclude.isdigit() and g.user and g.user["is_admin"] else None
    slots = available_slots(get_db(), int(doctor_id), d, exclude_id)
    message = "" if slots else "No free slots on this date. Try another day."
    return jsonify(slots=[{"value": t, "label": ftime(t)} for t in slots], message=message)


@app.route("/myappointments")        # old URL kept so existing links still work
@app.route("/my-appointments")
@login_required
def my_appointments():
    rows = get_db().execute(
        APPT_SELECT + "WHERE a.user_id = ? ORDER BY a.appointment_date DESC, a.appointment_time DESC",
        (g.user["id"],),
    ).fetchall()
    return render_template("myappointments.html", appointments=rows,
                           today=now().date().isoformat())


@app.route("/appointments/<int:appt_id>/cancel", methods=["POST"])
@login_required
def cancel_appointment(appt_id):
    db = get_db()
    appt = db.execute("SELECT * FROM appointments WHERE id = ? AND user_id = ?",
                      (appt_id, g.user["id"])).fetchone()
    if appt is None:
        abort(404)
    if appt["status"] not in ACTIVE_STATUSES or appt["appointment_date"] < now().date().isoformat():
        flash("This appointment can no longer be cancelled.", "error")
    else:
        db.execute("UPDATE appointments SET status = 'Cancelled' WHERE id = ?", (appt_id,))
        db.commit()
        flash("Appointment cancelled. The slot is open for others.", "info")
    return redirect(url_for("my_appointments"))


# --------------------------------------------------------------------------- #
# Admin area
# --------------------------------------------------------------------------- #
@app.route("/admin")
@admin_required
def admin_home():
    return redirect(url_for("admin_dashboard"))


def appointment_query():
    """Build the filtered appointment query used by the dashboard and the CSV export."""
    status = request.args.get("status", "")
    when = request.args.get("when", "")
    q = request.args.get("q", "").strip()
    today = now().date().isoformat()

    sql, params = APPT_SELECT + "WHERE 1=1 ", []
    if status in ALL_STATUSES:
        sql += "AND a.status = ? "
        params.append(status)
    if when == "today":
        sql += "AND a.appointment_date = ? "
        params.append(today)
    elif when == "upcoming":
        sql += "AND a.appointment_date >= ? "
        params.append(today)
    if q:
        sql += "AND (a.patient_name LIKE ? OR a.email LIKE ? OR a.phone LIKE ?) "
        params += [f"%{q}%"] * 3
    sql += "ORDER BY a.appointment_date DESC, a.appointment_time DESC"
    return sql, params, status, when, q, today


@app.route("/admin-dashboard")
@admin_required
def admin_dashboard():
    db = get_db()
    sql, params, status, when, q, today = appointment_query()
    appointments = db.execute(sql + " LIMIT 200", params).fetchall()

    stats = {
        "patients": db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 0").fetchone()[0],
        "pending": db.execute("SELECT COUNT(*) FROM appointments WHERE status = 'Pending'").fetchone()[0],
        "today": db.execute(
            "SELECT COUNT(*) FROM appointments WHERE appointment_date = ? AND status IN ('Pending','Approved')",
            (today,)).fetchone()[0],
        "doctors": db.execute("SELECT COUNT(*) FROM doctors WHERE active = 1").fetchone()[0],
        "unread": db.execute("SELECT COUNT(*) FROM messages WHERE is_read = 0").fetchone()[0],
    }
    status_counts = {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) AS n FROM appointments GROUP BY status")}
    default_pw = check_password_hash(g.user["password_hash"], DEFAULT_ADMIN_PASSWORD)
    return render_template("admin/dashboard.html", appointments=appointments, stats=stats,
                           statuses=ALL_STATUSES, status=status, when=when, q=q,
                           status_counts=status_counts, default_pw=default_pw, today=today)


def csv_safe(value):
    """Stop spreadsheet apps from running a cell that starts with = + - @ as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.route("/admin/appointments/export.csv")
@admin_required
def admin_export_appointments():
    sql, params, *_ = appointment_query()
    rows = get_db().execute(sql, params).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ID", "Patient", "Email", "Phone", "Doctor", "Date", "Time", "Status",
                     "Concern", "Clinic note", "Booked at"])
    for a in rows:
        writer.writerow([a["id"], *(csv_safe(a[k]) for k in (
            "patient_name", "email", "phone", "doctor_name", "appointment_date",
            "appointment_time", "status", "problem", "admin_note", "created_at"))])
    filename = f"appointments-{now().date().isoformat()}.csv"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/admin/appointments/<int:appt_id>", methods=["GET", "POST"])
@admin_required
def admin_appointment(appt_id):
    db = get_db()
    appt = db.execute(APPT_SELECT + "WHERE a.id = ?", (appt_id,)).fetchone()
    if appt is None:
        abort(404)
    doctors_list = db.execute(
        "SELECT * FROM doctors WHERE active = 1 OR id = ? ORDER BY name", (appt["doctor_id"],)).fetchall()
    form = {"doctor": str(appt["doctor_id"]), "date": appt["appointment_date"],
            "time": appt["appointment_time"]}

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "note":
            note = request.form.get("note", "").strip()
            if len(note) > 500:
                flash("Keep the note under 500 characters.", "error")
            else:
                db.execute("UPDATE appointments SET admin_note = ? WHERE id = ?", (note or None, appt_id))
                db.commit()
                flash("Note saved. The patient can see it on their appointments page.", "success")
                return redirect(url_for("admin_appointment", appt_id=appt_id))
        elif action == "reschedule":
            form = {k: request.form.get(k, "").strip() for k in ("doctor", "date", "time")}
            doctor = db.execute("SELECT * FROM doctors WHERE id = ?", (form["doctor"],)).fetchone() \
                if form["doctor"].isdigit() else None
            d = parse_date(form["date"])
            error = None
            if appt["status"] not in ACTIVE_STATUSES:
                error = f"A {appt['status'].lower()} appointment cannot be rescheduled."
            elif doctor is None:
                error = "Please choose a doctor."
            elif date_problem(d):
                error = date_problem(d)
            elif form["time"] not in available_slots(db, doctor["id"], d, exclude_id=appt_id):
                error = "That time is not available. Pick another slot."
            if error:
                flash(error, "error")
            else:
                try:
                    db.execute("UPDATE appointments SET doctor_id=?, appointment_date=?, appointment_time=? "
                               "WHERE id=?", (doctor["id"], d.isoformat(), form["time"], appt_id))
                    db.commit()
                except sqlite3.IntegrityError:
                    flash("Someone just took that slot. Pick another time.", "error")
                else:
                    flash("Appointment rescheduled.", "success")
                    return redirect(url_for("admin_appointment", appt_id=appt_id))
        else:
            abort(400)
        appt = db.execute(APPT_SELECT + "WHERE a.id = ?", (appt_id,)).fetchone()

    today = now().date()
    return render_template("admin/appointment.html", a=appt, doctors=doctors_list, form=form,
                           min_date=today.isoformat(),
                           max_date=(today + timedelta(days=BOOKING_WINDOW_DAYS)).isoformat())


@app.route("/admin/appointments/<int:appt_id>/status", methods=["POST"])
@admin_required
def admin_set_status(appt_id):
    new_status = request.form.get("status", "")
    if new_status not in ALL_STATUSES:
        abort(400)
    db = get_db()
    try:
        cur = db.execute("UPDATE appointments SET status = ? WHERE id = ?", (new_status, appt_id))
        db.commit()
    except sqlite3.IntegrityError:
        flash("Another live appointment already holds that slot, so this one can't be reopened.", "error")
    else:
        if cur.rowcount == 0:
            abort(404)
        flash(f"Appointment marked {new_status.lower()}.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/appointments/<int:appt_id>/delete", methods=["POST"])
@admin_required
def admin_delete_appointment(appt_id):
    db = get_db()
    db.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
    db.commit()
    flash("Appointment deleted.", "info")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/patients")
@admin_required
def admin_patients():
    rows = get_db().execute(
        "SELECT u.*, COUNT(a.id) AS visits FROM users u "
        "LEFT JOIN appointments a ON a.user_id = u.id "
        "WHERE u.is_admin = 0 GROUP BY u.id ORDER BY u.created_at DESC"
    ).fetchall()
    return render_template("admin/patients.html", patients=rows)


@app.route("/admin/patients/<int:user_id>")
@admin_required
def admin_patient(user_id):
    db = get_db()
    patient = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (user_id,)).fetchone()
    if patient is None:
        abort(404)
    appts = db.execute(APPT_SELECT + "WHERE a.user_id = ? ORDER BY a.appointment_date DESC, "
                       "a.appointment_time DESC", (user_id,)).fetchall()
    return render_template("admin/patient.html", patient=patient, appointments=appts)


@app.route("/admin/patients/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_patient(user_id):
    db = get_db()
    patient = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (user_id,)).fetchone()
    if patient is None:
        abort(404)
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))   # their appointments go with them
    db.commit()
    flash(f"{patient['name']} and their appointments were deleted.", "info")
    return redirect(url_for("admin_patients"))


@app.route("/admin/staff", methods=["GET", "POST"])
@admin_required
def admin_staff():
    db = get_db()
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "email")}
        password = request.form.get("password", "")
        error = None
        if not form["name"] or len(form["name"]) > 80:
            error = "Please enter the staff member's name."
        elif not EMAIL_RE.match(form["email"]):
            error = "Please enter a valid email address."
        elif password_problem(password):
            error = password_problem(password)
        if error:
            flash(error, "error")
        else:
            try:
                db.execute("INSERT INTO users(name, email, phone, password_hash, is_admin) VALUES (?,?,?,?,1)",
                           (form["name"], form["email"].lower(), "", generate_password_hash(password)))
                db.commit()
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
            else:
                flash(f"Admin account created for {form['name']}. Share the password with them securely.",
                      "success")
                return redirect(url_for("admin_staff"))
    admins = db.execute("SELECT * FROM users WHERE is_admin = 1 ORDER BY created_at").fetchall()
    return render_template("admin/staff.html", admins=admins, form=form)


@app.route("/admin/staff/<int:user_id>/remove", methods=["POST"])
@admin_required
def admin_remove_staff(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 1", (user_id,)).fetchone()
    if target is None:
        abort(404)
    if target["id"] == g.user["id"]:
        flash("You cannot remove your own account while logged in.", "error")
    elif db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] <= 1:
        flash("At least one admin account must remain.", "error")
    else:
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        flash(f"{target['name']} was removed.", "info")
    return redirect(url_for("admin_staff"))


@app.route("/admin/messages")
@admin_required
def admin_messages():
    rows = get_db().execute("SELECT * FROM messages ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("admin/messages.html", messages=rows)


@app.route("/admin/messages/<int:msg_id>/read", methods=["POST"])
@admin_required
def admin_message_read(msg_id):
    db = get_db()
    db.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (msg_id,))
    db.commit()
    return redirect(url_for("admin_messages"))


@app.route("/admin/messages/<int:msg_id>/delete", methods=["POST"])
@admin_required
def admin_message_delete(msg_id):
    db = get_db()
    db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    db.commit()
    flash("Message deleted.", "info")
    return redirect(url_for("admin_messages"))


# ---- Doctors (admin) ------------------------------------------------------- #
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def save_doctor_image(file_storage):
    """Validate and store an uploaded photo. Returns a path relative to /static, or None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(secure_filename(file_storage.filename))[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("Photo must be a JPG, PNG or WebP image.")
    name = uuid.uuid4().hex
    if Image is not None:
        try:
            img = Image.open(file_storage.stream)
            img.verify()
            file_storage.stream.seek(0)
            img = Image.open(file_storage.stream).convert("RGB")
        except Exception as exc:
            raise ValueError("That file is not a valid image.") from exc
        img.thumbnail((900, 900))
        filename = f"{name}.jpg"
        img.save(os.path.join(app.config["UPLOAD_FOLDER"], filename), "JPEG", quality=85, optimize=True)
    else:
        filename = f"{name}{ext}"
        file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    return f"uploads/{filename}"


def remove_uploaded_image(rel_path):
    """Delete a photo only if it is one we uploaded (never the bundled images)."""
    if rel_path and rel_path.startswith("uploads/"):
        path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(rel_path))
        if os.path.exists(path):
            os.remove(path)


def doctor_form_values():
    return {k: request.form.get(k, "").strip() for k in ("name", "specialization", "experience", "bio")}


def doctor_form_error(vals):
    if not vals["name"] or not vals["specialization"] or not vals["experience"]:
        return "Name, specialization and experience are required."
    if len(vals["name"]) > 80 or len(vals["specialization"]) > 100 or len(vals["experience"]) > 40:
        return "One of the fields is too long."
    if len(vals["bio"]) > 600:
        return "Keep the short bio under 600 characters."
    return None


@app.route("/admin/doctors")
@admin_required
def admin_doctors():
    rows = get_db().execute(
        "SELECT d.*, (SELECT COUNT(*) FROM appointments a WHERE a.doctor_id = d.id) AS appt_count "
        "FROM doctors d ORDER BY d.active DESC, d.name"
    ).fetchall()
    return render_template("admin/doctors.html", doctors=rows)


@app.route("/add-doctor", methods=["GET", "POST"])   # old URL kept
@app.route("/admin/doctors/new", methods=["GET", "POST"])
@admin_required
def add_doctor():
    vals = {}
    if request.method == "POST":
        vals = doctor_form_values()
        error = doctor_form_error(vals)
        image = None
        if not error:
            try:
                image = save_doctor_image(request.files.get("image"))
            except ValueError as exc:
                error = str(exc)
        if error:
            flash(error, "error")
        else:
            db = get_db()
            db.execute(
                "INSERT INTO doctors(name, specialization, experience, bio, image) VALUES (?,?,?,?,?)",
                (vals["name"], vals["specialization"], vals["experience"], vals["bio"], image),
            )
            db.commit()
            flash(f"{vals['name']} was added.", "success")
            return redirect(url_for("admin_doctors"))
    return render_template("admin/doctor_form.html", doctor=None, vals=vals)


@app.route("/admin/doctors/<int:doc_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_doctor(doc_id):
    db = get_db()
    doctor = db.execute("SELECT * FROM doctors WHERE id = ?", (doc_id,)).fetchone()
    if doctor is None:
        abort(404)
    vals = dict(doctor)
    if request.method == "POST":
        vals = doctor_form_values()
        error = doctor_form_error(vals)
        image = doctor["image"]
        new_image = None
        if not error:
            try:
                new_image = save_doctor_image(request.files.get("image"))
            except ValueError as exc:
                error = str(exc)
        if error:
            flash(error, "error")
        else:
            if new_image:
                remove_uploaded_image(doctor["image"])
                image = new_image
            db.execute(
                "UPDATE doctors SET name=?, specialization=?, experience=?, bio=?, image=?, active=? WHERE id=?",
                (vals["name"], vals["specialization"], vals["experience"], vals["bio"], image,
                 1 if request.form.get("active") else 0, doc_id),
            )
            db.commit()
            flash("Doctor details saved.", "success")
            return redirect(url_for("admin_doctors"))
    return render_template("admin/doctor_form.html", doctor=doctor, vals=vals)


@app.route("/delete-doctor/<int:doc_id>", methods=["POST"])   # old URL kept
@app.route("/admin/doctors/<int:doc_id>/delete", methods=["POST"])
@admin_required
def delete_doctor(doc_id):
    db = get_db()
    doctor = db.execute("SELECT * FROM doctors WHERE id = ?", (doc_id,)).fetchone()
    if doctor is None:
        abort(404)
    has_history = db.execute("SELECT 1 FROM appointments WHERE doctor_id = ? LIMIT 1", (doc_id,)).fetchone()
    if has_history:
        db.execute("UPDATE doctors SET active = 0 WHERE id = ?", (doc_id,))
        flash(f"{doctor['name']} has appointment history, so they were hidden from the site "
              "instead of deleted.", "info")
    else:
        db.execute("DELETE FROM doctors WHERE id = ?", (doc_id,))
        remove_uploaded_image(doctor["image"])
        flash(f"{doctor['name']} was deleted.", "info")
    db.commit()
    return redirect(url_for("admin_doctors"))


# --------------------------------------------------------------------------- #
# Error pages
# --------------------------------------------------------------------------- #
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def handle_error(err):
    code = getattr(err, "code", 500)
    messages = {
        400: getattr(err, "description", "That request could not be processed."),
        403: "You do not have permission to open this page.",
        404: "We could not find that page. It may have moved.",
        413: "That file is too large. Photos can be up to 4 MB.",
        500: "Something went wrong on our side. Please try again in a moment.",
    }
    return render_template("errors/error.html", code=code, message=messages.get(code, "Error")), code


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1",
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)))
