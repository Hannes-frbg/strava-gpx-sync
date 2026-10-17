"""
setup_auth.py: Manuelle Erst-Authentifizierung bei Strava via Playwright.

Öffnet ein sichtbares Google Chrome Browser-Fenster mit Anti-Bot-Flags,
sodass ein Login (insbesondere über Google OAuth) manuell durchgeführt werden kann.
Nach erfolgreichem Erreichen des Dashboards wird der gesamte Browser-Zustand
(Cookies, LocalStorage) explizit in die Datei 'auth.json' exportiert.
"""

from __future__ import annotations

import re
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import (
    AUTH_FILE,
    LOGIN_URL,
    PROFILE_DIR,
    get_persistent_auth_context,
    setup_logging,
)

# Regex zur Erkennung des erfolgreichen Logins (Dashboard, Feed oder Athleten-Startseite)
DASHBOARD_REGEX = re.compile(r"https?://(www\.)?strava\.com/(dashboard|athlete|feed).*")
AUTH_TIMEOUT_MS = 120_000  # 120 Sekunden Timeout für die manuelle Anmeldung


def main() -> int:
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("Strava Authentifizierungs-Setup")
    logger.info("=" * 60)
    logger.info("Ziel-Sessiondatei: %s", AUTH_FILE)
    logger.info("Profil-Verzeichnis: %s", PROFILE_DIR)

    with sync_playwright() as p:
        try:
            logger.info("Starte Browser im sichtbaren Modus (headless=False) mit Bot-Schutz...")
            context = get_persistent_auth_context(p, headless=False, logger=logger)
        except Exception as e:
            logger.error("Kritischer Fehler beim Starten des Browsers: %s", e)
            return 1

        try:
            page = context.pages[0] if context.pages else context.new_page()

            logger.info("Navigiere zu Strava Login: %s", LOGIN_URL)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")

            logger.info("-" * 60)
            logger.info("AKTION ERFORDERLICH:")
            logger.info("Bitte melden Sie sich im geöffneten Browserfenster bei Strava an")
            logger.info("(z. B. über 'Mit Google anmelden').")
            logger.info("Warte auf Erreichen des Dashboards (Timeout: %d Sekunden)...", AUTH_TIMEOUT_MS // 1000)
            logger.info("-" * 60)

            # Warten, bis nach dem Login das Strava Dashboard oder der Feed geladen wird
            page.wait_for_url(DASHBOARD_REGEX, timeout=AUTH_TIMEOUT_MS)

            logger.info("Erfolgreicher Login erkannt! Aktuelle URL: %s", page.url)

            # Kurze Pause für vollständiges Setzen aller Session-Cookies & LocalStorage-Daten
            logger.info("Synchronisiere Browser-Speicher...")
            page.wait_for_timeout(3000)

            # Expliziter Export des gesamten Zustands (Cookies + LocalStorage) in auth.json
            logger.info("Exportiere Session-Daten in '%s' (storage_state)...", AUTH_FILE.name)
            context.storage_state(path=str(AUTH_FILE))

            if AUTH_FILE.exists() and AUTH_FILE.stat().st_size > 0:
                logger.info(
                    "Session erfolgreich in '%s' gespeichert! (Dateigröße: %d Bytes)",
                    AUTH_FILE.name,
                    AUTH_FILE.stat().st_size,
                )
                logger.info("-" * 60)
                logger.info("Authentifizierung abgeschlossen!")
                logger.info("Sie können nun den GPX-Sync ausführen mit:")
                logger.info("    python sync_gpx.py")
                logger.info("-" * 60)
                return 0
            else:
                logger.error("Fehler: 'auth.json' konnte nicht erstellt werden oder ist leer.")
                return 1

        except PlaywrightTimeoutError:
            logger.error(
                "Zeitüberschreitung: Das Dashboard wurde innerhalb von %d Sekunden nicht erreicht.",
                AUTH_TIMEOUT_MS // 1000,
            )
            logger.error("Der Login wurde nicht rechtzeitig abgeschlossen oder abgebrochen.")
            return 1

        except Exception as e:
            logger.error("Unerwarteter Fehler während der Authentifizierung: %s", e, exc_info=True)
            return 1

        finally:
            logger.info("Schließe Browser-Kontext sauber...")
            try:
                context.close()
                logger.info("Browser geschlossen.")
            except Exception as close_err:
                logger.debug("Hinweis beim Schließen: %s", close_err)


if __name__ == "__main__":
    sys.exit(main())
