# Ayurvedic Book Processor Internal Runbook

This setup is intended for a small trusted internal team of about 25 people.
Do not expose it publicly without adding stronger authentication, HTTPS, backups,
and server monitoring.

## 1. Configure `.env`

Required values:

```text
GEMINI_API_KEY=your_real_key
GEMINI_MODEL=gemini-2.5-flash
GEMINI_EXTRACTION_MODEL=gemini-2.5-flash
GEMINI_VERIFICATION_MODEL=gemini-2.5-flash
GEMINI_FORMAT_MODEL=gemini-2.5-flash
SLIDE_PROMPT_MODEL=gemini-2.5-flash
POPPLER_PATH=C:\poppler\bin
MAX_RETRIES=4
GEMINI_DELAY_SECONDS=0
GEMINI_TIMEOUT_SECONDS=180
SPEED_MODE=fast
PAGE_WORKERS_PER_JOB=2
MAX_PARALLEL_JOBS=2
PROMPT_ENGINE_BATCH_SIZE=4
USE_EMBEDDED_PDF_TEXT=true
FORCE_REPROCESS_PAGES=false
DISABLE_INLINE_WORKERS=false
CREATE_STRUCTURED_NOTES=false
CREATE_DOCX=true
TEST_START_PAGE=1
TEST_MAX_PAGES=0
MAX_UPLOAD_MB=250
APP_USERNAME=team
APP_PASSWORD=change-this-before-sharing
```

Use `TEST_MAX_PAGES=0` for full PDFs. Use `TEST_MAX_PAGES=5` or `8` for demos
or free API keys.

For production usage with a paid API key, keep:

```text
SPEED_MODE=fast
PAGE_WORKERS_PER_JOB=2
MAX_PARALLEL_JOBS=2
```

If this runs without `429`, timeout, or laptop CPU/RAM pressure for a full day,
try `MAX_PARALLEL_JOBS=3` or `PAGE_WORKERS_PER_JOB=3`, but do not increase both
at the same time.

## Speed Levers Enabled

1. Paid Gemini tier and quota increase: done in Google AI/Cloud Console.
2. Parallel PDF processing: `MAX_PARALLEL_JOBS` and `PAGE_WORKERS_PER_JOB`.
3. Model routing: extraction, verification, formatting, and slide planning can use separate model env vars.
4. Fewer slide calls: `PROMPT_ENGINE_BATCH_SIZE=4` batches prompt planning.
5. Page cache/resume: verified pages are reused unless `FORCE_REPROCESS_PAGES=true`.
6. Embedded PDF text: `USE_EMBEDDED_PDF_TEXT=true` skips OCR where selectable text exists.
7. Separate workers: use `start_web.ps1` plus one or more `start_worker.ps1` windows.
8. Non-urgent batch path: use `batch_prompt_planner.py` for async Gemini Batch API prompt planning.

## 2. Start The Internal Server

For a 25-person team, the recommended production setup uses Docker to ensure all system dependencies (like Poppler and Devanagari fonts) are perfectly configured.

Run this single command in your terminal or command prompt:

```bash
docker-compose up --build -d
```

This will build the container and start the web server with 50 Waitress threads, easily supporting 25 concurrent users. The background dispatcher will run automatically.

To view logs:
```bash
docker-compose logs -f
```

To stop the server:
```bash
docker-compose down
```

Open locally:

```text
http://127.0.0.1:7860
```

Other people on the same network should open:

```text
http://YOUR-SERVER-IP:7860
```

Find your IP with `ipconfig` (Windows) or `ifconfig` (Linux/Mac).

## 3. Operational Rules

- Share the login only with trusted staff.
- Upload valid PDF files only.
- Keep one or two jobs running at once until quota is proven.
- If the server restarts, any job that was marked running will show as failed
  with a restart message. Click `Run Again`; already verified pages are reused.
- If a long book fails near the end, click `Run Again / Resume`. The processor
  uses that PDF's actual page count, skips pages already listed in
  `processing_state.json` with saved files in `verified_pages`, then retries the
  missing pages and final DOCX creation.
- If a job fails, read the failure reason shown under the job row and the Recent Log.
- Completed Word files are copied to `completed_docs`.
- Upload the `.docx` to Google Drive, then open with Google Docs.

## 4. Backup Folders

Back up these folders regularly:

```text
completed_docs
jobs
logs
```

Also back up `.env` securely because it contains the API key.

## 5. Known Limits

- Basic username/password login only.
- No per-user accounts.
- No automatic Google Docs upload.
- No HTTPS unless you put it behind a reverse proxy or private tunnel.
- No database server; jobs are stored in `jobs/jobs.json`.

For a larger rollout, move this to a real server with HTTPS, backups, monitoring,
and a proper database-backed job queue.
