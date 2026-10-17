"""
test_segments.py: Unit tests for Strava segment extraction and formatting.
"""

import unittest
from pathlib import Path
from sync_gpx import format_segments_table, get_activity_segments
from config import AUTH_FILE, launch_browser, create_browser_context
import mcp_server


class TestSegments(unittest.TestCase):
    def test_format_segments_table_empty(self):
        empty_data = {
            "status": "success",
            "activity_id": "12345",
            "title": "Test Ride",
            "count": 0,
            "segments": [],
        }
        output = format_segments_table(empty_data)
        self.assertIn("Keine Segmente für diese Aktivität gefunden", output)
        self.assertIn("12345", output)

    def test_format_segments_table_error(self):
        err_data = {
            "status": "error",
            "message": "Session abgelaufen",
        }
        output = format_segments_table(err_data)
        self.assertTrue(output.startswith("Fehler: Session abgelaufen"))

    def test_format_segments_table_success(self):
        sample_data = {
            "status": "success",
            "activity_id": "20239426058",
            "title": "Fahrt am Nachmittag",
            "count": 2,
            "segments": [
                {
                    "id": "101",
                    "segment_id": "999",
                    "name": "Super Long Segment Name That Exceeds Normal Length For Display",
                    "distance": "1,82 km",
                    "elapsed_time": "6:27",
                    "moving_time": "6:25",
                    "elevation_gain": "25 m",
                    "avg_grade": "1,3%",
                    "avg_speed": "17,0 km/h",
                    "avg_power": "77 W",
                    "vam": None,
                    "avg_hr": "147 bpm",
                    "max_hr": "154 bpm",
                    "achievement": "PR",
                    "rank": 1,
                    "climb_category": None,
                    "starred": True,
                    "url": "https://www.strava.com/segments/999",
                },
                {
                    "id": "102",
                    "segment_id": "888",
                    "name": "Mountain Pass",
                    "distance": "5,00 km",
                    "elapsed_time": "20:00",
                    "moving_time": "19:30",
                    "elevation_gain": "350 m",
                    "avg_grade": "7,0%",
                    "avg_speed": "15,0 km/h",
                    "avg_power": "220 W",
                    "vam": "1050",
                    "avg_hr": "165 bpm",
                    "max_hr": "178 bpm",
                    "achievement": None,
                    "rank": None,
                    "climb_category": "2",
                    "starred": False,
                    "url": "https://www.strava.com/segments/888",
                },
            ],
        }
        output = format_segments_table(sample_data)
        self.assertIn("Fahrt am Nachmittag", output)
        self.assertIn("2 Segmente gefunden", output)
        self.assertIn("Mountain Pass [Cat 2]", output)
        self.assertIn("★ PR (#1)", output)
        self.assertIn("77 W", output)

    def test_mcp_tool_registration(self):
        tool_names = [t.name for t in mcp_server.mcp._tool_manager.list_tools()]
        self.assertIn("get_activity_segments", tool_names)

    def test_invalid_activity_id(self):
        from unittest.mock import MagicMock
        mock_ctx = MagicMock()
        res = get_activity_segments(mock_ctx, "abc")
        self.assertEqual(res["status"], "error")
        self.assertIn("Ungültige Aktivitäts-ID", res["message"])


if __name__ == "__main__":
    unittest.main()
