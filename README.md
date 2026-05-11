# Ayurvedic Book Processor

Private production tool for converting scanned Ayurvedic textbook PDFs into
verified Word documents using Gemini multimodal transcription and verification.

The system is designed for a small trusted internal team. Users upload PDFs in a
browser, the app processes them as background jobs, saves progress page by page,
and publishes completed `.docx` files for download.

## Current Production Status

Ready for internal production use on a trusted laptop/server and private network.

Not intended for public internet exposure without HTTPS, stronger authentication,
monitoring, backups, and a database-backed job queue.

## Key Features

- Browser-based PDF upload and job queue.
- Basic username/password login.
- Full-PDF processing mode for production.
- Gemini extraction plus verification for higher transcription accuracy.
- Page-by-page progress saving.
- Resume support for failed long jobs.
- Final Word documents copied to `completed_docs`.
- Waitress-based production server startup.
- Upload-size limit and safer production response headers.

## How Processing Works

For every uploaded PDF:

1. The app creates a job folder under `jobs`.
2. The processor reads the PDF's actual page count.
3. Each page is converted to an image.
4. Gemini extracts the visible text.
5. Gemini verifies the extracted text against the image.
6. Verified page text is saved immediately.
7. After all pages are verified, the app creates a `.docx`.
8. Final Word files are copied to `completed_docs`.

If processing fails, click `Run Again / Resume`. Already verified pages are
skipped, and only missing pages/final output steps are retried.

## Important Files

| Path | Purpose |
|---|---|
| `app.py` | Flask web app, upload UI, auth, job queue, downloads, background runner. |
| `ultimate_book_processor.py` | PDF conversion, Gemini calls, page verification, DOCX generation. |
| `start_production.ps1` | Starts the production Waitress server. |
| `.env` | Real production settings and secrets. Do not share publicly. |
| `.env.example` | Safe configuration template without real secrets. |
| `PRODUCTION_RUNBOOK.md` | Operational checklist and production rules. |
| `CODE_EXPLANATION_FOR_COWORKERS.docx` | Line-by-line code explanation for coworkers. |
| `CODE_EXPLANATION_FOR_COWORKERS.pdf` | PDF version of the line-by-line explanation. |

## Important Folders

| Folder | Purpose |
|---|---|
| `jobs` | Per-upload job folders, logs, state, resumable page output. |
| `completed_docs` | Final Word documents ready for staff to download/use. |
| `logs` | Project-level logs. Job-specific logs are under each job folder. |
| `pdfs` | Root script input folder; web uploads use per-job `pdfs` folders. |
| `output_notes` | Root script output folder; web jobs use per-job `output_notes`. |
| `extracted_pages` | Raw page transcription storage. |
| `verified_pages` | Verified page transcription storage used for resume. |
| `chapter_sources` | Combined verified page text before DOCX output. |

## Production Configuration

Recommended `.env` values for tomorrow's production run:

```text
TEST_MAX_PAGES=0
TEST_PDF_LIMIT=1
SPEED_MODE=accuracy
PAGE_WORKERS_PER_JOB=1
MAX_PARALLEL_JOBS=1
CREATE_DOCX=true
CREATE_STRUCTURED_NOTES=false
FORCE_REPROCESS_PAGES=false
USE_EMBEDDED_PDF_TEXT=true
EXACT_TEXT_ONLY=false
MAX_RETRIES=5
GEMINI_TIMEOUT_SECONDS=180
```

Keep parallelism conservative until Gemini quota and machine capacity are proven.
Set `FORCE_REPROCESS_PAGES=true` when you need to overwrite old raw/verified page outputs after prompt changes or bad OCR results.
Set `EXACT_TEXT_ONLY=true` when you only want real selectable PDF text copied from the file. In that mode scanned-image pages are marked failed instead of being converted with OCR.

## Start The Server

Run PowerShell:

```powershell
cd "C:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor"
.\start_production.ps1
```

Open locally:

```text
http://127.0.0.1:7860
```

For users on the same private network:

```text
http://YOUR-LAPTOP-IP:7860
```

If Windows Firewall prompts, allow Python/Waitress on private networks.

## Staff Workflow

1. Open the app URL.
2. Log in with the shared internal credentials.
3. Upload PDF files.
4. Wait for the job to complete.
5. Download final Word files from `Completed Word Documents`.
6. If a job fails, click `Run Again / Resume`.

Staff should not change processing settings unless the operator approves it.

## Resume Behavior

Resume is not tied to any fixed page count. Each PDF uses its own actual page
count.

Example:

- A 73-page book fails on page 70.
- Pages 1-69 are already verified and saved.
- Click `Run Again / Resume`.
- The processor skips pages 1-69 and retries missing pages.

This prevents wasting Gemini calls on pages that already finished.

## Monitoring

During production, watch:

- job status in the web UI,
- `Recent Log` in the web UI,
- files appearing in `completed_docs`,
- Gemini quota/cost dashboard,
- laptop/server power and internet stability.

Useful job logs:

```text
jobs/<job_id>/logs/interface_run.log
jobs/<job_id>/logs/processor.log
```

## Backup

Back up these regularly:

```text
completed_docs
jobs
logs
.env
```

`jobs` is important because it contains resumable progress.

`.env` contains the paid API key, so store it securely.

## Troubleshooting

### Job Failed

Read the failure reason in the UI and click `Run Again / Resume`.

### No Word File Appears

Check:

```text
jobs/<job_id>/logs/processor.log
```

The most common reasons are PDF conversion errors, Gemini timeouts, or failed
pages that need a resume.

### App Does Not Open

Restart the server with:

```powershell
.\start_production.ps1
```

Then check:

```text
http://127.0.0.1:7860/health
```

### Other Users Cannot Connect

Check:

- same private network,
- correct laptop IP address,
- Windows Firewall allowed private network access,
- server is running on `HOST=0.0.0.0`.

## Security Notes

- Do not share `.env`.
- Do not share the Gemini API key.
- Use a strong `APP_PASSWORD`.
- Keep this on a trusted private network.
- Do not expose directly to the public internet.

## Documentation For Coworkers

Line-by-line explanation documents:

- `CODE_EXPLANATION_FOR_COWORKERS.docx`
- `CODE_EXPLANATION_FOR_COWORKERS.pdf`

Operational checklist:

- `PRODUCTION_RUNBOOK.md`

## One-Sentence Summary

This is a private PDF-to-Word processing queue that uses Gemini to transcribe and
verify scanned Ayurvedic book pages, saves every page as it goes, and resumes
failed jobs without starting over.
