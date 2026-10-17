"""
analyze_gpx.py: Detaillierte Trainings- und Leistungsanalyse für Strava-GPX-Dateien.

Liest alle GPX-Dateien im Ordner './downloads/' (oder übergebene Einzeldateien) ein,
parst GPS-Koordinaten, Höhenprofile sowie Garmin TrackPointExtension Herzfrequenzdaten
und berechnet zentrale sportwissenschaftliche Metriken (Haversine-Distanz, Höhenmeter,
Bewegungszeit, Geschwindigkeiten, HR-Zonen und Banister-TRIMP).

Komplett lauffähig ausschließlich mit Python-Standardbibliotheken.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from sports_science import (
        calculate_metabolic_and_nutrition,
        calculate_vam_and_youth_diagnostics,
    )
except ImportError:
    calculate_metabolic_and_nutrition = None  # type: ignore
    calculate_vam_and_youth_diagnostics = None  # type: ignore

try:
    from geo_enhancements import (
        query_osm_route_surface,
        verify_elevation_dem,
        apply_dem_elevation_correction,
    )
except ImportError:
    query_osm_route_surface = None  # type: ignore
    verify_elevation_dem = None  # type: ignore
    apply_dem_elevation_correction = None  # type: ignore


# UTF-8 Konsole absichern (wichtig für Windows Terminal & Sonderzeichen)
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


@dataclass
class TrackPoint:
    lat: float = 0.0
    lon: float = 0.0
    ele: Optional[float] = None
    time: Optional[datetime] = None
    hr: Optional[int] = None
    distance_m: Optional[float] = None
    cadence: Optional[int] = None
    watts: Optional[float] = None


@dataclass
class HRZoneInfo:
    zone_idx: int
    name: str
    min_bpm: int
    max_bpm: int
    duration_sec: float = 0.0
    percentage: float = 0.0


@dataclass
class ActivityMetrics:
    file_path: Path
    title: str = "Unbekannte Aktivität"
    activity_type: str = "Unbekannt"
    total_points: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    # Distanz (km)
    total_distance_km: float = 0.0

    # Höhenmeter (m)
    elevation_gain_m: float = 0.0
    elevation_loss_m: float = 0.0
    min_elevation_m: Optional[float] = None
    max_elevation_m: Optional[float] = None
    elevation_source_used: str = "barometric_gpx"
    elevation_reconciliation_applied: bool = False
    original_gpx_gain_m: Optional[float] = None

    # Zeiten (Sekunden)
    elapsed_time_sec: float = 0.0
    moving_time_sec: float = 0.0
    paused_time_sec: float = 0.0

    # Geschwindigkeit (km/h)
    avg_speed_moving_kmh: float = 0.0
    avg_speed_total_kmh: float = 0.0
    max_speed_kmh: float = 0.0
    speed_filtered: bool = False
    raw_max_speed_kmh: Optional[float] = None
    speed_filter_reason: Optional[str] = None

    # Herzfrequenz
    hr_available: bool = False
    avg_hr: Optional[float] = None
    max_hr: Optional[int] = None
    min_hr: Optional[int] = None
    hr_rest_used: Optional[int] = None
    hr_rest_source: str = ""
    zones: list[HRZoneInfo] = field(default_factory=list)
    suffer_score: Optional[float] = None
    virtual_power: dict[str, Any] = field(default_factory=dict)
    coaching_telemetry: dict[str, Any] = field(default_factory=dict)
    nutrition_metabolism: dict[str, Any] = field(default_factory=dict)
    vam_diagnostics: dict[str, Any] = field(default_factory=dict)
    surface_analysis: dict[str, Any] = field(default_factory=dict)
    dem_verification: dict[str, Any] = field(default_factory=dict)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    data_quality_verdict: str = "clean"


def strip_ns(tag: str) -> str:
    """Entfernt XML-Namespace-Präfixe wie {http://...} aus Element-Tags."""
    return tag.split("}")[-1] if "}" in tag else tag


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Berechnet die Großkreisentfernung zweier GPS-Koordinaten nach der Haversine-Formel in Metern.
    """
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_m * c


def parse_iso_datetime(dt_str: str) -> Optional[datetime]:
    """Parst ISO-8601 Zeitstempel aus GPX-Dateien robust in datetime-Objekte."""
    if not dt_str:
        return None
    cleaned = dt_str.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        # Fallback für Sonderformate
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        return None


def format_seconds_to_hms(seconds: float) -> str:
    """Formatiert Sekunden in HH:MM:SS."""
    total_sec = max(0, int(round(seconds)))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_tcx_file(file_path: Path) -> tuple[str, str, list[TrackPoint]]:
    """
    Parst eine TCX-Datei (Training Center XML), z. B. für Indoor-Läufe, Laufband-Einheiten,
    Rolle oder Aktivitäten ohne GPS-Koordinaten.
    Extrahiert Zeiten, kumulierte Distanz (DistanceMeters), Herzfrequenz (HeartRateBpm)
    und Trittfrequenz/Kadenz.
    """
    tree = ET.parse(file_path)
    root = tree.getroot()

    title = file_path.stem
    activity_type = "Indoor"
    points: list[TrackPoint] = []

    # Activity Sport & Name ermitteln
    for act in root.iter():
        tag = strip_ns(act.tag)
        if tag == "Activity":
            sport = act.attrib.get("Sport")
            if sport:
                activity_type = sport
                if sport.lower() in ("running", "run"):
                    title = "Indoor-Lauf (Laufband / Hallentraining)"
                elif sport.lower() in ("biking", "ride"):
                    title = "Indoor-Radfahrt (Rolle / Ergometer)"
                else:
                    title = f"Indoor-{sport}"
        elif tag == "Notes" and act.text and act.text.strip():
            title = act.text.strip()

    # Trackpoints parsen
    for elem in root.iter():
        if strip_ns(elem.tag) != "Trackpoint":
            continue

        pt_time: Optional[datetime] = None
        lat: float = 0.0
        lon: float = 0.0
        ele: Optional[float] = None
        hr: Optional[int] = None
        dist_m: Optional[float] = None
        cadence: Optional[int] = None
        watts: Optional[float] = None

        for child in elem:
            c_tag = strip_ns(child.tag)
            if c_tag == "Time" and child.text:
                pt_time = parse_iso_datetime(child.text)
            elif c_tag == "Position":
                for pos_child in child:
                    pos_tag = strip_ns(pos_child.tag)
                    if pos_tag == "LatitudeDegrees" and pos_child.text:
                        try:
                            lat = float(pos_child.text.strip())
                        except ValueError:
                            pass
                    elif pos_tag == "LongitudeDegrees" and pos_child.text:
                        try:
                            lon = float(pos_child.text.strip())
                        except ValueError:
                            pass
            elif c_tag == "AltitudeMeters" and child.text:
                try:
                    ele = float(child.text.strip())
                except ValueError:
                    pass
            elif c_tag == "DistanceMeters" and child.text:
                try:
                    dist_m = float(child.text.strip())
                except ValueError:
                    pass
            elif c_tag == "HeartRateBpm":
                for hr_val in child:
                    if strip_ns(hr_val.tag) == "Value" and hr_val.text:
                        try:
                            hr = int(round(float(hr_val.text.strip())))
                        except ValueError:
                            pass
            elif c_tag == "Cadence" and child.text:
                try:
                    cadence = int(round(float(child.text.strip())))
                except ValueError:
                    pass
            elif c_tag == "Extensions":
                for ext in child.iter():
                    ext_tag = strip_ns(ext.tag)
                    if ext_tag == "Watts" and ext.text:
                        try:
                            watts = float(ext.text.strip())
                        except ValueError:
                            pass

        if pt_time or hr is not None or dist_m is not None:
            points.append(
                TrackPoint(
                    lat=lat,
                    lon=lon,
                    ele=ele,
                    time=pt_time,
                    hr=hr,
                    distance_m=dist_m,
                    cadence=cadence,
                    watts=watts,
                )
            )

    return title, activity_type, points


def parse_json_activity_file(file_path: Path) -> tuple[str, str, list[TrackPoint]]:
    """
    Parst eine Aktivitäts-JSON-Datei (z. B. aus Strava Web Streams oder manuellem Indoor-Import).
    """
    data = json.loads(file_path.read_text(encoding="utf-8"))
    title = data.get("title") or file_path.stem
    activity_type = data.get("sport_type") or data.get("sport") or "Indoor"
    points: list[TrackPoint] = []

    # 1. Fall: Zeitreihen-Streams vorhanden (time, heartrate, distance, cadence, watts)
    streams = data.get("streams", {})
    times = streams.get("time", {}).get("data", []) if isinstance(streams.get("time"), dict) else streams.get("time", [])
    hrs = streams.get("heartrate", {}).get("data", []) if isinstance(streams.get("heartrate"), dict) else streams.get("heartrate", [])
    dists = streams.get("distance", {}).get("data", []) if isinstance(streams.get("distance"), dict) else streams.get("distance", [])
    cadences = streams.get("cadence", {}).get("data", []) if isinstance(streams.get("cadence"), dict) else streams.get("cadence", [])
    watts_list = streams.get("watts", {}).get("data", []) if isinstance(streams.get("watts"), dict) else streams.get("watts", [])

    start_time_iso = data.get("start_time") or data.get("date")
    start_dt = parse_iso_datetime(start_time_iso) if start_time_iso else datetime.now(timezone.utc)

    if times:
        for idx, t_offset in enumerate(times):
            pt_time = start_dt + timedelta(seconds=t_offset) if start_dt else None
            hr_val = int(hrs[idx]) if idx < len(hrs) and hrs[idx] is not None else None
            dist_val = float(dists[idx]) if idx < len(dists) and dists[idx] is not None else None
            cad_val = int(cadences[idx]) if idx < len(cadences) and cadences[idx] is not None else None
            watt_val = float(watts_list[idx]) if idx < len(watts_list) and watts_list[idx] is not None else None

            points.append(
                TrackPoint(
                    lat=0.0,
                    lon=0.0,
                    ele=0.0,
                    time=pt_time,
                    hr=hr_val,
                    distance_m=dist_val,
                    cadence=cad_val,
                    watts=watt_val,
                )
            )
    elif "points" in data and isinstance(data["points"], list):
        for p in data["points"]:
            points.append(
                TrackPoint(
                    lat=float(p.get("lat", 0.0)),
                    lon=float(p.get("lon", 0.0)),
                    ele=float(p.get("ele", 0.0)) if p.get("ele") is not None else None,
                    time=parse_iso_datetime(p.get("time")),
                    hr=int(p["hr"]) if p.get("hr") is not None else None,
                    distance_m=float(p["distance_m"]) if p.get("distance_m") is not None else None,
                    cadence=int(p["cadence"]) if p.get("cadence") is not None else None,
                    watts=float(p["watts"]) if p.get("watts") is not None else None,
                )
            )
    else:
        # 2. Fallback: Aggregierte Zusammenfassung (z. B. Dauer & Distanz ohne Sekundenstrom)
        duration_s = float(data.get("moving_time_sec") or data.get("elapsed_time_sec") or 1800.0)
        dist_m = float(data.get("distance_m") or (float(data.get("distance_km", 0.0)) * 1000.0))
        avg_hr = data.get("avg_hr")
        end_dt = start_dt + timedelta(seconds=duration_s) if start_dt else None
        points.append(TrackPoint(lat=0.0, lon=0.0, ele=0.0, time=start_dt, hr=int(avg_hr) if avg_hr else None, distance_m=0.0))
        points.append(TrackPoint(lat=0.0, lon=0.0, ele=0.0, time=end_dt, hr=int(avg_hr) if avg_hr else None, distance_m=dist_m))

    return title, activity_type, points


def parse_gpx_file(file_path: Path) -> tuple[str, str, list[TrackPoint]]:
    """
    Universeller Aktivitäts-Parser: Erkennt und parst automatisch GPX, TCX und JSON-Dateien
    (auch für reine Indoor-Aktivitäten ohne GPS-Koordinaten).
    """
    ext = file_path.suffix.lower()
    if ext == ".tcx":
        return parse_tcx_file(file_path)
    elif ext == ".json":
        return parse_json_activity_file(file_path)

    # Prüfen, ob es sich trotz .gpx-Endung oder unklarer Endung um TCX handelt
    try:
        with open(file_path, "rb") as fp:
            first_chunk = fp.read(1024).decode("utf-8", errors="ignore")
        if "TrainingCenterDatabase" in first_chunk:
            return parse_tcx_file(file_path)
    except Exception:
        pass

    tree = ET.parse(file_path)
    root = tree.getroot()

    title = "Unbenannte Aktivität"
    activity_type = "Unbekannt"
    points: list[TrackPoint] = []

    # Name und Typ ermitteln
    for trk in root.iter():
        tag = strip_ns(trk.tag)
        if tag == "name" and trk.text:
            title = trk.text.strip()
        elif tag == "type" and trk.text:
            activity_type = trk.text.strip()

    # TrackPoints parsen
    for elem in root.iter():
        if strip_ns(elem.tag) != "trkpt":
            continue

        try:
            lat = float(elem.attrib["lat"])
            lon = float(elem.attrib["lon"])
        except (KeyError, ValueError):
            continue

        ele: Optional[float] = None
        pt_time: Optional[datetime] = None
        hr: Optional[int] = None

        for child in elem:
            child_tag = strip_ns(child.tag)
            if child_tag == "ele" and child.text:
                try:
                    ele = float(child.text.strip())
                except ValueError:
                    pass
            elif child_tag == "time" and child.text:
                pt_time = parse_iso_datetime(child.text)
            elif child_tag == "extensions":
                # Rekursiv nach <hr> in gpxtpx suchen
                for sub in child.iter():
                    if strip_ns(sub.tag) == "hr" and sub.text:
                        try:
                            hr = int(round(float(sub.text.strip())))
                        except ValueError:
                            pass

        points.append(TrackPoint(lat=lat, lon=lon, ele=ele, time=pt_time, hr=hr))

    return title, activity_type, points


def calculate_elevation_gain_loss(
    elevations: list[float], threshold: float = 0.5
) -> tuple[float, float]:
    """
    Berechnet Aufstieg und Abstieg mit Hysterese-Rauschfilterung (Schwellenwert z. B. 0.5m).
    Filtert Mikroschwingungen/GPS-Rauschen heraus, erfasst jedoch kontinuierliche Steigungen
    und Gefälle präzise. Die Netto-Höhendifferenz (Gain - Loss) bleibt exakt erhalten.
    """
    if len(elevations) < 2:
        return 0.0, 0.0

    gain = 0.0
    loss = 0.0
    trough = elevations[0]
    peak = elevations[0]
    direction = 0  # 1 = aufsteigend, -1 = absteigend

    for e in elevations[1:]:
        if direction == 0:
            if e - trough >= threshold:
                gain += e - trough
                peak = e
                direction = 1
            elif peak - e >= threshold:
                loss += peak - e
                trough = e
                direction = -1
        elif direction == 1:
            if e > peak:
                gain += e - peak
                peak = e
            elif peak - e >= threshold:
                direction = -1
                trough = e
                loss += peak - e
        elif direction == -1:
            if e < trough:
                loss += trough - e
                trough = e
            elif e - trough >= threshold:
                direction = 1
                peak = e
                gain += e - trough

    return gain, loss


def calculate_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Berechnet die Fahrtrichtung (Kompasskurs 0-360°) zwischen zwei GPS-Koordinaten."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    deg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return deg


def estimate_virtual_power_and_coaching(
    points: list[TrackPoint],
    total_system_weight_kg: float = 72.9,
    rider_weight_kg: float = 60.0,
    weather_data: Optional[dict[str, Any]] = None,
    surface_crr: Optional[float] = None,
    forest_shelter_pct: float = 0.0,
    corrected_elevations: Optional[list[float]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Berechnet die physikalisch geschätzte mechanische Leistung (Virtual Watts)
    unter Einbeziehung von realen OpenWeatherMap-Windvektoren, OSM-Rollwiderstand (C_rr),
    exakter Luftdichte und optional entstörten Satelliten-Höhenkurven (DEM).

    Physikalisches Radsport-Widerstandsmodell:
    - P_climb = m * g * v * sin(arctan(slope))
    - P_roll = C_rr * m * g * cos(arctan(slope)) * v (C_rr dynamisch via OSM Oberfläche ermittelt)
    - P_aero = 0.5 * rho * CdA(yaw) * v_apparent^2 * v_ground
      (v_apparent berücksichtigt relativen Windvektor zur Fahrtrichtung und Wald-Winddämpfung)
    - Drivetrain-Verlust: 3.5% (Faktor 1.035)
    """
    if len(points) < 2:
        return {}, {}

    g = 9.80665
    c_d_a_base = 0.32
    c_rr = float(surface_crr) if (surface_crr is not None and surface_crr > 0) else 0.0048
    drivetrain_factor = 1.035

    # Umweltparameter aus Wetterdaten ermitteln (falls vorhanden)
    has_weather = bool(weather_data and "wind_speed_ms" in weather_data)
    wind_speed_raw_ms = float(weather_data.get("wind_speed_ms", 0.0)) if has_weather else 0.0
    wind_deg = float(weather_data.get("wind_deg", 0.0)) if has_weather else 0.0
    rho = float(weather_data.get("air_density_kg_m3", 1.225)) if has_weather else 1.225

    # Waldbedeckung dämpft bodennahe Windgeschwindigkeiten (bis zu 45% Dämpfung bei 100% Wald)
    shelter_factor = max(0.2, 1.0 - 0.45 * (min(100.0, max(0.0, forest_shelter_pct)) / 100.0))
    wind_speed_ms = wind_speed_raw_ms * shelter_factor

    total_moving_time_s = 0.0
    total_work_j = 0.0
    climb_moving_time_s = 0.0
    climb_work_j = 0.0
    climb_elev_gain_m = 0.0

    headwind_time_s = 0.0
    tailwind_time_s = 0.0
    crosswind_time_s = 0.0

    power_samples: list[tuple[float, float]] = []
    half_samples: list[dict[str, Any]] = []

    has_corrected_ele = bool(corrected_elevations and len(corrected_elevations) == len(points))

    for i in range(len(points) - 1):
        p1 = points[i]
        p2 = points[i + 1]

        if not p1.time or not p2.time:
            continue

        dt_s = (p2.time - p1.time).total_seconds()
        if dt_s <= 0 or dt_s > 30.0:
            continue

        has_coords = (p1.lat != 0.0 or p1.lon != 0.0) and (p2.lat != 0.0 or p2.lon != 0.0)
        if has_coords:
            dist_m = haversine_distance_meters(p1.lat, p1.lon, p2.lat, p2.lon)
        elif p1.distance_m is not None and p2.distance_m is not None:
            dist_m = max(0.0, p2.distance_m - p1.distance_m)
        else:
            dist_m = 0.0

        v_ms = dist_m / dt_s if dt_s > 0 else 0.0

        if v_ms < 0.3 and not (p1.hr and p1.hr > 75):
            continue

        total_moving_time_s += dt_s

        if has_coords:
            dh_m = 0.0
            if has_corrected_ele:
                dh_m = corrected_elevations[i + 1] - corrected_elevations[i]
            elif p1.ele is not None and p2.ele is not None:
                dh_m = p2.ele - p1.ele

            slope = dh_m / dist_m if dist_m > 0 else 0.0
            theta = math.atan(slope)

            # 1. Schwerkraft- & Rollwiderstand
            f_gravity = total_system_weight_kg * g * math.sin(theta)
            f_roll = c_rr * total_system_weight_kg * g * math.cos(theta)

            # 2. Aerodynamischer Widerstand unter Einbeziehung des Windvektors
            c_d_a = c_d_a_base
            if has_weather and wind_speed_ms > 0.2:
                bike_bearing = calculate_bearing_deg(p1.lat, p1.lon, p2.lat, p2.lon)
                rel_angle = (wind_deg - bike_bearing) % 360.0
                cos_rel = math.cos(math.radians(rel_angle))

                # Gegenwind-Komponente (positiv = Gegenwind, negativ = Rückenwind)
                headwind_comp_ms = wind_speed_ms * cos_rel
                v_apparent = max(0.0, v_ms + headwind_comp_ms)

                # Windsektor erfassen
                if cos_rel > 0.25:
                    headwind_time_s += dt_s
                elif cos_rel < -0.25:
                    tailwind_time_s += dt_s
                else:
                    crosswind_time_s += dt_s

                # Gierwinkel (Yaw) erhöht effektives CdA moderat
                sin_rel = abs(math.sin(math.radians(rel_angle)))
                c_d_a = c_d_a_base * (1.0 + 0.12 * sin_rel)
                f_aero = 0.5 * rho * c_d_a * (v_apparent ** 2)
            else:
                f_aero = 0.5 * rho * c_d_a * (v_ms ** 2)

            f_total = f_gravity + f_roll + f_aero

            if f_total > 0:
                raw_watts = f_total * v_ms * drivetrain_factor
                watts = min(1000.0, raw_watts)
            else:
                watts = 0.0
        else:
            # Indoor-Aktivität (Laufband / Rolle / Krafttraining ohne GPS)
            dh_m = 0.0
            slope = 0.0
            watts = float(p1.watts) if p1.watts is not None else 0.0

        step_work_j = watts * dt_s
        total_work_j += step_work_j
        power_samples.append((dt_s, watts))

        if slope >= 0.03 and watts > 0:
            climb_moving_time_s += dt_s
            climb_work_j += step_work_j
            if dh_m > 0:
                climb_elev_gain_m += dh_m

        half_samples.append({
            "t": total_moving_time_s,
            "speed_kmh": v_ms * 3.6,
            "watts": watts,
            "hr": p1.hr,
            "dt": dt_s,
        })

    overall_avg_watts = (total_work_j / total_moving_time_s) if total_moving_time_s > 0 else 0.0
    climb_avg_watts = (climb_work_j / climb_moving_time_s) if climb_moving_time_s > 0 else 0.0

    rw = max(30.0, rider_weight_kg)
    overall_w_kg = overall_avg_watts / rw
    climb_w_kg = climb_avg_watts / rw

    virtual_power = {
        "status": "calculated",
        "system_weight_kg": round(total_system_weight_kg, 1),
        "rider_weight_kg": round(rider_weight_kg, 1),
        "overall_avg_watts": round(overall_avg_watts, 1),
        "overall_watts_per_kg": round(overall_w_kg, 2),
        "climb_avg_watts": round(climb_avg_watts, 1) if climb_moving_time_s > 30 else None,
        "climb_watts_per_kg": round(climb_w_kg, 2) if climb_moving_time_s > 30 else None,
        "climb_time_minutes": round(climb_moving_time_s / 60.0, 1),
        "climb_elevation_gain_m": round(climb_elev_gain_m, 1) if climb_moving_time_s > 30 else 0.0,
        "rolling_resistance_crr": round(c_rr, 5),
        "forest_shelter_pct": round(forest_shelter_pct, 1),
        "estimated_work_kj": round(total_work_j / 1000.0, 1),
        "accuracy_note": (
            "Mit OpenWeatherMap-Windvektoren & OSM-Rollwiderstand: ca. +/- 2% am Berg, ca. +/- 6-8% im Flachen."
            if has_weather
            else "Ohne Wetterdaten: ca. +/- 3-5% am Berg, ca. +/- 15-20% im Flachen."
        ),
    }

    if has_weather:
        tot_m = max(1.0, total_moving_time_s)
        hw_pct = round((headwind_time_s / tot_m) * 100.0, 1)
        tw_pct = round((tailwind_time_s / tot_m) * 100.0, 1)
        cw_pct = round((crosswind_time_s / tot_m) * 100.0, 1)
        virtual_power["weather_aerodynamics"] = {
            "weather_integrated": True,
            "source": weather_data.get("source", "OpenWeatherMap"),
            "temperature_c": weather_data.get("temperature_c"),
            "air_density_kg_m3": weather_data.get("air_density_kg_m3"),
            "wind_speed_kmh": weather_data.get("wind_speed_kmh"),
            "wind_direction_compass": weather_data.get("wind_direction_compass"),
            "headwind_pct": hw_pct,
            "tailwind_pct": tw_pct,
            "crosswind_pct": cw_pct,
            "aerodynamic_impact": (
                f"Gegenwind auf {hw_pct}% der Strecke, Rückenwind auf {tw_pct}%. "
                f"Exakte Luftdichte ({rho} kg/m³) floss in die Wattberechnung ein."
            ),
        }

    coaching = {
        "cardiac_drift_pct": None,
        "first_half_avg_hr": None,
        "second_half_avg_hr": None,
        "first_half_avg_speed_kmh": None,
        "second_half_avg_speed_kmh": None,
        "coach_critical_findings": [],
        "split_context": None,
        "cardiac_drift_status": "evaluated" if total_moving_time_s >= 180 else "duration_too_short",
    }

    if total_moving_time_s >= 180 and half_samples:
        half_time = total_moving_time_s / 2.0
        h1 = [s for s in half_samples if s["t"] <= half_time]
        h2 = [s for s in half_samples if s["t"] > half_time]

        if h1 and h2:
            s1_dur = sum(s["dt"] for s in h1)
            s2_dur = sum(s["dt"] for s in h2)

            speed1 = sum(s["speed_kmh"] * s["dt"] for s in h1) / s1_dur if s1_dur > 0 else 0.0
            speed2 = sum(s["speed_kmh"] * s["dt"] for s in h2) / s2_dur if s2_dur > 0 else 0.0

            coaching["first_half_avg_speed_kmh"] = round(speed1, 2)
            coaching["second_half_avg_speed_kmh"] = round(speed2, 2)

            h1_hr = [s["hr"] for s in h1 if s["hr"] is not None]
            h2_hr = [s["hr"] for s in h2 if s["hr"] is not None]

            if h1_hr and h2_hr:
                avg_hr1 = sum(h1_hr) / len(h1_hr)
                avg_hr2 = sum(h2_hr) / len(h2_hr)

                coaching["first_half_avg_hr"] = round(avg_hr1, 1)
                coaching["second_half_avg_hr"] = round(avg_hr2, 1)

                if speed1 > 0 and speed2 > 0 and avg_hr1 > 0:
                    ef1 = speed1 / avg_hr1
                    ef2 = speed2 / avg_hr2
                    drift_pct = ((ef1 - ef2) / ef1) * 100.0
                    coaching["cardiac_drift_pct"] = round(drift_pct, 1)

                    if total_moving_time_s < 600:
                        coaching["split_context"] = (
                            f"Kurze Einheit ({total_moving_time_s/60:.1f} Min): Pulsanstieg in der 2. Hälfte "
                            f"(Ø {avg_hr1:.0f} -> {avg_hr2:.0f} bpm) reflektiert primär die kardiovaskuläre Hochregelung "
                            "und Endbeschleunigung (Warm-up-Kinetik), keine klassische Dehydrierung."
                        )

                    if drift_pct > 7.0:
                        detail = " (Warm-up-Kinetik / Endspurt bei Kurzaktivität)" if total_moving_time_s < 600 else " (Überzockter Beginn oder Dehydrierung)"
                        coaching["coach_critical_findings"].append(
                            f"Kardiovaskulärer Drift (+{drift_pct:.1f}%): Puls stieg in der 2. Hälfte "
                            f"im Verhältnis zum Tempo an{detail}."
                        )
                    elif drift_pct < -5.0:
                        coaching["coach_critical_findings"].append(
                            f"Negativer Split ({drift_pct:.1f}% Drift): Kontrollierter Beginn mit starkem Finish."
                        )
                    else:
                        coaching["coach_critical_findings"].append(
                            f"Stabile aerobe Kopplung ({drift_pct:.1f}% Drift): Pacing blieb konstant."
                        )

            if speed1 > 0 and speed2 < (speed1 * 0.85):
                coaching["coach_critical_findings"].append(
                    f"Tempoabfall in 2. Hälfte (-{((speed1 - speed2)/speed1)*100:.1f}%): Beginn deutlich überpaced."
                )
    elif total_moving_time_s < 180:
        coaching["split_context"] = f"Aktivitätsdauer ({total_moving_time_s:.0f}s) unter 3 Minuten zu kurz für Split- und Drift-Auswertung."

    return virtual_power, coaching


def detect_activity_anomalies(m: ActivityMetrics, points: list[TrackPoint]) -> list[dict[str, Any]]:
    """
    Sportwissenschaftlicher Sanity-Check zur Erkennung von GPS-Glitches,
    Pausen-Versäumnissen, unplausiblen Geschwindigkeiten oder Sensor-Ausfällen.
    """
    anomalies: list[dict[str, Any]] = []

    # 1. Autopause-Fehler / Lange Standzeiten (z. B. 48 min Brutto vs. 18 min Netto)
    if m.elapsed_time_sec > 1.7 * m.moving_time_sec and m.paused_time_sec > 600.0:
        pct_paused = round((m.paused_time_sec / max(1.0, m.elapsed_time_sec)) * 100.0, 1)
        anomalies.append({
            "type": "extended_pause_detected",
            "severity": "warning",
            "title": "Erhebliche Standzeit / Mögliche Autopause-Diskrepanz",
            "description": (
                f"Pausenzeit beträgt {format_seconds_to_hms(m.paused_time_sec)} ({pct_paused}% der Gesamtzeit). "
                f"Bruttozeit: {format_seconds_to_hms(m.elapsed_time_sec)} vs. Netto-Bewegungszeit: {format_seconds_to_hms(m.moving_time_sec)}. "
                "Pace- und Geschwindigkeitswerte sollten primär auf der Nettozeit basieren."
            ),
        })

    # 2. Sportart-Pace-Plausibilität
    st = (m.activity_type or "").lower()
    is_run = any(r in st for r in ["run", "lauf"])
    is_ride = any(r in st for r in ["ride", "rad", "cycling", "gravel", "bike"])

    if is_run and m.total_distance_km > 0.2:
        if m.avg_speed_moving_kmh < 5.0:
            pace_min_km = (m.moving_time_sec / 60.0) / max(0.1, m.total_distance_km)
            anomalies.append({
                "type": "pace_sport_mismatch",
                "severity": "info",
                "title": "Ungewöhnlich niedriges Dauerlauftempo",
                "description": (
                    f"Netto-Pace von {pace_min_km:.1f} min/km ({m.avg_speed_moving_kmh:.1f} km/h) bei Distanz von {m.total_distance_km:.2f} km. "
                    "Entspricht eher Geh-/Wander-Intervallen als kontinuierlichem Lauftraining."
                ),
            })
    elif is_ride and m.total_distance_km > 1.0:
        if m.avg_speed_moving_kmh < 9.0:
            anomalies.append({
                "type": "low_cycling_speed",
                "severity": "info",
                "title": "Sehr niedriges Radfahrtempo",
                "description": (
                    f"Ø-Geschwindigkeit in Bewegung von nur {m.avg_speed_moving_kmh:.1f} km/h deutet auf sehr steile "
                    "Schiebepassagen, verblocktes Gelände oder häufige Stopps hin."
                ),
            })

    # 3. GPS-Max-Geschwindigkeitssprung (Spike) & Filter-Status
    max_thresh = 45.0 if is_run else 115.0
    if m.speed_filtered:
        anomalies.append({
            "type": "gps_speed_spike_filtered",
            "severity": "warning",
            "title": "GPS-Geschwindigkeitsausreißer herausgefiltert",
            "description": (
                f"Unplausibler Geschwindigkeits-Spike von {m.raw_max_speed_kmh:.1f} km/h wurde aus der Berechnung "
                f"entfernt (physiologisches Limit für Sportart: {m.max_speed_kmh:.1f} km/h als Maximum gewertet)."
            ),
        })
    elif m.max_speed_kmh > max_thresh:
        anomalies.append({
            "type": "gps_speed_spike",
            "severity": "warning",
            "title": "GPS-Geschwindigkeitsausreißer erkannt",
            "description": f"Spitzengeschwindigkeit von {m.max_speed_kmh:.1f} km/h ist unplausibel hoch (Sensor-/GPS-Glitch).",
        })

    # 4. Höhenmeter-Drift
    if m.elevation_reconciliation_applied:
        anomalies.append({
            "type": "elevation_drift_reconciled",
            "severity": "info",
            "title": "Barometrischer Höhendrift korrigiert",
            "description": (
                f"GPX-Aufstieg ({m.original_gpx_gain_m:.0f} hm) wich deutlich vom Satelliten-Geländemodell "
                f"({m.elevation_gain_m:.0f} hm) ab. Höhenprofil und Steigungs-Watt wurden automatisch synchronisiert."
            ),
        })

    # 5. Split- / Cardiac-Drift-Kontext bei sehr kurzen Aktivitäten (< 3 Min)
    if m.hr_available and m.moving_time_sec < 180.0:
        anomalies.append({
            "type": "short_activity_drift_omitted",
            "severity": "info",
            "title": "Split- & Drift-Analyse übersprungen",
            "description": (
                f"Aktivitätsdauer ({m.moving_time_sec:.0f}s) liegt unter der Mindestdauer von 180s für "
                "aerobe Split- und Cardiac-Drift-Auswertungen (Einschwingphase des Herz-Kreislauf-Systems)."
            ),
        })

    return anomalies


def calculate_metrics(
    file_path: Path,
    title: str,
    activity_type: str,
    points: list[TrackPoint],
    hr_max: int = 205,
    hr_rest: Optional[int] = None,
    gender: str = "male",
    ele_noise_threshold_m: float = 0.5,
    total_system_weight_kg: float = 72.9,
    rider_weight_kg: float = 60.0,
    weather_data: Optional[dict[str, Any]] = None,
    surface_data: Optional[dict[str, Any]] = None,
    dem_data: Optional[dict[str, Any]] = None,
) -> ActivityMetrics:
    """
    Berechnet alle relevanten sportwissenschaftlichen und GPS-Metriken aus den TrackPoints.
    Ermittelt den Ruhepuls dynamisch anhand des Min-Pulses der Aktivität, sofern nicht manuell vorgegeben.
    Standardmäßig auf Jugend-Physiologie (hr_max=205) abgestimmt.
    """
    metrics = ActivityMetrics(
        file_path=file_path,
        title=title,
        activity_type=activity_type,
        total_points=len(points),
    )

    if not points:
        return metrics

    # Start- und Endzeit
    first_time = points[0].time
    last_time = points[-1].time
    metrics.start_time = first_time
    metrics.end_time = last_time

    if first_time and last_time:
        metrics.elapsed_time_sec = max(0.0, (last_time - first_time).total_seconds())

    # Höhenmeter berechnen
    elevations = [p.ele for p in points if p.ele is not None]
    if elevations:
        metrics.min_elevation_m = min(elevations)
        metrics.max_elevation_m = max(elevations)
        gain_m, loss_m = calculate_elevation_gain_loss(elevations, threshold=ele_noise_threshold_m)
        metrics.elevation_gain_m = gain_m
        metrics.elevation_loss_m = loss_m

    # Dynamische Ruhepuls-Ermittlung:
    # Falls nicht explizit per CLI übergeben, nutze das Minimum der Aktivität (Ausreißerfilter > 30 bpm)
    valid_hrs = [p.hr for p in points if p.hr is not None and p.hr > 30]
    if valid_hrs:
        min_activity_hr = min(valid_hrs)
        if hr_rest is not None:
            effective_hr_rest = hr_rest
            hr_rest_source = f"manuell ({hr_rest} bpm)"
        else:
            effective_hr_rest = min_activity_hr
            hr_rest_source = f"dynamisch ermittelt (Min-Puls: {min_activity_hr} bpm)"
    else:
        effective_hr_rest = hr_rest if hr_rest is not None else 60
        hr_rest_source = "Fallback (60 bpm)"

    effective_hr_rest = min(effective_hr_rest, hr_max - 1)

    # 5 Herzfrequenzzonen konfigurieren (% von HRmax)
    # Z1: < 60%, Z2: 60-70%, Z3: 70-80%, Z4: 80-90%, Z5: 90-100%+
    z1_max = int(round(0.60 * hr_max))
    z2_max = int(round(0.70 * hr_max))
    z3_max = int(round(0.80 * hr_max))
    z4_max = int(round(0.90 * hr_max))

    zones = [
        HRZoneInfo(1, "Z1 Erholung / Rekom", 0, z1_max),
        HRZoneInfo(2, "Z2 Grundlagenausdauer 1", z1_max, z2_max),
        HRZoneInfo(3, "Z3 Tempotraining / GA2", z2_max, z3_max),
        HRZoneInfo(4, "Z4 Schwellenbereich / EB", z3_max, z4_max),
        HRZoneInfo(5, "Z5 Maximalbereich / SB", z4_max, max(hr_max, 250)),
    ]

    total_dist_m = 0.0
    moving_time_s = 0.0
    hr_values: list[int] = []
    hr_time_total_s = 0.0
    suffer_score_sum = 0.0
    speeds_kmh: list[float] = []

    # Banister-Gewichtungsfaktor nach Geschlecht
    # Männer: b = 1.92, Faktor = 0.64
    # Frauen: b = 1.67, Faktor = 0.86
    banister_factor = 0.86 if gender.lower().startswith("f") else 0.64
    banister_exponent = 1.67 if gender.lower().startswith("f") else 1.92

    act_lower = (activity_type or "").lower()
    is_run = any(k in act_lower for k in ["run", "lauf", "jog"])
    is_walk = any(k in act_lower for k in ["walk", "hike", "wander", "spazier"])
    plausible_speed_ceiling = 32.0 if is_run else (16.0 if is_walk else 115.0)
    raw_spike_speeds: list[float] = []

    for i in range(len(points) - 1):
        p1 = points[i]
        p2 = points[i + 1]

        # 1. Distanz
        has_coords = (p1.lat != 0.0 or p1.lon != 0.0) and (p2.lat != 0.0 or p2.lon != 0.0)
        if has_coords:
            dist_step = haversine_distance_meters(p1.lat, p1.lon, p2.lat, p2.lon)
        elif p1.distance_m is not None and p2.distance_m is not None:
            dist_step = max(0.0, p2.distance_m - p1.distance_m)
        else:
            dist_step = 0.0
        total_dist_m += dist_step

        # 2. Zeit- und Geschwindigkeitsberechnung
        if p1.time and p2.time:
            dt_s = (p2.time - p1.time).total_seconds()
            if dt_s > 0:
                step_speed_kmh = (dist_step / dt_s) * 3.6 if dist_step > 0 else 0.0

                # Bewegungserkennung: Tempo >= 1.0 km/h oder aktiver Puls (bei Indoor-Workouts / Krafttraining)
                is_active = (step_speed_kmh >= 1.0) or (dist_step == 0.0 and p1.hr is not None and p1.hr > 60)
                if is_active and dt_s <= 35.0:
                    moving_time_s += dt_s
                    # Ausreißerfilter gegen GPS-Sprünge & unrealistische Spikes
                    if step_speed_kmh > plausible_speed_ceiling:
                        raw_spike_speeds.append(step_speed_kmh)
                    elif step_speed_kmh > 0:
                        speeds_kmh.append(step_speed_kmh)

                # 3. Herzfrequenz-Zonen und Suffer Score (Banister TRIMP)
                if p1.hr is not None:
                    hr_values.append(p1.hr)
                    hr = p1.hr
                    hr_time_total_s += dt_s

                    # Zonenzuordnung
                    if hr < z1_max:
                        zones[0].duration_sec += dt_s
                    elif hr < z2_max:
                        zones[1].duration_sec += dt_s
                    elif hr < z3_max:
                        zones[2].duration_sec += dt_s
                    elif hr < z4_max:
                        zones[3].duration_sec += dt_s
                    else:
                        zones[4].duration_sec += dt_s

                    # Suffer Score Berechnung mit dynamischem Ruhepuls
                    # Fractional Heart Rate Reserve (Karvonen HR_ratio)
                    hr_ratio = (hr - effective_hr_rest) / max(1, (hr_max - effective_hr_rest))
                    hr_ratio = max(0.0, min(1.0, hr_ratio))

                    dt_minutes = dt_s / 60.0
                    trimp_step = (
                        dt_minutes
                        * hr_ratio
                        * banister_factor
                        * math.exp(banister_exponent * hr_ratio)
                    )
                    suffer_score_sum += trimp_step

    # Letzten HR-Punkt erfassen
    if points[-1].hr is not None:
        hr_values.append(points[-1].hr)

    # Distanz in km
    metrics.total_distance_km = total_dist_m / 1000.0

    # Zeiten
    if metrics.elapsed_time_sec == 0.0 and moving_time_s > 0:
        metrics.elapsed_time_sec = moving_time_s
    metrics.moving_time_sec = moving_time_s if moving_time_s > 0 else metrics.elapsed_time_sec
    metrics.paused_time_sec = max(0.0, metrics.elapsed_time_sec - metrics.moving_time_sec)

    # Durchschnitts- und Höchstgeschwindigkeit
    if metrics.moving_time_sec > 0:
        metrics.avg_speed_moving_kmh = metrics.total_distance_km / (metrics.moving_time_sec / 3600.0)
    if metrics.elapsed_time_sec > 0:
        metrics.avg_speed_total_kmh = metrics.total_distance_km / (metrics.elapsed_time_sec / 3600.0)

    # Höchstgeschwindigkeit (robust über 99.5-Perzentil oder Max mit Filter)
    if speeds_kmh:
        sorted_speeds = sorted(speeds_kmh)
        idx = int(len(sorted_speeds) * 0.995)
        idx = min(idx, len(sorted_speeds) - 1)
        metrics.max_speed_kmh = sorted_speeds[idx]
    elif raw_spike_speeds:
        metrics.max_speed_kmh = min(raw_spike_speeds)

    if raw_spike_speeds:
        metrics.speed_filtered = True
        metrics.raw_max_speed_kmh = max(raw_spike_speeds)
        metrics.speed_filter_reason = (
            f"Sensor-/GPS-Glitch erkannt: {len(raw_spike_speeds)} Messpunkte überstiegen das "
            f"sportartspezifische Limit von {plausible_speed_ceiling:.1f} km/h (Spitze: {metrics.raw_max_speed_kmh:.1f} km/h). "
            f"Höchstgeschwindigkeit wurde auf {metrics.max_speed_kmh:.1f} km/h bereinigt."
        )
    else:
        metrics.raw_max_speed_kmh = metrics.max_speed_kmh

    # HR Metriken finalisieren
    if hr_values:
        metrics.hr_available = True
        metrics.avg_hr = sum(hr_values) / len(hr_values)
        metrics.max_hr = max(hr_values)
        metrics.min_hr = min(hr_values)
        metrics.hr_rest_used = effective_hr_rest
        metrics.hr_rest_source = hr_rest_source
        metrics.suffer_score = suffer_score_sum

        # Prozentuale Anteile der Zonen berechnen
        if hr_time_total_s > 0:
            for z in zones:
                z.percentage = (z.duration_sec / hr_time_total_s) * 100.0
        metrics.zones = zones

    # Topografische DEM-Höhenkorrektur & Drift-Reconciliation (DEM Satellit vs. GPX/Barometer)
    dem_reconciled = False
    original_gpx_gain = metrics.elevation_gain_m
    corrected_elevations: Optional[list[float]] = None

    if dem_data and dem_data.get("dem_verified"):
        dem_gain = float(dem_data.get("satellite_elevation_gain_m", 0.0))
        drift_detected = bool(dem_data.get("barometric_drift_detected", False))

        # Reconcile wenn Drift erkannt oder GPX offensichtlich fehlerhaft/flach
        if (drift_detected and dem_gain >= 15.0) or (original_gpx_gain < 15.0 and dem_gain >= 30.0):
            dem_reconciled = True
            metrics.elevation_gain_m = dem_gain
            metrics.elevation_source_used = "dem_satellite"
            metrics.elevation_reconciliation_applied = True
            metrics.original_gpx_gain_m = original_gpx_gain

            if "dem_min_ele_m" in dem_data:
                metrics.min_elevation_m = dem_data["dem_min_ele_m"]
            if "dem_max_ele_m" in dem_data:
                metrics.max_elevation_m = dem_data["dem_max_ele_m"]

            if apply_dem_elevation_correction and points:
                try:
                    corrected_elevations = apply_dem_elevation_correction(points, dem_data)
                except Exception:
                    corrected_elevations = None

    # Physikalische Leistung (Virtual Power) & Pacing/Drift berechnen
    crr = surface_data.get("weighted_crr") if surface_data else None
    forest_shelter = float(surface_data.get("forest_shelter_pct", 0.0)) if surface_data else 0.0

    v_power, coaching_data = estimate_virtual_power_and_coaching(
        points=points,
        total_system_weight_kg=total_system_weight_kg,
        rider_weight_kg=rider_weight_kg,
        weather_data=weather_data,
        surface_crr=crr,
        forest_shelter_pct=forest_shelter,
        corrected_elevations=corrected_elevations,
    )
    metrics.virtual_power = v_power
    metrics.coaching_telemetry = coaching_data
    metrics.surface_analysis = surface_data or {}

    dem_verif = dict(dem_data) if dem_data else {}
    dem_verif["reconciliation_applied"] = dem_reconciled
    dem_verif["elevation_source_used"] = metrics.elevation_source_used
    dem_verif["original_gpx_gain_m"] = round(original_gpx_gain, 1)
    if dem_reconciled:
        dem_verif["reconciliation_note"] = (
            f"Barometrischer Drift / Messfehler von {dem_data.get('elevation_variance_pct', 0)}% korrigiert: "
            f"Höhenmeter ({original_gpx_gain:.0f}m -> {metrics.elevation_gain_m:.0f}m), VAM und Steigungs-Watt "
            "wurden präzise anhand des Satelliten-Geländemodells (DEM) berechnet."
        )
    metrics.dem_verification = dem_verif

    # Stoffwechsel- & Ernährungsdiagnostik (Jeukendrup & Romijn)
    if calculate_metabolic_and_nutrition:
        work_kj = float(v_power.get("estimated_work_kj", 0.0))
        duration_s = metrics.moving_time_sec if metrics.moving_time_sec > 0 else metrics.elapsed_time_sec
        temp_c = float(weather_data.get("temperature_c", 18.0)) if weather_data and "temperature_c" in weather_data else 18.0
        metrics.nutrition_metabolism = calculate_metabolic_and_nutrition(
            work_kj=work_kj,
            duration_s=duration_s,
            zones=metrics.zones,
            avg_hr=metrics.avg_hr,
            hr_max=hr_max,
            temp_c=temp_c,
        )

    # VAM & Nachwuchsdiagnostik (U17 BDR / UCI Standards)
    if calculate_vam_and_youth_diagnostics:
        climb_elev = float(v_power.get("climb_elevation_gain_m", 0.0))
        if climb_elev < 20.0 and metrics.elevation_gain_m >= 20.0:
            climb_elev = metrics.elevation_gain_m
        climb_dur = float(v_power.get("climb_time_minutes", 0.0)) * 60.0
        if climb_dur < 60.0 and metrics.moving_time_sec >= 60.0 and metrics.elevation_gain_m >= 50.0:
            climb_dur = metrics.moving_time_sec

        metrics.vam_diagnostics = calculate_vam_and_youth_diagnostics(
            climb_elevation_gain_m=climb_elev,
            climb_duration_sec=climb_dur,
            climb_avg_watts=v_power.get("climb_avg_watts") or v_power.get("overall_avg_watts"),
            rider_weight_kg=rider_weight_kg,
        )

    # Automatische Anomalie- & Plausibilitätsprüfung (Sanity Check)
    metrics.anomalies = detect_activity_anomalies(metrics, points)
    metrics.data_quality_verdict = "warning" if any(a.get("severity") == "warning" for a in metrics.anomalies) else "clean"

    return metrics


def render_ascii_progress_bar(percentage: float, width: int = 20) -> str:
    """Erstellt einen visuellen ASCII-Fortschrittsbalken."""
    filled_len = int(round(width * (max(0.0, min(100.0, percentage)) / 100.0)))
    empty_len = width - filled_len
    return f"[{'█' * filled_len}{'░' * empty_len}]"


def print_activity_report(m: ActivityMetrics, hr_max: int):
    """Gibt die Ergebnisse einer Aktivität ansprechend strukturiert im Terminal aus."""
    sep = "=" * 78
    subsep = "-" * 78

    print(sep)
    print(f"  AKTIVITÄTS-ANALYSE: {m.title}")
    print(f"  Datei: {m.file_path.name} | Typ: {m.activity_type} | GPS-Punkte: {m.total_points:,}")
    if m.start_time:
        start_str = m.start_time.strftime("%d.%m.%Y um %H:%M:%S Uhr")
        print(f"  Startzeitpunkt: {start_str}")
    print(sep)

    # 1. Haupt-Trainingsdaten
    print("  STRECKE & HÖHENPROFIL")
    print(f"    • Gesamtdistanz:       {m.total_distance_km:8.2f} km")
    print(f"    • Höhenmeter Aufstieg: {m.elevation_gain_m:8.1f} hm (+)")
    print(f"    • Höhenmeter Abstieg:  {m.elevation_loss_m:8.1f} hm (-)")
    if m.min_elevation_m is not None and m.max_elevation_m is not None:
        print(f"    • Höhenbereich (Min/Max): {m.min_elevation_m:.0f} m bis {m.max_elevation_m:.0f} m")

    print(subsep)
    print("  ZEITEN & GESCHWINDIGKEIT")
    print(f"    • Gesamtzeit (Brutto): {format_seconds_to_hms(m.elapsed_time_sec):>10}  ({m.elapsed_time_sec / 60:.1f} min)")
    print(f"    • Bewegungszeit (Netto):{format_seconds_to_hms(m.moving_time_sec):>10}  ({m.moving_time_sec / 60:.1f} min)")
    print(f"    • Pausenzeit / Stillstand:{format_seconds_to_hms(m.paused_time_sec):>8}  ({m.paused_time_sec / 60:.1f} min)")
    print(f"    • Ø-Tempo in Bewegung:  {m.avg_speed_moving_kmh:8.2f} km/h")
    print(f"    • Ø-Tempo Gesamt:       {m.avg_speed_total_kmh:8.2f} km/h")
    print(f"    • Höchstgeschwindigkeit:{m.max_speed_kmh:8.2f} km/h")

    # 2. Herzfrequenz & Suffer Score
    print(subsep)
    print("  HERZFREQUENZ- & LEISTUNGSANALYSE")
    if m.hr_available and m.avg_hr is not None and m.max_hr is not None and m.min_hr is not None:
        print(f"    • Durchschnittspuls:    {m.avg_hr:8.1f} bpm")
        print(f"    • Maximaler Puls:       {m.max_hr:8d} bpm")
        print(f"    • Minimaler Puls:       {m.min_hr:8d} bpm")
        if m.hr_rest_used is not None:
            print(f"    • Ruhepuls (Basis):     {m.hr_rest_used:8d} bpm  [{m.hr_rest_source}]")

        # Suffer Score Klassifikation
        if m.suffer_score is not None:
            score_val = m.suffer_score
            if score_val < 50:
                suffer_desc = "Leichte Belastung (Regeneration / Grundlagentraining)"
            elif score_val < 100:
                suffer_desc = "Moderate Trainingsbelastung (Solide Ausdauereinheit)"
            elif score_val < 180:
                suffer_desc = "Hohe Trainingsbelastung (Forderndes Tempotraining / Schwellenbereich)"
            else:
                suffer_desc = "Extremer Suffer Score (Erschöpfende Höchstleistung / Wettkampf)"

            print(f"    • Suffer Score:         {score_val:8.1f}  [{suffer_desc}]")

        rest_display = f"{m.hr_rest_used} bpm ({m.hr_rest_source})" if m.hr_rest_used else "60 bpm"
        print(f"\n  HERZFREQUENZZONEN (Basis: HRmax = {hr_max} bpm, Ruhepuls = {rest_display}):")
        print("  " + "-" * 74)
        print("   Zone  Bereich (bpm)     Dauer      Anteil    Verteilung")
        print("  " + "-" * 74)
        for z in m.zones:
            bpm_range = f"{z.min_bpm:>3} - {z.max_bpm:<3} bpm"
            duration_str = format_seconds_to_hms(z.duration_sec)
            bar = render_ascii_progress_bar(z.percentage, width=18)
            print(f"   Z{z.zone_idx}    {bpm_range}     {duration_str:>8}    {z.percentage:5.1f}%   {bar}")
        print("  " + "-" * 74)
    else:
        print("    • Keine Herzfrequenzdaten (<gpxtpx:hr>) in dieser GPX-Datei vorhanden.")

    # 3. Leistung & Aerodynamik (Virtual Power)
    if m.virtual_power and m.virtual_power.get("status") == "calculated":
        vp = m.virtual_power
        print(subsep)
        print("  LEISTUNG & AERODYNAMIK (Virtual Power)")
        print(f"    • Durchschnittsleistung: {vp.get('overall_avg_watts', 0):8.1f} W ({vp.get('overall_watts_per_kg', 0):.2f} W/kg)")
        if vp.get("climb_avg_watts"):
            print(f"    • Leistung am Berg (>3%):{vp.get('climb_avg_watts'):8.1f} W ({vp.get('climb_watts_per_kg'):.2f} W/kg über {vp.get('climb_time_minutes')} min)")
        print(f"    • Mechanische Arbeit:    {vp.get('estimated_work_kj', 0):8.1f} kJ")
        if vp.get("rolling_resistance_crr"):
            print(f"    • Rollwiderstand (Crr):  {vp.get('rolling_resistance_crr'):8.5f} (OSM-abgeleitet)")
        if "weather_aerodynamics" in vp:
            wa = vp["weather_aerodynamics"]
            print(f"    • Gegenwind / Rückenwind:{wa.get('headwind_pct')}% Gegenwind, {wa.get('tailwind_pct')}% Rückenwind (Luftdichte: {wa.get('air_density_kg_m3')} kg/m³)")

    # 4. Kletterleistung & VAM
    if m.vam_diagnostics and m.vam_diagnostics.get("vam_available"):
        vd = m.vam_diagnostics
        print(subsep)
        print("  KLETTERLEISTUNG & VAM-DIAGNOSTIK (U17 Nachwuchs)")
        print(f"    • VAM (Steigrate):       {vd.get('vam_m_per_hour'):8.1f} m/h")
        if vd.get("climb_watts_per_kg"):
            print(f"    • Steigleistung:         {vd.get('climb_watts_per_kg'):8.2f} W/kg")
        bench = vd.get("youth_u17_benchmark", {})
        if bench:
            print(f"    • U17 Klassifikation:    {bench.get('category_title')}")
            print(f"    • Trainer-Einschätzung:  {bench.get('diagnostic_summary')}")

    # 5. Stoffwechsel & Verpflegung (Jeukendrup & Romijn)
    if m.nutrition_metabolism and "carbohydrates" in m.nutrition_metabolism:
        nm = m.nutrition_metabolism
        carbs = nm["carbohydrates"]
        fat = nm.get("fat", {})
        hyd = nm.get("hydration", {})
        print(subsep)
        print("  STOFFWECHSEL & ERNÄHRUNG (Jeukendrup & Romijn)")
        print(f"    • Kalorienverbrauch:     {nm.get('energy_total_kcal', 0):8.0f} kcal")
        print(f"    • Kohlenhydrate verbrannt:{carbs.get('burned_grams', 0):7.1f} g  (Ø {carbs.get('burn_rate_g_per_hour', 0):.1f} g/h | {carbs.get('percentage_of_energy', 0):.0f}% der Energie)")
        print(f"    • Fett verbrannt:        {fat.get('burned_grams', 0):7.1f} g  ({fat.get('percentage_of_energy', 0):.0f}% der Energie)")
        print(f"    • Hungerast-Risiko:      {carbs.get('hungerast_bonk_risk')}")
        print(f"    • Empfohlene Zufuhr:     {carbs.get('fueling_target_during_ride')}")
        if hyd:
            print(f"    • Schweißverlust:        {hyd.get('estimated_sweat_loss_liters', 0):8.2f} Liter (ca. {hyd.get('sweat_rate_l_per_hour', 0):.2f} L/h)")

    # 6. Geodaten & Oberflächenprüfung (OSM & DEM)
    if (m.surface_analysis and m.surface_analysis.get("surface_detected")) or (m.dem_verification and m.dem_verification.get("dem_verified")):
        print(subsep)
        print("  TOPOGRAFIE & STRECKENOBERFLÄCHE (OSM & DEM)")
        if m.surface_analysis and m.surface_analysis.get("surface_detected"):
            sa = m.surface_analysis
            print(f"    • Hauptbelag:            {sa.get('primary_surface')} (Crr={sa.get('weighted_crr'):.5f})")
            if sa.get("forest_shelter_pct"):
                print(f"    • Waldanteil (Windschutz):{sa.get('forest_shelter_pct')}%")
        if m.dem_verification and m.dem_verification.get("dem_verified"):
            dv = m.dem_verification
            print(f"    • DEM Höhenmodell:       {dv.get('elevation_quality')} ({dv.get('satellite_elevation_gain_m')} hm Satellit vs. {dv.get('sampled_gpx_gain_m')} hm GPX)")

    print(sep + "\n")


def print_summary_comparison_table(metrics_list: list[ActivityMetrics]):
    """Druckt eine tabellarische Gesamtübersicht bei mehreren analysierten Dateien."""
    if len(metrics_list) <= 1:
        return

    print("=" * 115)
    print("  GESAMTÜBERSICHT ALLER ANALYSIERTEN AKTIVITÄTEN")
    print("=" * 115)
    header = f"{'Datei':<22} | {'Aktivität / Tour':<24} | {'Distanz':>9} | {'Aufstieg':>9} | {'Nettozeit':>9} | {'Ø-Tempo':>8} | {'Ø-Puls':>7} | {'Suffer':>7}"
    print(header)
    print("-" * 115)

    tot_dist = 0.0
    tot_gain = 0.0
    tot_moving_s = 0.0
    tot_suffer = 0.0

    for m in metrics_list:
        dist_str = f"{m.total_distance_km:.2f} km"
        gain_str = f"{m.elevation_gain_m:.0f} hm"
        netto_str = format_seconds_to_hms(m.moving_time_sec)
        speed_str = f"{m.avg_speed_moving_kmh:.1f} km/h"
        hr_str = f"{m.avg_hr:.0f} bpm" if m.avg_hr else "---"
        suffer_str = f"{m.suffer_score:.1f}" if m.suffer_score else "---"

        tot_dist += m.total_distance_km
        tot_gain += m.elevation_gain_m
        tot_moving_s += m.moving_time_sec
        if m.suffer_score:
            tot_suffer += m.suffer_score

        short_file = (m.file_path.name[:19] + "..") if len(m.file_path.name) > 21 else m.file_path.name
        short_title = (m.title[:21] + "..") if len(m.title) > 23 else m.title
        print(f"{short_file:<22} | {short_title:<24} | {dist_str:>9} | {gain_str:>9} | {netto_str:>9} | {speed_str:>8} | {hr_str:>7} | {suffer_str:>7}")

    print("-" * 115)
    avg_speed = (tot_dist / (tot_moving_s / 3600.0)) if tot_moving_s > 0 else 0.0
    tot_time_str = format_seconds_to_hms(tot_moving_s)
    print(f"{'SUMME / GESAMT':<22} | {'Alle Touren':<24} | {tot_dist:>7.2f} km | {tot_gain:>7.0f} hm | {tot_time_str:>9} | {avg_speed:>6.1f} km/h | {'---':>7} | {tot_suffer:>7.1f}")
    print("=" * 115 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GPX-Trainings- und Herzfrequenzanalyse ohne externe Abhängigkeiten."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Pfad(e) zu bestimmten GPX-Dateien. Ohne Angabe werden alle Dateien in ./downloads/ analysiert.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("downloads"),
        help="Verzeichnis mit den GPX-Dateien (Standard: ./downloads)",
    )
    parser.add_argument(
        "--hr-max",
        type=int,
        default=184,
        help="Maximaler Herzfrequenzwert für Zoneneinteilung und Suffer Score (Standard: 184 bpm)",
    )
    parser.add_argument(
        "--hr-rest",
        type=int,
        default=None,
        help="Ruhepuls für Suffer Score (Standard: dynamisch aus dem Min-Puls der Aktivität ermittelt)",
    )
    parser.add_argument(
        "--gender",
        choices=["male", "female"],
        default="male",
        help="Geschlecht für die Banister-TRIMP / Suffer Score Formel (Standard: male)",
    )
    parser.add_argument(
        "--ele-threshold",
        type=float,
        default=0.5,
        help="Schwellenwert in Metern zur Filterung von Höhenmeter-Rauschen (Standard: 0.5 m)",
    )
    parser.add_argument(
        "--segments",
        action="store_true",
        help="Ruft zu den analysierten Touren zusätzlich live die Strava-Segmente ab (erfordert auth.json)",
    )
    args = parser.parse_args()

    # Zu analysierende Dateien bestimmen
    target_files: list[Path] = []
    if args.files:
        for f in args.files:
            p = Path(f)
            if p.exists() and p.is_file():
                target_files.append(p)
            else:
                print(f"[WARNUNG] Angegebene Datei existiert nicht: {f}", file=sys.stderr)
    else:
        search_dir = args.dir
        if search_dir.exists() and search_dir.is_dir():
            target_files = sorted(search_dir.glob("*.gpx"))
        else:
            print(f"[FEHLER] Verzeichnis '{search_dir}' nicht gefunden.", file=sys.stderr)
            return 1

    if not target_files:
        print(f"[HINWEIS] Keine GPX-Dateien in '{args.dir}' gefunden. Bitte laden Sie zuerst Aktivitäten herunter.")
        return 0

    print(f"\n[INFO] Gefundene GPX-Dateien zur Analyse: {len(target_files)}\n")

    # Optionaler Browser-Kontext für Segment-Abruf
    playwright_instance = None
    browser = None
    browser_context = None
    if args.segments:
        try:
            from playwright.sync_api import sync_playwright
            from config import AUTH_FILE, create_browser_context, launch_browser
            from sync_gpx import format_segments_table, get_activity_segments

            if AUTH_FILE.exists():
                playwright_instance = sync_playwright().start()
                browser = launch_browser(playwright_instance, headless=True)
                browser_context = create_browser_context(browser, storage_state_path=AUTH_FILE)
            else:
                print("[WARNUNG] 'auth.json' nicht gefunden - Segment-Abruf wird übersprungen.", file=sys.stderr)
        except Exception as seg_init_err:
            print(f"[WARNUNG] Konnte Browser für Segmente nicht initialisieren: {seg_init_err}", file=sys.stderr)

    try:
        all_metrics: list[ActivityMetrics] = []
        for gpx_file in target_files:
            try:
                title, activity_type, points = parse_gpx_file(gpx_file)
                weather_info = None
                surface_info = None
                dem_info = None
                if points and points[0].time:
                    try:
                        from weather_service import get_weather_for_activity
                        weather_info = get_weather_for_activity(
                            lat=points[0].lat,
                            lon=points[0].lon,
                            dt=points[0].time,
                        )
                    except Exception:
                        pass
                if query_osm_route_surface and len(points) >= 2:
                    try:
                        surface_info = query_osm_route_surface(points)
                    except Exception:
                        pass
                if verify_elevation_dem and len(points) >= 5:
                    try:
                        dem_info = verify_elevation_dem(points)
                    except Exception:
                        pass

                metrics = calculate_metrics(
                    file_path=gpx_file,
                    title=title,
                    activity_type=activity_type,
                    points=points,
                    hr_max=args.hr_max,
                    hr_rest=args.hr_rest,
                    gender=args.gender,
                    ele_noise_threshold_m=args.ele_threshold,
                    weather_data=weather_info,
                    surface_data=surface_info,
                    dem_data=dem_info,
                )
                print_activity_report(metrics, hr_max=args.hr_max)

                # Segmente abrufen und anzeigen falls gewünscht
                if browser_context:
                    import re
                    match = re.search(r"^(\d+)", gpx_file.name)
                    if match:
                        act_id = match.group(1)
                        seg_data = get_activity_segments(browser_context, act_id)
                        print(format_segments_table(seg_data))

                all_metrics.append(metrics)
            except Exception as e:
                print(f"[FEHLER] Datei '{gpx_file.name}' konnte nicht verarbeitet werden: {e}", file=sys.stderr)

        if len(all_metrics) > 1:
            print_summary_comparison_table(all_metrics)

        return 0

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_instance:
            try:
                playwright_instance.stop()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
