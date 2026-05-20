@echo off
echo =======================================================
echo     Ayurvedic Book Processor - Production Startup
echo =======================================================
echo.

cd /d "%~dp0"

:: Set default environment variables
if "%HOST%"=="" set HOST=0.0.0.0
if "%PORT%"=="" set PORT=7860
if "%MAX_UPLOAD_MB%"=="" set MAX_UPLOAD_MB=250
set DISABLE_INLINE_WORKERS=false

:: Avoid proxy issues with Gemini API
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set http_proxy=
set https_proxy=
set all_proxy=

echo Starting Web Server on http://localhost:%PORT%...
echo Press Ctrl+C to stop.
echo.

.\.venv\Scripts\python.exe -m waitress --host=%HOST% --port=%PORT% --threads=50 app:app
