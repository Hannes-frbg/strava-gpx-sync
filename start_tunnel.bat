@echo off
chcp 65001 >nul
echo ======================================================================
echo   Strava GPX Sync - Cloudflare HTTPS Tunnel
echo ======================================================================
echo.
echo Sobald der Tunnel bereit ist, wird eine oeffentliche HTTPS-URL angezeigt:
echo   z. B. https://xxxx-xxxx-xxxx.trycloudflare.com
echo.
echo Nutze diesen Endpunkt in Claude.ai (Konnektoren):
echo   URL: https://xxxx-xxxx-xxxx.trycloudflare.com/sse
echo.
echo ======================================================================
cloudflared tunnel --url http://localhost:8000
pause
