"""
council_review.py: Multi-Agent Triangulation & Council Review Engine.

Kombiniert 3 heterogene Spitzen-KI-Modelle über OpenRouter zu einem
kompromisslosen, halluzinationsfreien Trainer-Rat für Nachwuchsathleten:
1. deepseek/deepseek-v4.1-flash: Der mathematisch-physikalische Watt- & Pacing-Kritiker.
2. z-ai/glm-5.3-flash: Der BDR U17 Regenerations- & Gesundheits-Auditor (HRV, Schlaf, Überlastung).
3. openai/gpt-6-luna: Der Chef-Stratege (Synthese, Konsensbildung, Schmeichel-Filter & Trainingsdirektive).

Arbeitet parallel/asynchron für minimale Latenz und Cent-Bruchteile an Kosten.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional

from config import OPENROUTER_API_KEY

logger = logging.getLogger("strava_mcp.council")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Modell-IDs auf OpenRouter
MODEL_DEEPSEEK = "deepseek/deepseek-v4.1-flash"
MODEL_GLM = "z-ai/glm-5.3-flash"
MODEL_GPT6_LUNA = "openai/gpt-6-luna"


def call_openrouter_single(
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 500,
    temperature: float = 0.2,
    timeout: float = 25.0,
) -> Dict[str, Any]:
    """
    Führt einen synchronen Aufruf an die OpenRouter API durch.
    Gibt den extrahierten Text sowie Token-Nutzung zurück.
    """
    if not OPENROUTER_API_KEY:
        return {
            "status": "error",
            "model": model,
            "message": "OPENROUTER_API_KEY nicht in .env oder Umgebungsvariablen konfiguriert.",
            "content": "",
        }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    req = urllib.request.Request(
        OPENROUTER_API_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://strava-gpx-sync.local",
            "X-Title": "Strava GPX Sync Coach Council",
            "User-Agent": "StravaGPXSync/2.0",
        },
        data=json.dumps(payload).encode("utf-8"),
    )

    t_start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = round(time.time() - t_start, 2)
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content") or ""

            # Falls content leer ist, aber reasoning vorhanden, nur als Notfall-Fallback
            if not content and "reasoning" in msg and len(msg.get("reasoning", "")) > 10:
                raw_reasoning = msg.get("reasoning", "")
                # Wenn im reasoning ein Schlussabschnitt existiert
                content = raw_reasoning.strip()

            usage = data.get("usage", {})
            return {
                "status": "success",
                "model": model,
                "content": content.strip(),
                "latency_seconds": elapsed,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
    except Exception as e:

        elapsed = round(time.time() - t_start, 2)
        logger.warning("OpenRouter API Fehler für Modell %s: %s", model, e)
        return {
            "status": "error",
            "model": model,
            "message": str(e),
            "content": "",
            "latency_seconds": elapsed,
        }


def run_council_review(
    activity_metrics: Optional[Dict[str, Any]] = None,
    training_load: Optional[Dict[str, Any]] = None,
    health_metrics: Optional[Dict[str, Any]] = None,
    athlete_profile: Optional[Dict[str, Any]] = None,
    user_context_or_question: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """
    Führt das vollständige 3-Stufen Triangulations-Review durch:
    Schritt 1 (Parallel):
      - DeepSeek V4.1-Flash analysiert Watt, Physik, Pacing, VAM und Decoupling.
      - GLM-5.3-Flash analysiert HRV, Schlaf, Vitalwerte, Ramp Rate und U17-Gesundheit.
    Schritt 2 (Synthese):
      - GPT-6 Luna gleicht beide Berichte ab, deckt Widersprüche auf, eliminiert Schmeicheleien
        und formuliert die finale Trainingsdirektive.
    """
    if not OPENROUTER_API_KEY:
        return {
            "status": "error",
            "message": "OPENROUTER_API_KEY ist nicht hinterlegt. Bitte in .env eintragen.",
        }

    # 1. Datenpaket für die Prüfer zusammenstellen
    profile = athlete_profile or {
        "age": 15,
        "category": "U17",
        "rider_weight_kg": 58.0,
        "bike_weight_kg": 9.5,
        "hr_max": 205,
        "hr_rest": 52,
    }

    data_summary = {
        "athlete": profile,
        "recent_activity": activity_metrics or {},
        "training_load_ctl_atl": training_load or {},
        "apple_health_vitals": health_metrics or {},
        "athlete_question_or_notes": user_context_or_question or "Allgemeine Status- und Trainingsanalyse anfordern.",
    }
    raw_json_str = json.dumps(data_summary, indent=2, ensure_ascii=False)

    # 2. Prompts definieren
    sys_prompt_deepseek = (
        "Du bist der mathematisch-physikalische Datenprüfer im Trainer-Rat für einen 15-jährigen "
        "Nachwuchsradsportler (U17, ca. 55-60 kg, Trek Checkpoint Gravelbike).\n"
        "Deine Pflicht: Überprüfe die vorgelegten Trainings- und Leistungsdaten absolut unbarmherzig.\n"
        "Prüfe: Wattwerte, W/kg, VAM (Steigrate m/h), TSS, Pacing, Verpflegung (Kohlenhydrate nach Jeukendrup) "
        "und Aerobic Decoupling (kardiale Drift).\n"
        "REGEL: Verbiete jedes Schmeicheln oder Schönreden! Benenne exakte Datenpunkte, Pacing-Fehler, "
        "Überzocken an Anstiegen oder suboptimale Zonenverteilung.\n"
        "Antworte strukturiert in max. 3-4 prägnanten, faktenbasierten Aufzählungspunkten (kein Vorwort)."
    )

    sys_prompt_glm = (
        "Du bist der Sportarzt und Regenerations-Auditor im Trainer-Rat nach BDR U17-Richtlinien "
        "für einen 15-jährigen Nachwuchsathleten.\n"
        "Deine Pflicht: Überprüfe Belastung, Ermüdung, Schlafqualität (Tiefschlaf/REM), HRV-Trends "
        "und Ruhepuls auf physiologische Verträglichkeit.\n"
        "Achte streng auf: Übertrainingsgefahr, zu hohe wöchentliche TSS Ramp Rate (>5-8 TSS/Woche), "
        "Schlafmangel (<8h für Jugendliche) und Überlastungssignale.\n"
        "REGEL: Stoppe den Athleten konsequent mit einem Restday oder Rekom, wenn Vitalwerte warnen!\n"
        "Antworte strukturiert in max. 3-4 präzisen Aufzählungspunkten (kein Vorwort)."
    )

    # 3. Parallele Ausführung von DeepSeek und GLM-5.3
    t_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_deepseek = executor.submit(
            call_openrouter_single,
            model=MODEL_DEEPSEEK,
            system_prompt=sys_prompt_deepseek,
            user_prompt=f"Hier sind die Rohdaten des Athleten:\n```json\n{raw_json_str}\n```",
            max_tokens=2500,
            temperature=0.1,
            timeout=timeout,
        )
        f_glm = executor.submit(
            call_openrouter_single,
            model=MODEL_GLM,
            system_prompt=sys_prompt_glm,
            user_prompt=f"Hier sind die Rohdaten des Athleten:\n```json\n{raw_json_str}\n```",
            max_tokens=2500,
            temperature=0.1,
            timeout=timeout,
        )

        res_deepseek = f_deepseek.result()
        res_glm = f_glm.result()

    # 4. Schritt 2: Synthese und Urteil durch OpenAI GPT-6 Luna (Der Chef-Coach & Mentor)
    sys_prompt_luna = (
        "Du bist der Leitende Chef-Coach und Mentor für einen 15-jährigen Nachwuchsathleten (U17).\n"
        "Du erhältst die Rohdaten des Athleten sowie die ungeschminkten, harten Prüfberichte der beiden Spezial-KIs:\n"
        "1. DeepSeek V4.1-Flash (knallharte physikalisch-mathematische Datenanalyse)\n"
        "2. GLM-5.3-Flash (BDR U17 Sportarzt & strenger Regenerations-Wächter)\n\n"
        "Deine Rolle als Chef-Coach (Gesunder Menschenverstand & Führung):\n"
        "Die beiden Prüfer im Hintergrund neigen dazu, alles 'totzukritisieren'. Deine Aufgabe ist es, "
        "ihre harten Fakten zu nehmen, sie aber wie ein weltklasse, menschlicher Erfolgstrainer zu vermitteln:\n"
        "1. VERDIENTE ANERKENNUNG & MOTIVATION: Würdige echte Meilensteine, Härte und Willenskraft ehrlich. "
        "Wenn ein 15-Jähriger 106 km auf Schotter durchzieht oder am Ende noch attackiert, ist das eine außergewöhnliche "
        "Leistung, die Respekt verdient – rede das nicht klein!\n"
        "2. KONSTRUKTIVE LEKTIONEN STATT VERMIESUNG: Erkläre die Kritikpunkte (z.B. Pausen, Pacing, Verpflegung) "
        "nicht als 'Fehler', sondern als die goldenen Schlüssel, mit denen er in Zukunft noch viel stärker wird.\n"
        "3. FÜRSORGLICHER SCHUTZ: Erkläre verständlich, warum jetzt Erholung und Schlaf nötig sind – "
        "damit der Körper diesen gewaltigen Reiz in echte Muskel- und Ausdauer-Superkompensation umwandeln kann.\n"
        "4. KLARE TRAININGSANWEISUNG: Konkrete Vorgabe für die nächsten 24-48 Stunden.\n\n"
        "Stil: Respektvoll, motivierend, sportwissenschaftlich 100% präzise, auf Augenhöhe, ohne künstliches Rumgesülze, "
        "aber mit echtem Trainer-Stolz auf das Nachwuchstalent. Ca. 180-250 Wörter."
    )


    synthesis_input = (
        f"=== ROHDATEN ===\n{raw_json_str}\n\n"
        f"=== BERICHT 1: DEEPSEEK (WATT & PHYSIK) ===\n{res_deepseek.get('content', 'Keine Daten')}\n\n"
        f"=== BERICHT 2: GLM-5.3 (REGENERATION & GESUNDHEIT) ===\n{res_glm.get('content', 'Keine Daten')}\n\n"
        f"Formuliere jetzt das finale Urteil des Trainer-Rats."
    )

    res_luna = call_openrouter_single(
        model=MODEL_GPT6_LUNA,
        system_prompt=sys_prompt_luna,
        user_prompt=synthesis_input,
        max_tokens=1500,
        temperature=0.2,
        timeout=timeout,
    )



    total_time = round(time.time() - t_start, 2)

    # Token-Kosten grob kalkulieren (in USD)
    p_tokens = (
        res_deepseek.get("prompt_tokens", 0)
        + res_glm.get("prompt_tokens", 0)
        + res_luna.get("prompt_tokens", 0)
    )
    c_tokens = (
        res_deepseek.get("completion_tokens", 0)
        + res_glm.get("completion_tokens", 0)
        + res_luna.get("completion_tokens", 0)
    )
    # Durchschnittlich ca. 0.08 $ / 1M Input, 0.40 $ / 1M Output
    cost_est_usd = round((p_tokens * 0.00000008) + (c_tokens * 0.0000004), 6)

    return {
        "status": "success",
        "council_ensemble": {
            "physics_critic": {
                "model": MODEL_DEEPSEEK,
                "role": "Mathematisch-Physikalischer Datenprüfer",
                "verdict": res_deepseek.get("content", ""),
                "latency_s": res_deepseek.get("latency_seconds", 0),
            },
            "recovery_auditor": {
                "model": MODEL_GLM,
                "role": "BDR U17 Regenerations- & Gesundheits-Auditor",
                "verdict": res_glm.get("content", ""),
                "latency_s": res_glm.get("latency_seconds", 0),
            },
            "head_coach_synthesis": {
                "model": MODEL_GPT6_LUNA,
                "role": "Leitender Chef-Coach & Widerspruchs-Richter",
                "final_directive": res_luna.get("content", ""),
                "latency_s": res_luna.get("latency_seconds", 0),
            },
        },
        "performance_and_costs": {
            "total_council_latency_seconds": total_time,
            "total_tokens_used": p_tokens + c_tokens,
            "estimated_cost_usd": cost_est_usd,
            "cost_in_euro_cent": round(cost_est_usd * 100.0, 4),
        },
    }
