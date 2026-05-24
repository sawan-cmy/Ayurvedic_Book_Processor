# Edge Cases Checklist

Date: 2026-05-24

This checklist covers the main edge cases to test before demo and before production v1.

## Fixed In Current Hardening Pass

- Auth fails closed unless `ALLOW_AUTH_BYPASS=true`.
- POST routes require CSRF tokens.
- Normal users only see/control their own jobs.
- Admin-only settings are blocked for normal users.
- Downloads check job ownership.
- Manual page correction paths are constrained inside the job folder.
- Existing job `.env` files were scrubbed of copied secrets.
- New job `.env` files do not store API keys or web passwords.
- Docker builds exclude `.env`, jobs, PDFs, outputs, logs, and local virtualenvs.
- Docker image runs as a non-root user.
- SSE connections are capped.
- Invalid numeric environment values fall back to safe defaults instead of crashing startup.
- Long uploaded filenames are capped.
- PDF header check handles valid PDFs where the `%PDF-` marker is not byte zero.

## Demo-Critical Edge Cases

Test these first because they can affect the live demo.

- App starts with valid `.env`.
- App rejects unauthenticated requests with HTTP 401.
- Homepage loads after login.
- `/status` loads after login.
- Upload one small valid PDF.
- Upload rejects non-PDF files.
- Upload rejects a `.pdf` file that does not start with a PDF header.
- Upload works when `TEST_MAX_PAGES` is set to a small number.
- Job moves from queued to running to completed or failed with a visible reason.
- Failed/completed job can be retried with `Run Again / Resume`.
- Download button appears only after output exists.
- Existing job `.env` files do not contain API keys or passwords.
- Production check runs and only shows expected warnings.

## Authentication And Authorization

- No users exist in the DB.
  - Expected: app should require auth unless `ALLOW_AUTH_BYPASS=true`.
- Wrong password repeated many times.
  - Expected: temporary throttling blocks attempts.
- Normal user tries to access another user's job id.
  - Expected: 404 or forbidden behavior.
- Normal user tries to download another user's DOCX.
  - Expected: 404.
- Normal user tries to change global settings.
  - Expected: 403.
- Admin user manages all jobs.
  - Expected: allowed.
- Admin user tries to delete their own account.
  - Expected: refused.

## CSRF And Browser Safety

- POST without `_csrf`.
  - Expected: HTTP 403.
- POST with invalid `_csrf`.
  - Expected: HTTP 403.
- POST with valid `_csrf`.
  - Expected: route runs normally.
- Upload form includes hidden CSRF field.
- Retry/delete/settings/admin forms include hidden CSRF field.
- Browser refresh does not accidentally repeat a destructive action.

## Upload Edge Cases

- Empty upload form.
  - Expected: clear error message.
- Multiple PDFs uploaded at once.
  - Expected: up to queue limit accepted.
- More than queue limit uploaded.
  - Expected: extras rejected with explanation.
- Duplicate filename uploaded while queued/running.
  - Expected: rejected.
- Filename with path traversal like `../../book.pdf`.
  - Expected: sanitized and saved inside job folder.
- Filename with Unicode or very long name.
  - Expected: safe filename or controlled error.
- Upload larger than `MAX_UPLOAD_MB`.
  - Expected: rejected.
- Low disk space.
  - Expected: upload blocked before processing.
- Corrupt PDF.
  - Expected: job fails cleanly with reason.
- Password-protected PDF.
  - Expected: job fails cleanly with reason.
- PDF with zero pages or unreadable page count.
  - Expected: job fails cleanly with reason.

## PDF Processing Edge Cases

- PDF with embedded text.
  - Expected: embedded text path works if enabled.
- Scanned image-only PDF.
  - Expected: Gemini extraction path works.
- Mixed text and scanned pages.
  - Expected: pages process independently.
- Very large page image.
  - Expected: timeout or controlled failure, not app crash.
- Very high page count.
  - Expected: respects `TEST_MAX_PAGES` in demo mode.
- Poppler missing or invalid path.
  - Expected: production warning and job failure reason.
- DPI too high.
  - Expected: slower processing but no crash; production should cap it later.

## Queue And Retry Edge Cases

- Server restarts while job is running.
  - Expected: job marked failed with resume instruction.
- Job folder missing when retry is clicked.
  - Expected: retry refused with visible error.
- Job folder exists but PDF missing.
  - Expected: job fails cleanly.
- Two users upload same filename.
  - Expected: allowed per user if ownership is separate.
- Same user uploads same filename while active.
  - Expected: rejected.
- Multiple browser tabs upload at same time.
  - Expected: queue limit still enforced.
- Duplicate workers accidentally started.
  - Expected: avoid for demo; production v1 needs real leases.

## Gemini/API Edge Cases

- Missing `GEMINI_API_KEY`.
  - Expected: production warning and job failure reason.
- Invalid API key.
  - Expected: job fails cleanly with key/billing message.
- Free-tier quota exceeded.
  - Expected: retry/backoff or clear failure message.
- Gemini timeout.
  - Expected: page marked failed/review needed.
- Gemini returns empty text.
  - Expected: page marked failed or review needed.
- Gemini returns malformed/partial content.
  - Expected: verification or review catches it.
- Network disconnected mid-job.
  - Expected: retryable failure, no app crash.

## Review And Manual Correction

- Failed page review with no failed pages.
  - Expected: clear empty state.
- Manual correction submitted empty.
  - Expected: rejected.
- Manual correction contains large text.
  - Expected: saved or size-limited later.
- Crafted `pdf_stem` path in form.
  - Expected: sanitized, cannot write outside job folder.
- Regenerate DOCX after manual corrections.
  - Expected: corrected page included.

## Download And Output Edge Cases

- Download before file exists.
  - Expected: 404.
- Download path traversal filename.
  - Expected: 404.
- Download another user's output.
  - Expected: 404.
- Completed docs folder missing.
  - Expected: app recreates or fails cleanly.
- DOCX generation succeeds but some pages failed.
  - Expected: `completed_with_review_needed`.
- Review report missing.
  - Expected: no broken download link.

## Slide Deck Edge Cases

- Generate slide deck before PDF processing completes.
  - Expected: refused with clear message.
- Generate slide deck twice.
  - Expected: second request says already running.
- Slide deck generation fails.
  - Expected: failure visible and retry possible.
- Missing slide deck ZIP.
  - Expected: no download or 404.
- Large source text.
  - Expected: chunking or controlled failure.
- AI image generation unavailable.
  - Expected: fallback or clear warning.

## UI/Browser Edge Cases

- Multiple tabs open.
  - Expected: status still works; SSE cap prevents overload.
- SSE unavailable.
  - Expected: page can still be refreshed manually.
- Mobile viewport.
  - Expected: no overlapping buttons/text.
- Long filenames.
  - Expected: layout remains readable.
- Dark mode toggle.
  - Expected: no contrast/readability issues.

## Deployment Edge Cases

- `.env` missing.
  - Expected: copied from `.env.example` or clear setup warning.
- Docker build.
  - Expected: `.env`, jobs, PDFs, logs, outputs excluded by `.dockerignore`.
- Docker container runs as non-root.
  - Expected: app can still write mounted data folders.
- Port 7860 already in use.
  - Expected: start fails clearly or use another port.
- Multiple web workers.
  - Expected: production warning; avoid until queue is redesigned.
- Public internet exposure.
  - Expected: not allowed without HTTPS/private tunnel.

## Production V1 Must Add

- Real queue with worker leases.
- Stronger PDF validation.
- Global Gemini rate limiter.
- Better upload scanning if files come from untrusted users.
- Postgres-backed users/jobs.
- Object storage or dedicated data volume.
- Audit logs.
- Full automated test suite for these edge cases.

## Fast Pre-Demo Smoke Test

Run this sequence before showing the demo:

1. Start server.
2. Open homepage with login.
3. Confirm `/status` returns HTTP 200.
4. Upload one small PDF.
5. Wait for completion or visible failure reason.
6. Download DOCX if complete.
7. Click `Run Again / Resume` on one safe job.
8. Run `production_check.py`.
9. Confirm only expected warning is page limit.
