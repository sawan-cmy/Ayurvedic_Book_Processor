$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

function Clear-ConfiguredProxyEnv {
    $mode = if ($env:CLEAR_PROXY_ENV) { $env:CLEAR_PROXY_ENV.ToLowerInvariant() } else { "local" }
    if (@("0", "false", "no", "off", "never") -contains $mode) { return }
    $clearAll = @("1", "true", "yes", "on", "all") -contains $mode
    foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($value -and ($clearAll -or $value.ToLowerInvariant().Contains("127.0.0.1") -or $value.ToLowerInvariant().Contains("localhost") -or $value.ToLowerInvariant().Contains("[::1]"))) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
    }
}

Clear-ConfiguredProxyEnv

.\.venv\Scripts\python.exe worker.py
