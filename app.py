from __future__ import annotations

import json
import secrets
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from html import escape

from flask import Flask, Response, jsonify, redirect, request, send_file, url_for
from dotenv import load_dotenv
from werkzeug.exceptions import RequestEntityTooLarge


ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
COMPLETED_DOCS_DIR = ROOT / "completed_docs"
LOG_DIR = ROOT / "logs"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PROCESSOR = ROOT / "ultimate_book_processor.py"
JOBS_DB = JOBS_DIR / "jobs.json"

load_dotenv(ENV_FILE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "250")) * 1024 * 1024

jobs_lock = threading.Lock()
dispatcher_lock = threading.Lock()
running_processes: dict[str, subprocess.Popen[str]] = {}
dispatcher_started = False


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_project_files() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    COMPLETED_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8-sig"), encoding="utf-8")
    if not JOBS_DB.exists():
        save_jobs([])


def valid_job_id(job_id: str) -> bool:
    if len(job_id) != 24:
        return False
    prefix, suffix = job_id[:15], job_id[16:]
    return job_id[15] == "_" and prefix.replace("_", "").isdigit() and suffix.isalnum()


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
        return max(1, min(10, int(read_env().get("MAX_PARALLEL_JOBS", "3"))))
    except ValueError:
        return 3


def app_credentials() -> tuple[str, str]:
    env = read_env()
    return env.get("APP_USERNAME", "").strip(), env.get("APP_PASSWORD", "").strip()


def auth_enabled() -> bool:
    username, password = app_credentials()
    return bool(username and password and password != "change-this-password")


def authorized() -> bool:
    if not auth_enabled():
        return True
    username, password = app_credentials()
    auth = request.authorization
    if not auth:
        return False
    return secrets.compare_digest(auth.username or "", username) and secrets.compare_digest(auth.password or "", password)


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


def safe_pdf_name(filename: str) -> str:
    name = Path(filename).name.strip()
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name or f"uploaded_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"


def looks_like_pdf(pdf_file: Any) -> bool:
    position = pdf_file.stream.tell()
    header = pdf_file.stream.read(5)
    pdf_file.stream.seek(position)
    return header == b"%PDF-"


def load_jobs() -> list[dict[str, Any]]:
    ensure_project_files()
    try:
        data = json.loads(JOBS_DB.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    temp = JOBS_DB.with_suffix(".json.tmp")
    temp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(JOBS_DB)


def update_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        jobs = load_jobs()
        for job in jobs:
            if job["id"] == job_id:
                job.update(updates)
                job["updated_at"] = now_text()
                break
        save_jobs(jobs)


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def create_job(pdf_file: Any) -> None:
    env = read_env()
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    folder = job_dir(job_id)
    pdf_dir = folder / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_pdf_name(pdf_file.filename or "uploaded.pdf")
    pdf_file.save(pdf_dir / filename)

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
        "created_at": now_text(),
        "updated_at": now_text(),
        "return_code": None,
    }
    with jobs_lock:
        jobs = load_jobs()
        jobs.insert(0, job)
        save_jobs(jobs)


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


def summarize_failure(job_id: str) -> str:
    text = job_log(job_id, max_chars=12000)
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if "[ERROR]" in stripped:
            return stripped.split("[ERROR]", 1)[-1].strip()
        if "Missing GEMINI_API_KEY" in stripped or "Could not read PDF pages" in stripped:
            return stripped
    return "Processing finished but no Word document was produced. Check the recent log."


def run_job(job: dict[str, Any]) -> None:
    job_id = job["id"]
    folder = job_dir(job_id)
    log_dir = folder / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "interface_run.log"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    update_job(job_id, status="running", started_at=now_text())
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

    with jobs_lock:
        running_processes.pop(job_id, None)
    outputs = job_output_files(job_id)
    published_outputs = publish_docx_outputs(job_id) if return_code == 0 else []
    update_job(
        job_id,
        status="completed" if return_code == 0 and outputs else "failed",
        return_code=return_code,
        finished_at=now_text(),
        output_count=len(outputs),
        published_outputs=published_outputs,
        failure_reason="" if return_code == 0 and outputs else summarize_failure(job_id),
    )


def dispatcher_loop() -> None:
    while True:
        with jobs_lock:
            jobs = load_jobs()
            running_ids = [job_id for job_id, proc in running_processes.items() if proc.poll() is None]
            for job_id in list(running_processes):
                if job_id not in running_ids:
                    running_processes.pop(job_id, None)
            slots = max_parallel_jobs() - len(running_processes)
            queued = sorted(
                [job for job in jobs if job.get("status") == "queued"],
                key=lambda job: job.get("created_at", ""),
            )

        for job in queued[: max(0, slots)]:
            threading.Thread(target=run_job, args=(job,), daemon=True).start()

        threading.Event().wait(3)


def ensure_dispatcher() -> None:
    global dispatcher_started
    with dispatcher_lock:
        if dispatcher_started:
            return
        with jobs_lock:
            jobs = load_jobs()
            changed = False
            for job in jobs:
                if job.get("status") == "running":
                    job["status"] = "failed"
                    job["return_code"] = None
                    job["failure_reason"] = "Server restarted while this job was marked running. Use Run Again to resume it."
                    job["updated_at"] = now_text()
                    changed = True
            if changed:
                save_jobs(jobs)
        dispatcher_started = True
        threading.Thread(target=dispatcher_loop, daemon=True).start()


def reset_job(job_id: str) -> bool:
    if not valid_job_id(job_id):
        return False
    jobs = load_jobs()
    found = False
    for job in jobs:
        if job["id"] == job_id and job.get("status") in {"failed", "completed"}:
            job["status"] = "queued"
            job["return_code"] = None
            job["updated_at"] = now_text()
            found = True
    save_jobs(jobs)
    return found


def delete_job(job_id: str) -> None:
    if not valid_job_id(job_id):
        return
    with jobs_lock:
        proc = running_processes.get(job_id)
        if proc and proc.poll() is None:
            proc.terminate()
        jobs = [job for job in load_jobs() if job["id"] != job_id]
        save_jobs(jobs)
    folder = job_dir(job_id)
    if folder.exists() and folder.resolve().is_relative_to(JOBS_DIR.resolve()):
        shutil.rmtree(folder)


def render_page() -> str:
    ensure_dispatcher()
    env = read_env()
    key_ready = bool(env.get("GEMINI_API_KEY")) and env.get("GEMINI_API_KEY") != "your_key_here"
    structured_text = "On" if env.get("CREATE_STRUCTURED_NOTES", "false").lower() == "true" else "Off"
    speed_mode = env.get("SPEED_MODE", "balanced").lower()
    page_workers = env.get("PAGE_WORKERS_PER_JOB", "2")
    test_start_page = env.get("TEST_START_PAGE", "1")
    test_max_pages = env.get("TEST_MAX_PAGES", "0")
    jobs = load_jobs()
    running_count = sum(1 for job in jobs if job.get("status") == "running")
    queued_count = sum(1 for job in jobs if job.get("status") == "queued")
    completed_docs = completed_doc_files()
    upload_message = request.args.get("uploaded")
    error_message = request.args.get("error")

    job_items = []
    for job in jobs[:80]:
        outputs = [COMPLETED_DOCS_DIR / name for name in job.get("published_outputs", [])]
        if not outputs:
            outputs = job_output_files(job["id"])
        links = "".join(
            f"<a class='button secondary' href='{url_for('download_output', job_id=job['id'], filename=path.name)}'>Download DOCX</a>"
            for path in outputs
        )
        actions = links
        if job.get("status") in {"failed", "completed"}:
            actions += f"<form action='{url_for('retry_job', job_id=job['id'])}' method='post'><button class='secondary'>Run Again / Resume</button></form>"
        if job.get("status") != "running":
            actions += f"<form action='{url_for('remove_job', job_id=job['id'])}' method='post'><button class='danger'>Delete</button></form>"
        status_class = f"status {job.get('status', 'queued')}"
        failure = f"<small class='problem'>{escape(job.get('failure_reason', ''))}</small>" if job.get("failure_reason") else ""
        job_items.append(
            f"<li><div><strong>{escape(job['filename'])}</strong><small><span class='{status_class}'>{escape(job['status'])}</span> created {escape(job['created_at'])} | id {escape(job['id'])}</small>{failure}</div>"
            f"<div class='actions inline'>{actions}</div></li>"
        )
    jobs_html = "".join(job_items) or "<li><div><strong>No jobs yet</strong><small>Upload PDFs to start conversion automatically.</small></div></li>"
    docs_html = "".join(
        f"<li><div><strong>{escape(path.name)}</strong><small>{round(path.stat().st_size / (1024 * 1024), 2)} MB | {datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}</small></div>"
        f"<div class='actions inline'><a class='button secondary' href='{url_for('download_completed_doc', filename=path.name)}'>Download Word File</a></div></li>"
        for path in completed_docs[:60]
    ) or "<li><div><strong>No Word files yet</strong><small>Completed jobs will appear here.</small></div></li>"
    notice_html = ""
    if upload_message:
        notice_html = f"<div class='notice success'>{escape(upload_message)} PDF file(s) uploaded and queued for processing.</div>"
    elif error_message:
        notice_html = f"<div class='notice error'>{escape(error_message)}</div>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Book Processor</title>
  <style>
    :root {{
      --bg: #f5f6f8; --panel: #fff; --text: #1f2933; --muted: #697586;
      --line: #d9dee5; --accent: #0f766e; --accent-dark: #115e59; --bad: #b42318; --soft: #e8f3f1; --blue: #175cd3;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Segoe UI, Arial, sans-serif; font-size: 15px; }}
    header {{ background: linear-gradient(180deg, #ffffff 0%, #f7fbfa 100%); border-bottom: 1px solid var(--line); padding: 18px 28px; display: flex; justify-content: space-between; gap: 18px; align-items: center; }}
    h1 {{ margin: 0; font-size: 22px; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04); }}
    h2 {{ margin: 0 0 14px; font-size: 17px; }}
    label {{ display: block; margin: 12px 0 6px; color: var(--muted); font-size: 13px; }}
    input[type=file], input[type=number], select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fff; }}
    .button, button {{ border: 0; border-radius: 6px; background: var(--accent); color: #fff; padding: 10px 14px; font-weight: 650; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; min-height: 40px; }}
    .button:hover, button:hover {{ background: var(--accent-dark); }}
    .secondary {{ background: #eef2f6; color: var(--text); border: 1px solid var(--line); }}
    .secondary:hover {{ background: #e4e9f0; }}
    .danger {{ background: var(--bad); }}
    .danger:hover {{ background: #912018; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .actions.inline {{ margin-top: 0; justify-content: flex-end; }}
    .statusbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .pill {{ border: 1px solid var(--line); background: #fff; padding: 8px 10px; border-radius: 999px; color: var(--muted); font-size: 13px; }}
    .pill.ready {{ background: var(--soft); border-color: #b7ddd6; }}
    .notice {{ border-radius: 8px; padding: 12px 14px; border: 1px solid var(--line); background: #fff; }}
    .notice.success {{ background: #ecfdf3; border-color: #abefc6; color: #067647; }}
    .notice.error {{ background: #fef3f2; border-color: #fecdca; color: var(--bad); }}
    .progressbox {{ border: 1px solid #b7ddd6; background: #f0fdfa; border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .progressbox.idle {{ border-color: var(--line); background: #fff; }}
    .spinner {{ width: 18px; height: 18px; border: 3px solid #b7ddd6; border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; flex: none; }}
    .status {{ display: inline-block; border-radius: 999px; padding: 3px 8px; margin-right: 6px; font-weight: 700; text-transform: uppercase; font-size: 11px; }}
    .status.queued {{ background: #eff8ff; color: #175cd3; }}
    .status.running {{ background: #f0fdfa; color: #0f766e; }}
    .status.completed {{ background: #ecfdf3; color: #067647; }}
    .status.failed {{ background: #fef3f2; color: #b42318; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .steps {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    .step {{ border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 12px; }}
    .step b {{ display: block; margin-bottom: 3px; }}
    .pill strong {{ color: var(--text); }}
    .problem {{ color: var(--bad); }}
    ul {{ margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }}
    li {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px; display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; min-height: 56px; }}
    small {{ display: block; color: var(--muted); margin-top: 3px; }}
    pre {{ margin: 0; max-height: 320px; overflow: auto; background: #101828; color: #e6edf3; padding: 14px; border-radius: 8px; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 12px; }}
    .wide {{ grid-column: 1 / -1; }}
    .note {{ color: var(--muted); margin-top: 8px; }}
    @media (max-width: 760px) {{ header {{ flex-direction: column; align-items: flex-start; }} main {{ padding: 16px; }} .grid {{ grid-template-columns: 1fr; }} li {{ grid-template-columns: 1fr; }} .actions.inline {{ justify-content: flex-start; }} }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Ayurvedic Book Processor</h1>
      <div class="note">Gemini multimodal transcription + verification | parallel DOCX jobs</div>
    </div>
    <div class="statusbar">
      <span class="pill ready">Mode: <strong>{'Full PDF' if test_max_pages == '0' else escape(test_max_pages) + ' pages'}</strong></span>
      <span class="pill">Structured notes: <strong>{structured_text}</strong></span>
      <span class="pill ready">Speed: <strong>{speed_mode.title()}</strong></span>
      <span class="pill ready">Parallel jobs: <strong>{max_parallel_jobs()}</strong></span>
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
    let uploadInProgress = false;
    let lastJobSignature = {json.dumps("|".join(f"{job.get('id')}:{job.get('status')}" for job in jobs))};
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
    setInterval(async () => {{
      if (uploadInProgress) return;
      const response = await fetch('/status');
      const data = await response.json();
      if (data.log) document.getElementById('log').textContent = data.log;
      const jobs = data.jobs || [];
      const running = jobs.filter((job) => job.status === 'running').length;
      const queued = jobs.filter((job) => job.status === 'queued').length;
      const completed = jobs.filter((job) => job.status === 'completed').length;
      const failed = jobs.filter((job) => job.status === 'failed').length;
      const nextJobSignature = jobs.map((job) => `${{job.id}}:${{job.status}}`).join('|');
      if (nextJobSignature !== lastJobSignature) {{
        window.location.reload();
        return;
      }}
      document.getElementById('progressTitle').textContent = running > 0 ? 'Working on files' : (queued > 0 ? 'Waiting to start' : 'No active jobs');
      document.getElementById('progressDetail').textContent = `Running: ${{running}} | Queued: ${{queued}} | Completed jobs: ${{completed}} | Failed jobs: ${{failed}}`;
      document.getElementById('progressSpinner').style.display = (running > 0 || queued > 0) ? 'block' : 'none';
      document.getElementById('progressBox').classList.toggle('idle', running === 0 && queued === 0);
    }}, 8000);
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
    for file in request.files.getlist("pdfs"):
        if not file or not file.filename:
            continue
        filename = safe_pdf_name(file.filename)
        if not filename.lower().endswith(".pdf"):
            rejected.append(f"{filename}: only PDF files are allowed")
            continue
        if not looks_like_pdf(file):
            rejected.append(f"{filename}: file is not a readable PDF upload")
            continue
        if file and file.filename and file.filename.lower().endswith(".pdf"):
            create_job(file)
            uploaded += 1
    if uploaded == 0:
        reason = "; ".join(rejected) if rejected else "No PDF file was selected. Please choose one or more PDF files."
        return redirect(url_for("index", error=reason))
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
    parallel = parse_int_field(parallel, 1, 1, 5)
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
    delete_job(job_id)
    return redirect(url_for("index"))


@app.get("/status")
def status() -> Response:
    jobs = load_jobs()
    latest_log = job_log(jobs[0]["id"]) if jobs else ""
    return jsonify({"refresh": False, "jobs": jobs, "log": latest_log})


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
