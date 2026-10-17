# PowerShell Starter Script for Strava GPX Sync MCP Server & Dashboard
$ErrorActionPreference = "Continue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonBin = "python"
if (Test-Path "$scriptDir\.venv\Scripts\python.exe") {
    $pythonBin = "$scriptDir\.venv\Scripts\python.exe"
}

# 1. Prüfen, ob Port 8000 noch von einer alten Python-Instanz belegt wird
$activeConn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($activeConn) {
    $procId = $activeConn.OwningProcess
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like "*python*") {
        Write-Host "[AUTO-CLEANUP] Alte Server-Instanz auf Port 8000 gefunden (PID $procId)." -ForegroundColor Yellow
        Write-Host "[AUTO-CLEANUP] Beende alte Instanz für sauberen Neustart..." -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "  Starting Strava GPX Sync MCP Server & Local Leaderboard Dashboard..." -ForegroundColor Green
Write-Host "  Using Python: $pythonBin" -ForegroundColor Gray
Write-Host "================================================================================" -ForegroundColor Cyan

& $pythonBin "$scriptDir\run_server.py"
