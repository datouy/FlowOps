$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root 'social-auto-upload-main'
$frontendDir = Join-Path $backendDir 'sau_frontend'
$python = Join-Path $root 'venv\Scripts\python.exe'
$logsDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Test-PortListening([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Http([string]$url, [int]$attempts = 40) {
    for ($i = 0; $i -lt $attempts; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $false
}

$started = @()

if (-not (Test-PortListening 5409)) {
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Backend Python not found: $python"
    }
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process `
        -FilePath $python `
        -ArgumentList 'sau_backend.py' `
        -WorkingDirectory $backendDir `
        -RedirectStandardOutput (Join-Path $logsDir 'autostart-backend.out.log') `
        -RedirectStandardError (Join-Path $logsDir 'autostart-backend.err.log') `
        -WindowStyle Hidden | Out-Null
    $started += 'backend'
}

if (-not (Wait-Http 'http://127.0.0.1:5409/health')) {
    throw 'Backend did not become ready on http://127.0.0.1:5409'
}

if (-not (Test-PortListening 5173)) {
    $npm = Get-Command npm.cmd -ErrorAction Stop
    Start-Process `
        -FilePath $npm.Source `
        -ArgumentList @('run', 'dev', '--', '--host', '0.0.0.0') `
        -WorkingDirectory $frontendDir `
        -RedirectStandardOutput (Join-Path $logsDir 'autostart-frontend.out.log') `
        -RedirectStandardError (Join-Path $logsDir 'autostart-frontend.err.log') `
        -WindowStyle Hidden | Out-Null
    $started += 'frontend'
}

if (-not (Wait-Http 'http://127.0.0.1:5173/')) {
    throw 'Frontend did not become ready on http://127.0.0.1:5173'
}

$summary = if ($started.Count) {
    "Started: $($started -join ', ')"
} else {
    'Services were already running'
}
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $summary" |
    Add-Content -Path (Join-Path $logsDir 'autostart.log') -Encoding utf8
