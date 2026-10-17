"""
sports_science.py: Wissenschaftliche Stoffwechsel-, Ernährungs- und Nachwuchs-Leistungsdiagnostik.

Modelle:
1. Substratoxidation nach Asker Jeukendrup & J.A. Romijn:
   - Berechnet exakt verbrannte Gramm Kohlenhydrate (Glykogen) und Fette aus mechanischer Arbeit (kJ) und HF-Zonen.
   - Ermittelt stündliche Verbrennungsrate (g/h) und Hungerast-Risiko (Bonking).
   - Gibt grammgenaue Verpflegungsempfehlungen (Carbs/h) und Schweißverlust (L/h).
2. VAM (Velocità Ascensionale Media) & U17-Leistungsdiagnostik:
   - Steigrate in Höhenmetern pro Stunde (m/h) und W/kg am Berg.
   - Klassifikation nach Nachwuchs-Leistungsklassen (BDR / UCI U17 Standards).
3. 100% lokal, deterministisch und ohne externe Abhängigkeiten.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def calculate_metabolic_and_nutrition(
    work_kj: float,
    duration_s: float,
    zones: list,
    avg_hr: Optional[float] = None,
    hr_max: int = 205,
    temp_c: float = 18.0,
) -> Dict[str, Any]:
    """
    Berechnet den realen Kohlenhydrat- und Fettverbrauch sowie den Flüssigkeitsbedarf
    nach den sportwissenschaftlichen Modellen von Jeukendrup & Romijn.
    """
    duration_h = max(0.01, duration_s / 3600.0)

    # Brutto-Wirkungsgrad des menschlichen Körpers auf dem Fahrrad (~21.5%)
    gross_efficiency = 0.215
    total_metabolic_kj = work_kj / gross_efficiency if gross_efficiency > 0 else 0.0
    total_kcal = total_metabolic_kj / 4.184

    # Falls keine mechanische Wattarbeit vorliegt (z. B. Indoor-Laufband, Ergometer oder Workout):
    # Kalorienberechnung anhand von Herzfrequenz und Belastungsdauer (Keytel et al. / Hiilloskorpi Modell)
    if total_kcal <= 0.0:
        if avg_hr and hr_max:
            rel_hr = max(0.4, min(1.0, avg_hr / hr_max))
            hourly_burn = 350.0 + (rel_hr * 550.0)
            total_kcal = hourly_burn * duration_h
            total_metabolic_kj = total_kcal * 4.184
            work_kj = total_metabolic_kj * gross_efficiency
        elif duration_s > 0:
            total_kcal = 450.0 * duration_h
            total_metabolic_kj = total_kcal * 4.184
            work_kj = total_metabolic_kj * gross_efficiency

    # Standard-Verteilung, falls keine Zonen vorliegen (aus avg_hr / hr_max)
    fat_ratio = 0.50
    carb_ratio = 0.50

    if zones and len(zones) >= 5:
        total_zone_time = sum(z.duration_sec for z in zones)
        if total_zone_time > 0:
            # Gewichtete Substratoxidation nach Intensitätszonen (Romijn et al.)
            # Z1 (<60%): 80% Fett, 20% KH
            # Z2 (60-70% / FatMax): 60% Fett, 40% KH
            # Z3 (70-80%): 35% Fett, 65% KH
            # Z4 (80-90% / Schwellenbereich): 15% Fett, 85% KH
            # Z5 (>90% / VO2max): 5% Fett, 95% KH
            zone_weights = [
                (zones[0].duration_sec, 0.20, 0.80),
                (zones[1].duration_sec, 0.40, 0.60),
                (zones[2].duration_sec, 0.65, 0.35),
                (zones[3].duration_sec, 0.85, 0.15),
                (zones[4].duration_sec, 0.95, 0.05),
            ]
            carb_kcal_sum = sum(dur * c_pct for dur, c_pct, _ in zone_weights) / total_zone_time
            carb_ratio = carb_kcal_sum
            fat_ratio = 1.0 - carb_ratio
    elif avg_hr and hr_max:
        intensity = min(1.0, avg_hr / hr_max)
        if intensity < 0.65:
            carb_ratio, fat_ratio = 0.30, 0.70
        elif intensity < 0.78:
            carb_ratio, fat_ratio = 0.55, 0.45
        elif intensity < 0.88:
            carb_ratio, fat_ratio = 0.80, 0.20
        else:
            carb_ratio, fat_ratio = 0.95, 0.05

    carb_kcal = total_kcal * carb_ratio
    fat_kcal = total_kcal * fat_ratio

    # Brennwerte: 4.1 kcal/g Kohlenhydrate, 9.3 kcal/g Fett
    carbs_burned_g = round(carb_kcal / 4.1, 1)
    fat_burned_g = round(fat_kcal / 9.3, 1)
    carbs_per_hour = round(carbs_burned_g / duration_h, 1)

    # Schweiß- und Flüssigkeitsverlust (abhängig von Temperatur und Leistung)
    # Basis: ca. 0.45 L/h bei 15°C + 0.035 L/h je °C über 15°C + Arbeitszuschlag
    heat_factor = max(0.0, temp_c - 15.0) * 0.035
    work_factor = (work_kj / duration_h / 200.0) * 0.15 if duration_h > 0 else 0.0
    sweat_rate_l_h = round(max(0.40, min(2.2, 0.45 + heat_factor + work_factor)), 2)
    total_sweat_liters = round(sweat_rate_l_h * duration_h, 2)

    # Glykogen-Erschöpfung & Hungerast-Risiko (Bonk Risk)
    # Typischer Glykogenspeicher bei Nachwuchssportlern: ca. 350-400g (Leber + Muskulatur)
    if carbs_burned_g > 250 and duration_h > 1.75:
        hungerast_risk = "SEHR HOCH (Gefahr des totalen Leistungseinbruchs / Bonk)"
        hungerast_note = "Deine Glykogenspeicher wurden fast vollständig geleert. Ohne regelmäßige Riegel/Gels auf dem Rad war ein massiver Einbruch am Ende unvermeidbar!"
    elif carbs_burned_g > 150 and duration_h > 1.25:
        hungerast_risk = "MODERAT"
        hungerast_note = "Spürbare Glykogenentleerung. Ausreichende Kohlenhydratzufuhr während der Fahrt zwingend erforderlich."
    else:
        hungerast_risk = "GERING"
        hungerast_note = "Die Glykogenspeicher waren für diese Belastungsdauer ausreichend gefüllt."

    # Verpflegungsempfehlung für ähnliche Einheiten
    if duration_h < 1.0:
        fueling_target = "0-20 g/h (Wasser und Elektrolyte reichen völlig aus)"
    elif duration_h <= 2.0:
        fueling_target = "30-60 g/h Kohlenhydrate (z. B. 1 Riegel oder 500ml Isodrink pro Stunde)"
    else:
        fueling_target = "60-90 g/h Kohlenhydrate (2:1 Maltodextrin/Fruktose Mix + ca. 600-800ml Wasser mit Natrium)"

    return {
        "energy_total_kcal": round(total_kcal, 0),
        "carbohydrates": {
            "burned_grams": carbs_burned_g,
            "burn_rate_g_per_hour": carbs_per_hour,
            "percentage_of_energy": round(carb_ratio * 100.0, 1),
            "fueling_target_during_ride": fueling_target,
            "hungerast_bonk_risk": hungerast_risk,
            "hungerast_assessment": hungerast_note,
        },
        "fat": {
            "burned_grams": fat_burned_g,
            "percentage_of_energy": round(fat_ratio * 100.0, 1),
        },
        "hydration": {
            "estimated_sweat_loss_liters": total_sweat_liters,
            "sweat_rate_l_per_hour": sweat_rate_l_h,
            "hydration_recommendation": f"Mindestens {round(total_sweat_liters * 0.8, 1)} Liter Flüssigkeit mit ca. 500mg Natrium pro Liter zuführen.",
        },
    }


def calculate_vam_and_youth_diagnostics(
    climb_elevation_gain_m: float,
    climb_duration_sec: float,
    climb_avg_watts: Optional[float],
    rider_weight_kg: float = 60.0,
) -> Dict[str, Any]:
    """
    Berechnet die VAM (Velocità Ascensionale Media in m/h) und klassifiziert
    die Kletterleistung nach offiziellen Nachwuchs-Standards (U17 BDR / UCI).
    """
    if climb_elevation_gain_m < 20.0 or climb_duration_sec < 60.0:
        return {
            "vam_available": False,
            "reason": "Zu geringe Höhendifferenz oder Dauer für valide VAM-Berechnung.",
        }

    climb_hours = climb_duration_sec / 3600.0
    vam = round(climb_elevation_gain_m / climb_hours, 1)

    rw = max(30.0, rider_weight_kg)
    w_kg = round(climb_avg_watts / rw, 2) if climb_avg_watts else None

    # U17 Klassifizierung nach Bund Deutscher Radfahrer (BDR) & Pro-Standards
    if vam >= 1250:
        cat_key = "NATIONAL_ELITE"
        cat_name = "Nationale U17-Spitzenklasse (Kader-Niveau)"
        desc = "Herausragende Kletterleistung! Entspricht der nationalen Spitze im Nachwuchsbereich."
    elif vam >= 1000:
        cat_key = "ADVANCED_RACER"
        cat_name = "Starkes U17-Rennniveau (Top-Gruppe)"
        desc = "Sehr starke Steigleistung. Auf diesem Niveau fährst du bei regionalen Nachwuchsrennen ganz vorne mit."
    elif vam >= 800:
        cat_key = "CLUB_ATHLETE"
        cat_name = "Ambitioniertes Vereinsniveau"
        desc = "Solides und wettkampffähiges Renntempo. Für Attacken an steilen Rampen fehlt noch etwas spezifische Kraftausdauer."
    elif vam >= 600:
        cat_key = "SOLID_ENDURANCE"
        cat_name = "Solides Grundlagenausdauer-Klettern"
        desc = "Gleichmäßiges Bergtempo im Grundlagenbereich (GA1/GA2). Keine akute Rennhärte."
    else:
        cat_key = "RECOVERY_OR_GENTLE"
        cat_name = "Regeneratives Klettern"
        desc = "Lockerer Kletterbereich ohne rennspezifische Intensität."

    return {
        "vam_available": True,
        "vam_m_per_hour": vam,
        "climb_elevation_m": round(climb_elevation_gain_m, 1),
        "climb_time_minutes": round(climb_duration_sec / 60.0, 1),
        "climb_watts_per_kg": w_kg,
        "youth_u17_benchmark": {
            "category_key": cat_key,
            "category_title": cat_name,
            "diagnostic_summary": desc,
            "target_for_next_level": (
                "Nächstes Ziel: VAM > 1.000 m/h bei > 4.0 W/kg über 10-15 Minuten anstiege."
                if vam < 1000 else "Ziel: Konstanz über mehrere aufeinanderfolgende Anstiege halten."
            ),
        },
    }


def calculate_live_bonk_and_fueling_horizon(
    current_duration_minutes: float,
    current_distance_km: float,
    avg_hr: Optional[float] = None,
    carbs_consumed_grams: float = 0.0,
    target_distance_km: Optional[float] = None,
    rider_weight_kg: float = 60.0,
    hr_max: int = 205,
) -> Dict[str, Any]:
    """
    Berechnet die dynamische Glykogen-Restreichweite (Bonk-Countdown) wie bei einem Elektroauto
    auf Basis von Körpergewicht, Intensitätszone (Jeukendrup-Modell) und zugeführter Nahrung.
    """
    dur_min = max(0.1, current_duration_minutes)
    dur_h = dur_min / 60.0

    # Typischer Glykogenspeicher beim Ausdauerathleten: ca. 6.2g je kg Körpergewicht (Muskeln + Leber)
    total_glycogen_capacity_g = round(rider_weight_kg * 6.2, 1)

    # Stündliche Verbrennungsrate in Abhängigkeit von der Herzfrequenz
    if avg_hr and hr_max:
        intensity = min(1.0, max(0.4, avg_hr / hr_max))
        if intensity < 0.65:
            hourly_carb_burn_g = 35.0  # Hohe Fettverbrennung
        elif intensity < 0.75:
            hourly_carb_burn_g = 65.0  # Aerobe GA1-Mischverbrennung
        elif intensity < 0.85:
            hourly_carb_burn_g = 105.0  # Tempo/Sweetspot (starke Glykogenabhängigkeit)
        else:
            hourly_carb_burn_g = 145.0  # Schwellen-/VO2max-Bereich (>90% Kohlenhydrate)
    else:
        hourly_carb_burn_g = 65.0

    total_carbs_burned_g = round(hourly_carb_burn_g * dur_h, 1)
    effective_carbs_remaining_g = max(
        0.0,
        round(total_glycogen_capacity_g - total_carbs_burned_g + carbs_consumed_grams, 1),
    )

    pct_remaining = round((effective_carbs_remaining_g / total_glycogen_capacity_g) * 100.0, 1)

    # Restzeit und Reststrecke bis zum Hungerast (Bonking)
    carb_burn_per_min = hourly_carb_burn_g / 60.0
    minutes_until_bonk = (
        round(effective_carbs_remaining_g / carb_burn_per_min, 1)
        if carb_burn_per_min > 0
        else 999.0
    )

    avg_speed_kmh = (current_distance_km / dur_h) if (current_distance_km > 0 and dur_h > 0) else 25.0
    km_until_bonk = round((minutes_until_bonk / 60.0) * avg_speed_kmh, 1)

    # Status und Handlungsempfehlungen
    if pct_remaining < 15.0 or minutes_until_bonk < 20.0:
        status_key = "CRITICAL_BONK_IMMOBILIZATION"
        status_text = "AKUTES HUNGERAST-RISIKO (Bonk imminent!)"
        action = "SOFORT 30-50g schnelle Kohlenhydrate zuführen (Gel oder Isodrink), Tempo sofort auf Z1 drosseln!"
    elif pct_remaining < 30.0 or minutes_until_bonk < 45.0:
        status_key = "DEPLETION_WARNING"
        status_text = "Glykogenspeicher stark entleert (<30%)"
        action = "Nächste Verpflegung einleiten: 30g Kohlenhydrate in den nächsten 10 Minuten essen."
    elif pct_remaining < 60.0:
        status_key = "NORMAL_ENDURANCE"
        status_text = "Speicher im normalen Arbeitsbereich"
        action = "Regelmäßig alle 30 Minuten 20-30g Kohlenhydrate nachschieben."
    else:
        status_key = "FULL_BATTERY"
        status_text = "Speicher gut gefüllt"
        action = "Aktuell keine Eile, Wasser und Elektrolyte reichen aus."

    target_distance_feasible = True
    deficit_at_finish_g = 0.0
    if target_distance_km and target_distance_km > current_distance_km:
        remaining_dist = target_distance_km - current_distance_km
        needed_min = (remaining_dist / avg_speed_kmh) * 60.0
        needed_carbs = (needed_min / 60.0) * hourly_carb_burn_g
        if needed_carbs > effective_carbs_remaining_g:
            target_distance_feasible = False
            deficit_at_finish_g = round(needed_carbs - effective_carbs_remaining_g, 1)

    return {
        "glycogen_capacity_grams": total_glycogen_capacity_g,
        "carbs_burned_grams": total_carbs_burned_g,
        "carbs_consumed_grams": carbs_consumed_grams,
        "carbs_remaining_grams": effective_carbs_remaining_g,
        "glycogen_battery_pct": pct_remaining,
        "burn_rate_g_per_hour": round(hourly_carb_burn_g, 1),
        "bonk_countdown": {
            "minutes_remaining": minutes_until_bonk,
            "km_remaining": km_until_bonk,
            "status": status_key,
            "status_description": status_text,
            "recommended_action": action,
        },
        "target_horizon": {
            "target_distance_km": target_distance_km,
            "is_feasible_without_fueling": target_distance_feasible,
            "projected_carb_deficit_grams": deficit_at_finish_g,
        },
    }


def calculate_dynamic_sweat_and_dehydration(
    temperature_c: float,
    duration_minutes: float,
    avg_hr: Optional[float] = None,
    fluid_consumed_ml: float = 0.0,
    humidity_pct: float = 50.0,
    rider_weight_kg: float = 60.0,
    hr_max: int = 205,
) -> Dict[str, Any]:
    """
    Berechnet den dynamischen Schweiß- und Natriumverlust sowie das physiologische
    Flüssigkeitsdefizit (% des Körpergewichts) und gibt präzise Trinkmengen vor.
    """
    dur_h = max(0.01, duration_minutes / 60.0)

    # Basis-Schweißrate: ~0.45 L/h bei 15°C
    base_rate = 0.45
    temp_factor = max(0.0, temperature_c - 15.0) * 0.038
    humidity_factor = max(0.0, humidity_pct - 50.0) * 0.0035

    rel_hr = (avg_hr / hr_max) if (avg_hr and hr_max) else 0.72
    intensity_factor = max(0.0, rel_hr - 0.55) * 0.55

    sweat_rate_l_h = round(min(2.4, max(0.40, base_rate + temp_factor + humidity_factor + intensity_factor)), 2)
    total_sweat_ml = round(sweat_rate_l_h * dur_h * 1000.0, 1)

    # Typischer Natriumverlust im Ausdauersport: ~850 mg Natrium pro Liter Schweiß
    sodium_lost_mg = round(sweat_rate_l_h * dur_h * 850.0, 0)

    net_deficit_ml = max(0.0, total_sweat_ml - fluid_consumed_ml)
    deficit_pct_body_weight = round((net_deficit_ml / (rider_weight_kg * 1000.0)) * 100.0, 2)

    # Sportwissenschaftliche Dehydrationsstufen (Armstrong et al. / Noakes)
    if deficit_pct_body_weight >= 2.5:
        severity = "HIGH_PERFORMANCE_LOSS"
        alert_title = "Kritische Dehydration (Leistungsabfall >15%)"
        alert_msg = f"Flüssigkeitsdefizit beträgt {deficit_pct_body_weight}% deines Gewichts. Blutvolumen und Schlagvolumen sinken rapide!"
        rec_drink_ml = round(net_deficit_ml * 0.8, 0)
    elif deficit_pct_body_weight >= 1.6:
        severity = "MODERATE_DEHYDRATION"
        alert_title = "Spürbares Flüssigkeitsdefizit (Kardiovaskulärer Drift)"
        alert_msg = f"Defizit von {deficit_pct_body_weight}% erreicht. Herzfrequenz klettert ohne Mehrleistung."
        rec_drink_ml = round(net_deficit_ml * 0.7, 0)
    elif deficit_pct_body_weight >= 0.8:
        severity = "MILD_HYDRATION_NEED"
        alert_title = "Leichtes Flüssigkeitsdefizit"
        alert_msg = "Im tolerablen Bereich, regelmäßiges Trinken empfohlen."
        rec_drink_ml = round(net_deficit_ml * 0.6, 0)
    else:
        severity = "OPTIMAL_HYDRATED"
        alert_title = "Optimal hydriert"
        alert_msg = "Flüssigkeitshaushalt im grünen Bereich."
        rec_drink_ml = 200.0

    return {
        "sweat_rate_l_per_hour": sweat_rate_l_h,
        "total_sweat_lost_ml": total_sweat_ml,
        "sodium_lost_mg": sodium_lost_mg,
        "fluid_consumed_ml": fluid_consumed_ml,
        "net_fluid_deficit_ml": round(net_deficit_ml, 1),
        "fluid_deficit_pct_body_weight": deficit_pct_body_weight,
        "dehydration_severity": severity,
        "alert_title": alert_title,
        "alert_message": alert_msg,
        "immediate_drinking_target_ml": rec_drink_ml,
        "recommended_sodium_mg": round((rec_drink_ml / 1000.0) * 500.0, 0),
    }


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
) -> Dict[str, Any]:
    """
    Berechnet den wahren Windschatten- oder Gegenwind-Einfluss auf einem Strava-Segment.
    Zeigt als 'Virtual Ghost' an, ob der Athlet physisch vor oder hinter seiner Bestzeit liegt,
    bereinigt um den physikalischen Windvektor.
    """
    if segment_distance_m <= 0 or current_speed_kmh <= 0:
        return {"status": "error", "message": "Ungültige Distanz oder Geschwindigkeit."}

    v_ground_ms = current_speed_kmh / 3.6
    current_time_s = segment_distance_m / v_ground_ms

    # Scheinbarer Windwinkel (Anströmung)
    rel_angle_deg = (wind_direction_deg - segment_bearing_deg) % 360.0
    rad = math.radians(rel_angle_deg)
    headwind_comp_kmh = wind_speed_kmh * math.cos(rad)
    headwind_comp_ms = headwind_comp_kmh / 3.6

    apparent_air_speed_ms = max(0.0, v_ground_ms + headwind_comp_ms)

    # Aerodynamischer Widerstand mit echtem Wind
    f_aero_actual = 0.5 * air_density_kg_m3 * cda * (apparent_air_speed_ms ** 2)
    p_aero_actual = f_aero_actual * v_ground_ms

    # Aerodynamischer Widerstand bei Windstille (Nullwind-Referenz)
    f_aero_calm = 0.5 * air_density_kg_m3 * cda * (v_ground_ms ** 2)
    p_aero_calm = f_aero_calm * v_ground_ms

    power_delta_watts = round(p_aero_actual - p_aero_calm, 1)

    # Wind-bereinigte Äquivalenzgeschwindigkeit
    # Wie schnell wäre der Fahrer bei Windstille mit derselben mechanischen Leistung gefahren?
    if power_delta_watts > 0:
        # Bei Gegenwind: Fahrer leistete mehr als die Geschwindigkeit anzeigt
        wind_penalty_kmh = round(min(8.0, (headwind_comp_kmh * 0.42)), 1)
        wind_adjusted_speed_kmh = round(current_speed_kmh + wind_penalty_kmh, 1)
    else:
        # Bei Rückenwind: Fahrer profitierte vom Wind
        wind_boost_kmh = round(min(8.0, (abs(headwind_comp_kmh) * 0.38)), 1)
        wind_adjusted_speed_kmh = round(max(5.0, current_speed_kmh - wind_boost_kmh), 1)

    wind_adjusted_time_s = round(segment_distance_m / (wind_adjusted_speed_kmh / 3.6), 1)

    # Virtual Ghost Vergleich (Reale Uhr vs. Physikalische Leistung)
    raw_delta_pr_s = round(current_time_s - baseline_pr_seconds, 1)
    wind_adjusted_delta_pr_s = round(wind_adjusted_time_s - baseline_pr_seconds, 1)

    if wind_adjusted_delta_pr_s < 0:
        ghost_verdict = f"Physisch {abs(wind_adjusted_delta_pr_s):.1f}s VOR dem Allzeit-Rekord (Gegenwind stiehlt dir Tempo!)"
    else:
        ghost_verdict = f"Physisch {wind_adjusted_delta_pr_s:.1f}s hinter dem Allzeit-Rekord."

    return {
        "segment_name": segment_name,
        "segment_distance_m": round(segment_distance_m, 1),
        "actual_speed_kmh": round(current_speed_kmh, 1),
        "actual_time_seconds": round(current_time_s, 1),
        "baseline_pr_seconds": baseline_pr_seconds,
        "wind_aerodynamics": {
            "wind_speed_kmh": wind_speed_kmh,
            "headwind_component_kmh": round(headwind_comp_kmh, 1),
            "wind_type": "Gegenwind" if headwind_comp_kmh > 2 else ("Rückenwind" if headwind_comp_kmh < -2 else "Neutraler Seitenwind"),
            "aerodynamic_power_delta_watts": power_delta_watts,
        },
        "wind_normalized_performance": {
            "wind_adjusted_speed_kmh": wind_adjusted_speed_kmh,
            "wind_adjusted_time_seconds": wind_adjusted_time_s,
            "raw_time_delta_vs_pr_seconds": raw_delta_pr_s,
            "wind_normalized_delta_vs_pr_seconds": wind_adjusted_delta_pr_s,
            "virtual_ghost_verdict": ghost_verdict,
        },
    }


def analyze_gravel_vibration_and_tire_pressure(
    surface_type: str = "gravel",
    tire_width_mm: float = 40.0,
    current_pressure_bar: Optional[float] = None,
    vibration_g_peak: Optional[float] = None,
    total_system_weight_kg: float = 72.9,
) -> Dict[str, Any]:
    """
    Sportwissenschaftliche Vibrations- und Reifendruckanalyse nach dem Silca/Poertner Impedanzmodell.
    Ermittelt den optimalen Reifendruck für das Systemgewicht und berechnet die Verlustleistung (Watt).
    """
    # Silca Berto/Poertner Basisdruck für 700c Laufräder bei ~73kg Systemgewicht
    # Feiner Schotter verlangt ~2.3 Bar, grober Schotter ~2.0 Bar, Asphalt ~2.9 Bar
    st = surface_type.lower()
    if any(k in st for k in ["asphalt", "paved", "road"]):
        optimal_pressure_bar = round(max(2.2, 3.2 - ((tire_width_mm - 32) * 0.05)), 2)
        desc = "Glatter Asphalt"
        base_grip = 9.5
    elif any(k in st for k in ["compact", "fine", "fein"]):
        optimal_pressure_bar = round(max(1.8, 2.5 - ((tire_width_mm - 38) * 0.04)), 2)
        desc = "Festgefahrener Feinschotter"
        base_grip = 8.0
    elif any(k in st for k in ["cobblestone", "pflaster"]):
        optimal_pressure_bar = round(max(1.7, 2.1 - ((tire_width_mm - 38) * 0.04)), 2)
        desc = "Kopfsteinpflaster / grober Schotter"
        base_grip = 6.5
    else:
        # Standard Gravel
        optimal_pressure_bar = round(max(1.8, 2.3 - ((tire_width_mm - 40) * 0.04)), 2)
        desc = "Standard Schotter / Waldweg"
        base_grip = 7.0

    # Gewichtsadaption (+/- 0.02 Bar je kg Differenz zu 73kg)
    weight_delta = total_system_weight_kg - 72.9
    optimal_pressure_bar = round(optimal_pressure_bar + (weight_delta * 0.02), 2)

    impedance_loss_watts = 0.0
    pressure_diagnosis = "Optimaler Reifendruck"

    if current_pressure_bar:
        diff = round(current_pressure_bar - optimal_pressure_bar, 2)
        if diff > 0.4:
            # Reifen zu hart: springt über Kieselsteine statt abzurollen
            impedance_loss_watts = round(min(28.0, diff * 16.0), 1)
            pressure_diagnosis = (
                f"Reifen zu hart (+{diff} Bar über Optimum)! Das Rad vibriert stark. "
                f"Du verlierst ca. {impedance_loss_watts} Watt durch hochfrequente Impedanzverluste."
            )
        elif diff < -0.5:
            # Reifen zu weich: Walkarbeit und Durchschlagsgefahr
            impedance_loss_watts = round(min(15.0, abs(diff) * 10.0), 1)
            pressure_diagnosis = f"Reifen zu weich ({diff} Bar unter Optimum)! Erhöhter Rollwiderstand und Durchschlagsgefahr."
        else:
            pressure_diagnosis = "Reifendruck liegt perfekt im sportwissenschaftlichen Effizienz-Fenster."

    # Vibrationsanalyse über g-Kraft (IMU Sensor)
    vibration_assessment = "Nicht gemessen"
    grip_index = base_grip
    if vibration_g_peak:
        if vibration_g_peak > 3.8:
            vibration_assessment = "Sehr starke Schläge (>3.8g). Hohes Rutschrisiko auf losem Schotter!"
            grip_index = max(2.0, base_grip - 3.5)
        elif vibration_g_peak > 2.2:
            vibration_assessment = "Spürbares Schotter-Rumpeln (2.2g - 3.8g). Solide Traktion."
            grip_index = max(4.0, base_grip - 1.5)
        else:
            vibration_assessment = "Sanfte Fahrbahndämpfung (<2.2g). Hohe Laufruhe."

    return {
        "surface_analyzed": desc,
        "tire_width_mm": tire_width_mm,
        "total_system_weight_kg": total_system_weight_kg,
        "optimal_recommended_pressure_bar": optimal_pressure_bar,
        "current_pressure_bar": current_pressure_bar,
        "estimated_impedance_loss_watts": impedance_loss_watts,
        "pressure_verdict": pressure_diagnosis,
        "vibration_telemetry": {
            "g_force_peak": vibration_g_peak,
            "vibration_assessment": vibration_assessment,
            "grip_safety_index_1_to_10": round(grip_index, 1),
        },
    }


def calculate_skiba_w_prime_balance(
    power_samples: List[float],
    critical_power_watts: float = 230.0,
    w_prime_kj: float = 16.0,
    sample_interval_sec: float = 1.0,
) -> Dict[str, Any]:
    """
    Berechnet die dynamische W'-Balance (den anaeroben Akku) nach Dr. Philip Skiba.
    Sobald die Leistung über der Critical Power (CP) liegt, leert sich der Akku;
    liegt sie darunter, lädt er sich exponentiell wieder auf.
    """
    if not power_samples:
        return {"status": "error", "message": "Keine Leistungsdaten übergeben."}

    w_prime_joules = w_prime_kj * 1000.0
    current_w_prime = w_prime_joules
    min_w_prime = w_prime_joules
    depletion_events = 0
    surges_above_cp = 0

    history: List[float] = []

    for p in power_samples:
        if p > critical_power_watts:
            surges_above_cp += 1
            # Entleerung
            expenditure = (p - critical_power_watts) * sample_interval_sec
            current_w_prime = max(0.0, current_w_prime - expenditure)
        else:
            # Dynamische Rekonstitution nach Skiba (2012)
            d_cp = critical_power_watts - p
            # Erholungszeitkonstante tau
            tau = 546.0 * math.exp(-0.01 * d_cp) + 316.0
            rec = (w_prime_joules - current_w_prime) * (1.0 - math.exp(-sample_interval_sec / max(1.0, tau)))
            current_w_prime = min(w_prime_joules, current_w_prime + rec)

        if current_w_prime < min_w_prime:
            min_w_prime = current_w_prime
        if (current_w_prime / w_prime_joules) < 0.20:
            depletion_events += 1

        history.append(round(current_w_prime / 1000.0, 2))

    min_pct = round((min_w_prime / w_prime_joules) * 100.0, 1)

    if min_pct < 10.0:
        verdict = f"Extrem kritische Entleerung ({min_pct}% Restakku). Nahe am vollständigen Laktat-Kollaps!"
    elif min_pct < 30.0:
        verdict = f"Starke anaerobe Belastung ({min_pct}% Tiefststand). Solide anaerobe Kapazität genutzt."
    else:
        verdict = f"Kontrolliert aerob gefahren. Anaerober Akku blieb weitgehend geschont ({min_pct}% Minimum)."

    return {
        "critical_power_watts": critical_power_watts,
        "initial_w_prime_kj": w_prime_kj,
        "min_w_prime_kj": round(min_w_prime / 1000.0, 2),
        "min_w_prime_pct": min_pct,
        "surges_above_cp_count": surges_above_cp,
        "depleted_seconds_below_20pct": depletion_events * sample_interval_sec,
        "w_prime_diagnosis": verdict,
        "w_prime_history_kj_sample": history[::max(1, len(history) // 30)],  # Kompakte Vorschau
    }


def generate_live_coach_whisper(
    speed_kmh: float,
    hr: Optional[int] = None,
    hr_max: int = 205,
    slope_pct: float = 0.0,
    duration_min: float = 0.0,
    wind_headwind_kmh: float = 0.0,
    temperature_c: float = 20.0,
    vibration_level: str = "normal",
    current_situation: Optional[str] = None,
    rider_name: str = "Athlet",
) -> Dict[str, Any]:
    """
    Generiert einen ultra-prägnanten, motivierenden Live-Coach-Whisper (12-18 Wörter)
    für den Fahrradcomputer / Garmin-Bildschirm, der sportwissenschaftlich fundiert
    sofort Orientierung und Motivation liefert.
    """
    rel_hr = (hr / hr_max) if (hr and hr_max) else None
    whisper_text = ""

    # 1. Spezifische Situationen priorisieren
    if wind_headwind_kmh > 14.0 and slope_pct < 2.5:
        # Starker Gegenwind im Flachen
        if rel_hr and rel_hr > 0.85:
            whisper_text = f"Gegenwind frisst Watt (+{wind_headwind_kmh:.0f} km/h). Geh in den Unterlenker, nimm Druck raus!"
        else:
            whisper_text = f"Stark gebolzt im Gegenwind (+{wind_headwind_kmh:.0f} km/h). Klein machen, aero bleiben und Druck halten!"

    elif slope_pct > 7.0:
        # Steiler Anstieg
        if rel_hr and rel_hr > 0.90:
            whisper_text = "Puls am Anschlag! Zwei Gänge leichter, Trittfrequenz hoch halten und gleichmäßig atmen."
        else:
            whisper_text = f"Starker Bergantritt bei {slope_pct:.1f}% Steigung. Oberkörper öffnen, sauber im Rhythmus bleiben!"

    elif vibration_level in ["rough", "high_chatter"]:
        # Rumpeliger Schotter
        whisper_text = "Rauher Schotter! Lenker locker greifen, Gewicht leicht nach hinten und rund pedalieren."

    elif temperature_c >= 28.0 and duration_min > 40.0:
        # Hitze
        whisper_text = f"Hitze kocht ({temperature_c:.0f}°C). Jetzt kräftigen Schluck mit Natrium trinken, Beine kühlen!"

    elif rel_hr and rel_hr < 0.65 and speed_kmh > 24.0:
        # Schöner GA1 Flow
        whisper_text = "Weltklasse Grundlagen-Pacing! Aerob voll im grünen Bereich – genau so weiterkurbeln."

    elif rel_hr and rel_hr > 0.92:
        # Roter Bereich
        whisper_text = "VO2max Peak! Wenn es ein Intervall ist: Zähne zusammenbeißen. Wenn GA1: Sofort drosseln!"

    elif current_situation and "berg" in current_situation.lower():
        whisper_text = "Rhythmus halten, gleichmäßig atmen. Der Gipfel gehört dir!"

    else:
        # Standard motivierender Begleiter
        if duration_min > 90.0:
            whisper_text = "Starke Ausdauer! Denk an Riegel oder Gel – Glykogenspeicher jetzt aktiv auffüllen."
        else:
            whisper_text = "Läuft richtig flüssig! Trittfrequenz stabil halten und die Beine kreisen lassen."

    return {
        "live_coach_whisper": whisper_text,
        "telemetry_context": {
            "speed_kmh": round(speed_kmh, 1),
            "hr_bpm": hr,
            "hr_rel_pct": round(rel_hr * 100.0, 1) if rel_hr else None,
            "slope_pct": round(slope_pct, 1),
            "headwind_kmh": round(wind_headwind_kmh, 1),
            "temperature_c": round(temperature_c, 1),
            "duration_min": round(duration_min, 1),
            "vibration_level": vibration_level,
        },
    }


def estimate_vo2max_from_power(
    watts: float,
    rider_weight_kg: float = 60.0,
    age: int = 15,
    duration_s: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Berechnet die geschätzte maximale Sauerstoffaufnahme (VO2max in ml/kg/min)
    aus mechanischen Watt-Leistungen an Anstiegen oder Rampen-Tests nach den drei
    etablierten sportwissenschaftlichen Formeln (ACSM, Storer et al., Hawley & Noakes).
    """
    w = max(10.0, float(watts))
    weight = max(35.0, float(rider_weight_kg))
    a = max(12, int(age))
    w_kg = round(w / weight, 2)

    # 1. ACSM Formel für Radsport: VO2 = (10.8 * W / kg) + 7.0
    vo2_acsm = round((10.8 * w / weight) + 7.0, 1)

    # 2. Storer et al. Formel (Radsport-Stufentest/Berg):
    # VO2 = (10.51 * W + 6.38 * Gewicht - 10.7 * Alter + 413) / Gewicht
    vo2_storer = round((10.51 * w + 6.38 * weight - 10.7 * a + 413.0) / weight, 1)

    # 3. Hawley & Noakes (1992): Peak Power Modell
    vo2_hawley = round(((0.01141 * w + 0.435) / weight) * 1000.0, 1)

    # Konsenswert (Mittelwert)
    vo2_consensus = round((vo2_acsm + vo2_storer + vo2_hawley) / 3.0, 1)

    # BDR / U17 Nachwuchs-Klassifikation
    if vo2_consensus >= 65.0:
        level_key = "ELITE_NATIONAL"
        level_name = "BDR Bundeskader / Spitzenklasse (U17)"
        verdict = "Außergewöhnliche kardiovaskuläre Kapazität. Auf Niveau von nationalen Spitzen-Nachwuchsfahrern."
    elif vo2_consensus >= 58.0:
        level_key = "LANDESKADER"
        level_name = "Landeskader / Sehr stark ambitioniert (U17)"
        verdict = "Sehr hohe aerobe Leistungsfähigkeit. Exzellente Basis für Renn- und Lizenzsport."
    elif vo2_consensus >= 50.0:
        level_key = "ADVANCED"
        level_name = "Ambitionierter Nachwuchs-Radsportler"
        verdict = "Überdurchschnittliche Ausdauer. Solide Grundlage für Renneinsätze und schwere Bergetappen."
    elif vo2_consensus >= 42.0:
        level_key = "FIT"
        level_name = "Gut trainierter Jugendbereich"
        verdict = "Guter Fitnesszustand. Mit gezieltem Schwellen- und Intervalltraining ausbaufähig."
    else:
        level_key = "BASE"
        level_name = "Aufbau- / Grundlagenbereich"
        verdict = "Grundlegende Fitness. Fokus sollte zunächst auf extensivem GA1-Volumen liegen."

    return {
        "status": "success",
        "power_input": {
            "watts": round(w, 1),
            "watts_per_kg": w_kg,
            "rider_weight_kg": weight,
            "age": a,
            "duration_s": duration_s,
        },
        "vo2max_estimates": {
            "consensus_ml_kg_min": vo2_consensus,
            "acsm_ml_kg_min": vo2_acsm,
            "storer_ml_kg_min": vo2_storer,
            "hawley_noakes_ml_kg_min": vo2_hawley,
        },
        "u17_benchmark": {
            "level_key": level_key,
            "level_name": level_name,
            "verdict": verdict,
        },
    }


def calculate_aerobic_decoupling(
    points_or_metrics: Any,
    min_duration_sec: float = 1200.0,
) -> Dict[str, Any]:
    """
    Berechnet die aerobe Entkopplung (Aerobic Decoupling / Cardiac Drift nach Joe Friel)
    für eine Ausfahrt.

    Vergleicht den Efficiency Factor (EF = Leistung oder Tempo / Puls) der ersten Fahrthälfte
    mit der zweiten Fahrthälfte:
    Decoupling % = ((EF_1 - EF_2) / EF_1) * 100
    """
    # Unterstützt Liste von Punkten mit Zeit, HR und Speed/Watts
    if not points_or_metrics or len(points_or_metrics) < 20:
        return {
            "status": "insufficient_data",
            "message": "Zu wenige Trackpoints für eine kardiale Drift-Berechnung (mind. 20 Punkte nötig).",
            "decoupling_pct": None,
        }

    valid_pts = []
    for p in points_or_metrics:
        hr = getattr(p, "hr", None) or (p.get("hr") if isinstance(p, dict) else None)
        watts = getattr(p, "watts", None) or (p.get("watts") if isinstance(p, dict) else None)
        v = getattr(p, "speed_kmh", None) or (p.get("speed_kmh") if isinstance(p, dict) else None)
        if hr and hr > 50:
            valid_pts.append({"hr": float(hr), "watts": float(watts) if watts else None, "speed": float(v) if v else None})

    if len(valid_pts) < 20:
        return {
            "status": "no_hr_data",
            "message": "Keine ausreichenden Herzfrequenzdaten vorhanden.",
            "decoupling_pct": None,
        }

    mid = len(valid_pts) // 2
    h1 = valid_pts[:mid]
    h2 = valid_pts[mid:]

    avg_hr_1 = sum(x["hr"] for x in h1) / len(h1)
    avg_hr_2 = sum(x["hr"] for x in h2) / len(h2)

    # 1. Priorität: Leistung (Watts)
    w1 = [x["watts"] for x in h1 if x["watts"] is not None]
    w2 = [x["watts"] for x in h2 if x["watts"] is not None]

    use_watts = len(w1) > 10 and len(w2) > 10
    if use_watts:
        avg_output_1 = sum(w1) / len(w1)
        avg_output_2 = sum(w2) / len(w2)
        metric_name = "Pw:HR (Watt zu Herzfrequenz)"
    else:
        # Fallback auf Tempo
        s1 = [x["speed"] for x in h1 if x["speed"] and x["speed"] > 5.0]
        s2 = [x["speed"] for x in h2 if x["speed"] and x["speed"] > 5.0]
        if len(s1) > 10 and len(s2) > 10:
            avg_output_1 = sum(s1) / len(s1)
            avg_output_2 = sum(s2) / len(s2)
            metric_name = "Pa:HR (Tempo zu Herzfrequenz)"
        else:
            avg_output_1 = 1.0
            avg_output_2 = 1.0
            metric_name = "HR-Ratio"

    ef_1 = (avg_output_1 / avg_hr_1) if avg_hr_1 > 0 else 1.0
    ef_2 = (avg_output_2 / avg_hr_2) if avg_hr_2 > 0 else 1.0

    decoupling_pct = round(((ef_1 - ef_2) / ef_1) * 100.0, 1) if ef_1 > 0 else 0.0

    if decoupling_pct < 3.0:
        status_key = "OPTIMAL_AEROBIC"
        verdict = "Exzellente aerobe Basis. Nahezu keine kardiale Drift – die Sauerstoffversorgung ist vollkommen stabil."
    elif decoupling_pct <= 5.0:
        status_key = "SOLID_ENDURANCE"
        verdict = "Solide aerobe Ausdauer. Geringe kardiale Drift im physiologischen Normalbereich (<5%)."
    elif decoupling_pct <= 8.0:
        status_key = "MODERATE_DRIFT"
        verdict = "Moderate kardiale Drift. Mögliche Ursachen: Beginndende Dehydrierung, unzureichende Kohlenhydrate oder warme Witterung."
    else:
        status_key = "HIGH_DECOUPLING"
        verdict = "Signifikante Entkopplung (>8%)! Pulskriechen erkannt: Deutlicher Ermüdungseffekt oder akuter Flüssigkeitsmangel."

    return {
        "status": "success",
        "metric_type": metric_name,
        "decoupling_pct": decoupling_pct,
        "half_1": {
            "avg_hr": round(avg_hr_1, 1),
            "avg_output": round(avg_output_1, 1),
            "efficiency_factor": round(ef_1, 3),
        },
        "half_2": {
            "avg_hr": round(avg_hr_2, 1),
            "avg_output": round(avg_output_2, 1),
            "efficiency_factor": round(ef_2, 3),
        },
        "classification": {
            "status_key": status_key,
            "verdict": verdict,
            "acceptable_threshold_pct": 5.0,
        },
    }

