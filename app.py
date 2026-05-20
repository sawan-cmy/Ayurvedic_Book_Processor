from __future__ import annotations

import json
import logging
import secrets
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from html import escape

from flask import Flask, Response, jsonify, redirect, request, send_file, stream_with_context, url_for
from dotenv import load_dotenv
from werkzeug.exceptions import RequestEntityTooLarge

from image_deck_generator import generate_image_slide_deck
from utils import now_text, safe_pdf_name, looks_like_pdf


ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
COMPLETED_DOCS_DIR = ROOT / "completed_docs"
SLIDE_DECK_OUTPUTS_DIR = ROOT / "slide_deck_outputs"
LOG_DIR = ROOT / "logs"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PROCESSOR = ROOT / "ultimate_book_processor.py"
JOBS_DB = JOBS_DIR / "jobs.db"
JOBS_LOCK_FILE = JOBS_DIR / "jobs.db.lock"
JOB_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-fA-F]{8}$")
MAX_QUEUED_PER_USER = 3
MIN_FREE_DISK_MB = 1024

logger = logging.getLogger(__name__)

load_dotenv(ENV_FILE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "250")) * 1024 * 1024

jobs_lock = threading.RLock()
dispatcher_lock = threading.Lock()
deck_generation_lock = threading.Lock()
jobs_file_lock_guard = threading.RLock()
jobs_file_lock_state = threading.local()
running_processes: dict[str, subprocess.Popen[str]] = {}
running_job_ids: set[str] = set()
running_deck_generations: set[str] = set()
dispatcher_started = False

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def jobs_file_lock() -> Any:
    """Lock jobs.db across processes before any SQLite write."""
    depth = int(getattr(jobs_file_lock_state, "depth", 0))
    if depth:
        jobs_file_lock_state.depth = depth + 1
        try:
            yield
        finally:
            jobs_file_lock_state.depth -= 1
        return

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with jobs_file_lock_guard:
        with JOBS_LOCK_FILE.open("a+b") as handle:
            locked = False
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
                jobs_file_lock_state.depth = 1
                yield
            finally:
                jobs_file_lock_state.depth = 0
                if locked:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table_name})")}


def create_tables() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id          TEXT PRIMARY KEY,
                        filename    TEXT NOT NULL,
                        status      TEXT NOT NULL DEFAULT 'queued',
                        uploaded_by TEXT,
                        created_at  TEXT NOT NULL,
                        updated_at  TEXT NOT NULL,
                        started_at  TEXT,
                        finished_at TEXT,
                        error       TEXT,
                        return_code INTEGER,
                        meta        TEXT
                    )
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        username      TEXT PRIMARY KEY,
                        password_hash TEXT NOT NULL,
                        is_admin      INTEGER NOT NULL DEFAULT 0,
                        created_at    TEXT NOT NULL,
                        last_login_at TEXT
                    )
                """)
                job_columns = _table_columns(con, "jobs")
                for column_name, column_type in {
                    "uploaded_by": "TEXT",
                    "started_at": "TEXT",
                    "finished_at": "TEXT",
                    "return_code": "INTEGER",
                    "meta": "TEXT",
                }.items():
                    if column_name not in job_columns:
                        con.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_type}")
                con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_uploaded_by ON jobs(uploaded_by)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
                user_columns = _table_columns(con, "users")
                if "last_login_at" not in user_columns:
                    con.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")


def slide_deck_zip_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / f"{job_id}_slide_deck.zip"

def load_slide_deck_state(job_id: str) -> dict[str, Any]:
    state_path = SLIDE_DECK_OUTPUTS_DIR / job_id / "deck_state.json"
    if not state_path.exists():
        return {"status": "not_started"}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"status": "not_started"}
        return data
    except Exception:
        return {"status": "not_started"}

def review_report_file(job_id: str) -> Path:
    return job_dir(job_id) / "output_notes" / "review_report.md"

def slide_deck_review_report_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "review_report.md"

def slide_deck_coverage_report_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "source_coverage_report.md"

def slide_deck_prompt_used_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "prompt_used.md"

def friendly_prompt_engine_status(status: str) -> str:
    if status == "used": return "Ready"
    if status == "error": return "Error"
    if status == "skipped": return "Skipped"
    return "Pending"

def prompt_status_note(status: str) -> str:
    if status == "used": return "Prompt engine succeeded."
    if status == "error": return "Prompt engine encountered an error."
    if status == "skipped": return "Prompt engine skipped."
    return "Prompt engine has not started."

def render_slide_deck_panel(job_id: str, job_status: str) -> str:
    if job_status not in {"completed", "completed_with_review_needed"}:
        return ""
    state = load_slide_deck_state(job_id)
    deck_status = state.get("status", "not_started")
    if deck_status == "not_started":
        action = f"<form action='{url_for('generate_image_slide_deck_route', job_id=job_id)}' method='post' class='inline' style='margin:0;'><button class='secondary'>Generate Image Slide Deck</button></form>"
        return f"<div class='deckbox'><strong>Slide Deck:</strong> Not started {action}</div>"
    
    html = [f"<div class='deckbox'><strong>Slide Deck:</strong> {deck_status.replace('_', ' ').title()}"]
    
    if deck_status == "generating":
        eng_stat = state.get("prompt_engine_status", "")
        img_stat = state.get("image_gen_status", "")
        html.append(f"<small>Engine: {eng_stat} | Images: {img_stat}</small>")
    elif deck_status == "failed":
        err = state.get("latest_error", "Unknown error")
        html.append(f"<small class='problem'>{escape(err)}</small>")
        html.append(f"<form action='{url_for('generate_image_slide_deck_route', job_id=job_id)}' method='post' class='inline' style='margin-top:6px;'><button class='secondary'>Retry Generation</button></form>")
    elif deck_status == "completed":
        zip_path = slide_deck_zip_file(job_id)
        if zip_path.exists():
            mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
            html.append(f"<small>Ready ({mb} MB)</small>")
            html.append(f"<div class='actions inline' style='margin-top:8px;'><a class='button' href='{url_for('download_image_slide_deck', job_id=job_id)}'>Download ZIP</a></div>")
    
    html.append("</div>")
    return "".join(html)

_stale_jobs_recovered = False

def recover_stale_running_jobs() -> None:
    global _stale_jobs_recovered
    if _stale_jobs_recovered:
        return
    _stale_jobs_recovered = True
    
    try:
        with jobs_lock:
            with jobs_file_lock():
                with sqlite3.connect(JOBS_DB) as con:
                    con.row_factory = sqlite3.Row
                    rows = con.execute("SELECT * FROM jobs WHERE status = 'running'").fetchall()
                    for row in rows:
                        job = _row_to_job(row)
                        job["status"] = "failed"
                        job["error"] = "Server restarted while this job was running. Click Run Again to resume."
                        job["failure_reason"] = "Server restarted while this job was running. Click Run Again to resume."
                        job["updated_at"] = now_text()
                        job["finished_at"] = now_text()
                        
                        row_data = _job_to_row(job)
                        con.execute("""
                            UPDATE jobs
                            SET filename = ?, status = ?, uploaded_by = ?, created_at = ?, updated_at = ?,
                                started_at = ?, finished_at = ?, error = ?, return_code = ?, meta = ?
                            WHERE id = ?
                        """, (row_data[1], row_data[2], row_data[3], row_data[4], row_data[5], row_data[6], row_data[7], row_data[8], row_data[9], row_data[10], job["id"]))
                    
        if SLIDE_DECK_OUTPUTS_DIR.exists():
            for job_dir_path in SLIDE_DECK_OUTPUTS_DIR.iterdir():
                if not job_dir_path.is_dir():
                    continue
                state_path = job_dir_path / "deck_state.json"
                if state_path.exists():
                    try:
                        data = json.loads(state_path.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and data.get("status") == "generating":
                            data["status"] = "failed"
                            data["latest_error"] = "Server restarted while this job was running. Click Run Again to resume."
                            state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as exc:
                        logger.exception("Could not recover stale slide deck state %s: %s", state_path, exc)
    except Exception as exc:
        logger.exception("Could not recover stale running jobs: %s", exc)


def ensure_project_files() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    COMPLETED_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    SLIDE_DECK_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8-sig"), encoding="utf-8")
    create_tables()
    recover_stale_running_jobs()

    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                if count == 0:
                    username = os.getenv("APP_USERNAME", "").strip()
                    password = os.getenv("APP_PASSWORD", "").strip()
                    if username and password and password != "change-this-password":
                        import bcrypt
                        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                        con.execute("INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                                    (username, pw_hash, now_text()))


def valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(job_id or ""))


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    ensure_project_files()
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    ensure_project_files()
    existing = read_env()
    existing.update(updates)
    ordered_keys = [
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_EXTRACTION_MODEL",
        "GEMINI_VERIFICATION_MODEL",
        "GEMINI_FORMAT_MODEL",
        "SLIDE_PROMPT_MODEL",
        "PDF_DIR",
        "OUTPUT_DIR",
        "POPPLER_PATH",
        "DPI",
        "MAX_RETRIES",
        "GEMINI_DELAY_SECONDS",
        "GEMINI_TIMEOUT_SECONDS",
        "SPEED_MODE",
        "PAGE_WORKERS_PER_JOB",
        "RESET_MASTER",
        "CREATE_DOCX",
        "TEST_START_PAGE",
        "TEST_MAX_PAGES",
        "TEST_PDF_LIMIT",
        "CREATE_STRUCTURED_NOTES",
        "MAX_PARALLEL_JOBS",
        "DISABLE_INLINE_WORKERS",
        "PROMPT_ENGINE_BATCH_SIZE",
        "USE_PROMPT_ENGINE",
        "USE_AI_IMAGES",
        "IMAGE_GEN_MODEL",
        "PROMPT_ENGINE_MODE",
        "MAX_UPLOAD_MB",
        "APP_USERNAME",
        "APP_PASSWORD",
    ]
    lines: list[str] = []
    for key in ordered_keys:
        if key in existing:
            lines.append(f"{key}={existing[key]}")
    for key in sorted(set(existing) - set(ordered_keys)):
        lines.append(f"{key}={existing[key]}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_dotenv(ENV_FILE, override=True)
    upload_mb = existing.get("MAX_UPLOAD_MB", os.getenv("MAX_UPLOAD_MB", "250"))
    app.config["MAX_CONTENT_LENGTH"] = int(upload_mb) * 1024 * 1024


def max_parallel_jobs() -> int:
    try:
        return max(1, min(25, int(read_env().get("MAX_PARALLEL_JOBS", "3"))))
    except ValueError:
        return 3


def inline_workers_enabled() -> bool:
    runtime_value = os.environ.get("DISABLE_INLINE_WORKERS")
    value = runtime_value if runtime_value is not None else read_env().get("DISABLE_INLINE_WORKERS", "false")
    return value.strip().lower() not in {"1", "true", "yes", "on"}


def auth_enabled() -> bool:
    ensure_project_files()
    with sqlite3.connect(JOBS_DB) as con:
        count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return count > 0


def current_username() -> str | None:
    auth = request.authorization
    return auth.username if auth and auth.username else None


def is_current_admin() -> bool:
    username = current_username()
    if not username:
        return False
    with sqlite3.connect(JOBS_DB) as con:
        row = con.execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()
        return bool(row and row[0])


def authorized() -> bool:
    if not auth_enabled():
        return True
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return False
    with sqlite3.connect(JOBS_DB) as con:
        con.row_factory = sqlite3.Row
        user = con.execute("SELECT password_hash FROM users WHERE username = ?", (auth.username,)).fetchone()
        if not user:
            return False
        import bcrypt
        ok = bcrypt.checkpw(auth.password.encode(), user["password_hash"].encode())
    if ok:
        with jobs_lock:
            with jobs_file_lock():
                with sqlite3.connect(JOBS_DB) as con:
                    con.execute("UPDATE users SET last_login_at = ? WHERE username = ?", (now_text(), auth.username))
    return ok


@app.before_request
def require_auth() -> Response | None:
    if request.endpoint == "health":
        return None
    if authorized():
        return None
    return Response(
        "Authentication required",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Ayurvedic Book Processor"'},
    )


@app.route("/admin", methods=["GET"])
def admin_dashboard() -> Response | str:
    if not is_current_admin():
        return Response("Forbidden: Admin access required", status=403)
    jobs = load_jobs()
    counts = {status: sum(1 for job in jobs if job.get("status") == status) for status in ["queued", "running", "completed", "completed_with_review_needed", "failed"]}
    disk = shutil.disk_usage(ROOT)
    with sqlite3.connect(JOBS_DB) as con:
        user_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows = "".join(
        f"<tr><td>{escape(str(job.get('filename', '')))}</td><td>{escape(str(job.get('status', '')))}</td><td>{escape(str(job.get('uploaded_by') or ''))}</td><td>{escape(str(job.get('updated_at', '')))}</td></tr>"
        for job in jobs[:25]
    ) or "<tr><td colspan='4'>No jobs</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>Admin Dashboard</title>
    <style>
    :root {{ --bg:#f5f6f8; --panel:#fff; --text:#1f2933; --muted:#697586; --line:#d9dee5; --accent:#0f766e; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:Segoe UI,Arial,sans-serif; }}
    main {{ max-width:1100px; margin:0 auto; padding:24px; display:grid; gap:16px; }}
    .top {{ display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }}
    .tile, section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .tile strong {{ display:block; font-size:24px; margin-top:4px; }}
    small {{ color:var(--muted); }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); }}
    th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:10px; font-size:14px; }}
    .button {{ background:var(--accent); color:#fff; padding:10px 14px; border-radius:6px; text-decoration:none; display:inline-flex; }}
    @media (max-width:700px) {{ main {{ padding:14px; }} table {{ display:block; overflow-x:auto; }} }}
    </style></head><body><main>
      <div class='top'><div><h1>Admin Dashboard</h1><small>Queue, disk, users, and recent jobs</small></div><div><a class='button' href='/'>Back</a> <a class='button' href='/admin/users'>Users</a></div></div>
      <div class='grid'>
        <div class='tile'><small>Queued</small><strong>{counts['queued']}</strong></div>
        <div class='tile'><small>Running</small><strong>{counts['running']}</strong></div>
        <div class='tile'><small>Completed</small><strong>{counts['completed'] + counts['completed_with_review_needed']}</strong></div>
        <div class='tile'><small>Failed</small><strong>{counts['failed']}</strong></div>
        <div class='tile'><small>Users</small><strong>{user_count}</strong></div>
        <div class='tile'><small>Free Disk</small><strong>{disk.free // (1024 * 1024)} MB</strong></div>
      </div>
      <section><h2>Recent Jobs</h2><table><tr><th>File</th><th>Status</th><th>User</th><th>Updated</th></tr>{rows}</table></section>
    </main></body></html>"""


@app.route("/admin/users", methods=["GET"])
def admin_users() -> Response | str:
    auth = request.authorization
    if not auth or not is_current_admin():
        return Response("Forbidden: Admin access required", status=403)
    with sqlite3.connect(JOBS_DB) as con:
        con.row_factory = sqlite3.Row
        users = con.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        
    html = ["<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>Admin - Users</title><style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2933}table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #d9dee5;padding:10px;text-align:left}.button,button{border:0;border-radius:6px;background:#0f766e;color:white;padding:9px 12px;text-decoration:none}input{padding:9px;border:1px solid #d9dee5;border-radius:6px;margin:4px 0}@media(max-width:700px){body{margin:14px}table{display:block;overflow-x:auto}}</style></head><body>"]
    html.append("<p><a class='button' href='/admin'>Admin Dashboard</a> <a class='button' href='/'>Back</a></p>")
    html.append("<h1>Manage Users</h1>")
    html.append("<table><tr><th>Username</th><th>Admin</th><th>Created</th><th>Last Login</th><th>Actions</th></tr>")
    for u in users:
        admin_str = "Yes" if u["is_admin"] else "No"
        delete_btn = ""
        if u["username"] != auth.username:
            delete_btn = f"<form action='/admin/users/{u['username']}/delete' method='post' style='display:inline;'><button type='submit'>Delete</button></form>"
        html.append(f"<tr><td>{escape(u['username'])}</td><td>{admin_str}</td><td>{escape(u['created_at'])}</td><td>{escape(str(u['last_login_at'] or ''))}</td><td>{delete_btn}</td></tr>")
    html.append("</table>")
    
    html.append("<h2>Add User</h2>")
    html.append("<form action='/admin/users/add' method='post'>")
    html.append("Username: <input type='text' name='username' required><br>")
    html.append("Password: <input type='password' name='password' required><br>")
    html.append("Admin: <input type='checkbox' name='is_admin' value='1'><br>")
    html.append("<button type='submit'>Add User</button>")
    html.append("</form>")
    
    html.append("</body></html>")
    return "".join(html)

@app.route("/admin/users/add", methods=["POST"])
def admin_users_add() -> Response:
    if not is_current_admin():
        return redirect(url_for("admin_users"))
            
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    is_admin = 1 if request.form.get("is_admin") == "1" else 0
    
    if username and password:
        import bcrypt
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        with jobs_lock:
            with jobs_file_lock():
                with sqlite3.connect(JOBS_DB) as con:
                    try:
                        con.execute("INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                                    (username, pw_hash, is_admin, now_text()))
                    except sqlite3.IntegrityError:
                        logger.info("Admin attempted to add existing user %s", username)
                
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<username>/delete", methods=["POST"])
def admin_users_delete(username: str) -> Response:
    auth = request.authorization
    if not auth or not is_current_admin():
        return redirect(url_for("admin_users"))
    
    if auth.username == username:
        return redirect(url_for("admin_users"))
        
    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.execute("DELETE FROM users WHERE username = ?", (username,))
        
    return redirect(url_for("admin_users"))


@app.after_request
def add_production_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_: RequestEntityTooLarge) -> Response:
    max_mb = int(app.config.get("MAX_CONTENT_LENGTH", 250 * 1024 * 1024) / (1024 * 1024))
    return redirect(url_for("index", error=f"Upload is too large. Maximum allowed size is {max_mb} MB."))


@app.before_request
def start_runtime() -> None:
    if request.endpoint != "health":
        ensure_project_files()
        ensure_dispatcher()


def parse_int_field(value: str | None, default: int, minimum: int, maximum: int) -> str:
    try:
        parsed = int(value or default)
    except ValueError:
        parsed = default
    return str(max(minimum, min(maximum, parsed)))


JOB_ROW_COLUMNS = (
    "id",
    "filename",
    "status",
    "uploaded_by",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "error",
    "return_code",
    "meta",
)
JOB_BASE_KEYS = set(JOB_ROW_COLUMNS) - {"meta"}


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    meta_str = job.pop("meta", "{}")
    try:
        meta = json.loads(meta_str) if meta_str else {}
    except Exception:
        meta = {}
    
    result = {}
    result.update(meta)
    result.update({k: v for k, v in job.items() if v is not None})
    return result

def _job_to_row(job: dict[str, Any]) -> tuple:
    meta = {k: v for k, v in job.items() if k not in JOB_BASE_KEYS}
    meta_str = json.dumps(meta, ensure_ascii=False)
    return (
        job.get("id"),
        job.get("filename"),
        job.get("status", "queued"),
        job.get("uploaded_by"),
        job.get("created_at"),
        job.get("updated_at"),
        job.get("started_at"),
        job.get("finished_at"),
        job.get("error"),
        job.get("return_code"),
        meta_str,
    )

def load_jobs() -> list[dict[str, Any]]:
    ensure_project_files()
    with sqlite3.connect(JOBS_DB) as con:
        con.row_factory = sqlite3.Row
        return [_row_to_job(row) for row in con.execute("SELECT * FROM jobs ORDER BY created_at DESC")]

def save_jobs(jobs: list[dict[str, Any]]) -> None:
    ensure_project_files()
    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.execute("BEGIN TRANSACTION")
                con.execute("DELETE FROM jobs")
                con.executemany("""
                    INSERT INTO jobs (id, filename, status, uploaded_by, created_at, updated_at, started_at, finished_at, error, return_code, meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [_job_to_row(job) for job in jobs])

def update_job(job_id: str, **updates: Any) -> None:
    ensure_project_files()
    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if not row:
                    return
                job = _row_to_job(row)
                job.update(updates)
                job["updated_at"] = now_text()
                row_data = _job_to_row(job)
                
                con.execute("""
                    UPDATE jobs
                    SET filename = ?, status = ?, uploaded_by = ?, created_at = ?, updated_at = ?,
                        started_at = ?, finished_at = ?, error = ?, return_code = ?, meta = ?
                    WHERE id = ?
                """, (row_data[1], row_data[2], row_data[3], row_data[4], row_data[5], row_data[6], row_data[7], row_data[8], row_data[9], row_data[10], job_id))

def job_by_id(job_id: str) -> dict[str, Any] | None:
    ensure_project_files()
    with sqlite3.connect(JOBS_DB) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            return _row_to_job(row)
    return None


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def ensure_child_path(parent: Path, child: Path) -> Path:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    if parent_resolved != child_resolved and parent_resolved not in child_resolved.parents:
        raise ValueError("Resolved upload path escaped the job directory.")
    return child_resolved


def queued_count_for_user(username: str | None) -> int:
    ensure_project_files()
    with sqlite3.connect(JOBS_DB) as con:
        if username:
            return int(
                con.execute(
                    "SELECT COUNT(*) FROM jobs WHERE uploaded_by = ? AND status = 'queued'",
                    (username,),
                ).fetchone()[0]
            )
        return int(con.execute("SELECT COUNT(*) FROM jobs WHERE uploaded_by IS NULL AND status = 'queued'").fetchone()[0])


def filename_already_active(filename: str, username: str | None = None) -> bool:
    ensure_project_files()
    with sqlite3.connect(JOBS_DB) as con:
        if username:
            row = con.execute(
                "SELECT 1 FROM jobs WHERE filename = ? AND uploaded_by = ? AND status IN ('queued', 'running') LIMIT 1",
                (filename, username),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT 1 FROM jobs WHERE filename = ? AND status IN ('queued', 'running') LIMIT 1",
                (filename,),
            ).fetchone()
        return row is not None


def ensure_disk_space(required_bytes: int | None = None) -> None:
    ensure_project_files()
    free_bytes = shutil.disk_usage(JOBS_DIR).free
    upload_bytes = max(0, int(required_bytes or 0))
    reserve_bytes = int(os.getenv("MIN_FREE_DISK_MB", str(MIN_FREE_DISK_MB))) * 1024 * 1024
    if free_bytes < upload_bytes + reserve_bytes:
        free_mb = free_bytes // (1024 * 1024)
        reserve_mb = reserve_bytes // (1024 * 1024)
        raise RuntimeError(f"Not enough disk space. Free: {free_mb} MB; required reserve: {reserve_mb} MB.")


def create_job(pdf_file: Any, uploaded_by: str | None = None) -> str:
    ensure_project_files()
    env = read_env()
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    folder = job_dir(job_id)
    pdf_dir = folder / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_pdf_name(pdf_file.filename or "uploaded.pdf")
    target_path = ensure_child_path(pdf_dir, pdf_dir / filename)
    pdf_file.save(target_path)

    job_env = env.copy()
    job_env.update(
        {
            "PDF_DIR": "./pdfs",
            "OUTPUT_DIR": "./output_notes",
            "CREATE_DOCX": "true",
            "RESET_MASTER": "false",
            "TEST_START_PAGE": env.get("TEST_START_PAGE", "1"),
            "TEST_MAX_PAGES": env.get("TEST_MAX_PAGES", "0"),
            "TEST_PDF_LIMIT": "1",
            "CREATE_STRUCTURED_NOTES": env.get("CREATE_STRUCTURED_NOTES", "false"),
            "SPEED_MODE": env.get("SPEED_MODE", "balanced"),
            "PAGE_WORKERS_PER_JOB": env.get("PAGE_WORKERS_PER_JOB", "2"),
        }
    )
    env_lines = [f"{key}={value}" for key, value in job_env.items()]
    (folder / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    job = {
        "id": job_id,
        "filename": filename,
        "status": "queued",
        "uploaded_by": uploaded_by,
        "created_at": now_text(),
        "updated_at": now_text(),
        "return_code": None,
    }
    
    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.execute("""
                    INSERT INTO jobs (id, filename, status, uploaded_by, created_at, updated_at, started_at, finished_at, error, return_code, meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, _job_to_row(job))
    return job_id


def job_output_files(job_id: str) -> list[Path]:
    output_dir = job_dir(job_id) / "output_notes"
    if not output_dir.exists():
        return []
    return sorted(output_dir.glob("*.docx"), key=lambda path: path.stat().st_mtime, reverse=True)


def completed_doc_files() -> list[Path]:
    ensure_project_files()
    return sorted(COMPLETED_DOCS_DIR.glob("*.docx"), key=lambda path: path.stat().st_mtime, reverse=True)

def publish_docx_outputs(job_id: str) -> list[str]:
    published: list[str] = []
    for source in job_output_files(job_id):
        target_name = f"{job_id}_{source.name}"
        target = COMPLETED_DOCS_DIR / target_name
        shutil.copy2(source, target)
        published.append(target_name)
    return published


def job_log(job_id: str, max_chars: int = 5000) -> str:
    log_path = job_dir(job_id) / "logs" / "interface_run.log"
    if not log_path.exists():
        log_path = job_dir(job_id) / "logs" / "processor.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")[-max_chars:]


def load_job_state(job_id: str) -> dict[str, Any]:
    state_path = job_dir(job_id) / "processing_state.json"
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_job_state(job_id: str, state: dict[str, Any]) -> None:
    state_path = job_dir(job_id) / "processing_state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def failed_page_count(job_id: str) -> int:
    failed = load_job_state(job_id).get("failed_pages", {})
    if not isinstance(failed, dict):
        return 0
    return sum(len(pages) for pages in failed.values() if isinstance(pages, dict))


def flatten_page_map(section: Any) -> list[tuple[str, int, Any]]:
    rows: list[tuple[str, int, Any]] = []
    if not isinstance(section, dict):
        return rows
    for pdf_stem, pages in section.items():
        if isinstance(pages, dict):
            values = pages.keys()
        elif isinstance(pages, list):
            values = pages
        else:
            continue
        for value in values:
            try:
                rows.append((str(pdf_stem), int(value), pages.get(value, {}) if isinstance(pages, dict) else {}))
            except (TypeError, ValueError):
                continue
    return sorted(rows, key=lambda row: (row[0], row[1]))


def job_progress(job_id: str) -> dict[str, Any]:
    state = load_job_state(job_id)
    total_pages = sum(int(value) for value in state.get("chapter_page_counts", {}).values() if str(value).isdigit())
    extracted = flatten_page_map(state.get("extracted_pages", {}))
    verified = flatten_page_map(state.get("verified_pages", {}))
    failed = flatten_page_map(state.get("failed_pages", {}))
    manual = flatten_page_map(state.get("manually_verified_pages", {}))
    if total_pages == 0:
        total_pages = max([page for _, page, _ in extracted + verified + failed] or [0])
    latest_error = ""
    if failed:
        row = failed[-1][2] if isinstance(failed[-1][2], dict) else {}
        latest_error = friendly_error(str(row.get("error", "")))
    return {
        "total_pages": total_pages,
        "extracted_pages": len(extracted),
        "verified_pages": len(verified),
        "failed_pages": len(failed),
        "manually_reviewed_pages": len(manual),
        "latest_error": latest_error,
    }


def friendly_status(status: str) -> str:
    return {
        "queued": "Queued",
        "running": "Processing",
        "completed": "Completed",
        "completed_with_review_needed": "Completed - Review Needed",
        "failed": "Failed",
    }.get(status, status.replace("_", " ").title())


def friendly_deck_status(status: str) -> str:
    return {
        "not_started": "Not Generated",
        "generating": "Generating Slide Deck",
        "completed": "Slide Deck Ready",
        "completed_with_review_needed": "Slide Deck Ready - Review Needed",
        "failed": "Slide Deck Failed",
    }.get(status, status.replace("_", " ").title())


def friendly_prompt_engine_status(status: str) -> str:
    return {
        "not_started": "Not Run",
        "used": "Master Prompt Used",
        "partial": "Partially Used",
        "failed": "Prompt Fallback",
        "missing_api_key": "No API Key",
        "disabled": "Disabled",
        "unavailable": "Unavailable",
    }.get(status, status.replace("_", " ").title())


def prompt_status_class(status: str) -> str:
    if status == "used":
        return "good"
    if status in {"partial", "failed", "missing_api_key", "unavailable"}:
        return "warn"
    return "neutral"


def prompt_status_note(status: str) -> str:
    return {
        "used": "The slide visual plan was generated with the configured master prompt.",
        "partial": "Some slides used the master prompt; at least one slide used fallback planning.",
        "failed": "Gemini prompt planning failed, so the deck used exact-content fallback.",
        "missing_api_key": "No Gemini API key was available for prompt planning.",
        "disabled": "Prompt planning is disabled by USE_PROMPT_ENGINE.",
        "unavailable": "The Gemini client package was not available.",
        "not_started": "Generate the slide deck to run the master prompt.",
    }.get(status, "Review the slide deck report for details.")


def friendly_error(error: str) -> str:
    text = error or ""
    lowered = text.lower()
    if "deadline" in lowered or "timeout" in lowered or "504" in lowered:
        return "Gemini timed out on this page. Retry later or review manually."
    if "no verified source" in lowered:
        return "Complete PDF processing before generating a slide deck."
    if "slide count too low" in lowered:
        return "Slide deck does not cover all source text. Please review coverage report."
    if not text:
        return ""
    return text[:180]


def current_stage(status: str, progress: dict[str, Any]) -> str:
    if status == "queued":
        return "Waiting"
    if status == "running":
        if progress["verified_pages"] < progress["extracted_pages"]:
            return "Verifying"
        return "Extracting"
    if status in {"completed", "completed_with_review_needed"}:
        return "Complete"
    if status == "failed":
        return "Stopped"
    return "Working"


def percent(value: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, round((value / total) * 100)))


def render_pdf_progress(job: dict[str, Any], display_status: str, outputs: list[Path]) -> str:
    progress = job_progress(job["id"])
    total = progress["total_pages"]
    verified = progress["verified_pages"]
    failed = progress["failed_pages"]
    manual = progress["manually_reviewed_pages"]
    bar = percent(verified + failed, total)
    details = [
        f"Verified {verified} / {total or '?'}",
        f"Extracted {progress['extracted_pages']}",
        f"Failed {failed}",
        f"Manual review {manual}",
        f"Stage: {current_stage(display_status, progress)}",
    ]
    if progress["latest_error"]:
        details.append(f"Latest issue: {progress['latest_error']}")
    times = [
        f"created {escape(str(job.get('created_at', '')))}",
        f"started {escape(str(job.get('started_at', '')))}" if job.get("started_at") else "",
        f"finished {escape(str(job.get('finished_at', '')))}" if job.get("finished_at") else "",
    ]
    return (
        f"<div class='jobmeta'><strong>{friendly_status(display_status)}</strong><small>{' | '.join(item for item in times if item)}</small>"
        f"<div class='bar'><span style='width:{bar}%'></span></div>"
        f"<small>{escape(' | '.join(details))}</small></div>"
    )


def render_slide_deck_panel(job_id: str, display_status: str) -> str:
    state = load_slide_deck_state(job_id)
    deck_status = str(state.get("status", "not_started"))
    coverage = str(state.get("coverage_status", "UNKNOWN"))
    prompt_engine = str(state.get("prompt_engine_status", "not_started"))
    prompt_profile = str(state.get("prompt_profile", ""))
    rendered = int(state.get("rendered_slide_count") or 0)
    slides = int(state.get("slide_count") or 0)
    bar = percent(rendered, slides)
    latest_error = friendly_error(str(state.get("latest_error", "")))
    parts = [
        "<div class='deckbox'>",
        f"<div class='deckhead'><strong>{friendly_deck_status(deck_status)}</strong><span class='chip {prompt_status_class(prompt_engine)}'>{friendly_prompt_engine_status(prompt_engine)}</span></div>",
        f"<div class='bar'><span style='width:{bar}%'></span></div>",
        f"<small>Source chars: {state.get('source_char_count', 0)} | Chunks: {state.get('chunk_count', 0)} | Rendered {rendered} / {slides} slides | PNG: {rendered} | PDF: {'yes' if state.get('pdf_created') else 'no'} | ZIP: {'yes' if state.get('zip_created') else 'no'} | Coverage: {escape(coverage)}</small>",
        f"<small><strong>Prompt:</strong> {escape(prompt_profile or 'not recorded')} | <strong>Status:</strong> {escape(friendly_prompt_engine_status(prompt_engine))}</small>",
        f"<small>{escape(prompt_status_note(prompt_engine))}</small>",
    ]
    if deck_status == "completed_with_review_needed":
        parts.append("<small class='problem'>Slide deck was created, but prompt/content conflicts or validation warnings need review.</small>")
    if latest_error:
        parts.append(f"<small class='problem'>Slide deck generation failed: {escape(latest_error)}</small>")
    parts.append("<div class='actions'>")
    if display_status in {"completed", "completed_with_review_needed"}:
        if deck_status == "generating":
            parts.append("<button class='secondary' disabled>Generating Slide Deck...</button>")
        else:
            label = "Retry Slide Deck Generation" if deck_status == "failed" else "Generate Ayurveda Slide Deck"
            parts.append(f"<form action='{url_for('generate_image_slide_deck_route', job_id=job_id)}' method='post'><button class='secondary'>{label}</button></form>")
        deck_done = deck_status in {"completed", "completed_with_review_needed"}
        if deck_done and slide_deck_zip_file(job_id).exists():
            parts.append(f"<a class='button secondary' href='{url_for('download_image_slide_deck', job_id=job_id)}'>Download Ayurveda Slide Deck</a>")
        if deck_done and slide_deck_coverage_report_file(job_id).exists():
            parts.append(f"<a class='button secondary' href='{url_for('download_slide_deck_coverage_report', job_id=job_id)}'>Download Slide Coverage Report</a>")
        if deck_done and slide_deck_review_report_file(job_id).exists():
            parts.append(f"<a class='button secondary' href='{url_for('download_slide_deck_review_report', job_id=job_id)}'>Download Slide Review Report</a>")
        if deck_done and slide_deck_prompt_used_file(job_id).exists():
            parts.append(f"<a class='button secondary' href='{url_for('download_slide_deck_prompt_used', job_id=job_id)}'>Download Prompt Used</a>")
        if deck_status != "not_started":
            parts.append(f"<a class='button secondary' href='{url_for('review_slide_deck', job_id=job_id)}'>Review Slide Deck</a>")
    parts.append("</div></div>")
    return "".join(parts)


def review_report_file(job_id: str) -> Path:
    return job_dir(job_id) / "review_report.md"


def slide_deck_zip_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / f"{job_id}_slide_deck.zip"


def slide_deck_review_report_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "review_report.md"


def slide_deck_coverage_report_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "source_coverage_report.md"


def slide_deck_prompt_used_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "prompt_used.md"


def slide_deck_state_file(job_id: str) -> Path:
    return SLIDE_DECK_OUTPUTS_DIR / job_id / "deck_state.json"


def load_slide_deck_state(job_id: str) -> dict[str, Any]:
    state_path = slide_deck_state_file(job_id)
    default = {
        "job_id": job_id,
        "status": "not_started",
        "source_char_count": 0,
        "source_word_count": 0,
        "chunk_count": 0,
        "slide_count": 0,
        "rendered_slide_count": 0,
        "pdf_created": False,
        "zip_created": slide_deck_zip_file(job_id).exists(),
        "coverage_status": "UNKNOWN",
        "prompt_profile": "",
        "prompt_engine_status": "not_started",
        "latest_error": "",
        "started_at": "",
        "finished_at": "",
    }
    if not state_path.exists():
        return default
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            default.update(data)
    except Exception:
        default["status"] = "failed"
        default["latest_error"] = "Could not read slide deck progress."
    default["zip_created"] = default.get("zip_created") or slide_deck_zip_file(job_id).exists()
    return default


def job_by_id(job_id: str) -> dict[str, Any] | None:
    for job in load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def summarize_failure(job_id: str) -> str:
    text = job_log(job_id, max_chars=12000)
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if "[ERROR]" in stripped:
            return stripped.split("[ERROR]", 1)[-1].strip()
        if "Missing GEMINI_API_KEY" in stripped or "Could not read PDF pages" in stripped:
            return stripped
    return "Processing finished but no Word document was produced. Check the recent log."


def run_slide_deck_job(job_id: str) -> None:
    try:
        generate_image_slide_deck(job_id, job_dir(job_id), SLIDE_DECK_OUTPUTS_DIR, completed_docs_dir=COMPLETED_DOCS_DIR)
    except Exception:
        logging.exception("Ayurveda slide deck generation failed for job %s", job_id)
    finally:
        with deck_generation_lock:
            running_deck_generations.discard(job_id)


def start_slide_deck_generation(job_id: str) -> bool:
    with deck_generation_lock:
        if job_id in running_deck_generations:
            return False
        running_deck_generations.add(job_id)
    threading.Thread(target=run_slide_deck_job, args=(job_id,), daemon=True).start()
    return True


def run_job(job: dict[str, Any]) -> None:
    job_id = job["id"]
    with jobs_lock:
        running_job_ids.add(job_id)
    try:
        folder = job_dir(job_id)
        log_dir = folder / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "interface_run.log"
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        update_job(job_id, status="running", started_at=now_text())
        return_code: int | None = None
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"=== Job started {now_text()} ===\n")
                log.flush()
                proc = subprocess.Popen(
                    [sys.executable, str(PROCESSOR)],
                    cwd=str(folder),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                )
                with jobs_lock:
                    running_processes[job_id] = proc
                return_code = proc.wait()
                log.write(f"\n=== Job finished with code {return_code} at {now_text()} ===\n")
        finally:
            with jobs_lock:
                running_processes.pop(job_id, None)

        outputs = job_output_files(job_id)
        published_outputs = publish_docx_outputs(job_id) if return_code == 0 else []
        failed_pages = failed_page_count(job_id)
        if return_code == 0 and outputs and failed_pages:
            final_status = "completed_with_review_needed"
            failure_reason = f"DOCX created, but {failed_pages} page(s) need manual review."
        elif return_code == 0 and outputs:
            final_status = "completed"
            failure_reason = ""
        else:
            final_status = "failed"
            failure_reason = summarize_failure(job_id)
        update_job(
            job_id,
            status=final_status,
            return_code=return_code,
            finished_at=now_text(),
            output_count=len(outputs),
            published_outputs=published_outputs,
            failure_reason=failure_reason,
        )
    except Exception as exc:
        logging.exception("Worker failed to run job %s", job_id)
        update_job(
            job_id,
            status="failed",
            return_code=None,
            finished_at=now_text(),
            failure_reason=f"Worker failed before processing could finish: {exc}",
        )
    finally:
        with jobs_lock:
            running_processes.pop(job_id, None)
            running_job_ids.discard(job_id)


def claim_queued_jobs(slots: int) -> list[dict[str, Any]]:
    if slots <= 0:
        return []
    ensure_project_files()
    claimed = []
    with jobs_lock:
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?", (slots,)).fetchall()
                for row in rows:
                    job = _row_to_job(row)
                    job["status"] = "running"
                    job["started_at"] = now_text()
                    job["updated_at"] = now_text()
                    job["return_code"] = None
                    job["failure_reason"] = ""
                    
                    row_data = _job_to_row(job)
                    con.execute("""
                        UPDATE jobs
                        SET status = ?, updated_at = ?, started_at = ?, return_code = ?, meta = ?
                        WHERE id = ?
                    """, (job["status"], job["updated_at"], job["started_at"], job["return_code"], row_data[10], job["id"]))
                    claimed.append(job)
    return claimed


def dispatcher_loop() -> None:
    while True:
        with jobs_lock:
            running_ids = [job_id for job_id, proc in running_processes.items() if proc.poll() is None]
            for job_id in list(running_processes):
                if job_id not in running_ids:
                    running_processes.pop(job_id, None)
                    running_job_ids.discard(job_id)
            slots = max_parallel_jobs() - len(running_job_ids)

        claimed_jobs = claim_queued_jobs(max(0, slots))
        with jobs_lock:
            for job in claimed_jobs:
                running_job_ids.add(job["id"])

        for job in claimed_jobs:
            threading.Thread(target=run_job, args=(job,), daemon=True).start()

        threading.Event().wait(3)


def ensure_dispatcher() -> None:
    global dispatcher_started
    if not inline_workers_enabled():
        return
    with dispatcher_lock:
        if dispatcher_started:
            return
        stale_job_ids = [job["id"] for job in load_jobs() if job.get("status") == "running"]
        for stale_job_id in stale_job_ids:
            update_job(
                stale_job_id,
                status="failed",
                return_code=None,
                finished_at=now_text(),
                failure_reason="Server restarted while this job was marked running. Use Run Again to resume it.",
            )
        dispatcher_started = True
        threading.Thread(target=dispatcher_loop, daemon=True).start()


def reset_job(job_id: str) -> bool:
    if not valid_job_id(job_id):
        return False
    job = job_by_id(job_id)
    if job and job.get("status") in {"failed", "completed", "completed_with_review_needed"}:
        update_job(job_id, status="queued", return_code=None)
        return True
    return False


def delete_job(job_id: str) -> tuple[bool, str]:
    if not valid_job_id(job_id):
        return False, "Job was not found."
    with jobs_lock:
        job = job_by_id(job_id)
        if not job:
            return False, "Job was not found."
        if job.get("status") == "running":
            return False, "Running jobs cannot be deleted. Wait for the job to finish or fail first."
        proc = running_processes.get(job_id)
        if proc and proc.poll() is None:
            return False, "Running jobs cannot be deleted. Wait for the job to finish or fail first."
        running_job_ids.discard(job_id)
        with jobs_file_lock():
            with sqlite3.connect(JOBS_DB) as con:
                con.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    folder = job_dir(job_id)
    if folder.exists() and folder.resolve().is_relative_to(JOBS_DIR.resolve()):
        shutil.rmtree(folder)
    return True, "Job deleted."


def page_text_file(job_id: str, folder_name: str, pdf_stem: str, page_number: int, suffix: str) -> Path:
    return job_dir(job_id) / folder_name / pdf_stem / f"page_{page_number:03d}_{suffix}.md"


def first_pdf_stem_for_page(job_id: str, page_number: int) -> str:
    state = load_job_state(job_id)
    for pdf_stem, pages in state.get("failed_pages", {}).items():
        if isinstance(pages, dict) and str(page_number) in pages:
            return str(pdf_stem)
    for section_name in ["verified_pages", "extracted_pages"]:
        for pdf_stem, pages in state.get(section_name, {}).items():
            if isinstance(pages, list) and page_number in [int(value) for value in pages if str(value).isdigit()]:
                return str(pdf_stem)
    return next(iter(state.get("chapter_page_counts", {}) or {"pages": 0}))


def render_page() -> str:
    ensure_dispatcher()
    env = read_env()
    key_ready = bool(env.get("GEMINI_API_KEY")) and env.get("GEMINI_API_KEY") != "your_key_here"
    structured_text = "On" if env.get("CREATE_STRUCTURED_NOTES", "false").lower() == "true" else "Off"
    speed_mode = env.get("SPEED_MODE", "balanced").lower()
    page_workers = env.get("PAGE_WORKERS_PER_JOB", "2")
    prompt_batch = env.get("PROMPT_ENGINE_BATCH_SIZE", "4")
    embedded_text = "On" if env.get("USE_EMBEDDED_PDF_TEXT", "true").lower() == "true" else "Off"
    inline_workers = "Inline" if inline_workers_enabled() else "Separate"
    test_start_page = env.get("TEST_START_PAGE", "1")
    test_max_pages = env.get("TEST_MAX_PAGES", "0")
    jobs = load_jobs()
    running_count = sum(1 for job in jobs if job.get("status") == "running")
    queued_count = sum(1 for job in jobs if job.get("status") == "queued")
    completed_docs = completed_doc_files()
    disk = shutil.disk_usage(ROOT)
    free_disk_mb = disk.free // (1024 * 1024)
    admin_link_html = "<a class='button secondary' href='/admin'>Admin</a>" if is_current_admin() else ""
    upload_message = request.args.get("uploaded")
    deck_message = request.args.get("deck")
    error_message = request.args.get("error")

    job_items = []
    for job in jobs[:80]:
        outputs = [COMPLETED_DOCS_DIR / name for name in job.get("published_outputs", [])]
        if not outputs:
            outputs = job_output_files(job["id"])
        display_status = job.get("status", "queued")
        if display_status == "completed" and outputs and failed_page_count(job["id"]):
            display_status = "completed_with_review_needed"
        actions = "".join(
            f"<a class='button secondary' href='{url_for('download_output', job_id=job['id'], filename=path.name)}'>Download DOCX</a>"
            for path in outputs
        )
        if review_report_file(job["id"]).exists():
            actions += f"<a class='button secondary' href='{url_for('download_review_report', job_id=job['id'])}'>Download Review Report</a>"
        if failed_page_count(job["id"]):
            actions += f"<a class='button secondary' href='{url_for('review_pages', job_id=job['id'])}'>Review Failed Pages</a>"
        if job.get("status") in {"failed", "completed"}:
            actions += f"<form action='{url_for('retry_job', job_id=job['id'])}' method='post'><button class='secondary'>Run Again / Resume</button></form>"
        if job.get("status") != "running":
            actions += f"<form action='{url_for('remove_job', job_id=job['id'])}' method='post'><button class='danger'>Delete</button></form>"
        status_class = f"status {display_status}"
        failure = f"<small class='problem'>{escape(job.get('failure_reason', ''))}</small>" if job.get("failure_reason") else ""
        job_items.append(
            f"<li><div><strong>{escape(job['filename'])}</strong><small><span class='{status_class}'>{escape(friendly_status(display_status))}</span> id {escape(job['id'])}</small>{failure}"
            f"{render_pdf_progress(job, display_status, outputs)}{render_slide_deck_panel(job['id'], display_status)}</div>"
            f"<div class='actions inline'>{actions}</div></li>"
        )
    jobs_html = "".join(job_items) or "<li><div><strong>No jobs yet</strong><small>Upload PDFs to start conversion automatically.</small></div></li>"
    docs_html = "".join(
        f"<li><div><strong>{escape(path.name)}</strong><small>{round(path.stat().st_size / (1024 * 1024), 2)} MB | {datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}</small></div>"
        f"<div class='actions inline'><a class='button secondary' href='{url_for('download_completed_doc', filename=path.name)}'>Download Word File</a></div></li>"
        for path in completed_docs[:60]
    ) or "<li><div><strong>No Word files yet</strong><small>Completed jobs will appear here.</small></div></li>"
    notice_html = ""
    if deck_message:
        notice_html = f"<div class='notice success'>{escape(deck_message)}</div>"
    elif upload_message:
        notice_html = f"<div class='notice success'>{escape(upload_message)} PDF file(s) uploaded and queued for processing.</div>"
    elif error_message:
        notice_html = f"<div class='notice error'>{escape(error_message)}</div>"

    _deck_cache: dict[str, dict[str, Any]] = {}
    for job in jobs:
        _deck_cache[job['id']] = load_slide_deck_state(job['id'])
    initial_signature = "|".join(
        f"{job.get('id')}:{job.get('status')}:{_deck_cache.get(job['id'], {}).get('status')}:{_deck_cache.get(job['id'], {}).get('prompt_engine_status')}" for job in jobs
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Book Processor</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f6f8; --panel: #fff; --panel-2: #fbfdff; --text: #1f2933; --muted: #697586;
      --line: #d9dee5; --accent: #0f766e; --accent-dark: #115e59; --bad: #b42318; --soft: #e8f3f1; --blue: #175cd3;
      --field: #fff; --shadow: rgba(16, 24, 40, .04); --pre-bg: #101828; --pre-text: #e6edf3;
    }}
    [data-theme="dark"] {{
      color-scheme: dark;
      --bg: #111827; --panel: #1f2937; --panel-2: #16202d; --text: #f3f4f6; --muted: #a8b3c2;
      --line: #374151; --accent: #2dd4bf; --accent-dark: #14b8a6; --bad: #f87171; --soft: #143a37; --blue: #93c5fd;
      --field: #111827; --shadow: rgba(0, 0, 0, .22); --pre-bg: #050b14; --pre-text: #dbeafe;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Segoe UI, Arial, sans-serif; font-size: 15px; }}
    header {{ background: var(--panel); border-bottom: 1px solid var(--line); padding: 18px 28px; display: flex; justify-content: space-between; gap: 18px; align-items: center; }}
    h1 {{ margin: 0; font-size: 22px; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px var(--shadow); }}
    h2 {{ margin: 0 0 14px; font-size: 17px; }}
    label {{ display: block; margin: 12px 0 6px; color: var(--muted); font-size: 13px; }}
    input[type=file], input[type=number], select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: var(--field); color: var(--text); }}
    .button, button {{ border: 0; border-radius: 6px; background: var(--accent); color: #fff; padding: 10px 14px; font-weight: 650; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; min-height: 40px; }}
    .button:hover, button:hover {{ background: var(--accent-dark); }}
    .secondary {{ background: var(--panel-2); color: var(--text); border: 1px solid var(--line); }}
    .secondary:hover {{ background: var(--soft); }}
    .danger {{ background: var(--bad); }}
    .danger:hover {{ background: #912018; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .actions.inline {{ margin-top: 0; justify-content: flex-end; }}
    .statusbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .pill {{ border: 1px solid var(--line); background: var(--panel); padding: 8px 10px; border-radius: 999px; color: var(--muted); font-size: 13px; }}
    .pill.ready {{ background: var(--soft); border-color: #b7ddd6; }}
    .notice {{ border-radius: 8px; padding: 12px 14px; border: 1px solid var(--line); background: #fff; }}
    .notice.success {{ background: #ecfdf3; border-color: #abefc6; color: #067647; }}
    .notice.error {{ background: #fef3f2; border-color: #fecdca; color: var(--bad); }}
    .progressbox {{ border: 1px solid #b7ddd6; background: #f0fdfa; border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .progressbox.idle {{ border-color: var(--line); background: var(--panel); }}
    .jobmeta, .deckbox {{ margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }}
    .deckbox {{ background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 10px; }}
    .deckhead {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .chip {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 700; border: 1px solid var(--line); }}
    .chip.good {{ background: #ecfdf3; border-color: #abefc6; color: #067647; }}
    .chip.warn {{ background: #fff6ed; border-color: #fedf89; color: #c4320a; }}
    .chip.neutral {{ background: #eef2f6; color: #475467; }}
    .bar {{ height: 8px; background: #eef2f6; border-radius: 999px; overflow: hidden; margin: 8px 0; }}
    .bar span {{ display: block; height: 100%; background: var(--accent); }}
    .spinner {{ width: 18px; height: 18px; border: 3px solid #b7ddd6; border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; flex: none; }}
    .status {{ display: inline-block; border-radius: 999px; padding: 3px 8px; margin-right: 6px; font-weight: 700; text-transform: uppercase; font-size: 11px; }}
    .status.queued {{ background: #eff8ff; color: #175cd3; }}
    .status.running {{ background: #f0fdfa; color: #0f766e; }}
    .status.completed {{ background: #ecfdf3; color: #067647; }}
    .status.completed_with_review_needed {{ background: #fff6ed; color: #c4320a; }}
    .status.failed {{ background: #fef3f2; color: #b42318; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .steps {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    .step {{ border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 12px; }}
    .step b {{ display: block; margin-bottom: 3px; }}
    .pill strong {{ color: var(--text); }}
    .problem {{ color: var(--bad); }}
    ul {{ margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }}
    li {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px; display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; min-height: 56px; }}
    small {{ display: block; color: var(--muted); margin-top: 3px; }}
    pre {{ margin: 0; max-height: 320px; overflow: auto; background: var(--pre-bg); color: var(--pre-text); padding: 14px; border-radius: 8px; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 12px; }}
    .wide {{ grid-column: 1 / -1; }}
    .note {{ color: var(--muted); margin-top: 8px; }}
    .top-actions {{ display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }}
    @media (max-width: 760px) {{ header {{ flex-direction: column; align-items: flex-start; }} main {{ padding: 14px; }} .grid, .steps {{ grid-template-columns: 1fr; }} li {{ grid-template-columns: 1fr; }} .actions.inline, .top-actions {{ justify-content: flex-start; }} .button, button {{ width: 100%; }} .statusbar {{ width: 100%; }} }}
  </style>
</head>
<body data-theme="light">
  <header>
    <div>
      <h1>Ayurvedic Book Processor</h1>
      <div class="note">Gemini multimodal transcription + verification | parallel DOCX jobs</div>
    </div>
    <div class="top-actions">
      {admin_link_html}
      <button id="themeToggle" class="secondary" type="button">Dark Mode</button>
    </div>
    <div class="statusbar">
      <span class="pill ready">Mode: <strong>{'Full PDF' if test_max_pages == '0' else escape(test_max_pages) + ' pages'}</strong></span>
      <span class="pill">Structured notes: <strong>{structured_text}</strong></span>
      <span class="pill ready">Speed: <strong>{speed_mode.title()}</strong></span>
      <span class="pill ready">Parallel jobs: <strong>{max_parallel_jobs()}</strong></span>
      <span class="pill ready">Page workers: <strong>{escape(page_workers)}</strong></span>
      <span class="pill ready">Embedded text: <strong>{embedded_text}</strong></span>
      <span class="pill ready">Prompt batch: <strong>{escape(prompt_batch)}</strong></span>
      <span class="pill ready">Workers: <strong>{inline_workers}</strong></span>
      <span class="pill ready">Free disk: <strong>{free_disk_mb} MB</strong></span>
      <span class="pill {'problem' if not key_ready else 'ready'}">API key: <strong>{'Set' if key_ready else 'Missing'}</strong></span>
      <span class="pill {'ready' if auth_enabled() else 'problem'}">Login: <strong>{'On' if auth_enabled() else 'Off'}</strong></span>
    </div>
  </header>
  <main>
    {notice_html}
    <section>
      <h2>Workflow</h2>
      <div class="steps">
        <div class="step"><b>1. Upload PDF</b><small>Scanned books or chapters only.</small></div>
        <div class="step"><b>2. Automatic processing</b><small>Up to {max_parallel_jobs()} jobs run together.</small></div>
        <div class="step"><b>3. Download DOCX</b><small>Final Word files appear below.</small></div>
      </div>
    </section>
    <section>
      <h2>Live Progress</h2>
      <div id="progressBox" class="progressbox {'idle' if running_count == 0 and queued_count == 0 else ''}">
        <div>
          <strong id="progressTitle">{'Working on files' if running_count else ('Waiting to start' if queued_count else 'No active jobs')}</strong>
          <small id="progressDetail">Running: {running_count} | Queued: {queued_count} | Completed Word files: {len(completed_docs)}</small>
        </div>
        <div id="progressSpinner" class="spinner" style="display: {'block' if running_count or queued_count else 'none'}"></div>
      </div>
    </section>
    <div class="grid">
      <section>
        <h2>Upload PDFs</h2>
        <form id="uploadForm" action="/upload" method="post" enctype="multipart/form-data">
          <label>Select scanned PDF files</label>
          <input id="pdfInput" type="file" name="pdfs" accept="application/pdf,.pdf" multiple>
          <div class="actions"><button id="uploadButton" type="submit">Upload and Start</button><a class="button secondary" href="/">Refresh Status</a></div>
          <div class="note">After upload, jobs start automatically. Existing running jobs continue; new uploads wait in the queue.</div>
        </form>
      </section>
      <section>
        <h2>Processing Settings</h2>
        <form action="/settings" method="post">
          <div class="note">Use a page limit for tests only. If a long PDF fails near the end, Run Again / Resume reuses saved verified pages.</div>
          <label>Structured Nidan Panchak notes</label>
          <select name="structured">
            <option value="false" {'selected' if structured_text == 'Off' else ''}>Off: exact book transcription only</option>
            <option value="true" {'selected' if structured_text == 'On' else ''}>On: also create structured notes</option>
          </select>
          <label>Start page</label>
          <input type="number" name="test_start_page" min="1" value="{test_start_page}">
          <label>Pages to process</label>
          <select name="test_max_pages">
            <option value="0" {'selected' if test_max_pages == '0' else ''}>Whole PDF</option>
            <option value="1" {'selected' if test_max_pages == '1' else ''}>1 page</option>
            <option value="2" {'selected' if test_max_pages == '2' else ''}>2 pages</option>
            <option value="3" {'selected' if test_max_pages == '3' else ''}>3 pages</option>
            <option value="4" {'selected' if test_max_pages == '4' else ''}>4 pages</option>
            <option value="5" {'selected' if test_max_pages == '5' else ''}>5 pages</option>
            <option value="6" {'selected' if test_max_pages == '6' else ''}>6 pages</option>
            <option value="7" {'selected' if test_max_pages == '7' else ''}>7 pages</option>
            <option value="8" {'selected' if test_max_pages == '8' else ''}>8 pages</option>
            <option value="9" {'selected' if test_max_pages == '9' else ''}>9 pages</option>
            <option value="10" {'selected' if test_max_pages == '10' else ''}>10 pages</option>
          </select>
          <div class="note">Set Start page to 1 and choose 10 pages to process only the first 10 pages. Choose Whole PDF for full processing.</div>
          <label>Speed mode</label>
          <select name="speed_mode">
            <option value="fast" {'selected' if speed_mode == 'fast' else ''}>Fast: one Gemini pass per page</option>
            <option value="balanced" {'selected' if speed_mode == 'balanced' else ''}>Balanced: two-pass verification, parallel pages</option>
            <option value="accuracy" {'selected' if speed_mode == 'accuracy' else ''}>Highest accuracy: slower two-pass verification</option>
          </select>
          <label>Page workers per PDF</label>
          <select name="page_workers">
            <option value="1" {'selected' if page_workers == '1' else ''}>1 page at a time</option>
            <option value="2" {'selected' if page_workers == '2' else ''}>2 pages at a time</option>
            <option value="3" {'selected' if page_workers == '3' else ''}>3 pages at a time</option>
            <option value="4" {'selected' if page_workers == '4' else ''}>4 pages at a time</option>
          </select>
          <label>Maximum parallel jobs</label>
          <select name="parallel">
            <option value="1" {'selected' if max_parallel_jobs() == 1 else ''}>1</option>
            <option value="2" {'selected' if max_parallel_jobs() == 2 else ''}>2</option>
            <option value="3" {'selected' if max_parallel_jobs() == 3 else ''}>3</option>
            <option value="5" {'selected' if max_parallel_jobs() == 5 else ''}>5</option>
            <option value="8" {'selected' if max_parallel_jobs() == 8 else ''}>8</option>
            <option value="10" {'selected' if max_parallel_jobs() == 10 else ''}>10</option>
            <option value="15" {'selected' if max_parallel_jobs() == 15 else ''}>15</option>
            <option value="20" {'selected' if max_parallel_jobs() == 20 else ''}>20</option>
            <option value="25" {'selected' if max_parallel_jobs() == 25 else ''}>25</option>
          </select>
          <div class="actions"><button type="submit">Save Settings</button></div>
        </form>
      </section>
      <section class="wide">
        <h2>Job Queue</h2>
        <div class="statusbar" style="margin-bottom: 12px;">
          <span class="pill">Running: <strong>{running_count}</strong></span>
          <span class="pill">Queued: <strong>{queued_count}</strong></span>
          <span class="pill">Total jobs: <strong>{len(jobs)}</strong></span>
        </div>
        <ul>{jobs_html}</ul>
      </section>
      <section class="wide">
        <h2>Completed Word Documents</h2>
        <ul>{docs_html}</ul>
      </section>
      <section class="wide">
        <h2>Recent Log</h2>
        <pre id="log">{escape(job_log(jobs[0]['id']) if jobs else 'No jobs yet.')}</pre>
      </section>
    </div>
  </main>
  <script>
    const savedTheme = localStorage.getItem('theme') || 'light';
    const themeToggle = document.getElementById('themeToggle');
    document.body.dataset.theme = savedTheme;
    themeToggle.textContent = savedTheme === 'dark' ? 'Light Mode' : 'Dark Mode';
    themeToggle.addEventListener('click', () => {{
      const nextTheme = document.body.dataset.theme === 'dark' ? 'light' : 'dark';
      document.body.dataset.theme = nextTheme;
      localStorage.setItem('theme', nextTheme);
      themeToggle.textContent = nextTheme === 'dark' ? 'Light Mode' : 'Dark Mode';
    }});
    let uploadInProgress = false;
    let lastJobSignature = {json.dumps(initial_signature)};
    const uploadForm = document.getElementById('uploadForm');
    const uploadButton = document.getElementById('uploadButton');
    const pdfInput = document.getElementById('pdfInput');
    if (uploadForm) {{
      uploadForm.addEventListener('submit', (event) => {{
        if (!pdfInput.files || pdfInput.files.length === 0) {{
          return;
        }}
        uploadInProgress = true;
        uploadButton.disabled = true;
        uploadButton.textContent = 'Uploading...';
      }});
    }}
    function applyStatusPayload(data) {{
      if (!data || uploadInProgress) return;
      if (data.log) document.getElementById('log').textContent = data.log;
      const jobs = data.jobs || [];
      const running = jobs.filter((job) => job.status === 'running').length;
      const queued = jobs.filter((job) => job.status === 'queued').length;
      const completed = jobs.filter((job) => job.status === 'completed').length;
      const failed = jobs.filter((job) => job.status === 'failed').length;
      const system = data.system || {{}};
      const nextJobSignature = jobs.map((job) => `${{job.id}}:${{job.status}}:${{(job.deck_state || {{}}).status || 'not_started'}}:${{(job.deck_state || {{}}).prompt_engine_status || 'not_started'}}`).join('|');
      if (nextJobSignature !== lastJobSignature) {{
        window.location.reload();
        return;
      }}
      document.getElementById('progressTitle').textContent = running > 0 ? 'Working on files' : (queued > 0 ? 'Waiting to start' : 'No active jobs');
      document.getElementById('progressDetail').textContent = `Running: ${{running}} | Queued: ${{queued}} | Completed jobs: ${{completed}} | Failed jobs: ${{failed}} | Free disk: ${{system.free_disk_mb || '?'}} MB`;
      document.getElementById('progressSpinner').style.display = (running > 0 || queued > 0) ? 'block' : 'none';
      document.getElementById('progressBox').classList.toggle('idle', running === 0 && queued === 0);
    }}
    async function pollStatus() {{
      if (uploadInProgress) return;
      const response = await fetch('/status');
      applyStatusPayload(await response.json());
    }}
    if (window.EventSource) {{
      const events = new EventSource('/events');
      events.addEventListener('status', (event) => applyStatusPayload(JSON.parse(event.data)));
      events.onerror = () => {{
        events.close();
        setInterval(pollStatus, 5000);
      }};
    }} else {{
      setInterval(pollStatus, 5000);
    }}
  </script>
</body>
</html>"""


@app.get("/")
def index() -> Response:
    ensure_dispatcher()
    return Response(render_page(), mimetype="text/html")


@app.get("/health")
def health() -> Response:
    return jsonify({"ok": True, "time": now_text()})


@app.post("/upload")
def upload() -> Response:
    ensure_dispatcher()
    uploaded = 0
    rejected: list[str] = []
    username = current_username()
    try:
        ensure_disk_space(request.content_length)
    except RuntimeError as exc:
        return redirect(url_for("index", error=str(exc)))

    for file in request.files.getlist("pdfs"):
        if not file or not file.filename:
            continue
        raw_filename = file.filename
        filename = safe_pdf_name(file.filename)
        if Path(raw_filename).suffix.lower() != ".pdf":
            rejected.append(f"{filename}: only PDF files are allowed")
            continue
        if not looks_like_pdf(file):
            rejected.append(f"{filename}: file is not a readable PDF upload")
            continue
        with jobs_lock:
            queued_for_user = queued_count_for_user(username)
            if queued_for_user >= MAX_QUEUED_PER_USER:
                rejected.append(f"{filename}: queue limit reached ({MAX_QUEUED_PER_USER} queued files per user)")
                continue
            if filename_already_active(filename, username):
                rejected.append(f"{filename}: this file is already queued or running")
                continue
            create_job(file, uploaded_by=username)
            uploaded += 1
    if uploaded == 0:
        reason = "; ".join(rejected) if rejected else "No PDF file was selected. Please choose one or more PDF files."
        return redirect(url_for("index", error=reason))
    if rejected:
        return redirect(url_for("index", deck=f"{uploaded} PDF file(s) queued. Skipped: {'; '.join(rejected)}"))
    return redirect(url_for("index", uploaded=str(uploaded)))


@app.post("/settings")
def settings() -> Response:
    structured = request.form.get("structured", "false")
    parallel = request.form.get("parallel", "3")
    speed_mode = request.form.get("speed_mode", "balanced")
    page_workers = request.form.get("page_workers", "2")
    if speed_mode not in {"fast", "balanced", "accuracy"}:
        speed_mode = "accuracy"
    test_start_page = parse_int_field(request.form.get("test_start_page"), 1, 1, 10000)
    test_max_pages = parse_int_field(request.form.get("test_max_pages"), 0, 0, 10)
    page_workers = parse_int_field(page_workers, 1, 1, 4)
    parallel = parse_int_field(parallel, 1, 1, 25)
    updates = {"TEST_START_PAGE": test_start_page, "TEST_MAX_PAGES": test_max_pages, "TEST_PDF_LIMIT": "1"}
    updates.update(
        {
            "CREATE_DOCX": "true",
            "CREATE_STRUCTURED_NOTES": "true" if structured == "true" else "false",
            "RESET_MASTER": "false",
            "MAX_PARALLEL_JOBS": parallel,
            "SPEED_MODE": speed_mode,
            "PAGE_WORKERS_PER_JOB": page_workers,
        }
    )
    write_env(updates)
    return redirect(url_for("index"))


@app.post("/retry/<job_id>")
def retry_job(job_id: str) -> Response:
    reset_job(job_id)
    return redirect(url_for("index"))


@app.post("/delete/<job_id>")
def remove_job(job_id: str) -> Response:
    deleted, message = delete_job(job_id)
    key = "deck" if deleted else "error"
    return redirect(url_for("index", **{key: message}))


@app.get("/jobs/<job_id>/review-pages")
def review_pages(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("Job not found", status=404)
    state = load_job_state(job_id)
    failed_rows = flatten_page_map(state.get("failed_pages", {}))
    rows: list[str] = []
    for pdf_stem, page_number, info in failed_rows:
        row = info if isinstance(info, dict) else {}
        extracted_path = page_text_file(job_id, "extracted_pages", pdf_stem, page_number, "raw")
        verified_path = page_text_file(job_id, "verified_pages", pdf_stem, page_number, "verified")
        extracted = extracted_path.read_text(encoding="utf-8", errors="replace") if extracted_path.exists() else ""
        verified = verified_path.read_text(encoding="utf-8", errors="replace") if verified_path.exists() else ""
        retry_count = state.get("retry_counts", {}).get(f"{pdf_stem}:page_{page_number:03d}:extract", 0)
        rows.append(
            f"<section><h2>Page {page_number}</h2>"
            f"<p><b>PDF:</b> {escape(pdf_stem)}<br><b>Stage:</b> {escape(str(row.get('stage', 'page_processing')))}<br><b>Error:</b> {escape(friendly_error(str(row.get('error', ''))))}<br><b>Retry count:</b> {escape(str(retry_count))}<br><b>Status:</b> needs review</p>"
            f"<form action='{url_for('retry_page', job_id=job_id, page_number=page_number)}' method='post'><input type='hidden' name='pdf_stem' value='{escape(pdf_stem)}'><button>Retry Page</button></form>"
            f"<form action='{url_for('save_page', job_id=job_id, page_number=page_number)}' method='post'><input type='hidden' name='pdf_stem' value='{escape(pdf_stem)}'><label>Manual correction</label><textarea name='corrected_text' rows='10'>{escape(verified or extracted)}</textarea><button>Save Manual Correction</button></form>"
            f"<details><summary>Extracted text</summary><pre>{escape(extracted)}</pre></details><details><summary>Verified text</summary><pre>{escape(verified)}</pre></details></section>"
        )
    body = "".join(rows) or "<p>No failed pages are currently recorded for this job.</p>"
    return Response(
        f"""<!doctype html><html><head><meta charset='utf-8'><title>Review Failed Pages</title>
        <style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2933}}section{{border:1px solid #ddd;border-radius:8px;padding:14px;margin:12px 0}}textarea{{width:100%;font-family:Consolas,monospace}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:12px}}.button,button{{padding:10px 14px;border:0;border-radius:6px;background:#0f766e;color:white;text-decoration:none;display:inline-block;margin:4px}}</style></head><body>
        <h1>Review Failed Pages</h1><p><a class='button' href='/'>Back</a></p>
        <form action='{url_for('retry_failed_pages', job_id=job_id)}' method='post'><button>Retry Failed Pages</button></form>
        <form action='{url_for('regenerate_docx', job_id=job_id)}' method='post'><button>Regenerate DOCX</button></form>{body}</body></html>""",
        mimetype="text/html",
    )


@app.post("/jobs/<job_id>/retry-page/<int:page_number>")
def retry_page(job_id: str, page_number: int) -> Response:
    reset_job(job_id)
    return redirect(url_for("review_pages", job_id=job_id))


@app.post("/jobs/<job_id>/retry-failed-pages")
def retry_failed_pages(job_id: str) -> Response:
    reset_job(job_id)
    return redirect(url_for("index", deck="Failed pages queued for retry."))


@app.post("/jobs/<job_id>/save-page/<int:page_number>")
def save_page(job_id: str, page_number: int) -> Response:
    if not valid_job_id(job_id):
        return Response("Job not found", status=404)
    corrected = request.form.get("corrected_text", "").strip()
    if not corrected:
        return redirect(url_for("review_pages", job_id=job_id, error="Correction text is empty."))
    pdf_stem = request.form.get("pdf_stem") or first_pdf_stem_for_page(job_id, page_number)
    target = page_text_file(job_id, "verified_pages", pdf_stem, page_number, "verified")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(corrected + "\n", encoding="utf-8")
    state = load_job_state(job_id)
    state.setdefault("verified_pages", {}).setdefault(pdf_stem, [])
    pages = {int(value) for value in state["verified_pages"][pdf_stem] if str(value).isdigit()}
    pages.add(page_number)
    state["verified_pages"][pdf_stem] = sorted(pages)
    state.setdefault("manually_verified_pages", {}).setdefault(pdf_stem, [])
    manual_pages = {int(value) for value in state["manually_verified_pages"][pdf_stem] if str(value).isdigit()}
    manual_pages.add(page_number)
    state["manually_verified_pages"][pdf_stem] = sorted(manual_pages)
    failed = state.setdefault("failed_pages", {}).setdefault(pdf_stem, {})
    if isinstance(failed, dict):
        failed.pop(str(page_number), None)
    save_job_state(job_id, state)
    return redirect(url_for("review_pages", job_id=job_id))


@app.post("/jobs/<job_id>/regenerate-docx")
def regenerate_docx(job_id: str) -> Response:
    reset_job(job_id)
    return redirect(url_for("index", deck="DOCX regeneration queued."))


@app.post("/jobs/<job_id>/generate-image-slide-deck")
def generate_image_slide_deck_route(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return redirect(url_for("index", error="Slide deck could not be generated because the job was not found."))
    job = job_by_id(job_id)
    if not job:
        return redirect(url_for("index", error="Slide deck could not be generated because the job was not found."))
    status_text = job.get("status", "queued")
    if status_text == "completed" and job_output_files(job_id) and failed_page_count(job_id):
        status_text = "completed_with_review_needed"
    if status_text not in {"completed", "completed_with_review_needed"}:
        return redirect(url_for("index", error="Process the PDF first before generating the slide deck."))
    started = start_slide_deck_generation(job_id)
    message = "Slide deck generation started." if started else "Slide deck generation is already running."
    return redirect(url_for("index", deck=message))


def status_payload() -> dict[str, Any]:
    jobs = []
    for job in load_jobs():
        item = job.copy()
        if item.get("status") == "completed" and job_output_files(item["id"]) and failed_page_count(item["id"]):
            item["status"] = "completed_with_review_needed"
        item["progress"] = job_progress(item["id"])
        item["deck_state"] = load_slide_deck_state(item["id"])
        jobs.append(item)
    latest_log = job_log(jobs[0]["id"]) if jobs else ""
    disk = shutil.disk_usage(ROOT)
    return {
        "refresh": False,
        "jobs": jobs,
        "log": latest_log,
        "system": {
            "free_disk_mb": disk.free // (1024 * 1024),
            "total_disk_mb": disk.total // (1024 * 1024),
            "queued": sum(1 for job in jobs if job.get("status") == "queued"),
            "running": sum(1 for job in jobs if job.get("status") == "running"),
        },
    }


@app.get("/status")
def status() -> Response:
    return jsonify(status_payload())


@app.get("/events")
def events() -> Response:
    @stream_with_context
    def generate() -> Iterator[str]:
        last_payload = ""
        while True:
            payload = json.dumps(status_payload(), ensure_ascii=False)
            if payload != last_payload:
                yield f"event: status\ndata: {payload}\n\n"
                last_payload = payload
            time.sleep(3)

    return Response(generate(), mimetype="text/event-stream")


@app.get("/download/<job_id>/<path:filename>")
def download_output(job_id: str, filename: str) -> Response:
    if not valid_job_id(job_id):
        return Response("File not found", status=404)
    completed_path = (COMPLETED_DOCS_DIR / Path(filename).name).resolve()
    if (
        COMPLETED_DOCS_DIR.resolve() in completed_path.parents
        and completed_path.suffix.lower() == ".docx"
        and completed_path.exists()
    ):
        return send_file(completed_path, as_attachment=True)
    output_dir = (job_dir(job_id) / "output_notes").resolve()
    path = (output_dir / Path(filename).name).resolve()
    if output_dir not in path.parents or path.suffix.lower() != ".docx" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


@app.get("/download-review-report/<job_id>")
def download_review_report(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("File not found", status=404)
    path = review_report_file(job_id).resolve()
    if job_dir(job_id).resolve() not in path.parents or path.name != "review_report.md" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


@app.get("/jobs/<job_id>/download-image-slide-deck")
def download_image_slide_deck(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("File not found", status=404)
    zip_root = SLIDE_DECK_OUTPUTS_DIR.resolve()
    path = slide_deck_zip_file(job_id).resolve()
    if zip_root not in path.parents or path.name != f"{job_id}_slide_deck.zip" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


@app.get("/jobs/<job_id>/download-slide-deck-review-report")
def download_slide_deck_review_report(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("File not found", status=404)
    output_root = SLIDE_DECK_OUTPUTS_DIR.resolve()
    path = slide_deck_review_report_file(job_id).resolve()
    if output_root not in path.parents or path.name != "review_report.md" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


@app.get("/jobs/<job_id>/download-slide-coverage-report")
def download_slide_deck_coverage_report(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("File not found", status=404)
    output_root = SLIDE_DECK_OUTPUTS_DIR.resolve()
    path = slide_deck_coverage_report_file(job_id).resolve()
    if output_root not in path.parents or path.name != "source_coverage_report.md" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


@app.get("/jobs/<job_id>/download-slide-prompt-used")
def download_slide_deck_prompt_used(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("File not found", status=404)
    output_root = SLIDE_DECK_OUTPUTS_DIR.resolve()
    path = slide_deck_prompt_used_file(job_id).resolve()
    if output_root not in path.parents or path.name != "prompt_used.md" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


@app.get("/jobs/<job_id>/review-slide-deck")
def review_slide_deck(job_id: str) -> Response:
    if not valid_job_id(job_id):
        return Response("Job not found", status=404)
    state = load_slide_deck_state(job_id)
    output_dir = SLIDE_DECK_OUTPUTS_DIR / job_id
    coverage_text = slide_deck_coverage_report_file(job_id).read_text(encoding="utf-8", errors="replace") if slide_deck_coverage_report_file(job_id).exists() else "No coverage report yet."
    review_text = slide_deck_review_report_file(job_id).read_text(encoding="utf-8", errors="replace") if slide_deck_review_report_file(job_id).exists() else "No review report yet."
    prompt_text = slide_deck_prompt_used_file(job_id).read_text(encoding="utf-8", errors="replace")[:20000] if slide_deck_prompt_used_file(job_id).exists() else "No prompt file yet."
    slides_json = output_dir / "slides.json"
    slides_text = slides_json.read_text(encoding="utf-8", errors="replace")[:12000] if slides_json.exists() else "{}"
    prompt_engine = str(state.get("prompt_engine_status", "not_started"))
    prompt_profile = str(state.get("prompt_profile", ""))
    note_class = "ok" if prompt_engine == "used" else "warn"
    return Response(
        f"""<!doctype html><html><head><meta charset='utf-8'><title>Slide Deck Review</title>
        <style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2933}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:14px 0}}.tile{{border:1px solid #ddd;border-radius:8px;padding:12px;background:#fff}}.tile small{{display:block;color:#697586;margin-top:4px}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:12px;border:1px solid #ddd;border-radius:6px;max-height:520px;overflow:auto}}.button,button{{padding:10px 14px;border:0;border-radius:6px;background:#0f766e;color:white;text-decoration:none;display:inline-block;margin:4px}}.warn{{color:#c4320a;font-weight:700}}.ok{{color:#067647;font-weight:700}}</style></head><body>
        <h1>Ayurveda Slide Deck Review</h1>
        <p><a class='button' href='/'>Back</a></p>
        <form action='{url_for('regenerate_slide_deck', job_id=job_id)}' method='post'><button>Regenerate Slide Deck</button></form>
        <div class='summary'>
          <div class='tile'><b>Prompt Engine</b><small>{escape(friendly_prompt_engine_status(prompt_engine))}</small></div>
          <div class='tile'><b>Prompt Profile</b><small>{escape(prompt_profile or 'not recorded')}</small></div>
          <div class='tile'><b>Coverage</b><small>{escape(str(state.get('coverage_status', 'UNKNOWN')))}</small></div>
          <div class='tile'><b>Slides</b><small>{escape(str(state.get('rendered_slide_count', 0)))} / {escape(str(state.get('slide_count', 0)))}</small></div>
        </div>
        <p class='{note_class}'>{escape(prompt_status_note(prompt_engine))}</p>
        <h2>Status</h2><pre>{escape(json.dumps(state, ensure_ascii=False, indent=2))}</pre>
        <h2>Coverage Report</h2><pre>{escape(coverage_text)}</pre>
        <h2>Review Report</h2><pre>{escape(review_text)}</pre>
        <h2>Prompt Used</h2><pre>{escape(prompt_text)}</pre>
        <h2>Slides JSON Preview</h2><pre>{escape(slides_text)}</pre>
        </body></html>""",
        mimetype="text/html",
    )


@app.post("/jobs/<job_id>/regenerate-slide-deck")
def regenerate_slide_deck(job_id: str) -> Response:
    return generate_image_slide_deck_route(job_id)


@app.get("/download-doc/<path:filename>")
def download_completed_doc(filename: str) -> Response:
    path = (COMPLETED_DOCS_DIR / Path(filename).name).resolve()
    if COMPLETED_DOCS_DIR.resolve() not in path.parents or path.suffix.lower() != ".docx" or not path.exists():
        return Response("File not found", status=404)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    ensure_project_files()
    ensure_dispatcher()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "7860"))
    app.run(host=host, port=port, debug=False, threaded=True)
