#requires -Version 5.1
# tunebox launcher. Binds 0.0.0.0 so phones on the same WiFi can reach it too.

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$port = 8730
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL' -and $_.IPAddress -ne '127.0.0.1' } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "  tunebox" -ForegroundColor Yellow
Write-Host "  this pc     http://localhost:$port"
if ($ip) { Write-Host "  same wifi   http://${ip}:$port" }
Write-Host ""

uv run python -m app $port
