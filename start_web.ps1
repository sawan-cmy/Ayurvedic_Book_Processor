$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not $env:HOST) { $env:HOST = "0.0.0.0" }
if (-not $env:PORT) { $env:PORT = "7860" }
$env:DISABLE_INLINE_WORKERS = "true"

# Avoid inherited local proxy placeholders breaking Gemini API calls.
Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:ALL_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
Remove-Item Env:https_proxy -ErrorAction SilentlyContinue
Remove-Item Env:all_proxy -ErrorAction SilentlyContinue

.\.venv\Scripts\waitress-serve.exe --host=$env:HOST --port=$env:PORT app:app
