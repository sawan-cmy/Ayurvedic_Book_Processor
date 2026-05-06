# Ayurvedic Book Processor Internal Runbook

This setup is intended for a small trusted internal team of 10 to 20 people.
Do not expose it publicly without adding stronger authentication, HTTPS, backups,
and server monitoring.

## 1. Configure `.env`

Required values:

```text
GEMINI_API_KEY=your_real_key
GEMINI_MODEL=gemini-2.5-flash
POPPLER_PATH=C:\poppler\bin
MAX_RETRIES=5
GEMINI_TIMEOUT_SECONDS=180
SPEED_MODE=accuracy
PAGE_WORKERS_PER_JOB=1
MAX_PARALLEL_JOBS=1
CREATE_STRUCTURED_NOTES=true
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
SPEED_MODE=accuracy
PAGE_WORKERS_PER_JOB=1
MAX_PARALLEL_JOBS=1
```

Increase parallelism only after you confirm Gemini quota and machine capacity.

## 2. Start The Internal Server

Run PowerShell:

```powershell
cd "C:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor"
.\start_production.ps1
```

Open locally:

```text
http://127.0.0.1:7860
```

Other people on the same network should open:

```text
http://YOUR-LAPTOP-IP:7860
```

Find your IP with:

```powershell
ipconfig
```

If Windows Firewall asks, allow Python/Waitress on Private networks.

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
