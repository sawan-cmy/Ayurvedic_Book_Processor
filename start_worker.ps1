$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

# Avoid inherited local proxy placeholders breaking Gemini API calls.
Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:ALL_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
Remove-Item Env:https_proxy -ErrorAction SilentlyContinue
Remove-Item Env:all_proxy -ErrorAction SilentlyContinue

.\.venv\Scripts\python.exe worker.py
