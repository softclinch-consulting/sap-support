"""
SAP AMS Help Desk
=================
A self-hosted help desk for SAP application support (AMS), built with
Flask + SQLite. One dependency: Flask.

Run:
    pip install -r requirements.txt
    python app.py               # start on http://127.0.0.1:5000
    python app.py --demo        # load demo companies, users, tickets and KB first

Roles
    client      - customer user: raises tickets for their company, comments,
                  accepts or reopens resolutions, reads public KB articles
    consultant  - SAP consultant: works every ticket, internal notes,
                  transports / SAP Notes, writes KB articles, sees reports
    admin       - everything above + users, client companies, SLA policy
"""
import argparse
import csv
import functools
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------
# Configuration (override with environment variables)
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("HELPDESK_DB", os.path.join(BASE_DIR, "helpdesk.db"))
UPLOAD_DIR = os.environ.get("HELPDESK_UPLOADS", os.path.join(BASE_DIR, "uploads"))
APP_NAME = os.environ.get("HELPDESK_NAME", "SoftClinch AMS Desk")
LOCAL_TZ = ZoneInfo(os.environ.get("HELPDESK_TZ", "Asia/Kolkata"))
AUTO_ASSIGN = os.environ.get("HELPDESK_AUTO_ASSIGN", "1") == "1"

SAP_MODULES = [
    ("FI", "Financial Accounting"), ("CO", "Controlling"),
    ("MM", "Materials Management"), ("SD", "Sales & Distribution"),
    ("PP", "Production Planning"), ("QM", "Quality Management"),
    ("PM", "Plant Maintenance"), ("WM/EWM", "Warehouse Management"),
    ("PS", "Project System"), ("HCM", "HCM / Payroll"),
    ("SuccessFactors", "SuccessFactors"), ("ABAP", "ABAP / Custom Development"),
    ("Basis", "Basis / Technical"), ("Security", "Security & GRC"),
    ("BW/BI", "BW / Analytics"), ("Fiori", "Fiori / UI5"),
    ("Integration", "PI/PO, CPI, IDocs"), ("Other", "Other"),
]
MODULE_CODES = [m[0] for m in SAP_MODULES]
CATEGORIES = ["Incident", "Service Request", "Change Request", "Problem", "Authorization / Access"]
CATEGORY_PREFIX = {"Incident": "INC", "Service Request": "SR", "Change Request": "CR",
                   "Problem": "PRB", "Authorization / Access": "ACC"}
PRIORITIES = ["P1", "P2", "P3", "P4"]
PRIORITY_LABELS = {"P1": "Critical", "P2": "High", "P3": "Medium", "P4": "Low"}
PRIORITY_HELP = {
    "P1": "Production down or business-critical process stopped, no workaround",
    "P2": "Major function impaired for many users, workaround is difficult",
    "P3": "Function impaired for some users, workaround exists",
    "P4": "Question, minor issue or cosmetic defect",
}
STATUSES = ["New", "Assigned", "In Progress", "Awaiting Customer", "Awaiting SAP",
            "In Testing", "Resolved", "Closed"]
OPEN_STATUSES = STATUSES[:6]
DONE_STATUSES = ("Resolved", "Closed")
PAUSED_STATUSES = {"Awaiting Customer", "Awaiting SAP"}   # resolution SLA clock stops
SAP_SYSTEMS = ["PRD", "QAS", "DEV", "SBX"]
ROLES = ["client", "consultant", "admin"]
DEFAULT_SLA = {"P1": (0.5, 4), "P2": (1, 8), "P3": (4, 24), "P4": (8, 72)}  # hours
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "pdf", "txt", "log", "csv", "xlsx",
               "xls", "docx", "doc", "pptx", "zip", "msg", "eml", "xml", "json"}

app = Flask(__name__)


def _secret_key():
    env = os.environ.get("HELPDESK_SECRET_KEY")
    if env:
        return env
    path = os.path.join(BASE_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


app.config.update(
    SECRET_KEY=_secret_key(),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, contact_name TEXT, contact_email TEXT,
  sap_release TEXT, notes TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL COLLATE NOCASE,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('client','consultant','admin')),
  company_id INTEGER REFERENCES companies(id), modules TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sla_policies(
  priority TEXT PRIMARY KEY, response_hours REAL NOT NULL, resolution_hours REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL,
  company_id INTEGER NOT NULL REFERENCES companies(id), reporter_id INTEGER REFERENCES users(id),
  assignee_id INTEGER REFERENCES users(id), module TEXT NOT NULL, category TEXT NOT NULL,
  priority TEXT NOT NULL, status TEXT NOT NULL, sap_system TEXT, sap_client TEXT, tcode TEXT,
  error_message TEXT, business_impact TEXT, users_affected TEXT, transport_request TEXT,
  sap_note TEXT, root_cause TEXT, resolution TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, response_due TEXT NOT NULL,
  resolution_hours REAL NOT NULL, first_response_at TEXT, paused_at TEXT,
  paused_seconds REAL NOT NULL DEFAULT 0, resolved_at TEXT, closed_at TEXT,
  reopen_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), body TEXT NOT NULL, internal INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  comment_id INTEGER REFERENCES comments(id) ON DELETE SET NULL, user_id INTEGER REFERENCES users(id),
  filename TEXT NOT NULL, stored_name TEXT NOT NULL, size INTEGER,
  internal INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), field TEXT NOT NULL, old_value TEXT, new_value TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_articles(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, module TEXT NOT NULL, tcode TEXT,
  symptoms TEXT NOT NULL, solution TEXT NOT NULL, tags TEXT,
  visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('public','internal')),
  source_ticket_id INTEGER REFERENCES tickets(id) ON DELETE SET NULL,
  author_id INTEGER REFERENCES users(id), views INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tickets_company ON tickets(company_id);
CREATE INDEX IF NOT EXISTS ix_tickets_assignee ON tickets(assignee_id);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_comments_ticket ON comments(ticket_id);
CREATE INDEX IF NOT EXISTS ix_history_ticket ON history(ticket_id);
"""


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    con = connect()
    con.executescript(SCHEMA)
    for p, (resp, res) in DEFAULT_SLA.items():
        con.execute("INSERT OR IGNORE INTO sla_policies VALUES(?,?,?)", (p, resp, res))
    con.commit()
    con.close()


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat() if dt else None


def parse(s):
    if not s:
        return None
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def fmt_duration(seconds):
    seconds = int(abs(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


@app.template_filter("dt")
def filter_dt(value, fmt="%d %b %Y, %H:%M"):
    dt = parse(value)
    return dt.astimezone(LOCAL_TZ).strftime(fmt) if dt else "—"


@app.template_filter("ago")
def filter_ago(value):
    dt = parse(value)
    if not dt:
        return "—"
    secs = (utcnow() - dt).total_seconds()
    if secs < 60:
        return "just now"
    return fmt_duration(secs) + " ago"


@app.template_filter("hrs")
def filter_hrs(h):
    h = float(h or 0)
    return f"{round(h * 60)} min" if h < 1 else f"{h:g} h"


@app.template_filter("filesize")
def filter_filesize(n):
    n = n or 0
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------
# SLA engine
# --------------------------------------------------------------------------
def get_sla(db):
    return {r["priority"]: r for r in db.execute("SELECT * FROM sla_policies")}


def resolution_due(t, now=None):
    """Resolution deadline, pushed back by time spent in a paused status."""
    now = now or utcnow()
    paused = t["paused_seconds"] or 0
    if t["paused_at"]:
        paused += (now - parse(t["paused_at"])).total_seconds()
    return parse(t["created_at"]) + timedelta(hours=t["resolution_hours"], seconds=paused)


def sla_info(t, now=None):
    now = now or utcnow()
    rdue = parse(t["response_due"])
    if t["first_response_at"]:
        response = "met" if parse(t["first_response_at"]) <= rdue else "breached"
    elif t["status"] in DONE_STATUSES:
        response = "met" if parse(t["resolved_at"] or t["closed_at"]) <= rdue else "breached"
    else:
        response = "breached" if now > rdue else "pending"

    due = resolution_due(t, now)
    total = t["resolution_hours"] * 3600
    if t["resolved_at"]:
        state = "met" if parse(t["resolved_at"]) <= due else "breached"
        text = "Met" if state == "met" else f"Missed by {fmt_duration((parse(t['resolved_at']) - due).total_seconds())}"
    elif t["paused_at"]:
        state, text = "paused", f"Paused · {fmt_duration((due - now).total_seconds())} left"
    elif now > due:
        state, text = "breached", f"Overdue {fmt_duration((now - due).total_seconds())}"
    else:
        left = (due - now).total_seconds()
        state = "at_risk" if left < max(total * 0.25, 3600) else "on_track"
        text = f"{fmt_duration(left)} left"
    return {"response": response, "response_due": rdue, "resolution": state,
            "resolution_due": due, "text": text,
            "sort_key": 0 if state == "breached" else 1 if state == "at_risk" else 2}


# --------------------------------------------------------------------------
# Auth, CSRF, access control
# --------------------------------------------------------------------------
@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute(
            "SELECT u.*, c.name AS company_name FROM users u LEFT JOIN companies c ON c.id=u.company_id "
            "WHERE u.id=? AND u.active=1", (uid,)).fetchone()
        if g.user is None:
            session.clear()


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or token != request.form.get("_csrf"):
            abort(400, "Your form expired. Go back, reload the page and try again.")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def is_staff():
    return g.user is not None and g.user["role"] in ("consultant", "admin")


def is_admin():
    return g.user is not None and g.user["role"] == "admin"


# Template globals (also visible inside imported macros)
app.jinja_env.globals.update(
    app_name=APP_NAME, csrf_token=csrf_token, is_staff=is_staff, is_admin=is_admin,
    MODULES=SAP_MODULES, CATEGORIES=CATEGORIES, PRIORITIES=PRIORITIES,
    PRIORITY_LABELS=PRIORITY_LABELS, PRIORITY_HELP=PRIORITY_HELP,
    STATUSES=STATUSES, SAP_SYSTEMS=SAP_SYSTEMS, ROLES=ROLES)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path))
        return view(*a, **kw)
    return wrapped


def roles_required(*roles):
    def deco(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if g.user["role"] not in roles:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


staff_required = roles_required("consultant", "admin")
admin_required = roles_required("admin")


def scope():
    """SQL fragment restricting tickets to what the current user may see."""
    if g.user["role"] == "client":
        return " AND t.company_id = ?", [g.user["company_id"]]
    return "", []


TICKET_SELECT = """
SELECT t.*, c.name AS company_name, r.name AS reporter_name, a.name AS assignee_name
FROM tickets t JOIN companies c ON c.id = t.company_id
LEFT JOIN users r ON r.id = t.reporter_id LEFT JOIN users a ON a.id = t.assignee_id
WHERE 1=1"""


def get_ticket_or_404(tid):
    s, p = scope()
    t = get_db().execute(TICKET_SELECT + " AND t.id=?" + s, [tid, *p]).fetchone()
    if t is None:
        abort(404)
    return t


def split_modules(s):
    return [m for m in (s or "").split(",") if m]


# --------------------------------------------------------------------------
# Ticket operations (shared by routes and demo seeding)
# --------------------------------------------------------------------------
TRACKED_FIELDS = {"status", "priority", "assignee_id", "module", "category",
                  "transport_request", "sap_note", "title"}
FIELD_LABELS = {"assignee_id": "assignee", "transport_request": "transport request",
                "sap_note": "SAP Note"}


def user_name(db, uid):
    if not uid:
        return None
    r = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    return r["name"] if r else None


def log_history(db, tid, uid, field, old, new, now):
    db.execute("INSERT INTO history(ticket_id,user_id,field,old_value,new_value,created_at) "
               "VALUES(?,?,?,?,?,?)", (tid, uid, field, old, new, iso(now)))


def auto_assign(db, module):
    if not AUTO_ASSIGN:
        return None
    rows = db.execute(
        "SELECT u.id, u.modules, (SELECT COUNT(*) FROM tickets t WHERE t.assignee_id=u.id "
        "AND t.status NOT IN ('Resolved','Closed')) AS load FROM users u "
        "WHERE u.role='consultant' AND u.active=1").fetchall()
    cands = [r for r in rows if module in split_modules(r["modules"])]
    return min(cands, key=lambda r: (r["load"], r["id"]))["id"] if cands else None


def create_ticket(db, f, company_id, reporter_id, now):
    pol = get_sla(db)[f["priority"]]
    assignee = f.get("assignee_id") or auto_assign(db, f["module"])
    status = "Assigned" if assignee else "New"
    cur = db.execute(
        """INSERT INTO tickets(title,description,company_id,reporter_id,assignee_id,module,category,
           priority,status,sap_system,sap_client,tcode,error_message,business_impact,users_affected,
           created_at,updated_at,response_due,resolution_hours)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f["title"], f["description"], company_id, reporter_id, assignee, f["module"], f["category"],
         f["priority"], status, f.get("sap_system"), f.get("sap_client"),
         (f.get("tcode") or "").upper() or None, f.get("error_message"), f.get("business_impact"),
         f.get("users_affected"), iso(now), iso(now),
         iso(now + timedelta(hours=pol["response_hours"])), pol["resolution_hours"]))
    tid = cur.lastrowid
    db.execute("UPDATE tickets SET number=? WHERE id=?",
               (f"{CATEGORY_PREFIX.get(f['category'], 'TKT')}-{tid:06d}", tid))
    log_history(db, tid, reporter_id, "created", None, status, now)
    if assignee:
        log_history(db, tid, None, "assignee", None, user_name(db, assignee) + " (auto)", now)
    return tid


def status_changes(t, new_status, now):
    """Side effects of a status change: SLA pause/resume, resolved/closed stamps, reopen count."""
    old = t["status"]
    if old == new_status:
        return {}
    ch = {"status": new_status}
    if t["paused_at"] and new_status not in PAUSED_STATUSES:
        ch["paused_seconds"] = (t["paused_seconds"] or 0) + (now - parse(t["paused_at"])).total_seconds()
        ch["paused_at"] = None
    if new_status in PAUSED_STATUSES and not t["paused_at"]:
        ch["paused_at"] = iso(now)
    if new_status == "Resolved":
        ch["resolved_at"], ch["closed_at"] = iso(now), None
    elif new_status == "Closed":
        ch["closed_at"] = iso(now)
        if not t["resolved_at"]:
            ch["resolved_at"] = iso(now)
    elif old in DONE_STATUSES:
        ch["resolved_at"] = ch["closed_at"] = None
        ch["reopen_count"] = (t["reopen_count"] or 0) + 1
    return ch


def save_ticket(db, t, changes, actor_id, now):
    if not changes:
        return False
    changes = dict(changes, updated_at=iso(now))
    cols = ", ".join(f"{k}=?" for k in changes)          # keys come from code, never user input
    db.execute(f"UPDATE tickets SET {cols} WHERE id=?", [*changes.values(), t["id"]])
    for k, v in changes.items():
        if k in TRACKED_FIELDS and str(t[k] or "") != str(v or ""):
            old, new = t[k], v
            if k == "assignee_id":
                old, new = user_name(db, old), user_name(db, new)
            log_history(db, t["id"], actor_id, FIELD_LABELS.get(k, k), old, new, now)
    return True


def reload_ticket(db, tid):
    return db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()


def set_status(db, tid, status, actor_id, now):
    t = reload_ticket(db, tid)
    ch = status_changes(t, status, now)
    actor_role = db.execute("SELECT role FROM users WHERE id=?", (actor_id,)).fetchone()
    if (actor_role and actor_role["role"] != "client" and not t["first_response_at"]
            and status not in ("New", "Assigned")):
        ch["first_response_at"] = iso(now)
    save_ticket(db, t, ch, actor_id, now)


def add_comment(db, tid, user, body, internal, now):
    cur = db.execute("INSERT INTO comments(ticket_id,user_id,body,internal,created_at) VALUES(?,?,?,?,?)",
                     (tid, user["id"], body, 1 if internal else 0, iso(now)))
    t = reload_ticket(db, tid)
    ch = {}
    if user["role"] != "client" and not internal and not t["first_response_at"]:
        ch["first_response_at"] = iso(now)
    if user["role"] == "client" and t["status"] == "Awaiting Customer":
        ch.update(status_changes(t, "In Progress", now))       # customer replied: clock resumes
    if ch:
        save_ticket(db, t, ch, user["id"], now)
    else:
        db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (iso(now), tid))
    return cur.lastrowid


def save_uploads(db, tid, comment_id, user_id, internal, files, now):
    saved, rejected = 0, []
    for fs in files:
        if not fs or not fs.filename:
            continue
        name = secure_filename(fs.filename) or "file"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_EXT:
            rejected.append(fs.filename)
            continue
        stored = f"{tid}_{secrets.token_hex(8)}_{name}"
        path = os.path.join(UPLOAD_DIR, stored)
        fs.save(path)
        db.execute("INSERT INTO attachments(ticket_id,comment_id,user_id,filename,stored_name,size,internal,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (tid, comment_id, user_id, fs.filename[:200], stored, os.path.getsize(path),
                    1 if internal else 0, iso(now)))
        saved += 1
    return saved, rejected


def related_kb(db, t, client_view):
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9_/]{4,}", f"{t['title']} {t['error_message'] or ''}")}
    vis = " AND visibility='public'" if client_view else ""
    scored = []
    for a in db.execute("SELECT * FROM kb_articles WHERE 1=1" + vis):
        hay = f"{a['title']} {a['symptoms']} {a['tags'] or ''}".lower()
        score = sum(1 for w in words if w in hay)
        if t["tcode"] and a["tcode"] and t["tcode"].upper() == a["tcode"].upper():
            score += 3
        if a["module"] == t["module"]:
            score += 1
        if score >= 2:
            scored.append((score, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:4]]


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
def no_users():
    return get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if not no_users():
        return redirect(url_for("login"))
    if request.method == "POST":
        name, email = request.form.get("name", "").strip(), request.form.get("email", "").strip()
        pw = request.form.get("password", "")
        if not name or "@" not in email or len(pw) < 8:
            flash("Enter your name, a valid email and a password of at least 8 characters.", "error")
        else:
            db = get_db()
            db.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                       (name, email, generate_password_hash(pw), "admin", iso(utcnow())))
            db.commit()
            flash("Admin account created. Sign in to continue.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if no_users():
        return redirect(url_for("setup"))
    if request.method == "POST":
        u = get_db().execute("SELECT * FROM users WHERE email=? AND active=1",
                             (request.form.get("email", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("That email and password don't match an active account.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        cur, new, conf = (request.form.get(k, "") for k in ("current", "new", "confirm"))
        if not check_password_hash(g.user["password_hash"], cur):
            flash("Current password is incorrect.", "error")
        elif len(new) < 8 or new != conf:
            flash("New password must be at least 8 characters and match the confirmation.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("Password changed.", "ok")
            return redirect(url_for("profile"))
    return render_template("profile.html")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    db, now = get_db(), utcnow()
    s, p = scope()
    open_rows = db.execute(TICKET_SELECT + " AND t.status NOT IN ('Resolved','Closed')" + s, p).fetchall()
    tickets = [(t, sla_info(t, now)) for t in open_rows]
    week_ago = iso(now - timedelta(days=7))
    resolved_week = db.execute("SELECT COUNT(*) FROM tickets t WHERE resolved_at >= ?" + s,
                               [week_ago, *p]).fetchone()[0]
    attention = sorted([x for x in tickets if x[1]["resolution"] in ("breached", "at_risk")
                        or x[0]["status"] == "New"],
                       key=lambda x: (x[1]["sort_key"], x[0]["priority"], x[1]["resolution_due"]))[:10]
    if g.user["role"] == "client":
        mine = [x for x in tickets if x[0]["reporter_id"] == g.user["id"]]
    else:
        mine = [x for x in tickets if x[0]["assignee_id"] == g.user["id"]]
    mine.sort(key=lambda x: (x[0]["priority"], x[1]["resolution_due"]))

    def counts(key, order):
        c = {}
        for t, _ in tickets:
            c[t[key]] = c.get(t[key], 0) + 1
        return [(k, c[k]) for k in order if k in c]

    tiles = {
        "open": len(tickets),
        "critical": sum(1 for t, _ in tickets if t["priority"] in ("P1", "P2")),
        "breached": sum(1 for _, i in tickets if i["resolution"] == "breached"),
        "waiting": sum(1 for t, _ in tickets if t["status"] == "Awaiting Customer"),
        "resolved_week": resolved_week,
    }
    recent = db.execute(TICKET_SELECT + s + " ORDER BY t.updated_at DESC LIMIT 8", p).fetchall()
    by_module = counts("module", MODULE_CODES)
    return render_template("dashboard.html", tiles=tiles, attention=attention, mine=mine[:10],
                           by_module=by_module, by_status=counts("status", STATUSES),
                           by_priority=counts("priority", PRIORITIES),
                           max_module=max([c for _, c in by_module] or [1]),
                           recent=[(t, sla_info(t, now)) for t in recent])


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------
def filtered_tickets():
    db, now = get_db(), utcnow()
    a = request.args
    sql, params = TICKET_SELECT, []
    s, p = scope()
    sql, params = sql + s, params + p
    status = a.get("status", "open")
    if status == "open":
        sql += " AND t.status NOT IN ('Resolved','Closed')"
    elif status in STATUSES:
        sql += " AND t.status=?"
        params.append(status)
    for field in ("priority", "module", "category"):
        if a.get(field):
            sql += f" AND t.{field}=?"
            params.append(a[field])
    if a.get("company") and is_staff():
        sql += " AND t.company_id=?"
        params.append(a.get("company"))
    assignee = a.get("assignee")
    if assignee == "me":
        sql += " AND t.assignee_id=?"
        params.append(g.user["id"])
    elif assignee == "none":
        sql += " AND t.assignee_id IS NULL"
    elif assignee and assignee.isdigit():
        sql += " AND t.assignee_id=?"
        params.append(int(assignee))
    if a.get("q"):
        like = f"%{a['q'].strip()}%"
        sql += (" AND (t.number LIKE ? OR t.title LIKE ? OR t.description LIKE ? OR t.tcode LIKE ? "
                "OR t.error_message LIKE ? OR t.transport_request LIKE ? OR t.sap_note LIKE ?)")
        params += [like] * 7
    rows = [(t, sla_info(t, now)) for t in db.execute(sql + " LIMIT 2000", params)]
    if a.get("sla") in ("breached", "at_risk", "paused", "on_track", "met"):
        rows = [x for x in rows if x[1]["resolution"] == a["sla"]]
    sort = a.get("sort", "due")
    if sort == "due":
        rows.sort(key=lambda x: (x[0]["status"] in DONE_STATUSES, x[1]["resolution_due"]))
    elif sort == "priority":
        rows.sort(key=lambda x: (x[0]["priority"], x[1]["resolution_due"]))
    elif sort == "created":
        rows.sort(key=lambda x: x[0]["created_at"], reverse=True)
    else:
        rows.sort(key=lambda x: x[0]["updated_at"], reverse=True)
    return rows


def staff_users(db):
    return db.execute("SELECT id, name, modules FROM users WHERE role IN ('consultant','admin') "
                      "AND active=1 ORDER BY name").fetchall()


@app.route("/tickets")
@login_required
def tickets():
    db = get_db()
    return render_template("tickets.html", rows=filtered_tickets(), staff=staff_users(db),
                           companies=db.execute("SELECT id,name FROM companies ORDER BY name").fetchall(),
                           args=request.args)


@app.route("/tickets.csv")
@login_required
def tickets_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Number", "Title", "Client", "Module", "Category", "Priority", "Status", "Assignee",
                "Reporter", "System", "Client No.", "T-code", "Created", "Response SLA",
                "Resolution due", "Resolution SLA", "Resolved", "Transport", "SAP Note", "Reopened"])
    for t, i in filtered_tickets():
        w.writerow([t["number"], t["title"], t["company_name"], t["module"], t["category"],
                    t["priority"], t["status"], t["assignee_name"] or "", t["reporter_name"] or "",
                    t["sap_system"] or "", t["sap_client"] or "", t["tcode"] or "",
                    filter_dt(t["created_at"]), i["response"], filter_dt(i["resolution_due"]),
                    i["resolution"], filter_dt(t["resolved_at"]) if t["resolved_at"] else "",
                    t["transport_request"] or "", t["sap_note"] or "", t["reopen_count"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=tickets-{utcnow():%Y%m%d}.csv"})


@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def ticket_new():
    db = get_db()
    companies = db.execute("SELECT id,name FROM companies WHERE active=1 ORDER BY name").fetchall()
    f = request.form
    if request.method == "POST":
        errors = []
        title, desc = f.get("title", "").strip(), f.get("description", "").strip()
        if not title or not desc:
            errors.append("Title and description are required.")
        if f.get("module") not in MODULE_CODES:
            errors.append("Choose the SAP module.")
        if f.get("category") not in CATEGORIES:
            errors.append("Choose a category.")
        if f.get("priority") not in PRIORITIES:
            errors.append("Choose a priority.")
        if g.user["role"] == "client":
            company_id = g.user["company_id"]
            if not company_id:
                errors.append("Your account isn't linked to a client company. Ask your admin to fix this.")
        else:
            company_id = f.get("company_id", type=int)
            if not company_id:
                errors.append("Choose the client company.")
        if f.get("priority") == "P1" and not f.get("business_impact", "").strip():
            errors.append("P1 tickets need a business impact statement.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            data = {k: (f.get(k) or "").strip() or None for k in
                    ("title", "description", "module", "category", "priority", "sap_system",
                     "sap_client", "tcode", "error_message", "business_impact", "users_affected")}
            if is_staff() and f.get("assignee_id", type=int):
                data["assignee_id"] = f.get("assignee_id", type=int)
            now = utcnow()
            tid = create_ticket(db, data, company_id, g.user["id"], now)
            _saved, rejected = save_uploads(db, tid, None, g.user["id"], False,
                                            request.files.getlist("files"), now)
            if rejected:
                flash("Skipped files with unsupported types: " + ", ".join(rejected), "error")
            db.commit()
            num = db.execute("SELECT number FROM tickets WHERE id=?", (tid,)).fetchone()[0]
            flash(f"Ticket {num} created.", "ok")
            return redirect(url_for("ticket_detail", tid=tid))
    return render_template("ticket_new.html", companies=companies, staff=staff_users(db), form=f,
                           sla=get_sla(db))


@app.route("/tickets/<int:tid>")
@login_required
def ticket_detail(tid):
    db = get_db()
    t = get_ticket_or_404(tid)
    client = g.user["role"] == "client"
    hide = " AND internal=0" if client else ""
    comments = db.execute("SELECT c.*, u.name AS author, u.role AS author_role FROM comments c "
                          "LEFT JOIN users u ON u.id=c.user_id WHERE ticket_id=?" + hide.replace("internal", "c.internal")
                          + " ORDER BY c.created_at, c.id", (tid,)).fetchall()
    atts = db.execute("SELECT * FROM attachments WHERE ticket_id=?" + hide + " ORDER BY id", (tid,)).fetchall()
    by_comment = {}
    loose = []
    for a in atts:
        (by_comment.setdefault(a["comment_id"], []) if a["comment_id"] else loose).append(a)
    history = db.execute("SELECT h.*, u.name AS who FROM history h LEFT JOIN users u ON u.id=h.user_id "
                         "WHERE ticket_id=? ORDER BY h.created_at DESC, h.id DESC", (tid,)).fetchall()
    return render_template("ticket_detail.html", t=t, sla=sla_info(t), comments=comments,
                           by_comment=by_comment, loose=loose, history=history,
                           staff=staff_users(db), kb=related_kb(db, t, client))


@app.route("/tickets/<int:tid>/update", methods=["POST"])
@staff_required
def ticket_update(tid):
    db, now, f = get_db(), utcnow(), request.form
    t = get_ticket_or_404(tid)
    ch = {}
    for field in ("transport_request", "sap_note", "root_cause", "resolution", "tcode",
                  "sap_system", "sap_client"):
        if field in f:
            v = f.get(field, "").strip() or None
            if field == "tcode" and v:
                v = v.upper()
            if v != t[field]:
                ch[field] = v
    for field, allowed in (("module", MODULE_CODES), ("category", CATEGORIES)):
        if f.get(field) in allowed and f.get(field) != t[field]:
            ch[field] = f[field]
    assignee = f.get("assignee_id", type=int) or None
    if "assignee_id" in f and assignee != t["assignee_id"]:
        ch["assignee_id"] = assignee
    if f.get("priority") in PRIORITIES and f["priority"] != t["priority"]:
        pol = get_sla(db)[f["priority"]]
        ch["priority"] = f["priority"]
        ch["response_due"] = iso(parse(t["created_at"]) + timedelta(hours=pol["response_hours"]))
        ch["resolution_hours"] = pol["resolution_hours"]
    new_status = f.get("status")
    if new_status in STATUSES:
        if new_status in DONE_STATUSES and not (ch.get("resolution") or t["resolution"]):
            flash("Add the resolution before marking the ticket Resolved or Closed.", "error")
            return redirect(url_for("ticket_detail", tid=tid))
        if ch.get("assignee_id") and new_status == "New":
            new_status = "Assigned"
        if not assignee and new_status == "Assigned":
            flash("Pick an assignee to set the status to Assigned.", "error")
            return redirect(url_for("ticket_detail", tid=tid))
        ch.update(status_changes(t, new_status, now))
        if not t["first_response_at"] and new_status not in ("New", "Assigned"):
            ch["first_response_at"] = iso(now)
    elif ch.get("assignee_id") and t["status"] == "New":
        ch.update(status_changes(t, "Assigned", now))
    if save_ticket(db, t, ch, g.user["id"], now):
        db.commit()
        flash("Ticket updated.", "ok")
    else:
        flash("Nothing changed.", "info")
    return redirect(url_for("ticket_detail", tid=tid))


@app.route("/tickets/<int:tid>/comment", methods=["POST"])
@login_required
def ticket_comment(tid):
    db, now = get_db(), utcnow()
    t = get_ticket_or_404(tid)
    body = request.form.get("body", "").strip()
    files = request.files.getlist("files")
    if not body and not any(fs.filename for fs in files):
        flash("Write a message or attach a file.", "error")
        return redirect(url_for("ticket_detail", tid=tid) + "#reply")
    internal = is_staff() and request.form.get("internal") == "1"
    cid = add_comment(db, tid, g.user, body or "(attachment)", internal, now)
    saved, rejected = save_uploads(db, tid, cid, g.user["id"], internal, files, now)
    if rejected:
        flash("Skipped files with unsupported types: " + ", ".join(rejected), "error")
    # Staff may move the status in the same step as replying
    new_status = request.form.get("then_status")
    if is_staff() and new_status in OPEN_STATUSES and new_status != t["status"]:
        set_status(db, tid, new_status, g.user["id"], now)
    db.commit()
    flash("Internal note added." if internal else "Reply posted.", "ok")
    return redirect(url_for("ticket_detail", tid=tid) + "#reply")


@app.route("/tickets/<int:tid>/accept", methods=["POST"])
@login_required
def ticket_accept(tid):
    db, now = get_db(), utcnow()
    t = get_ticket_or_404(tid)
    if t["status"] != "Resolved":
        abort(400, "Only resolved tickets can be closed.")
    set_status(db, tid, "Closed", g.user["id"], now)
    if request.form.get("body", "").strip():
        add_comment(db, tid, g.user, request.form["body"].strip(), False, now)
    db.commit()
    flash("Ticket closed. Thank you for confirming.", "ok")
    return redirect(url_for("ticket_detail", tid=tid))


@app.route("/tickets/<int:tid>/reopen", methods=["POST"])
@login_required
def ticket_reopen(tid):
    db, now = get_db(), utcnow()
    t = get_ticket_or_404(tid)
    reason = request.form.get("body", "").strip()
    if t["status"] not in DONE_STATUSES:
        abort(400, "Only resolved or closed tickets can be reopened.")
    if not reason:
        flash("Tell the team why the issue isn't fixed so they can pick it up.", "error")
        return redirect(url_for("ticket_detail", tid=tid))
    add_comment(db, tid, g.user, "Reopened: " + reason, False, now)
    set_status(db, tid, "In Progress" if t["assignee_id"] else "New", g.user["id"], now)
    db.commit()
    flash("Ticket reopened.", "ok")
    return redirect(url_for("ticket_detail", tid=tid))


@app.route("/attachments/<int:aid>")
@login_required
def attachment(aid):
    a = get_db().execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if a is None:
        abort(404)
    get_ticket_or_404(a["ticket_id"])          # access check
    if a["internal"] and not is_staff():
        abort(404)
    return send_from_directory(UPLOAD_DIR, a["stored_name"], as_attachment=True, download_name=a["filename"])


# --------------------------------------------------------------------------
# Knowledge base
# --------------------------------------------------------------------------
@app.route("/kb")
@login_required
def kb_list():
    db, a = get_db(), request.args
    sql, params = "SELECT k.*, u.name AS author FROM kb_articles k LEFT JOIN users u ON u.id=k.author_id WHERE 1=1", []
    if not is_staff():
        sql += " AND k.visibility='public'"
    if a.get("module"):
        sql += " AND k.module=?"
        params.append(a["module"])
    if a.get("q"):
        like = f"%{a['q'].strip()}%"
        sql += " AND (k.title LIKE ? OR k.symptoms LIKE ? OR k.solution LIKE ? OR k.tcode LIKE ? OR k.tags LIKE ?)"
        params += [like] * 5
    articles = db.execute(sql + " ORDER BY k.updated_at DESC", params).fetchall()
    return render_template("kb_list.html", articles=articles, args=a)


def get_article_or_404(kid):
    k = get_db().execute("SELECT k.*, u.name AS author, t.number AS source_number FROM kb_articles k "
                         "LEFT JOIN users u ON u.id=k.author_id LEFT JOIN tickets t ON t.id=k.source_ticket_id "
                         "WHERE k.id=?", (kid,)).fetchone()
    if k is None or (k["visibility"] == "internal" and not is_staff()):
        abort(404)
    return k


@app.route("/kb/<int:kid>")
@login_required
def kb_view(kid):
    k = get_article_or_404(kid)
    db = get_db()
    db.execute("UPDATE kb_articles SET views=views+1 WHERE id=?", (kid,))
    db.commit()
    return render_template("kb_view.html", k=k)


def kb_form_data(f):
    return {k: (f.get(k) or "").strip() or None for k in
            ("title", "module", "tcode", "symptoms", "solution", "tags", "visibility")}


@app.route("/kb/new", methods=["GET", "POST"])
@staff_required
def kb_new():
    db = get_db()
    if request.method == "POST":
        d = kb_form_data(request.form)
        if not d["title"] or not d["symptoms"] or not d["solution"] or d["module"] not in MODULE_CODES:
            flash("Title, module, symptoms and solution are required.", "error")
            return render_template("kb_form.html", k=d, source=request.form.get("source_ticket_id"))
        now = iso(utcnow())
        cur = db.execute("INSERT INTO kb_articles(title,module,tcode,symptoms,solution,tags,visibility,"
                         "source_ticket_id,author_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (d["title"], d["module"], (d["tcode"] or "").upper() or None, d["symptoms"],
                          d["solution"], d["tags"], d["visibility"] if d["visibility"] == "internal" else "public",
                          request.form.get("source_ticket_id", type=int), g.user["id"], now, now))
        db.commit()
        flash("Article published.", "ok")
        return redirect(url_for("kb_view", kid=cur.lastrowid))
    prefill, source = {}, None
    if request.args.get("from_ticket", type=int):
        t = get_ticket_or_404(request.args.get("from_ticket", type=int))
        source = t["id"]
        symptoms = t["description"] + (f"\n\nError message: {t['error_message']}" if t["error_message"] else "")
        solution = (f"Root cause: {t['root_cause']}\n\n" if t["root_cause"] else "") + (t["resolution"] or "")
        if t["sap_note"]:
            solution += f"\n\nSAP Note: {t['sap_note']}"
        prefill = {"title": t["title"], "module": t["module"], "tcode": t["tcode"],
                   "symptoms": symptoms, "solution": solution, "visibility": "public"}
    return render_template("kb_form.html", k=prefill, source=source)


@app.route("/kb/<int:kid>/edit", methods=["GET", "POST"])
@staff_required
def kb_edit(kid):
    k = get_article_or_404(kid)
    if request.method == "POST":
        d = kb_form_data(request.form)
        if not d["title"] or not d["symptoms"] or not d["solution"] or d["module"] not in MODULE_CODES:
            flash("Title, module, symptoms and solution are required.", "error")
            return render_template("kb_form.html", k=dict(d, id=kid), source=None)
        db = get_db()
        db.execute("UPDATE kb_articles SET title=?,module=?,tcode=?,symptoms=?,solution=?,tags=?,visibility=?,"
                   "updated_at=? WHERE id=?",
                   (d["title"], d["module"], (d["tcode"] or "").upper() or None, d["symptoms"], d["solution"],
                    d["tags"], d["visibility"] if d["visibility"] == "internal" else "public", iso(utcnow()), kid))
        db.commit()
        flash("Article saved.", "ok")
        return redirect(url_for("kb_view", kid=kid))
    return render_template("kb_form.html", k=k, source=None)


@app.route("/kb/<int:kid>/delete", methods=["POST"])
@admin_required
def kb_delete(kid):
    get_article_or_404(kid)
    db = get_db()
    db.execute("DELETE FROM kb_articles WHERE id=?", (kid,))
    db.commit()
    flash("Article deleted.", "ok")
    return redirect(url_for("kb_list"))


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def aggregate(rows, keyfn):
    groups = {}
    for t, i in rows:
        groups.setdefault(keyfn(t) or "Unassigned", []).append((t, i))
    out = []
    for key, items in groups.items():
        resolved = [(t, i) for t, i in items if t["resolved_at"]]
        resp_judged = [i for _, i in items if i["response"] in ("met", "breached")]
        net_hours = [((parse(t["resolved_at"]) - parse(t["created_at"])).total_seconds()
                      - (t["paused_seconds"] or 0)) / 3600 for t, _ in resolved]
        out.append({
            "key": key, "total": len(items),
            "open": sum(1 for t, _ in items if t["status"] not in DONE_STATUSES),
            "resolved": len(resolved),
            "resp_pct": round(100 * sum(1 for i in resp_judged if i == "met") / len(resp_judged)) if resp_judged else None,
            "res_pct": round(100 * sum(1 for _, i in resolved if i["resolution"] == "met") / len(resolved)) if resolved else None,
            "avg_hours": round(sum(net_hours) / len(net_hours), 1) if net_hours else None,
            "breached_open": sum(1 for t, i in items if t["status"] not in DONE_STATUSES and i["resolution"] == "breached"),
            "reopened": sum(1 for t, _ in items if t["reopen_count"]),
        })
    return sorted(out, key=lambda r: -r["total"])


@app.route("/reports")
@staff_required
def reports():
    db, now = get_db(), utcnow()
    today = now.astimezone(LOCAL_TZ).date()
    d_from = request.args.get("from") or (today - timedelta(days=90)).isoformat()
    d_to = request.args.get("to") or today.isoformat()
    try:
        start = datetime.fromisoformat(d_from).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        end = (datetime.fromisoformat(d_to) + timedelta(days=1)).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    except ValueError:
        abort(400, "Dates must be in YYYY-MM-DD format.")
    sql, params = TICKET_SELECT + " AND t.created_at >= ? AND t.created_at < ?", [iso(start), iso(end)]
    if request.args.get("company", type=int):
        sql += " AND t.company_id=?"
        params.append(request.args.get("company", type=int))
    rows = [(t, sla_info(t, now)) for t in db.execute(sql, params)]
    overall = aggregate(rows, lambda t: "All")
    return render_template(
        "reports.html", d_from=d_from, d_to=d_to, args=request.args,
        companies=db.execute("SELECT id,name FROM companies ORDER BY name").fetchall(),
        overall=overall[0] if overall else None,
        by_module=aggregate(rows, lambda t: t["module"]),
        by_priority=sorted(aggregate(rows, lambda t: t["priority"]), key=lambda r: r["key"]),
        by_category=aggregate(rows, lambda t: t["category"]),
        by_consultant=aggregate(rows, lambda t: t["assignee_name"]),
        by_company=aggregate(rows, lambda t: t["company_name"]))


# --------------------------------------------------------------------------
# Admin: users, companies, SLA
# --------------------------------------------------------------------------
@app.route("/admin/users")
@admin_required
def admin_users():
    users = get_db().execute(
        "SELECT u.*, c.name AS company_name, (SELECT COUNT(*) FROM tickets t WHERE t.assignee_id=u.id "
        "AND t.status NOT IN ('Resolved','Closed')) AS open_load FROM users u "
        "LEFT JOIN companies c ON c.id=u.company_id ORDER BY u.active DESC, u.role, u.name").fetchall()
    return render_template("users.html", users=users, split_modules=split_modules)


@app.route("/admin/users/new", methods=["GET", "POST"])
@app.route("/admin/users/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def admin_user_form(uid=None):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    if uid and u is None:
        abort(404)
    companies = db.execute("SELECT id,name FROM companies ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.form
        name, email, role = f.get("name", "").strip(), f.get("email", "").strip(), f.get("role")
        pw = f.get("password", "")
        company_id = f.get("company_id", type=int) or None
        modules = ",".join(m for m in f.getlist("modules") if m in MODULE_CODES)
        active = 1 if f.get("active") == "1" else 0
        errors = []
        if not name or "@" not in email:
            errors.append("Name and a valid email are required.")
        if role not in ROLES:
            errors.append("Choose a role.")
        if role == "client" and not company_id:
            errors.append("Client users must belong to a client company.")
        if (not u and len(pw) < 8) or (pw and len(pw) < 8):
            errors.append("Password must be at least 8 characters.")
        if u and u["id"] == g.user["id"] and (role != "admin" or not active):
            errors.append("You can't remove your own admin role or deactivate yourself.")
        clash = db.execute("SELECT id FROM users WHERE email=? AND id IS NOT ?", (email, uid)).fetchone()
        if clash:
            errors.append("Another user already has that email.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            if role != "client":
                company_id = None
            if u:
                db.execute("UPDATE users SET name=?,email=?,role=?,company_id=?,modules=?,active=? WHERE id=?",
                           (name, email, role, company_id, modules, active, uid))
                if pw:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
            else:
                db.execute("INSERT INTO users(name,email,password_hash,role,company_id,modules,active,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (name, email, generate_password_hash(pw), role, company_id, modules, active, iso(utcnow())))
            db.commit()
            flash("User saved.", "ok")
            return redirect(url_for("admin_users"))
    return render_template("user_form.html", u=u, companies=companies,
                           user_modules=split_modules(u["modules"]) if u else [])


@app.route("/admin/companies")
@admin_required
def admin_companies():
    rows = get_db().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM users u WHERE u.company_id=c.id) AS users, "
        "(SELECT COUNT(*) FROM tickets t WHERE t.company_id=c.id AND t.status NOT IN ('Resolved','Closed')) AS open "
        "FROM companies c ORDER BY c.active DESC, c.name").fetchall()
    return render_template("companies.html", companies=rows)


@app.route("/admin/companies/new", methods=["GET", "POST"])
@app.route("/admin/companies/<int:cid>/edit", methods=["GET", "POST"])
@admin_required
def admin_company_form(cid=None):
    db = get_db()
    c = db.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone() if cid else None
    if cid and c is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        d = {k: (f.get(k) or "").strip() or None for k in ("name", "contact_name", "contact_email", "sap_release", "notes")}
        active = 1 if f.get("active") == "1" else 0
        if not d["name"]:
            flash("Company name is required.", "error")
        elif db.execute("SELECT id FROM companies WHERE name=? AND id IS NOT ?", (d["name"], cid)).fetchone():
            flash("A company with that name already exists.", "error")
        else:
            if c:
                db.execute("UPDATE companies SET name=?,contact_name=?,contact_email=?,sap_release=?,notes=?,active=? WHERE id=?",
                           (*d.values(), active, cid))
            else:
                db.execute("INSERT INTO companies(name,contact_name,contact_email,sap_release,notes,active,created_at) "
                           "VALUES(?,?,?,?,?,?,?)", (*d.values(), active, iso(utcnow())))
            db.commit()
            flash("Company saved.", "ok")
            return redirect(url_for("admin_companies"))
    return render_template("company_form.html", c=c)


@app.route("/admin/sla", methods=["GET", "POST"])
@admin_required
def admin_sla():
    db = get_db()
    if request.method == "POST":
        try:
            vals = {p: (float(request.form[f"resp_{p}"]), float(request.form[f"res_{p}"])) for p in PRIORITIES}
        except (KeyError, ValueError):
            flash("Enter hours as numbers, e.g. 0.5 or 24.", "error")
            return redirect(url_for("admin_sla"))
        if any(r <= 0 or s <= 0 or r > s for r, s in vals.values()):
            flash("Hours must be above zero, and response time can't exceed resolution time.", "error")
            return redirect(url_for("admin_sla"))
        for p, (r, s) in vals.items():
            db.execute("UPDATE sla_policies SET response_hours=?, resolution_hours=? WHERE priority=?", (r, s, p))
        db.commit()
        flash("SLA policy saved. It applies to new tickets and to tickets whose priority changes.", "ok")
        return redirect(url_for("admin_sla"))
    return render_template("sla.html", sla=get_sla(db))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def handle_error(e):
    messages = {403: "You don't have access to this page.",
                404: "That page or ticket doesn't exist, or you don't have access to it.",
                413: "The upload is too large. Attachments can be up to 20 MB per message."}
    msg = messages.get(e.code) or getattr(e, "description", "Something went wrong.")
    return render_template("error.html", code=e.code, message=msg), e.code


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--demo", action="store_true", help="load demo data into an empty database")
    ap.add_argument("--host", default=os.environ.get("HELPDESK_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("HELPDESK_PORT", "5000")))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    if args.demo:
        import demo_data
        demo_data.seed()
    app.run(host=args.host, port=args.port, debug=args.debug)
