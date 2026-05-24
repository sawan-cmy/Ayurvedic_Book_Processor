# Post-Demo Roadmap

This is the upgrade list for making Ayurvedic Book Processor ready for larger production use after the live demo.

## 1. Stabilize Current App

- Fix the pytest temp-folder hang.
- Add a fast test suite that finishes in under 1 minute.
- Add app startup and `/status` smoke tests.
- Remove or repair permission-blocked cache and lock files.
- Keep the current single-machine demo flow stable while larger changes are built separately.

## 2. Production Queue

- Replace Flask inline queue behavior with Redis plus RQ, Celery, or Dramatiq.
- Run processing in separate worker processes.
- Add job leases, worker heartbeat, retry count, and stuck-job recovery.
- Prevent duplicate processing when workers restart or crash.
- Keep Flask focused on upload, status display, and downloads.

## 3. Database Upgrade

- Move jobs, users, settings, and audit events from SQLite to Postgres.
- Add database migrations.
- Track uploads, retries, deletes, downloads, and admin actions.
- Add better job metadata: total pages, completed pages, failed pages, duration, owner, and cost estimate.

## 4. Storage Upgrade

- Move PDFs, page images, logs, DOCX files, and slide decks out of repo folders.
- Use a dedicated data volume first.
- Move to S3-compatible storage or Google Cloud Storage later.
- Add retention cleanup for old jobs, images, logs, and generated files.
- Keep secrets out of all job folders and exported archives.

## 5. Speed Improvements

- Split processing into stages:
  - Upload
  - Page extraction
  - OCR/Gemini extraction
  - Verification
  - DOCX generation
  - Slide deck generation
- Parallelize page processing safely.
- Use embedded PDF text when available.
- Add fast, balanced, and accuracy modes per job.
- Batch Gemini calls where possible.
- Avoid reprocessing pages that already succeeded.

## 6. Gemini Quota And Cost Control

- Add a global Gemini rate limiter across all workers.
- Track requests per minute, tokens per minute, retries, and failures.
- Add exponential backoff with jitter for quota and transient API errors.
- Add per-job cost and time estimates.
- Decide model usage per stage instead of using one model for everything.

## 7. Security

- Keep `GEMINI_API_KEY`, app passwords, and other secrets out of job `.env` files.
- Rotate any old key that was previously copied into job folders.
- Move secrets to environment variables or a secret manager.
- Put the app behind HTTPS, Tailscale, Cloudflare Tunnel, or a reverse proxy.
- Improve user roles and permissions.
- Add audit logs for admin actions.

## 8. Operations Dashboard

- Show queue depth.
- Show active workers and worker health.
- Show pages processed per minute.
- Show failed jobs and failed pages.
- Show Gemini quota errors and retry rate.
- Show disk usage and cleanup warnings.
- Show recent uploads, downloads, and retries.

## 9. Scale Target

- Define the real target:
  - Jobs per day
  - Pages per job
  - Users per day
  - Required turnaround time
- Load test with dummy PDFs.
- Tune worker count based on Gemini quota, CPU, RAM, and disk speed.
- Design for 100+ queued jobs without slowing the web UI.

## 10. Deployment

- Create a production Docker setup.
- Add `.env.production.example`.
- Add backup and restore instructions.
- Add restart and recovery runbook.
- Add log rotation.
- Add monitoring and alerting.

## Recommended First Sprint

1. Fix pytest and add reliable smoke tests.
2. Add Redis queue plus separate workers.
3. Add Gemini rate limiter.
4. Move job metadata to Postgres.
5. Add storage cleanup and retention.

## Demo Settings Reminder

For the live demo, keep the setup simple:

- Run one web process on one machine.
- Keep `TEST_MAX_PAGES=7` for a fast demo.
- Use `TEST_MAX_PAGES=0` only for full-book production processing.
- Keep `MAX_PARALLEL_JOBS x PAGE_WORKERS_PER_JOB <= 6` until quota is proven.
