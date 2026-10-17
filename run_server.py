"""
run_server.py: Production-Ready Master Launcher für den Strava GPX Sync MCP Server.

Startet alle Dienste in einem einzigen Befehl:
1. Prüft Umgebung und Abhängigkeiten
2. Bindet auf 0.0.0.0:8000 (erreichbar lokal, im Heimnetzwerk und via Cloudflare Tunnel)
3. Initialisiert den langlebigen Playwright-Browser und den Background-Sync
4. Startet das minimalistische Schwarz-Weiß Operations-Dashboard
5. Gibt ein übersichtliches Terminal-Banner mit allen URLs und Tokens aus
"""

from __future__ import annotations

import os
import signal
import sys
import time

# UTF-8 auf Windows Terminal erzwingen
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import uvicorn
from config import AUTH_FILE, MCP_AUTH_TOKEN
from dashboard import get_local_ip
from mcp_server import (
    BrowserManager,
    create_starlette_app,
    setup_uvicorn_logging_filter,
)
from background_sync import BackgroundSyncService


def print_banner(local_ip: str, port: int, token: str) -> None:
    domain = "https://mcp-strava.ewigetomate.de"
    claude_url = f"{domain}/mcp?token={token}"
    local_dash = f"http://localhost:{port}/dashboard"
    lan_dash = f"http://{local_ip}:{port}/dashboard"
    local_app = f"http://localhost:{port}/app"
    lan_app = f"http://{local_ip}:{port}/app"

    banner = f"""
================================================================================
  [ STRAVA GPX SYNC // PRODUCTION MCP SERVER & OPERATIONS DASHBOARD ]
================================================================================
  ● STATUS:                ONLINE & READY
  ● BINDING:               0.0.0.0:{port} (Localhost + Local Network + Cloudflare)
  ● PUBLIC DOMAIN:         {domain}
  ● ACTIVE TOOLS:          26 Tools (Council Review, VO2max, Decoupling, Coggan Load, RAG)

  ────────────────────────────────────────────────────────────────────────────
  [ DASHBOARDS & WEB APPS // HEIMNETZWERK & LOCALHOST ]
  ● OPERATIONS DASHBOARD:  {local_dash} (LAN: {lan_dash})
  ● APPLE HEALTH WEB APP:  {local_app} (LAN: {lan_app})

  ────────────────────────────────────────────────────────────────────────────
  [ CLAUDE.AI CONNECTOR SETUP // 1-CLICK URL ]
  ● CONNECTOR URL:         {claude_url}
  ● AUTH TOKEN:            {token}

  ● ANLEITUNG:             In Claude.ai > Profil > Einstellungen > Connectors >
                           'Benutzerdefinierter Connector hinzufügen' >
                           Obige URL einfügen > Erweitert > Streamable HTTP >
                           'Keine Anmeldung' wählen > Fertig!
================================================================================
  [Drücke STRG + C zum sicheren Beenden des Servers]
================================================================================
"""
    print(banner, flush=True)


def main():
    setup_uvicorn_logging_filter()
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    token = os.getenv("MCP_AUTH_TOKEN", MCP_AUTH_TOKEN)
    local_ip = get_local_ip()

    print_banner(local_ip, port, token)

    # 1. Background-Sync Service starten
    bg_service = None
    try:
        bg_service = BackgroundSyncService.get_instance(interval_minutes=45)
        bg_service.start()
    except Exception as e:
        print(f"[WARN] Background-Sync konnte nicht gestartet werden: {e}", flush=True)

    # 2. Apple Health Auto-Scanner starten (erkennt iPhone Health Export App im Hintergrund)
    health_scanner = None
    try:
        from config import HEALTH_EXPORT_SCAN_INTERVAL
        from apple_health_service import AppleHealthAutoScanner
        health_scanner = AppleHealthAutoScanner.get_instance(check_interval_seconds=HEALTH_EXPORT_SCAN_INTERVAL)
        health_scanner.start()
    except Exception as e:
        print(f"[WARN] Apple Health Scanner Standby: {e}", flush=True)

    # 3. Playwright Singleton vorwärmen (beschleunigt erste Antwort)
    if AUTH_FILE.exists():
        try:
            print("[INFO] Initialisiere Playwright Browser-Session...", flush=True)
            BrowserManager.get_instance().ensure_initialized()
            print("[INFO] Playwright Browser erfolgreich vorgewärmt.", flush=True)
        except Exception as e:
            print(f"[WARN] Browser-Vorwärmung übersprungen: {e}", flush=True)

    # 4. Starlette Application erstellen
    app = create_starlette_app(token=token)

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",  # Saubere Logs ohne Request-Spam
        access_log=False,
    )
    server = uvicorn.Server(config)

    def handle_exit(sig, frame):
        print("\n[INFO] Fahre Server sauber herunter...", flush=True)
        server.should_exit = True

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        server.run()
    finally:
        print("[INFO] Beende Hintergrunddienste...", flush=True)
        if health_scanner:
            health_scanner.stop()
        if bg_service:
            bg_service.stop()
        BrowserManager.get_instance().close()
        print("[INFO] Server erfolgreich gestoppt. Auf Wiedersehen!", flush=True)


if __name__ == "__main__":
    main()
