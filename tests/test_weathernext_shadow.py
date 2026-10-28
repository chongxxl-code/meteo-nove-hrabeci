import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_weathernext_shadow as collector
import verify_weathernext_shadow as verifier


class WeatherNextShadowTests(unittest.TestCase):
    def fake_payload(self):
        times = [f"2026-10-{28 + (hour // 24):02d}T{hour % 24:02d}:00" for hour in range(6, 73, 6)]
        hourly = {"time": times}
        for member in range(64):
            key = "temperature_2m" if member == 0 else f"temperature_2m_member{member:02d}"
            hourly[key] = [float(member) / 10 + i for i in range(len(times))]
        return {
            "latitude": 51.0,
            "longitude": 14.5,
            "elevation": 352.0,
            "hourly": hourly,
        }

    def fake_meta(self):
        run = datetime(2026, 10, 28, 0, tzinfo=timezone.utc)
        available = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
        return {
            "last_run_initialisation_time": int(run.timestamp()),
            "last_run_availability_time": int(available.timestamp()),
            "temporal_resolution_seconds": 21600,
            "update_interval_seconds": 43200,
        }

    def test_collector_preserves_native_leads_and_operational_gate(self):
        record = collector.build_run_record(
            self.fake_meta(),
            self.fake_payload(),
            collected_at=datetime(2026, 10, 28, 13, 5, tzinfo=timezone.utc),
        )
        self.assertEqual([p["model_lead_hours"] for p in record["forecast_points"]], list(range(6, 73, 6)))
        self.assertEqual(record["ensemble_members"], 64)
        self.assertFalse(record["forecast_points"][0]["available_before_valid"])
        self.assertFalse(record["forecast_points"][1]["available_before_valid"])
        self.assertTrue(next(p for p in record["forecast_points"] if p["model_lead_hours"] == 24)["available_before_valid"])

    def test_duplicate_run_is_not_appended_twice(self):
        record = collector.build_run_record(self.fake_meta(), self.fake_payload())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.jsonl"
            self.assertTrue(collector.append_run_once(path, record))
            self.assertFalse(collector.append_run_once(path, record))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_percentile_and_frost_summary(self):
        self.assertAlmostEqual(collector.percentile([0.0, 10.0], 0.5), 5.0)
        cases = [
            {"frost_probability": 0.8, "observed_c": -1.0},
            {"frost_probability": 0.1, "observed_c": 2.0},
        ]
        summary = verifier.summarize_frost(cases)
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["observed_frost_events"], 1)
        self.assertAlmostEqual(summary["brier_score"], 0.025, places=4)

    def test_temperature_summary_keeps_small_samples_non_conclusive(self):
        summary = verifier.summarize_temperature([{"error_c": 1.0}, {"error_c": -0.5}])
        self.assertEqual(summary["n"], 2)
        self.assertFalse(summary["enough_data"])
        self.assertEqual(summary["mae_c"], 0.75)
        self.assertEqual(summary["bias_c"], 0.25)

    def test_api_outage_fails_closed_and_writes_error_status(self):
        old_data = collector.DATA
        old_archive = collector.ARCHIVE_DIR
        old_status = collector.STATUS_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                collector.DATA = root
                collector.ARCHIVE_DIR = root / "weathernext2-shadow"
                collector.STATUS_PATH = root / "weathernext2-status.json"
                with patch.object(collector, "get_json", side_effect=RuntimeError("simulated API outage")):
                    with self.assertRaises(SystemExit):
                        collector.main()
                status = json.loads(collector.STATUS_PATH.read_text(encoding="utf-8"))
                self.assertFalse(status["ok"])
                self.assertEqual(status["stage"], "error")
                self.assertIn("simulated API outage", status["error"])
        finally:
            collector.DATA = old_data
            collector.ARCHIVE_DIR = old_archive
            collector.STATUS_PATH = old_status


if __name__ == "__main__":
    unittest.main()
