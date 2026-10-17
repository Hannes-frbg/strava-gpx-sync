"""
rag_engine.py: Lokales Vektor-RAG & Semantische Suche für Strava-Aktivitäten.

Ermöglicht semantische Ähnlichkeitsabfragen über historische Aktivitäten,
Segmente, Steigungsprofile und Belastungsmetriken ohne Cloud-Abhängigkeit.
- Vektordatenbank: SQLite (strava_rag.db) mit Float32-BLOB-Speicherung.
- Embedding-Modell: Lokales, ressourcenschonendes ONNX-Modell via FastEmbed (384-dim).
- Zero-Cloud: Läuft 100% lokal auf dem Host-System mit Sub-Millisekunden-Suchzeit.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import struct
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import numpy as np

from config import DOWNLOADS_DIR

logger = logging.getLogger("strava_mcp.rag")

DB_PATH = Path(__file__).parent / "strava_rag.db"


class LocalEmbedder:
    """
    Ressourcenschonender lokaler Embedder mit FastEmbed (ONNX-basiert).
    Wird erst bei der ersten Nutzung lazy initialisiert.
    """

    _instance: Optional[LocalEmbedder] = None
    _model: Any = None

    @classmethod
    def get_instance(cls) -> LocalEmbedder:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_model(self) -> Any:
        if self._model is None:
            logger.info("Initialisiere FastEmbed TextEmbedding (lokales ONNX-Modell)...")
            try:
                from fastembed import TextEmbedding

                # BAAI/bge-small-en-v1.5 ist extrem kompakt (~33MB) und schnell auf CPU
                self._model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            except Exception as e:
                logger.error("Fehler beim Laden von FastEmbed: %s", e)
                raise
        return self._model

    def embed_text(self, text: str) -> np.ndarray:
        """Erzeugt einen 384-dimensionalen normalisierten Embedding-Vektor."""
        model = self._ensure_model()
        # FastEmbed liefert einen Generator von np.ndarray
        embeddings = list(model.embed([text]))
        vec = embeddings[0].astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Erzeugt Embeddings für mehrere Texte in einem Batch."""
        if not texts:
            return []
        model = self._ensure_model()
        raw_embs = list(model.embed(texts))
        result = []
        for raw in raw_embs:
            vec = raw.astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            result.append(vec)
        return result


def _vec_to_blob(vec: np.ndarray) -> bytes:
    """Konvertiert einen numpy float32 Vektor in binäre BLOB-Bytes."""
    return vec.astype(np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    """Konvertiert binäre BLOB-Bytes zurück in einen numpy float32 Vektor."""
    return np.frombuffer(blob, dtype=np.float32)


class ActivityVectorStore:
    """
    SQLite-basierter Vektor- und Dokumentenspeicher für Strava-Aktivitäten.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Erstellt das Datenbankschema falls nicht vorhanden."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    activity_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sport_type TEXT NOT NULL,
                    activity_date TEXT NOT NULL,
                    distance_km REAL,
                    elevation_m REAL,
                    moving_time_s INTEGER,
                    suffer_score REAL,
                    avg_hr REAL,
                    max_hr REAL,
                    segments_json TEXT,
                    document_text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_date ON activities (activity_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities (sport_type)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_profile (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coaching_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    note TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_cache (
                    cache_key TEXT PRIMARY KEY,
                    weather_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geo_cache (
                    cache_key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS apple_health_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_date TEXT UNIQUE,
                    sleep_hours REAL,
                    deep_sleep_minutes REAL,
                    rem_sleep_minutes REAL,
                    resting_hr INTEGER,
                    hrv_ms REAL,
                    readiness_score REAL,
                    readiness_verdict TEXT,
                    raw_json TEXT,
                    synced_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS training_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_key TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    user_id TEXT DEFAULT 'default'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS post_workout_checkins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    rpe_score INTEGER,
                    legs_feeling INTEGER,
                    mental_state TEXT,
                    fueling_adherence TEXT,
                    weather_felt TEXT,
                    pain_or_discomfort TEXT,
                    athlete_notes TEXT,
                    session_rpe_load REAL,
                    user_id TEXT DEFAULT 'default'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS apple_health_workouts (
                    id TEXT PRIMARY KEY,
                    activity_type TEXT,
                    duration_minutes REAL,
                    distance_meters REAL,
                    energy_kcal REAL,
                    start_date TEXT,
                    end_date TEXT,
                    source TEXT,
                    raw_json TEXT,
                    synced_at TEXT NOT NULL,
                    user_id TEXT DEFAULT 'default'
                )
                """
            )
            # Multi-User Migration: Sicherstellen, dass user_id in relevanten Tabellen existiert
            for table in ("athlete_profile", "coaching_notes", "apple_health_metrics", "activities", "training_alerts", "post_workout_checkins", "apple_health_workouts"):
                try:
                    cur = conn.execute(f"PRAGMA table_info({table})")
                    columns = [row["name"] for row in cur.fetchall()]
                    if "user_id" not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT DEFAULT 'default'")
                except Exception as mig_err:
                    logger.debug("Migrationshinweis für Tabelle '%s': %s", table, mig_err)

            # Apple Health Metriken Spaltenerweiterung (Steps, Kcal, Distanz, VO2max, Gewicht)
            try:
                cur = conn.execute("PRAGMA table_info(apple_health_metrics)")
                columns = [row["name"] for row in cur.fetchall()]
                for col, col_type in (
                    ("steps", "INTEGER"),
                    ("active_energy_kcal", "REAL"),
                    ("distance_meters", "REAL"),
                    ("vo2_max", "REAL"),
                    ("weight_kg", "REAL"),
                ):
                    if col not in columns:
                        conn.execute(f"ALTER TABLE apple_health_metrics ADD COLUMN {col} {col_type}")
            except Exception as mig_err:
                logger.debug("Migrationshinweis für apple_health_metrics Spalten: %s", mig_err)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_coaching_notes_user ON coaching_notes (user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_athlete_profile_user ON athlete_profile (user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_user ON activities (user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_training_alerts_user ON training_alerts (user_id, is_active)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_checkins_act ON post_workout_checkins (activity_id, user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_health_metrics_user_date ON apple_health_metrics (user_id, recorded_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_health_workouts_user_date ON apple_health_workouts (user_id, start_date)")
            conn.commit()

    @staticmethod
    def build_document_text(
        name: str,
        activity_id: str,
        sport_type: str,
        activity_date: str,
        distance_km: float,
        elevation_m: float,
        moving_time_s: int,
        suffer_score: Optional[float] = None,
        avg_hr: Optional[float] = None,
        max_hr: Optional[float] = None,
        segments: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Synthetisiert eine semantisch reichhaltige Beschreibung einer Aktivität
        für das Embedding-Modell.
        """
        hours = moving_time_s // 3600
        mins = (moving_time_s % 3600) // 60
        secs = moving_time_s % 60
        time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m {secs}s"

        parts = [
            f"Aktivität: {name}.",
            f"ID: {activity_id}.",
            f"Sportart: {sport_type}.",
            f"Datum: {activity_date}.",
            f"Distanz: {distance_km:.1f} km.",
            f"Höhenmeter: {elevation_m:.0f} Meter Anstieg.",
            f"Bewegungszeit: {time_str}.",
        ]

        if distance_km > 0 and moving_time_s > 0:
            avg_speed = (distance_km / (moving_time_s / 3600.0))
            parts.append(f"Durchschnittsgeschwindigkeit: {avg_speed:.1f} km/h.")

        if suffer_score is not None and suffer_score > 0:
            intensity = (
                "extrem hart" if suffer_score > 150
                else "hart / anspruchsvoll" if suffer_score > 90
                else "moderat" if suffer_score > 45
                else "locker / regenerativ"
            )
            parts.append(f"Belastung / Suffer Score: {suffer_score:.0f} ({intensity}).")

        if avg_hr:
            parts.append(f"Durchschnittspuls: {avg_hr:.0f} bpm.")
        if max_hr:
            parts.append(f"Maximalpuls: {max_hr:.0f} bpm.")

        # Topografische Charakteristik
        if distance_km > 0:
            vert_rate = (elevation_m / distance_km) * 10.0  # Höhenmeter pro 10km
            if vert_rate > 200:
                parts.append("Profil: Sehr steile Bergetappe / Klettertour mit vielen Höhenmetern.")
            elif vert_rate > 100:
                parts.append("Profil: Hügeliges Streckenprofil mit spürbaren Anstiegen.")
            elif vert_rate < 30:
                parts.append("Profil: Flachlandtour / flache Ausdauerfahrt.")

        # Segmente einbetten
        if segments:
            seg_descriptions = []
            for s in segments[:10]:  # Top 10 Segmente
                s_name = s.get("name", "Segment")
                s_dist = s.get("distance", "")
                s_elev = s.get("elevation_difference", "")
                s_grad = s.get("average_grade", "")
                s_pr = " (Persönlicher Rekord / PR)" if s.get("is_pr") else ""
                s_power = f", {s.get('average_watts')} Watt" if s.get("average_watts") else ""

                desc = f"{s_name} ({s_dist}, {s_grad} Steigung{s_power}{s_pr})"
                seg_descriptions.append(desc)

            if seg_descriptions:
                parts.append("Befahrene Segmente und Anstiege: " + "; ".join(seg_descriptions) + ".")

        return " ".join(parts)

    def upsert_activity(
        self,
        activity_id: str,
        name: str,
        sport_type: str,
        activity_date: str,
        distance_km: float,
        elevation_m: float,
        moving_time_s: int,
        suffer_score: Optional[float] = None,
        avg_hr: Optional[float] = None,
        max_hr: Optional[float] = None,
        segments: Optional[List[Dict[str, Any]]] = None,
        document_text: Optional[str] = None,
        embedding: Optional[np.ndarray] = None,
        user_id: str = "default",
    ) -> None:
        """Fügt eine Aktivität in die Datenbank ein oder aktualisiert sie."""
        clean_id = str(activity_id).strip()

        if document_text is None:
            document_text = self.build_document_text(
                name=name,
                activity_id=clean_id,
                sport_type=sport_type,
                activity_date=activity_date,
                distance_km=distance_km,
                elevation_m=elevation_m,
                moving_time_s=moving_time_s,
                suffer_score=suffer_score,
                avg_hr=avg_hr,
                max_hr=max_hr,
                segments=segments,
            )

        if embedding is None:
            embedder = LocalEmbedder.get_instance()
            embedding = embedder.embed_text(document_text)

        blob = _vec_to_blob(embedding)
        segments_json = json.dumps(segments or [], ensure_ascii=False)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO activities (
                    activity_id, name, sport_type, activity_date,
                    distance_km, elevation_m, moving_time_s, suffer_score,
                    avg_hr, max_hr, segments_json, document_text,
                    embedding, updated_at, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    name=excluded.name,
                    sport_type=excluded.sport_type,
                    activity_date=excluded.activity_date,
                    distance_km=excluded.distance_km,
                    elevation_m=excluded.elevation_m,
                    moving_time_s=excluded.moving_time_s,
                    suffer_score=excluded.suffer_score,
                    avg_hr=excluded.avg_hr,
                    max_hr=excluded.max_hr,
                    segments_json=excluded.segments_json,
                    document_text=excluded.document_text,
                    embedding=excluded.embedding,
                    updated_at=excluded.updated_at,
                    user_id=excluded.user_id
                """,
                (
                    clean_id,
                    name,
                    sport_type,
                    activity_date,
                    distance_km,
                    elevation_m,
                    moving_time_s,
                    suffer_score,
                    avg_hr,
                    max_hr,
                    segments_json,
                    document_text,
                    blob,
                    now_iso,
                    user_id,
                ),
            )
            conn.commit()

    def get_all_activities(self) -> List[Dict[str, Any]]:
        """Liefert alle gespeicherten Aktivitäten als Dictionaries."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM activities ORDER BY activity_date DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def get_activity(self, activity_id: str) -> Optional[Dict[str, Any]]:
        """Sucht eine Aktivität anhand der ID."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM activities WHERE activity_id = ?",
                (str(activity_id).strip(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def count(self) -> int:
        """Gibt die Gesamtanzahl der indexierten Aktivitäten zurück."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM activities")
            return int(cur.fetchone()[0])

    def search_semantic(
        self,
        query: str,
        limit: int = 5,
        sport_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Führt eine semantische Vektorsuche über alle indexierten Aktivitäten durch.
        Berechnet die Kosinus-Ähnlichkeit im Vektorraum.
        """
        embedder = LocalEmbedder.get_instance()
        query_vec = embedder.embed_text(query)

        rows = self.get_all_activities()
        if not rows:
            return []

        # Optional nach Sportart vorfiltern
        filtered_rows = []
        for r in rows:
            if sport_filter:
                sf = sport_filter.lower()
                st = (r["sport_type"] or "").lower()
                if sf not in st:
                    continue
            filtered_rows.append(r)

        if not filtered_rows:
            return []

        # Matrix aller Vektoren aufbauen
        vectors = np.array([_blob_to_vec(r["embedding"]) for r in filtered_rows])
        # Kosinus-Ähnlichkeit: Da alle Vektoren normalisiert sind, entspricht Cos-Sim dem Skalarprodukt
        sims = np.dot(vectors, query_vec)

        # Top-K Indizes bestimmen
        top_indices = np.argsort(-sims)[:limit]

        results = []
        for idx in top_indices:
            score = float(sims[idx])
            row = filtered_rows[idx]
            segments = []
            if row["segments_json"]:
                try:
                    segments = json.loads(row["segments_json"])
                except Exception:
                    pass

            results.append({
                "activity_id": row["activity_id"],
                "name": row["name"],
                "sport_type": row["sport_type"],
                "date": row["activity_date"],
                "distance_km": row["distance_km"],
                "elevation_m": row["elevation_m"],
                "moving_time_s": row["moving_time_s"],
                "suffer_score": row["suffer_score"],
                "avg_hr": row["avg_hr"],
                "max_hr": row["max_hr"],
                "similarity_score": round(max(0.0, min(1.0, score)), 4),
                "segments_count": len(segments),
                "key_segments": [
                    {
                        "name": s.get("name"),
                        "distance": s.get("distance"),
                        "grade": s.get("average_grade"),
                        "watts": s.get("average_watts"),
                        "pr": s.get("is_pr", False),
                    }
                    for s in segments[:5]
                ],
                "document_summary": row["document_text"],
            })

        return results

    DEFAULT_PROFILE = {
        "category": "Development Racer / Lightweight Climber",
        "gender": "male",
        "rider_weight_kg": 60.0,
        "bike_model": "Trek Checkpoint Gravel",
        "bike_weight_kg": 10.4,
        "gear_weight_kg": 2.5,
        "hr_max": 205,
        "hr_rest": 59,
        "coaching_tone": "uncompromising_analytical",
        "primary_sport": "Cycling (Gravel / Road)",
        "goals": "Solide Grundlagenausdauer aufbauen, Pacing an Steigungen disziplinieren, Sehnenüberlastung in intensiven Aufbauphasen vermeiden",
        "known_weaknesses": [
            "Tendenz zum Überpacen auf den ersten 15-20 Minuten",
            "Mangelnde Pacing-Disziplin bei Zwischenanstiegen",
        ],
    }

    def get_profile(self, user_id: str = "default") -> Dict[str, Any]:
        """Gibt das vollständige Athletenprofil für einen bestimmten Nutzer zurück."""
        clean_user = str(user_id).strip() or "default"
        profile = dict(self.DEFAULT_PROFILE)
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT key, value_json FROM athlete_profile WHERE user_id = ? OR user_id IS NULL",
                (clean_user,),
            )
            rows = cur.fetchall()
            for r in rows:
                k = r["key"]
                try:
                    val = json.loads(r["value_json"])
                    profile[k] = val
                except Exception:
                    pass

        # Gesamtsystemgewicht dynamisch berechnen
        r_wt = float(profile.get("rider_weight_kg", 60.0))
        b_wt = float(profile.get("bike_weight_kg", 9.5))
        g_wt = float(profile.get("gear_weight_kg", 2.5))
        profile["total_system_weight_kg"] = round(r_wt + b_wt + g_wt, 2)
        return profile

    def set_profile_field(self, key: str, value: Any, user_id: str = "default") -> Dict[str, Any]:
        """Setzt oder aktualisiert ein Feld im Athletenprofil für einen bestimmten Nutzer."""
        clean_key = str(key).strip().lower()
        clean_user = str(user_id).strip() or "default"
        now_iso = datetime.now(timezone.utc).isoformat()
        val_json = json.dumps(value, ensure_ascii=False)

        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM athlete_profile WHERE key = ? AND (user_id = ? OR user_id IS NULL)",
                (clean_key, clean_user),
            )
            if cur.fetchone():
                conn.execute(
                    """
                    UPDATE athlete_profile
                    SET value_json = ?, updated_at = ?, user_id = ?
                    WHERE key = ? AND (user_id = ? OR user_id IS NULL)
                    """,
                    (val_json, now_iso, clean_user, clean_key, clean_user),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO athlete_profile (key, value_json, updated_at, user_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at,
                        user_id=excluded.user_id
                    """,
                    (clean_key, val_json, now_iso, clean_user),
                )
            conn.commit()

        logger.info("Athletenprofil [%s] aktualisiert: %s = %s", clean_user, clean_key, value)
        return self.get_profile(user_id=clean_user)

    def add_coaching_note(self, note: str, category: str = "general", user_id: str = "default") -> Dict[str, Any]:
        """Speichert eine Beobachtung oder Notiz des Trainers/KI im Langzeitgedächtnis für einen Nutzer."""
        clean_user = str(user_id).strip() or "default"
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_note = str(note).strip()
        clean_cat = str(category).strip().lower()

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO coaching_notes (timestamp, category, note, user_id)
                VALUES (?, ?, ?, ?)
                """,
                (now_iso, clean_cat, clean_note, clean_user),
            )
            conn.commit()
            note_id = cur.lastrowid

        logger.info("Coaching-Notiz [%s/%s] gespeichert: %s", clean_user, clean_cat, clean_note[:60])
        return {
            "id": note_id,
            "timestamp": now_iso,
            "category": clean_cat,
            "note": clean_note,
            "user_id": clean_user,
        }

    def get_coaching_notes(self, limit: int = 10, category: Optional[str] = None, user_id: str = "default") -> List[Dict[str, Any]]:
        """Liest die neuesten Trainer- und Feedbacknotizen für einen bestimmten Nutzer aus."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            if category:
                cur = conn.execute(
                    "SELECT * FROM coaching_notes WHERE (user_id = ? OR user_id IS NULL) AND category = ? ORDER BY id DESC LIMIT ?",
                    (clean_user, str(category).strip().lower(), max(1, limit)),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM coaching_notes WHERE (user_id = ? OR user_id IS NULL) ORDER BY id DESC LIMIT ?",
                    (clean_user, max(1, limit)),
                )
            return [dict(r) for r in cur.fetchall()]

    def record_post_workout_checkin(
        self,
        activity_id: str,
        rpe_score: int,
        legs_feeling: int = 5,
        mental_state: Optional[str] = None,
        fueling_adherence: Optional[str] = None,
        weather_felt: Optional[str] = None,
        pain_or_discomfort: Optional[str] = None,
        athlete_notes: Optional[str] = None,
        duration_minutes: Optional[float] = None,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Speichert das subjektive Athleten-Feedback (Session-RPE / Foster-Modell)
        für eine absolvierte Einheit im Langzeitgedächtnis.
        """
        clean_user = str(user_id).strip() or "default"
        clean_act = str(activity_id).strip()
        now_iso = datetime.now(timezone.utc).isoformat()
        rpe = max(1, min(10, int(rpe_score)))
        legs = max(1, min(10, int(legs_feeling)))

        # Dauer ermitteln für Session-RPE (sRPE = RPE * Dauer in Minuten)
        dur_min = duration_minutes
        if dur_min is None or dur_min <= 0:
            with self._get_connection() as conn:
                cur = conn.execute(
                    "SELECT moving_time_seconds, elapsed_time_seconds FROM activities WHERE activity_id = ? AND (user_id = ? OR user_id IS NULL)",
                    (clean_act, clean_user),
                )
                row = cur.fetchone()
                if row:
                    mov_s = row["moving_time_seconds"] or row["elapsed_time_seconds"] or 0
                    dur_min = mov_s / 60.0
                else:
                    dur_min = 60.0  # Fallback 1h

        session_rpe_load = round(rpe * dur_min, 1)

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO post_workout_checkins (
                    activity_id, recorded_at, rpe_score, legs_feeling,
                    mental_state, fueling_adherence, weather_felt,
                    pain_or_discomfort, athlete_notes, session_rpe_load, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_act,
                    now_iso,
                    rpe,
                    legs,
                    mental_state or "",
                    fueling_adherence or "",
                    weather_felt or "",
                    pain_or_discomfort or "",
                    athlete_notes or "",
                    session_rpe_load,
                    clean_user,
                ),
            )
            checkin_id = cur.lastrowid
            conn.commit()

        # Falls Schmerzen oder Beschwerden gemeldet wurden: automatisch eine Coaching-Notiz anlegen!
        if pain_or_discomfort and pain_or_discomfort.strip().lower() not in ["keine", "none", "nein", "kein", ""]:
            self.add_coaching_note(
                note=f"[Aktivität {clean_act}] Schmerzen/Beschwerden gemeldet: {pain_or_discomfort}",
                category="injury",
                user_id=clean_user,
            )

        return {
            "checkin_id": checkin_id,
            "activity_id": clean_act,
            "recorded_at": now_iso,
            "rpe_score": rpe,
            "legs_feeling": legs,
            "session_rpe_load": session_rpe_load,
            "duration_minutes_used": round(dur_min, 1),
            "mental_state": mental_state,
            "fueling_adherence": fueling_adherence,
            "weather_felt": weather_felt,
            "pain_or_discomfort": pain_or_discomfort,
            "athlete_notes": athlete_notes,
            "user_id": clean_user,
        }

    def get_post_workout_checkins(
        self,
        activity_id: Optional[str] = None,
        limit: int = 10,
        user_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """Liest gespeicherte Post-Workout-Checkins aus."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            if activity_id:
                cur = conn.execute(
                    "SELECT * FROM post_workout_checkins WHERE (user_id = ? OR user_id IS NULL) AND activity_id = ? ORDER BY id DESC LIMIT ?",
                    (clean_user, str(activity_id).strip(), max(1, limit)),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM post_workout_checkins WHERE (user_id = ? OR user_id IS NULL) ORDER BY id DESC LIMIT ?",
                    (clean_user, max(1, limit)),
                )
            return [dict(r) for r in cur.fetchall()]

    def save_apple_health_metrics(
        self,
        recorded_date: str,
        sleep_hours: Optional[float] = None,
        deep_sleep_minutes: Optional[float] = None,
        rem_sleep_minutes: Optional[float] = None,
        resting_hr: Optional[int] = None,
        hrv_ms: Optional[float] = None,
        readiness_score: Optional[float] = None,
        readiness_verdict: str = "",
        raw_data: Optional[Dict[str, Any]] = None,
        user_id: str = "default",
        steps: Optional[int] = None,
        active_energy_kcal: Optional[float] = None,
        distance_meters: Optional[float] = None,
        vo2_max: Optional[float] = None,
        weight_kg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Speichert oder aktualisiert Apple Health Metriken für ein bestimmtes Datum und einen Nutzer."""
        clean_user = str(user_id).strip() or "default"
        now_iso = datetime.now(timezone.utc).isoformat()

        if raw_data:
            if steps is None and "steps" in raw_data:
                try:
                    steps = int(float(raw_data["steps"]))
                except Exception:
                    pass
            if active_energy_kcal is None and "active_energy_kcal" in raw_data:
                try:
                    active_energy_kcal = float(raw_data["active_energy_kcal"])
                except Exception:
                    pass
            if distance_meters is None and "distance_meters" in raw_data:
                try:
                    distance_meters = float(raw_data["distance_meters"])
                except Exception:
                    pass
            if vo2_max is None and "vo2_max" in raw_data:
                try:
                    vo2_max = float(raw_data["vo2_max"])
                except Exception:
                    pass
            if weight_kg is None and "weight_kg" in raw_data:
                try:
                    weight_kg = float(raw_data["weight_kg"])
                except Exception:
                    pass

        raw_json = json.dumps(raw_data or {}, ensure_ascii=False)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO apple_health_metrics (
                    recorded_date, sleep_hours, deep_sleep_minutes, rem_sleep_minutes,
                    resting_hr, hrv_ms, readiness_score, readiness_verdict, raw_json, synced_at, user_id,
                    steps, active_energy_kcal, distance_meters, vo2_max, weight_kg
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recorded_date) DO UPDATE SET
                    sleep_hours=excluded.sleep_hours,
                    deep_sleep_minutes=excluded.deep_sleep_minutes,
                    rem_sleep_minutes=excluded.rem_sleep_minutes,
                    resting_hr=excluded.resting_hr,
                    hrv_ms=excluded.hrv_ms,
                    readiness_score=excluded.readiness_score,
                    readiness_verdict=excluded.readiness_verdict,
                    raw_json=excluded.raw_json,
                    synced_at=excluded.synced_at,
                    user_id=excluded.user_id,
                    steps=excluded.steps,
                    active_energy_kcal=excluded.active_energy_kcal,
                    distance_meters=excluded.distance_meters,
                    vo2_max=excluded.vo2_max,
                    weight_kg=excluded.weight_kg
                """,
                (
                    recorded_date, sleep_hours, deep_sleep_minutes, rem_sleep_minutes,
                    resting_hr, hrv_ms, readiness_score, readiness_verdict, raw_json, now_iso, clean_user,
                    steps, active_energy_kcal, distance_meters, vo2_max, weight_kg,
                ),
            )
            conn.commit()

        logger.info(
            "Apple Health Metriken für %s [%s] gespeichert: Schlaf=%.1fh, Ruhepuls=%s, HRV=%sms, Steps=%s, Readiness=%.0f",
            recorded_date, clean_user, sleep_hours or 0.0, resting_hr or "--", hrv_ms or "--", steps or "--", readiness_score or 0.0,
        )
        return {
            "recorded_date": recorded_date,
            "sleep_hours": sleep_hours,
            "deep_sleep_minutes": deep_sleep_minutes,
            "rem_sleep_minutes": rem_sleep_minutes,
            "resting_hr": resting_hr,
            "hrv_ms": hrv_ms,
            "steps": steps,
            "active_energy_kcal": active_energy_kcal,
            "distance_meters": distance_meters,
            "vo2_max": vo2_max,
            "weight_kg": weight_kg,
            "readiness_score": readiness_score,
            "readiness_verdict": readiness_verdict,
            "synced_at": now_iso,
            "user_id": clean_user,
        }

    def get_latest_apple_health_metrics(self, user_id: str = "default") -> Optional[Dict[str, Any]]:
        """Liefert den aktuellsten Eintrag aus Apple Health für einen bestimmten Nutzer."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM apple_health_metrics WHERE user_id = ? OR user_id IS NULL ORDER BY recorded_date DESC LIMIT 1",
                (clean_user,),
            )
            row = cur.fetchone()
            if row:
                res = dict(row)
                if res.get("raw_json"):
                    try:
                        res["raw_data"] = json.loads(res["raw_json"])
                    except Exception:
                        pass
                return res
            return None

    def get_apple_health_history(self, days: int = 7, user_id: str = "default") -> List[Dict[str, Any]]:
        """Liefert die letzten N Tage an Apple Health Gesundheitsdaten."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM apple_health_metrics WHERE user_id = ? OR user_id IS NULL ORDER BY recorded_date DESC LIMIT ?",
                (clean_user, max(1, days)),
            )
            result = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("raw_json"):
                    try:
                        d["raw_data"] = json.loads(d["raw_json"])
                    except Exception:
                        pass
                result.append(d)
            return result

    def save_apple_health_workout(
        self,
        id: str,
        activity_type: str,
        duration_minutes: float,
        distance_meters: float = 0.0,
        energy_kcal: float = 0.0,
        start_date: str = "",
        end_date: str = "",
        source: str = "",
        raw_data: Optional[Dict[str, Any]] = None,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Speichert oder aktualisiert ein Apple Health Workout (z. B. Laufen, Radfahren)."""
        clean_user = str(user_id).strip() or "default"
        clean_id = str(id).strip()
        now_iso = datetime.now(timezone.utc).isoformat()
        raw_json = json.dumps(raw_data or {}, ensure_ascii=False)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO apple_health_workouts (
                    id, activity_type, duration_minutes, distance_meters, energy_kcal,
                    start_date, end_date, source, raw_json, synced_at, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    activity_type=excluded.activity_type,
                    duration_minutes=excluded.duration_minutes,
                    distance_meters=excluded.distance_meters,
                    energy_kcal=excluded.energy_kcal,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    source=excluded.source,
                    raw_json=excluded.raw_json,
                    synced_at=excluded.synced_at,
                    user_id=excluded.user_id
                """,
                (
                    clean_id, activity_type, duration_minutes, distance_meters, energy_kcal,
                    start_date, end_date, source, raw_json, now_iso, clean_user,
                ),
            )
            conn.commit()

        return {
            "id": clean_id,
            "activity_type": activity_type,
            "duration_minutes": duration_minutes,
            "distance_meters": distance_meters,
            "energy_kcal": energy_kcal,
            "start_date": start_date,
            "end_date": end_date,
            "source": source,
            "synced_at": now_iso,
            "user_id": clean_user,
        }

    def get_apple_health_workouts(self, days: int = 14, user_id: str = "default") -> List[Dict[str, Any]]:
        """Liefert die in Apple Health erfassten Workouts der letzten N Tage."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM apple_health_workouts
                WHERE user_id = ? OR user_id IS NULL
                ORDER BY start_date DESC
                LIMIT ?
                """,
                (clean_user, max(1, days * 10)),
            )
            result = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("raw_json"):
                    try:
                        d["raw_data"] = json.loads(d["raw_json"])
                    except Exception:
                        pass
                result.append(d)
            return result

    def save_training_alert(
        self,
        alert_key: str,
        severity: str,
        title: str,
        message: str,
        category: str = "load",
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Speichert oder aktualisiert eine proaktive Trainings- oder Belastungswarnung."""
        clean_user = str(user_id).strip() or "default"
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE training_alerts SET is_active = 0
                WHERE alert_key = ? AND user_id = ?
                """,
                (alert_key, clean_user),
            )
            cur = conn.execute(
                """
                INSERT INTO training_alerts (alert_key, severity, title, message, category, created_at, is_active, user_id)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (alert_key, severity, title, message, category, now_iso, clean_user),
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "alert_key": alert_key,
                "severity": severity,
                "title": title,
                "message": message,
                "category": category,
                "created_at": now_iso,
                "is_active": True,
                "user_id": clean_user,
            }

    def get_active_training_alerts(self, user_id: str = "default", limit: int = 20) -> List[Dict[str, Any]]:
        """Liefert alle aktuell aktiven Belastungs- und Gesundheitswarnungen."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM training_alerts
                WHERE (user_id = ? OR user_id IS NULL) AND is_active = 1
                ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, created_at DESC
                LIMIT ?
                """,
                (clean_user, max(1, limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def dismiss_training_alert(self, alert_id: int, user_id: str = "default") -> bool:
        """Deaktiviert eine spezifische Trainingswarnung."""
        clean_user = str(user_id).strip() or "default"
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE training_alerts SET is_active = 0 WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (alert_id, clean_user),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_segment_history(
        self,
        segment_query: str,
        limit: int = 20,
        max_efforts: Optional[int] = None,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Führt eine longitudinale Verlaufsanalyse für ein Strava-Segment über alle Aktivitäten durch.
        Berechnet Fortschritt bei Zeit, Watt, W/kg, Herzfrequenz, VAM und aerober Effizienz (EF).
        """
        query_clean = str(segment_query).strip().lower()
        effective_limit = max_efforts if max_efforts is not None else limit

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT activity_id, name, sport_type, activity_date, segments_json
                FROM activities
                WHERE segments_json IS NOT NULL AND length(segments_json) > 5 AND (user_id = ? OR user_id = 'default')
                ORDER BY activity_date ASC
                """,
                (user_id,)
            )
            rows = cur.fetchall()

        efforts = []
        matching_segment_name = None
        segment_id_found = None

        def _parse_time(val: Any) -> float:
            if not val:
                return 0.0
            parts = str(val).strip().split(":")
            try:
                if len(parts) == 2:
                    return float(parts[0]) * 60.0 + float(parts[1])
                elif len(parts) == 3:
                    return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
                return float(val)
            except Exception:
                return 0.0

        def _parse_float(val: Any) -> Optional[float]:
            if val is None:
                return None
            s = re.sub(r"[^\d,\.\-]", "", str(val)).replace(",", ".")
            try:
                return float(s)
            except Exception:
                return None

        for row in rows:
            try:
                segs = json.loads(row["segments_json"])
            except Exception:
                continue

            for s in segs:
                s_id = str(s.get("segment_id", "")).strip()
                s_name = str(s.get("name", "")).strip()
                if query_clean == s_id.lower() or query_clean in s_name.lower():
                    if not matching_segment_name:
                        matching_segment_name = s_name
                        segment_id_found = s_id

                    m_time_s = _parse_time(
                        s.get("moving_time") or s.get("elapsed_time") or s.get("elapsed_time_s") or s.get("time_seconds")
                    )
                    watts = _parse_float(s.get("avg_power") or s.get("avg_watts"))
                    avg_hr = _parse_float(s.get("avg_hr") or s.get("avg_hr_bpm"))
                    speed = _parse_float(s.get("avg_speed") or s.get("avg_speed_kmh"))
                    vam = _parse_float(s.get("vam") or s.get("vam_m_per_hour"))
                    ef = round(watts / avg_hr, 3) if (watts and avg_hr and avg_hr > 40) else None

                    efforts.append({
                        "activity_id": row["activity_id"],
                        "activity_name": row["name"],
                        "activity_date": row["activity_date"][:10],
                        "segment_id": s_id,
                        "segment_name": s_name,
                        "distance": s.get("distance") or s.get("distance_m"),
                        "average_grade": s.get("avg_grade") or s.get("average_grade_pct"),
                        "time_seconds": round(m_time_s, 1),
                        "elapsed_time_s": int(round(m_time_s, 0)),
                        "time_hms": str(s.get("moving_time") or s.get("elapsed_time") or f"{int(m_time_s // 60):02d}:{int(m_time_s % 60):02d}"),
                        "avg_speed_kmh": speed,
                        "avg_watts": watts,
                        "avg_hr_bpm": avg_hr,
                        "max_hr_bpm": _parse_float(s.get("max_hr")),
                        "vam_m_per_hour": vam,
                        "efficiency_factor": ef,
                        "is_pr": bool(s.get("achievement") == "PR" or s.get("is_pr") or s.get("pr_rank") == 1),
                    })

        if not efforts:
            suggested = self.get_available_segments(limit=10, user_id=user_id)
            return {
                "status": "not_found",
                "query": segment_query,
                "message": f"Keine Aktivitäten mit Segment '{segment_query}' in den bisherigen Aktivitäten gefunden.",
                "available_segments": suggested,
            }

        efforts = efforts[-effective_limit:]
        valid_times = [e["time_seconds"] for e in efforts if e["time_seconds"] > 0]
        pr_effort = min(efforts, key=lambda e: e["time_seconds"] if e["time_seconds"] > 0 else 999999) if valid_times else efforts[0]

        delta_info = None
        if len(efforts) >= 2:
            first = efforts[0]
            last = efforts[-1]
            t_first = first["time_seconds"]
            t_last = last["time_seconds"]
            diff_s = round(t_last - t_first, 1)
            improvement_s = round(t_first - t_last, 1)
            pct_change = round((diff_s / max(1.0, t_first)) * 100.0, 1) if t_first > 0 else 0.0

            watts_first = first.get("avg_watts")
            watts_last = last.get("avg_watts")
            diff_watts = round(watts_last - watts_first, 1) if (watts_first and watts_last) else None

            delta_info = {
                "first_effort_date": first["activity_date"],
                "latest_effort_date": last["activity_date"],
                "time_difference_seconds": diff_s,
                "time_improvement_s": improvement_s,
                "time_change_percentage": pct_change,
                "power_difference_watts": diff_watts,
                "watts_delta": diff_watts,
                "trend": "improved" if diff_s < 0 else "slower" if diff_s > 0 else "neutral",
            }

        return {
            "status": "success",
            "segment_name": matching_segment_name,
            "segment_id": segment_id_found,
            "efforts_count": len(efforts),
            "total_efforts_recorded": len(efforts),
            "personal_record": {
                "date": pr_effort["activity_date"],
                "time_hms": pr_effort["time_hms"],
                "elapsed_time_s": pr_effort.get("elapsed_time_s"),
                "time_seconds": pr_effort.get("time_seconds"),
                "avg_watts": pr_effort.get("avg_watts"),
                "avg_hr": pr_effort.get("avg_hr_bpm"),
                "speed_kmh": pr_effort.get("avg_speed_kmh"),
                "activity_name": pr_effort["activity_name"],
            },
            "trends": delta_info,
            "longitudinal_progression": delta_info,
            "efforts_history": efforts,
            "history": efforts,
        }

    def get_available_segments(self, limit: int = 30, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Liefert eine Liste aller in der Datenbank erfassten Strava-Segmente."""
        with self._get_connection() as conn:
            if user_id:
                cur = conn.execute(
                    """
                    SELECT segments_json FROM activities
                    WHERE segments_json IS NOT NULL AND length(segments_json) > 5 AND (user_id = ? OR user_id = 'default')
                    """,
                    (user_id,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT segments_json FROM activities
                    WHERE segments_json IS NOT NULL AND length(segments_json) > 5
                    """
                )
            rows = cur.fetchall()

        seen: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                segs = json.loads(r["segments_json"])
                for s in segs:
                    s_id = str(s.get("segment_id", "")).strip()
                    if s_id and s_id not in seen:
                        seen[s_id] = {
                            "segment_id": s_id,
                            "name": s.get("name"),
                            "distance": s.get("distance"),
                            "average_grade": s.get("avg_grade"),
                            "elevation_gain": s.get("elevation_gain"),
                            "climb_category": s.get("climb_category"),
                        }
            except Exception:
                continue

        return list(seen.values())[:limit]

    def get_segment_progress_history(
        self,
        segment_name: str,
        limit: int = 10,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Durchsucht alle bisher über get_activity_segments erfassten Segment-Daten
        nach einem Segmentnamen und liefert den chronologischen Verlauf:
        Datum, Zeit, Durchschnittsleistung, VAM, Rang/PR-Status pro Durchfahrt.
        """
        effective_limit = max(1, min(100, limit))
        query_clean = str(segment_name).strip().lower()

        with self._get_connection() as conn:
            if user_id and user_id != "default":
                cur = conn.execute(
                    """
                    SELECT activity_id, name, activity_date, segments_json FROM activities
                    WHERE segments_json IS NOT NULL AND length(segments_json) > 5 AND (user_id = ? OR user_id = 'default')
                    ORDER BY activity_date ASC
                    """,
                    (user_id,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT activity_id, name, activity_date, segments_json FROM activities
                    WHERE segments_json IS NOT NULL AND length(segments_json) > 5
                    ORDER BY activity_date ASC
                    """
                )
            rows = cur.fetchall()

        def _parse_time(val: Any) -> float:
            if val is None:
                return 0.0
            if isinstance(val, (int, float)):
                return float(val)
            s = str(val).strip()
            parts = s.split(":")
            try:
                if len(parts) == 3:
                    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2:
                    return float(parts[0]) * 60 + float(parts[1])
                elif len(parts) == 1:
                    return float(re.sub(r"[^\d.]", "", parts[0]) or 0)
            except Exception:
                return 0.0
            return 0.0

        def _parse_float(val: Any) -> Optional[float]:
            if val is None:
                return None
            s = re.sub(r"[^\d,\.\-]", "", str(val)).replace(",", ".")
            try:
                return float(s)
            except Exception:
                return None

        efforts = []
        matching_segment_name = ""
        segment_id_found = ""

        for row in rows:
            try:
                segs = json.loads(row["segments_json"])
            except Exception:
                continue

            for s in segs:
                s_id = str(s.get("segment_id", "")).strip()
                s_name = str(s.get("name", "")).strip()
                if query_clean == s_id.lower() or query_clean in s_name.lower():
                    if not matching_segment_name:
                        matching_segment_name = s_name
                        segment_id_found = s_id

                    m_time_s = _parse_time(
                        s.get("moving_time") or s.get("elapsed_time") or s.get("elapsed_time_s") or s.get("time_seconds")
                    )
                    watts = _parse_float(s.get("avg_power") or s.get("avg_watts"))
                    avg_hr = _parse_float(s.get("avg_hr") or s.get("avg_hr_bpm"))
                    speed = _parse_float(s.get("avg_speed") or s.get("avg_speed_kmh"))
                    vam = _parse_float(s.get("vam") or s.get("vam_m_per_hour"))
                    rank_val = s.get("rank") or s.get("pr_rank")
                    is_pr = bool(s.get("achievement") == "PR" or s.get("is_pr") or rank_val == 1)

                    status_str = "PR" if is_pr else (f"Rang {rank_val}" if rank_val else "Standard")

                    time_hms_str = str(
                        s.get("moving_time")
                        or s.get("elapsed_time")
                        or f"{int(m_time_s // 60):02d}:{int(m_time_s % 60):02d}"
                    )

                    efforts.append({
                        "date": str(row["activity_date"])[:10],
                        "time": time_hms_str,
                        "time_seconds": round(m_time_s, 1),
                        "avg_watts": watts,
                        "vam": vam,
                        "rank": rank_val,
                        "is_pr": is_pr,
                        "status": status_str,
                        "avg_hr": avg_hr,
                        "avg_speed_kmh": speed,
                        "distance": s.get("distance") or s.get("distance_m"),
                        "elevation_gain": s.get("elevation_gain"),
                        "average_grade": s.get("avg_grade") or s.get("average_grade_pct"),
                        "activity_id": str(row["activity_id"]),
                        "activity_name": str(row["name"]),
                    })

        if not efforts:
            suggested = self.get_available_segments(limit=10, user_id=user_id)
            return {
                "status": "not_found",
                "segment_name": segment_name,
                "message": f"Keine Aktivitäten mit dem Segment '{segment_name}' in der Datenbank gefunden.",
                "available_segments": suggested,
                "data_quality_and_anomalies": {
                    "verdict": "clean",
                    "anomaly_count": 0,
                    "anomalies": [],
                },
            }

        # Chronologisch sortieren (von ältestem zu neuestem Durchlauf)
        efforts.sort(key=lambda x: (x["date"], x["time_seconds"]))
        efforts = efforts[-effective_limit:]

        # PR ermitteln (schnellste gültige Zeit)
        valid_times = [e for e in efforts if e["time_seconds"] > 0]
        pr_effort = min(valid_times, key=lambda e: e["time_seconds"]) if valid_times else efforts[0]

        # Progression / Trend berechnen
        progression = None
        if len(efforts) >= 2:
            first = efforts[0]
            latest = efforts[-1]
            t_first = first["time_seconds"]
            t_latest = latest["time_seconds"]
            improvement_s = round(t_first - t_latest, 1)  # Positiv = schneller geworden
            pct_change = round((improvement_s / max(1.0, t_first)) * 100.0, 1)

            w_first = first.get("avg_watts")
            w_latest = latest.get("avg_watts")
            diff_watts = round(w_latest - w_first, 1) if (w_first and w_latest) else None

            trend_status = "improved" if improvement_s > 0 else ("slower" if improvement_s < 0 else "neutral")

            summary_text = (
                f"Fortschritt von {first['date']} bis {latest['date']}: "
                f"{'+' if improvement_s >= 0 else ''}{improvement_s:.1f}s ({'+' if pct_change >= 0 else ''}{pct_change:.1f}%) "
                f"{f'bei {diff_watts:+.0f}W Leistung' if diff_watts is not None else ''}."
            )

            progression = {
                "first_effort_date": first["date"],
                "latest_effort_date": latest["date"],
                "time_improvement_seconds": improvement_s,
                "time_improvement_hms": f"{int(abs(improvement_s) // 60):02d}:{int(abs(improvement_s) % 60):02d}",
                "improvement_percentage": pct_change,
                "watts_delta": diff_watts,
                "trend": trend_status,
                "summary": summary_text,
            }

        # Datenqualitätsprüfung / Anomalien
        anomalies = []
        missing_watts = sum(1 for e in efforts if e.get("avg_watts") is None)
        if missing_watts > 0:
            anomalies.append({
                "type": "missing_segment_power",
                "severity": "info",
                "title": "Teilweise fehlende Leistungswerte (Watt)",
                "description": f"In {missing_watts} von {len(efforts)} Segment-Durchfahrten waren keine Leistungswerte vorhanden (z. B. ohne Powermeter).",
            })

        return {
            "status": "success",
            "segment_name": matching_segment_name or segment_name,
            "segment_id": segment_id_found,
            "total_efforts_recorded": len(efforts),
            "chronological_progress": efforts,
            "personal_record": {
                "date": pr_effort["date"],
                "time": pr_effort["time"],
                "time_seconds": pr_effort["time_seconds"],
                "avg_watts": pr_effort.get("avg_watts"),
                "vam": pr_effort.get("vam"),
                "activity_name": pr_effort["activity_name"],
            },
            "progression_summary": progression,
            "data_quality_and_anomalies": {
                "verdict": "info" if anomalies else "clean",
                "anomaly_count": len(anomalies),
                "anomalies": anomalies,
            },
        }


def sync_local_gpx_to_vector_store() -> int:
    """
    Scannt das ./downloads/-Verzeichnis, analysiert alle GPX-Dateien
    und indexiert sie im Vektor-Store, falls noch nicht vorhanden oder aktualisiert.
    Gibt die Anzahl neu/aktualisierter Einträge zurück.
    """
    from analyze_gpx import calculate_metrics, parse_gpx_file

    store = ActivityVectorStore()
    count_indexed = 0

    if not DOWNLOADS_DIR.exists():
        return 0

    activity_files = [
        f for f in DOWNLOADS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in (".gpx", ".tcx", ".json")
    ]
    logger.info("Starte Indexierung lokaler Aktivitäten (Gefunden: %d)...", len(activity_files))

    for f in activity_files:
        try:
            # ID aus Dateinamen ermitteln
            file_id = f.stem.split("_")[0]
            existing = store.get_activity(file_id)

            title, act_type, points = parse_gpx_file(f)
            if not points:
                continue

            metrics = calculate_metrics(f, title, act_type, points)
            act_date = points[0].time.strftime("%Y-%m-%d") if points[0].time else "2026-01-01"

            store.upsert_activity(
                activity_id=file_id,
                name=title or f.stem,
                sport_type=act_type or "Ride",
                activity_date=act_date,
                distance_km=round(metrics.total_distance_km, 2),
                elevation_m=round(metrics.elevation_gain_m, 1),
                moving_time_s=int(metrics.moving_time_sec),
                suffer_score=metrics.suffer_score,
                avg_hr=metrics.avg_hr,
                max_hr=metrics.max_hr,
            )
            count_indexed += 1
        except Exception as e:
            logger.warning("Konnte Datei '%s' nicht für Vektor-Store verarbeiten: %s", f.name, e)

    logger.info("Vektor-Store Synchronisation abgeschlossen (%d Aktivitäten im Index).", store.count())
    return count_indexed
