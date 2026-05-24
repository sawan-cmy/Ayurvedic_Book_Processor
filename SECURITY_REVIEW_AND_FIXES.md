# Security Review And Fixes

Date: 2026-05-24

This document summarizes the security issues found in the Ayurvedic Book Processor project and what was changed to reduce risk before the demo and later production hardening.

## Summary

The application is now safer for a controlled demo and small trusted-team use. The most important fixes were:

- Authentication now fails closed by default.
- Destructive POST actions now require CSRF tokens.
- Normal users can only see and control their own jobs.
- Existing job `.env` files were scrubbed of copied secrets.
- New job `.env` files do not store `GEMINI_API_KEY`, `APP_USERNAME`, or `APP_PASSWORD`.
- Docker builds now exclude local secrets and job data.
- Server-Sent Events are capped to reduce thread exhaustion.

This is still not a full enterprise security design. Before broad public use, the app should be placed behind HTTPS/private networking, and the queue/database architecture should be upgraded.

## Findings And Fixes

### 1. Authentication Could Fail Open

**Risk:** High

If no users existed in the database, the app previously allowed access without login. That was convenient for setup, but unsafe for production.

**Fix:**

- `authorized()` now fails closed by default when no users exist.
- A deliberate `ALLOW_AUTH_BYPASS=true` environment flag is required for no-login development mode.
- Production warnings report when auth bypass is enabled.

**Files changed:**

- `app.py`

## 2. Missing CSRF Protection

**Risk:** High

The app used Basic Auth. Browsers automatically send Basic Auth credentials to the same site, so a malicious webpage could trigger POST actions from a logged-in user's browser.

Affected actions included:

- Upload
- Settings update
- Retry job
- Delete job
- Admin user add/delete
- Manual page correction
- Slide deck regeneration

**Fix:**

- Added HMAC-based CSRF tokens.
- Added server-side CSRF validation for all non-GET requests.
- Added hidden CSRF inputs to all generated forms.

**Files changed:**

- `app.py`

## 3. Users Could Access Other Users' Jobs

**Risk:** High

Any logged-in user could see status, logs, downloads, review pages, delete, retry, or regenerate outputs for any job if they knew or guessed the job id or filename.

**Fix:**

- Added ownership checks for jobs.
- Admin users can still see and manage all jobs.
- Non-admin users see only their own uploaded jobs.
- Download routes now check job ownership.
- Completed document downloads now check the job id prefix in the completed filename.

**Files changed:**

- `app.py`

## 4. Secrets Were Copied Into Job Folders

**Risk:** High

Older job `.env` files contained copied `GEMINI_API_KEY`, `APP_USERNAME`, and `APP_PASSWORD` values.

**Fix:**

- New job `.env` files contain only non-secret processor settings.
- `GEMINI_API_KEY` is passed only to the processor subprocess at runtime.
- Web-only credentials are stripped from the processor subprocess environment.
- Existing job `.env` files were scrubbed of `GEMINI_API_KEY`, `APP_USERNAME`, and `APP_PASSWORD`.

**Files changed:**

- `app.py`
- Existing `jobs/**/.env` files were cleaned locally.

## 5. Docker Build Could Include Secrets And Data

**Risk:** High

The Dockerfile used `COPY . .` and there was no `.dockerignore`, so `.env`, jobs, PDFs, completed docs, logs, and cache files could be included in the Docker build context.

**Fix:**

- Added `.dockerignore`.
- Excluded `.env`, job data, PDFs, outputs, logs, caches, and local virtualenvs.
- Updated Docker image to run as a non-root user.
- Reduced `docker-compose.yml` parallelism from a risky value to a safer default.

**Files changed:**

- `.dockerignore`
- `Dockerfile`
- `docker-compose.yml`

## 6. Basic Auth Without HTTPS

**Risk:** Medium

The app binds to `0.0.0.0` for LAN access. Basic Auth over plain HTTP is not safe on an untrusted network.

**Current mitigation:**

- This remains acceptable only for a trusted demo network or private machine.

**Required production fix:**

- Put the app behind HTTPS, Tailscale, Cloudflare Tunnel, VPN, or a reverse proxy with TLS.

**Files changed:**

- Documentation only.

## 7. No Login Rate Limiting

**Risk:** Medium

Before the fix, there was no throttle for repeated login attempts.

**Fix:**

- Added simple in-memory failed-login throttling.
- Repeated failed attempts from the same IP and username are blocked temporarily.

**Files changed:**

- `app.py`

## 8. Server-Sent Events Could Exhaust Threads

**Risk:** Medium

Each `/events` connection stays open. Too many browser tabs or clients could occupy all Waitress threads.

**Fix:**

- Added an in-memory cap on concurrent SSE connections.
- Extra clients receive HTTP 429.

**Files changed:**

- `app.py`

## 9. Manual Page Save Path Needed Hardening

**Risk:** Medium

Manual correction accepted `pdf_stem` from a form and used it in a file path.

**Fix:**

- Sanitized `pdf_stem` with `Path(...).name`.
- Hardened `page_text_file()` so resolved paths must stay inside the job directory.

**Files changed:**

- `app.py`

## 10. PDF Upload Validation Is Still Basic

**Risk:** Medium

The app checks `.pdf` extension and `%PDF-` header. That blocks obvious non-PDF uploads, but malformed PDFs can still stress Poppler/Pillow or processing workers.

**Current mitigation:**

- Upload size limit.
- Disk-space reserve check.
- Queue limit per user.

**Recommended later fix:**

- Add stronger PDF validation.
- Run PDF processing in isolated workers.
- Add per-stage timeouts and resource limits.
- Add virus/malware scanning if files come from outside trusted staff.

## Current Demo Security Position

Safe enough for:

- Trusted demo machine.
- Trusted LAN.
- Small internal team.
- Controlled uploads.

Not yet safe enough for:

- Public internet exposure.
- Untrusted users.
- Large multi-user production.
- Enterprise compliance requirements.

## Remaining Production Security Work

- Put behind HTTPS or private tunnel.
- Move from Basic Auth to session login or SSO.
- Move secrets to a secret manager.
- Move jobs/users from SQLite to Postgres.
- Move files to controlled object storage.
- Add audit logs.
- Add full rate limiting.
- Add malware/PDF scanning.
- Add real worker sandboxing.
- Add backup and restore procedures.

## Demo Checklist

- Keep the server on a trusted network.
- Use one admin/demo account.
- Keep `TEST_MAX_PAGES=7` for fast demo.
- Do not expose the app directly to the public internet.
- Keep a fresh Gemini API key in `.env`.
- Use `production_check.py` before demo.
