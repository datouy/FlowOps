$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root 'social-auto-upload-main\.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Environment file not found: $envPath"
}

$token = ''
foreach ($line in Get-Content -LiteralPath $envPath -Encoding utf8) {
    if ($line -match '^\s*YUNCE_ACCESS_TOKEN\s*=\s*(.*)$') {
        $token = $Matches[1].Trim().Trim('"').Trim("'")
        break
    }
}
if (-not $token) {
    throw 'YUNCE_ACCESS_TOKEN is empty; remote access is not configured.'
}

$ip = Get-NetIPConfiguration |
    Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
    ForEach-Object { $_.IPv4Address.IPAddress } |
    Select-Object -First 1
if (-not $ip) {
    throw 'No active LAN IPv4 address was found.'
}

$url = "http://$ip`:5173/?access_token=$([uri]::EscapeDataString($token))"
Set-Clipboard -Value $url
Write-Host "Invite URL copied to clipboard: http://$ip`:5173/"
