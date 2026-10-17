"""
dashboard.py: Minimalistisches, eckiges Schwarz-Weiß Status-Dashboard (Leaderboard-Stil).
Ausschließlich im lokalen Netzwerk (LAN / localhost) erreichbar.
Blockiert Zugriffe von externen Netzwerken (z. B. via Cloudflare Tunnel) aus Sicherheitsgründen.
"""

from __future__ import annotations

import html
import json
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from config import (
    AUTH_FILE,
    BASE_DIR,
    DOWNLOADS_DIR,
    MCP_AUTH_TOKEN,
    PROFILE_DIR,
)

SERVER_START_TIME = time.time()


def get_local_ip() -> str:
    """Ermittelt die primäre IP-Adresse im lokalen WLAN/LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def is_local_request(request: Request) -> bool:
    """
    Prüft, ob der Request aus dem lokalen Netzwerk (LAN/localhost) stammt.
    Wird ein Request über den Cloudflare-Tunnel geleitet (Erkennbar an CF-Headern
    oder öffentlicher Domain), wird der Zugriff verweigert.
    """
    # 1. Cloudflare-Tunnel-Header prüfen
    cf_headers = ("cf-connecting-ip", "cf-ray", "cf-visitor", "x-forwarded-for")
    for h in ("cf-connecting-ip", "cf-ray", "cf-visitor"):
        if h in request.headers:
            return False

    # 2. Host-Header prüfen (z. B. mcp-strava.ewigetomate.de -> extern)
    host = request.headers.get("host", "").split(":")[0].lower()
    if "ewigetomate.de" in host or "trycloudflare.com" in host:
        return False

    # 3. Client-IP prüfen
    client_ip = request.client.host if request.client else ""
    if client_ip in ("127.0.0.1", "::1", "localhost", "testclient"):
        return True

    # Private IPv4-Netze nach RFC 1918
    if (
        client_ip.startswith("192.168.")
        or client_ip.startswith("10.")
        or any(client_ip.startswith(f"172.{i}.") for i in range(16, 32))
    ):
        return True

    return False


def get_dashboard_data() -> Dict[str, Any]:
    """Sammelt alle System-, Athleten- und Telemetriedaten für das Dashboard."""
    from rag_engine import ActivityVectorStore
    from training_load import load_historical_training_data
    from background_sync import BackgroundSyncService

    uptime_s = int(time.time() - SERVER_START_TIME)
    uptime_h = uptime_s // 3600
    uptime_m = (uptime_s % 3600) // 60
    uptime_str = f"{uptime_h}h {uptime_m}m {uptime_s % 60}s"

    store = ActivityVectorStore()
    profile = store.get_profile()

    # Trainingsbelastungsmodell laden
    try:
        engine = load_historical_training_data()
        training_status = engine.get_training_status()
    except Exception:
        training_status = {
            "metrics": {"ctl_fitness": 0.0, "atl_fatigue": 0.0, "tsb_form": 0.0, "ramp_rate_7d": 0.0},
            "form_zone": {"key": "UNKNOWN", "title": "Keine Daten"},
        }

    # Lokale Aktivitäten (GPX, TCX, JSON)
    files = sorted(
        [f for f in DOWNLOADS_DIR.iterdir() if f.is_file() and f.suffix.lower() in (".gpx", ".tcx", ".json")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    activities_list = []
    for rank, f in enumerate(files[:12], 1):
        ext = f.suffix.lower().lstrip(".")
        m = re.match(r"^(\d+)(?:_(.*))?\.(gpx|tcx|json)$", f.name, re.IGNORECASE)
        act_id = m.group(1) if m else "N/A"
        name = m.group(2).replace("_", " ") if m and m.group(2) else "Unbekannt"

        act_time = None
        try:
            with open(f, "rb") as fp:
                head = fp.read(1024).decode("utf-8", errors="ignore")
            t_match = re.search(r"<(?:time|Time)>([^<]+)</(?:time|Time)>", head)
            if t_match:
                act_time = t_match.group(1).strip()
            elif ext == "json":
                m_json = re.search(r'"(?:start_time|date)":\s*"([^"]+)"', head)
                if m_json:
                    act_time = m_json.group(1).strip()
        except Exception:
            pass

        f_stat = f.stat()
        mtime_iso = datetime.fromtimestamp(f_stat.st_mtime, tz=timezone.utc).strftime("%d.%m.%Y %H:%M")
        size_kb = round(f_stat.st_size / 1024, 1)

        activities_list.append({
            "rank": rank,
            "filename": f.name,
            "format": ext.upper(),
            "activity_id": act_id,
            "name": name,
            "date": act_time[:10] if act_time else mtime_iso[:10],
            "time_iso": act_time or mtime_iso,
            "size_kb": size_kb,
        })

    # Background-Sync-Status
    sync_service = BackgroundSyncService.get_instance()
    sync_status = sync_service.get_status()

    local_ip = get_local_ip()
    public_domain = "https://mcp-strava.ewigetomate.de"

    claude_connector_url = f"{public_domain}/mcp?token={MCP_AUTH_TOKEN}"
    claude_sse_url = f"{public_domain}/sse?token={MCP_AUTH_TOKEN}"
    local_mcp_url = f"http://127.0.0.1:8000/mcp?token={MCP_AUTH_TOKEN}"

    return {
        "status": "ONLINE",
        "service": "Strava GPX Sync Remote MCP Server",
        "uptime": uptime_str,
        "tools_count": 18,
        "transports": ["Streamable HTTP (/mcp)", "Legacy SSE (/sse)"],
        "local_ip": local_ip,
        "local_port": 8000,
        "public_domain": public_domain,
        "token": MCP_AUTH_TOKEN,
        "claude_connector_url": claude_connector_url,
        "claude_sse_url": claude_sse_url,
        "local_mcp_url": local_mcp_url,
        "auth_file_exists": AUTH_FILE.exists(),
        "athlete_profile": {
            "rider_weight_kg": profile.get("rider_weight_kg", 60.0),
            "bike_weight_kg": profile.get("bike_weight_kg", 9.5),
            "system_weight_kg": profile.get("total_system_weight_kg", 72.0),
            "hr_max": profile.get("hr_max", 205),
            "hr_rest": profile.get("hr_rest", 59),
            "athlete_type": profile.get("athlete_type", "Climber / Lightweight"),
        },
        "training_metrics": training_status.get("metrics", {}),
        "form_zone": training_status.get("form_zone", {}),
        "total_gpx_cached": len(files),
        "recent_activities": activities_list,
        "background_sync": sync_status,
        "apple_health": {
            "latest_metrics": store.get_latest_apple_health_metrics(),
            "readiness_score": (store.get_latest_apple_health_metrics() or {}).get("readiness_score"),
            "readiness_verdict": (store.get_latest_apple_health_metrics() or {}).get("readiness_verdict"),
            "recorded_date": (store.get_latest_apple_health_metrics() or {}).get("recorded_date"),
        },
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def render_dashboard_html() -> str:
    """Generiert das minimalistische, eckige Schwarz-Weiß Dashboard im Leaderboard-Stil."""
    data = get_dashboard_data()

    rows_html = ""
    for act in data["recent_activities"]:
        rank_str = f"#{act['rank']:02d}"
        fmt = act.get("format", "GPX")
        if fmt == "TCX":
            badge_html = '<span class="badge" style="border:1px solid #888;">INDOOR (TCX)</span>'
        elif fmt == "JSON":
            badge_html = '<span class="badge" style="border:1px solid #888;">INDOOR (STREAMS)</span>'
        else:
            badge_html = '<span class="badge badge-invert">GPS</span>'

        rows_html += f"""
        <tr>
            <td class="col-rank">{rank_str}</td>
            <td class="col-name"><strong>{html.escape(act['name'])}</strong></td>
            <td class="col-id">{html.escape(act['activity_id'])}</td>
            <td class="col-date">{html.escape(act['date'])}</td>
            <td class="col-size">{act['size_kb']} KB</td>
            <td class="col-status">{badge_html}</td>
        </tr>
        """

    if not rows_html:
        rows_html = "<tr><td colspan='6' style='text-align:center; padding:2rem;'>Keine GPX-Dateien im Cache vorhanden.</td></tr>"

    p = data["athlete_profile"]
    tm = data["training_metrics"]
    fz = data["form_zone"]
    bg = data["background_sync"]

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>STRAVA MCP // OPERATIONS LEADERBOARD</title>
    <style>
        :root {{
            --bg: #000000;
            --surface: #0a0a0a;
            --surface-card: #111111;
            --border: #333333;
            --border-bold: #ffffff;
            --text-main: #ffffff;
            --text-muted: #888888;
            --font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            border-radius: 0 !important; /* Strikt eckiges Design */
        }}
        body {{
            background-color: var(--bg);
            color: var(--text-main);
            font-family: var(--font-mono);
            font-size: 13px;
            line-height: 1.5;
            padding: 24px;
        }}
        .container {{
            max-width: 1280px;
            margin: 0 auto;
        }}
        header {{
            border: 2px solid var(--border-bold);
            padding: 20px;
            margin-bottom: 24px;
            background: var(--surface);
        }}
        .header-title-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
            margin-bottom: 12px;
        }}
        h1 {{
            font-size: 18px;
            letter-spacing: 2px;
            text-transform: uppercase;
        }}
        .header-meta {{
            display: flex;
            gap: 16px;
            color: var(--text-muted);
            font-size: 12px;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border: 1px solid var(--border);
            font-size: 11px;
            letter-spacing: 1px;
            text-transform: uppercase;
            font-weight: bold;
        }}
        .badge-invert {{
            background: #ffffff;
            color: #000000;
            border-color: #ffffff;
        }}
        .badge-live {{
            border-color: #ffffff;
            color: #ffffff;
            background: #111111;
        }}
        .badge-warn {{
            border-color: #ffffff;
            background: #222222;
            color: #ffffff;
        }}
        .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 24px;
        }}
        .grid-3 {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 24px;
        }}
        @media (max-width: 900px) {{
            .grid-2, .grid-3 {{
                grid-template-columns: 1fr;
            }}
        }}
        .box {{
            border: 1px solid var(--border);
            background: var(--surface);
            padding: 16px;
        }}
        .box-header {{
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
            margin-bottom: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 12px;
            font-weight: bold;
            letter-spacing: 1.5px;
            text-transform: uppercase;
        }}
        .field-row {{
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px dashed #222;
        }}
        .field-label {{
            color: var(--text-muted);
        }}
        .field-val {{
            font-weight: bold;
            word-break: break-all;
        }}
        .copy-box {{
            background: #111;
            border: 1px solid var(--border);
            padding: 12px;
            margin-top: 10px;
            position: relative;
        }}
        .copy-url {{
            font-size: 11px;
            color: #ccc;
            word-break: break-all;
            user-select: all;
            margin-bottom: 8px;
            display: block;
        }}
        button.btn-copy {{
            background: #ffffff;
            color: #000000;
            border: none;
            padding: 4px 10px;
            cursor: pointer;
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: bold;
            letter-spacing: 1px;
            text-transform: uppercase;
            transition: all 0.1s ease;
        }}
        button.btn-copy:hover {{
            background: #cccccc;
        }}
        button.btn-copy:active {{
            background: #888888;
        }}
        /* Leaderboard Table */
        .table-wrap {{
            border: 1px solid var(--border);
            overflow-x: auto;
            background: var(--surface);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 12px;
        }}
        th {{
            background: #ffffff;
            color: #000000;
            padding: 10px 14px;
            font-weight: bold;
            text-transform: uppercase;
            letter-spacing: 1px;
            border-bottom: 2px solid #000000;
        }}
        td {{
            padding: 10px 14px;
            border-bottom: 1px solid #1f1f1f;
        }}
        tr:hover td {{
            background: #141414;
        }}
        .col-rank {{ font-weight: bold; color: #fff; width: 60px; }}
        .col-id {{ color: var(--text-muted); font-size: 11px; }}
        .col-date {{ color: #ccc; }}
        .col-size {{ color: var(--text-muted); }}
        .stat-huge {{
            font-size: 28px;
            font-weight: bold;
            line-height: 1.1;
            margin: 6px 0;
            letter-spacing: 1px;
        }}
        .token-masked {{
            letter-spacing: 2px;
        }}
        footer {{
            margin-top: 30px;
            padding-top: 15px;
            border-top: 1px solid var(--border);
            color: var(--text-muted);
            font-size: 11px;
            display: flex;
            justify-content: space-between;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- HEADER -->
        <header>
            <div class="header-title-row">
                <div>
                    <h1>[ STRAVA GPX SYNC // OPERATIONS LEADERBOARD ]</h1>
                </div>
                <div>
                    <span class="badge badge-invert">● ONLINE</span>
                    <span class="badge badge-live">LAN EXCLUSIVE</span>
                </div>
            </div>
            <div class="header-meta">
                <span>LOCAL IP: <strong>{data['local_ip']}:{data['local_port']}</strong></span>
                <span>•</span>
                <span>ACTIVE TOOLS: <strong>{data['tools_count']}</strong></span>
                <span>•</span>
                <span>UPTIME: <strong id="live-uptime">{data['uptime']}</strong></span>
                <span>•</span>
                <span>SESSION: <strong>{'PLAYWRIGHT AUTH OK' if data['auth_file_exists'] else 'NO AUTH FILE'}</strong></span>
            </div>
        </header>

        <!-- TOP GRID: CREDENTIALS & CONNECTOR SETUP -->
        <div class="grid-2">
            <!-- BOX 1: CREDENTIALS & NETWORK -->
            <div class="box">
                <div class="box-header">
                    <span>[ SYSTEM CREDENTIALS ]</span>
                    <span class="badge">SECURITY ACTIVE</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Cloudflare Domain</span>
                    <span class="field-val">{data['public_domain']}</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Local Bind</span>
                    <span class="field-val">127.0.0.1:{data['local_port']}</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Home LAN IP</span>
                    <span class="field-val">{data['local_ip']}:{data['local_port']}</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Bearer Token</span>
                    <span class="field-val">
                        <span id="token-val" class="token-masked">••••••••••••••••••••••••••••</span>
                        <button class="btn-copy" style="margin-left: 8px;" onclick="toggleToken('{data['token']}')" id="btn-toggle-token">SHOW</button>
                    </span>
                </div>
                <div class="field-row">
                    <span class="field-label">Allowed Dashboard Access</span>
                    <span class="field-val">127.0.0.1 / Local LAN (192.168.*)</span>
                </div>
            </div>

            <!-- BOX 2: CLAUDE CONNECTOR URLS (ONE-CLICK) -->
            <div class="box">
                <div class="box-header">
                    <span>[ CLAUDE CONNECTOR SETUP ]</span>
                    <span class="badge badge-invert">1-CLICK COPY</span>
                </div>
                <p style="color: var(--text-muted); font-size: 11px; margin-bottom: 8px;">
                    Füge diese URL in Claude.ai unter <em>Benutzerdefinierter Connector</em> ein. Wähle <em>Erweitert &gt; Streamable HTTP</em> und <em>Keine Anmeldung</em>:
                </p>
                <div class="copy-box">
                    <span class="copy-url" id="claude-url">{data['claude_connector_url']}</span>
                    <button class="btn-copy" onclick="copyToClipboard('claude-url', this)">COPY CLAUDE URL</button>
                </div>
                <div class="copy-box" style="margin-top: 8px;">
                    <span class="copy-url" id="local-url">{data['local_mcp_url']}</span>
                    <button class="btn-copy" onclick="copyToClipboard('local-url', this)">COPY LOCAL URL</button>
                </div>
            </div>
        </div>

        <!-- MIDDLE GRID: ATHLETE STATS & COGGAN LOAD METRICS -->
        <div class="grid-3">
            <!-- ATHLETE PHYSIOLOGY -->
            <div class="box">
                <div class="box-header">
                    <span>[ ATHLETE PROFILE ]</span>
                    <span class="badge">{p.get('athlete_type', 'RIDER')}</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Fahrergewicht</span>
                    <span class="field-val">{p.get('rider_weight_kg', 0.0)} kg</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Radgewicht</span>
                    <span class="field-val">{p.get('bike_weight_kg', 0.0)} kg</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Systemgewicht</span>
                    <span class="field-val">{p.get('system_weight_kg', 0.0)} kg</span>
                </div>
                <div class="field-row">
                    <span class="field-label">HRmax / Ruhepuls</span>
                    <span class="field-val">{p.get('hr_max', 205)} / {p.get('hr_rest', 59)} bpm</span>
                </div>
            </div>

            <!-- COGGAN LOAD ENGINE -->
            <div class="box">
                <div class="box-header">
                    <span>[ COGGAN LOAD MODEL ]</span>
                    <span class="badge badge-invert">{fz.get('title', 'NEUTRAL')[:14]}</span>
                </div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <div>
                        <div class="field-label">CTL (FITNESS)</div>
                        <div class="stat-huge">{tm.get('ctl_fitness', 0.0)}</div>
                    </div>
                    <div>
                        <div class="field-label">ATL (FATIGUE)</div>
                        <div class="stat-huge">{tm.get('atl_fatigue', 0.0)}</div>
                    </div>
                    <div>
                        <div class="field-label">TSB (FORM)</div>
                        <div class="stat-huge">{tm.get('tsb_form', 0.0)}</div>
                    </div>
                </div>
                <div class="field-row">
                    <span class="field-label">Ramp Rate (7 Tage)</span>
                    <span class="field-val">{tm.get('ramp_rate_7d', 0.0)} CTL/Woche</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Wöchentlicher TSS</span>
                    <span class="field-val">{tm.get('weekly_tss_7d', 0.0)} TSS</span>
                </div>
            </div>

            <!-- ENGINE & SYNC STATE -->
            <div class="box">
                <div class="box-header">
                    <span>[ BACKGROUND SERVICES ]</span>
                    <span class="badge">{'RUNNING' if bg.get('running') else 'STANDBY'}</span>
                </div>
                <div class="field-row">
                    <span class="field-label">GPX Cache</span>
                    <span class="field-val">{data['total_gpx_cached']} Dateien</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Sync-Intervall</span>
                    <span class="field-val">{bg.get('interval_minutes', 45)} Minuten</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Letzter Sync</span>
                    <span class="field-val">{bg.get('last_sync_time', 'Nie')}</span>
                </div>
                <div class="field-row">
                    <span class="field-label">Playwright Browser</span>
                    <span class="field-val">Headless Singleton (Ready)</span>
                </div>
            </div>
        </div>

        <!-- LEADERBOARD: RECENT ACTIVITIES -->
        <div class="box" style="margin-bottom: 24px;">
            <div class="box-header">
                <span>[ GPX TOURS LEADERBOARD // NEUESTE AKTIVITÄTEN ]</span>
                <span class="badge badge-invert">{data['total_gpx_cached']} TOURS CACHED</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Rang</th>
                            <th>Aktivitäts-Name</th>
                            <th>Strava ID</th>
                            <th>Tour-Datum</th>
                            <th>Dateigröße</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody id="leaderboard-body">
                        {rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- FOOTER -->
        <footer>
            <span>STRAVA GPX SYNC // PRODUCTION RUNTIME 1.0.0</span>
            <span>RESTRICTED ACCESS: LOCAL LAN ONLY</span>
            <span id="live-time">{data['timestamp']}</span>
        </footer>
    </div>

    <script>
        function copyToClipboard(elementId, btn) {{
            const text = document.getElementById(elementId).innerText.trim();
            navigator.clipboard.writeText(text).then(() => {{
                const orig = btn.innerText;
                btn.innerText = "COPIED!";
                setTimeout(() => {{ btn.innerText = orig; }}, 1500);
            }});
        }}

        let isTokenShown = false;
        function toggleToken(realToken) {{
            const el = document.getElementById("token-val");
            const btn = document.getElementById("btn-toggle-token");
            if (isTokenShown) {{
                el.innerText = "••••••••••••••••••••••••••••";
                el.classList.add("token-masked");
                btn.innerText = "SHOW";
                isTokenShown = false;
            }} else {{
                el.innerText = realToken;
                el.classList.remove("token-masked");
                btn.innerText = "HIDE";
                isTokenShown = true;
            }}
        }}

        // Auto-Refresh Dashboard Data every 5 seconds
        setInterval(async () => {{
            try {{
                const res = await fetch('/api/dashboard-stats');
                if (res.ok) {{
                    const d = await res.json();
                    document.getElementById('live-uptime').innerText = d.uptime;
                    document.getElementById('live-time').innerText = d.timestamp;
                }}
            }} catch (e) {{
                console.debug('Dashboard polling paused:', e);
            }}
        }}, 5000);
    </script>
</body>
</html>
"""


async def dashboard_endpoint(request: Request) -> HTMLResponse:
    """
    Rendert das Web-Dashboard.
    Sicherheitsprüfung: Nur Anfragen aus dem lokalen Netzwerk (LAN/localhost) erlaubt!
    """
    if not is_local_request(request):
        return HTMLResponse(
            """<!DOCTYPE html>
            <html lang="en">
            <head><title>403 Forbidden</title></head>
            <body style="background:#000; color:#fff; font-family:monospace; padding:3rem;">
                <h1>[ 403 FORBIDDEN ] ACCESS DENIED</h1>
                <p>Das Operations-Dashboard ist aus Sicherheitsgründen ausschließlich im lokalen Heimnetzwerk (LAN / localhost) erreichbar.</p>
                <p>Zugriffe über den öffentlichen Cloudflare-Tunnel sind gesperrt.</p>
            </body>
            </html>
            """,
            status_code=403,
        )

    return HTMLResponse(render_dashboard_html())


async def dashboard_stats_api(request: Request) -> JSONResponse:
    """Liefert die Dashboard-Daten im JSON-Format für Live-Polling (nur lokal)."""
    if not is_local_request(request):
        return JSONResponse({"error": "Forbidden: Local network only"}, status_code=403)

    return JSONResponse(get_dashboard_data())
