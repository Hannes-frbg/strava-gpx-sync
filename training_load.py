"""
training_load.py: Sportwissenschaftliches Belastungsmodell (CTL / ATL / TSB).

Berechnet fortlaufende Trainingsbelastung, Ermüdung und Formkurve
nach den etablierten Standards von Eric Banister und Andrew Coggan:
- TSS / Fallback-TSS: Tägliche Belastung je Aktivität (mit/ohne Herzfrequenz).
- CTL (Chronic Training Load): 42-Tage-Exponentialdurchschnitt (Fitness).
- ATL (Acute Training Load): 7-Tage-Exponentialdurchschnitt (Ermüdung).
- TSB (Training Stress Balance): CTL minus ATL (Form / Frische).
- Ramp Rate: Wöchentliche Zuwachsrate der Fitness zur Verletzungsprävention.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ActivityLoadEntry:
    activity_id: str
    name: str
    sport_type: str
    activity_date: date
    duration_s: float
    distance_m: float
    elevation_gain_m: float
    suffer_score: Optional[float] = None
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    calculated_tss: float = 0.0
    tss_source: str = "fallback"  # "suffer_score", "hrss", "fallback"


@dataclass
class DailyLoadRecord:
    date_str: str
    tss: float
    ctl: float
    atl: float
    tsb: float
    activity_count: int
    activities: List[Dict[str, Any]]


def calculate_fallback_tss(
    duration_s: float,
    distance_m: float = 0.0,
    elevation_gain_m: float = 0.0,
    sport_type: str = "Ride",
) -> float:
    """
    Berechnet einen sportwissenschaftlich fundierten Fallback-TSS (Training Stress Score)
    für Einheiten ohne Herzfrequenzmessung auf Basis von Zeit, Distanz, Höhenmetern und Sportart.

    Stellt die mathematische Kontinuität der CTL/ATL-Kurven sicher.
    """
    duration_h = max(0.0, duration_s / 3600.0)
    if duration_h <= 0.0:
        return 0.0

    st_clean = sport_type.lower()

    # 1. Sportart-spezifische Basis-Stundenbelastung (TSS / Stunde bei moderatem GA1-Tempo)
    if any(k in st_clean for k in ["run", "laufen", "jog"]):
        base_tss_per_hour = 65.0
        elev_cost = 15.0  # 1 TSS je 15m Höhenmeter beim Laufen
        baseline_speed_kmh = 10.0
    elif any(k in st_clean for k in ["gravel", "schotter"]):
        base_tss_per_hour = 52.0
        elev_cost = 22.0  # 1 TSS je 22m Höhenmeter auf Schotter
        baseline_speed_kmh = 21.0
    elif any(k in st_clean for k in ["mountainbike", "mtb"]):
        base_tss_per_hour = 55.0
        elev_cost = 20.0
        baseline_speed_kmh = 16.0
    elif any(k in st_clean for k in ["ride", "rad", "radfahren", "cycling"]):
        base_tss_per_hour = 45.0
        elev_cost = 25.0  # 1 TSS je 25m Höhenmeter auf der Straße
        baseline_speed_kmh = 25.0
    elif any(k in st_clean for k in ["hike", "wander", "walk", "spazier"]):
        base_tss_per_hour = 25.0
        elev_cost = 30.0
        baseline_speed_kmh = 4.5
    elif any(k in st_clean for k in ["swim", "schwimm"]):
        base_tss_per_hour = 60.0
        elev_cost = 999.0
        baseline_speed_kmh = 2.5
    elif any(k in st_clean for k in ["ski", "snowboard"]):
        base_tss_per_hour = 50.0
        elev_cost = 50.0
        baseline_speed_kmh = 15.0
    else:
        base_tss_per_hour = 40.0
        elev_cost = 25.0
        baseline_speed_kmh = 20.0

    # 2. Basis-Belastung aus Dauer
    base_tss = duration_h * base_tss_per_hour

    # 3. Höhenmeter-Zuschlag (Arbeit gegen die Schwerkraft: W = m * g * h)
    elev_gain = max(0.0, float(elevation_gain_m))
    elevation_tss = elev_gain / elev_cost if elev_cost > 0 else 0.0

    # 4. Geschwindigkeits-Intensitätsfaktor (optional, wenn Distanz vorhanden)
    speed_factor = 1.0
    if distance_m > 0 and duration_h > 0.05:
        speed_kmh = (distance_m / 1000.0) / duration_h
        ratio = speed_kmh / baseline_speed_kmh
        # Sanft cappen zwischen 0.75 und 1.35
        speed_factor = max(0.75, min(1.35, 0.5 + 0.5 * ratio))

    total_tss = (base_tss * speed_factor) + elevation_tss

    # Plausibilitätsgrenzen: Min 3 TSS je Einheit, Max 500 TSS für Extrem-Events
    return round(max(3.0, min(500.0, total_tss)), 1)


def calculate_activity_tss(
    duration_s: float,
    distance_m: float = 0.0,
    elevation_gain_m: float = 0.0,
    sport_type: str = "Ride",
    suffer_score: Optional[float] = None,
    avg_hr: Optional[float] = None,
    max_hr: Optional[float] = 184.0,
) -> tuple[float, str]:
    """
    Bestimmt den optimalen TSS-Wert für eine Aktivität:
    1. Primär: Herzfrequenz-basierter Suffer Score (Banister TRIMP skaliert auf Coggan TSS).
    2. Sekundär: hrTSS aus avg_hr / Schwellenherzfrequenz.
    3. Fallback: Zeit-/Höhenmeter-/Sportart-Modell.
    """
    # Fall 1: Suffer Score (Banister TRIMP) liegt vor
    if suffer_score is not None and suffer_score > 0:
        # TRIMP Skalierung: 1 Stunde Schwellenbereich entspricht ~120-140 TRIMP und 100 TSS.
        # Skalierungsfaktor 0.82 kalibriert den Suffer Score auf standardisierte TSS-Werte.
        calibrated_tss = round(suffer_score * 0.82, 1)
        return max(5.0, calibrated_tss), "suffer_score"

    # Fall 2: Durchschnitts-Herzfrequenz liegt vor (ohne vollständige Trackpoints)
    if avg_hr and avg_hr > 40 and max_hr and max_hr > avg_hr and duration_s > 0:
        # Schwellenherzfrequenz LTHR ~ 88% von HRmax
        lthr = max_hr * 0.88
        intensity_factor = avg_hr / lthr
        duration_h = duration_s / 3600.0
        # hrTSS = 100 * h * IF^2
        hrtss = round(100.0 * duration_h * (intensity_factor**2), 1)
        return max(5.0, min(500.0, hrtss)), "hrss"

    # Fall 3: Fallback ohne Herzfrequenz
    fb_tss = calculate_fallback_tss(duration_s, distance_m, elevation_gain_m, sport_type)
    return fb_tss, "fallback"


class TrainingLoadEngine:
    """
    Engine zur tagesgenauen Berechnung des Banister/Coggan Modells (CTL, ATL, TSB).
    """

    LAMBDA_CTL = 1.0 - math.exp(-1.0 / 42.0)  # ~0.02353 (42 Tage Zeitkonstante)
    LAMBDA_ATL = 1.0 - math.exp(-1.0 / 7.0)   # ~0.13312 (7 Tage Zeitkonstante)

    def __init__(self, profile: Optional[Dict[str, Any]] = None) -> None:
        self._activities: List[ActivityLoadEntry] = []
        self.profile: Dict[str, Any] = profile or {}

    def add_activity(
        self,
        activity_id: str,
        name: str,
        sport_type: str,
        activity_date: date,
        duration_s: float,
        distance_m: float,
        elevation_gain_m: float,
        suffer_score: Optional[float] = None,
        avg_hr: Optional[float] = None,
        max_hr: Optional[float] = 184.0,
    ) -> ActivityLoadEntry:
        """Fügt eine Aktivität hinzu und berechnet ihren TSS."""
        tss, source = calculate_activity_tss(
            duration_s=duration_s,
            distance_m=distance_m,
            elevation_gain_m=elevation_gain_m,
            sport_type=sport_type,
            suffer_score=suffer_score,
            avg_hr=avg_hr,
            max_hr=max_hr,
        )

        entry = ActivityLoadEntry(
            activity_id=str(activity_id),
            name=name,
            sport_type=sport_type,
            activity_date=activity_date,
            duration_s=duration_s,
            distance_m=distance_m,
            elevation_gain_m=elevation_gain_m,
            suffer_score=suffer_score,
            avg_hr=avg_hr,
            max_hr=max_hr,
            calculated_tss=tss,
            tss_source=source,
        )
        self._activities.append(entry)
        return entry

    def clear(self) -> None:
        self._activities.clear()

    def compute_daily_timeline(
        self,
        until_date: Optional[date] = None,
        lookback_days: int = 60,
    ) -> List[DailyLoadRecord]:
        """
        Berechnet die lückenlose tägliche Zeitreihe (TSS, CTL, ATL, TSB) von der
        frühesten Aktivität (oder lookback_days vor today) bis zum Stichtag (Standard: heute).
        """
        today = until_date or datetime.now(timezone.utc).date()

        if not self._activities:
            # Leere Historie: Letzte Tage mit 0 TSS erzeugen
            start_date = today - timedelta(days=min(lookback_days, 14))
            records: List[DailyLoadRecord] = []
            curr = start_date
            while curr <= today:
                records.append(
                    DailyLoadRecord(
                        date_str=curr.isoformat(),
                        tss=0.0,
                        ctl=0.0,
                        atl=0.0,
                        tsb=0.0,
                        activity_count=0,
                        activities=[],
                    )
                )
                curr += timedelta(days=1)
            return records

        # 1. Aktivitäten nach Datum gruppieren
        activities_by_day: Dict[date, List[ActivityLoadEntry]] = {}
        min_date = self._activities[0].activity_date
        for act in self._activities:
            d = act.activity_date
            if d not in activities_by_day:
                activities_by_day[d] = []
            activities_by_day[d].append(act)
            if d < min_date:
                min_date = d

        # Frühestes Startdatum wählen: mindestens 42 Tage vor der ersten Aktivität bzw. min_date
        # damit die Exponentialkurve (CTL) sich aufbauen kann.
        start_date = min(min_date, today - timedelta(days=lookback_days))

        # 2. Tageweise Zeitreihe iterieren
        records = []
        ctl = 0.0
        atl = 0.0

        curr = start_date
        while curr <= today:
            day_acts = activities_by_day.get(curr, [])
            day_tss = round(sum(a.calculated_tss for a in day_acts), 1)

            # EWMA Formel
            ctl = ctl + (day_tss - ctl) * self.LAMBDA_CTL
            atl = atl + (day_tss - atl) * self.LAMBDA_ATL
            tsb = ctl - atl

            records.append(
                DailyLoadRecord(
                    date_str=curr.isoformat(),
                    tss=day_tss,
                    ctl=round(ctl, 1),
                    atl=round(atl, 1),
                    tsb=round(tsb, 1),
                    activity_count=len(day_acts),
                    activities=[
                        {
                            "id": a.activity_id,
                            "name": a.name,
                            "sport_type": a.sport_type,
                            "tss": a.calculated_tss,
                            "source": a.tss_source,
                            "distance_km": round(a.distance_m / 1000.0, 1) if a.distance_m else 0.0,
                            "elevation_m": round(a.elevation_gain_m, 0),
                        }
                        for a in day_acts
                    ],
                )
            )
            curr += timedelta(days=1)

        return records

    def get_training_status(
        self,
        until_date: Optional[date] = None,
        health_data: Optional[Dict[str, Any]] = None,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Gibt eine sportwissenschaftliche Auswertung des aktuellen Trainingszustands zurück.
        Klassifiziert TSB in Formzonen, berechnet 7-Tage-Last, Ramp Rate und verknüpft die
        Apple-Health-Readiness im Recovery-Loop inklusive Übertrainings-Warnungen.
        """
        timeline = self.compute_daily_timeline(until_date=until_date)
        if not timeline:
            return {
                "status": "empty",
                "message": "Keine Trainingsdaten zur Berechnung der Belastungsmodelle vorhanden.",
            }

        latest = timeline[-1]
        current_ctl = latest.ctl
        current_atl = latest.atl
        current_tsb = latest.tsb

        # 7-Tage-Summe TSS
        last_7_days = timeline[-7:] if len(timeline) >= 7 else timeline
        last_7_days_tss = round(sum(r.tss for r in last_7_days), 1)

        # Ramp Rate (Änderung CTL über die letzten 7 Tage)
        ctl_7_days_ago = timeline[-7].ctl if len(timeline) >= 7 else timeline[0].ctl
        ramp_rate = round(current_ctl - ctl_7_days_ago, 1)

        # Form-Zone Klassifikation nach Andrew Coggan & Joe Friel
        if current_tsb > 25.0:
            zone_key = "TRANSITION"
            zone_name = "Übergangsphase / Regeneration"
            status_desc = "Sehr hohe Frische bei drohendem Formverlust. Die Trainingsreize der letzten Wochen waren sehr gering."
            recommendation = "Gezielt neue Trainingsimpulse setzen (Grundlage oder Tempo), um das Fitnessniveau zu halten."
            color = "gray"
        elif 10.0 <= current_tsb <= 25.0:
            zone_key = "PEAK"
            zone_name = "Höchstform / Wettkampfbereit (Tapering)"
            status_desc = "Optimale Balance aus hoher Frische und guter Fitness. Idealer Zustand für Rennen oder Bestzeiten."
            recommendation = "Perfekt für Wettkämpfe oder harte Schlüssel-Segmente. Danach wieder gezielt Belastung aufbauen."
            color = "green"
        elif -10.0 <= current_tsb < 10.0:
            zone_key = "NEUTRAL"
            zone_name = "Ausgeglichener Bereich (Maintenance)"
            status_desc = "Gleichgewicht zwischen Belastung und Erholung. Solide Fitness, keine akute Ermüdung."
            recommendation = "Guter Zustand für regelmäßiges Training. Zur Steigerung der Fitness können intensivere Reize gesetzt werden."
            color = "blue"
        elif -30.0 <= current_tsb < -10.0:
            zone_key = "PRODUCTIVE"
            zone_name = "Optimaler Trainingsreiz (Productive Overload)"
            status_desc = "Produktiver Trainingsbereich! Hoher Reiz für maximalen Trainingsfortschritt bei kontrollierter Ermüdung."
            recommendation = "Trainingsplan wie geplant fortführen. Auf ausreichenden Schlaf und Proteinzufuhr achten."
            color = "yellow"
        else:
            zone_key = "OVERLOAD"
            zone_name = "Hohe Überlastung / Erschöpfungsrisiko (High Fatigue)"
            status_desc = "Akute Überlastung! Die Ermüdung übersteigt die Regenerationsfähigkeit erheblich."
            recommendation = "Dringend 1-2 Ruhetage oder ausschließlich aktive Regeneration (Zone 1, <45min) einlegen, um Übertraining zu vermeiden."
            color = "red"

        # Ramp Rate Warnung
        ramp_warning: Optional[str] = None
        if ramp_rate > 7.0:
            ramp_warning = f"Achtung: Fitness-Anstieg (+{ramp_rate} CTL/Woche) ist sehr steil! Über 5-7 CTL/Woche steigt das Verletzungsrisiko stark an."
        elif ramp_rate < -5.0:
            ramp_warning = f"Hinweis: CTL fällt aktuell um {abs(ramp_rate)} Punkte/Woche ab (Detraining)."

        # -------------------------------------------------------------------
        # RECOVERY-LOOP: Apple Health Readiness ↔ Trainingsstatus verknüpfen
        # -------------------------------------------------------------------
        if health_data is None:
            try:
                from apple_health_service import sync_apple_health_from_ios
                health_data = sync_apple_health_from_ios(force=False)
            except Exception:
                try:
                    from rag_engine import ActivityVectorStore
                    health_data = ActivityVectorStore().get_latest_apple_health_metrics(user_id=user_id)
                except Exception:
                    health_data = None

        readiness_score: Optional[float] = None
        confidence_score: Optional[float] = None
        confidence_level: str = "unknown"
        confidence_warning: Optional[str] = None
        is_stale: bool = False
        cache_age_days: int = 0
        cache_warning: Optional[str] = None
        recorded_date: Optional[str] = None
        signals_avail: list[str] = []
        signals_miss: list[str] = []

        if health_data and isinstance(health_data, dict):
            latest_readiness = (
                health_data.get("latest_metrics")
                or health_data.get("latest_readiness")
                or (health_data if "readiness_score" in health_data else None)
            )
            if latest_readiness and isinstance(latest_readiness, dict):
                r_val = latest_readiness.get("readiness_score")
                if r_val is not None:
                    try:
                        readiness_score = float(r_val)
                    except (ValueError, TypeError):
                        readiness_score = None
                c_val = latest_readiness.get("confidence_score")
                if c_val is not None:
                    try:
                        confidence_score = float(c_val)
                    except (ValueError, TypeError):
                        confidence_score = None
                confidence_level = str(latest_readiness.get("confidence") or ("high" if confidence_score == 1.0 else "medium"))
                confidence_warning = latest_readiness.get("confidence_warning")
                recorded_date = latest_readiness.get("recorded_date")
                signals_avail = list(latest_readiness.get("signals_available", []))
                signals_miss = list(latest_readiness.get("signals_missing", []))

            is_stale = bool(health_data.get("is_stale", False))
            cache_age_days = int(health_data.get("cache_age_days") or 0)
            cache_warning = health_data.get("data_staleness_warning") or health_data.get("cache_warning")

            if not is_stale and recorded_date:
                try:
                    rec_dt = datetime.strptime(str(recorded_date)[:10], "%Y-%m-%d").date()
                    diff = (date.today() - rec_dt).days
                    if diff >= 3:
                        is_stale = True
                        cache_age_days = max(cache_age_days, diff)
                        cache_warning = f"Apple Health Daten sind {diff} Tage alt ({recorded_date}). Letzter Erholungs-Score spiegelt nicht den tagesaktuellen Vitalzustand wider."
                except Exception:
                    pass

        # Vergleich: TSB (Form) vs. Readiness
        overtraining_warning: Optional[str] = None
        comparison_text: str = ""

        if readiness_score is not None:
            if current_tsb < 0.0 and readiness_score < 50.0:
                overtraining_warning = (
                    f"Achtung: Akute Überlastungs- und Übertrainingsgefahr! Formwert ist negativ (TSB {current_tsb:+.1f}) "
                    f"bei gleichzeitig defizitärer Apple-Health-Readiness ({readiness_score:.0f}/100, "
                    f"Konfidenz: {int((confidence_score or 1.0)*100)}%). Erholungsfähigkeit ist überfordert – "
                    "dringend Entlastungswoche oder 1-2 Ruhetage einlegen!"
                )
                comparison_text = (
                    f"Kritische Diskrepanz: Negative Form (TSB {current_tsb:+.1f}) trifft auf mangelhafte autonome Erholung "
                    f"(Readiness {readiness_score:.0f}/100). Erschöpfung übersteigt Regenerationskapazität."
                )
            elif current_tsb < 0.0 and readiness_score >= 70.0:
                comparison_text = (
                    f"Stabile Belastungsadaptation: Trainingsbelastung ist hoch (TSB {current_tsb:+.1f}), aber die Vitalwerte "
                    f"(Readiness {readiness_score:.0f}/100) signalisieren eine robuste Erholung. Körper toleriert Reiz gut."
                )
            elif current_tsb >= 0.0 and readiness_score < 50.0:
                comparison_text = (
                    f"Extrinsisches Erholungsdefizit: Physische Frische vorhanden (TSB {current_tsb:+.1f}), jedoch eingeschränkte "
                    f"Readiness ({readiness_score:.0f}/100). Ursache liegt vermutlich nicht im Training (Schlafmangel, Alltagsstress, Infekt)."
                )
            else:
                comparison_text = (
                    f"Ausgeglichene Balance: TSB ({current_tsb:+.1f}) und Readiness ({readiness_score:.0f}/100) harmonieren."
                )
        else:
            comparison_text = "Keine aktuellen Apple-Health-Readiness-Daten verfügbar."

        recovery_context = {
            "readiness_score": readiness_score,
            "confidence_score": confidence_score,
            "confidence_level": confidence_level,
            "confidence_warning": confidence_warning,
            "recorded_date": recorded_date,
            "is_stale": is_stale,
            "cache_age_days": cache_age_days,
            "stale_warning": cache_warning if is_stale else None,
            "tsb_form": current_tsb,
            "tsb_vs_readiness_comparison": comparison_text,
            "overtraining_warning": overtraining_warning,
            "signals_available": signals_avail,
            "signals_missing": signals_miss,
        }

        # Formzone Dictionary mit erweiterter Übertrainings-Warnung
        form_zone_dict = {
            "key": zone_key,
            "title": zone_name,
            "description": status_desc,
            "recommendation": recommendation,
            "color": color,
            "ramp_warning": ramp_warning,
            "overtraining_warning": overtraining_warning,
        }

        # Datenqualitäts- und Anomalien-Block (etabliertes Muster)
        anomalies = []
        if is_stale:
            anomalies.append({
                "type": "apple_health_stale",
                "severity": "info",
                "title": "Apple Health Vitaldaten veraltet",
                "description": cache_warning or f"Apple Health Daten sind {cache_age_days} Tage alt ({recorded_date}).",
            })
        if confidence_score is not None and confidence_score < 0.5:
            anomalies.append({
                "type": "low_readiness_confidence",
                "severity": "warning",
                "title": "Niedrige Konfidenz der Erholungsdaten",
                "description": confidence_warning or "Readiness basiert auf unvollständigen Signalen (Ruhepuls oder HRV fehlt).",
            })
        if overtraining_warning:
            anomalies.append({
                "type": "overtraining_risk_synergy",
                "severity": "critical",
                "title": "Akutes Übertrainings- und Erschöpfungsrisiko",
                "description": overtraining_warning,
            })
        if ramp_warning and ramp_rate > 7.0:
            anomalies.append({
                "type": "excessive_ramp_rate",
                "severity": "warning",
                "title": "Kritische Belastungssteigerung (Ramp Rate)",
                "description": ramp_warning,
            })

        verdict = "critical" if any(a["severity"] == "critical" for a in anomalies) else (
            "warning" if any(a["severity"] == "warning" for a in anomalies) else "clean"
        )
        data_quality = {
            "verdict": verdict,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
        }

        # Letzte 14 Tage als kompakter Verlauf
        recent_records = timeline[-14:]
        history_summary = [
            {
                "date": r.date_str,
                "tss": r.tss,
                "ctl": r.ctl,
                "atl": r.atl,
                "tsb": r.tsb,
                "activities": len(r.activities),
            }
            for r in recent_records
        ]

        # Athletenprofil sauber strukturiert
        athlete_profile = {
            "rider_weight_kg": float(self.profile.get("rider_weight_kg", 60.0)),
            "bike_weight_kg": float(self.profile.get("bike_weight_kg", 9.5)),
            "system_weight_kg": float(self.profile.get("total_system_weight_kg", 72.0)),
            "hr_max": int(self.profile.get("hr_max", 205)),
            "hr_rest": int(self.profile.get("hr_rest", 59)),
            "athlete_type": self.profile.get("athlete_type", "Climber / Lightweight"),
            "goals": self.profile.get("goals", "Wettkampf / Leistungsoptimierung"),
        }

        # Sportwissenschaftliche Coaching-Konfiguration (rein datengestützt, frei von LLM-Verhaltensanweisungen)
        coaching_directives = {
            "coaching_style": "direct_analytical",
            "focus_areas": [
                "overtraining_prevention",
                "pacing_discipline",
                "cardiac_drift_monitoring",
                "ramp_rate_safety",
            ],
        }

        return {
            "status": "success",
            "current_date": latest.date_str,
            "metrics": {
                "ctl_fitness": current_ctl,
                "atl_fatigue": current_atl,
                "tsb_form": current_tsb,
                "ramp_rate_7d": ramp_rate,
                "weekly_tss_7d": last_7_days_tss,
            },
            "form_zone": form_zone_dict,
            "recovery_context": recovery_context,
            "data_quality_and_anomalies": data_quality,
            "history_last_14_days": history_summary,
            "athlete_profile": athlete_profile,
            "coaching_directives": coaching_directives,
        }


def plan_ctl_periodization(
    target_date_str: str,
    target_ctl: Optional[float] = None,
    event_type: str = "gran_fondo",
    current_ctl: Optional[float] = None,
    weekly_hours_budget: Optional[float] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """
    Erstellt einen sportwissenschaftlichen Periodisierungs- und Belastungsplan (CTL/ATL/TSB)
    bis zu einem bestimmten Zieldatum (Wettkampf / Marathon / Gran Fondo / Saisonhöhepunkt).
    Modelliert progressive Überlastung, 3:1 oder 2:1 Belastungs-/Entlastungszyklen
    sowie eine zielgerichtete Tapering-Phase für maximale Superkompensation (+15 bis +25 TSB am Renntag).
    """
    from datetime import date as dt_date, timedelta
    from rag_engine import ActivityVectorStore

    try:
        target_date = dt_date.fromisoformat(target_date_str[:10])
    except Exception:
        return {"status": "error", "message": f"Ungültiges Datumsformat '{target_date_str}'. Bitte YYYY-MM-DD verwenden."}

    today = dt_date.today()
    days_until_race = (target_date - today).days
    if days_until_race < 7:
        return {
            "status": "error",
            "message": f"Vorbereitungszeit zu kurz ({days_until_race} Tage). Für eine fundierte Periodisierung sind mindestens 2-4 Wochen erforderlich.",
        }

    total_weeks = max(1, days_until_race // 7)

    # Event-Defaults für Ziel-CTL
    event_ctl_map = {
        "marathon": 75.0,
        "gran_fondo": 85.0,
        "cycling_race": 90.0,
        "radrennen": 90.0,
        "triathlon": 80.0,
        "triathlon_olympic": 75.0,
        "ironman": 115.0,
        "general_fitness": 55.0,
    }
    ev_key = event_type.lower().strip().replace(" ", "_")
    recommended_ctl = event_ctl_map.get(ev_key, 80.0)
    effective_target_ctl = float(target_ctl) if target_ctl is not None else recommended_ctl

    # Aktuelle Belastung ermitteln
    if current_ctl is None:
        try:
            engine = load_historical_training_data(user_id=user_id)
            stat = engine.get_training_status()
            sim_ctl = float(stat.get("metrics", {}).get("ctl_fitness", 35.0))
            sim_atl = float(stat.get("metrics", {}).get("atl_fatigue", sim_ctl))
        except Exception:
            sim_ctl = 35.0
            sim_atl = 35.0
    else:
        sim_ctl = float(current_ctl)
        sim_atl = sim_ctl

    start_ctl = sim_ctl

    # Taper-Dauer nach Sportart
    taper_weeks = 2 if total_weeks >= 5 else 1
    if ev_key == "ironman" and total_weeks >= 8:
        taper_weeks = 3
    elif ev_key == "marathon" and total_weeks >= 6:
        taper_weeks = 2

    build_weeks = max(1, total_weeks - taper_weeks)

    # Ziel-Peak vor dem Taper: Während des Tapers fällt CTL leicht (~4-6%), TSB steigt massiv (+15..+25)
    peak_ctl_target = effective_target_ctl + 3.0
    needed_ctl_gain = max(0.0, peak_ctl_target - start_ctl)
    required_ramp_rate = round(needed_ctl_gain / build_weeks, 2)

    # Sicherheitsbewertung
    if required_ramp_rate <= 3.5:
        ramp_assessment = "Sicher & nachhaltig (optimaler physiologischer Anpassungsbereich)"
        risk_level = "low"
    elif required_ramp_rate <= 5.5:
        ramp_assessment = "Ambitioniert & progressiv (Erholungswochen strikt diszipliniert einhalten)"
        risk_level = "moderate"
    elif required_ramp_rate <= 7.0:
        ramp_assessment = "Grenzwertig fordernd (Erhöhtes Risiko für Sehnenüberlastung und Infekte)"
        risk_level = "high"
    else:
        ramp_assessment = (
            f"Kritisch überdimensioniert! (>7.0 CTL/Woche Zuwachs). "
            f"Ziel-CTL von {effective_target_ctl:.0f} in {total_weeks} Wochen ist ohne massives Übertrainingsrisiko kaum erreichbar."
        )
        risk_level = "critical"

    # Wöchentlichen Plan generieren
    weekly_plan = []
    cur_date = today

    for w in range(1, total_weeks + 1):
        week_start = cur_date
        week_end = cur_date + timedelta(days=6)
        is_taper = (w > build_weeks)
        is_recovery_week = (not is_taper and w % 3 == 0)

        # Phasenbezeichnung & Ziel-TSS
        if is_taper:
            taper_step = w - build_weeks
            phase = f"Tapering Woche {taper_step}"
            reduction = 0.35 if taper_step == 1 else 0.55
            target_weekly_tss = round(max(150.0, sim_ctl * 7.0 * (1.0 - reduction)), 0)
            focus = "Tapering & Superkompensation: Volumen drastisch reduzieren, kurze anaerobe Reize (Openers), Kohlenhydratspeicher laden"
        elif is_recovery_week:
            phase = "Regenerationswoche (Deload)"
            target_weekly_tss = round(max(180.0, sim_ctl * 7.0 * 0.65), 0)
            focus = "Physiologische Adaptation: -35% Trainingsvolumen, lockere Z1/Z2 Einheiten, Mobility & Schlaf priorisieren"
        else:
            if w <= max(1, build_weeks // 2):
                phase = "Grundlagenaufbau (Base)"
                focus = "GA1 Grundlagenausdauer, aerobe Kapazität, lange ruhige Umfänge, Trittfrequenz-Effizienz"
            else:
                phase = "Spezifischer Aufbau (Build)"
                focus = "Sweetspot-Intervalle, Schwellentraining (FTP Over-Unders), rennspezifische Kletterintervalle"

            daily_target = sim_ctl + (required_ramp_rate / 7.0 * 7.0)
            target_weekly_tss = round(max(200.0, daily_target * 7.0), 0)

        # Banister EWMA Simulation für die Woche (7 Tage)
        daily_tss = target_weekly_tss / 7.0
        for _ in range(7):
            sim_ctl = sim_ctl + (daily_tss - sim_ctl) / 42.0
            sim_atl = sim_atl + (daily_tss - sim_atl) / 7.0
        sim_tsb = sim_ctl - sim_atl

        est_hours = round(target_weekly_tss / 50.0, 1)

        weekly_plan.append({
            "week_number": w,
            "period": f"{week_start.strftime('%d.%m.')} - {week_end.strftime('%d.%m.%Y')}",
            "phase": phase,
            "is_recovery_week": is_recovery_week,
            "target_weekly_tss": int(target_weekly_tss),
            "estimated_hours": est_hours,
            "projected_ctl": round(sim_ctl, 1),
            "projected_atl": round(sim_atl, 1),
            "projected_tsb": round(sim_tsb, 1),
            "key_focus": focus,
        })
        cur_date = week_end + timedelta(days=1)

    race_day_tsb = round(sim_tsb, 1)

    return {
        "status": "success",
        "target_date": target_date_str,
        "event_type": event_type,
        "weeks_to_target": total_weeks,
        "starting_fitness": {
            "current_ctl": round(start_ctl, 1),
            "target_ctl": round(effective_target_ctl, 1),
            "peak_ctl_before_taper": round(peak_ctl_target, 1),
        },
        "periodization_overview": {
            "build_weeks": build_weeks,
            "taper_weeks": taper_weeks,
            "required_weekly_ramp_rate": required_ramp_rate,
            "ramp_rate_assessment": ramp_assessment,
            "injury_risk_level": risk_level,
            "projected_race_day_tsb_form": race_day_tsb,
            "peak_freshness_confirmed": race_day_tsb >= 10.0,
        },
        "periodization_plan": weekly_plan,
        "coaching_takeaway": (
            f"In {total_weeks} Wochen bis zum Renntag ({target_date_str}): "
            f"Ziel-CTL von {effective_target_ctl:.0f} mit Ramp-Rate {required_ramp_rate:+.1f} CTL/Woche erreichbar. "
            f"Am Wettkampftag wird eine Form (TSB) von {race_day_tsb:+.1f} prognostiziert "
            f"({'Perfekte Superkompensation & frische Beine!' if race_day_tsb >= 10.0 else 'Gute Form'})."
        ),
    }


def plan_periodization(
    target_date: str,
    target_event: Optional[str] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """
    Berechnet einen zukunftsorientierten, sportwissenschaftlichen Periodisierungsplan bis zum Zieldatum.
    Nutzt das hinterlegte Athletenprofil (insbesondere das Feld 'goals' mit Marathon/Ironman-Zielen)
    und den aktuellen CTL-Wert aus dem Trainingsstatus.

    Rückgabe:
    - Wöchentliche CTL-Zielwerte (progressiver Zielpfad).
    - Empfohlene Ramp Rate (sicher: max. 5-7 CTL/Woche, optimal 3-5 CTL/Woche).
    - Grobe Phaseneinteilung (Grundlage / Aufbau / Tapering).
    - Strukturierter Wochenplan mit TSS-Vorgaben und Fokus-Beschreibungen.
    """
    from rag_engine import ActivityVectorStore

    store = ActivityVectorStore()
    profile = store.get_profile(user_id=user_id)
    profile_goals = str(profile.get("goals", "")).strip()

    # Zielwettkampf / Event-Typ aus Profil ableiten, falls nicht explizit übergeben
    if target_event and target_event.strip():
        chosen_event = target_event.strip()
        event_source = f"Manuell angegeben ('{chosen_event}')"
    else:
        goals_lower = profile_goals.lower()
        if "ironman" in goals_lower:
            chosen_event = "ironman"
        elif "marathon" in goals_lower:
            chosen_event = "marathon"
        elif any(k in goals_lower for k in ["gran fondo", "radmarathon", "jedermann"]):
            chosen_event = "gran_fondo"
        elif any(k in goals_lower for k in ["kriterium", "radrennen", "u17", "nachwuchs"]):
            chosen_event = "cycling_race"
        elif "triathlon" in goals_lower:
            chosen_event = "triathlon"
        else:
            chosen_event = "marathon"  # Standard-Ausdauerziel
        event_source = f"Automatisch aus Athletenprofil abgeleitet ('{profile_goals}')"

    # Basisberechnung über plan_ctl_periodization
    res = plan_ctl_periodization(
        target_date_str=target_date,
        event_type=chosen_event,
        user_id=user_id,
    )

    if res.get("status") != "success":
        return res

    overview = res.get("periodization_overview", {})
    start_fit = res.get("starting_fitness", {})
    plan = res.get("periodization_plan", [])

    build_w = overview.get("build_weeks", 0)
    taper_w = overview.get("taper_weeks", 0)
    base_w = max(1, build_w // 2)
    spec_build_w = max(1, build_w - base_w)

    phases_summary = {
        "base_phase": {
            "name": "Grundlagenphase (Base)",
            "duration_weeks": base_w,
            "focus": "GA1 Grundlagenausdauer, aerobe Kapazität, lange ruhige Umfänge, Trittfrequenz-Effizienz.",
        },
        "build_phase": {
            "name": "Spezifischer Aufbau (Build)",
            "duration_weeks": spec_build_w,
            "focus": "Sweetspot-Intervalle, Schwellentraining (FTP Over-Unders), rennspezifische Kletterintervalle.",
        },
        "taper_phase": {
            "name": "Wettkampf-Tapering (Taper)",
            "duration_weeks": taper_w,
            "focus": "Volumen um 40-50% drosseln, kurze spritzige Reize erhalten, Superkompensation & Carboloading.",
        },
    }

    # Wöchentliche Zielwerte übersichtlich formatieren
    weekly_targets = [
        {
            "week_number": w["week_number"],
            "period": w.get("period", ""),
            "dates": w.get("period") or f"{w.get('start_date', '')} bis {w.get('end_date', '')}",
            "phase": w["phase"],
            "is_recovery_week": w["is_recovery_week"],
            "target_ctl": w.get("target_ctl") or w.get("projected_ctl"),
            "target_weekly_tss": w["target_weekly_tss"],
            "focus": w.get("focus") or w.get("key_focus"),
        }
        for w in plan
    ]

    ramp_rate = overview.get("required_weekly_ramp_rate", 0.0)
    ramp_rate_safety = (
        "optimal_sicher (<= 3.5 CTL/Woche)"
        if ramp_rate <= 3.5
        else (
            "ambitioniert_machbar (3.5 - 5.5 CTL/Woche)"
            if ramp_rate <= 5.5
            else (
                "grenzwertig_fordernd (5.5 - 7.0 CTL/Woche, Obergrenze!)"
                if ramp_rate <= 7.0
                else "kritisch_ueberlastungsgefahr (> 7.0 CTL/Woche)"
            )
        )
    )

    anomalies = []
    if ramp_rate > 7.0:
        anomalies.append({
            "type": "excessive_ramp_rate_required",
            "severity": "warning",
            "title": "Kritisch hohe Ramp Rate erforderlich",
            "description": f"Erforderlicher Anstieg von +{ramp_rate:.1f} CTL/Woche übersteigt das empfohlene Sicherheitslimit von max. 5-7 CTL/Woche.",
        })
    if res.get("weeks_to_target", 0) < 4:
        anomalies.append({
            "type": "short_periodization_window",
            "severity": "info",
            "title": "Sehr kurzes Vorbereitungsfenster",
            "description": f"Nur {res.get('weeks_to_target')} Wochen bis zum Zieldatum. Physiologische Adaptationen sind zeitlich limitiert.",
        })

    return {
        "status": "success",
        "target_date": target_date,
        "target_event": chosen_event,
        "event_source": event_source,
        "athlete_goal_profile": profile_goals,
        "current_ctl": start_fit.get("current_ctl"),
        "target_ctl": start_fit.get("target_ctl"),
        "peak_ctl_before_taper": start_fit.get("peak_ctl_before_taper"),
        "weeks_to_target": res.get("weeks_to_target"),
        "recommended_ramp_rate": ramp_rate,
        "ramp_rate_safety_guideline": "sicher: max. 5-7 CTL/Woche",
        "ramp_rate_safety": ramp_rate_safety,
        "phases_summary": phases_summary,
        "weekly_targets": weekly_targets,
        "data_quality_and_anomalies": {
            "verdict": "warning" if any(a["severity"] == "warning" for a in anomalies) else "clean",
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
        },
        "coaching_takeaway": res.get("coaching_takeaway"),
    }


def evaluate_training_alerts(
    user_id: str = "default",
    training_engine: Optional[TrainingLoadEngine] = None,
    health_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Prüft proaktiv die physiologische Belastung, Erholungsdefizite und Datenqualität.
    Persistiert erkannte Warnungen in der Datenbank.
    """
    from datetime import datetime as dt_datetime
    from rag_engine import ActivityVectorStore
    store = ActivityVectorStore()
    engine = training_engine or load_historical_training_data(user_id=user_id)
    status = engine.get_training_status()
    metrics = status.get("metrics", {})
    ctl = metrics.get("ctl_fitness", 0.0)
    atl = metrics.get("atl_fatigue", 0.0)
    tsb = metrics.get("tsb_form", 0.0)
    ramp = metrics.get("ramp_rate_7d", 0.0)

    alerts = []

    # 1. Ramp Rate Check
    if ramp > 6.0:
        sev = "critical" if ramp >= 8.0 else "warning"
        alert = store.save_training_alert(
            alert_key="ramp_rate_excessive",
            severity=sev,
            title="Kritische Ramp Rate (Steigerungsrate zu steil)",
            message=(
                f"Die 7-Tage-Ramp-Rate liegt bei {ramp:+.1f} CTL/Woche (Optimal: +3 bis +5). "
                "Zu schneller Belastungsanstieg erhöht das Risiko für Sehnenüberlastung und Infekte drastisch."
            ),
            category="overtraining",
            user_id=user_id,
        )
        alerts.append(alert)

    # 2. Akute Erschöpfung (TSB < -25)
    if tsb < -25.0:
        sev = "critical" if tsb <= -35.0 else "warning"
        alert = store.save_training_alert(
            alert_key="acute_fatigue_overload",
            severity=sev,
            title="Akute Erschöpfung / Hohes Belastungsdefizit",
            message=(
                f"Der aktuelle TSB-Wert (Form) ist stark negativ ({tsb:+.1f}). "
                "Der Körper befindet sich in tiefer Erschöpfung. Geplante harte Intervalle sollten verschoben werden."
            ),
            category="fatigue",
            user_id=user_id,
        )
        alerts.append(alert)

    # 3. Apple Health Vitaldaten-Prüfung
    latest_health = health_data or store.get_latest_apple_health_metrics(user_id=user_id)
    if latest_health and latest_health.get("recorded_date"):
        try:
            rec_d = dt_datetime.strptime(str(latest_health["recorded_date"])[:10], "%Y-%m-%d").date()
            diff_days = (dt_datetime.now().date() - rec_d).days
            if diff_days >= 3:
                alert = store.save_training_alert(
                    alert_key="apple_health_stale",
                    severity="info",
                    title="Apple Health Vitaldaten veraltet",
                    message=(
                        f"Die letzten synchronisierten Vitaldaten (Ruhepuls/HRV) sind {diff_days} Tage alt ({latest_health['recorded_date']}). "
                        "Health Auto Export Server auf dem iPhone ist offline."
                    ),
                    category="data_quality",
                    user_id=user_id,
                )
                alerts.append(alert)
        except Exception:
            pass

        # Readiness vs. Fatigue Check
        readiness = latest_health.get("readiness_score")
        if readiness is not None and readiness < 55.0 and atl > 40.0:
            alert = store.save_training_alert(
                alert_key="recovery_deficit",
                severity="critical",
                title="Erholungsdefizit: Schlechte Vitalwerte bei hoher Ermüdung",
                message=(
                    f"Readiness-Score von nur {readiness:.0f}/100 trifft auf akute Ermüdung (ATL {atl:.1f}). "
                    "Parasympathische Erholung unzureichend. Dringend Regenerationstag einlegen!"
                ),
                category="recovery",
                user_id=user_id,
            )
            alerts.append(alert)

    return alerts


def compute_holistic_recovery(user_id: str = "default") -> Dict[str, Any]:
    """
    Schließt den Recovery-Loop zwischen Trainingsbelastung (CTL/ATL/TSB)
    und Apple Health Vitaldaten (Ruhepuls, HRV, Schlaf).
    Korreliert chronische und akute Ermüdung mit autonomer Regeneration.
    """
    from datetime import datetime as dt_datetime
    from rag_engine import ActivityVectorStore
    from apple_health_service import sync_apple_health_from_ios

    store = ActivityVectorStore()
    engine = load_historical_training_data(user_id=user_id)
    training_status = engine.get_training_status()
    metrics = training_status.get("metrics", {})

    ctl = float(metrics.get("ctl_fitness", 0.0))
    atl = float(metrics.get("atl_fatigue", 0.0))
    tsb = float(metrics.get("tsb_form", 0.0))
    ramp = float(metrics.get("ramp_rate_7d", 0.0))
    form_zone = metrics.get("form_zone", "Neutral")

    # Apple Health Vitaldaten abrufen
    health_data = sync_apple_health_from_ios(force=False)
    readiness_score = float(health_data.get("readiness_score", 50.0))
    confidence = health_data.get("confidence", "high")
    confidence_warning = health_data.get("confidence_warning")
    is_stale = health_data.get("is_stale", False)
    cache_age_days = health_data.get("cache_age_days", 0)
    rhr = health_data.get("resting_heart_rate")
    hrv = health_data.get("heart_rate_variability_sdnn")
    sleep_h = health_data.get("sleep_duration_hours")

    # TSB Komponente normalisieren (-30..+20 auf 15..100)
    tsb_norm = min(100.0, max(15.0, ((tsb + 25.0) / 45.0) * 80.0 + 20.0))

    # Gewichtung nach Datenqualität
    if confidence == "high" and not is_stale:
        composite = 0.50 * tsb_norm + 0.50 * readiness_score
    elif confidence == "moderate" and not is_stale:
        composite = 0.65 * tsb_norm + 0.35 * readiness_score
    else:
        composite = 0.80 * tsb_norm + 0.20 * readiness_score

    # Abzug bei kritischer Ramp Rate
    if ramp > 6.0:
        composite -= 8.0

    holistic_score = int(round(min(100.0, max(0.0, composite)), 0))

    # Korrelationen & Synergien analysieren
    correlations = []
    risk_flags = []
    permitted_zones = []

    # Fall 1: Hohe Ermüdung + Unterdrückte HRV / niedrige Readiness
    if tsb < -15.0 and readiness_score < 60.0:
        correlations.append(
            f"Kritische Erschöpfungssynergie: Akute Belastung (TSB {tsb:+.1f}, ATL {atl:.1f}) trifft auf "
            f"unterdrückte parasympathische Erholung (Readiness {readiness_score:.0f}/100, HRV {hrv} ms). "
            "Das vegetative Nervensystem ist im Sympathikus-Stress."
        )
        risk_flags.append("sympathetic_overreach")
    elif tsb > 5.0 and readiness_score >= 75.0:
        correlations.append(
            f"Ideales Superkompensations-Fenster: Hohe Frische (TSB {tsb:+.1f}) harmonisiert mit "
            f"hervorragenden Vitalwerten (Readiness {readiness_score:.0f}/100, Schlaf {sleep_h}h). "
            "Körper und vegetatives Nervensystem sind optimal auf Spitzenleistungen vorbereitet."
        )
    elif tsb < -10.0 and readiness_score >= 75.0:
        correlations.append(
            f"Funktionale Belastungsadaptation: Hohe akute Trainingslast (TSB {tsb:+.1f}), aber die "
            f"Vitalwerte (Readiness {readiness_score:.0f}/100) signalisieren eine stabile, robuste Regeneration. "
            "Der Körper toleriert den Reiz aktuell gut."
        )
    elif tsb > 10.0 and readiness_score < 55.0:
        correlations.append(
            f"Diskrepanz: Physische Frische (TSB {tsb:+.1f}) durch geringe Umfänge, jedoch schlechte Vitalwerte "
            f"(Readiness {readiness_score:.0f}/100). Ursache liegt nicht im Training, sondern in Schlafmangel, "
            "Infekt oder Alltagsstress."
        )
        risk_flags.append("extrinsic_stress_or_illness")

    if is_stale:
        correlations.append(
            f"Datenqualitäts-Hinweis: Apple Health Daten sind {cache_age_days} Tage alt. "
            "Erholungsindex stützt sich primär auf das Banister/Coggan-Belastungsmodell."
        )
        risk_flags.append("stale_vital_data")

    # Einstufung & Zonen-Empfehlung
    if holistic_score >= 80:
        verdict = "Optimal für Spitzenleistung & harte Schlüsseleinheiten"
        training_recommendation = (
            "Grünes Licht für intensive Einheiten: VO2max-Intervalle, Schwellentraining (FTP Over-Unders) "
            "oder harte Wettkampfeinsätze. Der Körper ist voll aufnahmefähig."
        )
        permitted_zones = ["Z1 Rekom", "Z2 GA1", "Z3 GA2", "Z4 Schwelle/FTP", "Z5 VO2max", "Z6 Anaerob"]
    elif holistic_score >= 65:
        verdict = "Solide Erholung – Reguläres Ausdauertraining empfohlen"
        training_recommendation = (
            "Aerobes Grundlagentraining (GA1) und moderates Sweetspot-Training ohne Bedenken möglich. "
            "Erschöpfende Maximalbelastungen heute vermeiden."
        )
        permitted_zones = ["Z1 Rekom", "Z2 GA1", "Z3 Sweetspot (kontrolliert)"]
    elif holistic_score >= 45:
        verdict = "Moderates Erholungsdefizit – Belastung drosseln"
        training_recommendation = (
            "Nur lockeres Grundlagentraining Z1/Z2 (max. 60-90 Min flach). "
            "Keine harten Antritte, Intervalle oder steile Bergauf-Segmente."
        )
        permitted_zones = ["Z1 Rekom", "Z2 GA1 (kurz)"]
    else:
        verdict = "Kritisches Erholungsdefizit – Dringend Regenerationstag einlegen"
        training_recommendation = (
            "Akute Überlastungsgefahr: Heute zwingend vollständiger Ruhetag oder max. 30 Minuten lockeres "
            "Rekom-Kurbeln (<50% HRmax). Früher Schlaf und protein-/kohlenhydratreiche Ernährung priorisieren."
        )
        permitted_zones = ["Z1 Rekom (optional, max 30 Min)", "Ruhetag"]

    return {
        "status": "success",
        "user_id": user_id,
        "holistic_recovery_score": holistic_score,
        "verdict": verdict,
        "training_recommendation": training_recommendation,
        "permitted_training_zones": permitted_zones,
        "risk_flags": risk_flags,
        "cross_domain_correlations": correlations,
        "training_load_telemetry": {
            "ctl_fitness": ctl,
            "atl_fatigue": atl,
            "tsb_form": tsb,
            "ramp_rate_7d": ramp,
            "form_zone": form_zone,
        },
        "apple_health_telemetry": {
            "readiness_score": readiness_score,
            "resting_heart_rate": rhr,
            "heart_rate_variability_sdnn": hrv,
            "sleep_duration_hours": sleep_h,
            "confidence": confidence,
            "is_stale": is_stale,
            "cache_age_days": cache_age_days,
            "confidence_warning": confidence_warning,
        },
    }


def load_historical_training_data(user_id: str = "default") -> TrainingLoadEngine:
    """
    Lädt alle verfügbaren Trainingsdaten aus dem lokalen Vektor-Store
    (bzw. synchronisiert ./downloads/), initialisiert die TrainingLoadEngine
    und berechnet das Belastungsmodell unter Berücksichtigung des Athletenprofils.
    """
    from rag_engine import ActivityVectorStore, sync_local_gpx_to_vector_store

    store = ActivityVectorStore()
    if store.count() == 0:
        sync_local_gpx_to_vector_store()

    profile = store.get_profile(user_id=user_id)
    profile_hr_max = float(profile.get("hr_max", 205.0))

    engine = TrainingLoadEngine(profile=profile)
    rows = store.get_all_activities()

    for r in rows:
        try:
            d_str = r["activity_date"]
            # Datum parsen (YYYY-MM-DD)
            try:
                act_date = date.fromisoformat(d_str[:10])
            except Exception:
                continue

            dist_m = float(r["distance_km"] or 0.0) * 1000.0
            elev_m = float(r["elevation_m"] or 0.0)
            duration_s = float(r["moving_time_s"] or 0.0)
            suffer = float(r["suffer_score"]) if r["suffer_score"] is not None else None
            avg_hr = float(r["avg_hr"]) if r["avg_hr"] is not None else None
            max_hr = float(r["max_hr"]) if r["max_hr"] is not None else profile_hr_max

            engine.add_activity(
                activity_id=r["activity_id"],
                name=r["name"],
                sport_type=r["sport_type"],
                activity_date=act_date,
                duration_s=duration_s,
                distance_m=dist_m,
                elevation_gain_m=elev_m,
                suffer_score=suffer,
                avg_hr=avg_hr,
                max_hr=max_hr,
            )
        except Exception:
            continue

    return engine


def check_and_create_proactive_coaching_alerts(
    user_id: str = "default",
    training_engine: Optional[TrainingLoadEngine] = None,
    downloads_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Prüft nach jedem Synchronisationslauf proaktiv kritische Schwellenwerte:
    1. ramp_rate_7d > 7.0 (Verletzungsrisiko / zu steiler Belastungsanstieg)
    2. tsb_form < -25 für mehr als 3 Tage in Folge (Übertraining / tiefe Erschöpfung)
    3. Speed-Spike-Anomalien in mehr als 2 Aktivitäten derselben Woche (GPS-Sensorfehler)

    Legt bei Überschreitung automatisch eine Coaching-Notiz via store.add_coaching_note an
    (Kategorie 'overtraining_risk' bzw. 'data_quality'), damit das nächste LLM in einer
    neuen Session sofort sieht, dass Handlungsbedarf besteht — ohne dass der Nutzer explizit danach fragen muss.
    """
    from datetime import date as dt_date, datetime as dt_datetime, timedelta as dt_timedelta, timezone as dt_timezone
    from pathlib import Path
    from rag_engine import ActivityVectorStore
    from config import DOWNLOADS_DIR

    store = ActivityVectorStore()
    engine = training_engine or load_historical_training_data(user_id=user_id)
    status = engine.get_training_status(user_id=user_id)
    metrics = status.get("metrics", {})
    ramp = float(metrics.get("ramp_rate_7d", 0.0))
    tsb = float(metrics.get("tsb_form", 0.0))

    triggered_alerts = []
    created_notes = []

    def _has_recent_note(cat: str, kw: str) -> bool:
        try:
            notes = store.get_coaching_notes(limit=15, category=cat, user_id=user_id)
            now = dt_datetime.now(dt_timezone.utc)
            for n in notes:
                if kw.lower() in str(n.get("note", "")).lower():
                    try:
                        ts = dt_datetime.fromisoformat(n["timestamp"])
                        if (now - ts).total_seconds() < 24.0 * 3600.0:
                            return True
                    except Exception:
                        return True
        except Exception:
            pass
        return False

    # 1. Schwellenwert: ramp_rate_7d > 7.0 (Verletzungsrisiko)
    if ramp > 7.0:
        alert_msg = (
            f"⚠️ PROAKTIVER ALERT [Verletzungsrisiko]: 7-Tage-Ramp-Rate liegt bei +{ramp:+.1f} CTL/Woche "
            "(kritischer Schwellenwert > 7.0 überschritten). Zu schneller Belastungsanstieg überfordert Sehnen, "
            "Bänder und Immunsystem. Belastungsumfang und Intensität für die nächsten 5-7 Tage drosseln!"
        )
        store.save_training_alert(
            alert_key="ramp_rate_excessive",
            severity="warning",
            title="Kritische 7-Tage-Ramp-Rate (>7.0 CTL/Woche)",
            message=alert_msg,
            category="overtraining_risk",
            user_id=user_id,
        )
        triggered_alerts.append({
            "threshold": "ramp_rate_7d > 7",
            "value": ramp,
            "category": "overtraining_risk",
            "message": alert_msg,
        })
        if not _has_recent_note("overtraining_risk", "Ramp-Rate"):
            note = store.add_coaching_note(note=alert_msg, category="overtraining_risk", user_id=user_id)
            created_notes.append(note)

    # 2. Schwellenwert: tsb_form < -25 für mehr als 3 Tage in Folge (Übertraining)
    consecutive_low_tsb_days = 0
    timeline = engine.compute_daily_timeline()
    if timeline:
        for r in reversed(timeline):
            if r.tsb < -25.0:
                consecutive_low_tsb_days += 1
            else:
                break

    if consecutive_low_tsb_days >= 3:
        alert_msg = (
            f"🚨 PROAKTIVER ALERT [Übertraining]: TSB-Formwert liegt seit {consecutive_low_tsb_days} Tagen "
            f"in Folge unter -25.0 (aktueller TSB: {tsb:+.1f}). Tiefe systemische Erschöpfung – "
            "dringend 1-2 Ruhetage oder ausschließlich Rekom (Z1) einlegen!"
        )
        store.save_training_alert(
            alert_key="consecutive_fatigue_overload",
            severity="critical",
            title=f"Chronische Überlastung ({consecutive_low_tsb_days} Tage TSB < -25)",
            message=alert_msg,
            category="overtraining_risk",
            user_id=user_id,
        )
        triggered_alerts.append({
            "threshold": "tsb_form < -25 für > 3 Tage",
            "consecutive_days": consecutive_low_tsb_days,
            "current_tsb": tsb,
            "category": "overtraining_risk",
            "message": alert_msg,
        })
        if not _has_recent_note("overtraining_risk", "TSB-Formwert"):
            note = store.add_coaching_note(note=alert_msg, category="overtraining_risk", user_id=user_id)
            created_notes.append(note)

    # 3. Schwellenwert: Speed-Spike-Anomalien in mehr als 2 Aktivitäten derselben Woche
    target_dir = Path(downloads_dir) if downloads_dir else DOWNLOADS_DIR
    today_d = dt_date.today()
    week_ago = today_d - dt_timedelta(days=7)
    all_acts = store.get_all_activities()
    spike_acts = []

    def _find_local_activity_file(d_path: Path, a_id: str) -> Optional[Path]:
        clean_id = str(a_id).strip()
        if not d_path.exists():
            return None
        for f in d_path.glob(f"{clean_id}*.*"):
            if f.suffix.lower() in (".gpx", ".tcx", ".json"):
                return f
        return None

    from analyze_gpx import parse_gpx_file, calculate_metrics

    for act in all_acts:
        try:
            a_d_str = str(act.get("activity_date", ""))[:10]
            if a_d_str:
                a_date = dt_date.fromisoformat(a_d_str)
                if a_date >= week_ago:
                    a_id = str(act.get("activity_id", "")).strip()
                    loc_f = find_local_activity_file(target_dir, a_id)
                    if loc_f and loc_f.exists():
                        t, sp, pts = parse_gpx_file(loc_f)
                        if pts:
                            m = calculate_metrics(loc_f, t, sp, pts)
                            if m.speed_filtered or any(
                                a.get("type") in ("gps_speed_spike", "gps_speed_spike_filtered")
                                for a in m.anomalies
                            ):
                                spike_acts.append({
                                    "activity_id": a_id,
                                    "name": act.get("name"),
                                    "date": a_d_str,
                                    "raw_max_kmh": m.raw_max_speed_kmh or m.max_speed_kmh,
                                })
        except Exception:
            continue

    if len(spike_acts) > 2:
        alert_msg = (
            f"⚠️ PROAKTIVER ALERT [Datenqualität]: In {len(spike_acts)} Aktivitäten dieser Woche wurden "
            "unplausible GPS-Geschwindigkeits-Spikes erkannt und herausgefiltert. Mögliche Ursache: Sensor-Hardware-Glitch, "
            "schlechter Satellitenempfang am Start oder lockere Gerätehalterung. GPS-Sensorik überprüfen!"
        )
        store.save_training_alert(
            alert_key="repeated_speed_spikes",
            severity="warning",
            title=f"Wiederholte GPS-Speed-Spikes ({len(spike_acts)} Einheiten)",
            message=alert_msg,
            category="data_quality",
            user_id=user_id,
        )
        triggered_alerts.append({
            "threshold": "Speed-Spikes in > 2 Aktivitäten der Woche",
            "count": len(spike_acts),
            "category": "data_quality",
            "activities": spike_acts,
            "message": alert_msg,
        })
        if not _has_recent_note("data_quality", "Geschwindigkeits-Spikes"):
            note = store.add_coaching_note(note=alert_msg, category="data_quality", user_id=user_id)
            created_notes.append(note)

    return {
        "status": "success",
        "user_id": user_id,
        "triggered_alerts_count": len(triggered_alerts),
        "triggered_alerts": triggered_alerts,
        "created_coaching_notes_count": len(created_notes),
        "created_coaching_notes": created_notes,
    }
