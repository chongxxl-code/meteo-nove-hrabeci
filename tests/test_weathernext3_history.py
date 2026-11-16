import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_weathernext3_history as wn3


def fake_read(_init, _lead):
    return {
        "station_head_temperature_2m_mean": 275.15,
        "station_head_temperature_2m_p10": 273.15,
        "station_head_temperature_2m_p90": 277.15,
        "station_head_dewpoint_temperature_2m_mean": 271.15,
    }


class WeatherNext3HistoryTests(unittest.TestCase):
    NOW = datetime(2026, 11, 16, 13, 45, tzinfo=timezone.utc)

    def test_uses_older_six_hour_init_and_only_matured_valid_times(self):
        record = wn3.build_record(self.NOW, read_fn=fake_read)
        self.assertEqual(record["run_initialization_utc"], "2026-11-15T12:00:00Z")
        self.assertEqual([x["lead_hours"] for x in record["forecast_points"]], [6, 12, 18])
        self.assertTrue(all(
            datetime.fromisoformat(x["valid_at_utc"].replace("Z", "+00:00"))
            <= self.NOW - timedelta(hours=1)
            for x in record["forecast_points"]
        ))
        self.assertFalse(record["production_use"])
        self.assertFalse(record["heating_control_use"])
        self.assertEqual(record["mode"], "historical_shadow_only")

    def test_never_publish_current_or_future_valid_time(self):
        with self.assertRaisesRegex(ValueError, "real-time"):
            wn3.historical_valid_times(self.NOW, self.NOW)

    def test_units_are_converted_from_kelvin(self):
        row = wn3.normalise_point(fake_read(None, 6), 6, self.NOW - timedelta(hours=6))
        self.assertEqual(row["temperature_2m_mean_c"], 2.0)
        self.assertEqual(row["temperature_2m_p10_c"], 0.0)
        self.assertEqual(row["temperature_2m_p90_c"], 4.0)
        self.assertEqual(row["dewpoint_temperature_2m_mean_c"], -2.0)

    def test_rejects_missing_and_invalid_values(self):
        data = fake_read(None, 6)
        del data["station_head_temperature_2m_mean"]
        with self.assertRaises(ValueError):
            wn3.normalise_point(data, 6, self.NOW)
        data = fake_read(None, 6)
        data["station_head_temperature_2m_mean"] = float("nan")
        with self.assertRaises(ValueError):
            wn3.normalise_point(data, 6, self.NOW)

    def test_duplicate_run_is_idempotent(self):
        record = wn3.build_record(self.NOW, read_fn=fake_read)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-11.jsonl"
            self.assertTrue(wn3.append_once(path, record))
            self.assertFalse(wn3.append_once(path, record))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["run_initialization_utc"], record["run_initialization_utc"])


if __name__ == "__main__":
    unittest.main()
