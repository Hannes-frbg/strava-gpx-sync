"""
geo_enhancements.py: Geodätische und topografische Präzisions-Dienste für Strava GPX Sync.

1. OpenStreetMap (OSM Overpass API):
   - Ermittelt echte Wegoberflächen (Asphalt, Schotter, Waldweg, Kopfsteinpflaster).
   - Berechnet den exakten, streckenspezifischen Rollwiderstand (C_rr) für Gravelbikes.
   - Ermittelt Waldanteil zur Dämpfung bodennaher Windgeschwindigkeiten.
2. Digital Elevation Model (DEM via Open-Meteo Elevation API):
   - Verifiziert Höhenmeter und Anstiegsprozente gegen Satelliten- und LIDAR-Geländemodelle.
   - Erkennt barometrischen Drift (Wetterumschwung während der Fahrt) und GPS-Höhenrauschen.
3. 100% anonym, öffentlich und ohne Registrierung / ohne API-Keys.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("strava_mcp.geo")

# Rollwiderstandskoeffizienten (C_rr) für 40mm Gravel-Bereifung bei 3.0 bar
SURFACE_CRR_MAP = {
    "asphalt": 0.0042,
    "paved": 0.0042,
    "concrete": 0.0044,
    "compacted": 0.0068,
    "fine_gravel": 0.0070,
    "gravel": 0.0098,
    "unpaved": 0.0095,
    "dirt": 0.0110,
    "ground": 0.0115,
    "earth": 0.0120,
    "cobblestone": 0.0135,
    "sett": 0.0130,
    "unhewn_cobblestone": 0.0150,
    "grass": 0.0140,
    "sand": 0.0200,
}


def query_osm_route_surface(points: list, max_samples: int = 12) -> Dict[str, Any]:
    """
    Fragt über die freie OpenStreetMap Overpass-API die Wegeigenschaften entlang der Tour ab.
    Berechnet den realen gewichteten Rollwiderstand C_rr und den Wald-Windschutzanteil.
    """
    if len(points) < 2:
        return {
            "surface_detected": False,
            "weighted_crr": 0.0048,
            "surface_distribution": {"asphalt": 100.0},
            "forest_shelter_pct": 0.0,
        }

    from rag_engine import ActivityVectorStore

    # Cache-Prüfung über Start- und Endkoordinaten
    p_start = points[0]
    p_end = points[-1]
    p_mid = points[len(points) // 2]
    cache_key = f"osm_surf_{p_start.lat:.3f}_{p_start.lon:.3f}_{p_mid.lat:.3f}_{p_mid.lon:.3f}_{p_end.lat:.3f}_{p_end.lon:.3f}"

    store = ActivityVectorStore()
    try:
        with store._get_connection() as conn:
            cur = conn.execute("SELECT data_json FROM geo_cache WHERE cache_key = ?", (cache_key,))
            row = cur.fetchone()
            if row:
                return json.loads(row["data_json"])
    except Exception:
        pass

    # Bounding Box der Tour bestimmen
    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Schrittweite für repräsentative Stichprobenpunkte entlang des Tracks
    step = max(1, len(points) // max_samples)
    sampled_pts = points[::step][:max_samples]

    surfaces_found: List[str] = []
    forest_hits = 0

    try:
        # Overpass Abfrage für Wege um die Stichprobenpunkte
        queries = []
        for p in sampled_pts:
            queries.append(f"way(around:35,{p.lat:.5f},{p.lon:.5f})[highway];")
            queries.append(f"way(around:40,{p.lat:.5f},{p.lon:.5f})[landuse=forest];")

        overpass_q = f"[out:json][timeout:8];({' '.join(queries)});out tags 50;"
        url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(overpass_q)

        req = urllib.request.Request(url, headers={"User-Agent": "StravaGPXSync/2.0 (OSM Surface Engine)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elements = data.get("elements", [])

            for el in elements:
                tags = el.get("tags", {})
                surf = tags.get("surface") or tags.get("tracktype")
                if surf:
                    clean_surf = str(surf).lower()
                    if clean_surf in SURFACE_CRR_MAP:
                        surfaces_found.append(clean_surf)
                    elif "gravel" in clean_surf or "grade2" in clean_surf or "grade3" in clean_surf:
                        surfaces_found.append("gravel")
                    elif "asphalt" in clean_surf or "paved" in clean_surf or "grade1" in clean_surf:
                        surfaces_found.append("asphalt")

                if tags.get("landuse") == "forest" or tags.get("natural") == "wood":
                    forest_hits += 1

    except Exception as e:
        logger.warning("OSM Overpass Abruf fehlgeschlagen (nutze Fallback): %s", e)

    # Auswertung
    if not surfaces_found:
        surfaces_found = ["asphalt"]

    dist_counts: Dict[str, int] = {}
    for s in surfaces_found:
        dist_counts[s] = dist_counts.get(s, 0) + 1

    total_hits = len(surfaces_found)
    distribution = {s: round((cnt / total_hits) * 100.0, 1) for s, cnt in dist_counts.items()}

    # Gewichteten Rollwiderstand berechnen
    weighted_crr = sum(
        SURFACE_CRR_MAP.get(s, 0.0048) * (cnt / total_hits)
        for s, cnt in dist_counts.items()
    )

    forest_shelter_pct = round(min(100.0, (forest_hits / max(1, len(sampled_pts))) * 100.0), 1)

    result = {
        "surface_detected": len(surfaces_found) > 1 or surfaces_found[0] != "asphalt",
        "primary_surface": max(dist_counts, key=dist_counts.get),
        "weighted_crr": round(weighted_crr, 5),
        "surface_distribution": distribution,
        "forest_shelter_pct": forest_shelter_pct,
        "surface_note": (
            f"Ermittelter Rollwiderstand Crr={weighted_crr:.4f}. "
            f"{distribution.get('gravel', 0) + distribution.get('compacted', 0):.0f}% Offroad/Schotteranteil erfasst."
        ),
    }

    # Cachen
    try:
        with store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO geo_cache (cache_key, data_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET data_json=excluded.data_json
                """,
                (cache_key, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception:
        pass

    return result


def verify_elevation_dem(points: list, max_samples: int = 40) -> Dict[str, Any]:
    """
    Gleicht die GPS-Höhenkurve der Tour mit hochauflösenden Satelliten-Geländemodellen
    (Open-Meteo Elevation / Copernicus DEM) ab. Erkennt Barometer-Drift und korrigiert Messfehler.
    100% anonym und ohne API-Key.
    """
    if len(points) < 5:
        return {"dem_verified": False, "reason": "Zu wenige Trackpoints"}

    from rag_engine import ActivityVectorStore

    p_start = points[0]
    p_end = points[-1]
    cache_key = f"dem_elev_{p_start.lat:.3f}_{p_start.lon:.3f}_{p_end.lat:.3f}_{p_end.lon:.3f}_{len(points)}"

    store = ActivityVectorStore()
    try:
        with store._get_connection() as conn:
            cur = conn.execute("SELECT data_json FROM geo_cache WHERE cache_key = ?", (cache_key,))
            row = cur.fetchone()
            if row:
                return json.loads(row["data_json"])
    except Exception:
        pass

    step = max(1, len(points) // max_samples)
    sampled = points[::step][:max_samples]

    lats_str = ",".join(f"{p.lat:.5f}" for p in sampled)
    lons_str = ",".join(f"{p.lon:.5f}" for p in sampled)

    url = f"https://api.open-meteo.com/v1/elevation?latitude={lats_str}&longitude={lons_str}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StravaGPXSync/2.0 (DEM Elevation Verifier)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            dem_elevs: List[float] = data.get("elevation", [])

            if not dem_elevs or len(dem_elevs) != len(sampled):
                return {"dem_verified": False, "reason": "DEM Daten unvollständig"}

            # Aufstieg nach Satelliten-Modell berechnen
            dem_gain = 0.0
            for i in range(1, len(dem_elevs)):
                diff = dem_elevs[i] - dem_elevs[i - 1]
                if diff > 1.0:  # Rauschfilter
                    dem_gain += diff

            gpx_elevs = [p.ele for p in sampled if p.ele is not None]
            gpx_gain = 0.0
            if len(gpx_elevs) > 1:
                for i in range(1, len(gpx_elevs)):
                    diff = gpx_elevs[i] - gpx_elevs[i - 1]
                    if diff > 1.0:
                        gpx_gain += diff

            variance_pct = round(abs(gpx_gain - dem_gain) / max(1.0, dem_gain) * 100.0, 1)
            is_reliable = variance_pct <= 15.0

            step_indices = [i * step for i in range(len(sampled))]

            result = {
                "dem_verified": True,
                "satellite_elevation_gain_m": round(dem_gain, 1),
                "sampled_gpx_gain_m": round(gpx_gain, 1),
                "elevation_variance_pct": variance_pct,
                "barometric_drift_detected": not is_reliable,
                "elevation_quality": "Exzellent (Satelliten-bestätigt)" if is_reliable else "Abweichung erkannt (GPS/Barometer-Drift)",
                "dem_min_ele_m": round(min(dem_elevs), 1),
                "dem_max_ele_m": round(max(dem_elevs), 1),
                "reconciled_elevation_gain_m": round(dem_gain, 1),
                "reconciliation_recommended": not is_reliable,
                "elevation_source_recommended": "dem_satellite" if not is_reliable else "barometric_gpx",
                "dem_elevations": [round(e, 2) for e in dem_elevs],
                "sampled_indices": step_indices,
            }

            try:
                with store._get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO geo_cache (cache_key, data_json, created_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET data_json=excluded.data_json
                        """,
                        (cache_key, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                    )
                    conn.commit()
            except Exception:
                pass

            return result

    except Exception as e:
        logger.warning("DEM Elevation Abruf fehlgeschlagen: %s", e)
        return {"dem_verified": False, "reason": str(e)}


def apply_dem_elevation_correction(points: list, dem_info: dict) -> list[float]:
    """
    Interpoliert die hochauflösenden Satelliten-Höhen (DEM) stetig über alle Trackpoints.
    Eliminiert barometrischen Drift (Wetterwechsel) und GPS-Höhenzittern vollständig.
    Liefert ein konsistentes Höhenprofil [ele_0, ele_1, ..., ele_N-1] für präzise Steigungs- und Wattberechnungen.
    """
    n_pts = len(points)
    if n_pts == 0:
        return []

    dem_elevs: List[float] = dem_info.get("dem_elevations", [])
    sampled_indices: List[int] = dem_info.get("sampled_indices", [])

    if not dem_elevs or not sampled_indices or len(dem_elevs) != len(sampled_indices):
        # Fallback: vorhandene GPX-Höhen
        return [float(p.ele) if p.ele is not None else 0.0 for p in points]

    corrected = [0.0] * n_pts
    k_max = len(sampled_indices) - 1

    for k in range(k_max):
        idx_start = sampled_indices[k]
        idx_end = sampled_indices[k + 1]
        ele_start = dem_elevs[k]
        ele_end = dem_elevs[k + 1]

        span = max(1, idx_end - idx_start)
        for i in range(idx_start, min(idx_end, n_pts)):
            t = (i - idx_start) / span
            corrected[i] = round(ele_start + t * (ele_end - ele_start), 2)

    # Restliche Punkte bis zum Ende mit dem letzten DEM-Wert auffüllen
    last_idx = sampled_indices[-1]
    last_ele = dem_elevs[-1]
    for i in range(last_idx, n_pts):
        corrected[i] = round(last_ele, 2)

    return corrected
