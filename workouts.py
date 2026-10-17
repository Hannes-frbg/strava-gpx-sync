"""
workouts.py: Strukturierter Trainingsgenerator und Workout-Export (.zwo für Zwift / Radcomputer).

Generiert physiologisch fundierte Intervalleinheiten:
- Grundlagenausdauer (GA1 / Zone 2 mit Trittfrequenz-Drills)
- Sweetspot (88-93% Schwellenleistung)
- VO2max Intervalle (4x4 Min oder 30/30s)
- Over-Under Schwellen-Adaptation
- Aktive Regeneration (Zone 1)
- Souplesse- & Trittfrequenz-Pyramide für Nachwuchsathleten

Liefert strukturierte Intervalle und valides Zwift .zwo XML.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET


def generate_structured_workout(
    workout_type: str = "sweetspot_intervals",
    target_duration_minutes: int = 60,
    intensity_level: str = "moderate",
    ftp_watts: Optional[float] = 230.0,
    hr_max: int = 205,
    export_format: str = "json",
) -> Dict[str, Any]:
    """
    Erstellt ein maßgeschneidertes, sportwissenschaftlich strukturiertes Intervall-Workout.
    """
    wt = workout_type.strip().lower()
    ftp = float(ftp_watts or 230.0)
    dur = max(30, min(240, int(target_duration_minutes)))

    intervals: List[Dict[str, Any]] = []
    title = ""
    description = ""
    primary_zone = ""

    if any(k in wt for k in ["sweetspot", "sst"]):
        title = "Sweetspot Threshold Builder (3x 10m SST)"
        description = "Gezielter aerober Kapazitätsaufbau knapp unter der anaeroben Schwelle (88-93% FTP). Maximaler Trainingseffekt bei kontrollierter Ermüdung."
        primary_zone = "Zone 3/4 (Sweetspot)"

        # Warmup 12m
        intervals.append({"name": "Warmup", "duration_sec": 720, "power_pct_ftp": 0.60, "hr_min": int(hr_max * 0.60), "hr_max": int(hr_max * 0.72), "cadence": 90, "type": "Warmup"})

        # 3x 10m SST mit 3m Pause
        sets = 3 if dur >= 60 else 2
        sst_dur = 600 if dur >= 60 else 480
        for i in range(1, sets + 1):
            intervals.append({"name": f"Sweetspot Intervall {i}", "duration_sec": sst_dur, "power_pct_ftp": 0.90, "hr_min": int(hr_max * 0.85), "hr_max": int(hr_max * 0.92), "cadence": 92, "type": "Interval"})
            if i < sets:
                intervals.append({"name": f"Aktive Pause {i}", "duration_sec": 180, "power_pct_ftp": 0.55, "hr_min": int(hr_max * 0.55), "hr_max": int(hr_max * 0.68), "cadence": 88, "type": "Rest"})

        # Cooldown
        total_used = sum(step["duration_sec"] for step in intervals)
        cooldown_s = max(300, (dur * 60) - total_used)
        intervals.append({"name": "Cooldown", "duration_sec": cooldown_s, "power_pct_ftp": 0.52, "hr_min": int(hr_max * 0.50), "hr_max": int(hr_max * 0.65), "cadence": 85, "type": "Cooldown"})

    elif any(k in wt for k in ["vo2max", "vo2", "peak"]):
        title = "VO2max High-Intensity Boost (4x 4m @ 108% FTP)"
        description = "Kardiovaskuläre Spitzenbelastung zur Erhöhung des Schlagvolumens und der maximalen Sauerstoffaufnahme."
        primary_zone = "Zone 5 (VO2max)"

        intervals.append({"name": "Warmup progressiv", "duration_sec": 720, "power_pct_ftp": 0.62, "hr_min": int(hr_max * 0.60), "hr_max": int(hr_max * 0.75), "cadence": 90, "type": "Warmup"})
        intervals.append({"name": "Pace-Opener (3x 30s High RPM)", "duration_sec": 240, "power_pct_ftp": 0.80, "hr_min": int(hr_max * 0.70), "hr_max": int(hr_max * 0.82), "cadence": 105, "type": "Interval"})

        reps = 4 if dur >= 55 else 3
        for i in range(1, reps + 1):
            intervals.append({"name": f"VO2max Block {i}", "duration_sec": 240, "power_pct_ftp": 1.08, "hr_min": int(hr_max * 0.90), "hr_max": int(hr_max * 0.96), "cadence": 98, "type": "Interval"})
            intervals.append({"name": f"Erholung {i}", "duration_sec": 180, "power_pct_ftp": 0.50, "hr_min": int(hr_max * 0.55), "hr_max": int(hr_max * 0.70), "cadence": 85, "type": "Rest"})

        total_used = sum(step["duration_sec"] for step in intervals)
        cooldown_s = max(300, (dur * 60) - total_used)
        intervals.append({"name": "Cooldown", "duration_sec": cooldown_s, "power_pct_ftp": 0.50, "hr_min": int(hr_max * 0.50), "hr_max": int(hr_max * 0.65), "cadence": 85, "type": "Cooldown"})

    elif any(k in wt for k in ["over_under", "overunder", "schwellen"]):
        title = "Over-Under Lactate Clearance (3x 9m)"
        description = "Schult den Laktatabbau unter Belastung: Abwechselnd 2 Min knapp unter der Schwelle (90%) und 1 Min darüber (105%)."
        primary_zone = "Zone 4 (Schwelle)"

        intervals.append({"name": "Warmup", "duration_sec": 720, "power_pct_ftp": 0.60, "hr_min": int(hr_max * 0.60), "hr_max": int(hr_max * 0.72), "cadence": 90, "type": "Warmup"})

        for block in range(1, 4):
            for sub in range(1, 4):
                intervals.append({"name": f"Under (Block {block}.{sub})", "duration_sec": 120, "power_pct_ftp": 0.90, "hr_min": int(hr_max * 0.85), "hr_max": int(hr_max * 0.90), "cadence": 90, "type": "Interval"})
                intervals.append({"name": f"Over (Block {block}.{sub})", "duration_sec": 60, "power_pct_ftp": 1.06, "hr_min": int(hr_max * 0.90), "hr_max": int(hr_max * 0.95), "cadence": 96, "type": "Interval"})
            if block < 3:
                intervals.append({"name": f"Serienpause {block}", "duration_sec": 240, "power_pct_ftp": 0.52, "hr_min": int(hr_max * 0.55), "hr_max": int(hr_max * 0.68), "cadence": 85, "type": "Rest"})

        total_used = sum(step["duration_sec"] for step in intervals)
        cooldown_s = max(300, (dur * 60) - total_used)
        intervals.append({"name": "Cooldown", "duration_sec": cooldown_s, "power_pct_ftp": 0.50, "hr_min": int(hr_max * 0.50), "hr_max": int(hr_max * 0.65), "cadence": 85, "type": "Cooldown"})

    elif any(k in wt for k in ["recovery", "rekom", "active_recovery"]):
        title = "Aktive Regeneration & Souplesse (Spinning)"
        description = "Förderung der Durchblutung und des Laktatabbaus ohne nennenswerte neuronale oder glykogenetische Belastung."
        primary_zone = "Zone 1 (Aktive Erholung)"

        tot_s = dur * 60
        intervals.append({"name": "Lockeres Einrollen", "duration_sec": int(tot_s * 0.25), "power_pct_ftp": 0.48, "hr_min": int(hr_max * 0.50), "hr_max": int(hr_max * 0.60), "cadence": 90, "type": "Warmup"})
        intervals.append({"name": "Souplesse Kurbeln (hohe Trittfrequenz)", "duration_sec": int(tot_s * 0.50), "power_pct_ftp": 0.52, "hr_min": int(hr_max * 0.52), "hr_max": int(hr_max * 0.62), "cadence": 96, "type": "Interval"})
        intervals.append({"name": "Ausrollen", "duration_sec": int(tot_s * 0.25), "power_pct_ftp": 0.45, "hr_min": int(hr_max * 0.48), "hr_max": int(hr_max * 0.58), "cadence": 88, "type": "Cooldown"})

    else:
        # Default: Base Endurance GA1 mit Trittfrequenz-Drills
        title = "Grundlagenausdauer GA1 mit Trittfrequenz-Pyramide"
        description = "Mitochondriendichte und Fettstoffwechsel optimieren. Eingestreute Kurbel-Drills schulen die neuromuskuläre Koordination."
        primary_zone = "Zone 2 (GA1)"

        intervals.append({"name": "Warmup", "duration_sec": 600, "power_pct_ftp": 0.58, "hr_min": int(hr_max * 0.55), "hr_max": int(hr_max * 0.68), "cadence": 90, "type": "Warmup"})
        main_dur = max(600, (dur * 60) - 1200)
        chunk = main_dur // 3
        intervals.append({"name": "GA1 Steady Pace", "duration_sec": chunk, "power_pct_ftp": 0.68, "hr_min": int(hr_max * 0.65), "hr_max": int(hr_max * 0.73), "cadence": 90, "type": "Interval"})
        intervals.append({"name": "GA1 mit Trittfrequenz-Drill (100 RPM)", "duration_sec": chunk, "power_pct_ftp": 0.70, "hr_min": int(hr_max * 0.68), "hr_max": int(hr_max * 0.75), "cadence": 100, "type": "Interval"})
        intervals.append({"name": "GA1 Steady Finish", "duration_sec": chunk, "power_pct_ftp": 0.68, "hr_min": int(hr_max * 0.65), "hr_max": int(hr_max * 0.73), "cadence": 90, "type": "Interval"})
        intervals.append({"name": "Cooldown", "duration_sec": 600, "power_pct_ftp": 0.50, "hr_min": int(hr_max * 0.50), "hr_max": int(hr_max * 0.62), "cadence": 85, "type": "Cooldown"})

    # Watt-Werte für jeden Schritt konkretisieren
    total_time_s = sum(step["duration_sec"] for step in intervals)
    for step in intervals:
        step["target_watts"] = round(step["power_pct_ftp"] * ftp, 0)
        step["duration_min"] = round(step["duration_sec"] / 60.0, 1)

    # Zwift ZWO XML Generator
    zwo_xml = _build_zwift_zwo_xml(title, description, intervals)

    return {
        "workout_title": title,
        "description": description,
        "primary_training_zone": primary_zone,
        "total_duration_minutes": round(total_time_s / 60.0, 1),
        "target_ftp_reference": ftp,
        "hr_max_reference": hr_max,
        "intervals_count": len(intervals),
        "intervals": intervals,
        "zwift_zwo_xml": zwo_xml,
    }


def _build_zwift_zwo_xml(title: str, description: str, intervals: List[Dict[str, Any]]) -> str:
    """Generiert standardkonformes Zwift Workout (.zwo) XML."""
    root = ET.Element("workout_file")
    ET.SubElement(root, "author").text = "Strava MCP KI-Coach"
    ET.SubElement(root, "name").text = title
    ET.SubElement(root, "description").text = description
    ET.SubElement(root, "sportType").text = "bike"
    workout_el = ET.SubElement(root, "workout")

    for step in intervals:
        st = step["type"]
        dur = str(step["duration_sec"])
        pwr = f"{step['power_pct_ftp']:.2f}"
        cad = str(step.get("cadence", 90))

        if st == "Warmup":
            el = ET.SubElement(workout_el, "Warmup", {
                "Duration": dur,
                "PowerLow": "0.50",
                "PowerHigh": pwr,
                "Cadence": cad,
            })
        elif st == "Cooldown":
            el = ET.SubElement(workout_el, "Cooldown", {
                "Duration": dur,
                "PowerLow": pwr,
                "PowerHigh": "0.45",
                "Cadence": cad,
            })
        elif st == "Rest":
            el = ET.SubElement(workout_el, "SteadyState", {
                "Duration": dur,
                "Power": pwr,
                "Cadence": cad,
            })
        else:
            el = ET.SubElement(workout_el, "SteadyState", {
                "Duration": dur,
                "Power": pwr,
                "Cadence": cad,
            })

    return ET.tostring(root, encoding="utf-8").decode("utf-8")
