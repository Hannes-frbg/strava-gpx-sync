"""
sync_gpx.py: Automatisierte Synchronisation von Strava-GPX-Dateien ohne offizielle API.

Lädt den persistenten Session-Zustand direkt aus 'auth.json' (storage_state) in den
Browser-Kontext, prüft die Session auf Gültigkeit (inkl. toleranter Fehlerbehandlung
im Headed-Modus), extrahiert die neueste Aktivität und lädt die GPX-Datei herunter.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from config import (
    AUTH_FILE,
    DOWNLOADS_DIR,
    STRAVA_BASE_URL,
    TRAINING_URL,
    create_browser_context,
    launch_browser,
    setup_logging,
)

RE_LOGIN_REDIRECT = re.compile(r"https?://(www\.)?strava\.com/login")
RE_LOGGED_IN_URL = re.compile(r"https?://(www\.)?strava\.com/(dashboard|athlete|feed).*")


def extract_activity_ids_from_page(page: Page, limit: int = 1) -> list[str]:
    """
    Extrahiert Aktivitäts-IDs von der aktuellen Strava-Trainingsseite.
    Unterstützt Paginierung: Falls das geforderte Limit größer als die Einträge der ersten
    Seite ist, wird über die Weiterschalt-Schaltfläche geblättert, bis genügend IDs gesammelt wurden.
    """
    found_ids: list[str] = []

    while True:
        # 1. Priorität: Suche nach <a>-Tags im DOM der aktuellen Seite
        try:
            activity_links = page.locator("tbody tr.training-activity-row a[href*='/activities/'], a[href*='/activities/']").all()
            for link in activity_links:
                href = link.get_attribute("href")
                if href:
                    match = re.search(r"/activities/(\d+)", href)
                    if match and match.group(1) not in found_ids:
                        found_ids.append(match.group(1))
                        if len(found_ids) >= limit:
                            break
        except Exception:
            pass

        if len(found_ids) >= limit:
            break

        # Prüfen, ob eine nächste Seite existiert
        next_btn = page.locator("button.next_page, a.next_page").first
        if next_btn.count() > 0 and next_btn.is_visible() and not next_btn.get_attribute("disabled"):
            next_btn.click()
            page.wait_for_timeout(2000)
        else:
            break

    # 2. Priorität: Fallback per Regex über den gesamten HTML-Seiteninhalt
    if not found_ids:
        page_html = page.content()
        matches = re.findall(r"/activities/(\d+)", page_html)
        for act_id in matches:
            if act_id not in found_ids:
                found_ids.append(act_id)
                if len(found_ids) >= limit:
                    break

    return found_ids


def handle_expired_session(
    page: Page,
    context: BrowserContext,
    headed: bool,
    logger,
) -> bool:
    """
    Behandelt abgelaufene Sessions tolerant:
    - Im Headed-Modus: Wartet bis zu 60s auf manuellen Login durch den Nutzer und aktualisiert auth.json.
    - Im Headless-Modus: Gibt verständliche Hilfestellung aus, ohne hart abzustürzen.
    """
    logger.warning("Session-Check: Weiterleitung auf /login erkannt.")

    if headed:
        logger.info("=" * 60)
        logger.info("HINWEIS (Headed-Modus aktiv):")
        logger.info("Das Browserfenster ist geöffnet. Sie können sich jetzt direkt anmelden.")
        logger.info("Warte bis zu 60 Sekunden auf erfolgreichen Login...")
        logger.info("=" * 60)

        try:
            page.wait_for_url(RE_LOGGED_IN_URL, timeout=60_000)
            logger.info("Login während der Laufzeit erkannt! Aktualisiere '%s'...", AUTH_FILE.name)
            page.wait_for_timeout(2000)

            # Neue Session direkt in auth.json sichern
            context.storage_state(path=str(AUTH_FILE))
            logger.info("Aktualisierte Session in '%s' gespeichert.", AUTH_FILE.name)

            # Trainingsseite erneut aufrufen
            logger.info("Navigiere erneut zur Trainingsliste: %s", TRAINING_URL)
            page.goto(TRAINING_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            if "/login" not in page.url:
                logger.info("Wiederanmeldung erfolgreich. Fahre mit Synchronisation fort.")
                return True
            else:
                logger.error("Weiterhin auf Login-Seite. Abbruch.")
                return False

        except PlaywrightTimeoutError:
            logger.error("Zeitüberschreitung: Es wurde innerhalb von 60 Sekunden kein Login durchgeführt.")
            return False
    else:
        logger.error("=" * 60)
        logger.error("SESSION ABGELAUFEN ODER UNGÜLTIG:")
        logger.error("Strava hat den Abruf auf die Login-Seite umgeleitet (%s).", page.url)
        logger.error("")
        logger.error("Mögliche Lösungen:")
        logger.error("  1. Führen Sie 'python setup_auth.py' aus, um eine frische 'auth.json' zu erzeugen.")
        logger.error("  2. Starten Sie mit sichtbarem Fenster: 'python sync_gpx.py --headed', um sich direkt anzumelden.")
        logger.error("=" * 60)
        return False


def sanitize_filename(name: str, max_length: int = 50) -> str:
    """
    Bereinigt einen Aktivitätsnamen für Dateisysteme (entfernt Sonderzeichen, ersetzt Leerzeichen).
    """
    # Ungültige Windows/Linux-Dateinamen-Zeichen entfernen: \ / : * ? " < > |
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    cleaned = cleaned.strip("._")
    return cleaned[:max_length] if cleaned else "Aktivitaet"


def download_activity_gpx(
    context: BrowserContext,
    activity_id: str,
    downloads_dir: Path,
    logger,
) -> Optional[Path]:
    """
    Lädt die GPX-Datei für eine Aktivität herunter und speichert sie primär
    unter dem bereinigten Namen der Tour (<activity_id>_<name>.gpx).
    """
    export_url = f"{STRAVA_BASE_URL}/activities/{activity_id}/export_gpx"
    logger.info("Starte API-Request-Download für Aktivität %s...", activity_id)

    try:
        response = context.request.get(export_url)
        is_valid_gpx = False
        tour_name: Optional[str] = None
        act_timestamp_dt: Optional[datetime.datetime] = None

        resp_url = getattr(response, "url", "")
        is_dash = isinstance(resp_url, str) and resp_url.endswith("/dashboard")
        if response.status == 200 and not is_dash:
            body = response.body()
            # Schnellprüfung: GPX muss valides XML sein und darf kein HTML sein
            if not (body.startswith(b"<!DOCTYPE") or b"<html" in body[:200] or b"<HTML" in body[:200]):
                try:
                    root = ET.fromstring(body)
                    if root.tag.endswith("gpx"):
                        is_valid_gpx = True
                        for elem in root.iter():
                            tag = elem.tag.split("}")[-1]
                            if tag == "name" and elem.text and not tour_name:
                                tour_name = elem.text.strip()
                            elif tag == "time" and elem.text and not act_timestamp_dt:
                                try:
                                    raw_t = elem.text.strip()
                                    if raw_t.endswith("Z"):
                                        act_timestamp_dt = datetime.datetime.fromisoformat(raw_t.replace("Z", "+00:00"))
                                    else:
                                        act_timestamp_dt = datetime.datetime.fromisoformat(raw_t)
                                except Exception:
                                    pass
                except Exception as parse_err:
                    logger.debug("Konnte GPX nicht parsen: %s", parse_err)

        if is_valid_gpx:
            if tour_name:
                safe_name = sanitize_filename(tour_name)
                filename = f"{activity_id}_{safe_name}.gpx"
                logger.info("Aktivitätsname erkannt: '%s' -> Dateiname: '%s'", tour_name, filename)
            else:
                filename = f"{activity_id}.gpx"

            target_file = downloads_dir / filename
            target_file.write_bytes(body)

            if act_timestamp_dt is not None:
                try:
                    ts_epoch = act_timestamp_dt.timestamp()
                    os.utime(target_file, (ts_epoch, ts_epoch))
                    logger.debug("Dateizeitstempel für %s auf %s synchronisiert.", target_file.name, act_timestamp_dt.isoformat())
                except Exception as utime_err:
                    logger.debug("Konnte Dateizeitstempel nicht synchronisieren: %s", utime_err)

            logger.info(
                "GPX-Datei erfolgreich gespeichert: %s (%d Bytes)",
                target_file.name,
                target_file.stat().st_size,
            )
            return target_file

        # Fallback 1: Strava TCX Export (/activities/{activity_id}/export_tcx)
        # TCX unterstützt Indoor-Läufe, Laufbänder und Workouts ohne GPS mit voller Herzfrequenz & Distanz
        tcx_url = f"{STRAVA_BASE_URL}/activities/{activity_id}/export_tcx"
        logger.info(
            "GPX-Export nicht verfügbar (Status %s oder kein GPX-XML). Versuche TCX-Export für %s...",
            response.status,
            activity_id,
        )
        try:
            tcx_resp = context.request.get(tcx_url)
            tcx_resp_url = getattr(tcx_resp, "url", "")
            is_tcx_dash = isinstance(tcx_resp_url, str) and tcx_resp_url.endswith("/dashboard")
            if tcx_resp.status == 200 and not is_tcx_dash:
                tcx_body = tcx_resp.body()
                if not (tcx_body.startswith(b"<!DOCTYPE") or b"<html" in tcx_body[:200] or b"<HTML" in tcx_body[:200]):
                    root = ET.fromstring(tcx_body)
                    if "TrainingCenterDatabase" in root.tag:
                        for elem in root.iter():
                            tag = elem.tag.split("}")[-1]
                            if tag == "Notes" and elem.text and not tour_name:
                                tour_name = elem.text.strip()
                            elif tag == "Time" and elem.text and not act_timestamp_dt:
                                raw_t = elem.text.strip()
                                act_timestamp_dt = datetime.datetime.fromisoformat(raw_t.replace("Z", "+00:00"))

                        safe_name = sanitize_filename(tour_name or "Indoor_Aktivitaet")
                        filename = f"{activity_id}_{safe_name}.tcx"
                        target_file = downloads_dir / filename
                        target_file.write_bytes(tcx_body)

                        if act_timestamp_dt is not None:
                            try:
                                ts_epoch = act_timestamp_dt.timestamp()
                                os.utime(target_file, (ts_epoch, ts_epoch))
                            except Exception:
                                pass

                        logger.info(
                            "TCX-Datei (Indoor) erfolgreich gespeichert: %s (%d Bytes)",
                            target_file.name,
                            target_file.stat().st_size,
                        )
                        return target_file
        except Exception as tcx_err:
            logger.debug("TCX-Download fehlgeschlagen: %s", tcx_err)

        # Fallback 2: Strava Web Streams API (/activities/{activity_id}/streams)
        streams_url = (
            f"{STRAVA_BASE_URL}/activities/{activity_id}/streams?"
            "stream_types[]=time&stream_types[]=heartrate&stream_types[]=cadence&"
            "stream_types[]=watts&stream_types[]=distance&stream_types[]=moving&stream_types[]=velocity_smooth"
        )
        logger.info("TCX nicht verfügbar. Versuche Strava Web-Streams API für %s...", activity_id)
        try:
            streams_resp = context.request.get(streams_url)
            if streams_resp.status == 200:
                streams_json = streams_resp.json()
                streams_data = {}
                if isinstance(streams_json, list):
                    for item in streams_json:
                        if isinstance(item, dict) and "type" in item and "data" in item:
                            streams_data[item["type"]] = item["data"]
                elif isinstance(streams_json, dict):
                    streams_data = streams_json

                if streams_data:
                    act_title = "Indoor_Aktivitaet"
                    act_type = "Run"
                    try:
                        act_page_resp = context.request.get(f"{STRAVA_BASE_URL}/activities/{activity_id}")
                        if act_page_resp.status == 200:
                            page_html = act_page_resp.text()
                            m = re.search(r"<title>(.*?)\s*\|\s*(.*?)\s*\|\s*Strava</title>", page_html)
                            if m:
                                act_title = m.group(1).strip()
                                act_type = m.group(2).strip()
                    except Exception:
                        pass

                    safe_name = sanitize_filename(act_title)
                    filename = f"{activity_id}_{safe_name}.json"
                    target_file = downloads_dir / filename
                    payload = {
                        "activity_id": str(activity_id),
                        "title": act_title,
                        "sport_type": act_type,
                        "streams": streams_data,
                    }
                    target_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    logger.info("Aktivitäts-Streams als JSON gespeichert: %s", target_file.name)
                    return target_file
        except Exception as str_err:
            logger.debug("Streams-Abruf fehlgeschlagen: %s", str_err)

        logger.error("Download fehlgeschlagen. Weder GPX, TCX noch Streams verfügbar für %s.", activity_id)
        return None
    except Exception as err:
        logger.error("Fehler beim Herunterladen der Aktivität %s: %s", activity_id, err)
        return None


def get_activity_segments(
    context: BrowserContext,
    activity_id: str,
    logger=None,
) -> dict[str, Any]:
    """
    Ruft die Detailseite einer Strava-Aktivität (https://www.strava.com/activities/{activity_id})
    im Headless-Modus auf und extrahiert alle berechneten Segmente.

    Args:
        context: Playwright BrowserContext mit gültigem Session-Zustand (auth.json).
        activity_id: Numerische Aktivitäts-ID (z. B. '20239426058').
        logger: Optionaler Logger für Diagnosemeldungen.

    Returns:
        Dictionary mit Status, Aktivitäts-Titel, Segment-Anzahl und Liste aller Segmente.
    """
    clean_id = re.sub(r"\D", "", str(activity_id).strip())
    if not clean_id:
        return {
            "status": "error",
            "message": f"Ungültige Aktivitäts-ID übergeben: '{activity_id}'",
        }

    activity_url = f"{STRAVA_BASE_URL}/activities/{clean_id}"
    if logger:
        logger.info("Rufe Aktivitätsseite für Segmente auf: %s", activity_url)

    page = context.new_page()
    try:
        page.goto(activity_url, wait_until="domcontentloaded")

        # Session-Check
        if "/login" in page.url:
            if logger:
                logger.warning("Session-Check fehlgeschlagen: Weiterleitung auf /login.")
            return {
                "status": "error",
                "message": (
                    "Strava-Session ist abgelaufen oder ungültig (Weiterleitung auf /login). "
                    "Bitte führen Sie 'python setup_auth.py' aus, um sich neu anzumelden."
                ),
            }

        # Warte kurz auf dynamisch nachgeladene Inhalte
        page.wait_for_timeout(2000)

        # Aktivitäts-Titel auslesen
        title = "Unbekannt"
        try:
            h1 = page.locator("h1").first
            if h1.count() > 0:
                title = h1.inner_text().strip()
            elif page.title():
                title = page.title().split("|")[0].strip()
        except Exception:
            pass

        # Auf Segment-Tabelle warten (mit Fallback bei Aktivitäten ohne Segmente)
        has_segments_table = False
        try:
            page.wait_for_selector(
                "table.segments, .segments-list, [data-tracking-element='segment']",
                timeout=8000,
            )
            has_segments_table = True
        except PlaywrightTimeoutError:
            if logger:
                logger.info("Keine Segment-Tabelle auf der Aktivitätsseite gefunden (evtl. keine Segmente auf dieser Route).")

        segments: list[dict[str, Any]] = []

        # 1. Methode: Extrahieren aus Strava Client-Datenmodell (window.pageView._segmentEfforts)
        try:
            js_data = page.evaluate("""() => {
                if (window.pageView && window.pageView._segmentEfforts && window.pageView._segmentEfforts.models) {
                    const stripTags = (s) => s ? s.replace(/<[^>]+>/g, '').trim() : null;
                    return window.pageView._segmentEfforts.models.map(m => {
                        const a = m.attributes || {};
                        return {
                            id: a.id ? String(a.id) : null,
                            segment_id: a.segment_id ? String(a.segment_id) : null,
                            name: a.name || a.display_name || null,
                            distance: stripTags(a.distance),
                            elapsed_time: a.elapsed_time || null,
                            moving_time: a.moving_time || null,
                            elevation_gain: stripTags(a.elev_gain || a.elev_difference),
                            avg_grade: stripTags(a.avg_grade),
                            avg_speed: stripTags(a.avg_speed),
                            avg_power: stripTags(a.avg_watts),
                            vam: a.vam ? String(a.vam) : null,
                            avg_hr: a.avg_hr ? `${a.avg_hr} bpm` : null,
                            max_hr: a.max_hr ? `${a.max_hr} bpm` : null,
                            achievement: a.achievement_description || null,
                            rank: a.effort_rank || null,
                            climb_category: (a.climb_category_desc && a.climb_category_desc !== 'NC') ? a.climb_category_desc : null,
                            starred: Boolean(a.starred_by_current_athlete),
                            url: a.segment_id ? `https://www.strava.com/segments/${a.segment_id}` : null
                        };
                    });
                }
                return null;
            }""")
            if js_data:
                segments = js_data
        except Exception as js_err:
            if logger:
                logger.debug("Client-Model Segment-Abruf nicht erfolgreich (%s), versuche DOM-Fallback.", js_err)

        # 2. Methode: DOM-Scraping Fallback falls Client-Modell nicht verfügbar
        if not segments and has_segments_table:
            try:
                table = page.locator("table.segments")
                if table.count() > 0:
                    rows = table.locator("tbody tr").all()
                    for r in rows:
                        effort_id = r.get_attribute("data-segment-effort-id") or r.get_attribute("id")
                        name_el = r.locator(".name")
                        name = name_el.inner_text().strip() if name_el.count() > 0 else "Unbekannt"

                        dist_span = r.locator('.stats span[title*="Distanz"]')
                        distance = dist_span.inner_text().strip() if dist_span.count() > 0 else None

                        elev_span = r.locator('.stats span[title*="Höhe"]')
                        elevation = elev_span.inner_text().strip() if elev_span.count() > 0 else None

                        grade_span = r.locator('.stats span[title*="Steigung"]')
                        grade = grade_span.inner_text().strip() if grade_span.count() > 0 else None

                        ach_el = r.locator(".achievement-col div")
                        achievement = ach_el.get_attribute("title") if ach_el.count() > 0 else None

                        climb_el = r.locator(".climb-cat-col")
                        climb = climb_el.inner_text().strip() if climb_el.count() > 0 else None

                        time_el = r.locator(".time-col")
                        time_val = time_el.inner_text().strip() if time_el.count() > 0 else None

                        tds = r.locator("td").all()
                        speed = tds[6].inner_text().strip() if len(tds) > 6 else None
                        power = tds[7].inner_text().strip() if len(tds) > 7 and tds[7].inner_text().strip() else None
                        vam = tds[8].inner_text().strip() if len(tds) > 8 and tds[8].inner_text().strip() else None
                        hr = tds[9].inner_text().strip() if len(tds) > 9 and tds[9].inner_text().strip() else None

                        segments.append({
                            "id": effort_id,
                            "segment_id": None,
                            "name": name,
                            "distance": distance,
                            "elapsed_time": time_val,
                            "moving_time": time_val,
                            "elevation_gain": elevation,
                            "avg_grade": grade,
                            "avg_speed": speed,
                            "avg_power": power,
                            "vam": vam,
                            "avg_hr": hr,
                            "max_hr": None,
                            "achievement": achievement,
                            "rank": None,
                            "climb_category": climb if climb else None,
                            "starred": False,
                            "url": None,
                        })
            except Exception as dom_err:
                if logger:
                    logger.warning("Fehler beim DOM-Scraping der Segmente: %s", dom_err)

        if logger:
            logger.info("Segmente für Aktivität %s ('%s') erfolgreich geladen: %d Segmente.", clean_id, title, len(segments))

        return {
            "status": "success",
            "activity_id": clean_id,
            "title": title,
            "count": len(segments),
            "segments": segments,
        }

    finally:
        try:
            page.close()
        except Exception:
            pass


def format_segments_table(segments_data: dict[str, Any]) -> str:
    """
    Formatiert die ausgelesenen Segmentdaten als übersichtliche Terminal-Tabelle.
    """
    if segments_data.get("status") != "success":
        return f"Fehler: {segments_data.get('message', 'Unbekannter Fehler')}"

    activity_id = segments_data.get("activity_id", "")
    title = segments_data.get("title", "Aktivität")
    count = segments_data.get("count", 0)
    segments = segments_data.get("segments", [])

    if count == 0:
        return (
            f"\n{'=' * 85}\n"
            f"Segmente für Aktivität {activity_id}: '{title}'\n"
            f"{'=' * 85}\n"
            f"Keine Segmente für diese Aktivität gefunden.\n"
            f"{'=' * 85}\n"
        )

    lines = []
    lines.append(f"\n{'=' * 95}")
    lines.append(f"Segmente für Aktivität {activity_id}: '{title}' ({count} Segmente gefunden)")
    lines.append(f"{'=' * 95}")
    lines.append(
        f"{'#':<3} {'Segment-Name':<32} {'Distanz':<9} {'Steigung':<9} {'Zeit':<7} {'Geschw.':<11} {'Leistung':<9} {'HF':<9} {'Auszeichnung'}"
    )
    lines.append("-" * 95)

    for i, seg in enumerate(segments, start=1):
        name = seg.get("name") or "Unbenannt"
        climb = seg.get("climb_category")
        if climb:
            name_display = f"{name} [Cat {climb}]"
        else:
            name_display = name

        if len(name_display) > 31:
            name_display = name_display[:29] + ".."

        dist = seg.get("distance") or "-"
        grade = seg.get("avg_grade") or "-"
        time_str = seg.get("moving_time") or seg.get("elapsed_time") or "-"
        speed = seg.get("avg_speed") or "-"
        power = seg.get("avg_power") or "-"
        hr = seg.get("avg_hr") or "-"

        ach_parts = []
        if seg.get("starred"):
            ach_parts.append("★")
        if seg.get("achievement"):
            ach_parts.append(str(seg.get("achievement")))
        if seg.get("rank"):
            ach_parts.append(f"(#{seg.get('rank')})")
        achievement = " ".join(ach_parts) if ach_parts else "-"

        lines.append(
            f"{i:<3} {name_display:<32} {dist:<9} {grade:<9} {time_str:<7} {speed:<11} {power:<9} {hr:<9} {achievement}"
        )

    lines.append("=" * 95)
    return "\n".join(lines)


def sync_activities(headed: bool = False, sync_limit: int = 1) -> int:
    """
    Hauptfunktion der Synchronisation.
    """
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("Strava GPX Synchronisation gestartet")
    logger.info("=" * 60)

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Zielverzeichnis: %s", DOWNLOADS_DIR)

    # Vorprüfung auf auth.json
    if not AUTH_FILE.exists():
        if not headed:
            logger.error("=" * 60)
            logger.error("KEINE AUTH-DATEI GEFUNDEN:")
            logger.error("Die Datei '%s' existiert noch nicht.", AUTH_FILE.name)
            logger.error("Bitte führen Sie zuerst das Setup-Skript aus:")
            logger.error("    python setup_auth.py")
            logger.error("=" * 60)
            return 1
        else:
            logger.info("Keine '%s' gefunden, da jedoch '--headed' aktiv ist, können Sie sich im Browser anmelden.", AUTH_FILE.name)

    with sync_playwright() as p:
        try:
            logger.info("Starte Browser (headless=%s)...", not headed)
            browser = launch_browser(p, headless=not headed, logger=logger)
        except Exception as e:
            logger.error("Kritischer Fehler beim Starten des Browsers: %s", e)
            return 1

        try:
            # Browser-Kontext mit storage_state aus auth.json initialisieren
            context = create_browser_context(browser, storage_state_path=AUTH_FILE, logger=logger)
            page = context.new_page()

            # 1. Navigiere zu Trainingsliste
            logger.info("Navigiere zu Trainingsliste: %s", TRAINING_URL)
            page.goto(TRAINING_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # Cloudflare-Schutzprüfung abwarten, falls eine Challenge aktiv ist
            page_title = page.title().lower()
            if "just a moment" in page_title or "cloudflare" in page_title:
                logger.info("Cloudflare-Prüfung erkannt, warte auf Weiterleitung...")
                page.wait_for_timeout(5000)

            # 2. Session-Check
            current_url = page.url
            if "/login" in current_url:
                session_recovered = handle_expired_session(page, context, headed, logger)
                if not session_recovered:
                    return 1

            logger.info("Session ist aktiv (aktuelle URL: %s).", page.url)

            # 3. Neueste Aktivitäts-IDs extrahieren (inkl. automatischer Paginierung falls sync_limit > 20)
            activity_ids = extract_activity_ids_from_page(page, limit=sync_limit)
            if not activity_ids:
                logger.warning("Keine Aktivitäten gefunden. Ist die Trainingsliste leer?")
                return 0

            logger.info("Gefundene Aktivitäten auf der Seite: %d", len(activity_ids))

            ids_to_check = activity_ids[:sync_limit]
            for index, activity_id in enumerate(ids_to_check, start=1):
                logger.info("-" * 40)
                logger.info("[%d/%d] Prüfe Aktivität ID: %s", index, len(ids_to_check), activity_id)

                # Prüfen, ob bereits eine Datei für diese Aktivität existiert (<id>.gpx oder <id>_<name>.gpx)
                existing_files = list(DOWNLOADS_DIR.glob(f"{activity_id}*.gpx"))
                if existing_files:
                    logger.info("Aktivität bereits vorhanden: '%s'. Überspringe Download.", existing_files[0].name)
                    if sync_limit == 1:
                        logger.info("Neueste Aktivität ist bereits aktuell. Synchronisation beendet.")
                    continue

                # Download durchführen und unter bereinigtem Tour-Namen abspeichern
                download_activity_gpx(context, activity_id, DOWNLOADS_DIR, logger)

            # Session-Cookies nach erfolgreichem Lauf auffrischen
            try:
                context.storage_state(path=str(AUTH_FILE))
                logger.debug("Session-Daten in '%s' aktualisiert.", AUTH_FILE.name)
            except Exception:
                pass

            logger.info("=" * 60)
            logger.info("Synchronisation erfolgreich abgeschlossen.")
            logger.info("=" * 60)
            return 0

        finally:
            try:
                browser.close()
            except Exception:
                pass


def show_activity_segments_cli(
    activity_id: str,
    as_json: bool = False,
    headed: bool = False,
) -> int:
    """
    CLI-Hilfsfunktion zum Auslesen und Anzeigen von Segmenten im Terminal oder als JSON.
    """
    logger = setup_logging()
    if not AUTH_FILE.exists():
        logger.error("Keine '%s' gefunden. Bitte führen Sie zuerst 'python setup_auth.py' aus.", AUTH_FILE.name)
        return 1

    with sync_playwright() as p:
        browser = launch_browser(p, headless=not headed, logger=logger)
        try:
            context = create_browser_context(browser, storage_state_path=AUTH_FILE, logger=logger)
            data = get_activity_segments(context, activity_id, logger=logger)
            if as_json:
                import json
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print(format_segments_table(data))
            return 0 if data.get("status") == "success" else 1
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Strava GPX Synchronisation und Segment-Analyse")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Startet sichtbaren Browser (standardmäßig headless)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Anzahl der neuesten Aktivitäten, die geprüft werden sollen (Standard: 1)",
    )
    parser.add_argument(
        "--segments",
        type=str,
        default=None,
        metavar="ACTIVITY_ID",
        help="Liest alle berechneten Segmente zu einer Aktivitäts-ID live aus Strava aus",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Gibt Segment-Ergebnisse als unformatiertes JSON aus",
    )
    args = parser.parse_args()

    if args.segments:
        return show_activity_segments_cli(args.segments, as_json=args.json, headed=args.headed)

    return sync_activities(headed=args.headed, sync_limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())

