@echo off
title Strava GPX Sync // MCP Server & Operations Dashboard
cd /d "%~dp0"

echo ================================================================================
echo   Strava GPX Sync // Starting Production Environment...
echo ================================================================================

REM Alte Python-Instanz auf Port 8000 bei Bedarf automatisch beenden
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if ($p.ProcessName -like '*python*') { Write-Host '[AUTO-CLEANUP] Schliesse alte Server-Instanz auf Port 8000 (PID ' $p.Id ')...' -ForegroundColor Yellow; Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 } }"

if exist .venv\Scripts\python.exe (
    set PYTHON_BIN=.venv\Scripts\python.exe
) else (
    set PYTHON_BIN=python
)

"%PYTHON_BIN%" run_server.py
pause
