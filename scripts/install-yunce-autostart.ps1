param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$valueName = 'YunCeFlowOps'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

if ($Remove) {
    Remove-ItemProperty -Path $runKey -Name $valueName -ErrorAction SilentlyContinue
    Write-Host 'YunCe login autostart removed.'
    exit 0
}

$root = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot 'start-yunce-background.ps1'
if (-not (Test-Path -LiteralPath $startScript)) {
    throw "Startup script not found: $startScript"
}

$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$command = "`"$powershell`" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`""
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $valueName -Value $command -PropertyType String -Force | Out-Null

& $startScript
Write-Host 'YunCe login autostart installed and services are ready.'
