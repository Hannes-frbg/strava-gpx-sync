"""
apple_health_service.py: Hochperformante Integration für Apple Watch / Apple Health.

Unterstützt sowohl:
1. Eigene Apple Health Export App (REST-API auf iOS, z.B. Port 8080: /api/export, /api/summary, /api/sleep, /api/vitals, /api/workouts)
2. Auto Health Export MCP Server (Legacy-Fallback via MCP 2024-11-05 Protokoll auf Port 9000)

Funktionsweise:
- Passiver Mikro-Socket-Probe (<0.35s Timeout, ~0% CPU/Akku) alle 1-5 Sekunden.
- Ist die iOS-App geschlossen/gesperrt: Vollkommen lautlos, keine Fehler ("Wenn keine Antwort kommt, passt das").
- Sobald die App auf dem iPhone aktiv wird: Automatischer Abruf aller Vitalwerte, Schlafphasen, Aktivitäten und Workouts.
- Speichert und aktualisiert die Daten nahtlos in SQLite (strava_rag.db) und aktualisiert das Athletenprofil.
- Berechnet den sportwissenschaftlichen Erholungs- und U17-Readiness-Score (0-100).
- Liefert dem MCP-Server sofortigen Offline- und Online-Zugriff ohne Blockierung.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config import (
    HEALTH_AUTO_EXPORT_URL,
    HEALTH_AUTO_EXPORT_TOKEN,
    HEALTH_EXPORT_APP_URL,
    HEALTH_EXPORT_SCAN_INTERVAL,
)
from rag_engine import ActivityVectorStore

logger = logging.getLogger("strava_mcp.health")


def parse_url_host_port(url_str: str, default_host: str = "192.168.178.180", default_port: int = 8080) -> Tuple[str, int]:
    """Extrahiert Host und Port aus einer beliebigen URL oder IP-Angabe."""
    try:
        if not url_str.startswith("http://") and not url_str.startswith("https://"):
            url_str = f"http://{url_str}"
        parsed = urllib.parse.urlparse(url_str)
        host = parsed.hostname or default_host
        port = parsed.port or default_port
        return host, port
    except Exception:
        return default_host, default_port


def is_health_app_online(url: Optional[str] = None, timeout: float = 0.35) -> bool:
    """
    Führt einen extrem schnellen TCP-Socket-Check auf die eigene Health Export App (Port 8080) aus.
    Verbraucht keine nennenswerte CPU und keinen Akku auf dem iPhone (<0.35s).
    """
    target_url = url or HEALTH_EXPORT_APP_URL
    host, port = parse_url_host_port(target_url, default_host="192.168.178.180", default_port=8080)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        res = s.connect_ex((host, port))
        return res == 0
    except Exception:
        return False
    finally:
        s.close()


def is_health_server_online(timeout: float = 0.35) -> bool:
    """
    Rückwärtskompatibler Socket-Check:
    Prüft, ob entweder die eigene REST-App (Port 8080) oder der alte MCP-Server (Port 9000) online ist.
    """
    # 1. Zuerst eigene Health REST App testen (Standard)
    if is_health_app_online(timeout=timeout):
        return True

    # 2. Fallback: Alte MCP Health Auto Export URL testen
    host, port = parse_url_host_port(HEALTH_AUTO_EXPORT_URL, default_host="192.168.178.180", default_port=9000)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        res = s.connect_ex((host, port))
        return res == 0
    except Exception:
        return False
    finally:
        s.close()


def detect_health_server_type(timeout: float = 0.35) -> Optional[str]:
    """
    Erkennt automatisch den Typ des erreichbaren Health-Dienstes:
    - 'custom_rest': Eigene Health Export App (Standard, z.B. Port 8080)
    - 'mcp_legacy': Drittanbieter Health Auto Export MCP Server (Port 9000)
    - None: Offline / gesperrt
    """
    if is_health_app_online(timeout=timeout):
        return "custom_rest"

    host_legacy, port_legacy = parse_url_host_port(HEALTH_AUTO_EXPORT_URL, default_host="192.168.178.180", default_port=9000)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        if s.connect_ex((host_legacy, port_legacy)) == 0:
            return "mcp_legacy"
    except Exception:
        pass
    finally:
        s.close()

    return None


# ---------------------------------------------------------------------------
# Datenabruf von der eigenen Health Export App (REST API)
# ---------------------------------------------------------------------------
def _http_get_json(url: str, timeout: float = 3.0) -> Optional[Any]:
    """Führt einen einfachen HTTP-GET Request durch und liefert geparstes JSON zurück."""
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "StravaGPXSync/2.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
    except Exception as e:
        logger.debug("HTTP GET Fehler auf %s: %s", url, e)
    return None


def fetch_custom_health_data(base_url: str = HEALTH_EXPORT_APP_URL, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """
    Ruft Vitaldaten, Schlaf, tägliche Zusammenfassungen und Workouts von der eigenen iOS-App ab.
    Versucht zuerst den Komplett-Export (/api/export?days=30), und falls nicht verfügbar,
    die modularen Endpunkte (/api/vitals, /api/summary, /api/sleep, /api/workouts).
    """
    clean_base = base_url.rstrip("/")

    # 1. Vollständigen Gesamtexport versuchen
    export_url = f"{clean_base}/api/export?days=30"
    export_payload = _http_get_json(export_url, timeout=timeout)
    if export_payload and isinstance(export_payload, dict):
        summaries = (
            export_payload.get("dailySummaries")
            or export_payload.get("summaries")
            or export_payload.get("days")
            or []
        )
        workouts = export_payload.get("workouts") or []
        sleep = export_payload.get("sleep") or []
        vitals = export_payload.get("vitals") or {}

        if summaries or workouts or sleep or vitals:
            return {
                "source": "export",
                "vitals": vitals,
                "summaries": summaries,
                "sleep": sleep,
                "workouts": workouts,
                "metadata": export_payload.get("metadata", {}),
            }

    # 2. Modulare Einzel-Endpunkte abfragen
    vitals = _http_get_json(f"{clean_base}/api/vitals", timeout=2.5) or {}
    summaries = _http_get_json(f"{clean_base}/api/summary?days=30", timeout=3.0) or []
    sleep = _http_get_json(f"{clean_base}/api/sleep?days=14", timeout=3.0) or []
    workouts = _http_get_json(f"{clean_base}/api/workouts?days=30", timeout=3.0) or []

    if summaries or sleep or workouts or vitals:
        return {
            "source": "endpoints",
            "vitals": vitals if isinstance(vitals, dict) else {},
            "summaries": summaries if isinstance(summaries, list) else [],
            "sleep": sleep if isinstance(sleep, list) else [],
            "workouts": workouts if isinstance(workouts, list) else [],
        }

    return None


def parse_and_store_custom_health_data(payload: Dict[str, Any], user_id: str = "default") -> Dict[str, Any]:
    """
    Verarbeitet und persistiert alle Gesundheitsdaten der eigenen App:
    - Aktualisiert Körperwerte (VO2max, Gewicht, Ruhepuls) im Athletenprofil.
    - Speichert alle Workouts in 'apple_health_workouts'.
    - Berechnet für jeden erfassten Tag Schlafarchitektur (Tief/REM/Kern), HRV und den U17-Readiness-Score.
    - Persistiert alles in 'apple_health_metrics' für sofortigen KI- und MCP-Zugriff.
    """
    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    baseline_rhr = int(profile.get("hr_rest", 59))
    baseline_hrv = float(profile.get("baseline_hrv_ms", 68.0))

    vitals = payload.get("vitals") or {}
    summaries = payload.get("summaries") or []
    sleep_list = payload.get("sleep") or []
    workouts = payload.get("workouts") or []

    # 1. Athletenprofil aktualisieren, wenn valide Vitaldaten geliefert wurden
    if vitals:
        try:
            wt = vitals.get("weightKg")
            if wt and float(wt) > 30.0:
                store.set_profile_field("rider_weight_kg", round(float(wt), 1), user_id=user_id)

            rhr_v = vitals.get("restingHeartRateBpm")
            if rhr_v and int(rhr_v) > 30:
                store.set_profile_field("hr_rest", int(rhr_v), user_id=user_id)
                baseline_rhr = int(rhr_v)

            vo2 = vitals.get("vo2Max")
            if vo2 and float(vo2) > 15.0:
                store.set_profile_field("vo2_max", round(float(vo2), 1), user_id=user_id)

            hm = vitals.get("heightMeters")
            if hm and float(hm) > 1.0:
                store.set_profile_field("height_meters", round(float(hm), 2), user_id=user_id)
        except Exception as e:
            logger.debug("Hinweis bei Profil-Aktualisierung aus Vitals: %s", e)

    # 2. Workouts persistieren
    stored_workouts = []
    for w in workouts:
        if not isinstance(w, dict):
            continue
        w_id = str(w.get("id") or f"workout_{w.get('startDate', '')}")
        act_type = str(w.get("activityType") or "Workout")
        dur_min = float(w.get("durationMinutes") or 0.0)
        dist_m = float(w.get("totalDistanceMeters") or 0.0)
        kcal = float(w.get("totalEnergyKcal") or 0.0)
        s_date = str(w.get("startDate") or "")
        e_date = str(w.get("endDate") or "")
        source = str(w.get("source") or "Apple Watch")

        saved_w = store.save_apple_health_workout(
            id=w_id,
            activity_type=act_type,
            duration_minutes=dur_min,
            distance_meters=dist_m,
            energy_kcal=kcal,
            start_date=s_date,
            end_date=e_date,
            source=source,
            raw_data=w,
            user_id=user_id,
        )
        stored_workouts.append(saved_w)

    # 3. Schlafdaten nach Datum (YYYY-MM-DD) gruppieren
    sleep_by_date: Dict[str, Dict[str, float]] = {}
    for sn in sleep_list:
        if not isinstance(sn, dict):
            continue
        d_str = str(sn.get("date") or "")[:10]
        if not d_str:
            continue

        tot_min = float(sn.get("totalSleepMinutes") or 0.0)
        deep_min = float(sn.get("deepSleepMinutes") or 0.0)
        rem_min = float(sn.get("remSleepMinutes") or 0.0)
        core_min = float(sn.get("coreSleepMinutes") or 0.0)
        awake_min = float(sn.get("awakeMinutes") or 0.0)

        # Wenn Phasen nicht direkt summiert wurden, aus stages-Array extrahieren
        stages = sn.get("stages", [])
        if (deep_min == 0.0 and rem_min == 0.0) and stages:
            for s in stages:
                stg = str(s.get("stage") or "").lower()
                dur = float(s.get("durationMinutes") or 0.0)
                if stg == "deep":
                    deep_min += dur
                elif stg == "rem":
                    rem_min += dur
                elif stg == "core":
                    core_min += dur
                elif stg == "awake":
                    awake_min += dur

        if tot_min == 0.0:
            tot_min = deep_min + rem_min + core_min

        sleep_by_date[d_str] = {
            "total_sleep_hours": round(tot_min / 60.0, 2),
            "deep_sleep_minutes": round(deep_min, 1),
            "rem_sleep_minutes": round(rem_min, 1),
            "core_sleep_minutes": round(core_min, 1),
            "awake_minutes": round(awake_min, 1),
        }

    # 4. Tägliche Metriken konsolidieren
    today_str = datetime.now().strftime("%Y-%m-%d")
    all_dates = set(sleep_by_date.keys())
    for s in summaries:
        if isinstance(s, dict) and s.get("date"):
            all_dates.add(str(s["date"])[:10])
    if vitals and (vitals.get("restingHeartRateBpm") or vitals.get("hrvSdnnMs")):
        all_dates.add(today_str)

    stored_records = []
    summaries_dict = {str(s["date"])[:10]: s for s in summaries if isinstance(s, dict) and s.get("date")}

    for d_str in sorted(all_dates):
        sum_data = summaries_dict.get(d_str, {})
        slp_data = sleep_by_date.get(d_str, {})

        # Schlafdauer ermitteln
        sleep_hours: Optional[float] = None
        if slp_data.get("total_sleep_hours", 0.0) > 0.4:
            sleep_hours = slp_data["total_sleep_hours"]
        elif sum_data.get("sleepDurationHours") and float(sum_data["sleepDurationHours"]) > 0.4:
            sleep_hours = round(float(sum_data["sleepDurationHours"]), 2)

        deep_min = slp_data.get("deep_sleep_minutes")
        rem_min = slp_data.get("rem_sleep_minutes")

        # Ruhepuls
        resting_hr: Optional[int] = None
        if sum_data.get("restingHeartRateBpm") and int(sum_data["restingHeartRateBpm"]) > 30:
            resting_hr = int(round(float(sum_data["restingHeartRateBpm"])))
        elif d_str == today_str and vitals.get("restingHeartRateBpm"):
            resting_hr = int(vitals["restingHeartRateBpm"])

        # HRV (ms)
        hrv_ms: Optional[float] = None
        if sum_data.get("hrvSdnnMs") and float(sum_data["hrvSdnnMs"]) > 5.0:
            hrv_ms = round(float(sum_data["hrvSdnnMs"]), 1)
        elif d_str == today_str and vitals.get("hrvSdnnMs"):
            hrv_ms = round(float(vitals["hrvSdnnMs"]), 1)

        # Aktivitätsdaten
        steps = int(round(float(sum_data.get("steps", 0)))) if sum_data.get("steps") else None
        active_kcal = round(float(sum_data.get("activeEnergyKcal", 0)), 1) if sum_data.get("activeEnergyKcal") else None
        dist_m = round(float(sum_data.get("distanceMeters", 0)), 1) if sum_data.get("distanceMeters") else None

        vo2_val = float(vitals.get("vo2Max")) if (d_str == today_str and vitals.get("vo2Max")) else None
        weight_val = float(vitals.get("weightKg")) if (d_str == today_str and vitals.get("weightKg")) else None

        # Nur Tage mit messbaren Werten persistieren
        if not any([sleep_hours, resting_hr, hrv_ms, steps, active_kcal]):
            continue

        readiness_res = calculate_youth_readiness(
            sleep_hours=sleep_hours,
            deep_sleep_minutes=deep_min,
            resting_hr=resting_hr,
            hrv_ms=hrv_ms,
            baseline_resting_hr=baseline_rhr,
            baseline_hrv_ms=baseline_hrv,
        )

        raw_meta = {
            "source": payload.get("source", "custom_rest"),
            "steps": steps,
            "active_energy_kcal": active_kcal,
            "distance_meters": dist_m,
            "sleep_hours": sleep_hours,
            "deep_sleep_minutes": deep_min,
            "rem_sleep_minutes": rem_min,
            "core_sleep_minutes": slp_data.get("core_sleep_minutes"),
            "awake_minutes": slp_data.get("awake_minutes"),
            "resting_hr": resting_hr,
            "hrv_ms": hrv_ms,
            "vo2_max": vo2_val,
            "weight_kg": weight_val,
            "exercise_minutes": sum_data.get("exerciseMinutes"),
            "flights_climbed": sum_data.get("flightsClimbed"),
            "workout_count": sum_data.get("workoutCount"),
        }

        saved = store.save_apple_health_metrics(
            recorded_date=d_str,
            sleep_hours=sleep_hours,
            deep_sleep_minutes=deep_min,
            rem_sleep_minutes=rem_min,
            resting_hr=resting_hr,
            hrv_ms=hrv_ms,
            readiness_score=readiness_res.score,
            readiness_verdict=readiness_res.verdict,
            raw_data=raw_meta,
            user_id=user_id,
            steps=steps,
            active_energy_kcal=active_kcal,
            distance_meters=dist_m,
            vo2_max=vo2_val,
            weight_kg=weight_val,
        )
        stored_records.append(saved)

    return {
        "stored_records": stored_records,
        "workouts_count": len(stored_workouts),
        "vitals": vitals,
    }


# ---------------------------------------------------------------------------
# Legacy MCP 2024-11-05 Protokoll (Health Auto Export Fallback)
# ---------------------------------------------------------------------------
def execute_mcp_health_query(
    start_date: str,
    end_date: str,
    timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """Führt den vollen MCP-Handshake mit dem alten Health Auto Export Server (Port 9000) durch."""
    headers = {
        "Authorization": f"Bearer {HEALTH_AUTO_EXPORT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 1. Initialize
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "StravaGPXSync", "version": "2.0"},
        },
    }
    req_init = urllib.request.Request(
        HEALTH_AUTO_EXPORT_URL,
        data=json.dumps(init_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    session_id = None
    try:
        with urllib.request.urlopen(req_init, timeout=timeout) as resp:
            session_id = resp.headers.get("Mcp-Session-Id")
            if not session_id:
                for k, v in resp.headers.items():
                    if k.lower() == "mcp-session-id":
                        session_id = v
                        break
    except Exception as e:
        logger.debug("Fehler beim MCP Initialize: %s", e)
        return None

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    # 2. notifications/initialized
    notif_payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    req_notif = urllib.request.Request(
        HEALTH_AUTO_EXPORT_URL,
        data=json.dumps(notif_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_notif, timeout=timeout):
            pass
    except Exception as e:
        logger.debug("Hinweis bei notifications/initialized: %s", e)

    # 3. tools/call: get_health_metrics
    call_payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "get_health_metrics",
            "arguments": {
                "start": start_date,
                "end": end_date,
                "aggregate": False,
                "metrics": "sleep_analysis,resting_heart_rate,heart_rate_variability",
            },
        },
    }
    req_call = urllib.request.Request(
        HEALTH_AUTO_EXPORT_URL,
        data=json.dumps(call_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_call, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            res_json = json.loads(raw)
            return res_json.get("result", {})
    except Exception as e:
        logger.warning("Fehler bei get_health_metrics von iPhone (MCP): %s", e)
        return None


def parse_and_store_health_data(content_list: list) -> List[Dict[str, Any]]:
    """Parst die rohen JSON-Health-Metriken aus dem Legacy Health Auto Export MCP Server."""
    store = ActivityVectorStore()
    profile = store.get_profile()
    baseline_rhr = int(profile.get("hr_rest", 59))

    by_date: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "sleep_hours": 0.0,
        "deep_sleep_minutes": 0.0,
        "rem_sleep_minutes": 0.0,
        "resting_hr": None,
        "hrv_ms": None,
        "active_energy_kcal": 0.0,
        "steps": 0,
    })

    for c in content_list:
        raw_text = c.get("text", "")
        if not raw_text:
            continue
        try:
            d = json.loads(raw_text)
            metrics = d.get("data", {}).get("metrics", [])
            for m in metrics:
                name = m.get("name")
                data_points = m.get("data", [])

                if name == "sleep_analysis":
                    for dp in data_points:
                        dt_str = str(dp.get("date") or dp.get("start") or "")[:10]
                        val = str(dp.get("value", "")).lower()
                        qty_hours = float(dp.get("qty", 0.0))

                        if not dt_str:
                            continue

                        if "tief" in val:
                            by_date[dt_str]["deep_sleep_minutes"] += (qty_hours * 60.0)
                            by_date[dt_str]["sleep_hours"] += qty_hours
                        elif "rem" in val:
                            by_date[dt_str]["rem_sleep_minutes"] += (qty_hours * 60.0)
                            by_date[dt_str]["sleep_hours"] += qty_hours
                        elif "kern" in val or "asleep" in val:
                            by_date[dt_str]["sleep_hours"] += qty_hours

                elif name == "resting_heart_rate":
                    for dp in data_points:
                        dt_str = str(dp.get("date") or dp.get("start") or "")[:10]
                        qty_bpm = dp.get("qty")
                        if dt_str and qty_bpm:
                            by_date[dt_str]["resting_hr"] = int(round(float(qty_bpm)))

                elif name == "heart_rate_variability":
                    for dp in data_points:
                        dt_str = str(dp.get("date") or dp.get("start") or "")[:10]
                        qty_hrv = dp.get("qty")
                        if dt_str and qty_hrv:
                            by_date[dt_str]["hrv_ms"] = round(float(qty_hrv), 1)

        except Exception as parse_err:
            logger.debug("Fehler beim Parsen eines Legacy MCP Inhaltsblocks: %s", parse_err)

    stored_records = []
    for date_str, rec in sorted(by_date.items()):
        slp = round(rec["sleep_hours"], 2) if rec["sleep_hours"] > 0.5 else None
        deep = round(rec["deep_sleep_minutes"], 1) if rec["deep_sleep_minutes"] > 0 else None
        rem = round(rec["rem_sleep_minutes"], 1) if rec["rem_sleep_minutes"] > 0 else None
        rhr = rec["resting_hr"]
        hrv = rec["hrv_ms"]

        score, verdict = calculate_youth_readiness(
            sleep_hours=slp,
            deep_sleep_minutes=deep,
            resting_hr=rhr,
            hrv_ms=hrv,
            baseline_resting_hr=baseline_rhr,
        )

        saved = store.save_apple_health_metrics(
            recorded_date=date_str,
            sleep_hours=slp,
            deep_sleep_minutes=deep,
            rem_sleep_minutes=rem,
            resting_hr=rhr,
            hrv_ms=hrv,
            readiness_score=score,
            readiness_verdict=verdict,
            raw_data=rec,
        )
        stored_records.append(saved)

    return stored_records


# ---------------------------------------------------------------------------
# Sportwissenschaftliche Readiness & Konfidenz-Berechnung
# ---------------------------------------------------------------------------
class ReadinessTuple(tuple):
    """
    Tuple-Subklasse (score, verdict) mit zusätzlichen Attributen für Konfidenz und Signal-Verfügbarkeit.
    Vollständig abwärtskompatibel für `score, verdict = calculate_youth_readiness(...)`.
    """
    def __new__(cls, score: float, verdict: str, details: Optional[Dict[str, Any]] = None):
        obj = super().__new__(cls, (score, verdict))
        obj.score = score
        obj.verdict = verdict
        obj.details = details or {}
        obj.confidence = obj.details.get("confidence", "high")
        obj.confidence_score = obj.details.get("confidence_score", 1.0)
        obj.signals_available = obj.details.get("signals_available", [])
        obj.signals_missing = obj.details.get("signals_missing", [])
        obj.confidence_warning = obj.details.get("confidence_warning")
        return obj


def evaluate_signal_confidence(
    sleep_hours: Optional[float],
    resting_hr: Optional[int],
    hrv_ms: Optional[float],
) -> Dict[str, Any]:
    """Ermittelt die sportwissenschaftliche Datenqualität und Konfidenz der Vitaldaten."""
    signals_present: List[str] = []
    signals_missing: List[str] = []

    if sleep_hours is not None and sleep_hours > 0.5:
        signals_present.append("sleep")
    else:
        signals_missing.append("sleep")

    if resting_hr is not None and resting_hr > 30:
        signals_present.append("resting_heart_rate")
    else:
        signals_missing.append("resting_heart_rate")

    if hrv_ms is not None and hrv_ms > 5:
        signals_present.append("heart_rate_variability")
    else:
        signals_missing.append("heart_rate_variability")

    count = len(signals_present)
    if count == 3:
        confidence = "high"
        confidence_score = 1.0
        warning = None
    elif count == 2:
        confidence = "medium"
        confidence_score = 0.67
        missing_de = " / ".join("Ruhepuls" if m == "resting_heart_rate" else "HRV" if m == "heart_rate_variability" else "Schlaf" for m in signals_missing)
        warning = f"Mittlere Konfidenz (2/3 Signale): {missing_de} nicht erfasst."
    elif count == 1:
        confidence = "low"
        confidence_score = 0.33
        present_de = "Schlaf" if signals_present[0] == "sleep" else "Ruhepuls" if signals_present[0] == "resting_heart_rate" else "HRV"
        missing_de = " & ".join("Ruhepuls" if m == "resting_heart_rate" else "HRV" if m == "heart_rate_variability" else "Schlaf" for m in signals_missing)
        warning = (
            f"Niedrige Konfidenz (nur 1/3 Signale): {missing_de} fehlen komplett! "
            f"Der Score basiert ausschließlich auf {present_de} und ist nur eingeschränkt aussagekräftig."
        )
    else:
        confidence = "none"
        confidence_score = 0.0
        warning = "Keine validen Vitaldaten erfasst (0/3 Signale)."

    return {
        "confidence": confidence,
        "confidence_score": confidence_score,
        "signals_count": count,
        "signals_total": 3,
        "signals_available": signals_present,
        "signals_missing": signals_missing,
        "confidence_warning": warning,
    }


def calculate_youth_readiness(
    sleep_hours: Optional[float],
    deep_sleep_minutes: Optional[float],
    resting_hr: Optional[int],
    hrv_ms: Optional[float],
    baseline_resting_hr: int = 59,
    baseline_hrv_ms: float = 68.0,
) -> ReadinessTuple:
    """
    Berechnet einen sportwissenschaftlichen Erholungs- und Leistungsbereitschafts-Score (0-100)
    für den Athleten, basierend auf Apple Health Ruheherzfrequenz, HRV (SDNN) und Schlafphasen.
    """
    conf_info = evaluate_signal_confidence(sleep_hours, resting_hr, hrv_ms)
    score = 80.0  # Basiswert
    verdicts = []

    # 1. Schlaf & Tiefschlaf
    if sleep_hours is not None and sleep_hours > 0.5:
        if sleep_hours >= 8.0:
            score += 10.0
            verdicts.append(f"Hervorragender Schlaf ({sleep_hours:.1f}h)")
        elif sleep_hours >= 7.0:
            score += 5.0
            verdicts.append(f"Guter Schlaf ({sleep_hours:.1f}h)")
        elif sleep_hours >= 5.8:
            score -= 5.0
            verdicts.append(f"Etwas verkürzter Schlaf ({sleep_hours:.1f}h)")
        else:
            score -= 15.0
            verdicts.append(f"Schlafdefizit ({sleep_hours:.1f}h)")

        if deep_sleep_minutes is not None and deep_sleep_minutes >= 60.0:
            score += 5.0
            verdicts.append(f"Starker Tiefschlaf ({deep_sleep_minutes:.0f} min)")
    else:
        verdicts.append("Schlaf nicht getrackt (Uhr auf Nachttisch)")

    # 2. Ruhepuls am Tag
    if resting_hr is not None:
        diff_hr = resting_hr - baseline_resting_hr
        if diff_hr <= -2:
            score += 8.0
            verdicts.append(f"Exzellenter Ruhepuls ({resting_hr} bpm, unter Normalwert)")
        elif diff_hr <= 2:
            score += 4.0
            verdicts.append(f"Stabiler Ruhepuls ({resting_hr} bpm im Idealbereich)")
        elif diff_hr <= 5:
            score -= 6.0
            verdicts.append(f"Leicht erhöhter Ruhepuls ({resting_hr} bpm)")
        else:
            score -= 16.0
            verdicts.append(f"Deutlich erhöhter Ruhepuls ({resting_hr} bpm - Belastung)")

    # 3. Herzfrequenzvariabilität (HRV)
    if hrv_ms is not None:
        if hrv_ms >= 85.0 or (hrv_ms >= baseline_hrv_ms + 10.0):
            score += 8.0
            verdicts.append(f"Hervorragende HRV ({hrv_ms:.0f} ms - Top-Regeneration)")
        elif hrv_ms >= 60.0:
            score += 4.0
            verdicts.append(f"Ausgeglichene HRV ({hrv_ms:.0f} ms)")
        elif hrv_ms >= 42.0:
            score -= 6.0
            verdicts.append(f"Leicht gedämpfte HRV ({hrv_ms:.0f} ms)")
        else:
            score -= 14.0
            verdicts.append(f"Gedämpfte HRV ({hrv_ms:.0f} ms - Sympathikus aktiv)")

    if conf_info["confidence_warning"]:
        verdicts.append(f"[{conf_info['confidence_warning']}]")

    final_score = round(max(15.0, min(100.0, score)), 1)
    verdict_text = " | ".join(verdicts) if verdicts else "Vitaldaten im Normbereich."
    return ReadinessTuple(final_score, verdict_text, conf_info)


def enrich_record_with_quality(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Erweitert einen Health-Datensatz um präzise Konfidenz- und Signal-Vollständigkeitsangaben."""
    if not record:
        return None
    r = dict(record)
    conf = evaluate_signal_confidence(r.get("sleep_hours"), r.get("resting_hr"), r.get("hrv_ms"))
    r["confidence"] = conf["confidence"]
    r["confidence_score"] = conf["confidence_score"]
    r["signals_available"] = conf["signals_available"]
    r["signals_missing"] = conf["signals_missing"]
    r["confidence_warning"] = conf["confidence_warning"]
    return r


# ---------------------------------------------------------------------------
# Hauptfunktion: Sync & Passive Cache-Bereitstellung
# ---------------------------------------------------------------------------
def sync_apple_health_from_ios(force: bool = False, user_id: str = "default") -> Dict[str, Any]:
    """
    Zentrale Synchronisations- und Abruffunktion:
    1. Prüft blitzschnell per Socket (<0.35s), ob die eigene iOS-App oder das iPhone online ist.
    2. Wenn online:
       - Ruft die Gesundheitsdaten live ab (/api/export oder modulare Endpunkte).
       - Persistiert sie in SQLite und aktualisiert das Athletenprofil.
    3. Wenn offline:
       - Liefert sofort und ohne Verzögerung die lokal gespeicherten Daten aus der SQLite-Datenbank.
       - Fügt Altersangabe (cache_age_days) und Staleness-Warnung hinzu.
    """
    store = ActivityVectorStore()
    clean_user = str(user_id).strip() or "default"

    # Server-Typ ermitteln
    server_type = detect_health_server_type(timeout=0.35)

    if not server_type and not force:
        # Offline-Modus: Sofort SQLite Cache bereitstellen
        latest = enrich_record_with_quality(store.get_latest_apple_health_metrics(user_id=clean_user))
        raw_history = store.get_apple_health_history(days=7, user_id=clean_user)
        history = [enrich_record_with_quality(h) for h in raw_history]

        cache_age_days = None
        is_stale = False
        staleness_msg = None

        if latest and latest.get("recorded_date"):
            try:
                rec_dt = datetime.strptime(latest["recorded_date"], "%Y-%m-%d").date()
                today_d = datetime.now().date()
                cache_age_days = max(0, (today_d - rec_dt).days)
                if cache_age_days >= 2:
                    is_stale = True
                    staleness_msg = (
                        f"Achtung: Die aktuellsten Vitaldaten sind {cache_age_days} Tage alt "
                        f"(Stand: {latest['recorded_date']}). Health Export App auf dem iPhone ist offline."
                    )
            except Exception:
                pass

        base_msg = "Health Export App auf dem iPhone ist aktuell nicht aktiv. Daten werden aus der lokalen Datenbank bereitgestellt."
        combined_msg = f"{staleness_msg} {base_msg}" if staleness_msg else base_msg

        return {
            "status": "cached_offline",
            "server_online": False,
            "sync_source": "cache",
            "cache_age_days": cache_age_days,
            "is_stale": is_stale,
            "data_staleness_warning": staleness_msg,
            "message": combined_msg,
            "latest_metrics": latest,
            "recent_days_history": history,
            "coaching_takeaway": (
                f"Tages-Readiness (Cache): {latest.get('readiness_score', '--')}/100 "
                f"(Stand: {latest.get('recorded_date', '--')}). {latest.get('readiness_verdict', '')}"
                if latest else "Keine historischen Vitaldaten in der lokalen Datenbank gefunden."
            ),
        }

    # Online-Sync: Eigene REST-API priorisieren
    t0 = time.time()
    if server_type == "custom_rest" or (force and not server_type):
        logger.info("Eigene Health Export App online auf %s! Starte automatischen Sync...", HEALTH_EXPORT_APP_URL)
        data = fetch_custom_health_data(base_url=HEALTH_EXPORT_APP_URL, timeout=4.0)
        duration_s = round(time.time() - t0, 2)

        if data:
            parse_res = parse_and_store_custom_health_data(data, user_id=clean_user)
            stored_records = parse_res.get("stored_records", [])
            workouts_cnt = parse_res.get("workouts_count", 0)

            latest = enrich_record_with_quality(store.get_latest_apple_health_metrics(user_id=clean_user))
            history = [enrich_record_with_quality(h) for h in store.get_apple_health_history(days=7, user_id=clean_user)]

            logger.info(
                "Health Export Sync erfolgreich abgeschlossen in %.2fs! %d Tage, %d Workouts synchronisiert.",
                duration_s, len(stored_records), workouts_cnt,
            )

            return {
                "status": "success",
                "server_online": True,
                "sync_source": "custom_rest",
                "query_duration_seconds": duration_s,
                "synchronized_days_count": len(stored_records),
                "workouts_count": workouts_cnt,
                "latest_vitals": parse_res.get("vitals", {}),
                "latest_metrics": latest,
                "recent_days_history": history,
                "coaching_takeaway": (
                    f"Tages-Readiness: {latest.get('readiness_score', '--')}/100 "
                    f"(Konfidenz: {latest.get('confidence', 'high')}). "
                    f"Details: {latest.get('readiness_verdict', '')}"
                    if latest else "Vitaldaten erfolgreich aktualisiert."
                ),
            }

    # Fallback auf Legacy MCP Protokoll (Port 9000)
    if server_type == "mcp_legacy":
        logger.info("Legacy Health Auto Export MCP Server online auf %s! Starte MCP Sync...", HEALTH_AUTO_EXPORT_URL)
        today_dt = datetime.now()
        start_str = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
        end_str = today_dt.strftime("%Y-%m-%d")

        mcp_res = execute_mcp_health_query(start_date=start_str, end_date=end_str, timeout=6.0)
        duration_s = round(time.time() - t0, 2)

        if mcp_res and "content" in mcp_res:
            stored = parse_and_store_health_data(mcp_res["content"])
            latest = enrich_record_with_quality(store.get_latest_apple_health_metrics(user_id=clean_user))
            history = [enrich_record_with_quality(h) for h in store.get_apple_health_history(days=7, user_id=clean_user)]

            return {
                "status": "success",
                "server_online": True,
                "sync_source": "mcp_legacy",
                "query_duration_seconds": duration_s,
                "synchronized_days_count": len(stored),
                "latest_metrics": latest,
                "recent_days_history": history,
                "coaching_takeaway": (
                    f"Tages-Readiness: {latest.get('readiness_score', '--')}/100 "
                    f"Details: {latest.get('readiness_verdict', '')}"
                    if latest else "Vitaldaten via MCP aktualisiert."
                ),
            }

    # Falls Online-Sync fehlschlug: Cache zurückgeben
    latest = enrich_record_with_quality(store.get_latest_apple_health_metrics(user_id=clean_user))
    history = [enrich_record_with_quality(h) for h in store.get_apple_health_history(days=7, user_id=clean_user)]
    return {
        "status": "partial_success",
        "server_online": False,
        "sync_source": "cache_fallback",
        "message": "Verbindung zum iPhone konnte nicht hergestellt werden. Lokale Cachedaten geliefert.",
        "latest_metrics": latest,
        "recent_days_history": history,
    }


# ---------------------------------------------------------------------------
# Apple Health Auto-Scanner Daemon
# ---------------------------------------------------------------------------
class AppleHealthAutoScanner:
    """
    Hintergrund-Daemon für vollautomatischen Health-Sync:
    Testet alle 1-5 Sekunden per Mikro-Socket-Probe (<0.001% CPU),
    ob die Health Export App auf dem iPhone erreichbar ist.

    - Bleibt absolut lautlos, solange keine Antwort kommt ("dann passt das").
    - Sobald die App aktiv wird, zieht der Scanner automatisch alle Vitaldaten,
      Schlafphasen und Workouts ab ("so dass die über eine Zeit lang abgegriffen werden").
    - Läuft reaktiv weiter: Bei offener App aktualisiert er sich schonend alle ~20 Sekunden.
    - Wird die App geschlossen, fällt er sofort in den sparsamen Socket-Check zurück.
    """

    _instance: Optional["AppleHealthAutoScanner"] = None
    _lock = threading.Lock()

    def __init__(self, check_interval_seconds: int = HEALTH_EXPORT_SCAN_INTERVAL) -> None:
        self.interval = max(1, check_interval_seconds)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_successful_sync: float = 0.0
        self._is_online = False

    @classmethod
    def get_instance(cls, check_interval_seconds: int = HEALTH_EXPORT_SCAN_INTERVAL) -> "AppleHealthAutoScanner":
        with cls._lock:
            if cls._instance is None:
                cls._instance = AppleHealthAutoScanner(check_interval_seconds=check_interval_seconds)
            return cls._instance

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="apple_health_scanner",
            )
            self._thread.start()
            logger.info("Apple Health Auto-Scanner aktiv (Polling-Takt: alle %ds).", self.interval)

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def is_online(self) -> bool:
        return self._is_online

    def _run_loop(self) -> None:
        while self._running:
            try:
                server_type = detect_health_server_type(timeout=0.35)
                if server_type:
                    self._is_online = True
                    now = time.time()
                    # Sofort bei Erstverbindung ODER wenn mindestens 20s seit letztem Sync vergangen sind
                    if (now - self._last_successful_sync) >= 20.0:
                        logger.info("Apple Health App auf iPhone aktiv (%s)! Starte automatischen Datenabgleich...", server_type)
                        res = sync_apple_health_from_ios(force=True)
                        self._last_successful_sync = time.time()
                        days_synced = res.get("synchronized_days_count", 0)
                        workouts_synced = res.get("workouts_count", 0)
                        r_score = res.get("latest_metrics", {}).get("readiness_score", "--")
                        logger.info(
                            "Auto-Sync erfolgreich (%s): %d Tage, %d Workouts synchronisiert. Readiness: %s/100.",
                            server_type, days_synced, workouts_synced, r_score,
                        )
                else:
                    self._is_online = False
            except Exception as e:
                logger.debug("Fehler im Health Auto-Scanner: %s", e)

            # Sparsamer Schlaftakt in 1-Sekunden-Intervallen für sofortiges Anhalten bei Server-Stopp
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)
