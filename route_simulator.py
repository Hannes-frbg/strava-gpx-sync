"""
route_simulator.py: Prädiktiver Routen-Simulator & Renntaktiker für geplante GPX-Dateien (Komoot / Strava Routes).

Simuliert vor der Fahrt auf Basis der geplanten GPX-Datei:
1. Topografie & Rollwiderstand: Echte OSM-Wegbeläge (Schotter vs. Asphalt Crr) und Satelliten-Höhenmodell (DEM).
2. Wetter- & Windvorhersage: Berechnet für den geplanten Startzeitpunkt exakte Windvektoren (Gegenwind vs. Rückenwind).
3. Pacing- & Watt-Strategie: Vorgaben für Anstiege und Flachpassagen zur Einhaltung der gewünschten Trainingszone (GA1/GA2 vs. Schwelle).
4. Grammgenauer Verpflegungsfahrplan (Jeukendrup & Romijn): Benötigte Kohlenhydrate (g), Flaschenanzahl, Riegel-Timing und Schweißverlust.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from analyze_gpx import (
    TrackPoint,
    calculate_bearing_deg,
    calculate_elevation_gain_loss,
    format_seconds_to_hms,
    haversine_distance_meters,
    parse_gpx_file,
)
from geo_enhancements import query_osm_route_surface, verify_elevation_dem
from sports_science import calculate_metabolic_and_nutrition, calculate_vam_and_youth_diagnostics
from weather_service import get_weather_for_activity, get_weather_universal

logger = logging.getLogger("strava_mcp.simulator")


def simulate_planned_route(
    gpx_file_path: str,
    planned_start_time: Optional[str] = None,
    target_intensity: str = "endurance_ga1",
    target_avg_speed_kmh: Optional[float] = None,
    rider_weight_kg: float = 60.0,
    total_system_weight_kg: float = 72.9,
    hr_max: int = 205,
    hr_rest: int = 52,
) -> Dict[str, Any]:
    """
    Simuliert eine geplante Komoot- oder Strava-Route vorab.
    Berechnet die voraussichtliche Fahrtzeit, Wattwerte, Windeffekte und den exakten Ernährungsplan.

    Args:
        gpx_file_path: Pfad zur geplanten GPX-Datei (z. B. 'downloads/komoot_tour.gpx').
        planned_start_time: Geplanter Startzeitpunkt als ISO-String (z. B. '2026-09-22T14:00:00').
        target_intensity: 'recovery', 'endurance_ga1', 'tempo_ga2', 'threshold_race'.
        target_avg_speed_kmh: Optionales Wunschtempo in km/h.
        rider_weight_kg: Fahrergewicht in kg (Standard: 60.0 kg).
        total_system_weight_kg: Gesamtgewicht Fahrer + Rad + Ausrüstung (Standard: 72.9 kg).
        hr_max: Maximalpuls (Standard: 205 bpm).
        hr_rest: Ruhepuls (Standard: 52 bpm).

    Returns:
        Umfassender Simulations- und Coachingbericht für den Renntag/Ausfahrt.
    """
    path_obj = Path(gpx_file_path)
    if not path_obj.exists():
        return {
            "status": "error",
            "message": f"GPX-Datei '{gpx_file_path}' existiert nicht.",
        }

    title, activity_type, points = parse_gpx_file(path_obj)
    if len(points) < 5:
        return {
            "status": "error",
            "message": "Die GPX-Datei enthält zu wenige Trackpoints für eine Simulation.",
        }

    # 1. Distanz und Höhenmeter berechnen
    total_dist_m = 0.0
    for i in range(len(points) - 1):
        total_dist_m += haversine_distance_meters(
            points[i].lat, points[i].lon, points[i + 1].lat, points[i + 1].lon
        )
    total_dist_km = total_dist_m / 1000.0

    elevations = [p.ele for p in points if p.ele is not None]
    gain_m, loss_m = calculate_elevation_gain_loss(elevations, threshold=0.5) if elevations else (0.0, 0.0)

    # 2. Geodaten (OSM Oberfläche & Satelliten-Geländemodell) abrufen
    surface_data = query_osm_route_surface(points)
    dem_data = verify_elevation_dem(points)

    crr = float(surface_data.get("weighted_crr", 0.0048))
    forest_shelter = float(surface_data.get("forest_shelter_pct", 0.0))

    # 3. Wettervorhersage für die Region und Startzeitpunkt
    p_start = points[0]
    dt_target = None
    if planned_start_time:
        try:
            dt_target = datetime.fromisoformat(planned_start_time.replace("Z", "+00:00"))
        except Exception:
            pass
    if not dt_target:
        dt_target = datetime.now(timezone.utc) + timedelta(hours=2)

    weather_data = None
    try:
        weather_data = get_weather_for_activity(lat=p_start.lat, lon=p_start.lon, dt=dt_target)
    except Exception as e:
        logger.debug("Wettervorhersage übersprungen: %s", e)

    temp_c = float(weather_data.get("temperature_c", 19.0)) if weather_data else 19.0
    wind_speed_raw_ms = float(weather_data.get("wind_speed_ms", 3.0)) if weather_data else 3.0
    wind_deg = float(weather_data.get("wind_deg", 0.0)) if weather_data else 0.0
    rho = float(weather_data.get("air_density_kg_m3", 1.225)) if weather_data else 1.225

    # Wald dämpft Wind
    shelter_factor = max(0.25, 1.0 - 0.45 * (forest_shelter / 100.0))
    wind_speed_ms = wind_speed_raw_ms * shelter_factor

    # 4. Leistungs- und Tempoprofil je nach gewählter Intensität festlegen
    # Für einen leichten Bergfahrer / Wettkampffahrer (ca. 60 kg):
    # - GA1: ca. 110 - 145 W (1.8 - 2.4 W/kg)
    # - GA2: ca. 150 - 180 W (2.5 - 3.0 W/kg)
    # - Schwelle: ca. 190 - 230 W (3.2 - 3.8 W/kg)
    intensity_map = {
        "recovery": {"name": "Regeneration / Rekom (Z1)", "base_flat_w": 95.0, "climb_limit_w": 130.0, "climb_w_kg": 2.1},
        "endurance_ga1": {"name": "Grundlagenausdauer 1 (GA1)", "base_flat_w": 130.0, "climb_limit_w": 165.0, "climb_w_kg": 2.75},
        "tempo_ga2": {"name": "Tempotraining (GA2)", "base_flat_w": 165.0, "climb_limit_w": 200.0, "climb_w_kg": 3.33},
        "threshold_race": {"name": "Wettkampf / Schwellenbereich (EB)", "base_flat_w": 195.0, "climb_limit_w": 240.0, "climb_w_kg": 4.0},
    }
    cfg = intensity_map.get(target_intensity.lower(), intensity_map["endurance_ga1"])

    # 5. Physische Strecken-Simulation (Segment für Segment)
    sim_time_s = 0.0
    sim_work_j = 0.0
    headwind_meters = 0.0
    tailwind_meters = 0.0
    steep_climbs_detected = []

    g = 9.80665
    c_d_a_base = 0.32
    drivetrain_factor = 1.035

    for i in range(len(points) - 1):
        p1 = points[i]
        p2 = points[i + 1]
        dist_step = haversine_distance_meters(p1.lat, p1.lon, p2.lat, p2.lon)
        if dist_step < 1.0:
            continue

        dh_step = (p2.ele - p1.ele) if (p1.ele is not None and p2.ele is not None) else 0.0
        slope = dh_step / dist_step

        # Windvektor
        bike_bearing = calculate_bearing_deg(p1.lat, p1.lon, p2.lat, p2.lon)
        rel_angle = (wind_deg - bike_bearing) % 360.0
        cos_rel = math.cos(math.radians(rel_angle))
        headwind_comp = wind_speed_ms * cos_rel

        if cos_rel > 0.25:
            headwind_meters += dist_step
        elif cos_rel < -0.25:
            tailwind_meters += dist_step

        # Ziel-Leistung für diesen Abschnitt
        if slope >= 0.06:
            target_watts = cfg["climb_limit_w"]
            if len(steep_climbs_detected) < 5 and dist_step > 50:
                steep_climbs_detected.append({
                    "km": round(total_dist_km * (i / len(points)), 1),
                    "slope_pct": round(slope * 100.0, 1),
                    "target_watts": target_watts,
                })
        elif slope >= 0.02:
            target_watts = (cfg["base_flat_w"] + cfg["climb_limit_w"]) / 2.0
        elif slope <= -0.03:
            target_watts = cfg["base_flat_w"] * 0.4  # Rollen / leichtes Mittreten bergab
        else:
            target_watts = cfg["base_flat_w"]

        # Numerische Approximation der Geschwindigkeit für gegebene Watt
        # P = (F_gravity + F_roll + F_aero) * v * 1.035
        v = 7.0  # Startschätzung ~ 25 km/h
        theta = math.atan(slope)
        f_grav = total_system_weight_kg * g * math.sin(theta)
        f_roll = crr * total_system_weight_kg * g * math.cos(theta)

        # 3 Newton-Raphson Iterationsschritte zur exakten Tempofindung
        for _ in range(4):
            v_air = max(0.5, v + headwind_comp)
            f_aero = 0.5 * rho * c_d_a_base * (v_air ** 2)
            f_res = f_grav + f_roll + f_aero
            calc_w = max(10.0, f_res * v * drivetrain_factor)
            ratio = target_watts / calc_w if calc_w > 0 else 1.0
            v = max(1.5, min(22.0, v * math.pow(ratio, 0.33)))

        dt_step = dist_step / v
        sim_time_s += dt_step
        sim_work_j += target_watts * dt_step

    sim_moving_hours = sim_time_s / 3600.0
    sim_avg_speed_kmh = round(total_dist_km / sim_moving_hours, 1) if sim_moving_hours > 0 else 20.0
    sim_avg_watts = round(sim_work_j / sim_time_s, 1) if sim_time_s > 0 else cfg["base_flat_w"]
    sim_work_kj = round(sim_work_j / 1000.0, 1)

    # 6. Stoffwechsel- und Verpflegungsberechnung (Jeukendrup & Romijn)
    nutrition = calculate_metabolic_and_nutrition(
        work_kj=sim_work_kj,
        duration_s=sim_time_s,
        zones=[],
        avg_hr=148.0 if target_intensity == "endurance_ga1" else 162.0,
        hr_max=hr_max,
        temp_c=temp_c,
    )

    carbs_total_g = nutrition["carbohydrates"]["burned_grams"]
    carbs_rate_gh = nutrition["carbohydrates"]["burn_rate_g_per_hour"]
    sweat_liters = nutrition["hydration"]["estimated_sweat_loss_liters"]

    # Konkrete Packliste für die Tour
    bottles_needed = max(1, int(math.ceil(sweat_liters * 0.85 / 0.75)))
    carbs_to_pack_g = round(min(carbs_total_g * 0.75, sim_moving_hours * 60.0), 0)

    fueling_plan = [
        f"Trinkflaschen: {bottles_needed}x 750ml Flasche(n) mit Elektrolyten (ca. 500mg Natrium pro Flasche).",
        f"Kohlenhydrat-Ziel: Ca. {carbs_to_pack_g:.0f}g Kohlenhydrate insgesamt auf dem Rad zuführen.",
    ]
    if sim_moving_hours >= 1.5:
        fueling_plan.append("Timing: Alle 20-30 Minuten 3-4 große Schlucke trinken (ca. 150-200ml).")
        fueling_plan.append("Verpflegung: Nach 45 Minuten ersten Riegel/Gel, danach alle 45 Minuten wiederholen.")
    else:
        fueling_plan.append("Bei Touren unter 90 Minuten reicht eine Flasche mit Wasser/Elektrolyten völlig aus.")

    hw_pct = round((headwind_meters / max(1.0, total_dist_m)) * 100.0, 1)
    tw_pct = round((tailwind_meters / max(1.0, total_dist_m)) * 100.0, 1)

    return {
        "status": "success",
        "route_title": title,
        "intensity_mode": cfg["name"],
        "planned_start": dt_target.strftime("%d.%m.%Y um %H:%M Uhr"),
        "key_metrics": {
            "distance_km": round(total_dist_km, 2),
            "elevation_gain_m": round(gain_m, 1),
            "elevation_loss_m": round(loss_m, 1),
            "estimated_moving_time": format_seconds_to_hms(sim_time_s),
            "estimated_moving_hours": round(sim_moving_hours, 2),
            "estimated_avg_speed_kmh": sim_avg_speed_kmh,
            "estimated_avg_watts": sim_avg_watts,
            "estimated_watts_per_kg": round(sim_avg_watts / rider_weight_kg, 2),
            "estimated_work_kj": sim_work_kj,
        },
        "weather_and_aerodynamics": {
            "forecast_temperature_c": temp_c,
            "wind_speed_kmh": weather_data.get("wind_speed_kmh") if weather_data else round(wind_speed_raw_ms * 3.6, 1),
            "wind_direction_compass": weather_data.get("wind_direction_compass") if weather_data else "N",
            "headwind_share_pct": hw_pct,
            "tailwind_share_pct": tw_pct,
            "tactical_wind_advice": (
                f"Achtung: Gegenwind auf {hw_pct}% der Strecke. "
                "Zu Beginn auf Flachpassagen nicht überzocken und tief im Lenker bleiben."
                if hw_pct > 35 else "Günstige Windverhältnisse mit überwiegend Rücken- oder Seitenwind."
            ),
        },
        "terrain_and_surface": {
            "primary_surface": surface_data.get("primary_surface", "asphalt"),
            "rolling_resistance_crr": crr,
            "surface_breakdown": surface_data.get("surface_distribution", {}),
            "forest_shelter_pct": forest_shelter,
            "dem_elevation_check": dem_data.get("elevation_quality", "Unbekannt"),
        },
        "nutrition_and_fueling_strategy": {
            "calories_total_kcal": nutrition.get("energy_total_kcal"),
            "carbs_burned_grams": carbs_total_g,
            "carbs_burn_rate_g_per_hour": carbs_rate_gh,
            "fat_burned_grams": nutrition.get("fat", {}).get("burned_grams"),
            "sweat_loss_liters": sweat_liters,
            "hungerast_bonk_risk": nutrition["carbohydrates"]["hungerast_bonk_risk"],
            "fueling_checklist": fueling_plan,
        },
        "pacing_tactics": {
            "rider_profile": f"{rider_weight_kg:.0f} kg Bergfahrer",
            "flat_max_watts": round(cfg["base_flat_w"], 1),
            "climb_limit_watts": round(cfg["climb_limit_w"], 1),
            "climb_w_kg": round(cfg["climb_w_kg"], 1),
            "critical_dropoff_km": round(total_dist_km * 0.7, 1),
            "guidelines": (
                f"In der Ebene maximal {cfg['base_flat_w']:.0f} W halten. An Rampen über 6% auf maximal "
                f"{cfg['climb_limit_w']:.0f} W ({cfg['climb_w_kg']:.1f} W/kg) begrenzen, um Einbrüche ab km {total_dist_km * 0.7:.0f} zu verhindern."
            ),
        },
        "coaching_tactics": (
            f"In der Ebene maximal {cfg['base_flat_w']:.0f} W halten. An Rampen über 6% auf maximal "
            f"{cfg['climb_limit_w']:.0f} W ({cfg['climb_w_kg']:.1f} W/kg) begrenzen, um Einbrüche ab km {total_dist_km * 0.7:.0f} zu verhindern."
        ),
    }
