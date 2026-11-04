$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root 'logs\firewall.log'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ERROR not elevated" |
        Add-Content -LiteralPath $logPath -Encoding utf8
    throw 'This script must be run from an elevated PowerShell window.'
}

$ruleName = 'YunCe LAN 5173'
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Set-NetFirewallRule -DisplayName $ruleName -Enabled True -Direction Inbound -Action Allow
} else {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 5173 `
        -RemoteAddress LocalSubnet `
        -Profile Any | Out-Null
}
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') OK rule enabled" |
    Add-Content -LiteralPath $logPath -Encoding utf8
Write-Host 'YunCe LAN firewall rule is enabled for TCP 5173 from the local subnet.'
