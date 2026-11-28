import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_heating_system as audit
import build_indoor_thermal_model as thermal


class StaleHeatingGateTests(unittest.TestCase):
    def test_stale_indoor_cannot_support_heating_energy_claim(self):
        source = {
            "latest_indoor_c": 16.9,
            "latest_setpoint_c": 7.0,
            "last_timestamp": "2026-11-18T19:33:26Z",
        }
        result = thermal.latest_for_current_heating(source, {"ok": False})
        self.assertIsNone(result["latest_indoor_c"])
        self.assertIsNone(result["latest_setpoint_c"])
        self.assertEqual(result["last_timestamp"], source["last_timestamp"])
        self.assertEqual(source["latest_indoor_c"], 16.9)

    def test_fresh_indoor_preserves_current_values(self):
        source = {"latest_indoor_c": 16.9, "latest_setpoint_c": 7.0}
        self.assertEqual(thermal.latest_for_current_heating(source, {"ok": True}), source)

    def test_trained_fit_stale_indoor_correctly_withholds_forecast(self):
        model = {
            "prediction_ready": True,
            "current_indoor_state": {"ok": False},
            "passive_forecast": None,
        }
        self.assertTrue(audit.passive_forecast_publication_is_safe(model))
        model["passive_forecast"] = {"horizons": []}
        self.assertFalse(audit.passive_forecast_publication_is_safe(model))

    def test_fresh_indoor_requires_covered_forecast(self):
        model = {
            "prediction_ready": True,
            "current_indoor_state": {"ok": True},
            "passive_forecast": None,
        }
        self.assertFalse(audit.passive_forecast_publication_is_safe(model))
        model["passive_forecast"] = {
            "forecast_coverage_contract": {"minimum_model_branches": 2},
            "horizons": [{"coverage_sufficient": True, "observed_model_branches": 3}],
        }
        self.assertTrue(audit.passive_forecast_publication_is_safe(model))
        model["passive_forecast"]["horizons"][0]["observed_model_branches"] = 1
        self.assertFalse(audit.passive_forecast_publication_is_safe(model))

    def test_untrained_fit_never_publishes_forecast(self):
        model = {
            "prediction_ready": False,
            "current_indoor_state": {"ok": True},
            "passive_forecast": None,
        }
        self.assertTrue(audit.passive_forecast_publication_is_safe(model))
        model["passive_forecast"] = {"horizons": []}
        self.assertFalse(audit.passive_forecast_publication_is_safe(model))


    def test_dynamic_reference_15_c_is_more_expensive_than_7_c(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        start = datetime(2026, 11, 28, 8, 0, tzinfo=ZoneInfo("Europe/Prague"))
        times = [(start + timedelta(hours=h)).isoformat() for h in range(1, 26)]
        snapshot = {"models": {
            "chmi": {"time": times, "temperature_2m": [-5.0] * len(times)},
            "ec": {"time": times, "temperature_2m": [-4.0] * len(times)},
        }}
        prior = {"w_per_k": {"low": 172.9, "central": 230.5, "high": 345.7}}
        def reference(setpoint, **opts):
            return thermal.live_setpoint_maintenance_reference(
                {"latest_setpoint_c": setpoint, "dp2_raw": opts.get("mode", "manual")},
                snapshot, start, prior, 0.995, 4.39393,
                opts.get("connected", True), opts.get("forecast_fresh", True),
            )
        seven = reference(7)
        fifteen = reference(15)
        self.assertIsNotNone(fifteen)
        self.assertEqual(fifteen["setpoint_c"], 15)
        self.assertGreater(fifteen["estimated_cost_czk"]["central"], seven["estimated_cost_czk"]["central"])
        self.assertGreater(fifteen["estimated_cost_czk"]["high"], fifteen["estimated_cost_czk"]["low"])
        self.assertFalse(fifteen["actual_consumption_measured"])
        self.assertFalse(fifteen["actual_cost_estimate_available"])
        self.assertIsNone(reference(None))
        self.assertIsNone(reference(15, mode="off"))
        self.assertIsNone(reference(15, connected=False))
        self.assertIsNone(reference(15, forecast_fresh=False))

    def test_dynamic_reference_requires_two_continuous_forecasts(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        start = datetime(2026, 11, 28, 8, 0, tzinfo=ZoneInfo("Europe/Prague"))
        times = [(start + timedelta(hours=h)).isoformat() for h in range(1, 26)]
        snapshot = {"models": {"chmi": {"time": times, "temperature_2m": [-4.0] * len(times)}}}
        result = thermal.live_setpoint_maintenance_reference(
            {"latest_setpoint_c": 15, "dp2_raw": "manual"}, snapshot, start,
            {"w_per_k": {"low": 172.9, "central": 230.5, "high": 345.7}},
            0.995, 4.39393, True, True,
        )
        self.assertIsNone(result)



if __name__ == "__main__":
    unittest.main()
