"""
Zentrale Konfiguration und Hilfsfunktionen für das Strava GPX Sync Projekt.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Playwright,
)

# Verzeichnispfade
BASE_DIR: Path = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

AUTH_FILE: Path = BASE_DIR / "auth.json"
PROFILE_DIR: Path = BASE_DIR / "browser_profile"
DOWNLOADS_DIR: Path = BASE_DIR / "downloads"


def get_user_downloads_dir(user_id: str = "default") -> Path:
    """Gibt das nutzerspezifische Download-Verzeichnis für GPX-Dateien zurück."""
    if user_id == "default":
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        return DOWNLOADS_DIR
    clean_user = re.sub(r"[^\w\-]", "_", str(user_id).strip())
    user_dir = DOWNLOADS_DIR / clean_user
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_user_auth_file(user_id: str = "default") -> Path:
    """Gibt den Pfad zur nutzerspezifischen auth.json (Session-State) zurück."""
    if user_id == "default":
        return AUTH_FILE
    clean_user = re.sub(r"[^\w\-]", "_", str(user_id).strip())
    return BASE_DIR / f"auth_{clean_user}.json"

# OpenRouter API Konfiguration für Multi-Agent Triangulation
OPENROUTER_API_KEY: str = os.getenv(
    "OPENROUTER_API_KEY",
    "",
)

# MCP Server Remote-Authentifizierung (für Cloudflare Tunnel & Claude.ai Web Custom Connector)
# Kann flexibel über Umgebungsvariable MCP_AUTH_TOKEN gesetzt oder überschrieben werden.
MCP_AUTH_TOKEN: str = os.getenv(
    "MCP_AUTH_TOKEN",
    "",
)

# OpenWeatherMap API Konfiguration
OPENWEATHER_API_KEY: str = os.getenv(
    "OPENWEATHER_API_KEY",
    "",
)


# Apple Health Auto Export (Lokaler MCP Server auf iOS - Fallback)
HEALTH_AUTO_EXPORT_URL: str = os.getenv("HEALTH_AUTO_EXPORT_URL", "")
HEALTH_AUTO_EXPORT_TOKEN: str = os.getenv(
    "HEALTH_AUTO_EXPORT_TOKEN",
    "",
)

# Eigene Apple Health Export App (Lokale REST-API auf iOS)
HEALTH_EXPORT_APP_URL: str = os.getenv("HEALTH_EXPORT_APP_URL", "")
HEALTH_EXPORT_SCAN_INTERVAL: int = int(os.getenv("HEALTH_EXPORT_SCAN_INTERVAL", "5"))

# Strava URLs
STRAVA_BASE_URL: str = "https://www.strava.com"
LOGIN_URL: str = f"{STRAVA_BASE_URL}/login"
TRAINING_URL: str = f"{STRAVA_BASE_URL}/athlete/training"
DASHBOARD_URL: str = f"{STRAVA_BASE_URL}/dashboard"

# Browser Anti-Bot & Cloudflare-Schutz-Einstellungen
BROWSER_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]

# Realistischer, stabiler Chrome User-Agent für Windows
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

PREFERRED_CHANNEL: str = "chrome"

# JavaScript-Snippet zur Maskierung von navigator.webdriver (Schutz vor Cloudflare & Bot-Detection)
STEALTH_INIT_SCRIPT: str = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});
"""


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Richtet ein einheitliches Konsolen-Logging mit Zeitstempel und UTF-8-Support ein."""
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return logging.getLogger("strava_sync")


def launch_browser(
    playwright: Playwright,
    headless: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Browser:
    """
    Startet eine Browser-Instanz (bevorzugt Google Chrome, Fallback auf Chromium).
    Entfernt Standard-Automatisierungs-Flags zur Umgehung von Bot-Erkennungen.
    """
    log = logger or logging.getLogger("strava_sync")
    launch_kwargs = {
        "headless": headless,
        "args": BROWSER_ARGS,
        "ignore_default_args": ["--enable-automation"],
    }

    try:
        log.debug("Starte Browser mit channel='%s' (headless=%s)...", PREFERRED_CHANNEL, headless)
        return playwright.chromium.launch(channel=PREFERRED_CHANNEL, **launch_kwargs)
    except PlaywrightError as err:
        log.warning(
            "Kanal '%s' nicht verfügbar (%s). Wechsle auf Standard-Chromium...",
            PREFERRED_CHANNEL,
            err,
        )
        return playwright.chromium.launch(**launch_kwargs)


def create_browser_context(
    browser: Browser,
    storage_state_path: Optional[Path] = AUTH_FILE,
    logger: Optional[logging.Logger] = None,
) -> BrowserContext:
    """
    Erstellt einen neuen Browser-Kontext.
    Lädt 'auth.json', falls vorhanden, und setzt Anti-Bot-Header (User-Agent, Viewport, Stealth).
    """
    log = logger or logging.getLogger("strava_sync")
    context_kwargs = {
        "user_agent": DEFAULT_USER_AGENT,
        "viewport": {"width": 1920, "height": 1080},
        "accept_downloads": True,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
    }

    if storage_state_path and storage_state_path.exists():
        log.info("Lade persistente Session aus '%s'...", storage_state_path.name)
        context_kwargs["storage_state"] = str(storage_state_path)
    else:
        log.warning("Keine Session-Datei gefunden unter '%s'.", storage_state_path)

    context = browser.new_context(**context_kwargs)
    # Navigator-Webdriver-Flag neutralisieren
    context.add_init_script(STEALTH_INIT_SCRIPT)
    return context


def get_persistent_auth_context(
    playwright: Playwright,
    headless: bool = False,
    profile_dir: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> BrowserContext:
    """
    Startet einen persistenten Browser-Kontext für setup_auth.py.
    Wichtig für Google OAuth, da Google Anmeldungen in reinen Automationsprofilen oft blockiert.
    """
    log = logger or logging.getLogger("strava_sync")
    target_profile = profile_dir or PROFILE_DIR
    target_profile.mkdir(parents=True, exist_ok=True)

    launch_kwargs = {
        "user_data_dir": str(target_profile),
        "headless": headless,
        "args": BROWSER_ARGS,
        "ignore_default_args": ["--enable-automation"],
        "user_agent": DEFAULT_USER_AGENT,
        "viewport": {"width": 1920, "height": 1080},
        "accept_downloads": True,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
    }

    try:
        log.debug("Starte persistenten Kontext mit channel='%s'...", PREFERRED_CHANNEL)
        context = playwright.chromium.launch_persistent_context(
            channel=PREFERRED_CHANNEL,
            **launch_kwargs,
        )
    except PlaywrightError as err:
        log.warning(
            "Kanal '%s' nicht verfügbar (%s). Versuche Standard-Chromium...",
            PREFERRED_CHANNEL,
            err,
        )
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)

    context.add_init_script(STEALTH_INIT_SCRIPT)
    return context
