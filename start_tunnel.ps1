# start_tunnel.ps1: Startet den Cloudflare Tunnel fuer den Strava MCP Server
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  Strava GPX Sync - Cloudflare HTTPS Tunnel" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Sobald der Tunnel bereit ist, wird eine oeffentliche HTTPS-URL angezeigt:" -ForegroundColor Yellow
Write-Host "  z. B. https://xxxx-xxxx-xxxx.trycloudflare.com" -ForegroundColor Green
Write-Host ""
Write-Host "Nutze diesen Endpunkt in Claude.ai (Konnektoren):" -ForegroundColor Yellow
Write-Host "  URL: https://xxxx-xxxx-xxxx.trycloudflare.com/sse" -ForegroundColor Green
Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan

cloudflared tunnel --url http://localhost:8000
