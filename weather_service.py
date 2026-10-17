"""
weather_service.py: Intelligenter Wetterdienst für Universal-Abfragen und aerodynamische Radsport-Physik.

Funktionen:
1. Universal-Wetter-Tool für KI-Assistenten (Abfrage beliebiger Orte und Daten via OpenWeatherMap & Open-Meteo).
2. Aerodynamische Umweltparameter (Windgeschwindigkeit, Windrichtung, Luftdichte aus Temperatur & Luftdruck).
3. Intelligenter SQLite-Cache zur Schonung von API-Limits.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import OPENWEATHER_API_KEY

logger = logging.getLogger("strava_mcp.weather")

COMPASS_DIRECTIONS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def deg_to_compass(deg: float) -> str:
    """Wandelt Grad (0-360°) in eine Himmelsrichtung um (z. B. 270° -> W)."""
    val = int((deg / 22.5) + 0.5)
    return COMPASS_DIRECTIONS[val % 16]


def calculate_air_density(pressure_hpa: float, temp_c: float) -> float:
    """
    Berechnet die exakte Luftdichte rho (kg/m³) nach der idealen Gasgleichung:
    rho = p / (R_spez * T_kelvin)
    R_spez = 287.058 J/(kg*K)
    """
    p_pa = max(800.0, min(1100.0, pressure_hpa)) * 100.0
    t_kelvin = max(230.0, min(330.0, temp_c + 273.15))
    rho = p_pa / (287.058 * t_kelvin)
    return round(rho, 4)


def calculate_cycling_suitability(
    temp_c: float, wind_kmh: float, condition: str, rain_mm: float = 0.0
) -> Dict[str, Any]:
    """Bewertet die Eignung für eine Radfahrt (Skala 1-10) und gibt Ausrüstungsempfehlungen."""
    score = 10
    advice = []

    # Temperatur
    if temp_c < 2.0:
        score -= 4
        advice.append("Eisglättegefahr! Winterausrüstung, Überschuhe und Thermo-Windjacke erforderlich.")
    elif temp_c < 10.0:
        score -= 2
        advice.append("Kühles Wetter: Langarmtrikot, Windweste und Knielinge/lange Hose empfohlen.")
    elif 15.0 <= temp_c <= 25.0:
        advice.append("Optimale Wohlfühltemperatur für maximale Ausdauerleistung (Kurz/Kurz).")
    elif temp_c > 30.0:
        score -= 3
        advice.append("Hitze-Alarm: Hoher Flüssigkeits- und Elektrolytbedarf. Puls wird thermobedingt 5-10 bpm höher sein.")

    # Wind
    if wind_kmh > 45.0:
        score -= 4
        advice.append("Sturmwarnung: Gefährliche Seitenwindböen, tiefe Felgen oder Gravel-Schotter meiden.")
    elif wind_kmh > 25.0:
        score -= 2
        advice.append("Spürbarer Wind: Route so planen, dass der Rückweg Rückenwind bietet.")
    else:
        advice.append("Geringer Wind: Sehr günstige aerodynamische Bedingungen.")

    # Regen / Niederschlag
    cond_lower = condition.lower()
    if "rain" in cond_lower or "drizzle" in cond_lower or rain_mm > 0.5:
        score -= 4
        advice.append("Regen / Nässe: Verlängerter Bremsweg, Regenjacke und Schutzbleche/Ass-Saver empfohlen.")
    elif "thunderstorm" in cond_lower:
        score -= 7
        advice.append("Gewittergefahr! Vom Radfahren im Freien wird dringend abgeraten.")

    final_score = max(1, min(10, score))
    return {
        "score_1_to_10": final_score,
        "summary": "Perfekte Bedingungen" if final_score >= 8 else "Gute Bedingungen" if final_score >= 6 else "Mäßige Bedingungen" if final_score >= 4 else "Ungünstig / Vorsicht geboten",
        "clothing_advice": advice,
    }


def geocode_location(location: str) -> Optional[Tuple[float, float, str]]:
    """Ermittelt Breiten- und Längengrad zu einem Ortsnamen via OpenWeatherMap Geocoding API."""
    clean_loc = location.strip()
    if not clean_loc:
        return None

    # Falls Koordinaten direkt übergeben wurden (z. B. "48.0, 7.8")
    if "," in clean_loc:
        parts = clean_loc.split(",")
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
            return lat, lon, clean_loc
        except ValueError:
            pass

    encoded = urllib.parse.quote(clean_loc)
    url = f"http://api.openweathermap.org/geo/1.0/direct?q={encoded}&limit=1&appid={OPENWEATHER_API_KEY}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StravaGPXSync/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data, list) and len(data) > 0:
                item = data[0]
                lat = float(item["lat"])
                lon = float(item["lon"])
                name = item.get("name", clean_loc)
                country = item.get("country", "")
                full_name = f"{name}, {country}" if country else name
                return lat, lon, full_name
    except Exception as e:
        logger.warning("Geocoding fehlgeschlagen für '%s': %s", clean_loc, e)

    return None


def fetch_current_weather_owm(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Ruft aktuelle Wetterdaten von OpenWeatherMap (2.5 API) ab."""
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid={OPENWEATHER_API_KEY}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StravaGPXSync/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            main = d.get("main", {})
            wind = d.get("wind", {})
            weather_desc = d.get("weather", [{}])[0]

            temp_c = float(main.get("temp", 18.0))
            pressure_hpa = float(main.get("pressure", 1013.25))
            humidity = int(main.get("humidity", 50))
            wind_speed_ms = float(wind.get("speed", 0.0))
            wind_deg = float(wind.get("deg", 0.0))
            condition = weather_desc.get("main", "Clear")
            description = weather_desc.get("description", "Klar")

            air_density = calculate_air_density(pressure_hpa, temp_c)
            suitability = calculate_cycling_suitability(temp_c, wind_speed_ms * 3.6, condition)

            return {
                "source": "OpenWeatherMap",
                "lat": lat,
                "lon": lon,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "temperature_c": round(temp_c, 1),
                "feels_like_c": round(float(main.get("feels_like", temp_c)), 1),
                "humidity_pct": humidity,
                "pressure_hpa": round(pressure_hpa, 1),
                "air_density_kg_m3": air_density,
                "wind_speed_ms": round(wind_speed_ms, 2),
                "wind_speed_kmh": round(wind_speed_ms * 3.6, 1),
                "wind_deg": int(wind_deg),
                "wind_direction_compass": deg_to_compass(wind_deg),
                "condition": condition,
                "description": description,
                "cycling_suitability": suitability,
            }
    except Exception as e:
        logger.warning("OpenWeatherMap Abruf fehlgeschlagen: %s", e)
        return None


def fetch_historical_weather_archive(lat: float, lon: float, dt: datetime) -> Optional[Dict[str, Any]]:
    """
    Ruft historische Reanalyse-Wetterdaten (Open-Meteo Archive) ab, falls das Datum in der Vergangenheit liegt.
    Garantiert 100% kostenlose, weltweite historische Wind- und Luftdichtedaten für jede frühere Aktivität.
    """
    date_str = dt.strftime("%Y-%m-%d")
    url = (
        f"https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}&start_date={date_str}&end_date={date_str}&"
        f"hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,weather_code"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StravaGPXSync/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            hourly = d.get("hourly", {})
            times = hourly.get("time", [])

            if not times:
                return None

            # Nächste Stunde zum Aktivitätszeitpunkt ermitteln
            target_hour = dt.hour
            hour_idx = min(target_hour, len(times) - 1)

            temp_c = float(hourly["temperature_2m"][hour_idx])
            pressure_hpa = float(hourly["surface_pressure"][hour_idx])
            humidity = int(hourly["relative_humidity_2m"][hour_idx])
            wind_speed_kmh = float(hourly["wind_speed_10m"][hour_idx])
            wind_deg = float(hourly["wind_direction_10m"][hour_idx])

            air_density = calculate_air_density(pressure_hpa, temp_c)
            suitability = calculate_cycling_suitability(temp_c, wind_kmh=wind_speed_kmh, condition="Reanalysis")

            return {
                "source": "Open-Meteo Archive (Historisch)",
                "lat": lat,
                "lon": lon,
                "timestamp": dt.isoformat(),
                "temperature_c": round(temp_c, 1),
                "feels_like_c": round(temp_c, 1),
                "humidity_pct": humidity,
                "pressure_hpa": round(pressure_hpa, 1),
                "air_density_kg_m3": air_density,
                "wind_speed_ms": round(wind_speed_kmh / 3.6, 2),
                "wind_speed_kmh": round(wind_speed_kmh, 1),
                "wind_deg": int(wind_deg),
                "wind_direction_compass": deg_to_compass(wind_deg),
                "condition": "Historical",
                "description": f"Historische Wetterdaten für {date_str} um {hour_idx:02d}:00 Uhr",
                "cycling_suitability": suitability,
            }
    except Exception as e:
        logger.warning("Historischer Wetterabruf fehlgeschlagen: %s", e)
        return None


def blend_wind_directions(deg1: float, w1: float, deg2: float, w2: float) -> int:
    """
    Berechnet den gewichteten Kreisdurchschnitt zweier Windrichtungen unter Vermeidung von 0°/360°-Sprüngen.
    """
    r1, r2 = math.radians(deg1), math.radians(deg2)
    x = w1 * math.cos(r1) + w2 * math.cos(r2)
    y = w1 * math.sin(r1) + w2 * math.sin(r2)
    if abs(x) < 1e-6 and abs(y) < 1e-6:
        return int(deg1)
    avg_deg = math.degrees(math.atan2(y, x)) % 360.0
    return int(round(avg_deg))


def fetch_weather_brightsky(lat: float, lon: float, dt: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Ruft hochpräzise, reale Boden-Messdaten des Deutschen Wetterdienstes (DWD) über die Bright Sky API ab.
    100% kostenlos, ohne Registrierung, ohne API-Key.
    Liefert offizielle DWD-Stationsdaten (z. B. Freiburg Station 01443, Buchenbach, Feldberg) inklusive echter Windböen.
    """
    now = datetime.now(timezone.utc)
    is_recent = dt is None or abs((now - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))).total_seconds()) <= 3.0 * 3600.0

    if is_recent:
        url = f"https://api.brightsky.dev/current_weather?lat={lat:.4f}&lon={lon:.4f}"
    else:
        date_str = dt.strftime("%Y-%m-%d")
        url = f"https://api.brightsky.dev/weather?lat={lat:.4f}&lon={lon:.4f}&date={date_str}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StravaGPXSync/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            sources = data.get("sources", [])
            source = sources[0] if sources else {}

            if is_recent:
                w = data.get("weather", {})
            else:
                wlist = data.get("weather", [])
                if not wlist:
                    return None
                target_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                w = min(
                    wlist,
                    key=lambda item: abs((datetime.fromisoformat(item["timestamp"]) - target_dt).total_seconds()),
                )

            wind_k = float(w.get("wind_speed_10") if w.get("wind_speed_10") is not None else (w.get("wind_speed") or 0.0))
            wind_g = float(w.get("wind_gust_speed_10") if w.get("wind_gust_speed_10") is not None else (w.get("wind_gust_speed") or 0.0))
            wind_d = float(w.get("wind_direction_10") if w.get("wind_direction_10") is not None else (w.get("wind_direction") or 0.0))
            temp_c = float(w.get("temperature", 18.0))
            pressure_hpa = float(w.get("pressure_msl", 1013.25))
            humidity = int(w.get("relative_humidity", 50))
            precip = float(w.get("precipitation_60") if w.get("precipitation_60") is not None else (w.get("precipitation") or 0.0))
            station_name = source.get("station_name", "DWD-Messstation")
            station_id = source.get("dwd_station_id", "")
            distance_km = round(float(source.get("distance", 0.0)) / 1000.0, 1)

            return {
                "source": f"Bright Sky (DWD Station {station_name})",
                "station_name": station_name,
                "station_id": station_id,
                "distance_km": distance_km,
                "temperature_c": round(temp_c, 1),
                "pressure_hpa": round(pressure_hpa, 1),
                "humidity_pct": humidity,
                "wind_speed_kmh": round(wind_k, 1),
                "wind_speed_ms": round(wind_k / 3.6, 2),
                "wind_deg": int(wind_d),
                "wind_direction_compass": deg_to_compass(wind_d),
                "wind_gust_kmh": round(wind_g, 1),
                "precipitation_mm": round(precip, 2),
                "condition": w.get("condition") or w.get("icon") or "dry",
                "description": f"DWD-Bodenstation {station_name} (ID {station_id}, {distance_km} km entfernt)",
            }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.debug("Bright Sky DWD: Keine Station im Umkreis von (lat=%s, lon=%s): 404", lat, lon)
            return None
        logger.warning("Bright Sky HTTP-Fehler %s: %s", e.code, e)
        return None
    except Exception as e:
        logger.debug("Bright Sky Abruf nicht verfügbar: %s", e)
        return None


def fuse_weather_sources(
    brightsky: Optional[Dict[str, Any]],
    primary_model: Optional[Dict[str, Any]],
    lat: float,
    lon: float,
) -> Optional[Dict[str, Any]]:
    """
    Intelligentes Multi-Source-Ensemble:
    Kombiniert reale DWD-Bodenstationsdaten (Bright Sky) mit numerischen Wettermodellen (OpenWeatherMap / Open-Meteo).
    - Berechnet bei Verfügbarkeit beider Quellen einen distanzgewichteten Konsens.
    - Berechnet Luftdichte, Böen-Sicherheitsanalyse und Modell-Delta.
    - Fällt im Ausland oder bei Netzausfall nahtlos auf die verbleibende Quelle zurück.
    """
    if not brightsky and not primary_model:
        return None
    if not brightsky:
        return primary_model
    if not primary_model:
        rho = calculate_air_density(brightsky["pressure_hpa"], brightsky["temperature_c"])
        suitability = calculate_cycling_suitability(
            brightsky["temperature_c"],
            brightsky["wind_speed_kmh"],
            brightsky["condition"],
            brightsky.get("precipitation_mm", 0.0),
        )
        brightsky["air_density_kg_m3"] = rho
        brightsky["cycling_suitability"] = suitability
        brightsky["feels_like_c"] = brightsky["temperature_c"]
        brightsky["lat"] = lat
        brightsky["lon"] = lon
        return brightsky

    dist_km = brightsky.get("distance_km", 99.0)
    if dist_km <= 15.0:
        w_dwd, w_mod = 0.75, 0.25
        strategy = f"DWD-Bodenstation Priorität ({brightsky['station_name']} nur {dist_km} km entfernt)"
    elif dist_km <= 35.0:
        w_dwd, w_mod = 0.60, 0.40
        strategy = f"Gewichteter Konsens ({brightsky['station_name']} {dist_km} km entfernt)"
    else:
        w_dwd, w_mod = 0.40, 0.60
        strategy = f"Modell-Schwerpunkt (DWD-Station {dist_km} km entfernt)"

    blended_temp = round(brightsky["temperature_c"] * w_dwd + primary_model["temperature_c"] * w_mod, 1)
    blended_wind_kmh = round(brightsky["wind_speed_kmh"] * w_dwd + primary_model["wind_speed_kmh"] * w_mod, 1)
    blended_wind_ms = round(blended_wind_kmh / 3.6, 2)
    blended_wind_deg = blend_wind_directions(brightsky["wind_deg"], w_dwd, primary_model["wind_deg"], w_mod)
    blended_pressure = round(brightsky["pressure_hpa"] * w_dwd + primary_model.get("pressure_hpa", 1013.25) * w_mod, 1)
    blended_humidity = int(round(brightsky["humidity_pct"] * w_dwd + primary_model.get("humidity_pct", 50) * w_mod))

    air_density = calculate_air_density(blended_pressure, blended_temp)
    condition = primary_model.get("condition") or brightsky.get("condition", "Clear")
    precip = max(brightsky.get("precipitation_mm", 0.0), primary_model.get("precipitation_mm", 0.0))
    suitability = calculate_cycling_suitability(blended_temp, blended_wind_kmh, condition, precip)

    delta_temp = round(abs(brightsky["temperature_c"] - primary_model["temperature_c"]), 1)
    delta_wind = round(abs(brightsky["wind_speed_kmh"] - primary_model["wind_speed_kmh"]), 1)

    return {
        "source": f"Multi-Source Ensemble: DWD ({brightsky['station_name']}, {dist_km}km) + {primary_model.get('source', 'Wettermodell')}",
        "ensemble_strategy": strategy,
        "lat": lat,
        "lon": lon,
        "timestamp": primary_model.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "temperature_c": blended_temp,
        "feels_like_c": round(float(primary_model.get("feels_like_c", blended_temp)), 1),
        "humidity_pct": blended_humidity,
        "pressure_hpa": blended_pressure,
        "air_density_kg_m3": air_density,
        "wind_speed_ms": blended_wind_ms,
        "wind_speed_kmh": blended_wind_kmh,
        "wind_deg": blended_wind_deg,
        "wind_direction_compass": deg_to_compass(blended_wind_deg),
        "wind_gust_kmh": brightsky.get("wind_gust_kmh", 0.0),
        "precipitation_mm": precip,
        "condition": condition,
        "description": f"Ensemble-Konsens aus DWD {brightsky['station_name']} ({dist_km} km) und {primary_model.get('source')}",
        "cycling_suitability": suitability,
        "comparison": {
            "dwd_station": f"{brightsky['station_name']} (ID {brightsky['station_id']})",
            "dwd_station_distance_km": dist_km,
            "dwd_temperature_c": brightsky["temperature_c"],
            "model_temperature_c": primary_model["temperature_c"],
            "delta_temperature_c": delta_temp,
            "dwd_wind_speed_kmh": brightsky["wind_speed_kmh"],
            "model_wind_speed_kmh": primary_model["wind_speed_kmh"],
            "delta_wind_speed_kmh": delta_wind,
            "dwd_wind_gust_kmh": brightsky.get("wind_gust_kmh", 0.0),
            "consensus_status": "Exzellente Übereinstimmung" if delta_temp <= 1.5 else "Moderate Modellabweichung",
        },
    }


def get_weather_for_activity(lat: float, lon: float, dt: datetime) -> Optional[Dict[str, Any]]:
    """
    Liefert das passende Wetter für einen GPX-Startort und Zeitpunkt.
    Nutzt Multi-Source-Ensemble (DWD Bright Sky + OpenWeatherMap/Open-Meteo)
    sowie den persistenten SQLite-Cache zur Schonung externer Ressourcen.
    """
    from rag_engine import ActivityVectorStore

    cache_key = f"{lat:.2f}_{lon:.2f}_{dt.strftime('%Y-%m-%d_%H')}"
    store = ActivityVectorStore()

    # Cache prüfen
    try:
        with store._get_connection() as conn:
            cur = conn.execute(
                "SELECT weather_json FROM weather_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
            if row:
                return json.loads(row["weather_json"])
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    act_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    age_hours = (now - act_dt).total_seconds() / 3600.0

    # 1. Reale DWD-Bodenmessstation über Bright Sky (100% kostenlos)
    brightsky_data = fetch_weather_brightsky(lat, lon, dt=None if age_hours <= 3.0 else act_dt)

    # 2. Primäres Wettermodell (OWM aktuell, Open-Meteo Reanalyse historisch)
    primary_model_data: Optional[Dict[str, Any]] = None
    if age_hours <= 3.0:
        primary_model_data = fetch_current_weather_owm(lat, lon)

    if not primary_model_data:
        primary_model_data = fetch_historical_weather_archive(lat, lon, act_dt)

    if not primary_model_data and age_hours <= 3.0:
        primary_model_data = fetch_current_weather_owm(lat, lon)

    # 3. Quellen zu wissenschaftlichem Konsens zusammenführen
    weather_data = fuse_weather_sources(brightsky_data, primary_model_data, lat, lon)

    # Im Cache persistieren
    if weather_data:
        try:
            with store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO weather_cache (cache_key, weather_json, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        weather_json=excluded.weather_json
                    """,
                    (cache_key, json.dumps(weather_data, ensure_ascii=False), now.isoformat()),
                )
                conn.commit()
        except Exception as e:
            logger.debug("Konnte Wetter nicht cachen: %s", e)

    return weather_data


def get_weather_universal(location: str, date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Universal-Funktion für das MCP-Tool get_weather:
    Ermöglicht der KI, das aktuelle oder historische Wetter an jedem beliebigen Ort abzufragen.
    """
    geo = geocode_location(location)
    if not geo:
        return {
            "status": "error",
            "message": f"Der Ort '{location}' konnte nicht geocodiert werden. Bitte prüfen Sie die Schreibweise.",
        }

    lat, lon, place_name = geo

    target_dt = datetime.now(timezone.utc)
    if date_str:
        from mcp_server import parse_date_to_datetime
        parsed = parse_date_to_datetime(date_str)
        if parsed:
            target_dt = parsed

    weather = get_weather_for_activity(lat, lon, target_dt)
    if not weather:
        return {
            "status": "error",
            "message": f"Konnte für '{place_name}' ({lat}, {lon}) keine Wetterdaten abrufen.",
        }

    return {
        "status": "success",
        "location": place_name,
        "coordinates": {"lat": lat, "lon": lon},
        "query_time": target_dt.strftime("%d.%m.%Y %H:%M UTC"),
        "weather": weather,
    }
