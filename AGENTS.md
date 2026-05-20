# AGENTS.md - Ayurvedic Book Processor

This file is read by the Codex agent before every task.
Read the entire file before writing any code.

## Project Overview

A Flask web application that converts scanned Ayurvedic textbook PDFs (Hindi/Sanskrit
Devanagari) into verified Word documents and AI-illustrated slide decks. Used by an
internal team of up to 100 people on a private network. Runs inside Docker.

Stack: Python 3.11, Flask, Waitress, Gemini API (paid tier), PIL/Pillow,
python-docx, pdf2image (Poppler), SQLite (after migration from JSON).

## File Map - Read These Before Touching Anything

| File | What It Does | Touch Carefully Because |
| --- | --- | --- |
| app.py | Flask app, all routes, job queue, auth, UI HTML | 1480 lines, HTML is inline f-strings |
| ultimate_book_processor.py | PDF -> image -> Gemini OCR -> verify -> docx | Core processing pipeline |
| image_deck_generator.py | Slide deck orchestration | Calls Gemini + Imagen in sequence |
| image_deck_renderer.py | PIL rendering + Imagen AI image gen | Devanagari font handling is fragile |
| image_deck_exporter.py | PDF/zip/coverage report export | Coverage check is exact-match |
| image_deck_prompts.py | ALL Gemini prompt strings | DO NOT EDIT prompt strings |
| worker.py | Standalone background dispatcher | Entry point for Docker worker |
| utils.py | Shared helpers | Create this if missing |

## Absolute Rules - Never Break These

- Never edit prompt strings in image_deck_prompts.py.
  They are production-tuned for Devanagari accuracy.
- Never load all PDF pages into memory at once.
  Always use `first_page=N, last_page=N` in `convert_from_path()`.
  A 500-page book at 300 DPI = 6-8GB RAM if loaded all at once.
- Never use raw filename for path construction.
  Always `werkzeug.utils.secure_filename()` first, then check the resolved
  path stays inside the job directory.
- Never write to `jobs.json` (or `jobs.db`) without a lock.
  Use `jobs_file_lock()` context manager for file-level locking and
  `jobs_lock` threading.Lock for in-process locking. Always acquire both.
- Never silently swallow exceptions in job processing.
  Every exception must be logged and surfaced as a job error with a
  human-readable message. Users cannot read Python tracebacks.
- Never delete a running job.
  Check `job["status"] == "running"` before any delete operation.
- All new code must use `from __future__ import annotations`.
- All functions must have type annotations.
- Use logging not print. Match the log format in the existing file.
- Do not add pip dependencies without adding them to `requirements.txt` too.

## Current Known Issues (What You Are Fixing)

### Critical

- `jobs.json` race condition under concurrent load -> migrate to SQLite WAL
- Single shared password -> per-user accounts with bcrypt
- Stale running jobs after restart -> recover on startup
- Slide images generated sequentially -> parallelise with ThreadPoolExecutor
- No per-user rate limiting -> add queue limit per user (max 3 queued per user)
- No disk space check before job start -> add `shutil.disk_usage` check
- PDF loaded all at once in some paths -> fix to page-by-page
- No upload deduplication -> check for same filename already running

### Moderate

- `count_pdf_pages()` uses fragile regex -> use `pdfinfo_from_path`
- Proxy-clearing code copy-pasted in 3 files -> move to `utils.py`
- Temp images not always cleaned up -> use context manager
- No Devanagari ratio validation after OCR -> add ratio check
- Gemini JSON sometimes wrapped in markdown fences -> strip before parsing
- `valid_job_id()` has edge case with all-underscore prefix -> use regex

### UI

- No real-time progress (polling only) -> add Server-Sent Events (SSE)
- No admin dashboard -> add `/admin` route
- No disk/queue status visible -> add system status bar
- Mobile layout breaks on small screens -> add responsive CSS
- No dark mode -> add CSS custom properties + toggle

## Architecture: How Jobs Flow

User uploads PDF
-> `create_job()` saves PDF to `jobs/<job_id>/pdfs/`
-> job added to `jobs.db` with `status="queued"`
-> `dispatcher_loop()` picks it up when slot available
-> runs `ultimate_book_processor.py` as subprocess
-> processor writes `verified_pages/` page by page
-> processor creates `.docx` in `output_notes/`
-> app copies `.docx` to `completed_docs/`
-> status set to `"completed"`
-> user downloads from UI

For slide decks:

User clicks "Generate Slide Deck"
-> `image_deck_generator.py` runs in thread
-> calls Gemini for visual plan (JSON)
-> calls Imagen for each slide background
-> PIL renders text over images
-> saves PNG slides + PDF + ZIP
-> status set to `"completed"`

## Database Schema (Target - After SQLite Migration)

```sql
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    uploaded_by TEXT,              -- username, added with per-user auth
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    return_code INTEGER,
    meta        TEXT               -- JSON blob for all other fields
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_uploaded_by ON jobs(uploaded_by);
CREATE INDEX idx_jobs_created_at ON jobs(created_at);

CREATE TABLE users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);
```

## Environment Variables Reference

| Variable | Notes |
| --- | --- |
| GEMINI_API_KEY | Required. Paid tier. |
| GEMINI_MODEL | Default model (`gemini-2.5-flash`) |
| GEMINI_EXTRACTION_MODEL | Use `gemini-2.5-pro` for accuracy |
| GEMINI_VERIFICATION_MODEL | Use `gemini-2.5-pro` for accuracy |
| GEMINI_FORMAT_MODEL | `gemini-2.5-flash` is fine |
| SLIDE_PROMPT_MODEL | `gemini-2.5-flash` is fine |
| DPI | 300 recommended for scanned books |
| MAX_RETRIES | 4 |
| GEMINI_TIMEOUT_SECONDS | 180 |
| PAGE_WORKERS_PER_JOB | 2-4 |
| MAX_PARALLEL_JOBS | 4-8 for 100 users |
| MAX_UPLOAD_MB | 250 |
| APP_USERNAME | Replaced by per-user DB after auth migration |
| APP_PASSWORD | Replaced by per-user DB after auth migration |
| SMTP_FROM | For email notifications |
| SMTP_PASSWORD | Gmail app password |
| ADMIN_EMAIL | Who gets daily summary emails |

## Testing

Tests live in `tests/`. Run with:

```bash
pytest tests/ -v
```

All new features need at least:

- One happy path test
- One failure/edge case test
- One concurrent access test if touching job state

Do not mock the Gemini client unless testing error handling specifically.
Use real small test PDFs in `tests/fixtures/`.

## Code Style

- `from __future__ import annotations` at top of every file
- Type-annotate everything
- Path objects not strings for file paths
- `logging.getLogger(__name__)` not print
- Docstrings for public functions
- Constants in `SCREAMING_SNAKE_CASE` at module level
- No bare `except:` - always catch specific exceptions or `except Exception as e:`

## How To Approach Each Task

1. Read the relevant source file(s) completely first
2. Identify every call site of the function you are changing
3. Make the change
4. Update every call site
5. Add or update tests
6. Run `pytest tests/ -v` and confirm all pass
7. Run `python -c "from app import app; print('import OK')"` to confirm no import errors

## Tasks - Work Through In This Order

When given a task number, implement it completely including tests before
moving to the next.

1. Task 1: SQLite migration (replace `jobs.json`)
2. Task 2: Per-user accounts with bcrypt
3. Task 3: Recover stale jobs on startup
4. Task 4: Parallel AI image generation (ThreadPoolExecutor)
5. Task 5: Per-user upload rate limiting (max 3 queued per user)
6. Task 6: Disk space check before job start
7. Task 7: SSE real-time progress endpoint
8. Task 8: Admin dashboard at `/admin`
9. Task 9: Responsive CSS + dark mode toggle
10. Task 10: Fix `valid_job_id`, `count_pdf_pages`, and `utils.py` extraction
11. Task 11: Devanagari ratio validation
12. Task 12: Imagen output caching
13. Task 13: Daily email summary
14. Task 14: All remaining edge cases from `edge_cases_100users.md`

## Do Not

- Do not rewrite working code to "clean it up" unless it is in scope
- Do not change the Gemini prompts
- Do not change the visual layout of rendered slides
- Do not add Docker or docker-compose changes unless asked
- Do not install packages not in `requirements.txt`
- Do not commit `.env`
