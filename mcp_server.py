"""
mcp_server.py: Lokaler Model Context Protocol (MCP) Server für Strava GPX Sync.

Stellt KI-Assistenten (Claude Desktop, Cursor etc.) Werkzeuge bereit, um:
1. Die neuesten Aktivitäten live von Strava abzurufen (list_strava_activities).
2. GPX-Dateien herunterzuladen und sportwissenschaftlich zu analysieren (download_and_analyze_activity).
3. Bereits lokal vorliegende GPX-Dateien aufzulisten und auszuwerten.

Kommunikation erfolgt über stdio mittels FastMCP.
WICHTIG: Alle Server-Logs werden auf sys.stderr ausgegeben, um das JSON-RPC-Protokoll auf stdout nicht zu stören.
"""

from __future__ import annotations

import asyncio
import atexit
from collections import defaultdict
import concurrent.futures
import functools
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Set
from urllib.parse import parse_qs

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from config import (
    AUTH_FILE,
    BASE_DIR,
    DOWNLOADS_DIR,
    MCP_AUTH_TOKEN,

    TRAINING_URL,
    create_browser_context,
    get_user_downloads_dir,
    launch_browser,
)
from sync_gpx import download_activity_gpx, get_activity_segments as fetch_activity_segments
from analyze_gpx import (
    calculate_metrics,
    format_seconds_to_hms,
    haversine_distance_meters,
    parse_gpx_file,
)
from rag_engine import ActivityVectorStore, sync_local_gpx_to_vector_store
from training_load import load_historical_training_data
from background_sync import BackgroundSyncService
from dashboard import dashboard_endpoint, dashboard_stats_api, is_local_request, render_dashboard_html
from council_review import run_council_review
from sports_science import estimate_vo2max_from_power, calculate_aerobic_decoupling

# Logging strikt auf sys.stderr konfigurieren (stdout ist für MCP JSON-RPC reserviert!)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MCP] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("strava_mcp")


class UvicornAccessFilter(logging.Filter):
    """
    Unterdrückt irrelevante HTTP-Statuscodes (404 Not Found und 405 Method Not Allowed)
    im 'uvicorn.access' Logger, wie z. B. automatisierte Probes auf /.well-known/
    oder GET-Aufrufe auf /sse.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if " 405 " in msg or " 404 " in msg:
                return False
        except Exception:
            pass

        if record.args:
            for arg in record.args:
                if arg in (404, 405, "404", "405"):
                    return False

        return True


def setup_uvicorn_logging_filter() -> None:
    """
    Registriert den UvicornAccessFilter auf dem 'uvicorn.access' Logger, dessen Handlern
    und in der globalen Uvicorn LOGGING_CONFIG.
    """
    access_filter = UvicornAccessFilter()

    # 1. In uvicorn.config.LOGGING_CONFIG hinterlegen
    try:
        import uvicorn.config
        if hasattr(uvicorn.config, "LOGGING_CONFIG") and isinstance(uvicorn.config.LOGGING_CONFIG, dict):
            cfg = uvicorn.config.LOGGING_CONFIG
            cfg.setdefault("filters", {})["mcp_access_filter"] = {
                "()": UvicornAccessFilter
            }
            if "access" in cfg.get("handlers", {}):
                handlers_filters = cfg["handlers"]["access"].setdefault("filters", [])
                if "mcp_access_filter" not in handlers_filters:
                    handlers_filters.append("mcp_access_filter")
                cfg["handlers"]["access"]["stream"] = "ext://sys.stderr"
            if "default" in cfg.get("handlers", {}):
                cfg["handlers"]["default"]["stream"] = "ext://sys.stderr"
    except Exception as e:
        logger.debug("Konnte uvicorn.config.LOGGING_CONFIG nicht anpassen: %s", e)

    # 2. Direkt auf dem Logger und seinen Handlern registrieren
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(access_filter)
    for handler in uvicorn_access_logger.handlers:
        handler.addFilter(access_filter)


# Filter frühzeitig initialisieren
setup_uvicorn_logging_filter()


class TokenAuthMiddleware:
    """
    Sichere Authentifizierungs- und Session-Tracking-Middleware für Remote MCP Server
    (speziell optimiert für Cloudflare Tunnels und Claude.ai Web Custom Connectors).

    Funktionen:
    1. Header-Authentifizierung: 'Authorization: Bearer <token>'
    2. Header-Fallback: 'x-api-key: <token>'
    3. Query-Parameter-Fallback: '?token=<token>' oder '?auth_token=<token>'
    4. Automatische Session-Autorisierung für Folgerequests (POST /messages/?session_id=...)
       nach erfolgreichem Handshake auf /sse.
    5. CORS Preflight Requests (OPTIONS) mit 200 OK für nahtlosen Browserzugriff.
    6. Öffentliche Health- & Statusrouten ('/', '/health').
    """

    def __init__(self, app: ASGIApp, token: str, public_paths: Optional[Set[str]] = None):
        self.app = app
        self.token = token.strip()
        self.public_paths = public_paths or {
            "/",
            "/health",
            "/favicon.ico",
            "/dashboard",
            "/dashboard/",
            "/api/dashboard-stats",
            "/api/dashboard-stats/",
            "/api/live-coach",
            "/api/live-coach/",
            "/api/blackbox-upload",
            "/api/blackbox-upload/",
            "/app",
            "/app/",
            "/ui",
            "/ui/",
        }
        self.authenticated_sessions: Set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path", "")
        path = raw_path
        norm_path = raw_path.rstrip("/") if (raw_path and raw_path != "/") else raw_path

        # 1. Öffentliche Routen (Health Check, Root, Dashboard, Live-Coach, Apple Health UI) ohne Auth durchlassen
        is_public = (
            raw_path in self.public_paths
            or norm_path in self.public_paths
            or norm_path == "/dashboard"
            or raw_path.startswith("/dashboard/")
            or norm_path == "/api/dashboard-stats"
            or raw_path.startswith("/api/dashboard-stats/")
            or norm_path == "/app"
            or raw_path.startswith("/app/")
            or norm_path == "/ui"
            or raw_path.startswith("/ui/")
        )
        is_live_api = norm_path in ("/api/live-coach", "/api/blackbox-upload")
        if (is_public and scope.get("method") in ("GET", "HEAD")) or is_live_api:
            await self.app(scope, receive, send)
            return


        # 2. CORS Preflight Requests (OPTIONS) sofort beantworten
        if scope.get("method") == "OPTIONS":
            cors_headers = [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST, OPTIONS, HEAD"),
                (b"access-control-allow-headers", b"Authorization, Content-Type, Mcp-Session-Id, x-api-key"),
                (b"access-control-allow-credentials", b"true"),
                (b"content-length", b"0"),
            ]
            await send({"type": "http.response.start", "status": 200, "headers": cors_headers})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        # 3. Header und Query-Parameter auslesen
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8", "ignore").strip()
        api_key_header = headers.get(b"x-api-key", b"").decode("utf-8", "ignore").strip()

        query_string = scope.get("query_string", b"").decode("utf-8", "ignore")
        query_params = parse_qs(query_string)
        url_token = (
            query_params.get("token", [None])[0]
            or query_params.get("auth_token", [None])[0]
        )
        session_id = query_params.get("session_id", [None])[0]

        is_authenticated = False
        authenticated_via_url = False

        if auth_header.startswith("Bearer ") and auth_header[7:].strip() == self.token:
            is_authenticated = True
        elif api_key_header == self.token:
            is_authenticated = True
        elif url_token == self.token:
            is_authenticated = True
            authenticated_via_url = True
        elif session_id and session_id in self.authenticated_sessions:
            is_authenticated = True

        if not is_authenticated:
            resp = JSONResponse(
                {
                    "error": "Unauthorized",
                    "detail": "Ungültiger oder fehlender MCP-Authentifizierungs-Token.",
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="strava-mcp"',
                    "Access-Control-Allow-Origin": "*",
                },
            )
            await resp(scope, receive, send)
            return

        if session_id:
            self.authenticated_sessions.add(session_id)

        # Bei GET /sse: Interzeptieren des ersten Body-Chunks (Endpoint-Event),
        # um die von FastMCP generierte session_id automatisch als autorisiert zu registrieren
        # und ggf. ?token= an den data-Pfad anzuhängen
        if path == "/sse":
            async def intercepting_send(message: dict[str, Any]):
                if message["type"] == "http.response.body":
                    body_bytes = message.get("body", b"")
                    if b"event: endpoint" in body_bytes:
                        try:
                            body_text = body_bytes.decode("utf-8", "ignore")
                            match = re.search(r"session_id=([0-9a-fA-F\-]+)", body_text)
                            if match:
                                new_sid = match.group(1)
                                self.authenticated_sessions.add(new_sid)
                                if authenticated_via_url:
                                    body_text = re.sub(
                                        r"(session_id=[0-9a-fA-F\-]+)",
                                        rf"\1&token={self.token}",
                                        body_text,
                                    )
                                    message = dict(message)
                                    message["body"] = body_text.encode("utf-8")
                        except Exception:
                            pass
                await send(message)

            await self.app(scope, receive, intercepting_send)
        else:
            await self.app(scope, receive, send)


class BrowserManager:
    """
    Globaler Singleton-Manager für den Playwright-Browser und den authentifizierten Browser-Kontext.
    Hält eine langlebige Browser- und Kontext-Instanz auf einem dedizierten Worker-Thread,
    um Kaltstarts bei jedem Tool-Aufruf zu vermeiden.
    """

    _instance: Optional["BrowserManager"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="playwright_worker",
        )
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._is_initialized = False

    @classmethod
    def get_instance(cls) -> "BrowserManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = BrowserManager()
                atexit.register(cls._instance.close)
            return cls._instance

    def _is_alive(self) -> bool:
        try:
            return bool(
                self._is_initialized
                and self._browser
                and self._browser.is_connected()
                and self._context
            )
        except Exception:
            return False

    def _init_browser_internal(self) -> BrowserContext:
        """Läuft ausschließlich auf dem dedizierten Worker-Thread."""
        if self._is_alive() and self._context:
            return self._context

        self._shutdown_internal()

        if not AUTH_FILE.exists():
            raise FileNotFoundError(
                f"Keine Session-Datei unter '{AUTH_FILE.name}' gefunden. "
                "Bitte führen Sie einmalig 'python setup_auth.py' im Projektverzeichnis aus."
            )

        logger.info("Initialisiere langlebigen Playwright-Browser (Headless-Singleton)...")
        self._playwright = sync_playwright().start()
        self._browser = launch_browser(self._playwright, headless=True, logger=logger)
        self._context = create_browser_context(
            self._browser,
            storage_state_path=AUTH_FILE,
            logger=logger,
        )
        self._is_initialized = True
        logger.info("Browser-Singleton erfolgreich gestartet und einsatzbereit.")
        return self._context

    def _shutdown_internal(self) -> None:
        """Läuft ausschließlich auf dem dedizierten Worker-Thread."""
        if self._context:
            try:
                if AUTH_FILE.exists():
                    self._context.storage_state(path=str(AUTH_FILE))
            except Exception:
                pass
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._is_initialized = False

    def ensure_initialized(self) -> None:
        """Wärmt die Browser-Instanz im Voraus auf (z. B. beim Server-Startup)."""
        future = self._executor.submit(self._init_browser_internal)
        future.result()

    def _execute_internal(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        ctx = self._init_browser_internal()
        return func(ctx, *args, **kwargs)

    def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Führt eine Funktion synchron auf dem Browser-Worker-Thread aus."""
        future = self._executor.submit(self._execute_internal, func, *args, **kwargs)
        return future.result()

    async def run_async(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Führt eine Funktion asynchron (non-blocking) auf dem Browser-Worker-Thread aus."""
        loop = asyncio.get_running_loop()
        call_fn = functools.partial(self._execute_internal, func, *args, **kwargs)
        return await loop.run_in_executor(self._executor, call_fn)

    def close(self) -> None:
        """Beendet den Browser und Playwright sauber."""
        with self._lock:
            if not self._is_initialized and not self._browser:
                return
            logger.info("Beende globalen Playwright-Browser...")
            try:
                future = self._executor.submit(self._shutdown_internal)
                future.result(timeout=5)
            except Exception as e:
                logger.debug("Fehler beim Beenden des Browsers: %s", e)
            finally:
                self._is_initialized = False


# FastMCP Server initialisieren (Standard: 0.0.0.0:8000 für Netzwerkzugriff via SSE)
mcp = FastMCP(
    "Strava GPX Sync",
    host="0.0.0.0",
    port=8000,
    dependencies=["playwright", "mcp"],
)

# DNS-Rebinding-Schutz für Remote-Tunnels (Cloudflare Tunnel, ngrok, Custom Domains) deaktivieren
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@mcp.custom_route("/", methods=["GET"])
async def root_status(request: Request) -> Response:
    """Öffentlicher Status- und Willkommens-Endpunkt für Browser und Monitoring."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept and is_local_request(request):
        return HTMLResponse(render_dashboard_html())

    return JSONResponse(
        {
            "status": "online",
            "service": "Strava GPX Sync Remote MCP Server",
            "version": "1.0.0",
            "endpoints": {
                "dashboard": "/dashboard",
                "streamable_http": "/mcp",
                "sse": "/sse",
                "messages": "/messages/",
                "health": "/health",
            },
            "security": {
                "auth_type": "Bearer Token / x-api-key / ?token=",
                "cloudflare_tunnel_compatible": True,
                "claude_custom_connector_ready": True,
            },
            "tools_count": 18,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@mcp.custom_route("/dashboard", methods=["GET"])
async def route_dashboard(request: Request) -> HTMLResponse:
    """Operations-Dashboard für lokales Netzwerk."""
    return await dashboard_endpoint(request)


@mcp.custom_route("/api/dashboard-stats", methods=["GET"])
async def route_dashboard_stats(request: Request) -> JSONResponse:
    """Live-Telemetrie und Status-Daten für das Dashboard (nur LAN/lokal)."""
    return await dashboard_stats_api(request)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Einfacher Health-Check Endpunkt."""
    return JSONResponse(
        {
            "status": "healthy",
            "mcp_server": "Strava GPX Sync",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@mcp.custom_route("/api/live-coach", methods=["GET", "POST"])
async def route_live_coach(request: Request) -> JSONResponse:
    """
    Live-Coach API für Radcomputer, Garmin Connect IQ oder ESP32 Blackbox.
    Akzeptiert Telemetrie-Chunks während der Fahrt und liefert in Sekundenbruchteilen
    einen 12-18 Wörter Live-Whisper und physiologische Statuswerte (Bonk / Hydration) zurück.
    """
    if request.method in ("GET", "HEAD"):
        return JSONResponse({
            "status": "online",
            "service": "Live Coach AI Telemetry Engine",
            "endpoint": "/api/live-coach",
            "usage": "POST JSON mit {speed_kmh, hr, slope_pct, duration_min, wind_headwind_kmh, temperature_c, vibration_level, carbs_consumed_grams, fluid_consumed_ml}",
        })

    try:
        body = await request.json()
    except Exception:
        body = {}

    from sports_science import (
        calculate_dynamic_sweat_and_dehydration,
        calculate_live_bonk_and_fueling_horizon,
        generate_live_coach_whisper,
    )

    spd = float(body.get("speed_kmh", 0.0))
    hr_val = int(body["hr"]) if body.get("hr") is not None else None
    slope = float(body.get("slope_pct", 0.0))
    dur = float(body.get("duration_min", 0.0))
    wind = float(body.get("wind_headwind_kmh", 0.0))
    temp = float(body.get("temperature_c", 20.0))
    vib = str(body.get("vibration_level", "normal"))
    sit = body.get("current_situation")
    carbs = float(body.get("carbs_consumed_grams", 0.0))
    fluid = float(body.get("fluid_consumed_ml", 0.0))
    dist = float(body.get("distance_km", 0.0))

    whisper = generate_live_coach_whisper(
        speed_kmh=spd,
        hr=hr_val,
        slope_pct=slope,
        duration_min=dur,
        wind_headwind_kmh=wind,
        temperature_c=temp,
        vibration_level=vib,
        current_situation=sit,
    )
    bonk = calculate_live_bonk_and_fueling_horizon(
        current_duration_minutes=dur,
        current_distance_km=dist,
        avg_hr=hr_val,
        carbs_consumed_grams=carbs,
    )
    dehy = calculate_dynamic_sweat_and_dehydration(
        temperature_c=temp,
        duration_minutes=dur,
        avg_hr=hr_val,
        fluid_consumed_ml=fluid,
    )

    return JSONResponse({
        "status": "success",
        "live_coach_whisper": whisper["live_coach_whisper"],
        "bonk_minutes_remaining": bonk["bonk_countdown"]["minutes_remaining"],
        "bonk_status": bonk["bonk_countdown"]["status"],
        "glycogen_battery_pct": bonk["glycogen_battery_pct"],
        "dehydration_severity": dehy["dehydration_severity"],
        "immediate_drinking_target_ml": dehy["immediate_drinking_target_ml"],
        "telemetry_received": whisper["telemetry_context"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@mcp.custom_route("/api/blackbox-upload", methods=["POST"])
async def route_blackbox_upload(request: Request) -> JSONResponse:
    """
    Autonomer Upload-Endpunkt für die DIY-Sattelstützen-Blackbox (ESP32).
    Filtert kurze Hofrunden / Schaltungstests (<150m Radius) automatisch heraus.
    """
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"Ungültiges JSON: {e}"}, status_code=400)

    track_points = body.get("track_points", [])
    ride_name = body.get("ride_name") or f"Blackbox_Fahrt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if not track_points or len(track_points) < 5:
        return JSONResponse({"status": "discarded", "reason": "Zu wenige Messpunkte (<5)."}, status_code=200)

    # 150m Puffer-Check (Hofrunde / Schaltung einstellen filtern)
    first_pt = track_points[0]
    lat0, lon0 = float(first_pt.get("lat", 0.0)), float(first_pt.get("lon", 0.0))
    max_dist_from_start_m = 0.0

    for pt in track_points:
        lat = float(pt.get("lat", 0.0))
        lon = float(pt.get("lon", 0.0))
        if lat != 0.0 and lon != 0.0 and lat0 != 0.0 and lon0 != 0.0:
            d = haversine_distance_meters(lat0, lon0, lat, lon)
            if d > max_dist_from_start_m:
                max_dist_from_start_m = d

    if max_dist_from_start_m < 150.0:
        return JSONResponse({
            "status": "discarded",
            "reason": f"Testfahrt im Hof erkannt (Maximaler Radius nur {max_dist_from_start_m:.1f}m vom Startpunkt). Daten verworfen.",
        }, status_code=200)

    # In GPX-Datei umwandeln und im downloads-Ordner ablegen
    import html as _html
    now_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_slug = re.sub(r"[^\w\-]", "_", ride_name).strip("_")
    filename = f"blackbox_{now_tag}_{clean_slug}.gpx"
    out_path = DOWNLOADS_DIR / filename

    gpx_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Strava-MCP-Blackbox" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        '  <trk>',
        f'    <name>{_html.escape(ride_name)}</name>',
        '    <type>GravelRide</type>',
        '    <trkseg>',
    ]

    for pt in track_points:
        lat = pt.get("lat", 0.0)
        lon = pt.get("lon", 0.0)
        ele = pt.get("ele", 0.0)
        t_iso = pt.get("time") or datetime.now(timezone.utc).isoformat()
        hr = pt.get("hr")

        pt_str = f'      <trkpt lat="{lat}" lon="{lon}">'
        if ele is not None:
            pt_str += f'<ele>{ele}</ele>'
        pt_str += f'<time>{t_iso}</time>'
        if hr is not None:
            pt_str += f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{hr}</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>'
        pt_str += '</trkpt>'
        gpx_lines.append(pt_str)

    gpx_lines.append('    </trkseg>')
    gpx_lines.append('  </trk>')
    gpx_lines.append('</gpx>')

    try:
        out_path.write_text("\n".join(gpx_lines), encoding="utf-8")
        sync_local_gpx_to_vector_store()
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"Konnte GPX nicht speichern: {e}"}, status_code=500)

    return JSONResponse({
        "status": "saved",
        "file": str(out_path.name),
        "points_recorded": len(track_points),
        "max_distance_from_start_m": round(max_dist_from_start_m, 1),
        "message": "Fahrt erfolgreich als GPX gespeichert und in RAG-Vektorindex synchronisiert.",
    })



def get_suffer_description(score: Optional[float]) -> Optional[str]:
    """Liefert eine verständliche Einstufung des Suffer Scores."""
    if score is None:
        return None
    if score < 50:
        return "Leichte Belastung (Regeneration / Grundlagenausdauer)"
    elif score < 100:
        return "Moderate Trainingsbelastung (Solide Ausdauereinheit)"
    elif score < 180:
        return "Hohe Trainingsbelastung (Forderndes Tempotraining / Schwellenbereich)"
    else:
        return "Extremer Suffer Score (Erschöpfende Höchstleistung / Wettkampf)"


SPORT_MAPPING: dict[str, list[str]] = {
    "ride": [
        "radfahrt", "radfahren", "ride", "cycling", "schotterfahrt", "gravel", "gravelride",
        "e-bike", "mountainbike", "mtb", "indoorcycling", "virtuelle radfahrt", "virtualride",
    ],
    "gravelride": ["schotterfahrt", "gravel", "gravelride"],
    "gravel": ["schotterfahrt", "gravel", "gravelride"],
    "cycling": ["radfahrt", "radfahren", "ride", "cycling"],
    "mountainbike": ["mountainbike", "mtb", "e-mountainbike"],
    "run": [
        "lauf", "laufen", "run", "running", "traillauf", "trailrun", "laufband",
        "virtueller lauf", "virtualrun", "joggen", "dauerlauf",
    ],
    "trailrun": ["traillauf", "trailrun"],
    "hike": ["wanderung", "hike", "hiking", "wandern", "bergwandern"],
    "walk": ["spaziergang", "walk", "walking", "gehen"],
    "swim": ["schwimmen", "swim", "swimming"],
    "workout": ["training", "workout", "gewichtstraining", "krafttraining", "crossfit", "hiit", "fitness"],
    "weight_training": ["krafttraining", "gewichtstraining", "weights", "strength"],
    "yoga": ["yoga", "pilates"],
    "indoor": [
        "indoor", "indoorcycling", "laufband", "ergometer", "krafttraining", "gewichtstraining",
        "treadmill", "virtual", "virtuell", "virtuelle radfahrt", "virtueller lauf", "rolle",
        "trainer", "workout", "training", "zwift",
    ],
    "outdoor": ["radfahrt", "schotterfahrt", "gravel", "lauf", "traillauf", "wanderung", "spaziergang", "mountainbike"],
}


def parse_date_to_datetime(val: Any) -> Optional[datetime]:
    """
    Konvertiert flexible Datumsangaben (deutsches Format 'Sa., 19.9.2026', ISO-Strings oder Timestamps)
    in ein datetime-Objekt mit UTC-Zeitzone.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)

    s = str(val).strip()
    if not s:
        return None

    # Numerischer Timestamp-String
    if re.match(r"^\d{10,13}$", s):
        ts = int(s)
        if len(s) == 13:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    # Deutsches Datumsformat z. B. "Sa., 19.9.2026" oder "19.9.2026"
    m_de = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m_de:
        day, month, year = int(m_de.group(1)), int(m_de.group(2)), int(m_de.group(3))
        return datetime(year, month, day, tzinfo=timezone.utc)

    # ISO-Format z. B. "2026-09-19" oder "2026-09-19T14:30:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # Gängige String-Datumsformate
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def find_local_activity_file(downloads_dir: Path, activity_id: str) -> Optional[Path]:
    """Sucht nach einer lokalen Aktivitätsdatei (.gpx, .tcx oder .json) anhand der ID."""
    clean_id = str(activity_id).strip()
    if not downloads_dir.exists():
        return None
    for f in downloads_dir.glob(f"{clean_id}*.*"):
        if f.suffix.lower() in (".gpx", ".tcx", ".json"):
            return f
    return None


def get_all_local_activity_files(downloads_dir: Path) -> list[Path]:
    """Liefert alle lokal gespeicherten Aktivitäten (.gpx, .tcx, .json) sortiert nach Mtime."""
    if not downloads_dir.exists():
        return []
    files = [
        f for f in downloads_dir.iterdir()
        if f.is_file() and f.suffix.lower() in (".gpx", ".tcx", ".json")
    ]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def matches_sport(sport: str, filter_sport: Optional[str]) -> bool:
    """Prüft, ob eine Sportart dem übergebenen Filter entspricht (mehrsprachig de/en)."""
    if not filter_sport:
        return True
    fs = filter_sport.strip().lower()
    s = (sport or "").strip().lower()

    if fs in SPORT_MAPPING:
        aliases = SPORT_MAPPING[fs]
        return any(alias in s or s in alias for alias in aliases)
    return fs in s or s in fs


def matches_date_range(date_str: str, after_dt: Optional[datetime], before_dt: Optional[datetime]) -> bool:
    """Prüft, ob das Datum einer Aktivität im Zeitbereich [after, before] liegt."""
    if not after_dt and not before_dt:
        return True
    act_dt = parse_date_to_datetime(date_str)
    if not act_dt:
        return True
    if after_dt and act_dt < after_dt:
        return False
    if before_dt and act_dt > before_dt:
        return False
    return True


def _task_list_strava_activities(
    context: BrowserContext,
    limit: int = 20,
    page_num: int = 1,
    sport_type: Optional[str] = None,
    indoor_only: Optional[bool] = None,
    outdoor_only: Optional[bool] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> dict[str, Any]:
    """
    Task zum Auslesen, Paginieren und Filtern der Aktivitäten über den BrowserContext.
    Lädt programmatisch Folgeseiten, bis genügend gefilterte Aktivitäten vorliegen
    oder das Ende der Liste erreicht ist.
    """
    safe_limit = max(1, min(limit, 200))
    safe_page = max(1, page_num)
    target_count = safe_page * safe_limit

    after_dt = parse_date_to_datetime(after)
    before_dt = parse_date_to_datetime(before)
    if before_dt and before_dt.hour == 0 and before_dt.minute == 0 and before_dt.second == 0:
        before_dt = before_dt.replace(hour=23, minute=59, second=59)

    page = context.new_page()
    try:
        logger.info(
            "Navigiere zu Strava Trainingsliste (limit=%d, page=%d, sport=%s, indoor=%s, after=%s, before=%s)...",
            safe_limit, safe_page, sport_type, indoor_only, after, before,
        )
        page.goto(TRAINING_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # Session-Gültigkeit prüfen
        if "/login" in page.url:
            return {
                "status": "error",
                "message": (
                    "Strava-Session ist abgelaufen oder ungültig (Weiterleitung auf /login). "
                    "Bitte führen Sie 'python setup_auth.py' aus, um sich neu anzumelden."
                ),
            }

        seen_ids: set[str] = set()
        collected_activities: list[dict[str, Any]] = []
        all_discovered_sports: dict[str, int] = defaultdict(int)
        has_next_page = False
        loaded_pages_count = 1

        while True:
            # Aktivitäten aus den Zeilen der aktuellen Seite extrahieren
            rows = page.locator("tbody tr.training-activity-row").all()
            if not rows:
                rows = page.locator("tbody tr").all()

            for row in rows:
                links = row.locator("td.col-title a[href*='/activities/'], a[href*='/activities/']").all()
                act_id = None
                title = "Unbenannt"

                for link in links:
                    href = link.get_attribute("href") or ""
                    match = re.search(r"/activities/(\d+)", href)
                    if match:
                        act_id = match.group(1)
                        link_text = link.inner_text().strip()
                        if link_text and "bearbeiten" not in link_text.lower():
                            title = link_text
                        break

                if not act_id or act_id in seen_ids:
                    continue
                seen_ids.add(act_id)

                # Zellen auslesen
                type_td = row.locator("td.col-type").first
                sport = type_td.inner_text().strip() if type_td.count() > 0 else "Unbekannt"

                date_td = row.locator("td.col-date").first
                date_str = date_td.inner_text().strip() if date_td.count() > 0 else ""

                time_td = row.locator("td.col-time").first
                duration = time_td.inner_text().strip() if time_td.count() > 0 else ""

                dist_td = row.locator("td.col-dist").first
                distance = dist_td.inner_text().strip() if dist_td.count() > 0 else ""

                elev_td = row.locator("td.col-elev").first
                elevation = elev_td.inner_text().strip() if elev_td.count() > 0 else ""

                if not sport or "titel" in date_str.lower():
                    continue

                all_discovered_sports[sport] += 1
                is_indoor = any(k in (sport + " " + title).lower() for k in [
                    "indoor", "laufband", "ergometer", "krafttraining", "treadmill", "virtual", "rolle", "trainer", "zwift"
                ])

                # In-Memory-Filterung nach Sportart und Zeitbereich
                if not matches_sport(sport, sport_type):
                    continue
                if indoor_only is True and not is_indoor:
                    continue
                if outdoor_only is True and is_indoor:
                    continue
                if not matches_date_range(date_str, after_dt, before_dt):
                    continue

                act_dt = parse_date_to_datetime(date_str)
                iso_date = act_dt.strftime("%Y-%m-%d") if act_dt else None
                is_downloaded = find_local_activity_file(DOWNLOADS_DIR, act_id) is not None

                collected_activities.append({
                    "activity_id": act_id,
                    "title": title,
                    "sport": sport,
                    "is_indoor": is_indoor,
                    "date": date_str,
                    "iso_date": iso_date,
                    "distance": distance,
                    "duration": duration,
                    "elevation": elevation,
                    "is_downloaded_locally": is_downloaded,
                })

            # Paginierung prüfen: Haben wir bereits genug passende Aktivitäten gesammelt?
            next_btn = page.locator("button.next_page, a.next_page").first
            has_next_page = bool(
                next_btn.count() > 0
                and next_btn.is_visible()
                and not next_btn.get_attribute("disabled")
            )

            if len(collected_activities) >= target_count or not has_next_page:
                break

            # Nächste Seite laden
            next_btn.click()
            page.wait_for_timeout(2000)
            loaded_pages_count += 1

        # Paginierungs-Schnitt für die angeforderte Seite
        start_idx = (safe_page - 1) * safe_limit
        end_idx = start_idx + safe_limit
        page_items = collected_activities[start_idx:end_idx]

        has_more = len(collected_activities) > end_idx or has_next_page

        logger.info(
            "list_strava_activities: %d passende Aktivitäten gefunden (%d Seiten geladen). Liefere %d Einträge (Seite %d).",
            len(collected_activities), loaded_pages_count, len(page_items), safe_page,
        )

        return {
            "status": "success",
            "page": safe_page,
            "limit": safe_limit,
            "total_matching": len(collected_activities),
            "total_scanned_on_pages": sum(all_discovered_sports.values()),
            "sport_distribution": dict(all_discovered_sports),
            "count": len(page_items),
            "has_more": has_more,
            "filters": {
                "sport_type": sport_type,
                "indoor_only": indoor_only,
                "outdoor_only": outdoor_only,
                "after": after,
                "before": before,
            },
            "activities": page_items,
        }

    finally:
        try:
            page.close()
        except Exception:
            pass


def _sync_list_strava_activities(
    limit: int = 20,
    page: int = 1,
    sport_type: Optional[str] = None,
    indoor_only: Optional[bool] = None,
    outdoor_only: Optional[bool] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> dict[str, Any]:
    logger.info("Tool 'list_strava_activities' aufgerufen (limit=%d, page=%d)...", limit, page)

    if not AUTH_FILE.exists():
        return {
            "status": "error",
            "message": (
                f"Keine Session-Datei unter '{AUTH_FILE.name}' gefunden. "
                "Bitte führen Sie einmalig 'python setup_auth.py' im Projektverzeichnis aus."
            ),
        }

    try:
        return BrowserManager.get_instance().run(
            _task_list_strava_activities,
            limit=limit,
            page_num=page,
            sport_type=sport_type,
            indoor_only=indoor_only,
            outdoor_only=outdoor_only,
            after=after,
            before=before,
        )
    except Exception as e:
        logger.error("Fehler bei 'list_strava_activities': %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Unerwarteter Fehler beim Abruf von Strava: {str(e)}",
        }


@mcp.tool()
async def list_strava_activities(
    limit: int = 20,
    page: int = 1,
    sport_type: Optional[str] = None,
    indoor_only: Optional[bool] = None,
    outdoor_only: Optional[bool] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> dict[str, Any]:
    """
    Verbindet sich mit Strava unter Verwendung der gespeicherten Session (auth.json),
    ruft die Trainingsliste auf und liefert paginierte und gefilterte Aktivitäten.
    Bietet präzise Filterung nach Sportarten (z. B. 'Run', 'Ride', 'Hike') sowie Indoor/Outdoor-Trennung.
    Liefert zudem eine aggregierte Verteilung aller auf den Seiten vorgefundenen Sportarten.

    Args:
        limit: Maximale Anzahl an Aktivitäten pro Seite (Standard: 20).
        page: Seitennummer für die Paginierung (1-basiert, Standard: 1).
        sport_type: Optionaler Filter nach Sportart (z. B. "Ride", "GravelRide", "Run", "Hike", "Walk", "Indoor").
        indoor_only: Optional filtern nach reinen Indoor-Einheiten (Laufband, Ergometer, Rolle, Krafttraining).
        outdoor_only: Optional filtern nach Outdoor-Aktivitäten mit GPS.
        after: Optionaler Startzeitpunkt (ISO-Datum wie "2026-09-01" oder Timestamp).
        before: Optionaler Endzeitpunkt (ISO-Datum wie "2026-09-20" oder Timestamp).

    Returns:
        Strukturiertes Dictionary mit Status, Paginierungs-Metadaten, Filtern, Sportarten-Verteilung und Liste der Aktivitäten.
    """
    if not AUTH_FILE.exists():
        return {
            "status": "error",
            "message": (
                f"Keine Session-Datei unter '{AUTH_FILE.name}' gefunden. "
                "Bitte führen Sie einmalig 'python setup_auth.py' im Projektverzeichnis aus."
            ),
        }

    try:
        return await BrowserManager.get_instance().run_async(
            _task_list_strava_activities,
            limit=limit,
            page_num=page,
            sport_type=sport_type,
            indoor_only=indoor_only,
            outdoor_only=outdoor_only,
            after=after,
            before=before,
        )
    except Exception as e:
        logger.error("Fehler bei 'list_strava_activities': %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Unerwarteter Fehler beim Abruf von Strava: {str(e)}",
        }
    except Exception as e:
        logger.error("Fehler bei 'list_strava_activities': %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Unerwarteter Fehler beim Abruf von Strava: {str(e)}",
        }


def _sync_download_and_analyze_activity(
    activity_id: str,
    hr_max: Optional[int] = None,
    hr_rest: Optional[int] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Lädt die GPX-Datei einer bestimmten Strava-Aktivitäts-ID herunter (falls noch nicht lokal vorhanden)
    und führt eine umfassende sportwissenschaftliche Trainingsanalyse durch (Distanz, Höhenmeter, Zeiten,
    Geschwindigkeit, Herzfrequenz-Zonen, physikalische Leistung / Virtual Watts und Pacing / Cardiac Drift).
    Berücksichtigt das Athletenprofil (Gewicht, HRmax, Ruhepuls).
    """
    logger.info("Tool 'download_and_analyze_activity' aufgerufen für ID: %s (user_id=%s)", activity_id, user_id)

    target_downloads_dir = get_user_downloads_dir(user_id)
    target_downloads_dir.mkdir(parents=True, exist_ok=True)
    clean_id = str(activity_id).strip()

    # Athletenprofil laden
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    effective_hr_max = hr_max if hr_max is not None else int(profile.get("hr_max", 205))
    effective_hr_rest = hr_rest if hr_rest is not None else int(profile.get("hr_rest", 52))
    effective_rider_kg = float(profile.get("rider_weight_kg", 60.0))
    effective_bike_kg = float(profile.get("bike_weight_kg", 9.5))
    effective_gear_kg = float(profile.get("gear_weight_kg", 2.5))
    total_system_weight = float(
        profile.get("total_system_weight_kg", round(effective_rider_kg + effective_bike_kg + effective_gear_kg, 2))
    )
    rider_weight = effective_rider_kg

    # 1. Prüfen, ob die Datei bereits lokal existiert (.gpx, .tcx oder .json)
    target_file = find_local_activity_file(target_downloads_dir, clean_id)
    if not target_file and target_downloads_dir != DOWNLOADS_DIR:
        target_file = find_local_activity_file(DOWNLOADS_DIR, clean_id)
    was_downloaded = False

    # 2. Falls nicht vorhanden: Über Playwright herunterladen (nutzt BrowserManager Singleton)
    if not target_file or not target_file.exists():
        if not AUTH_FILE.exists():
            return {
                "status": "error",
                "message": (
                    f"Aktivität {clean_id} liegt nicht lokal vor und 'auth.json' existiert nicht. "
                    "Bitte führen Sie 'python setup_auth.py' aus."
                ),
            }

        logger.info("Aktivität %s nicht lokal gefunden. Starte Download (GPX/TCX/Streams) via BrowserManager...", clean_id)
        try:
            target_file = BrowserManager.get_instance().run(
                download_activity_gpx, clean_id, DOWNLOADS_DIR, logger
            )

            if not target_file or not target_file.exists():
                return {
                    "status": "error",
                    "message": (
                        f"Download für Aktivität {clean_id} fehlgeschlagen. "
                        "Möglicherweise existiert die Aktivität nicht oder Strava stellt keine Rohdaten bereit."
                    ),
                }
            was_downloaded = True

        except Exception as e:
            logger.error("Fehler beim Herunterladen von %s: %s", clean_id, e)
            return {
                "status": "error",
                "message": f"Download-Fehler für Aktivität {clean_id}: {str(e)}",
            }

    # 3. Aktivitätsanalyse durchführen (GPX, TCX oder JSON)
    try:
        logger.info("Führe Aktivitätsanalyse durch für Datei: %s", target_file.name)
        title, activity_type, points = parse_gpx_file(target_file)

        if not points:
            return {
                "status": "error",
                "message": f"Die Datei '{target_file.name}' enthält keine auswertbaren Datenpunkte.",
            }

        has_coords = bool(points and (points[0].lat != 0.0 or points[0].lon != 0.0))

        # Wetterdaten für Startzeitpunkt & Koordinaten ermitteln (nur bei Outdoor mit Koordinaten)
        weather_info = None
        if points and points[0].time and has_coords:
            try:
                from weather_service import get_weather_for_activity
                weather_info = get_weather_for_activity(
                    lat=points[0].lat,
                    lon=points[0].lon,
                    dt=points[0].time,
                )
            except Exception as w_err:
                logger.debug("Wetterabruf für Aktivität %s übersprungen: %s", clean_id, w_err)

        # OSM Streckenoberfläche (Asphalt vs. Gravel Crr) abrufen (nur Outdoor)
        surface_info = None
        if has_coords:
            try:
                from geo_enhancements import query_osm_route_surface
                surface_info = query_osm_route_surface(points)
            except Exception as surf_err:
                logger.debug("OSM Oberflächenabruf für Aktivität %s übersprungen: %s", clean_id, surf_err)

        # DEM Satelliten-Höhenmodell (Driftprüfung) abrufen (nur Outdoor)
        dem_info = None
        if has_coords:
            try:
                from geo_enhancements import verify_elevation_dem
                dem_info = verify_elevation_dem(points)
            except Exception as dem_err:
                logger.debug("DEM Höhenverifikation für Aktivität %s übersprungen: %s", clean_id, dem_err)

        metrics = calculate_metrics(
            file_path=target_file,
            title=title,
            activity_type=activity_type,
            points=points,
            hr_max=effective_hr_max,
            hr_rest=effective_hr_rest,
            total_system_weight_kg=total_system_weight,
            rider_weight_kg=rider_weight,
            weather_data=weather_info,
            surface_data=surface_info,
            dem_data=dem_info,
        )

        return {
            "status": "success",
            "activity_id": clean_id,
            "title": metrics.title,
            "activity_type": metrics.activity_type,
            "filename": target_file.name,
            "is_new_download": was_downloaded,
            "start_time": metrics.start_time.isoformat() if metrics.start_time else None,
            "total_gps_points": metrics.total_points,
            "distance": {
                "km": round(metrics.total_distance_km, 2),
                "meters": round(metrics.total_distance_km * 1000, 0),
            },
            "elevation": {
                "gain_m": round(metrics.elevation_gain_m, 1),
                "loss_m": round(metrics.elevation_loss_m, 1),
                "min_m": round(metrics.min_elevation_m, 0) if metrics.min_elevation_m is not None else None,
                "max_m": round(metrics.max_elevation_m, 0) if metrics.max_elevation_m is not None else None,
                "source_used": metrics.elevation_source_used,
                "reconciliation_applied": metrics.elevation_reconciliation_applied,
                "original_gpx_gain_m": round(metrics.original_gpx_gain_m, 1) if metrics.original_gpx_gain_m is not None else round(metrics.elevation_gain_m, 1),
                "satellite_dem_gain_m": round(dem_info.get("satellite_elevation_gain_m", 0.0), 1) if dem_info and dem_info.get("satellite_elevation_gain_m") else None,
                "variance_pct": dem_info.get("elevation_variance_pct", 0.0) if dem_info else 0.0,
            },
            "data_quality_and_anomalies": {
                "verdict": metrics.data_quality_verdict,
                "anomaly_count": len(metrics.anomalies),
                "anomalies": metrics.anomalies,
            },
            "time": {
                "elapsed_hms": format_seconds_to_hms(metrics.elapsed_time_sec),
                "moving_hms": format_seconds_to_hms(metrics.moving_time_sec),
                "paused_hms": format_seconds_to_hms(metrics.paused_time_sec),
                "moving_minutes": round(metrics.moving_time_sec / 60.0, 1),
            },
            "speed": {
                "avg_moving_kmh": round(metrics.avg_speed_moving_kmh, 2),
                "avg_total_kmh": round(metrics.avg_speed_total_kmh, 2),
                "max_kmh": round(metrics.max_speed_kmh, 2),
                "raw_max_kmh": round(metrics.raw_max_speed_kmh, 2) if metrics.raw_max_speed_kmh is not None else round(metrics.max_speed_kmh, 2),
                "speed_filtered": metrics.speed_filtered,
                "filter_reason": metrics.speed_filter_reason,
            },
            "cardiac_drift": {
                "drift_pct": metrics.coaching_telemetry.get("cardiac_drift_pct"),
                "status": metrics.coaching_telemetry.get("cardiac_drift_status", "evaluated" if metrics.coaching_telemetry.get("cardiac_drift_pct") is not None else "omitted"),
                "first_half_avg_hr": metrics.coaching_telemetry.get("first_half_avg_hr"),
                "second_half_avg_hr": metrics.coaching_telemetry.get("second_half_avg_hr"),
                "first_half_speed_kmh": metrics.coaching_telemetry.get("first_half_avg_speed_kmh"),
                "second_half_speed_kmh": metrics.coaching_telemetry.get("second_half_avg_speed_kmh"),
                "split_context": metrics.coaching_telemetry.get("split_context"),
                "findings": metrics.coaching_telemetry.get("coach_critical_findings", []),
            },
            "heart_rate": {
                "available": metrics.hr_available,
                "avg_bpm": round(metrics.avg_hr, 1) if metrics.avg_hr else None,
                "max_bpm": metrics.max_hr,
                "min_bpm": metrics.min_hr,
                "resting_bpm_basis": metrics.hr_rest_used,
                "resting_bpm_source": metrics.hr_rest_source,
                "hr_max_basis": effective_hr_max,
                "suffer_score": round(metrics.suffer_score, 1) if metrics.suffer_score else None,
                "suffer_score_description": get_suffer_description(metrics.suffer_score),
                "zones": [
                    {
                        "zone": f"Z{z.zone_idx}",
                        "name": z.name,
                        "bpm_range": f"{z.min_bpm}-{z.max_bpm} bpm",
                        "duration_hms": format_seconds_to_hms(z.duration_sec),
                        "duration_seconds": int(z.duration_sec),
                        "percentage": round(z.percentage, 1),
                    }
                    for z in metrics.zones
                ] if metrics.hr_available else [],
            },
            "virtual_power": metrics.virtual_power,
            "coaching_telemetry": metrics.coaching_telemetry,
            "nutrition_metabolism": metrics.nutrition_metabolism,
            "vam_diagnostics": metrics.vam_diagnostics,
            "surface_analysis": metrics.surface_analysis,
            "dem_verification": metrics.dem_verification,
            "weather_conditions": weather_info,
            "athlete_profile": {
                "rider_weight_kg": effective_rider_kg,
                "bike_weight_kg": effective_bike_kg,
                "gear_weight_kg": effective_gear_kg,
                "system_weight_kg": total_system_weight,
                "hr_max": effective_hr_max,
                "hr_rest": effective_hr_rest,
                "athlete_type": "Climber / Lightweight",
            },
            "coaching_directives": {
                "coaching_style": "direct_analytical",
                "focus_areas": [
                    "cardiac_drift_monitoring",
                    "pacing_discipline",
                    "climbing_power_consistency",
                    "virtual_watts_validation",
                ],
            },
        }

    except Exception as e:
        logger.error("Fehler bei der GPX-Analyse von %s: %s", target_file.name, e, exc_info=True)
        return {
            "status": "error",
            "message": f"Analyse-Fehler für '{target_file.name}': {str(e)}",
        }


@mcp.tool()
async def download_and_analyze_activity(
    activity_id: str,
    hr_max: Optional[int] = None,
    hr_rest: Optional[int] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Lädt die GPX-Datei einer bestimmten Strava-Aktivitäts-ID herunter (falls noch nicht lokal vorhanden)
    und führt eine umfassende sportwissenschaftliche Trainingsanalyse durch (Distanz, Höhenmeter, Zeiten,
    Geschwindigkeit, Herzfrequenz-Zonen, berechnete Virtual Watts und Pacing/Cardiac Drift).
    Nutzt automatisch das hinterlegte Athletenprofil (Gewicht, HRmax, Ruhepuls).

    Args:
        activity_id: Die numerische Strava-Aktivitäts-ID (z. B. "20239426058").
        hr_max: Optionaler Maximalpuls (Standard: aus Athletenprofil, default 205 bpm).
        hr_rest: Optionaler Ruhepuls (Standard: aus Athletenprofil bzw. dynamisch ermittelt).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Strukturiertes Dictionary mit allen berechneten Metriken, Virtual Power und Coaching-Telemetrie.
    """
    return await asyncio.to_thread(_sync_download_and_analyze_activity, activity_id, hr_max, hr_rest, user_id)


def _sync_get_activity_streams(
    activity_id: str,
    interval_seconds: int = 30,
) -> dict[str, Any]:
    """
    Synchroner Helfer für get_activity_streams:
    Liest die GPX-Datei ein, berechnet Distanz- und Tempowerte je Datenpunkt
    und führt ein zeitbasiertes Downsampling durch (z. B. ein Punkt alle 30s).
    """
    logger.info(
        "Tool 'get_activity_streams' aufgerufen für ID: %s (Intervall: %ds)...",
        activity_id,
        interval_seconds,
    )
    clean_id = str(activity_id).strip()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Datei suchen oder bei Bedarf herunterladen
    target_file = find_local_activity_file(DOWNLOADS_DIR, clean_id)

    if not target_file or not target_file.exists():
        if not AUTH_FILE.exists():
            return {
                "status": "error",
                "message": (
                    f"Aktivität {clean_id} liegt lokal nicht vor und 'auth.json' existiert nicht. "
                    "Bitte führen Sie 'python setup_auth.py' aus."
                ),
            }

        logger.info("Aktivität %s nicht lokal gefunden. Lade Aktivitätsdatei herunter...", clean_id)
        with sync_playwright() as p:
            try:
                browser = launch_browser(p, headless=True, logger=logger)
                context = create_browser_context(browser, storage_state_path=AUTH_FILE, logger=logger)
                target_file = download_activity_gpx(context, clean_id, DOWNLOADS_DIR, logger)
                browser.close()

                if not target_file or not target_file.exists():
                    return {
                        "status": "error",
                        "message": f"Download für Aktivität {clean_id} fehlgeschlagen.",
                    }
            except Exception as e:
                logger.error("Download-Fehler für %s: %s", clean_id, e)
                return {
                    "status": "error",
                    "message": f"Download-Fehler für Aktivität {clean_id}: {str(e)}",
                }

    # 2. Aktivitätsdatei parsen (GPX, TCX, JSON)
    try:
        title, activity_type, points = parse_gpx_file(target_file)
        if not points:
            return {
                "status": "error",
                "message": f"Die Datei '{target_file.name}' enthält keine auswertbaren Datenpunkte.",
            }

        # 3. Kumulierte Distanzen vorab berechnen
        cum_dist_m = 0.0
        dists_m: list[float] = [0.0]
        for i in range(1, len(points)):
            p_prev = points[i - 1]
            p_curr = points[i]
            if (p_prev.lat != 0.0 or p_prev.lon != 0.0) and (p_curr.lat != 0.0 or p_curr.lon != 0.0):
                d = haversine_distance_meters(p_prev.lat, p_prev.lon, p_curr.lat, p_curr.lon)
            elif p_curr.distance_m is not None and p_prev.distance_m is not None:
                d = max(0.0, p_curr.distance_m - p_prev.distance_m)
            else:
                d = 0.0
            cum_dist_m += d
            dists_m.append(cum_dist_m)

        # 4. Intelligentes Downsampling durchführen
        interval_step = max(1, interval_seconds)
        streams: list[dict[str, Any]] = []

        last_sampled_time = points[0].time
        last_sampled_idx = 0

        # Erster Punkt
        streams.append({
            "time": points[0].time.strftime("%H:%M:%S") if points[0].time else "00:00:00",
            "distance_km": 0.0,
            "ele": round(points[0].ele, 1) if points[0].ele is not None else None,
            "hr": points[0].hr,
            "speed_kmh": 0.0,
        })

        for i in range(1, len(points)):
            pt = points[i]
            is_last_point = (i == len(points) - 1)

            should_sample = False
            dt_s = 0.0

            if pt.time and last_sampled_time:
                dt_s = (pt.time - last_sampled_time).total_seconds()
                if dt_s >= interval_step or is_last_point:
                    should_sample = True
            elif (i - last_sampled_idx) >= interval_step or is_last_point:
                should_sample = True
                dt_s = float(i - last_sampled_idx)

            if should_sample:
                dd_m = dists_m[i] - dists_m[last_sampled_idx]
                speed_kmh = (dd_m / dt_s) * 3.6 if dt_s > 0 else 0.0
                speed_kmh = min(130.0, max(0.0, speed_kmh))

                streams.append({
                    "time": pt.time.strftime("%H:%M:%S") if pt.time else f"+{int(dists_m[i])}m",
                    "distance_km": round(dists_m[i] / 1000.0, 2),
                    "ele": round(pt.ele, 1) if pt.ele is not None else None,
                    "hr": pt.hr,
                    "speed_kmh": round(speed_kmh, 1),
                })
                last_sampled_time = pt.time
                last_sampled_idx = i

        logger.info(
            "Streams für Aktivität %s erzeugt: %d Punkte auf %d gesampelt (Intervall: %ds).",
            clean_id,
            len(points),
            len(streams),
            interval_step,
        )

        return {
            "status": "success",
            "activity_id": clean_id,
            "title": title,
            "activity_type": activity_type,
            "total_points_original": len(points),
            "sampled_points_count": len(streams),
            "interval_seconds": interval_step,
            "streams": streams,
        }

    except Exception as e:
        logger.error("Fehler beim Erzeugen der Streams für %s: %s", target_file.name, e, exc_info=True)
        return {
            "status": "error",
            "message": f"Fehler bei Stream-Generierung für '{target_file.name}': {str(e)}",
        }


@mcp.tool()
async def get_activity_streams(
    activity_id: str,
    interval_seconds: int = 30,
) -> dict[str, Any]:
    """
    Liefert zeitverlaufsorientierte Rohdaten (Streams) für eine Strava-Aktivität mit intelligentem Downsampling.
    Enthält Zeitstempel, kumulierte Distanz (km), Höhe (m), Puls (bpm) und Intervall-Geschwindigkeit (km/h).

    Args:
        activity_id: Die numerische Strava-Aktivitäts-ID (z. B. "20239426058").
        interval_seconds: Zeitlicher Abstand zwischen den Datenpunkten in Sekunden zur Reduzierung der Datenmenge (Standard: 30s).

    Returns:
        Kompaktes JSON-Dictionary mit den gesampelten Datenreihen (Streams).
    """
    return await asyncio.to_thread(_sync_get_activity_streams, activity_id, interval_seconds)


@mcp.tool()
def list_local_gpx_files(
    sport_type: Optional[str] = None,
    indoor_only: Optional[bool] = None,
    outdoor_only: Optional[bool] = None,
    limit: Optional[int] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Listet alle bereits im lokalen Download-Verzeichnis vorhandenen Aktivitätsdateien (.gpx, .tcx, .json) auf,
    ohne eine Internet- oder Browserverbindung zu benötigen.
    Bietet präzise Filterung nach Sportarten (z. B. 'Run', 'Ride', 'Hike') sowie Indoor/Outdoor-Trennung.
    Liefert Dateinamen, Aktivitäts-ID, Tour-Namen, Sportart, Indoor-Status, Dateigröße, den echten Startzeitpunkt der Aktivität
    sowie eine aggregierte Sportarten-Verteilung aller lokalen Dateien.

    Args:
        sport_type: Optionaler Filter nach Sportart (z. B. "Run", "Ride", "Hike", "Indoor").
        indoor_only: Optional filtern nach reinen Indoor-Einheiten (Laufband, Ergometer, Krafttraining).
        outdoor_only: Optional filtern nach Outdoor-Aktivitäten mit GPS.
        limit: Maximale Anzahl zurückzugebender Dateien (Standard: alle).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").
    """
    target_dir = get_user_downloads_dir(user_id)
    files = get_all_local_activity_files(target_dir)
    all_discovered_sports: dict[str, int] = defaultdict(int)
    matched_results = []

    for f in files:
        ext = f.suffix.lower().lstrip(".")
        match = re.match(r"^(\d+)(?:_(.*))?\.(gpx|tcx|json)$", f.name, re.IGNORECASE)
        act_id = match.group(1) if match else "Unbekannt"
        tour_name = match.group(2).replace("_", " ") if match and match.group(2) else "Unbekannt"

        # Schnell-Extraktion des echten Aktivitätszeitstempels & Head-Snippet
        activity_start_time: Optional[str] = None
        head = ""
        try:
            with open(f, "rb") as fp:
                head = fp.read(2048).decode("utf-8", errors="ignore")
            m_time = re.search(r"<(?:time|Time)>([^<]+)</(?:time|Time)>", head)
            if m_time:
                activity_start_time = m_time.group(1).strip()
            elif ext == "json":
                m_json = re.search(r'"(?:start_time|date)":\s*"([^"]+)"', head)
                if m_json:
                    activity_start_time = m_json.group(1).strip()
        except Exception:
            pass

        # Sportart & Indoor-Status ableiten
        sport = "Ride"
        combined_name = (f.name + " " + tour_name).lower()
        if ext == "json":
            m_s = re.search(r'"(?:sport_type|activity_type|sport|type)":\s*"([^"]+)"', head, re.IGNORECASE)
            if m_s:
                sport = m_s.group(1).strip()
        elif ext == "tcx":
            m_tcx = re.search(r'Sport="([^"]+)"', head, re.IGNORECASE)
            if m_tcx:
                sport = m_tcx.group(1).strip()
        elif ext == "gpx":
            m_gpx = re.search(r'<(?:type|sport)>([^<]+)</(?:type|sport)>', head, re.IGNORECASE)
            if m_gpx:
                sport = m_gpx.group(1).strip()

        if any(k in combined_name for k in ["lauf", "run", "joggen", "marathon"]):
            sport = "Run"
        elif any(k in combined_name for k in ["wander", "hike"]):
            sport = "Hike"
        elif any(k in combined_name for k in ["spazier", "walk"]):
            sport = "Walk"
        elif any(k in combined_name for k in ["kraft", "weight", "gym", "workout"]):
            sport = "Workout"
        elif any(k in combined_name for k in ["schotter", "gravel"]):
            sport = "GravelRide"
        elif any(k in combined_name for k in ["mtb", "mountain"]):
            sport = "MountainBike"

        is_indoor = any(k in combined_name or k in head.lower() for k in [
            "indoor", "laufband", "ergometer", "krafttraining", "treadmill", "virtual", "rolle", "trainer", "zwift", "hiit"
        ])

        all_discovered_sports[sport] += 1

        # Filter anwenden
        if not matches_sport(sport, sport_type):
            continue
        if indoor_only is True and not is_indoor:
            continue
        if outdoor_only is True and is_indoor:
            continue

        f_stat = f.stat()
        mtime_iso = datetime.fromtimestamp(f_stat.st_mtime, tz=timezone.utc).isoformat()

        matched_results.append({
            "filename": f.name,
            "format": ext.upper(),
            "activity_id": act_id,
            "tour_name": tour_name,
            "sport": sport,
            "is_indoor": is_indoor,
            "size_bytes": f_stat.st_size,
            "activity_start_time": activity_start_time or mtime_iso,
            "file_modified_at": mtime_iso,
            "last_modified": mtime_iso,
            "last_modified_timestamp": f_stat.st_mtime,
        })

    if limit is not None and limit > 0:
        paged_results = matched_results[:limit]
    else:
        paged_results = matched_results

    return {
        "status": "success",
        "user_id": user_id,
        "total_local_files": len(files),
        "total_matching": len(matched_results),
        "sport_distribution": dict(all_discovered_sports),
        "filters": {
            "sport_type": sport_type,
            "indoor_only": indoor_only,
            "outdoor_only": outdoor_only,
            "limit": limit,
        },
        "count": len(paged_results),
        "files": paged_results,
    }


def _sync_get_activity_segments(activity_id: str) -> dict[str, Any]:
    logger.info("Tool 'get_activity_segments' aufgerufen für ID: %s", activity_id)

    if not AUTH_FILE.exists():
        return {
            "status": "error",
            "message": (
                f"Keine Session-Datei unter '{AUTH_FILE.name}' gefunden. "
                "Bitte führen Sie einmalig 'python setup_auth.py' im Projektverzeichnis aus."
            ),
        }

    clean_id = re.sub(r"\D", "", str(activity_id).strip())
    if not clean_id:
        return {
            "status": "error",
            "message": f"Ungültige Aktivitäts-ID: '{activity_id}'",
        }

    try:
        return BrowserManager.get_instance().run(
            fetch_activity_segments, clean_id, logger
        )
    except Exception as e:
        logger.error("Fehler bei 'get_activity_segments' für ID %s: %s", clean_id, e, exc_info=True)
        return {
            "status": "error",
            "message": f"Unerwarteter Fehler beim Abruf der Segmente für Aktivität {clean_id}: {str(e)}",
        }


@mcp.tool()
async def get_activity_segments(activity_id: str) -> dict[str, Any]:
    """
    Liest alle von Strava berechneten Segmente zu einer Aktivitäts-ID live per Headless-Browser aus.
    Liefert Segment-Name, Distanz, Höhenmeter, Steigung, Zeit, Durchschnittsgeschwindigkeit,
    Leistung, VAM, Herzfrequenz und Auszeichnungen (PR / Rang / Medaillen).

    Args:
        activity_id: Die numerische Strava-Aktivitäts-ID (z. B. "20239426058").

    Returns:
        Strukturiertes Dictionary mit den Segmentdaten und Metriken der Aktivität.
    """
    if not AUTH_FILE.exists():
        return {
            "status": "error",
            "message": (
                f"Keine Session-Datei unter '{AUTH_FILE.name}' gefunden. "
                "Bitte führen Sie einmalig 'python setup_auth.py' im Projektverzeichnis aus."
            ),
        }

    clean_id = re.sub(r"\D", "", str(activity_id).strip())
    if not clean_id:
        return {
            "status": "error",
            "message": f"Ungültige Aktivitäts-ID: '{activity_id}'",
        }

    try:
        return await BrowserManager.get_instance().run_async(
            fetch_activity_segments, clean_id, logger
        )
    except Exception as e:
        logger.error("Fehler bei 'get_activity_segments' für ID %s: %s", clean_id, e, exc_info=True)
        return {
            "status": "error",
            "message": f"Unerwarteter Fehler beim Abruf der Segmente für Aktivität {clean_id}: {str(e)}",
        }


# ---------------------------------------------------------------------------
# Modul 1: Lokales Vektor-RAG & Semantische Suche
# ---------------------------------------------------------------------------
@mcp.tool()
async def search_activities_semantic(
    query: str,
    limit: int = 5,
    sport_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Führt eine semantische Vektorsuche über alle sportlichen Aktivitäten, Segmente und
    Streckenprofile durch (z. B. 'anstrengende Bergfahrten mit steilen Steigungen' oder
    'flache lockere Ausdauerrunden').
    Nutzt ein lokales 384-dimensionales FastEmbed ONNX-Modell und eine SQLite-Vektordatenbank.

    Args:
        query: Die semantische Suchanfrage (z. B. 'Gravel Tour mit vielen Höhenmetern').
        limit: Maximale Anzahl der zurückzugebenden Treffer (Standard: 5).
        sport_type: Optionaler Filter auf Sportart (z. B. 'Ride', 'GravelRide', 'Run', 'Hike').

    Returns:
        Strukturiertes Dictionary mit den besten Übereinstimmungen inkl. Ähnlichkeitsscore,
        Metriken, Schlüssel-Segmenten und Kontext-Zusammenfassung.
    """
    logger.info("Tool 'search_activities_semantic' aufgerufen: '%s' (limit=%d, sport=%s)", query, limit, sport_type)
    try:
        def _do_search() -> list[dict[str, Any]]:
            store = ActivityVectorStore()
            if store.count() == 0:
                logger.info("Vektor-Store ist leer. Führe initialen Sync der lokalen GPX-Dateien durch...")
                sync_local_gpx_to_vector_store()
            return store.search_semantic(query=query, limit=limit, sport_filter=sport_type)

        results = await asyncio.to_thread(_do_search)
        return {
            "status": "success",
            "query": query,
            "count": len(results),
            "sport_filter": sport_type,
            "results": results,
        }
    except Exception as e:
        logger.error("Fehler bei 'search_activities_semantic': %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Fehler bei der semantischen Suche: {str(e)}",
        }


# ---------------------------------------------------------------------------
# Modul 3: Wissenschaftliches Belastungsmodell (CTL / ATL / TSB)
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_training_status(user_id: str = "default") -> dict[str, Any]:
    """
    Berechnet den aktuellen Trainings- und Formzustand nach dem wissenschaftlichen
    Belastungsmodell von Eric Banister und Andrew Coggan:
    - CTL (Fitness): 42-Tage-Exponentialdurchschnitt.
    - ATL (Ermüdung): 7-Tage-Exponentialdurchschnitt.
    - TSB (Form / Frische): CTL minus ATL.
    - Ramp Rate: Wöchentlicher Fitnesszuwachs zur Verletzungsprävention.
    - Formzone & Handlungsempfehlungen für das nächste Training.
    - Recovery-Loop: Verknüpfung mit Apple-Health-Readiness, HRV-Konfidenz und Übertrainingswarnung.
    - Saubere Trennung von Metriken, strukturiertem Athletenprofil und Coaching-Direktiven.

    Args:
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Umfassende sportwissenschaftliche Diagnose mit aktuellem Status, Formzone,
        Recovery-Kontext, Warnungen, 14-Tage-Historie, Athletenprofil und Steuerungs-Direktiven.
    """
    logger.info("Tool 'get_training_status' aufgerufen (user_id=%s).", user_id)
    try:
        def _do_calc() -> dict[str, Any]:
            engine = load_historical_training_data(user_id=user_id)
            return engine.get_training_status(user_id=user_id)

        status = await asyncio.to_thread(_do_calc)
        return status
    except Exception as e:
        logger.error("Fehler bei 'get_training_status': %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Fehler bei der Berechnung des Belastungsmodells: {str(e)}",
        }


# ---------------------------------------------------------------------------
# Modul 2: Event-Driven Automatisierter Hintergrund-Sync
# ---------------------------------------------------------------------------
@mcp.tool()
async def trigger_background_sync(force: bool = False) -> dict[str, Any]:
    """
    Stößt manuell eine sofortige Hintergrund-Synchronisation mit Strava an.
    Prüft auf neue Einheiten, lädt fehlende GPX-Dateien herunter, extrahiert Segmente
    und aktualisiert den lokalen Vektor-Store sowie das Belastungsmodell.

    Args:
        force: Wenn True, werden auch bereits vorhandene Aktivitäten erneut abgeglichen.

    Returns:
        Statusbericht des Synchronisationslaufs mit Anzahl neuer Aktivitäten und Fehlern.
    """
    logger.info("Tool 'trigger_background_sync' aufgerufen (force=%s).", force)
    try:
        service = BackgroundSyncService.get_instance()
        res = await service.run_sync_async(force=force)
        return res
    except Exception as e:
        logger.error("Fehler bei 'trigger_background_sync': %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Fehler bei der Hintergrund-Synchronisation: {str(e)}",
        }


@mcp.tool()
async def get_sync_status() -> dict[str, Any]:
    """
    Liefert den aktuellen Status des Hintergrund-Synchronisationsdienstes
    (letzter Sync, Fehler, anstehendes Intervall).

    Returns:
        Statusinformationen des Background-Sync-Services.
    """
    service = BackgroundSyncService.get_instance()
    return {
        "status": "success",
        "sync_service": service.get_status(),
    }


# ---------------------------------------------------------------------------
# Modul 4: Athleten-Profil, Leistungs-Physiologie & Coaching-Gedächtnis (Memory)
# ---------------------------------------------------------------------------
@mcp.tool()
def get_athlete_profile(user_id: str = "default") -> dict[str, Any]:
    """
    Liefert das vollständige Profil des Athleten (Körpergewicht, Radgewicht,
    Maximalpuls, Ruhepuls, Trainingsziele, bekannte Schwächen und Coaching-Präferenzen).
    Enthält zudem die neuesten Trainer-Notizen aus früheren Chats und strukturierte Coaching-Direktiven.
    Dieses Tool sollte vom KI-Assistenten zu Beginn einer Trainingsanalyse aufgerufen werden.

    Args:
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Detailliertes Profil, Systemgewicht und strukturierte Coaching-Direktiven für die KI.
    """
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    notes = store.get_coaching_notes(limit=5, user_id=user_id)
    coaching_directives = {
        "coaching_style": "direct_analytical",
        "focus_areas": [
            "weakness_identification",
            "pacing_consistency",
            "cardiac_drift_monitoring",
            "cadence_efficiency",
            "overtraining_prevention",
        ],
    }
    return {
        "status": "success",
        "user_id": user_id,
        "athlete_profile": profile,
        "recent_coach_notes": notes,
        "coaching_directives": coaching_directives,
    }


@mcp.prompt("cycling_coach")
def cycling_coach_prompt(athlete_name: str = "Athlet") -> str:
    """
    Standardisierter MCP-Systemprompt für den ganzheitlichen Radsport- und Ausdauer-Coach.
    Definiert Tonfall, adaptive Empathie, Analysefokus und sportwissenschaftliche Leitlinien für das LLM,
    sauber getrennt von Tool-Daten-Payloads (verhindert Prompt-Injection-Risiken).
    """
    return f"""Du agierst als hochqualifizierter, sportwissenschaftlicher Headcoach, Mentor und Leistungsdiagnostiker für {athlete_name}.

Deine Coaching-Grundsätze & adaptive Tonalität:
1. Adaptive Empathie & emotionale Intelligenz:
   - Bei Erschöpfung, schlechten Tagen oder gedämpften Apple-Health-Vitaldaten (HRV niedrig, Ruhepuls erhöht): Zeige menschliches Verständnis, nimm Druck heraus und schütze die mentale Motivation ("Haken dran, jeder hat mal schlechte Tage. Wichtig ist jetzt Schlaf und gutes Essen").
   - Bei leichten Grundlagen- oder Regenerationstagen: Sei streng und disziplinierend! Verhindere, dass der Athlet im "Grauzonen-Niemandsland" zu schnell fährt und sich die Frische für harte Tage verbaut.
   - Vor Schlüssel-Einheiten, Segment-Jagden oder Rennen: Agiere taktisch scharf, fokussiert und motivierend wie ein echter Renningenieur.
2. Aktives Nachfragen (Active Listening):
   - Urteile nie vorschnell rein über Telemetriedaten! Frage bei Auffälligkeiten (hoher Puls, Pacing-Einbruch, Trittfrequenzabfall) proaktiv nach dem subjektiven Befinden (Beine-Gefühl, RPE, Schul-/Alltagsstress, Windschatten in der Gruppe oder Magenprobleme) und nutze 'record_post_workout_checkin'.
3. Ganzheitliche Belastungssteuerung (Banister/Coggan + Vitaldaten):
   - Korreliere stets die chronische Trainingsbelastung (CTL/ATL/TSB) mit Schlafarchitektur, HRV und Ruhepuls.
   - Ramp Rate streng im Blick behalten (+3 bis +5 CTL/Woche optimal, >6 CTL/Woche kritisch für Sehnenansätze und Immunsystem).
4. Nachwuchs- und Entwicklungsfokus:
   - Achte auf Trittfrequenz-Souplesse (90-100 U/min) zur Gelenkschonung.
   - Schütze Wachstumsfugen und Sehnen vor Überlastung (insb. Patellasehne).
5. Vorausschauende Taktik & Verpflegung:
   - Erinnere bei Fahrten >60 Min an gezielte Kohlenhydrat- (Jeukendrup) und Natriumzufuhr, um Hungeräste (Bonking) und Dehydrierung aktiv zu verhindern."""


@mcp.tool()
def update_athlete_profile(field: str, value: Any, user_id: str = "default") -> dict[str, Any]:
    """
    Aktualisiert ein Merkmal im Profil des Athleten (z. B. 'rider_weight_kg', 'bike_weight_kg',
    'gear_weight_kg', 'hr_max', 'hr_rest', 'goals', 'known_weaknesses').
    Persistiert die Information dauerhaft in der lokalen SQLite-Datenbank.

    Args:
        field: Name des Feldes (z. B. 'rider_weight_kg' oder 'goals').
        value: Der neue Wert (z. B. 61.5 oder 'Teilnahme an Nachwuchsrennen im Sommer').
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Aktualisiertes vollständiges Profil.
    """
    store = ActivityVectorStore()
    updated = store.set_profile_field(field, value, user_id=user_id)
    return {
        "status": "success",
        "user_id": user_id,
        "updated_field": field,
        "new_value": value,
        "profile": updated,
    }


@mcp.tool()
def add_coaching_note(note: str, category: str = "general", user_id: str = "default") -> dict[str, Any]:
    """
    Speichert eine wichtige Beobachtung, Rückmeldung oder ein Ereignis (z. B. 'Verletzung/Schmerzen',
    'Ernährungsfehler', 'Ausrüstungswechsel', 'Feedback zum Rennen') im Langzeitgedächtnis des MCP-Servers.
    Jedes LLM kann in künftigen Sitzungen auf diese Notizen zugreifen.

    Args:
        note: Inhalt der Notiz (z. B. 'Knieschmerzen nach 60km bei niedriger Trittfrequenz am Berg festgestellt').
        category: Kategorie der Notiz ('injury', 'pacing', 'nutrition', 'equipment', 'general').
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Bestätigung mit Zeitstempel und Notiz-ID.
    """
    store = ActivityVectorStore()
    res = store.add_coaching_note(note=note, category=category, user_id=user_id)
    return {
        "status": "success",
        "user_id": user_id,
        "note_entry": res,
    }


@mcp.tool()
def get_coaching_notes(limit: int = 10, category: Optional[str] = None, user_id: str = "default") -> dict[str, Any]:
    """
    Ruft gespeicherte Coaching- und Feedbacknotizen aus dem Langzeitgedächtnis des MCP-Servers ab.

    Args:
        limit: Maximale Anzahl der zurückzugebenden Notizen (Standard: 10).
        category: Optionaler Filter auf Kategorie ('injury', 'pacing', 'nutrition', etc.).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Liste historischer Coaching-Notizen.
    """
    store = ActivityVectorStore()
    notes = store.get_coaching_notes(limit=limit, category=category, user_id=user_id)
    return {
        "status": "success",
        "user_id": user_id,
        "count": len(notes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Modul 5: Universal-Wetterdienst & Aerodynamik (OpenWeatherMap)
# ---------------------------------------------------------------------------
@mcp.tool()
def get_weather(location: str, date: Optional[str] = None) -> dict[str, Any]:
    """
    Fragt das aktuelle oder historische Wetter an einem beliebigen Ort ab (unter Nutzung von
    OpenWeatherMap und Reanalyse-Archivdaten).
    Liefert Temperatur, gefühlte Temperatur, Luftfeuchtigkeit, barometrischen Druck,
    exakte Luftdichte rho (kg/m³), Windgeschwindigkeit, Windrichtung sowie einen
    Radsport-Eignungsindex (1-10) mit konkreten Bekleidungs- und Ausrüstungsempfehlungen.

    Args:
        location: Ortsname (z. B. "Freiburg", "Berlin, DE", "Schwarzwald") oder GPS-Koordinaten ("48.0, 7.8").
        date: Optionales Datum (z. B. "2026-09-21" bzw. None für aktuelles Wetter).

    Returns:
        Detaillierte meteorologische Auswertung und Eignung für Ausfahrten.
    """
    from weather_service import get_weather_universal
    return get_weather_universal(location=location, date_str=date)


# ---------------------------------------------------------------------------
# Modul 6: Geodaten, Topografie & Satelliten-Höhenmodell (OSM & DEM)
# ---------------------------------------------------------------------------
@mcp.tool()
def get_route_terrain_and_elevation(activity_id: str) -> dict[str, Any]:
    """
    Analysiert die Streckenoberfläche (Asphalt, Schotter, Kopfsteinpflaster etc.)
    über OpenStreetMap (Overpass API) und verifiziert das Höhenprofil gegen hochauflösende
    Satelliten- und Geländemodelle (Open-Meteo DEM).
    Erkennt barometrischen Drift und berechnet den physikalischen Rollwiderstand (C_rr).
    100% anonym, öffentlich und ohne Registrierung.

    Args:
        activity_id: Die numerische Strava-Aktivitäts-ID (z. B. "20239426058").

    Returns:
        Detaillierte Oberflächenanalyse, Schotteranteil, Windschutz durch Wald und DEM-Verifikation.
    """
    from geo_enhancements import query_osm_route_surface, verify_elevation_dem
    clean_id = str(activity_id).strip()
    target_file = find_local_activity_file(DOWNLOADS_DIR, clean_id)
    if not target_file:
        return {
            "status": "error",
            "message": f"Keine lokale Aktivitätsdatei für ID {clean_id} gefunden. Bitte vorher 'download_and_analyze_activity' ausführen.",
        }
    title, activity_type, points = parse_gpx_file(target_file)
    if not points:
        return {"status": "error", "message": "Keine Datenpunkte in der Datei."}

    has_coords = bool(points and (points[0].lat != 0.0 or points[0].lon != 0.0))
    if not has_coords:
        return {
            "status": "info",
            "activity_id": clean_id,
            "title": title,
            "activity_type": activity_type,
            "message": "Indoor-Aktivität ohne GPS-Koordinaten (Laufband / Ergometer). Oberflächen- und Geländemodelle sind für Hallen-/Indoor-Training nicht anwendbar.",
        }

    surface = query_osm_route_surface(points)
    dem = verify_elevation_dem(points)

    return {
        "status": "success",
        "activity_id": clean_id,
        "title": title,
        "surface_analysis": surface,
        "dem_verification": dem,
    }


# ---------------------------------------------------------------------------
# Modul 7: Wissenschaftliche Stoffwechsel- & Verpflegungsdiagnostik (Jeukendrup/Romijn)
# ---------------------------------------------------------------------------
@mcp.tool()
def get_metabolic_and_nutrition_diagnostics(activity_id: str) -> dict[str, Any]:
    """
    Berechnet die exakte Substratoxidation (verbrannte Gramm Kohlenhydrate & Fette),
    stündliche Verbrennungsraten, das akute Hungerast-Risiko (Bonking / "Ast")
    sowie Schweiß- und Natriumverlust für eine Aktivität nach Jeukendrup & Romijn.
    Funktioniert für Outdoor- und Indoor-Aktivitäten (Laufband, Ergometer, Krafttraining).
    100% deterministisch, lokal und ohne externe Cloud.

    Args:
        activity_id: Die numerische Strava-Aktivitäts-ID (z. B. "20239426058").

    Returns:
        Grammgenaue Stoffwechselanalyse, Bonk-Risiko, Schweißverlust und konkrete Verpflegungspläne.
    """
    clean_id = str(activity_id).strip()
    target_file = find_local_activity_file(DOWNLOADS_DIR, clean_id)
    if not target_file:
        return {
            "status": "error",
            "message": f"Keine lokale Datei für Aktivität {clean_id} gefunden. Bitte vorher 'download_and_analyze_activity' ausführen.",
        }
    title, activity_type, points = parse_gpx_file(target_file)
    if not points:
        return {"status": "error", "message": "Keine Datenpunkte in der Datei."}

    store = ActivityVectorStore()
    profile = store.get_profile()
    effective_hr_max = int(profile.get("hr_max", 205))
    effective_hr_rest = int(profile.get("hr_rest", 52))
    total_system_weight = float(profile.get("total_system_weight_kg", 72.0))
    rider_weight = float(profile.get("rider_weight_kg", 60.0))

    has_coords = bool(points and (points[0].lat != 0.0 or points[0].lon != 0.0))
    weather_info = None
    if points and points[0].time and has_coords:
        try:
            from weather_service import get_weather_for_activity
            weather_info = get_weather_for_activity(
                lat=points[0].lat,
                lon=points[0].lon,
                dt=points[0].time,
            )
        except Exception:
            pass

    metrics = calculate_metrics(
        file_path=target_file,
        title=title,
        activity_type=activity_type,
        points=points,
        hr_max=effective_hr_max,
        hr_rest=effective_hr_rest,
        total_system_weight_kg=total_system_weight,
        rider_weight_kg=rider_weight,
        weather_data=weather_info,
    )

    return {
        "status": "success",
        "activity_id": clean_id,
        "title": title,
        "energy_kcal": metrics.nutrition_metabolism.get("energy_total_kcal"),
        "metabolic_and_nutrition": metrics.nutrition_metabolism,
        "vam_and_youth_benchmark": metrics.vam_diagnostics,
        "coaching_takeaway": (
            "Ernährungshinweis: Bei intensiven Fahrten über 90 Minuten zwingend 60-90g Kohlenhydrate/h "
            "zuführen, um Leistungsabfall und Magen-Darm-Probleme zu verhindern."
        ),
    }


# ---------------------------------------------------------------------------
# Modul 8: Apple Watch & Health Export App (Vitaldaten, Schlaf, Workouts & U17-Readiness)
# ---------------------------------------------------------------------------
@mcp.tool()
def get_apple_health_readiness(force_sync: bool = False, days: int = 7, user_id: str = "default") -> dict[str, Any]:
    """
    Ruft Schlafdaten, Ruhepuls und Herzfrequenzvariabilität (HRV) von der Apple Watch ab
    (über die eigene iOS Health Export App oder Health Auto Export) und berechnet einen
    sportwissenschaftlichen Erholungs- und Leistungsbereitschafts-Score (0-100) für den Athleten.

    Funktioniert komplett passiv und blockiert nie:
    - Ist das iPhone mit aktiver App im WLAN, werden die Daten live abgeglichen.
    - Ist das Handy gesperrt/offline, wird sofort der aktuellste gespeicherte Datensatz aus SQLite geliefert.

    Args:
        force_sync: Erzwingt einen sofortigen Verbindungsversuch zum iPhone.
        days: Anzahl der zurückliegenden Tage für den Erholungsverlauf (Standard: 7).
        user_id: Nutzer-Identifikator (Standard: 'default').

    Returns:
        Detaillierter Erholungsstatus, Schlafqualität, HRV-Trend und Trainingsempfehlung.
    """
    from apple_health_service import sync_apple_health_from_ios, enrich_record_with_quality
    from rag_engine import ActivityVectorStore

    res = sync_apple_health_from_ios(force=force_sync, user_id=user_id)
    if days != 7:
        store = ActivityVectorStore()
        raw_history = store.get_apple_health_history(days=days, user_id=user_id)
        res["recent_days_history"] = [enrich_record_with_quality(h) for h in raw_history]
    return res


@mcp.tool()
def get_apple_health_vitals(force_sync: bool = False, user_id: str = "default") -> dict[str, Any]:
    """
    Ruft die neuesten Körper- und Vitalmesswerte von Apple Health ab:
    VO2max, Körpergewicht (kg), Körpergröße (m), Ruhepuls (bpm) und HRV (SDNN in ms).
    Vergleicht die Messwerte mit den hinterlegten Profil-Baselines und berechnet den BMI.

    Args:
        force_sync: Erzwingt einen sofortigen Verbindungsversuch zum iPhone.
        user_id: Nutzer-Identifikator (Standard: 'default').

    Returns:
        Neueste Vitalwerte, Baselines und Abweichungen.
    """
    from apple_health_service import sync_apple_health_from_ios, is_health_server_online
    from rag_engine import ActivityVectorStore

    if force_sync or is_health_server_online(timeout=0.35):
        sync_apple_health_from_ios(force=force_sync, user_id=user_id)

    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    latest = store.get_latest_apple_health_metrics(user_id=user_id) or {}
    raw = latest.get("raw_data") or {}

    weight_kg = latest.get("weight_kg") or raw.get("weight_kg") or profile.get("rider_weight_kg")
    vo2_max = latest.get("vo2_max") or raw.get("vo2_max") or profile.get("vo2_max")
    resting_hr = latest.get("resting_hr") or raw.get("resting_hr") or profile.get("hr_rest")
    hrv_ms = latest.get("hrv_ms") or raw.get("hrv_ms")
    height_m = raw.get("height_meters") or profile.get("height_meters", 1.77)

    bmi = None
    if weight_kg and height_m and float(height_m) > 0:
        bmi = round(float(weight_kg) / (float(height_m) ** 2), 1)

    baseline_rhr = int(profile.get("hr_rest", 59))
    baseline_hrv = float(profile.get("baseline_hrv_ms", 68.0))

    return {
        "user_id": user_id,
        "recorded_date": latest.get("recorded_date", "N/A"),
        "synced_at": latest.get("synced_at", "N/A"),
        "vitals": {
            "vo2_max": vo2_max,
            "weight_kg": weight_kg,
            "height_meters": height_m,
            "bmi": bmi,
            "resting_heart_rate_bpm": resting_hr,
            "hrv_sdnn_ms": hrv_ms,
        },
        "baselines": {
            "baseline_resting_hr_bpm": baseline_rhr,
            "baseline_hrv_ms": baseline_hrv,
            "rhr_difference": (resting_hr - baseline_rhr) if (resting_hr and baseline_rhr) else None,
            "hrv_difference": round(hrv_ms - baseline_hrv, 1) if (hrv_ms and baseline_hrv) else None,
        },
        "readiness_summary": {
            "score": latest.get("readiness_score"),
            "verdict": latest.get("readiness_verdict"),
        },
    }


@mcp.tool()
def get_apple_health_sleep_analysis(days: int = 14, force_sync: bool = False, user_id: str = "default") -> dict[str, Any]:
    """
    Analysiert die Schlafqualität und Schlafarchitektur der letzten N Nächte aus Apple Health:
    Tiefschlaf, REM-Schlaf, Kernschlaf, Wachphasen, Schlafdauer und Schlafdefizite.

    Args:
        days: Betrachtungszeitraum in Tagen (Standard: 14).
        force_sync: Erzwingt einen sofortigen Verbindungsversuch zum iPhone.
        user_id: Nutzer-Identifikator (Standard: 'default').

    Returns:
        Schlafdurchschnitte, Tiefschlafanteile und Nacht-für-Nacht-Architektur.
    """
    from apple_health_service import sync_apple_health_from_ios, is_health_server_online
    from rag_engine import ActivityVectorStore

    if force_sync or is_health_server_online(timeout=0.35):
        sync_apple_health_from_ios(force=force_sync, user_id=user_id)

    store = ActivityVectorStore()
    history = store.get_apple_health_history(days=days, user_id=user_id)

    nights = []
    total_hours_list = []
    deep_min_list = []
    rem_min_list = []

    for d in history:
        raw = d.get("raw_data") or {}
        tot_h = d.get("sleep_hours")
        deep = d.get("deep_sleep_minutes") or raw.get("deep_sleep_minutes")
        rem = d.get("rem_sleep_minutes") or raw.get("rem_sleep_minutes")
        core = raw.get("core_sleep_minutes")
        awake = raw.get("awake_minutes")

        if tot_h and tot_h > 0.4:
            total_hours_list.append(tot_h)
            if deep is not None:
                deep_min_list.append(deep)
            if rem is not None:
                rem_min_list.append(rem)

            tot_min = tot_h * 60.0
            pct_deep = round((deep / tot_min * 100), 1) if (deep and tot_min > 0) else None
            pct_rem = round((rem / tot_min * 100), 1) if (rem and tot_min > 0) else None

            nights.append({
                "date": d.get("recorded_date"),
                "total_sleep_hours": round(tot_h, 2),
                "deep_sleep_minutes": round(deep, 1) if deep is not None else None,
                "deep_sleep_percentage": pct_deep,
                "rem_sleep_minutes": round(rem, 1) if rem is not None else None,
                "rem_sleep_percentage": pct_rem,
                "core_sleep_minutes": round(core, 1) if core is not None else None,
                "awake_minutes": round(awake, 1) if awake is not None else None,
                "readiness_score": d.get("readiness_score"),
                "verdict": d.get("readiness_verdict"),
            })

    avg_sleep = round(sum(total_hours_list) / len(total_hours_list), 2) if total_hours_list else None
    avg_deep = round(sum(deep_min_list) / len(deep_min_list), 1) if deep_min_list else None
    avg_rem = round(sum(rem_min_list) / len(rem_min_list), 1) if rem_min_list else None

    sleep_verdict = "Keine Schlafdaten im ausgewählten Zeitraum."
    if avg_sleep:
        if avg_sleep >= 8.2:
            sleep_verdict = f"Hervorragendes Schlafvolumen (Schnitt: {avg_sleep}h). Maximale anabole Erholung und Regeneration gesichert."
        elif avg_sleep >= 7.2:
            sleep_verdict = f"Gute Schlafdauer (Schnitt: {avg_sleep}h). Für schwere Trainingsblöcke leichte Aufstockung auf >8h empfohlen."
        else:
            sleep_verdict = f"Schlafdefizit (Schnitt: {avg_sleep}h)! Hohes Risiko für Überlastung, verringerte Glykogenspeicher und gedämpfte HRV."

    return {
        "user_id": user_id,
        "analyzed_nights_count": len(nights),
        "days_requested": days,
        "average_sleep_hours": avg_sleep,
        "average_deep_sleep_minutes": avg_deep,
        "average_rem_sleep_minutes": avg_rem,
        "sleep_assessment": sleep_verdict,
        "nights": nights,
    }


@mcp.tool()
def get_apple_health_daily_activity(days: int = 14, force_sync: bool = False, user_id: str = "default") -> dict[str, Any]:
    """
    Ruft Alltagsaktivitäten aus Apple Health ab:
    Schritte, Aktiv- & Ruheenergie (kcal), Gehdauer/Distanz, Trainingsminuten und Treppensteigen.

    Args:
        days: Betrachtungszeitraum in Tagen (Standard: 14).
        force_sync: Erzwingt einen sofortigen Verbindungsversuch zum iPhone.
        user_id: Nutzer-Identifikator (Standard: 'default').

    Returns:
        Aktivitätsstatistiken, Tagesschnitte und Bestwerte.
    """
    from apple_health_service import sync_apple_health_from_ios, is_health_server_online
    from rag_engine import ActivityVectorStore

    if force_sync or is_health_server_online(timeout=0.35):
        sync_apple_health_from_ios(force=force_sync, user_id=user_id)

    store = ActivityVectorStore()
    history = store.get_apple_health_history(days=days, user_id=user_id)

    daily_list = []
    steps_list = []
    kcal_list = []
    dist_list = []

    for d in history:
        raw = d.get("raw_data") or {}
        st = d.get("steps") or raw.get("steps")
        kc = d.get("active_energy_kcal") or raw.get("active_energy_kcal")
        di = d.get("distance_meters") or raw.get("distance_meters")

        if st is not None:
            steps_list.append(int(st))
        if kc is not None:
            kcal_list.append(float(kc))
        if di is not None:
            dist_list.append(float(di))

        daily_list.append({
            "date": d.get("recorded_date"),
            "steps": int(st) if st is not None else None,
            "active_energy_kcal": round(float(kc), 1) if kc is not None else None,
            "distance_km": round(float(di) / 1000.0, 2) if di is not None else None,
            "exercise_minutes": raw.get("exercise_minutes"),
            "flights_climbed": raw.get("flights_climbed"),
            "workout_count": raw.get("workout_count"),
            "resting_hr": d.get("resting_hr"),
            "hrv_ms": d.get("hrv_ms"),
        })

    avg_steps = round(sum(steps_list) / len(steps_list), 0) if steps_list else None
    avg_kcal = round(sum(kcal_list) / len(kcal_list), 1) if kcal_list else None
    avg_dist_km = round(sum(dist_list) / len(dist_list) / 1000.0, 2) if dist_list else None

    return {
        "user_id": user_id,
        "days_analyzed": len(daily_list),
        "averages": {
            "avg_steps": int(avg_steps) if avg_steps else None,
            "avg_active_energy_kcal": avg_kcal,
            "avg_distance_km": avg_dist_km,
        },
        "peaks": {
            "max_steps": max(steps_list) if steps_list else None,
            "min_steps": min(steps_list) if steps_list else None,
            "max_active_kcal": max(kcal_list) if kcal_list else None,
        },
        "daily_breakdown": daily_list,
    }


@mcp.tool()
def get_apple_health_workouts(days: int = 14, force_sync: bool = False, user_id: str = "default") -> dict[str, Any]:
    """
    Ruft alle in Apple Health erfassten Workouts (Laufen, Radfahren, Krafttraining etc.) ab
    mit Sportart, Dauer, Distanz, Kalorien und Zeitstempeln.

    Args:
        days: Betrachtungszeitraum in Tagen (Standard: 14).
        force_sync: Erzwingt einen sofortigen Verbindungsversuch zum iPhone.
        user_id: Nutzer-Identifikator (Standard: 'default').

    Returns:
        Liste der erfassten Apple Health Trainingseinheiten.
    """
    from apple_health_service import sync_apple_health_from_ios, is_health_server_online
    from rag_engine import ActivityVectorStore

    if force_sync or is_health_server_online(timeout=0.35):
        sync_apple_health_from_ios(force=force_sync, user_id=user_id)

    store = ActivityVectorStore()
    workouts = store.get_apple_health_workouts(days=days, user_id=user_id)

    return {
        "user_id": user_id,
        "workouts_count": len(workouts),
        "days_requested": days,
        "workouts": workouts,
    }


@mcp.tool()
def sync_apple_health_now(user_id: str = "default") -> dict[str, Any]:
    """
    Startet einen manuellen Sofort-Sync mit der Apple Health Export App auf dem iPhone
    und liefert eine detaillierte Status- und Diagnosemeldung zurück.

    Args:
        user_id: Nutzer-Identifikator (Standard: 'default').

    Returns:
        Ergebnis des Synchronisationsversuchs mit Latenz und Anzahl der Datenpunkte.
    """
    from apple_health_service import sync_apple_health_from_ios
    return sync_apple_health_from_ios(force=True, user_id=user_id)


# ---------------------------------------------------------------------------
# Modul 9: Prädiktiver Routen-Simulator (Komoot / geplante GPX-Touren)
# ---------------------------------------------------------------------------
@mcp.tool()
def simulate_planned_route_coaching(
    gpx_file_path: str,
    planned_start_time: Optional[str] = None,
    target_intensity: str = "endurance_ga1",
) -> dict[str, Any]:
    """
    Simuliert eine geplante Tour (z. B. aus Komoot oder Strava Route Builder) VOR der Abfahrt!
    Kombiniert das Höhenprofil mit OpenStreetMap-Straßenbelägen (Rollwiderstand Crr),
    der exakten Wetter- und Windvorhersage für die Startzeit und dem Jeukendrup/Romijn-Stoffwechselmodell.

    Liefert:
    - Erwartete Fahrzeit, Durchschnittstempo und Wattwerte (Virtual Watts).
    - Taktische Warnungen vor Gegenwindabschnitten und steilen Rampen.
    - Grammgenauen Verpflegungsfahrplan (Trinkflaschen mit Elektrolyten, Riegel-Timing, Hungerast-Schutz).

    Args:
        gpx_file_path: Pfad zur geplanten GPX-Datei (z. B. 'downloads/geplante_tour.gpx' oder absolut).
        planned_start_time: Optionaler geplanter Start (ISO z. B. '2026-09-22T14:00:00' oder None für bald).
        target_intensity: Geplante Intensität: 'recovery', 'endurance_ga1' (Standard), 'tempo_ga2', 'threshold_race'.

    Returns:
        Komplette Renn- und Tourentaktik mit Verpflegungsplan und Pacing-Limits.
    """
    from route_simulator import simulate_planned_route
    from rag_engine import ActivityVectorStore

    store = ActivityVectorStore()
    profile = store.get_profile()
    rider_w = float(profile.get("rider_weight_kg", 60.0))
    system_w = float(profile.get("total_system_weight_kg", 72.9))
    h_max = int(profile.get("hr_max", 205))
    h_rest = int(profile.get("hr_rest", 52))

    return simulate_planned_route(
        gpx_file_path=gpx_file_path,
        planned_start_time=planned_start_time,
        target_intensity=target_intensity,
        rider_weight_kg=rider_w,
        total_system_weight_kg=system_w,
        hr_max=h_max,
        hr_rest=h_rest,
    )


# ---------------------------------------------------------------------------
# Modul 10: Ganzheitlicher Erholungs- & Belastungsabgleich (Recovery Loop)
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_holistic_recovery_status(user_id: str = "default") -> dict[str, Any]:
    """
    Schließt den Recovery-Loop zwischen Trainingsbelastung (CTL/ATL/TSB) und den
    Apple-Watch-Vitaldaten (Ruhepuls, HRV, Schlaf) sowie kardiovaskulärer Drift.
    Erkennt Diskrepanzen zwischen physischer Belastung und autonomer Nervensystem-Erholung
    (z. B. Sympathikus-Stress bei hoher Ermüdung und unterdrückter HRV) und liefert
    einen ganzheitlichen Erholungs-Score (0-100) mit zulässigen Trainingszonen für den heutigen Tag.

    Args:
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Ganzheitlicher Erholungsstatus, Zonenfreigabe, Korrelationen und Warnhinweise.
    """
    from training_load import compute_holistic_recovery
    return await asyncio.to_thread(compute_holistic_recovery, user_id=user_id)


# ---------------------------------------------------------------------------
# Modul 11: Longitudinalanalyse von Strava-Segmenten (Trend & Effizienz)
# ---------------------------------------------------------------------------
@mcp.tool()
async def analyze_segment_history(
    segment_name_or_id: str = "",
    segment_name: Optional[str] = None,
    segment_id: Optional[str] = None,
    max_efforts: int = 15,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Analysiert die historische Entwicklung eines bestimmten Strava-Segments über Wochen und Monate hinweg.
    Trackt Zeitverbesserungen, W/kg, Herzfrequenz und den aeroben Effizienzfaktor (EF = Watt / HF).
    Zeigt Fortschritte, Bestzeiten (PR) und Leistungssteigerungen auf.
    Wird kein Segment übergeben, wird eine Liste aller verfügbaren Segmente in der Datenbank geliefert.

    Args:
        segment_name_or_id: Name oder Teilname des Segments (z. B. "Schauinsland" oder ID).
        segment_name: Alias für segment_name_or_id.
        segment_id: Alias für numerische Segment-ID.
        max_efforts: Maximale Anzahl historischer Versuche (Standard: 15).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Strukturierte Trendanalyse mit PR-Vergleich, Leistungsentwicklung und Effizienzfaktor-Deltas.
    """
    query_str = (segment_name_or_id or segment_name or segment_id or "").strip()

    def _do():
        store = ActivityVectorStore()
        if not query_str:
            segs = store.get_available_segments(user_id=user_id)
            return {
                "status": "info",
                "message": "Kein Segment angegeben. Hier ist die Übersicht aller verfügbaren Segmente:",
                "available_segments_count": len(segs),
                "segments": segs,
            }
        return store.get_segment_history(query_str, max_efforts=max_efforts, user_id=user_id)

    return await asyncio.to_thread(_do)


@mcp.tool()
async def get_segment_progress_history(
    segment_name: str,
    limit: int = 10,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Durchsucht alle bisher über 'get_activity_segments' erfassten Segment-Daten in der lokalen
    Datenbank nach einem bestimmten Segmentnamen und gibt den chronologischen Leistungsverlauf zurück.

    Ermöglicht die Longitudinalanalyse eines Lieblingssegments über Wochen und Monate hinweg:
    - Datum der Fahrt
    - Fahrzeit (HH:MM:SS und Sekunden)
    - Durchschnittsleistung (Watt)
    - Vertikale Steiggeschwindigkeit (VAM in m/h)
    - Rang / PR-Status pro Durchfahrt
    - Chronologisch sortiert (älteste -> neueste Durchfahrt) mit Trend- und Fortschrittsberechnung.

    Args:
        segment_name: Name oder Teilname des Strava-Segments (z. B. "Dreisam", "Schauinsland").
        limit: Maximale Anzahl historischer Versuche (Standard: 10).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Strukturiertes Dictionary mit chronologischem Verlauf, PR, Watt-/Zeit-Verbesserung und Trend.
    """
    def _do():
        store = ActivityVectorStore()
        return store.get_segment_progress_history(segment_name=segment_name, limit=limit, user_id=user_id)

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Modul 12: Zielbasierte Periodisierung & CTL-Aufbaupfad (Wettkampf-Tapering)
# ---------------------------------------------------------------------------
@mcp.tool()
async def plan_periodization(
    target_date: str,
    target_event: Optional[str] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Berechnet einen zielgerichteten, sportwissenschaftlichen Periodisierungsplan bis zu einem Zieldatum.
    Nutzt das gespeicherte Athletenprofil (insbesondere das Feld 'goals' mit Marathon/Ironman-Zielen)
    sowie den aktuellen CTL-Wert aus dem Trainingsstatus, um einen progressiven CTL-Zielpfad zu berechnen.

    Rückgabe:
    - Wöchentliche CTL-Zielwerte (progressiver Zielpfad).
    - Empfohlene Ramp Rate (sicher: max. 5-7 CTL/Woche, optimal 3-5 CTL/Woche).
    - Grobe sportwissenschaftliche Phaseneinteilung (Grundlage / Aufbau / Tapering).
    - Strukturierter Wochenplan mit TSS-Vorgaben, Fokus-Beschreibungen und Anomalie-Prüfung.

    Args:
        target_date: Zieldatum des Hauptereignisses im ISO-Format (YYYY-MM-DD, z. B. "2026-10-18").
        target_event: Optionaler Typ des Events (z. B. "Marathon", "Ironman", "Gran Fondo", "Radrennen").
                      Wird keines übergeben, wird das Ziel automatisch aus dem Athletenprofil ('goals') abgeleitet.
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Strukturierter Periodisierungsplan mit wöchentlichen CTL-Zielen, Ramp Rate, Phasen und Empfehlungen.
    """
    from training_load import plan_periodization as _plan_p
    return await asyncio.to_thread(
        _plan_p,
        target_date=target_date,
        target_event=target_event,
        user_id=user_id,
    )


@mcp.tool()
async def plan_ctl_periodization(
    target_date: str,
    target_ctl: Optional[float] = None,
    event_type: str = "gran_fondo",
    current_ctl: Optional[float] = None,
    weekly_hours_budget: Optional[float] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Berechnet einen zukunftsorientierten, sportwissenschaftlichen Periodisierungsplan
    bis zu einem Wettkampf- oder Saisonhöhepunkt (z. B. Gran Fondo, Marathon, Ironman, Radrennen).
    Modelliert nach Banister/Coggan progressive Überlastungsphasen (Base, Build),
    3:1 Deload-Wochen zur physiologischen Adaptation sowie eine gezielte Tapering-Phase,
    sodass der Athlet am Wettkampftag mit maximaler Superkompensation (+10 bis +25 TSB) am Start steht.

    Args:
        target_date: Zieldatum des Wettkampfs im Format 'YYYY-MM-DD' (z. B. '2026-10-15').
        target_ctl: Optionale Ziel-Fitness (CTL). Wenn weggelassen, wird der sportspezifische Benchmark verwendet.
        event_type: Wettkampf-Kategorie ('gran_fondo', 'marathon', 'ironman', 'criterium', 'time_trial').
        current_ctl: Aktuelle Ausgangsfitness (Standard: wird automatisch aus der Datenbank berechnet).
        weekly_hours_budget: Maximales wöchentliches Zeitbudget in Stunden (prüft Machbarkeit).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Wochenweiser Trainingsplan mit Soll-TSS, Stundenbedarf, prognostiziertem CTL/ATL/TSB und Tapering-Prognose.
    """
    from training_load import plan_ctl_periodization as _plan_ctl
    return await asyncio.to_thread(
        _plan_ctl,
        target_date_str=target_date,
        target_ctl=target_ctl,
        event_type=event_type,
        current_ctl=current_ctl,
        weekly_hours_budget=weekly_hours_budget,
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Modul 13: Proaktives Alert- & Frühwarnsystem (Übertraining & Datenqualität)
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_training_alerts(
    user_id: str = "default",
    include_dismissed: bool = False,
) -> dict[str, Any]:
    """
    Liefert alle aktiven sportwissenschaftlichen Frühwarnungen und Alerts des Systems.
    Wird vom automatischen Hintergrund-Sync (alle 45 Min) zyklisch evaluiert und warnt vor:
    - Zu steiler Ramp Rate (>6.0 CTL/Woche Zuwachs = erhöhtes Sehnenüberlastungs-/Infektrisiko)
    - Akuter Überlastung (TSB < -25)
    - Autonomem Erholungsdefizit (niedrige Apple Health HRV/Readiness bei hohem Trainingsvolumen)
    - Veralteten Vitaldaten (Offline-Zustand des Health-Auto-Export-Servers auf dem iPhone)

    Args:
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").
        include_dismissed: Ob auch bereits quittierte Warnungen angezeigt werden sollen.

    Returns:
        Strukturierte Liste aller aktuellen Warnmeldungen mit Schweregrad ('critical', 'warning', 'info').
    """
    def _do():
        store = ActivityVectorStore()
        from training_load import evaluate_training_alerts
        evaluate_training_alerts(user_id=user_id)
        active = store.get_active_training_alerts(user_id=user_id, limit=20)
        return {
            "status": "success",
            "user_id": user_id,
            "alerts_count": len(active),
            "alerts": active,
        }

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Modul 14: Subjektives Athleten-Feedback & Session-RPE (Active Listening)
# ---------------------------------------------------------------------------
@mcp.tool()
def record_post_workout_checkin(
    activity_id: str,
    rpe_score: int,
    legs_feeling: int = 5,
    mental_state: Optional[str] = None,
    fueling_adherence: Optional[str] = None,
    weather_felt: Optional[str] = None,
    pain_or_discomfort: Optional[str] = None,
    athlete_notes: Optional[str] = None,
    duration_minutes: Optional[float] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Erfasst das subjektive Feedback des Athleten (Session-RPE / Foster-Modell 1-10,
    Beine-Gefühl, mentaler Zustand, Ernährungs-Treue, Schmerzen) zu einer Aktivität.
    Schließt den Dialog-Loop zwischen reiner Sensor-Telemetrie und dem echten Körpergefühl.

    Args:
        activity_id: Die Strava-Aktivitäts-ID (z. B. "20239426058").
        rpe_score: Subjektive Anstrengung auf der Borg/Foster-Skala von 1 (extrem leicht) bis 10 (maximaler Anschlag).
        legs_feeling: Frische der Beine von 1 (völlig leer/schwer wie Blei) bis 10 (super frisch/spritzig).
        mental_state: Optionaler mentaler Zustand (z. B. "gestresst von Schule/Klausur", "fokussiert", "entspannt").
        fueling_adherence: Ob die geplante Verpflegung eingehalten wurde (z. B. "1 Riegel + 500ml Iso", "nichts gegessen").
        weather_felt: Gefühltes Wetter (z. B. "starker Gegenwind", "drückende Schwüle").
        pain_or_discomfort: Etwaige Beschwerden (z. B. "leichtes Ziehen im linken Knie"). Erzeugt bei Schmerzen automatisch einen Trainer-Alert.
        athlete_notes: Freitext-Notiz des Athleten.
        duration_minutes: Optionale Dauer in Minuten (falls nicht automatisch ermittelt).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Bestätigung mit berechneter Session-RPE Belastung (sRPE = RPE * Dauer) und Archivierungs-ID.
    """
    store = ActivityVectorStore()
    res = store.record_post_workout_checkin(
        activity_id=activity_id,
        rpe_score=rpe_score,
        legs_feeling=legs_feeling,
        mental_state=mental_state,
        fueling_adherence=fueling_adherence,
        weather_felt=weather_felt,
        pain_or_discomfort=pain_or_discomfort,
        athlete_notes=athlete_notes,
        duration_minutes=duration_minutes,
        user_id=user_id,
    )
    return {
        "status": "success",
        "user_id": user_id,
        "checkin": res,
        "coaching_takeaway": (
            f"Session-RPE Belastung von {res.get('session_rpe_load')} Punkten erfasst. "
            f"RPE {rpe_score}/10 bei Beine-Score {legs_feeling}/10."
        ),
    }


@mcp.tool()
def get_post_workout_checkins(
    activity_id: Optional[str] = None,
    limit: int = 10,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Ruft historische Post-Workout-Checkins und subjektive Feedback-Berichte des Athleten ab.

    Args:
        activity_id: Optionaler Filter auf eine bestimmte Aktivität.
        limit: Maximale Anzahl (Standard: 10).
        user_id: Optionaler Bezeichner des Nutzers (Standard: "default").

    Returns:
        Liste der gespeicherten Feedback-Einträge mit RPE, Beine-Zustand und Schmerznotizen.
    """
    store = ActivityVectorStore()
    entries = store.get_post_workout_checkins(activity_id=activity_id, limit=limit, user_id=user_id)
    return {
        "status": "success",
        "user_id": user_id,
        "count": len(entries),
        "checkins": entries,
    }


# ---------------------------------------------------------------------------
# Modul 15: Strukturierter Trainingsgenerator & Zwift .ZWO Export
# ---------------------------------------------------------------------------
@mcp.tool()
def generate_structured_workout(
    workout_type: str = "sweetspot_intervals",
    target_duration_minutes: int = 60,
    intensity_level: str = "moderate",
    export_format: str = "json",
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Erstellt ein maßgeschneidertes, sportwissenschaftlich strukturiertes Intervall-Training
    für heute oder die nächsten Tage. Liefert Ziel-Watt, Ziel-Herzfrequenzen, Trittfrequenzen
    und exportiert fertiges Zwift .zwo XML für Smart-Trainer oder Radcomputer.

    Unterstützte Typen:
    - 'sweetspot_intervals': 88-93% FTP Schwellen-Booster (3x 10m)
    - 'vo2max_power': 4x 4 Min Spitzenbelastung zur VO2max-Steigerung
    - 'over_under_threshold': Laktat-Toleranz und Schwellenadaptation
    - 'endurance_ga1': Grundlagenausdauer mit Trittfrequenz-Pyramide
    - 'active_recovery': Regeneratives Kurbeln (Zone 1)

    Args:
        workout_type: Typ der Einheit (Standard: 'sweetspot_intervals').
        target_duration_minutes: Geplante Dauer in Minuten (30 bis 180).
        intensity_level: Gewünschte Härte ('light', 'moderate', 'hard').
        export_format: 'json' oder 'zwo' (Zwift XML).
        user_id: Optionaler Bezeichner des Nutzers.

    Returns:
        Strukturierter Trainingsablauf, Intervalle und Zwift .zwo Code.
    """
    from workouts import generate_structured_workout as _gen_w
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    hr_max = int(profile.get("hr_max", 205))
    ftp = float(profile.get("ftp_watts", 230.0))

    return _gen_w(
        workout_type=workout_type,
        target_duration_minutes=target_duration_minutes,
        intensity_level=intensity_level,
        ftp_watts=ftp,
        hr_max=hr_max,
        export_format=export_format,
    )


# ---------------------------------------------------------------------------
# Modul 16: Live-Glykogen-Reichweite (Bonk-Countdown) & Hydration Engine
# ---------------------------------------------------------------------------
@mcp.tool()
def calculate_live_bonk_and_fueling_horizon(
    current_duration_minutes: float,
    current_distance_km: float,
    avg_hr: Optional[float] = None,
    carbs_consumed_grams: float = 0.0,
    target_distance_km: Optional[float] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Berechnet die dynamische Glykogen-Restreichweite (den 'Live-Bonk-Countdown')
    wie bei einem Elektroauto. Zeigt an, wie viele Minuten und Kilometer noch
    bis zum totalen Hungerast verbleiben und wann Kohlenhydrate (Gels/Riegel) nachgetankt werden müssen.

    Args:
        current_duration_minutes: Bisherige Fahrzeit in Minuten.
        current_distance_km: Bisher zurückgelegte Distanz in km.
        avg_hr: Durchschnittlicher oder aktueller Puls.
        carbs_consumed_grams: Bisher während der Fahrt verzehrte Kohlenhydrate in Gramm (z. B. 40.0).
        target_distance_km: Geplante Gesamtdistanz der Tour in km (zur Machbarkeitsprüfung).
        user_id: Optionaler Bezeichner des Nutzers.

    Returns:
        Rest-Glykogen in %, Minuten und km bis zum Hungerast sowie konkrete Verpflegungsanweisungen.
    """
    from sports_science import calculate_live_bonk_and_fueling_horizon as _calc_bonk
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    rider_w = float(profile.get("rider_weight_kg", 60.0))
    hr_max = int(profile.get("hr_max", 205))

    return _calc_bonk(
        current_duration_minutes=current_duration_minutes,
        current_distance_km=current_distance_km,
        avg_hr=avg_hr,
        carbs_consumed_grams=carbs_consumed_grams,
        target_distance_km=target_distance_km,
        rider_weight_kg=rider_w,
        hr_max=hr_max,
    )


@mcp.tool()
def calculate_dynamic_hydration_alert(
    temperature_c: float,
    duration_minutes: float,
    avg_hr: Optional[float] = None,
    fluid_consumed_ml: float = 0.0,
    humidity_pct: float = 50.0,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Berechnet den realen Schweiß- und Natriumverlust sowie das physiologische Flüssigkeitsdefizit
    in % des Körpergewichts. Warnt vor Leistungseinbrüchen und kardiovaskulärem Drift durch Dehydrierung.

    Args:
        temperature_c: Aktuelle Asphalttemperatur / Umgebungstemperatur in °C.
        duration_minutes: Bisherige Fahrtdauer in Minuten.
        avg_hr: Durchschnittspuls.
        fluid_consumed_ml: Bisher getrunkene Flüssigkeit in Millilitern.
        humidity_pct: Relative Luftfeuchtigkeit in % (Standard: 50.0).
        user_id: Optionaler Bezeichner des Nutzers.

    Returns:
        Schweißrate L/h, Flüssigkeitsdefizit %, Warnstufe und exakte Trinkempfehlung mit Natriumangabe.
    """
    from sports_science import calculate_dynamic_sweat_and_dehydration as _calc_dehy
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    rider_w = float(profile.get("rider_weight_kg", 60.0))
    hr_max = int(profile.get("hr_max", 205))

    return _calc_dehy(
        temperature_c=temperature_c,
        duration_minutes=duration_minutes,
        avg_hr=avg_hr,
        fluid_consumed_ml=fluid_consumed_ml,
        humidity_pct=humidity_pct,
        rider_weight_kg=rider_w,
        hr_max=hr_max,
    )


# ---------------------------------------------------------------------------
# Modul 17: Wind-bereinigter Virtual Ghost & Schotter-Impedanz (Silca Modell)
# ---------------------------------------------------------------------------
@mcp.tool()
def predict_wind_adjusted_segment(
    segment_name: str,
    segment_distance_m: float,
    current_speed_kmh: float,
    baseline_pr_seconds: float,
    segment_bearing_deg: float = 0.0,
    wind_speed_kmh: float = 0.0,
    wind_direction_deg: float = 0.0,
    cda: float = 0.32,
    air_density_kg_m3: float = 1.225,
) -> dict[str, Any]:
    """
    Berechnet die wahre wind-bereinigte Leistung auf einem Segment (Virtual Ghost).
    Zeigt dem Athleten live an, ob er physisch vor oder hinter seiner Bestzeit liegt,
    bereinigt um Gegenwind, Rückenwind und Luftdichte.

    Args:
        segment_name: Name des Segments (z. B. "Schauinsland Anstieg").
        segment_distance_m: Länge des Segments in Metern.
        current_speed_kmh: Aktuelle Geschwindigkeit in km/h.
        baseline_pr_seconds: Bisherige Bestzeit auf dem Segment in Sekunden.
        segment_bearing_deg: Kompassrichtung des Segments in Grad (0 = Nord, 90 = Ost).
        wind_speed_kmh: Windgeschwindigkeit in km/h.
        wind_direction_deg: Windrichtung in Grad.
        cda: Luftwiderstandsbeiwert in m² (Standard: 0.32 für Gravelbike/Rennrad).
        air_density_kg_m3: Lokale Luftdichte in kg/m³ (Standard: 1.225).

    Returns:
        Windkomponente, aerodynamisches Watt-Delta, wind-bereinigte Zeit und Virtual Ghost Urteil.
    """
    from sports_science import predict_wind_adjusted_segment as _pred_seg
    return _pred_seg(
        segment_name=segment_name,
        segment_distance_m=segment_distance_m,
        current_speed_kmh=current_speed_kmh,
        baseline_pr_seconds=baseline_pr_seconds,
        segment_bearing_deg=segment_bearing_deg,
        wind_speed_kmh=wind_speed_kmh,
        wind_direction_deg=wind_direction_deg,
        cda=cda,
        air_density_kg_m3=air_density_kg_m3,
    )


@mcp.tool()
def analyze_gravel_vibration_and_tire_pressure(
    surface_type: str = "gravel",
    tire_width_mm: float = 40.0,
    current_pressure_bar: Optional[float] = None,
    vibration_g_peak: Optional[float] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Analysiert Untergrundrauhigkeit, Schottervibrationen und Reifendruck nach dem Silca-Impedanzmodell.
    Ermittelt den optimalen Reifendruck für das Trek Checkpoint Systemgewicht (72.9 kg)
    und beziffert Watt-Verluste durch springende Reifen.

    Args:
        surface_type: Untergrund ('asphalt', 'fine_gravel', 'gravel', 'cobblestones').
        tire_width_mm: Reifenbreite in mm (Standard: 40.0 mm).
        current_pressure_bar: Aktuell gefahrener Reifendruck in Bar (z. B. 2.8).
        vibration_g_peak: Gemessene Spitzenbeschleunigung der IMU in g (z. B. 3.2).
        user_id: Optionaler Bezeichner des Nutzers.

    Returns:
        Optimaler Reifendruck, Watt-Impedanzverlust und Schotter-Grip-Sicherheitsindex (1-10).
    """
    from sports_science import analyze_gravel_vibration_and_tire_pressure as _calc_tire
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    total_w = float(profile.get("total_system_weight_kg", 72.9))

    return _calc_tire(
        surface_type=surface_type,
        tire_width_mm=tire_width_mm,
        current_pressure_bar=current_pressure_bar,
        vibration_g_peak=vibration_g_peak,
        total_system_weight_kg=total_w,
    )


# ---------------------------------------------------------------------------
# Modul 18: Live-AI-Coach Whisper & Autonomer Blackbox-Upload
# ---------------------------------------------------------------------------
@mcp.tool()
def analyze_live_telemetry_chunk(
    speed_kmh: float,
    hr: Optional[int] = None,
    slope_pct: float = 0.0,
    duration_min: float = 0.0,
    wind_headwind_kmh: float = 0.0,
    temperature_c: float = 20.0,
    vibration_level: str = "normal",
    current_situation: Optional[str] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Generiert einen ultra-prägnanten, motivierenden Live-Coach-Whisper (12-18 Wörter)
    für den Fahrradcomputer / Garmin-Bildschirm während der Fahrt. Reagiert live auf Gegenwind,
    Steigungsprozente, Überhitzung, Schottervibrationen und Pacing.

    Args:
        speed_kmh: Aktuelle Geschwindigkeit in km/h.
        hr: Aktuelle Herzfrequenz in bpm.
        slope_pct: Aktuelle Steigung in % (positiv = Bergauf, negativ = Abfahrt).
        duration_min: Bisherige Fahrzeit in Minuten.
        wind_headwind_kmh: Effektive Gegenwindkomponente in km/h (negativ = Rückenwind).
        temperature_c: Aktuelle Temperatur in °C.
        vibration_level: Vibrationslevel der IMU ('smooth', 'normal', 'rough', 'high_chatter').
        current_situation: Optionale Notiz (z. B. 'Ortsausgang', 'Finaler Anstieg').
        user_id: Optionaler Bezeichner des Nutzers.

    Returns:
        Live-Coach-Whisper für das Display und Telemetrie-Snapshot.
    """
    from sports_science import generate_live_coach_whisper as _whisper
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    hr_max = int(profile.get("hr_max", 205))

    return _whisper(
        speed_kmh=speed_kmh,
        hr=hr,
        hr_max=hr_max,
        slope_pct=slope_pct,
        duration_min=duration_min,
        wind_headwind_kmh=wind_headwind_kmh,
        temperature_c=temperature_c,
        vibration_level=vibration_level,
        current_situation=current_situation,
        rider_name="Henrik",
    )


@mcp.tool()
async def ingest_blackbox_ride_log(
    track_points: list[dict[str, Any]],
    ride_name: Optional[str] = None,
    min_distance_km: float = 0.25,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Autonomer Upload-Endpunkt für die DIY-Sattelstützen-Blackbox (ESP32).
    Filtert Testfahrten und Schaltungs-Checks im Hof (<150m Radius) automatisch heraus.
    Erzeugt bei echten Touren eine GPX-Datei im Ordner 'downloads/' und synchronisiert
    sie sofort in den RAG-Vektorstore.

    Args:
        track_points: Liste von GPS-Punkten [{lat, lon, ele, time, hr}, ...].
        ride_name: Optionaler Name der Fahrt.
        min_distance_km: Mindestdistanz in km (Standard: 0.25).
        user_id: Optionaler Bezeichner des Nutzers.

    Returns:
        Statusmeldung mit Dateiname oder Begründung für Verwerfung (Hofrunde).
    """
    if not track_points or len(track_points) < 5:
        return {"status": "discarded", "reason": "Zu wenige Messpunkte (<5)."}

    first_pt = track_points[0]
    lat0, lon0 = float(first_pt.get("lat", 0.0)), float(first_pt.get("lon", 0.0))
    max_dist_from_start_m = 0.0

    for pt in track_points:
        lat = float(pt.get("lat", 0.0))
        lon = float(pt.get("lon", 0.0))
        if lat != 0.0 and lon != 0.0 and lat0 != 0.0 and lon0 != 0.0:
            d = haversine_distance_meters(lat0, lon0, lat, lon)
            if d > max_dist_from_start_m:
                max_dist_from_start_m = d

    if max_dist_from_start_m < 150.0:
        return {
            "status": "discarded",
            "reason": f"Testfahrt im Hof erkannt (Maximaler Radius nur {max_dist_from_start_m:.1f}m vom Startpunkt). Daten verworfen.",
        }

    import html as _html
    name = ride_name or f"Blackbox_Fahrt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    clean_slug = re.sub(r"[^\w\-]", "_", name).strip("_")
    filename = f"blackbox_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{clean_slug}.gpx"
    out_path = DOWNLOADS_DIR / filename

    gpx_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Strava-MCP-Blackbox" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        '  <trk>',
        f'    <name>{_html.escape(name)}</name>',
        '    <type>GravelRide</type>',
        '    <trkseg>',
    ]

    for pt in track_points:
        lat = pt.get("lat", 0.0)
        lon = pt.get("lon", 0.0)
        ele = pt.get("ele", 0.0)
        t_iso = pt.get("time") or datetime.now(timezone.utc).isoformat()
        hr = pt.get("hr")

        pt_str = f'      <trkpt lat="{lat}" lon="{lon}">'
        if ele is not None:
            pt_str += f'<ele>{ele}</ele>'
        pt_str += f'<time>{t_iso}</time>'
        if hr is not None:
            pt_str += f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{hr}</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>'
        pt_str += '</trkpt>'
        gpx_lines.append(pt_str)

    gpx_lines.append('    </trkseg>')
    gpx_lines.append('  </trk>')
    gpx_lines.append('</gpx>')

    try:
        out_path.write_text("\n".join(gpx_lines), encoding="utf-8")
        await asyncio.to_thread(sync_local_gpx_to_vector_store)
    except Exception as e:
        return {"status": "error", "message": f"Konnte GPX nicht speichern: {e}"}

    return {
        "status": "saved",
        "file": str(out_path.name),
        "points_recorded": len(track_points),
        "max_distance_from_start_m": round(max_dist_from_start_m, 1),
        "message": "Fahrt erfolgreich als GPX gespeichert und in RAG-Vektorindex synchronisiert.",
    }


@mcp.tool()
async def get_council_coaching_verdict(
    activity_id: Optional[str] = None,
    include_health: bool = True,
    question_or_topic: Optional[str] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Ruft das heterogene Multi-Agenten-Trainer-Council (Triangulation aus 3 Spitzen-Modellen via OpenRouter) ab:
    1. deepseek/deepseek-v4.1-flash: Gnadenloser mathematisch-physikalischer Datenprüfer (Watt, VAM, Pacing, Decoupling).
    2. z-ai/glm-5.3-flash: BDR U17 Regenerations- & Gesundheits-Auditor (Schlaf, HRV, Überlastungsschutz).
    3. openai/gpt-6-luna: Leitender Chef-Coach & Schiedsrichter (Synthese, Konsensbildung, Schmeichel-Filter & finale Trainingsdirektive).

    Args:
        activity_id: Optionale ID einer bestimmten Aktivität (Standard: neueste analysierte Aktivität).
        include_health: Ob aktuelle Apple Health Vitalwerte (HRV, Schlaf, Ruhepuls) einbezogen werden.
        question_or_topic: Konkrete Frage oder geplante nächste Einheit des Athleten.
        user_id: Benutzer-ID (Standard: 'default').

    Returns:
        Vollständiger Triangulationsbericht mit Einzelurteilen von DeepSeek & GLM sowie der finalen GPT-6 Luna Direktive.
    """
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)

    # 1. Belastungsdaten laden
    try:
        engine = load_historical_training_data()
        t_status = engine.get_training_status()
        load_data = t_status.get("metrics", {})
    except Exception:
        load_data = {}

    # 2. Health-Daten laden
    health_data = {}
    if include_health:
        health_data = store.get_latest_apple_health_metrics(user_id=user_id) or {}

    # 3. Aktivitätsdaten laden
    act_data = {}
    downloads = get_user_downloads_dir(user_id)
    target_file = None
    if activity_id:
        target_file = find_local_activity_file(downloads, activity_id)
    else:
        all_files = get_all_local_activity_files(downloads)
        if all_files:
            target_file = all_files[0]

    if target_file and target_file.suffix.lower() == ".gpx":
        try:
            t, a_type, pts = parse_gpx_file(target_file)
            if pts:
                m = calculate_metrics(target_file, t, a_type, pts, rider_weight_kg=profile.get("rider_weight_kg", 58.0))
                act_data = {
                    "title": t,
                    "distance_km": round(m.total_distance_km, 2),
                    "elevation_gain_m": round(m.elevation_gain_m, 1),
                    "moving_time_min": round(m.moving_time_sec / 60.0, 1),
                    "avg_speed_kmh": round(m.avg_speed_moving_kmh, 1),
                    "avg_hr": m.avg_hr,
                    "max_hr": m.max_hr,
                }
                # Decoupling berechnen
                dec = calculate_aerobic_decoupling(pts)
                if dec.get("status") == "success":
                    act_data["decoupling_pct"] = dec.get("decoupling_pct")
                    act_data["decoupling_verdict"] = dec.get("classification", {}).get("verdict")
        except Exception as e:
            logger.debug("Konnte Aktivität nicht für Council parsen: %s", e)

    # Council Review asynchron im Worker-Thread ausführen
    return await asyncio.to_thread(
        run_council_review,
        activity_metrics=act_data,
        training_load=load_data,
        health_metrics=health_data,
        athlete_profile=profile,
        user_context_or_question=question_or_topic,
    )


@mcp.tool()
def estimate_vo2max(
    watts: float,
    rider_weight_kg: Optional[float] = None,
    age: Optional[int] = None,
    duration_seconds: Optional[float] = None,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Berechnet die geschätzte maximale Sauerstoffaufnahme (VO2max in ml/kg/min)
    aus mechanischen Watt-Leistungen an Anstiegen oder Rampen-Tests nach den drei
    etablierten sportwissenschaftlichen Formeln (ACSM, Storer et al., Hawley & Noakes)
    inklusive Einstufung nach BDR U17-Kaderstandards.

    Args:
        watts: Gemessene oder berechnete Durchschnittsleistung am Berg / Test (in Watt).
        rider_weight_kg: Optionales Fahrergewicht in kg (Standard: aus Athletenprofil).
        age: Optionales Alter des Athleten (Standard: aus Athletenprofil, z.B. 15 Jahre).
        duration_seconds: Optionale Belastungsdauer in Sekunden.
        user_id: Benutzer-ID (Standard: 'default').

    Returns:
        Detaillierte VO2max-Aufschlüsselung mit Konsens-Wert und BDR-Benchmark.
    """
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    weight = rider_weight_kg if rider_weight_kg is not None else float(profile.get("rider_weight_kg", 58.0))
    athletic_age = age if age is not None else int(profile.get("age", 15))

    return estimate_vo2max_from_power(
        watts=watts,
        rider_weight_kg=weight,
        age=athletic_age,
        duration_s=duration_seconds,
    )


@mcp.tool()
async def calculate_activity_decoupling(
    activity_id: str,
    user_id: str = "default",
) -> dict[str, Any]:
    """
    Berechnet die aerobe Entkopplung (Aerobic Decoupling / Cardiac Drift nach Joe Friel)
    für eine Ausfahrt.

    Vergleicht den Efficiency Factor (Leistung oder Tempo im Verhältnis zur Herzfrequenz)
    der ersten Fahrthälfte mit der zweiten Fahrthälfte:
    - Drift < 3%: Exzellente aerobe Basis (vollkommen stabil)
    - Drift 3-5%: Solide aerobe Ausdauer (physiologischer Normalbereich)
    - Drift > 5%: Warnung bezüglich Dehydrierung, unzureichender Verpflegung oder Übermüdung.

    Args:
        activity_id: Die ID der zu analysierenden Strava-Aktivität.
        user_id: Benutzer-ID (Standard: 'default').

    Returns:
        Entkopplungsprozentsatz, Effizienz-Werte beider Hälften und Trainingsbewertung.
    """
    downloads = get_user_downloads_dir(user_id)
    target_file = find_local_activity_file(downloads, activity_id)
    if not target_file or not target_file.exists():
        return {
            "status": "error",
            "message": f"Keine lokale Datei für Aktivitäts-ID '{activity_id}' gefunden.",
        }

    if target_file.suffix.lower() != ".gpx":
        return {
            "status": "error",
            "message": "Entkopplungsanalyse erfordert eine GPX-Datei mit Zeit- und Herzfrequenzstempeln.",
        }

    try:
        _, _, pts = await asyncio.to_thread(parse_gpx_file, target_file)
        if not pts:
            return {"status": "error", "message": "GPX-Datei enthält keine Trackpunkte."}
        return calculate_aerobic_decoupling(pts)
    except Exception as e:
        return {"status": "error", "message": f"Fehler bei der Berechnung der Entkopplung: {e}"}


def create_starlette_app(token: Optional[str] = MCP_AUTH_TOKEN) -> ASGIApp:

    """
    Erstellt die vollständige Starlette-Anwendung mit Streamable HTTP, Legacy SSE,
    Health Check, Public Status und Operations-Dashboard.
    """
    http_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()

    streamable_handler = None
    for r in http_app.routes:
        if getattr(r, "path", None) == "/mcp":
            streamable_handler = r.endpoint

    webpage_dir = BASE_DIR / "webpage"

    routes = [
        Route("/", root_status, methods=["GET", "HEAD"]),
        Route("/health", health_check, methods=["GET", "HEAD"]),
        Route("/dashboard", dashboard_endpoint, methods=["GET", "HEAD"]),
        Route("/dashboard/", dashboard_endpoint, methods=["GET", "HEAD"]),
        Route("/api/dashboard-stats", dashboard_stats_api, methods=["GET", "HEAD"]),
        Route("/api/dashboard-stats/", dashboard_stats_api, methods=["GET", "HEAD"]),
        Route("/api/live-coach", route_live_coach, methods=["GET", "POST", "HEAD"]),
        Route("/api/blackbox-upload", route_blackbox_upload, methods=["POST"]),
    ]
    if webpage_dir.exists():
        routes.append(Mount("/app", StaticFiles(directory=str(webpage_dir), html=True)))

    if streamable_handler:
        routes.append(Route("/", streamable_handler, methods=["POST"]))
        routes.append(Route("/mcp", streamable_handler, methods=["GET", "POST"]))

    for r in sse_app.routes:
        p = getattr(r, "path", None)
        if p not in [
            "/",
            "/health",
            "/mcp",
            "/dashboard",
            "/dashboard/",
            "/api/dashboard-stats",
            "/api/dashboard-stats/",
            "/api/live-coach",
            "/api/blackbox-upload",
            "/app",
        ]:
            routes.append(r)


    combined_app = Starlette(
        debug=mcp.settings.debug,
        routes=routes,
        lifespan=lambda app: mcp.session_manager.run(),
    )
    combined_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS", "HEAD"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    if token:
        return TokenAuthMiddleware(combined_app, token=token)
    return combined_app


# Modul-Level Instanz für direkten Uvicorn-Start (uvicorn mcp_server:starlette_app)
starlette_app = create_starlette_app(MCP_AUTH_TOKEN)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Strava GPX Sync MCP Server (SSE Transport)")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host-Adresse für den Server (Standard: 0.0.0.0 für lokales Netzwerk)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port für den SSE-Transport (Standard: 8000)",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Deaktiviert das Vorwärmen des Playwright-Browsers beim Serverstart",
    )
    parser.add_argument(
        "--enable-bg-sync",
        action="store_true",
        default=True,
        help="Aktiviert den proaktiven Hintergrund-Sync (Standard: aktiv)",
    )
    parser.add_argument(
        "--bg-sync-interval",
        type=int,
        default=45,
        help="Basisintervall für Hintergrund-Sync in Minuten (Standard: 45)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=MCP_AUTH_TOKEN,
        help="Sicherheitstoken für die Authentifizierung von Remote-Anfragen (Standard: aus config.py)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Deaktiviert die Token-Authentifizierung (nur für lokale Offline-Tests!)",
    )
    args = parser.parse_args()

    # Logging-Filter für Uvicorn Access-Logs aktivieren
    setup_uvicorn_logging_filter()

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    active_token = None if args.no_auth else (args.token or MCP_AUTH_TOKEN)

    logger.info("=" * 70)
    logger.info("🚀 Strava GPX Sync MCP-Server (Streamable HTTP & SSE Transport)")
    logger.info("  -> Streamable HTTP URL:  http://127.0.0.1:%d/mcp  (oder http://127.0.0.1:%d/)", args.port, args.port)
    logger.info("  -> Legacy SSE URL:       http://127.0.0.1:%d/sse", args.port)
    logger.info("  -> Health-Check:         http://127.0.0.1:%d/health", args.port)
    if active_token:
        logger.info("  -> Authentifizierung:   AKTIV (Token-Schutz)")
        logger.info("  -> Dein MCP-Token:       %s", active_token)
        logger.info("  -> Claude.ai Streamable HTTP:")
        logger.info("     Transport:            Streamable HTTP (unter 'Erweitert' in Claude)")
        logger.info("     URL:                  https://<domain>/mcp  (oder https://<domain>/)")
        logger.info("     Header:               Authorization: Bearer %s", active_token)
        logger.info("     URL-Token:            https://<domain>/mcp?token=%s", active_token)
    else:
        logger.info("  -> Authentifizierung:   DEAKTIVIERT (--no-auth)")
    logger.info("=" * 70)

    # Optional: Browser im Hintergrund vorwärmen für schnellere erste Antwortzeiten
    if not args.no_warmup and AUTH_FILE.exists():
        try:
            logger.info("Wärme Playwright-Browser vor...")
            BrowserManager.get_instance().ensure_initialized()
            logger.info("Playwright-Browser einsatzbereit.")
        except Exception as e:
            logger.warning("Browser-Vorwärmung übersprungen: %s", e)

    # Vektor-Store mit lokalen Downloads vorwärmen
    try:
        store = ActivityVectorStore()
        if store.count() == 0:
            logger.info("Indexiere lokale GPX-Dateien in den Vektor-Store...")
            sync_local_gpx_to_vector_store()
            logger.info("Vektor-Store einsatzbereit (%d Aktivitäten).", store.count())
        else:
            logger.info("Vektor-Store geladen (%d Aktivitäten im Index).", store.count())
    except Exception as e:
        logger.warning("Vektor-Store Initialisierung übersprungen: %s", e)

    # Hintergrund-Synchronisationsdienst starten
    bg_service: Optional[BackgroundSyncService] = None
    if args.enable_bg_sync and AUTH_FILE.exists():
        try:
            bg_service = BackgroundSyncService.get_instance(interval_minutes=args.bg_sync_interval)
            bg_service.start()
        except Exception as e:
            logger.warning("Konnte Background-Sync nicht starten: %s", e)

    # Apple Health Auto-Scanner starten (erkennt iPhone Health Export App automatisch im Hintergrund)
    health_scanner = None
    try:
        from config import HEALTH_EXPORT_SCAN_INTERVAL
        from apple_health_service import AppleHealthAutoScanner
        health_scanner = AppleHealthAutoScanner.get_instance(check_interval_seconds=HEALTH_EXPORT_SCAN_INTERVAL)
        health_scanner.start()
    except Exception as e:
        logger.warning("Konnte Apple Health Auto-Scanner nicht starten: %s", e)

    try:
        final_app = starlette_app if (active_token == MCP_AUTH_TOKEN) else create_starlette_app(active_token)

        config = uvicorn.Config(
            final_app,
            host=args.host,
            port=args.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        anyio.run(server.serve)
    finally:
        if health_scanner:
            health_scanner.stop()
        if bg_service:
            bg_service.stop()
        BrowserManager.get_instance().close()
