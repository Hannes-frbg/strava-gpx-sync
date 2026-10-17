"""
test_mcp_server.py: Unit tests for MCP server optimizations (BrowserManager & UvicornAccessFilter).
"""

import logging
from pathlib import Path
import unittest
from mcp_server import BrowserManager, UvicornAccessFilter


class TestUvicornAccessFilter(unittest.TestCase):
    def setUp(self):
        self.filter = UvicornAccessFilter()
        self.logger = logging.getLogger("test_uvicorn_filter")
        self.logger.setLevel(logging.INFO)

    def test_filter_suppresses_404_and_405(self):
        record_404 = self.logger.makeRecord(
            name="uvicorn.access",
            level=logging.INFO,
            fn="server.py",
            lno=10,
            msg='%s - "%s %s %s" %d',
            args=("127.0.0.1:1234", "GET", "/.well-known/openid", "1.1", 404),
            exc_info=None,
        )
        self.assertFalse(self.filter.filter(record_404))

        record_405 = self.logger.makeRecord(
            name="uvicorn.access",
            level=logging.INFO,
            fn="server.py",
            lno=11,
            msg='%s - "%s %s %s" %d',
            args=("127.0.0.1:1234", "GET", "/sse", "1.1", 405),
            exc_info=None,
        )
        self.assertFalse(self.filter.filter(record_405))

    def test_filter_allows_normal_status_codes(self):
        record_200 = self.logger.makeRecord(
            name="uvicorn.access",
            level=logging.INFO,
            fn="server.py",
            lno=12,
            msg='%s - "%s %s %s" %d',
            args=("127.0.0.1:1234", "POST", "/messages", "1.1", 200),
            exc_info=None,
        )
        self.assertTrue(self.filter.filter(record_200))

        record_500 = self.logger.makeRecord(
            name="uvicorn.access",
            level=logging.INFO,
            fn="server.py",
            lno=13,
            msg='%s - "%s %s %s" %d',
            args=("127.0.0.1:1234", "GET", "/error", "1.1", 500),
            exc_info=None,
        )
        self.assertTrue(self.filter.filter(record_500))

    def test_filter_string_formatted_messages(self):
        record_str_404 = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="server.py",
            lineno=14,
            msg='127.0.0.1:1234 - "GET /.well-known/ HTTP/1.1" 404 Not Found',
            args=(),
            exc_info=None,
        )
        self.assertFalse(self.filter.filter(record_str_404))

        record_str_405 = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="server.py",
            lineno=15,
            msg='127.0.0.1:1234 - "GET /sse HTTP/1.1" 405 Method Not Allowed',
            args=(),
            exc_info=None,
        )
        self.assertFalse(self.filter.filter(record_str_405))


class TestBrowserManagerSingleton(unittest.TestCase):
    def test_singleton_identity(self):
        bm1 = BrowserManager.get_instance()
        bm2 = BrowserManager.get_instance()
        self.assertIs(bm1, bm2)


class TestActivityFilteringAndPagination(unittest.TestCase):
    def test_parse_date_to_datetime(self):
        from mcp_server import parse_date_to_datetime
        from datetime import datetime, timezone

        # German date format
        dt_de = parse_date_to_datetime("Sa., 19.9.2026")
        self.assertIsNotNone(dt_de)
        self.assertEqual(dt_de.year, 2026)
        self.assertEqual(dt_de.month, 9)
        self.assertEqual(dt_de.day, 19)

        # ISO format
        dt_iso = parse_date_to_datetime("2026-09-15")
        self.assertIsNotNone(dt_iso)
        self.assertEqual(dt_iso.year, 2026)
        self.assertEqual(dt_iso.month, 9)
        self.assertEqual(dt_iso.day, 15)

        # Empty / None
        self.assertIsNone(parse_date_to_datetime(None))
        self.assertIsNone(parse_date_to_datetime(""))
        self.assertIsNone(parse_date_to_datetime("invalid-string"))

    def test_matches_sport(self):
        from mcp_server import matches_sport

        # Aliases
        self.assertTrue(matches_sport("Radfahren", "Ride"))
        self.assertTrue(matches_sport("Schotterfahrt", "GravelRide"))
        self.assertTrue(matches_sport("Schotterfahrt", "Gravel"))
        self.assertTrue(matches_sport("Laufen", "Run"))
        self.assertTrue(matches_sport("Wanderung", "Hike"))
        self.assertTrue(matches_sport("Spaziergang", "Walk"))

        # Case insensitive
        self.assertTrue(matches_sport("Radfahren", "ride"))
        self.assertTrue(matches_sport("radfahren", "RIDE"))

        # Non-matching
        self.assertFalse(matches_sport("Wanderung", "Ride"))
        self.assertFalse(matches_sport("Radfahren", "Run"))

        # None / empty allows everything
        self.assertTrue(matches_sport("Radfahren", None))
        self.assertTrue(matches_sport("Radfahren", ""))

    def test_matches_date_range(self):
        from mcp_server import matches_date_range, parse_date_to_datetime

        after_dt = parse_date_to_datetime("2026-09-01")
        before_dt = parse_date_to_datetime("2026-09-20")

        self.assertTrue(matches_date_range("Sa., 19.9.2026", after_dt, before_dt))
        self.assertTrue(matches_date_range("Di., 15.9.2026", after_dt, before_dt))

        # Outside range
        self.assertFalse(matches_date_range("Sa., 19.9.2026", None, parse_date_to_datetime("2026-09-10")))
        self.assertFalse(matches_date_range("Sa., 19.9.2026", parse_date_to_datetime("2026-09-25"), None))


class TestBrowserManager(unittest.IsolatedAsyncioTestCase):
    async def test_run_async_supports_kwargs(self):
        """Testet, dass BrowserManager.run_async kwargs ohne TypeError via loop.run_in_executor ausführt."""
        from mcp_server import BrowserManager

        bm = BrowserManager.get_instance()
        orig_init = getattr(bm, "_init_browser_internal", None)
        bm._init_browser_internal = lambda: None
        try:
            # Dummy function taking kwargs
            def dummy_task(ctx, val, multiplier=2, suffix="!"):
                return f"{val * multiplier}{suffix}"

            res = await bm.run_async(dummy_task, 21, multiplier=2, suffix=" done")
            self.assertEqual(res, "42 done")
        finally:
            if orig_init:
                bm._init_browser_internal = orig_init


class TestTrainingLoadModel(unittest.TestCase):
    def test_fallback_tss_calculation(self):
        from training_load import calculate_fallback_tss

        # Radfahrt 1h flach
        tss_ride = calculate_fallback_tss(3600, 25000, 0, "Ride")
        self.assertGreaterEqual(tss_ride, 40.0)
        self.assertLessEqual(tss_ride, 60.0)

        # Radfahrt 1h mit 500m Höhenmeter (sollte deutlich mehr TSS haben)
        tss_climb = calculate_fallback_tss(3600, 25000, 500, "Ride")
        self.assertGreater(tss_climb, tss_ride + 15.0)

        # Wanderung vs Laufen
        tss_run = calculate_fallback_tss(3600, 10000, 100, "Run")
        tss_hike = calculate_fallback_tss(3600, 4500, 100, "Hike")
        self.assertGreater(tss_run, tss_hike)

    def test_hrtss_calculation(self):
        from training_load import calculate_activity_tss

        # Suffer Score None, aber avg_hr und max_hr vorhanden (hrTSS Fall)
        tss, source = calculate_activity_tss(
            duration_s=3600,
            distance_m=25000,
            elevation_gain_m=200,
            sport_type="Ride",
            suffer_score=None,
            avg_hr=150,
            max_hr=185,
        )
        self.assertEqual(source, "hrss")
        self.assertGreater(tss, 0.0)
        self.assertLess(tss, 300.0)

    def test_training_load_ewma_continuity(self):
        from datetime import date, timedelta
        from training_load import TrainingLoadEngine

        engine = TrainingLoadEngine()
        today = date.today()

        # 30 Tage konsistentes Training (50 TSS/Tag)
        for i in range(30):
            d = today - timedelta(days=30 - i)
            engine.add_activity(
                activity_id=f"test_{i}",
                name="Training",
                sport_type="Ride",
                activity_date=d,
                duration_s=3600,
                distance_m=30000,
                elevation_gain_m=200,
                suffer_score=60,
            )

        status = engine.get_training_status(until_date=today)
        self.assertEqual(status["status"], "success")
        metrics = status["metrics"]

        # CTL und ATL müssen positiv sein, ATL > CTL bei frischem Reiz
        self.assertGreater(metrics["ctl_fitness"], 10.0)
        self.assertGreater(metrics["atl_fatigue"], metrics["ctl_fitness"])
        self.assertLess(metrics["tsb_form"], 0.0)
        self.assertIn("form_zone", status)
        self.assertIn("key", status["form_zone"])


class TestRagEngine(unittest.TestCase):
    def test_document_synthesis(self):
        from rag_engine import ActivityVectorStore

        doc = ActivityVectorStore.build_document_text(
            name="Bergfahrt Schwarzwald",
            activity_id="12345",
            sport_type="GravelRide",
            activity_date="2026-09-18",
            distance_km=45.0,
            elevation_m=950.0,
            moving_time_s=7200,
            suffer_score=110.0,
            segments=[
                {"name": "Steiler Kandel", "distance": "3.5 km", "average_grade": "9.2%", "is_pr": True}
            ],
        )

        self.assertIn("Bergfahrt Schwarzwald", doc)
        self.assertIn("GravelRide", doc)
        self.assertIn("950 Meter Anstieg", doc)
        self.assertIn("Steiler Kandel", doc)
        self.assertIn("Klettertour", doc)

    def test_semantic_search_ranking(self):
        from rag_engine import ActivityVectorStore

        # SQLite In-Memory oder Temp DB testen
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_rag.db"
            store = ActivityVectorStore(db_path=test_db)

            # 2 Test-Aktivitäten anlegen
            store.upsert_activity(
                activity_id="101",
                name="Steile Bergetappe im Schwarzwald",
                sport_type="cycling",
                activity_date="2026-09-10",
                distance_km=35.0,
                elevation_m=1200.0,
                moving_time_s=6000,
                suffer_score=140.0,
            )
            store.upsert_activity(
                activity_id="102",
                name="Flache Ausdauerrunde am Fluss",
                sport_type="cycling",
                activity_date="2026-09-12",
                distance_km=60.0,
                elevation_m=80.0,
                moving_time_s=7000,
                suffer_score=50.0,
            )

            # Suche nach Bergfahrt
            res_climb = store.search_semantic("Steiler Berg Anstieg Höhenmeter", limit=2)
            self.assertEqual(len(res_climb), 2)
            self.assertEqual(res_climb[0]["activity_id"], "101")
            self.assertGreater(res_climb[0]["similarity_score"], res_climb[1]["similarity_score"])

            # Suche nach flacher Tour
            res_flat = store.search_semantic("Flache lockere Ausdauerfahrt", limit=2)
            self.assertEqual(res_flat[0]["activity_id"], "102")


class TestBackgroundSyncService(unittest.TestCase):
    def test_service_status_and_lifecycle(self):
        from background_sync import BackgroundSyncService

        svc = BackgroundSyncService.get_instance(interval_minutes=30)
        status = svc.get_status()
        self.assertEqual(status["base_interval_minutes"], 30)
        self.assertFalse(status["is_syncing_now"])


class TestAthleteProfileAndCoaching(unittest.TestCase):
    def test_profile_defaults_and_updates(self):
        import tempfile
        from pathlib import Path
        from rag_engine import ActivityVectorStore

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_rag.db"
            store = ActivityVectorStore(db_path=test_db)

            profile = store.get_profile()
            self.assertIn("category", profile)
            self.assertEqual(profile["hr_max"], 205)
            self.assertGreater(profile["total_system_weight_kg"], 50.0)

            # Update weight
            updated = store.set_profile_field("rider_weight_kg", 62.5)
            self.assertEqual(updated["rider_weight_kg"], 62.5)

    def test_coaching_notes_lifecycle(self):
        import tempfile
        from pathlib import Path
        from rag_engine import ActivityVectorStore

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_rag.db"
            store = ActivityVectorStore(db_path=test_db)

            store.add_coaching_note("Überzockt am ersten Anstieg", category="pacing")
            store.add_coaching_note("Knieschmerzen nach 50km", category="injury")

            all_notes = store.get_coaching_notes()
            self.assertEqual(len(all_notes), 2)

            injury_notes = store.get_coaching_notes(category="injury")
            self.assertEqual(len(injury_notes), 1)
            self.assertEqual(injury_notes[0]["category"], "injury")


class TestVirtualPower(unittest.TestCase):
    def test_virtual_power_and_drift_calculation(self):
        from datetime import datetime, timezone, timedelta
        from analyze_gpx import TrackPoint, estimate_virtual_power_and_coaching

        points = []
        base_time = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)
        # 20 Minuten stetige Steigung (5% Steigung bei 15 km/h ~ 4.17 m/s)
        lat = 48.0
        lon = 8.0
        ele = 300.0

        for i in range(120):  # 120 Punkte á 10s = 1200s (20 Min)
            t = base_time + timedelta(seconds=i * 10)
            lat += 0.0003
            ele += 2.0  # 2m Aufstieg alle 10s
            hr = 150 + (i // 10)  # leicht steigender Puls
            points.append(TrackPoint(lat=lat, lon=lon, ele=ele, time=t, hr=hr))

        v_power, coaching = estimate_virtual_power_and_coaching(
            points=points,
            total_system_weight_kg=72.0,
            rider_weight_kg=60.0,
        )

        self.assertEqual(v_power["status"], "calculated")
        self.assertGreater(v_power["overall_avg_watts"], 50.0)
        self.assertGreater(v_power["overall_watts_per_kg"], 1.0)
        self.assertIsNotNone(v_power["climb_avg_watts"])
        self.assertIn("cardiac_drift_pct", coaching)


class TestWeatherAerodynamics(unittest.TestCase):
    def test_air_density_calculation(self):
        from weather_service import calculate_air_density

        # Standard-Bedingungen (15°C, 1013.25 hPa) -> ~1.225 kg/m³
        rho_std = calculate_air_density(1013.25, 15.0)
        self.assertAlmostEqual(rho_std, 1.225, places=2)

        # Sommerhitze (30°C) -> dünnere Luft
        rho_hot = calculate_air_density(1013.25, 30.0)
        self.assertLess(rho_hot, rho_std)

        # Winterkälte (0°C) -> dichtere Luft
        rho_cold = calculate_air_density(1013.25, 0.0)
        self.assertGreater(rho_cold, rho_std)

    def test_deg_to_compass(self):
        from weather_service import deg_to_compass

        self.assertEqual(deg_to_compass(0), "N")
        self.assertEqual(deg_to_compass(90), "E")
        self.assertEqual(deg_to_compass(180), "S")
        self.assertEqual(deg_to_compass(270), "W")
        self.assertEqual(deg_to_compass(360), "N")

    def test_blend_wind_directions(self):
        from weather_service import blend_wind_directions

        # 350° und 10° sollten über 0° gemittelt 360° bzw. 0° ergeben
        blended = blend_wind_directions(350.0, 1.0, 10.0, 1.0)
        self.assertIn(blended, [0, 360])

        # Ungleiche Gewichtung (350° mit 0.8, 10° mit 0.2)
        blended_weighted = blend_wind_directions(350.0, 0.8, 10.0, 0.2)
        self.assertAlmostEqual(blended_weighted, 354, delta=2)

    def test_fuse_weather_sources(self):
        from weather_service import fuse_weather_sources

        dwd = {
            "source": "Bright Sky (DWD)",
            "station_name": "Freiburg",
            "station_id": "01443",
            "distance_km": 4.5,
            "temperature_c": 16.0,
            "pressure_hpa": 1015.0,
            "humidity_pct": 50,
            "wind_speed_kmh": 20.0,
            "wind_deg": 180,
            "wind_gust_kmh": 32.0,
            "condition": "dry",
        }
        owm = {
            "source": "OpenWeatherMap",
            "temperature_c": 14.0,
            "pressure_hpa": 1013.0,
            "humidity_pct": 60,
            "wind_speed_kmh": 10.0,
            "wind_deg": 180,
            "condition": "Clouds",
            "feels_like_c": 13.5,
        }

        # Bei 4.5 km Distanz muss DWD 75% Gewichtung erhalten
        fused = fuse_weather_sources(dwd, owm, lat=48.0, lon=7.85)
        self.assertIsNotNone(fused)
        self.assertIn("Multi-Source Ensemble", fused["source"])
        self.assertEqual(fused["wind_gust_kmh"], 32.0)
        # 16.0 * 0.75 + 14.0 * 0.25 = 15.5
        self.assertAlmostEqual(fused["temperature_c"], 15.5, places=1)
        self.assertIn("comparison", fused)
        self.assertEqual(fused["comparison"]["delta_temperature_c"], 2.0)

    def test_bearing_calculation(self):
        from analyze_gpx import calculate_bearing_deg

        # Fahrt nach Norden
        bearing_n = calculate_bearing_deg(48.0, 8.0, 48.01, 8.0)
        self.assertAlmostEqual(bearing_n, 0.0, delta=1.0)

        # Fahrt nach Osten
        bearing_e = calculate_bearing_deg(48.0, 8.0, 48.0, 8.01)
        self.assertAlmostEqual(bearing_e, 90.0, delta=2.0)

    def test_headwind_increases_power(self):
        from datetime import datetime, timezone, timedelta
        from analyze_gpx import TrackPoint, estimate_virtual_power_and_coaching

        points = []
        base_time = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)
        # 10 Minuten flach nach Norden mit 30 km/h (~8.33 m/s)
        lat = 48.0
        lon = 8.0
        for i in range(60):
            t = base_time + timedelta(seconds=i * 10)
            lat += 0.00075  # ~83m pro 10s = 8.3 m/s = 30 km/h
            points.append(TrackPoint(lat=lat, lon=lon, ele=200.0, time=t, hr=140))

        # 1. Ohne Wind (Windstille)
        p_calm, _ = estimate_virtual_power_and_coaching(
            points=points,
            total_system_weight_kg=72.9,
            rider_weight_kg=60.0,
            weather_data={"wind_speed_ms": 0.0, "wind_deg": 0, "air_density_kg_m3": 1.225},
        )

        # 2. Mit starkem Gegenwind aus Norden (Windrichtung 0° direkt entgegen)
        p_headwind, _ = estimate_virtual_power_and_coaching(
            points=points,
            total_system_weight_kg=72.9,
            rider_weight_kg=60.0,
            weather_data={"wind_speed_ms": 5.55, "wind_deg": 0, "air_density_kg_m3": 1.225},  # 20 km/h Gegenwind
        )

        # Gegenwind muss signifikant höhere Wattzahl fordern als Windstille
        self.assertGreater(
            p_headwind["overall_avg_watts"],
            p_calm["overall_avg_watts"] + 50.0,
        )
        self.assertIn("weather_aerodynamics", p_headwind)
        self.assertTrue(p_headwind["weather_aerodynamics"]["weather_integrated"])


class TestSportsScience(unittest.TestCase):
    def test_metabolic_and_nutrition_calculation(self):
        from sports_science import calculate_metabolic_and_nutrition
        from analyze_gpx import HRZoneInfo

        # 2 Stunden Fahrt bei 1000 kJ Arbeit (~140W Durchschnittsleistung)
        zones = [
            HRZoneInfo(1, "Z1", 0, 123, duration_sec=1200),
            HRZoneInfo(2, "Z2", 123, 144, duration_sec=3600),
            HRZoneInfo(3, "Z3", 144, 164, duration_sec=1800),
            HRZoneInfo(4, "Z4", 164, 185, duration_sec=600),
            HRZoneInfo(5, "Z5", 185, 205, duration_sec=0),
        ]
        res = calculate_metabolic_and_nutrition(
            work_kj=1000.0,
            duration_s=7200.0,
            zones=zones,
            avg_hr=145.0,
            hr_max=205,
            temp_c=22.0,
        )

        self.assertIn("energy_total_kcal", res)
        self.assertGreater(res["energy_total_kcal"], 900.0)
        carbs = res["carbohydrates"]
        self.assertGreater(carbs["burned_grams"], 50.0)
        self.assertGreater(carbs["burn_rate_g_per_hour"], 20.0)
        self.assertIn("hungerast_bonk_risk", carbs)
        self.assertIn("fueling_target_during_ride", carbs)

        fat = res["fat"]
        self.assertGreater(fat["burned_grams"], 20.0)

        hyd = res["hydration"]
        self.assertGreater(hyd["estimated_sweat_loss_liters"], 0.8)
        self.assertGreater(hyd["sweat_rate_l_per_hour"], 0.4)

    def test_vam_and_youth_diagnostics(self):
        from sports_science import calculate_vam_and_youth_diagnostics

        # 600 Höhenmeter in 30 Minuten (1800s) -> VAM = 1200 m/h
        res = calculate_vam_and_youth_diagnostics(
            climb_elevation_gain_m=600.0,
            climb_duration_sec=1800.0,
            climb_avg_watts=280.0,
            rider_weight_kg=60.0,
        )

        self.assertTrue(res["vam_available"])
        self.assertEqual(res["vam_m_per_hour"], 1200.0)
        self.assertAlmostEqual(res["climb_watts_per_kg"], 4.67, places=2)
        bench = res["youth_u17_benchmark"]
        self.assertIn("category_title", bench)
        self.assertIn("diagnostic_summary", bench)


class TestGeoEnhancements(unittest.TestCase):
    def test_query_osm_route_surface_and_cache(self):
        from geo_enhancements import query_osm_route_surface
        from analyze_gpx import TrackPoint

        pts = [
            TrackPoint(lat=48.000, lon=7.850),
            TrackPoint(lat=48.005, lon=7.855),
            TrackPoint(lat=48.010, lon=7.860),
        ]
        res = query_osm_route_surface(pts)
        self.assertIn("weighted_crr", res)
        self.assertGreater(res["weighted_crr"], 0.003)
        self.assertIn("surface_distribution", res)
        self.assertIn("forest_shelter_pct", res)

    def test_verify_elevation_dem(self):
        from geo_enhancements import verify_elevation_dem
        from analyze_gpx import TrackPoint

        pts = [
            TrackPoint(lat=48.000, lon=7.850, ele=250.0),
            TrackPoint(lat=48.005, lon=7.855, ele=260.0),
            TrackPoint(lat=48.010, lon=7.860, ele=270.0),
            TrackPoint(lat=48.015, lon=7.865, ele=280.0),
            TrackPoint(lat=48.020, lon=7.870, ele=290.0),
        ]
        res = verify_elevation_dem(pts)
        self.assertIn("dem_verified", res)


class TestFullDiagnosticsPipeline(unittest.TestCase):
    def test_calculate_metrics_with_surface_and_dem(self):
        from datetime import datetime, timezone, timedelta
        from pathlib import Path
        from analyze_gpx import TrackPoint, calculate_metrics

        points = []
        base_time = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)
        lat = 48.0
        lon = 8.0
        ele = 200.0

        for i in range(180):  # 180 Punkte á 10s = 1800s (30 Min)
            t = base_time + timedelta(seconds=i * 10)
            lat += 0.0005
            ele += 1.5  # Stetiger Anstieg
            hr = 155 + (i % 15)
            points.append(TrackPoint(lat=lat, lon=lon, ele=ele, time=t, hr=hr))

        surface_data = {
            "surface_detected": True,
            "primary_surface": "gravel",
            "weighted_crr": 0.0095,
            "surface_distribution": {"gravel": 80.0, "asphalt": 20.0},
            "forest_shelter_pct": 35.0,
        }
        dem_data = {
            "dem_verified": True,
            "satellite_elevation_gain_m": 265.0,
            "sampled_gpx_gain_m": 270.0,
            "elevation_variance_pct": 1.9,
            "barometric_drift_detected": False,
            "elevation_quality": "Exzellent (Satelliten-bestätigt)",
        }
        weather_data = {
            "temperature_c": 19.5,
            "wind_speed_ms": 3.5,
            "wind_deg": 180.0,
            "air_density_kg_m3": 1.220,
        }

        metrics = calculate_metrics(
            file_path=Path("dummy_tour.gpx"),
            title="Gravel Bergtest",
            activity_type="GravelRide",
            points=points,
            hr_max=205,
            hr_rest=52,
            weather_data=weather_data,
            surface_data=surface_data,
            dem_data=dem_data,
        )

        self.assertEqual(metrics.virtual_power["rolling_resistance_crr"], 0.0095)
        self.assertIn("nutrition_metabolism", dir(metrics))
        self.assertGreater(metrics.nutrition_metabolism["energy_total_kcal"], 100)
        self.assertGreater(metrics.nutrition_metabolism["carbohydrates"]["burned_grams"], 10)
        self.assertTrue(metrics.vam_diagnostics["vam_available"])
        self.assertGreater(metrics.vam_diagnostics["vam_m_per_hour"], 100)
        self.assertEqual(metrics.surface_analysis["primary_surface"], "gravel")
        self.assertTrue(metrics.dem_verification["dem_verified"])


class TestAppleHealthIntegration(unittest.TestCase):
    def test_calculate_youth_readiness(self):
        from apple_health_service import calculate_youth_readiness

        # Top Erholung
        score_top, verdict_top = calculate_youth_readiness(
            sleep_hours=8.8,
            deep_sleep_minutes=85,
            resting_hr=49,
            hrv_ms=92.0,
            baseline_resting_hr=52,
        )
        self.assertGreaterEqual(score_top, 85.0)
        self.assertIn("Hervorragender Schlaf", verdict_top)

        # Schlechte Erholung (Schlafmangel, hoher Ruhepuls, niedrige HRV)
        score_low, verdict_low = calculate_youth_readiness(
            sleep_hours=4.5,
            deep_sleep_minutes=20,
            resting_hr=70,
            hrv_ms=28.0,
            baseline_resting_hr=59,
        )
        self.assertLess(score_low, 55.0)

    def test_offline_sync_handling(self):
        from apple_health_service import sync_apple_health_from_ios

        # Wenn Handy-Server offline ist, soll kein Crash passieren, sondern sauberer Fallback
        res = sync_apple_health_from_ios(force=False)
        self.assertIn("server_online", res)
        self.assertIn("status", res)


class TestRouteSimulator(unittest.TestCase):
    def test_simulate_planned_route(self):
        from pathlib import Path
        from route_simulator import simulate_planned_route

        # Test mit vorhandener GPX-Datei
        gpx_file = "downloads/19878586372_Fahrt_am_Morgen.gpx"
        if Path(gpx_file).exists():
            sim = simulate_planned_route(
                gpx_file_path=gpx_file,
                target_intensity="endurance_ga1",
                rider_weight_kg=60.0,
            )
            self.assertEqual(sim["status"], "success")
            self.assertIn("key_metrics", sim)
            self.assertGreater(sim["key_metrics"]["estimated_avg_speed_kmh"], 15.0)
            self.assertIn("nutrition_and_fueling_strategy", sim)
            self.assertIn("coaching_tactics", sim)



class TestTokenAuthMiddleware(unittest.TestCase):
    def setUp(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from mcp_server import TokenAuthMiddleware

        self.token = "test-secret-token-123"

        async def sse_mock(request):
            data = "event: endpoint\r\ndata: /messages/?session_id=abcd-1234\r\n\r\n"
            return PlainTextResponse(data, media_type="text/event-stream")

        async def messages_mock(request):
            return JSONResponse({"status": "received"})

        async def health_mock(request):
            return JSONResponse({"status": "healthy"})

        async def root_mock(request):
            return JSONResponse({"status": "online"})

        base_app = Starlette(
            routes=[
                Route("/", root_mock, methods=["GET"]),
                Route("/", messages_mock, methods=["POST"]),
                Route("/health", health_mock, methods=["GET"]),
                Route("/sse", sse_mock, methods=["GET"]),
                Route("/messages/", messages_mock, methods=["POST"]),
                Route("/mcp", messages_mock, methods=["GET", "POST"]),
            ]
        )

        self.app = TokenAuthMiddleware(base_app, token=self.token)
        self.client = TestClient(self.app)

    def test_public_routes_allowed_without_auth(self):
        res_root = self.client.get("/")
        self.assertEqual(res_root.status_code, 200)
        self.assertEqual(res_root.json()["status"], "online")

        res_health = self.client.get("/health")
        self.assertEqual(res_health.status_code, 200)
        self.assertEqual(res_health.json()["status"], "healthy")

    def test_unauthorized_access_rejected(self):
        res = self.client.get("/sse")
        self.assertEqual(res.status_code, 401)
        self.assertIn("error", res.json())
        self.assertEqual(res.headers.get("www-authenticate"), 'Bearer realm="strava-mcp"')

    def test_bearer_token_authentication(self):
        res = self.client.get("/sse", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(res.status_code, 200)

    def test_x_api_key_authentication(self):
        res = self.client.get("/sse", headers={"x-api-key": self.token})
        self.assertEqual(res.status_code, 200)

    def test_url_query_token_authentication_and_endpoint_rewrite(self):
        res = self.client.get(f"/sse?token={self.token}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f"token={self.token}", res.text)
        self.assertIn("session_id=abcd-1234", res.text)

    def test_automatic_session_authorization_for_post_messages(self):
        # 1. Handshake über /sse registriert Session abcd-1234
        res_sse = self.client.get(f"/sse?token={self.token}")
        self.assertEqual(res_sse.status_code, 200)

        # 2. Folgerequest an /messages/?session_id=abcd-1234 ohne Header wird durchgelassen
        res_post = self.client.post("/messages/?session_id=abcd-1234", json={"test": True})
        self.assertEqual(res_post.status_code, 200)

    def test_unauthorized_post_messages_rejected(self):
        res = self.client.post("/messages/?session_id=unknown-session", json={"test": True})
        self.assertEqual(res.status_code, 401)

    def test_cors_options_preflight(self):
        res = self.client.options("/sse")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("access-control-allow-origin"), "*")
        self.assertIn("POST", res.headers.get("access-control-allow-methods", ""))

    def test_streamable_http_auth_required(self):
        # Unauthenticated POST /mcp -> 401
        res = self.client.post("/mcp", json={"method": "initialize"})
        self.assertEqual(res.status_code, 401)

        # Authenticated POST /mcp -> 200
        res_auth = self.client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"method": "initialize"},
        )
        self.assertEqual(res_auth.status_code, 200)

    def test_streamable_http_root_post_auth(self):
        # Unauthenticated POST / -> 401
        res = self.client.post("/", json={"method": "initialize"})
        self.assertEqual(res.status_code, 401)

        # Authenticated POST / -> 200
        res_auth = self.client.post(
            "/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"method": "initialize"},
        )
        self.assertEqual(res_auth.status_code, 200)


class TestRefactoredCoachingAndTimestamps(unittest.TestCase):
    def test_list_local_gpx_files_timestamps(self):
        from mcp_server import list_local_gpx_files
        res = list_local_gpx_files()
        self.assertEqual(res["status"], "success")
        if res["files"]:
            first = res["files"][0]
            self.assertIn("activity_start_time", first)
            self.assertIn("file_modified_at", first)
            self.assertIn("last_modified", first)
            self.assertIn("last_modified_timestamp", first)
            # Ensure file_modified_at is valid ISO string (contains 'T')
            self.assertIn("T", first["file_modified_at"])

    def test_get_training_status_structured_directives(self):
        from training_load import TrainingLoadEngine
        engine = TrainingLoadEngine(profile={"rider_weight_kg": 62.0, "bike_weight_kg": 8.5})
        status = engine.get_training_status()
        self.assertEqual(status["status"], "success")

        # Check structured athlete profile
        self.assertIn("athlete_profile", status)
        prof = status["athlete_profile"]
        self.assertEqual(prof["rider_weight_kg"], 62.0)
        self.assertIn("hr_max", prof)

        # Check structured coaching directives
        self.assertIn("coaching_directives", status)
        directives = status["coaching_directives"]
        self.assertIn("coaching_style", directives)
        self.assertIn("focus_areas", directives)
        self.assertIsInstance(directives["focus_areas"], list)

        # Ensure no TRAINER-DIREKTIVE prompt-injection string in status
        status_str = str(status)
        self.assertNotIn("TRAINER-DIREKTIVE", status_str)

    def test_multi_user_data_isolation(self):
        from rag_engine import ActivityVectorStore
        store = ActivityVectorStore()

        # Add notes for two distinct users
        store.add_coaching_note("Notiz User Alpha", category="pacing", user_id="user_alpha")
        store.add_coaching_note("Notiz User Beta", category="pacing", user_id="user_beta")

        notes_alpha = store.get_coaching_notes(user_id="user_alpha")
        notes_beta = store.get_coaching_notes(user_id="user_beta")

        alpha_texts = [n["note"] for n in notes_alpha]
        beta_texts = [n["note"] for n in notes_beta]

        self.assertIn("Notiz User Alpha", alpha_texts)
        self.assertNotIn("Notiz User Alpha", beta_texts)
        self.assertIn("Notiz User Beta", beta_texts)
        self.assertNotIn("Notiz User Beta", alpha_texts)

    def test_download_and_analyze_activity_local_file(self):
        import shutil
        from pathlib import Path
        from mcp_server import _sync_download_and_analyze_activity
        from config import DOWNLOADS_DIR, BASE_DIR

        demo_src = BASE_DIR / "demo" / "demo_100km_ride.gpx"
        test_dst = DOWNLOADS_DIR / "20281742429_demo_ride.gpx"
        created_file = False
        if not list(DOWNLOADS_DIR.glob("*20281742429*.gpx")) and demo_src.exists():
            shutil.copy(demo_src, test_dst)
            created_file = True

        try:
            res = _sync_download_and_analyze_activity("20281742429")
            self.assertEqual(res["status"], "success")
            self.assertIn("athlete_profile", res)
            self.assertIn("coaching_directives", res)
            self.assertIn("rider_weight_kg", res["athlete_profile"])
            self.assertIn("system_weight_kg", res["athlete_profile"])
            self.assertNotIn("TRAINER-DIREKTIVE", str(res))
        finally:
            if created_file and test_dst.exists():
                try:
                    test_dst.unlink()
                except Exception:
                    pass

    def test_dashboard_local_access_and_remote_restriction(self):
        from starlette.testclient import TestClient
        from mcp_server import starlette_app

        client = TestClient(starlette_app)

        # 1. Lokaler Zugriff: 200 OK und Dashboard-HTML
        res_local = client.get("/dashboard")
        self.assertEqual(res_local.status_code, 200)
        self.assertIn("OPERATIONS LEADERBOARD", res_local.text)

        # 1b. Lokaler Zugriff mit Trailing Slash: 200 OK
        res_local_slash = client.get("/dashboard/")
        self.assertEqual(res_local_slash.status_code, 200)
        self.assertIn("OPERATIONS LEADERBOARD", res_local_slash.text)

        # 2. Zugriff über Cloudflare (CF-Connecting-IP): 403 Forbidden
        res_cf = client.get("/dashboard", headers={"cf-connecting-ip": "203.0.113.1"})
        self.assertEqual(res_cf.status_code, 403)

        # 3. Zugriff über externe Domain (Host: ewigetomate.de): 403 Forbidden
        res_ext = client.get("/dashboard", headers={"host": "mcp-strava.ewigetomate.de"})
        self.assertEqual(res_ext.status_code, 403)

        # 4. Lokale JSON-API: 200 OK
        res_api = client.get("/api/dashboard-stats")
        self.assertEqual(res_api.status_code, 200)
        self.assertEqual(res_api.json()["status"], "ONLINE")

        # 4b. Lokale JSON-API mit Trailing Slash: 200 OK
        res_api_slash = client.get("/api/dashboard-stats/")
        self.assertEqual(res_api_slash.status_code, 200)
        self.assertEqual(res_api_slash.json()["status"], "ONLINE")


class TestIndoorActivitiesSupport(unittest.TestCase):
    """Tests für Indoor-Aktivitäten ohne GPS-Signal (TCX & JSON-Streams)."""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_and_analyze_tcx_indoor_run(self):
        from analyze_gpx import calculate_metrics, parse_gpx_file

        tcx_content = """<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Id>2026-09-26T10:00:00.000Z</Id>
      <Notes>Laufband Intervalltraining</Notes>
      <Lap StartTime="2026-09-26T10:00:00.000Z">
        <TotalTimeSeconds>120.0</TotalTimeSeconds>
        <DistanceMeters>400.0</DistanceMeters>
        <Track>
          <Trackpoint>
            <Time>2026-09-26T10:00:00Z</Time>
            <DistanceMeters>0.0</DistanceMeters>
            <HeartRateBpm><Value>130</Value></HeartRateBpm>
            <Cadence>82</Cadence>
          </Trackpoint>
          <Trackpoint>
            <Time>2026-09-26T10:01:00Z</Time>
            <DistanceMeters>200.0</DistanceMeters>
            <HeartRateBpm><Value>155</Value></HeartRateBpm>
            <Cadence>86</Cadence>
          </Trackpoint>
          <Trackpoint>
            <Time>2026-09-26T10:02:00Z</Time>
            <DistanceMeters>400.0</DistanceMeters>
            <HeartRateBpm><Value>172</Value></HeartRateBpm>
            <Cadence>88</Cadence>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""
        tcx_file = self.temp_path / "123456789_Laufband_Intervall.tcx"
        tcx_file.write_text(tcx_content, encoding="utf-8")

        title, act_type, points = parse_gpx_file(tcx_file)
        self.assertEqual(act_type, "Running")
        self.assertIn("Laufband Intervall", title)
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].hr, 130)
        self.assertEqual(points[1].distance_m, 200.0)

        metrics = calculate_metrics(tcx_file, title, act_type, points, hr_max=205, hr_rest=50)
        self.assertAlmostEqual(metrics.total_distance_km, 0.4, places=2)
        self.assertEqual(metrics.moving_time_sec, 120.0)
        self.assertTrue(metrics.hr_available)
        self.assertAlmostEqual(metrics.avg_hr, 152.3, places=1)
        self.assertGreater(metrics.suffer_score, 0.0)
        self.assertIn("energy_total_kcal", metrics.nutrition_metabolism)
        self.assertGreater(metrics.nutrition_metabolism["energy_total_kcal"], 0.0)

    def test_parse_and_analyze_json_streams(self):
        import json
        from analyze_gpx import calculate_metrics, parse_gpx_file

        json_data = {
            "activity_id": "987654321",
            "title": "Ergometer HIIT Session",
            "sport_type": "VirtualRide",
            "start_time": "2026-09-26T11:00:00Z",
            "streams": {
                "time": [0, 60, 120, 180],
                "heartrate": [120, 150, 175, 160],
                "distance": [0.0, 450.0, 950.0, 1400.0],
                "cadence": [80, 90, 105, 85],
                "watts": [120, 200, 310, 180],
            },
        }
        json_file = self.temp_path / "987654321_Ergometer_HIIT.json"
        json_file.write_text(json.dumps(json_data), encoding="utf-8")

        title, act_type, points = parse_gpx_file(json_file)
        self.assertEqual(act_type, "VirtualRide")
        self.assertEqual(len(points), 4)
        self.assertEqual(points[2].hr, 175)
        self.assertEqual(points[2].watts, 310.0)

        metrics = calculate_metrics(json_file, title, act_type, points, hr_max=205, hr_rest=52)
        self.assertAlmostEqual(metrics.total_distance_km, 1.4, places=2)
        self.assertEqual(metrics.moving_time_sec, 180.0)
        self.assertTrue(metrics.hr_available)
        self.assertGreater(metrics.suffer_score, 0.0)

    def test_indoor_tcx_fallback_in_sync_gpx(self):
        from unittest.mock import MagicMock
        from sync_gpx import download_activity_gpx

        mock_context = MagicMock()

        def mock_get(url):
            resp = MagicMock()
            if "export_gpx" in url:
                resp.status = 404
            elif "export_tcx" in url:
                resp.status = 200
                resp.body.return_value = b"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Id>2026-09-26T08:00:00Z</Id>
      <Notes>Morgenlauf Indoor</Notes>
    </Activity>
  </Activities>
</TrainingCenterDatabase>"""
            else:
                resp.status = 404
            return resp

        mock_context.request.get.side_effect = mock_get
        logger = MagicMock()

        res_path = download_activity_gpx(mock_context, "5555555555", self.temp_path, logger)
        self.assertIsNotNone(res_path)
        self.assertTrue(res_path.exists())
        self.assertTrue(res_path.name.endswith(".tcx"))
        self.assertIn("5555555555", res_path.name)


class TestV2ServerEnhancements(unittest.TestCase):
    """
    Umfassende Tests für die neuen V2-Features:
    - Apple-Health-Konfidenzbewertung & Cache-Staleness
    - DEM-Höhenreconciliation & Anomalie-Erkennung (z.B. Mittagslauf-Pausen)
    - Präzise Sportarten-Filterung & Verteilung
    - Entfernung von Coaching-Injection-Strings & MCP Prompt
    - Recovery-Loop (get_holistic_recovery_status)
    - Segment-Longitudinalanalyse (analyze_segment_history)
    - Zielbasierte Periodisierung (plan_ctl_periodization)
    - Proaktive Trainings-Alerts (get_training_alerts)
    """

    def setUp(self):
        import tempfile
        import shutil
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_apple_health_confidence_and_staleness(self):
        from apple_health_service import calculate_youth_readiness

        # 1. Alle Signale vorhanden -> Hohe Konfidenz
        res_full = calculate_youth_readiness(
            sleep_hours=8.5,
            deep_sleep_minutes=90.0,
            resting_hr=50,
            hrv_ms=65.0,
            baseline_resting_hr=52,
            baseline_hrv_ms=60.0,
        )
        # Unpacking als 2-Tuple testen (Rückwärtskompatibilität)
        score, verdict = res_full
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(verdict, str)
        self.assertEqual(res_full.confidence, "high")
        self.assertEqual(res_full.confidence_score, 1.0)
        self.assertIsNone(res_full.confidence_warning)
        self.assertEqual(len(res_full.signals_available), 3)

        # 2. Nur Schlafdaten vorhanden (RHR & HRV fehlen wie in Claudes Kritik am 23.9.)
        res_partial = calculate_youth_readiness(
            sleep_hours=7.5,
            deep_sleep_minutes=None,
            resting_hr=None,
            hrv_ms=None,
        )
        self.assertEqual(res_partial.confidence, "low")
        self.assertLess(res_partial.confidence_score, 0.5)
        self.assertIsNotNone(res_partial.confidence_warning)
        self.assertIn("resting_heart_rate", res_partial.signals_missing)
        self.assertIn("heart_rate_variability", res_partial.signals_missing)
        self.assertIn("Niedrige Konfidenz", res_partial.confidence_warning)

    def test_activity_anomalies_detection(self):
        from analyze_gpx import ActivityMetrics, detect_activity_anomalies, TrackPoint
        from datetime import datetime, timezone, timedelta

        # Simuliere Mittagslauf-Szenario: 1.96 km, 2910s elapsed, 1122s moving (>60% Pause)
        base_time = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        pts = [
            TrackPoint(lat=48.0, lon=7.8, ele=250.0, time=base_time),
            TrackPoint(lat=48.01, lon=7.81, ele=250.0, time=base_time + timedelta(seconds=2910)),
        ]

        metrics = ActivityMetrics(
            file_path=Path("dummy_lauf.gpx"),
            title="Mittagslauf",
            activity_type="Run",
            total_points=2,
            total_distance_km=1.96,
            elevation_gain_m=10.0,
            elevation_loss_m=10.0,
            elapsed_time_sec=2910.0,
            moving_time_sec=1122.0,
            paused_time_sec=1788.0,
            avg_speed_moving_kmh=6.28,
            max_speed_kmh=12.0,
        )

        anomalies = detect_activity_anomalies(metrics, pts)
        self.assertGreater(len(anomalies), 0)
        pause_anomaly = next((a for a in anomalies if a["type"] == "extended_pause_detected"), None)
        self.assertIsNotNone(pause_anomaly)
        self.assertEqual(pause_anomaly["severity"], "warning")
        self.assertIn("Pausenzeit", pause_anomaly["description"])
        self.assertIn("Nettozeit", pause_anomaly["description"])

    def test_dem_elevation_reconciliation(self):
        import json
        from geo_enhancements import verify_elevation_dem, apply_dem_elevation_correction
        from analyze_gpx import TrackPoint
        from unittest.mock import patch, MagicMock

        pts = [
            TrackPoint(lat=48.0, lon=7.8, ele=100.0),
            TrackPoint(lat=48.005, lon=7.805, ele=110.0),
            TrackPoint(lat=48.01, lon=7.81, ele=120.0),
            TrackPoint(lat=48.015, lon=7.815, ele=140.0),
            TrackPoint(lat=48.02, lon=7.82, ele=177.0),
        ]

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"elevation": [100.0, 130.0, 160.0, 200.0, 231.0]}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            dem_result = verify_elevation_dem(pts)
            self.assertTrue(dem_result["reconciliation_recommended"])
            self.assertEqual(dem_result["elevation_source_recommended"], "dem_satellite")
            self.assertAlmostEqual(dem_result["reconciled_elevation_gain_m"], 131.0, places=1)

            # Prüfe kontinuierliche Interpolation über alle Punkte
            corrected_pts = apply_dem_elevation_correction(pts, dem_result)
            self.assertEqual(len(corrected_pts), 5)
            self.assertAlmostEqual(corrected_pts[0], 100.0, places=1)
            self.assertAlmostEqual(corrected_pts[-1], 231.0, places=1)

    def test_sport_filtering_and_distribution(self):
        from mcp_server import matches_sport, list_local_gpx_files

        # 1. Deutsche Begriffe matchen präzise
        self.assertTrue(matches_sport("Lauf", "run"))
        self.assertTrue(matches_sport("Laufen", "run"))
        self.assertTrue(matches_sport("Radfahrt", "ride"))
        self.assertTrue(matches_sport("Schotterfahrt", "gravel"))
        self.assertTrue(matches_sport("Ergometer", "indoor"))
        self.assertTrue(matches_sport("Laufband", "indoor"))
        self.assertFalse(matches_sport("Lauf", "ride"))

        # 2. Lokale Dateien filtern & Sport-Verteilung
        run_file = self.temp_path / "11111_Mittagslauf.gpx"
        run_file.write_text("<gpx><trk><name>Mittagslauf</name><type>Run</type><trkseg></trkseg></trk></gpx>", encoding="utf-8")

        ride_file = self.temp_path / "22222_Abendradfahrt.gpx"
        ride_file.write_text("<gpx><trk><name>Abendradfahrt</name><type>Ride</type><trkseg></trkseg></trk></gpx>", encoding="utf-8")

        indoor_file = self.temp_path / "33333_Ergometer_Workout.json"
        indoor_file.write_text('{"title": "Ergometer Workout", "sport_type": "VirtualRide", "is_indoor": true}', encoding="utf-8")

        from unittest.mock import patch
        with patch("mcp_server.get_user_downloads_dir", return_value=self.temp_path):
            # Nur Läufe abfragen
            res_runs = list_local_gpx_files(sport_type="Run")
            self.assertEqual(res_runs["status"], "success")
            self.assertEqual(res_runs["count"], 1)
            self.assertEqual(res_runs["files"][0]["activity_id"], "11111")
            self.assertIn("Run", res_runs["sport_distribution"])

            # Nur Indoor abfragen
            res_indoor = list_local_gpx_files(indoor_only=True)
            self.assertEqual(res_indoor["count"], 1)
            self.assertEqual(res_indoor["files"][0]["activity_id"], "33333")

            # Alle abfragen
            res_all = list_local_gpx_files()
            self.assertEqual(res_all["count"], 3)
            self.assertEqual(res_all["total_local_files"], 3)

    def test_athlete_profile_and_mcp_prompt(self):
        from mcp_server import get_athlete_profile, cycling_coach_prompt

        profile_data = get_athlete_profile()
        self.assertEqual(profile_data["status"], "success")
        self.assertIn("athlete_profile", profile_data)
        self.assertIn("coaching_directives", profile_data)

        # Sicherheit: Keine Roh-Anweisung mehr als Daten-Payload
        self.assertNotIn("coaching_instruction_for_llm", profile_data)

        # MCP Prompt prüfen
        prompt_text = cycling_coach_prompt("Max Mustermann")
        self.assertIn("Max Mustermann", prompt_text)
        self.assertIn("Headcoach", prompt_text)
        self.assertIn("Banister/Coggan", prompt_text)
        self.assertIn("Ramp Rate", prompt_text)

    def test_holistic_recovery_status(self):
        from training_load import compute_holistic_recovery
        from unittest.mock import patch

        mock_health = {
            "readiness_score": 85.0,
            "resting_heart_rate": 48.0,
            "heart_rate_variability_sdnn": 72.0,
            "sleep_duration_hours": 8.2,
            "confidence": "high",
            "is_stale": False,
            "cache_age_days": 0,
        }

        with patch("apple_health_service.sync_apple_health_from_ios", return_value=mock_health):
            res = compute_holistic_recovery(user_id="default")
            self.assertEqual(res["status"], "success")
            self.assertIn("holistic_recovery_score", res)
            self.assertIn("verdict", res)
            self.assertIn("permitted_training_zones", res)
            self.assertIn("cross_domain_correlations", res)
            self.assertIsInstance(res["holistic_recovery_score"], int)
            self.assertGreaterEqual(res["holistic_recovery_score"], 0)
            self.assertLessEqual(res["holistic_recovery_score"], 100)

    def test_segment_history_longitudinal(self):
        from rag_engine import ActivityVectorStore

        store = ActivityVectorStore()
        # Segment-Efforts speichern
        effort1 = {
            "name": "Schauinsland West",
            "distance_m": 11500.0,
            "elevation_gain_m": 720.0,
            "average_grade_pct": 6.3,
            "elapsed_time_s": 2400,
            "avg_speed_kmh": 17.25,
            "avg_watts": 260.0,
            "avg_hr": 168.0,
            "pr_rank": 2,
        }
        effort2 = {
            "name": "Schauinsland West",
            "distance_m": 11500.0,
            "elevation_gain_m": 720.0,
            "average_grade_pct": 6.3,
            "elapsed_time_s": 2280,
            "avg_speed_kmh": 18.15,
            "avg_watts": 280.0,
            "avg_hr": 170.0,
            "pr_rank": 1,
        }

        store.upsert_activity(
            activity_id="test_act_101",
            name="Schauinsland Effort 1",
            sport_type="Ride",
            activity_date="2026-08-15",
            distance_km=30.0,
            elevation_m=800.0,
            moving_time_s=3600,
            segments=[effort1],
            user_id="test_user",
        )
        store.upsert_activity(
            activity_id="test_act_102",
            name="Schauinsland Effort 2",
            sport_type="Ride",
            activity_date="2026-09-15",
            distance_km=30.0,
            elevation_m=800.0,
            moving_time_s=3400,
            segments=[effort2],
            user_id="test_user",
        )

        history = store.get_segment_history("Schauinsland", max_efforts=10, user_id="test_user")
        self.assertEqual(history["status"], "success")
        self.assertEqual(history["efforts_count"], 2)
        self.assertIsNotNone(history["personal_record"])
        self.assertEqual(history["personal_record"]["elapsed_time_s"], 2280)
        self.assertEqual(history["trends"]["time_improvement_s"], 120)
        self.assertEqual(history["trends"]["watts_delta"], 20.0)

    def test_plan_ctl_periodization(self):
        from training_load import plan_ctl_periodization
        from datetime import date, timedelta

        # Zieldatum 10 Wochen in der Zukunft
        target_date_str = (date.today() + timedelta(weeks=10)).isoformat()
        res = plan_ctl_periodization(
            target_date_str=target_date_str,
            target_ctl=85.0,
            event_type="marathon",
            current_ctl=45.0,
            weekly_hours_budget=10.0,
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["weeks_to_target"], 10)
        self.assertEqual(len(res["periodization_plan"]), 10)

        overview = res["periodization_overview"]
        self.assertIn("required_weekly_ramp_rate", overview)
        self.assertIn("projected_race_day_tsb_form", overview)
        # Am Wettkampftag sollte TSB positiv sein (Superkompensation)
        self.assertGreater(overview["projected_race_day_tsb_form"], 5.0)

        # Deload-Woche prüfen
        deload_week = res["periodization_plan"][2] # Woche 3 (w % 3 == 0)
        self.assertTrue(deload_week["is_recovery_week"])
        self.assertIn("Regenerationswoche", deload_week["phase"])

    def test_proactive_alerts(self):
        from training_load import evaluate_training_alerts
        from rag_engine import ActivityVectorStore
        from unittest.mock import MagicMock

        mock_engine = MagicMock()
        mock_engine.get_training_status.return_value = {
            "metrics": {
                "ctl_fitness": 50.0,
                "atl_fatigue": 85.0,
                "tsb_form": -35.0,  # Akute Überlastung
                "ramp_rate_7d": 7.5, # Zu steile Ramp Rate
                "form_zone": "Kritische Überlastung",
            }
        }

        alerts = evaluate_training_alerts(user_id="test_alerts_user", training_engine=mock_engine)
        self.assertGreater(len(alerts), 0)

        ramp_alert = next((a for a in alerts if a["alert_key"] == "ramp_rate_excessive"), None)
        self.assertIsNotNone(ramp_alert)
        self.assertEqual(ramp_alert["severity"], "warning")

        fatigue_alert = next((a for a in alerts if a["alert_key"] == "acute_fatigue_overload"), None)
        self.assertIsNotNone(fatigue_alert)
        self.assertEqual(fatigue_alert["severity"], "critical")

        # Aus Datenbank abrufen
        store = ActivityVectorStore()
        active = store.get_active_training_alerts(user_id="test_alerts_user")
        self.assertGreaterEqual(len(active), 2)

    def test_gps_speed_spike_filtering_and_clean_calculations(self):
        from analyze_gpx import parse_json_activity_file, calculate_metrics, detect_activity_anomalies
        p = Path("downloads/20322258248_Mittagslauf.json")
        if p.exists():
            title, act_type, points = parse_json_activity_file(p)
            metrics = calculate_metrics(p, title, act_type, points)
            self.assertTrue(metrics.speed_filtered)
            self.assertIsNotNone(metrics.raw_max_speed_kmh)
            self.assertGreater(metrics.raw_max_speed_kmh, 45.0)
            # Bereinigtes Max-Tempo liegt im physiologisch realistischen Laufbereich
            self.assertLessEqual(metrics.max_speed_kmh, 32.0)
            self.assertIn("Sensor-/GPS-Glitch", metrics.speed_filter_reason)

            anomalies = detect_activity_anomalies(metrics, points)
            spike_anomaly = next((a for a in anomalies if a["type"] == "gps_speed_spike_filtered"), None)
            self.assertIsNotNone(spike_anomaly)
            self.assertEqual(spike_anomaly["severity"], "warning")

    def test_short_activity_split_and_cardiac_drift_eval(self):
        from analyze_gpx import parse_json_activity_file, calculate_metrics, estimate_virtual_power_and_coaching, TrackPoint, detect_activity_anomalies
        p = Path("downloads/20322258259_Mittagslauf.json")
        if p.exists():
            title, act_type, points = parse_json_activity_file(p)
            metrics = calculate_metrics(p, title, act_type, points)
            # 530s Dauer: Früher durch 600s-Limit genullt, jetzt vollständig ausgewertet
            self.assertIsNotNone(metrics.coaching_telemetry.get("cardiac_drift_pct"))
            self.assertIsNotNone(metrics.coaching_telemetry.get("first_half_avg_hr"))
            self.assertIsNotNone(metrics.coaching_telemetry.get("second_half_avg_hr"))
            self.assertIn("split_context", metrics.coaching_telemetry)
            self.assertIn("Warm-up-Kinetik", metrics.coaching_telemetry["split_context"])

        # Test für extrem kurze Aktivität (< 180s)
        from datetime import datetime, timezone, timedelta
        base_time = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        pts_short = [
            TrackPoint(lat=48.0, lon=7.8, ele=200.0, time=base_time, hr=140),
            TrackPoint(lat=48.001, lon=7.801, ele=200.0, time=base_time + timedelta(seconds=120), hr=150),
        ]
        m_short = calculate_metrics(Path("dummy.gpx"), "Micro Run", "Run", pts_short)
        anomalies_short = detect_activity_anomalies(m_short, pts_short)
        short_anomaly = next((a for a in anomalies_short if a["type"] == "short_activity_drift_omitted"), None)
        self.assertIsNotNone(short_anomaly)
        self.assertEqual(short_anomaly["severity"], "info")

    def test_clean_coaching_directives_payload_architecture(self):
        from mcp_server import _sync_download_and_analyze_activity, get_athlete_profile
        from training_load import TrainingLoadEngine

        # 1. Download & Analyze
        p = Path("downloads/20322258248_Mittagslauf.json")
        if p.exists():
            res = _sync_download_and_analyze_activity("20322258248")
            self.assertIn("coaching_directives", res)
            directives = res["coaching_directives"]
            self.assertNotIn("tone", directives)
            self.assertNotIn("no_flattery", directives)
            self.assertNotIn("guidelines", directives)
            self.assertIn("coaching_style", directives)
            self.assertIn("focus_areas", directives)

        # 2. Athlete Profile
        profile_res = get_athlete_profile()
        prof_dirs = profile_res["coaching_directives"]
        self.assertNotIn("tone", prof_dirs)
        self.assertNotIn("no_flattery", prof_dirs)
        self.assertNotIn("guidelines", prof_dirs)

        # 3. Training Load Status
        engine = TrainingLoadEngine()
        status_res = engine.get_training_status()
        load_dirs = status_res["coaching_directives"]
        self.assertNotIn("tone", load_dirs)
        self.assertNotIn("no_flattery", load_dirs)
        self.assertNotIn("guidelines", load_dirs)

    def test_segment_history_with_aliases(self):
        import asyncio
        from mcp_server import analyze_segment_history

        # Test Aufruf mit segment_name als Alias
        res_name = asyncio.run(analyze_segment_history(segment_name="NonExistentSegmentXYZ"))
        self.assertEqual(res_name["status"], "not_found")
        self.assertEqual(res_name["query"], "NonExistentSegmentXYZ")

        # Test Aufruf mit segment_id als Alias
        res_id = asyncio.run(analyze_segment_history(segment_id="9999999"))
        self.assertEqual(res_id["status"], "not_found")


class TestFourNewCapabilities(unittest.TestCase):
    """Tests für die 4 neuen Features: Recovery-Loop, Segment Progress, Periodisierung & Proaktive Alerts."""

    def test_1_recovery_loop_in_get_training_status(self):
        import asyncio
        from mcp_server import get_training_status
        from training_load import TrainingLoadEngine

        # Live Tool Aufruf
        res = asyncio.run(get_training_status())
        self.assertEqual(res["status"], "success")
        self.assertIn("recovery_context", res)
        rc = res["recovery_context"]
        self.assertIn("readiness_score", rc)
        self.assertIn("confidence_score", rc)
        self.assertIn("is_stale", rc)
        self.assertIn("tsb_vs_readiness_comparison", rc)

        # Simulation: TSB negativ UND Readiness < 50 => Übertrainings-Warnung
        from datetime import date, timedelta
        engine = TrainingLoadEngine()
        today = date.today()
        for i in range(14):
            engine.add_activity(
                activity_id=f"intense_{i}",
                name="Intervalle",
                sport_type="Ride",
                activity_date=today - timedelta(days=14 - i),
                duration_s=5400,
                distance_m=45000,
                elevation_gain_m=800,
                suffer_score=120,
            )

        mock_health = {
            "latest_metrics": {
                "readiness_score": 42.0,
                "confidence_score": 0.9,
                "confidence_level": "high",
                "recorded_date": str(today),
                "is_stale": False,
                "signals_available": ["sleep", "hrv", "resting_heart_rate"],
            }
        }
        # Status mit simulierter niedriger Readiness abfragen
        mock_res = engine.get_training_status(until_date=today, health_data=mock_health)
        mock_rc = mock_res["recovery_context"]
        self.assertEqual(mock_rc["readiness_score"], 42.0)
        self.assertFalse(mock_rc["is_stale"])
        self.assertLess(mock_res["metrics"]["tsb_form"], 0.0)
        self.assertIsNotNone(mock_rc["overtraining_warning"])
        self.assertIn("Überlastungs- und Übertrainingsgefahr", mock_rc["overtraining_warning"])

    def test_2_get_segment_progress_history(self):
        import asyncio
        from mcp_server import get_segment_progress_history

        # 1. Bekanntes Segment testen (Römerstraße aus Mock-DB)
        res = asyncio.run(get_segment_progress_history(segment_name="Römerstraße", limit=5))
        self.assertIn(res["status"], ("success", "not_found"))
        if res["status"] == "success":
            self.assertIn("chronological_progress", res)
            self.assertIn("personal_record", res)
            self.assertIn("data_quality_and_anomalies", res)
            progress = res["chronological_progress"]
            self.assertGreater(len(progress), 0)
            entry = progress[0]
            self.assertIn("date", entry)
            self.assertIn("time", entry)
            self.assertIn("avg_watts", entry)
            self.assertIn("vam", entry)
            self.assertIn("status", entry)

        # 2. Nicht existierendes Segment testen
        not_found_res = asyncio.run(get_segment_progress_history(segment_name="VölligUnbekanntesSegment9876"))
        self.assertEqual(not_found_res["status"], "not_found")
        self.assertIn("available_segments", not_found_res)
        self.assertIn("data_quality_and_anomalies", not_found_res)

    def test_3_plan_periodization(self):
        import asyncio
        from mcp_server import plan_periodization

        # 1. Automatische Ziel-Ermittlung aus Athletenprofil
        res = asyncio.run(plan_periodization(target_date="2026-12-31"))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["target_date"], "2026-12-31")
        self.assertIn("target_event", res)
        self.assertIn("event_source", res)
        self.assertIn("recommended_ramp_rate", res)
        self.assertIn("phases_summary", res)
        self.assertIn("weekly_targets", res)
        self.assertIn("data_quality_and_anomalies", res)

        # Überprüfe Phasen
        phases = res["phases_summary"]
        self.assertIn("base_phase", phases)
        self.assertIn("build_phase", phases)
        self.assertIn("taper_phase", phases)

        # Überprüfe wöchentliche CTL-Ziele
        self.assertGreater(len(res["weekly_targets"]), 0)
        first_week = res["weekly_targets"][0]
        self.assertIn("target_ctl", first_week)
        self.assertIn("target_weekly_tss", first_week)
        self.assertIn("phase", first_week)

        # 2. Explizites Event
        res_explicit = asyncio.run(plan_periodization(target_date="2027-04-15", target_event="gran_fondo"))
        self.assertEqual(res_explicit["status"], "success")
        self.assertEqual(res_explicit["target_event"], "gran_fondo")

    def test_4_proactive_alerts_in_sync(self):
        from training_load import check_and_create_proactive_coaching_alerts
        from rag_engine import ActivityVectorStore

        # Live Aufruf der Alert-Engine
        res = check_and_create_proactive_coaching_alerts()
        self.assertEqual(res["status"], "success")
        self.assertIn("triggered_alerts", res)
        self.assertIn("created_coaching_notes", res)

        # Test der Deduplizierung: zweiter Aufruf sofort danach darf keine doppelten Notizen erzeugen
        res2 = check_and_create_proactive_coaching_alerts()
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["created_coaching_notes_count"], 0)


class TestAdvancedCoachingAndBlackbox(unittest.TestCase):
    """Tests für die erweiterten Menschlicher-Coach-Tools, prädiktive Physik und Blackbox-Infrastruktur."""

    def test_post_workout_checkin(self):
        from mcp_server import record_post_workout_checkin, get_post_workout_checkins
        res = record_post_workout_checkin(
            activity_id="test_act_999",
            rpe_score=7,
            legs_feeling=4,
            mental_state="müde nach Schulstress",
            fueling_adherence="1 Riegel + 500ml Wasser",
            weather_felt="starker Gegenwind",
            pain_or_discomfort="leichtes Ziehen im linken Knie",
            athlete_notes="Auf den letzten 15 km eingebrochen",
            duration_minutes=75.0,
            user_id="test_user_coach",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["checkin"]["rpe_score"], 7)
        self.assertEqual(res["checkin"]["legs_feeling"], 4)
        self.assertAlmostEqual(res["checkin"]["session_rpe_load"], 525.0)

        # Abrufen
        fetched = get_post_workout_checkins(activity_id="test_act_999", user_id="test_user_coach")
        self.assertEqual(fetched["status"], "success")
        self.assertGreaterEqual(fetched["count"], 1)
        self.assertEqual(fetched["checkins"][0]["pain_or_discomfort"], "leichtes Ziehen im linken Knie")

    def test_generate_structured_workout(self):
        from mcp_server import generate_structured_workout
        # Sweetspot Workout
        res = generate_structured_workout(
            workout_type="sweetspot_intervals",
            target_duration_minutes=60,
            export_format="zwo",
        )
        self.assertIn("workout_title", res)
        self.assertIn("Sweetspot", res["workout_title"])
        self.assertGreater(res["intervals_count"], 3)
        self.assertIn("<workout_file>", res["zwift_zwo_xml"])
        self.assertIn("SteadyState", res["zwift_zwo_xml"])

        # VO2max Workout
        res_vo2 = generate_structured_workout(workout_type="vo2max_power", target_duration_minutes=50)
        self.assertIn("VO2max", res_vo2["workout_title"])

    def test_live_bonk_and_fueling_horizon(self):
        from mcp_server import calculate_live_bonk_and_fueling_horizon
        res = calculate_live_bonk_and_fueling_horizon(
            current_duration_minutes=90.0,
            current_distance_km=38.0,
            avg_hr=165,
            carbs_consumed_grams=30.0,
            target_distance_km=60.0,
        )
        self.assertIn("bonk_countdown", res)
        self.assertIn("minutes_remaining", res["bonk_countdown"])
        self.assertIn("recommended_action", res["bonk_countdown"])
        self.assertGreater(res["glycogen_capacity_grams"], 300)

    def test_dynamic_hydration_alert(self):
        from mcp_server import calculate_dynamic_hydration_alert
        res = calculate_dynamic_hydration_alert(
            temperature_c=28.0,
            duration_minutes=80.0,
            avg_hr=162,
            fluid_consumed_ml=300.0,
            humidity_pct=65.0,
        )
        self.assertGreater(res["sweat_rate_l_per_hour"], 0.8)
        self.assertGreater(res["net_fluid_deficit_ml"], 400.0)
        self.assertIn("immediate_drinking_target_ml", res)
        self.assertIn("alert_message", res)

    def test_wind_adjusted_segment_virtual_ghost(self):
        from mcp_server import predict_wind_adjusted_segment
        res = predict_wind_adjusted_segment(
            segment_name="Schauinsland Anstieg",
            segment_distance_m=4000.0,
            current_speed_kmh=18.0,
            baseline_pr_seconds=800.0,
            segment_bearing_deg=180.0,
            wind_speed_kmh=20.0,
            wind_direction_deg=180.0,  # Frontaler Gegenwind
        )
        self.assertEqual(res["wind_aerodynamics"]["wind_type"], "Gegenwind")
        self.assertGreater(res["wind_aerodynamics"]["aerodynamic_power_delta_watts"], 0)
        self.assertGreater(res["wind_normalized_performance"]["wind_adjusted_speed_kmh"], 18.0)
        self.assertIn("virtual_ghost_verdict", res["wind_normalized_performance"])

    def test_gravel_vibration_and_tire_pressure(self):
        from mcp_server import analyze_gravel_vibration_and_tire_pressure
        res = analyze_gravel_vibration_and_tire_pressure(
            surface_type="gravel",
            tire_width_mm=40.0,
            current_pressure_bar=3.2,  # Deutlich zu hart für Schotter
            vibration_g_peak=4.2,      # Hohe Erschütterungen
        )
        self.assertLess(res["optimal_recommended_pressure_bar"], 2.6)
        self.assertGreater(res["estimated_impedance_loss_watts"], 5.0)
        self.assertIn("Reifen zu hart", res["pressure_verdict"])
        self.assertLess(res["vibration_telemetry"]["grip_safety_index_1_to_10"], 7.0)

    def test_live_telemetry_chunk_whisper(self):
        from mcp_server import analyze_live_telemetry_chunk
        res = analyze_live_telemetry_chunk(
            speed_kmh=28.5,
            hr=178,
            slope_pct=1.0,
            duration_min=45.0,
            wind_headwind_kmh=18.0,
            temperature_c=22.0,
            vibration_level="normal",
        )
        self.assertIn("live_coach_whisper", res)
        self.assertIn("Gegenwind", res["live_coach_whisper"])

    def test_live_coach_starlette_route(self):
        import asyncio
        from starlette.testclient import TestClient
        from mcp_server import create_starlette_app

        app = create_starlette_app()
        client = TestClient(app)

        # GET Status
        resp_get = client.get("/api/live-coach")
        self.assertEqual(resp_get.status_code, 200)
        self.assertEqual(resp_get.json()["status"], "online")

        # POST Telemetry Chunk
        payload = {
            "speed_kmh": 26.0,
            "hr": 160,
            "slope_pct": 2.0,
            "duration_min": 60.0,
            "wind_headwind_kmh": 12.0,
            "temperature_c": 24.0,
            "vibration_level": "normal",
            "carbs_consumed_grams": 20.0,
            "fluid_consumed_ml": 400.0,
            "distance_km": 25.0,
        }
        resp_post = client.post("/api/live-coach", json=payload)
        self.assertEqual(resp_post.status_code, 200)
        data = resp_post.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("live_coach_whisper", data)
        self.assertIn("bonk_minutes_remaining", data)
        self.assertIn("dehydration_severity", data)

    def test_blackbox_ride_upload_filter(self):
        import asyncio
        from mcp_server import ingest_blackbox_ride_log

        # 1. Hofrunde (<150m Radius) muss verworfen werden
        short_points = [
            {"lat": 48.0000, "lon": 7.8500, "ele": 280, "time": "2026-10-01T14:00:00Z", "hr": 110},
            {"lat": 48.0001, "lon": 7.8501, "ele": 280, "time": "2026-10-01T14:00:30Z", "hr": 115},
            {"lat": 48.0002, "lon": 7.8500, "ele": 280, "time": "2026-10-01T14:01:00Z", "hr": 120},
            {"lat": 48.0001, "lon": 7.8499, "ele": 280, "time": "2026-10-01T14:01:30Z", "hr": 118},
            {"lat": 48.0000, "lon": 7.8500, "ele": 280, "time": "2026-10-01T14:02:00Z", "hr": 112},
        ]
        res_discard = asyncio.run(ingest_blackbox_ride_log(track_points=short_points, ride_name="Hof_Test"))
        self.assertEqual(res_discard["status"], "discarded")
        self.assertIn("Hof", res_discard["reason"])

        # 2. Echte Tour (>500m) muss als GPX gespeichert werden
        real_points = [
            {"lat": 48.0000, "lon": 7.8500, "ele": 280, "time": "2026-10-01T14:00:00Z", "hr": 130},
            {"lat": 48.0050, "lon": 7.8550, "ele": 285, "time": "2026-10-01T14:05:00Z", "hr": 145},
            {"lat": 48.0100, "lon": 7.8600, "ele": 290, "time": "2026-10-01T14:10:00Z", "hr": 150},
            {"lat": 48.0150, "lon": 7.8650, "ele": 300, "time": "2026-10-01T14:15:00Z", "hr": 155},
            {"lat": 48.0200, "lon": 7.8700, "ele": 310, "time": "2026-10-01T14:20:00Z", "hr": 160},
        ]
        res_save = asyncio.run(ingest_blackbox_ride_log(track_points=real_points, ride_name="Test_Echte_Ausfahrt"))
        self.assertEqual(res_save["status"], "saved")
        self.assertIn("blackbox_", res_save["file"])


class TestCustomHealthExportApp(unittest.TestCase):
    """Tests für die neue eigene Apple Health Export REST-API und Auto-Scanner Integration."""

    def test_custom_health_export_parsing_and_persistence(self):
        from apple_health_service import parse_and_store_custom_health_data
        from rag_engine import ActivityVectorStore

        mock_payload = {
            "source": "export",
            "vitals": {
                "weightKg": 58.5,
                "heightMeters": 1.78,
                "restingHeartRateBpm": 54,
                "hrvSdnnMs": 72.0,
                "vo2Max": 44.5,
            },
            "summaries": [
                {
                    "date": "2026-10-01",
                    "steps": 11500,
                    "distanceMeters": 9200,
                    "activeEnergyKcal": 620.0,
                    "restingHeartRateBpm": 54,
                    "hrvSdnnMs": 72.0,
                    "sleepDurationHours": 7.8,
                }
            ],
            "sleep": [
                {
                    "date": "2026-10-01",
                    "totalSleepMinutes": 468,
                    "deepSleepMinutes": 75.0,
                    "remSleepMinutes": 95.0,
                    "coreSleepMinutes": 260.0,
                    "awakeMinutes": 38.0,
                }
            ],
            "workouts": [
                {
                    "id": "mock_workout_apple_001",
                    "activityType": "Radfahren / Cycling",
                    "durationMinutes": 45.0,
                    "totalDistanceMeters": 18500,
                    "totalEnergyKcal": 420.0,
                    "startDate": "2026-10-01T15:00:00Z",
                    "endDate": "2026-10-01T15:45:00Z",
                    "source": "Apple Watch von Hannes",
                }
            ],
        }

        res = parse_and_store_custom_health_data(mock_payload, user_id="test_health_user")
        self.assertGreaterEqual(len(res["stored_records"]), 1)
        self.assertEqual(res["workouts_count"], 1)

        store = ActivityVectorStore()
        # Profilaktualisierung prüfen
        prof = store.get_profile(user_id="test_health_user")
        self.assertEqual(prof["rider_weight_kg"], 58.5)
        self.assertEqual(prof["hr_rest"], 54)
        self.assertEqual(prof["vo2_max"], 44.5)

        # Workouts prüfen
        workouts = store.get_apple_health_workouts(days=7, user_id="test_health_user")
        self.assertTrue(any(w["id"] == "mock_workout_apple_001" for w in workouts))

    def test_mcp_health_tools_offline_graceful(self):
        from mcp_server import (
            get_apple_health_readiness,
            get_apple_health_vitals,
            get_apple_health_sleep_analysis,
            get_apple_health_daily_activity,
            get_apple_health_workouts,
        )

        readiness = get_apple_health_readiness(force_sync=False, days=7, user_id="default")
        self.assertIn("server_online", readiness)
        self.assertIn("status", readiness)

        vitals = get_apple_health_vitals(force_sync=False, user_id="default")
        self.assertIn("vitals", vitals)
        self.assertIn("baselines", vitals)

        sleep = get_apple_health_sleep_analysis(days=7, force_sync=False, user_id="default")
        self.assertIn("nights", sleep)
        self.assertIn("sleep_assessment", sleep)

        activity = get_apple_health_daily_activity(days=7, force_sync=False, user_id="default")
        self.assertIn("averages", activity)
        self.assertIn("daily_breakdown", activity)

        workouts = get_apple_health_workouts(days=7, force_sync=False, user_id="default")
        self.assertIn("workouts", workouts)

    def test_apple_health_auto_scanner_instantiation(self):
        from apple_health_service import AppleHealthAutoScanner

        scanner = AppleHealthAutoScanner.get_instance(check_interval_seconds=5)
        self.assertEqual(scanner.interval, 5)
        self.assertFalse(scanner._running)

    def test_vo2max_estimation(self):
        from sports_science import estimate_vo2max_from_power
        from mcp_server import estimate_vo2max

        res = estimate_vo2max_from_power(watts=220.0, rider_weight_kg=55.0, age=15)
        self.assertEqual(res["status"], "success")
        self.assertIn("vo2max_estimates", res)
        self.assertGreater(res["vo2max_estimates"]["consensus_ml_kg_min"], 45.0)
        self.assertIn("u17_benchmark", res)

        tool_res = estimate_vo2max(watts=220.0, rider_weight_kg=55.0, age=15)
        self.assertEqual(tool_res["status"], "success")

    def test_aerobic_decoupling(self):
        from sports_science import calculate_aerobic_decoupling

        # Simuliere 40 Trackpoints mit konstanter Leistung und leichtem Pulsanstieg
        mock_points = []
        for i in range(40):
            hr = 140 if i < 20 else 148
            mock_points.append({"hr": hr, "watts": 180.0, "speed_kmh": 28.0})

        dec = calculate_aerobic_decoupling(mock_points)
        self.assertEqual(dec["status"], "success")
        self.assertIn("decoupling_pct", dec)
        self.assertGreater(dec["decoupling_pct"], 0.0)
        self.assertIn("classification", dec)

    def test_council_and_science_tools_registered(self):
        import mcp_server
        tools = [t.name for t in mcp_server.mcp._tool_manager.list_tools()]
        self.assertIn("get_council_coaching_verdict", tools)
        self.assertIn("estimate_vo2max", tools)
        self.assertIn("calculate_activity_decoupling", tools)


if __name__ == "__main__":
    unittest.main()





