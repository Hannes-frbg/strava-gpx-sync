"""
background_sync.py: Event-driven automatisierter Hintergrund-Sync für Strava.

Überwacht im Hintergrund in schonenden Intervallen (z. B. alle 30-60 Minuten mit Jitter)
neue Aktivitäten auf Strava, lädt GPX-Dateien herunter, extrahiert Segmente
und hält den lokalen Vektor-Store und die Trainingsbelastungsmetriken aktuell.

Schutzmechanismen:
- Anti-Bot-Schutz: Zufälliger Jitter (+/- 3-5 Minuten), menschliche Delays zwischen Downloads (2-4s).
- Session-Safety: Vorab-Check von auth.json, exponentielles Backoff bei temporären Fehlern.
- Thread-Sicherheit: Isoliertes Thread-Modell, das sich nahtlos in FastMCP einbettet.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import AUTH_FILE, DOWNLOADS_DIR

logger = logging.getLogger("strava_mcp.sync")


class BackgroundSyncService:
    """
    Singleton-Dienst für proaktive Hintergrund-Synchronisation.
    Läuft in einem dedizierten Worker-Thread mit eigenem Sleep/Jitter-Zyklus.
    """

    _instance: Optional[BackgroundSyncService] = None
    _lock = threading.Lock()

    def __init__(self, interval_minutes: int = 45) -> None:
        self.interval_seconds = max(900, interval_minutes * 60)  # Min. 15 Minuten
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._sync_lock = threading.Lock()

        # Status Tracking
        self.last_sync_time: Optional[datetime] = None
        self.last_sync_result: Dict[str, Any] = {
            "status": "never_run",
            "new_activities_synced": 0,
            "errors": [],
        }
        self.is_syncing: bool = False
        self.consecutive_errors: int = 0

    @classmethod
    def get_instance(cls, interval_minutes: int = 45) -> BackgroundSyncService:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(interval_minutes=interval_minutes)
            return cls._instance

    def start(self) -> None:
        """Startet den Hintergrund-Worker-Thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker_thread_main,
                name="StravaBackgroundSyncThread",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "Background-Sync-Service gestartet (Basisintervall: %d Minuten).",
                self.interval_seconds // 60,
            )

    def stop(self) -> None:
        """Stoppt den Hintergrund-Worker-Thread unterbrechungsfrei."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)
            logger.info("Background-Sync-Service gestoppt.")

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Schläft für `seconds`, bricht jedoch sofort ab, falls stop() aufgerufen wird."""
        step = 1.0
        elapsed = 0.0
        while self._running and not self._stop_event.is_set() and elapsed < seconds:
            time.sleep(min(step, seconds - elapsed))
            elapsed += step
        return self._running and not self._stop_event.is_set()

    def _worker_thread_main(self) -> None:
        """Arbeitsschleife im Hintergrund-Thread."""
        # Initiale Wartezeit (30s) nach Serverstart
        if not self._sleep_interruptible(30.0):
            return

        while self._running and not self._stop_event.is_set():
            try:
                logger.info("[Background-Sync] Starte zyklische Prüfung auf neue Aktivitäten...")
                self.run_sync_blocking(force=False)
            except Exception as e:
                self.consecutive_errors += 1
                logger.error("[Background-Sync] Unerwarteter Fehler im Sync-Zyklus: %s", e)

            # Jitter berechnen (+/- 180s), um starre Polling-Muster zu vermeiden
            jitter = random.randint(-180, 180)
            sleep_duration = max(600, self.interval_seconds + jitter)

            # Exponentielles Backoff bei Fehlern
            if self.consecutive_errors > 0:
                sleep_duration = min(7200, sleep_duration * min(3, self.consecutive_errors))
                logger.warning(
                    "[Background-Sync] Backoff aktiv (%d Fehler). Nächste Prüfung in ca. %d Min.",
                    self.consecutive_errors,
                    sleep_duration // 60,
                )

            logger.info(
                "[Background-Sync] Nächster geplanter Sync in ca. %.1f Minuten.",
                sleep_duration / 60.0,
            )

            if not self._sleep_interruptible(sleep_duration):
                break

    def run_sync_blocking(self, force: bool = False) -> Dict[str, Any]:
        """
        Führt den Synchronisationslauf synchron aus.
        Wird im Hintergrund-Thread oder via run_sync_async über asyncio.to_thread ausgeführt.
        """
        if not self._sync_lock.acquire(blocking=False):
            logger.info("[Background-Sync] Sync läuft bereits. Anfrage übersprungen.")
            return {
                "status": "in_progress",
                "message": "Ein Synchronisationslauf ist bereits aktiv.",
            }

        try:
            self.is_syncing = True
            sync_start = datetime.now(timezone.utc)
            new_synced_ids: List[str] = []
            errors: List[str] = []

            # 1. Auth-Check
            if not AUTH_FILE.exists():
                msg = f"Keine auth.json vorhanden unter {AUTH_FILE.name}. Sync übersprungen."
                logger.warning("[Background-Sync] %s", msg)
                self.last_sync_result = {
                    "status": "skipped",
                    "reason": msg,
                    "timestamp": sync_start.isoformat(),
                }
                return self.last_sync_result

            # 2. Module laden
            from mcp_server import BrowserManager, _task_list_strava_activities
            from sync_gpx import download_activity_gpx, get_activity_segments
            from rag_engine import ActivityVectorStore
            from analyze_gpx import calculate_metrics, parse_gpx_file

            bm = BrowserManager.get_instance()
            res = bm.run(_task_list_strava_activities, limit=15, page_num=1)

            if res.get("status") != "success":
                err_msg = res.get("message", "Konnte Aktivitäten nicht von Strava abrufen.")
                errors.append(err_msg)
                logger.error("[Background-Sync] %s", err_msg)
                self.consecutive_errors += 1
                return {"status": "error", "message": err_msg}

            strava_acts = res.get("activities", [])
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            vector_store = ActivityVectorStore()

            # 3. Neue Aktivitäten identifizieren
            to_download: List[Dict[str, Any]] = []
            for act in strava_acts:
                act_id = str(act.get("activity_id") or act.get("id") or "").strip()
                if not act_id:
                    continue
                existing_files = [
                    f for f in DOWNLOADS_DIR.glob(f"{act_id}*.*")
                    if f.suffix.lower() in (".gpx", ".tcx", ".json")
                ]
                if not existing_files or force:
                    to_download.append(act)

            logger.info(
                "[Background-Sync] %d Aktivitäten geprüft. %d neue/fehlende Einheiten gefunden.",
                len(strava_acts),
                len(to_download),
            )

            # 4. Sequenziell und schonend herunterladen
            for act in to_download:
                if self._stop_event.is_set():
                    break

                act_id = str(act.get("activity_id") or act.get("id") or "").strip()
                try:
                    logger.info("[Background-Sync] Lade Aktivität %s herunter...", act_id)
                    time.sleep(random.uniform(2.0, 4.0))  # Polite pause

                    gpx_path = bm.run(download_activity_gpx, act_id, DOWNLOADS_DIR, logger)
                    if not gpx_path or not gpx_path.exists():
                        errors.append(f"Download fehlgeschlagen für {act_id}")
                        continue

                    time.sleep(random.uniform(1.5, 3.0))
                    segments_res = bm.run(get_activity_segments, act_id, logger)
                    segments_list = (
                        segments_res.get("segments", [])
                        if segments_res.get("status") == "success"
                        else []
                    )

                    title, act_type, points = parse_gpx_file(gpx_path)
                    if points:
                        metrics = calculate_metrics(gpx_path, title, act_type, points)
                        act_date = (
                            points[0].time.strftime("%Y-%m-%d")
                            if points[0].time
                            else act.get("iso_date", "2026-01-01")
                        )

                        vector_store.upsert_activity(
                            activity_id=act_id,
                            name=title or act.get("title") or act.get("name", f"Aktivität {act_id}"),
                            sport_type=act_type or act.get("sport") or act.get("sport_type", "Ride"),
                            activity_date=act_date,
                            distance_km=round(metrics.total_distance_km, 2),
                            elevation_m=round(metrics.elevation_gain_m, 1),
                            moving_time_s=int(metrics.moving_time_sec),
                            suffer_score=metrics.suffer_score,
                            avg_hr=metrics.avg_hr,
                            max_hr=metrics.max_hr,
                            segments=segments_list,
                        )
                        new_synced_ids.append(act_id)
                        logger.info(
                            "[Background-Sync] Aktivität %s ('%s') erfolgreich synchronisiert.",
                            act_id,
                            title,
                        )

                except Exception as act_err:
                    logger.warning("[Background-Sync] Fehler bei %s: %s", act_id, act_err)
                    errors.append(f"{act_id}: {str(act_err)}")

            # 5. Proaktive Trainings- und Erholungs-Alerts auswerten & Coaching-Notizen anlegen
            active_alerts = []
            proactive_coaching_notes = []
            try:
                from training_load import (
                    load_historical_training_data,
                    evaluate_training_alerts,
                    check_and_create_proactive_coaching_alerts,
                )
                from apple_health_service import sync_apple_health_from_ios

                t_engine = load_historical_training_data(user_id="default")
                h_data = sync_apple_health_from_ios(force=False)
                active_alerts = evaluate_training_alerts(
                    user_id="default",
                    training_engine=t_engine,
                    health_data=h_data,
                )
                if active_alerts:
                    logger.warning("[Background-Sync] %d proaktive Trainings-Alerts identifiziert!", len(active_alerts))

                # Automatische Coaching-Notizen für kritische Schwellenwerte anlegen
                proactive_res = check_and_create_proactive_coaching_alerts(
                    user_id="default",
                    training_engine=t_engine,
                    downloads_dir=DOWNLOADS_DIR,
                )
                proactive_coaching_notes = proactive_res.get("created_coaching_notes", [])
                if proactive_coaching_notes:
                    logger.warning(
                        "[Background-Sync] %d neue Coaching-Notizen automatisch angelegt!",
                        len(proactive_coaching_notes),
                    )
            except Exception as alert_err:
                logger.debug("[Background-Sync] Alert-Evaluation übersprungen: %s", alert_err)

            self.consecutive_errors = 0
            self.last_sync_time = datetime.now(timezone.utc)
            self.last_sync_result = {
                "status": "success",
                "timestamp": self.last_sync_time.isoformat(),
                "total_checked": len(strava_acts),
                "new_activities_synced": len(new_synced_ids),
                "synced_ids": new_synced_ids,
                "active_alerts_count": len(active_alerts),
                "active_alerts": active_alerts,
                "created_coaching_notes_count": len(proactive_coaching_notes),
                "created_coaching_notes": proactive_coaching_notes,
                "errors": errors,
            }
            return self.last_sync_result

        finally:
            self.is_syncing = False
            self._sync_lock.release()

    async def run_sync_async(self, force: bool = False) -> Dict[str, Any]:
        """Asynchroner Wrapper für Aufrufe aus dem FastMCP-Event-Loop."""
        return await asyncio.to_thread(self.run_sync_blocking, force=force)

    def get_status(self) -> Dict[str, Any]:
        """Liefert den aktuellen Status des Hintergrund-Synchronisationsdienstes."""
        return {
            "service_running": self._running,
            "is_syncing_now": self.is_syncing,
            "base_interval_minutes": self.interval_seconds // 60,
            "last_sync_time": (
                self.last_sync_time.isoformat() if self.last_sync_time else None
            ),
            "consecutive_errors": self.consecutive_errors,
            "last_sync_result": self.last_sync_result,
        }
