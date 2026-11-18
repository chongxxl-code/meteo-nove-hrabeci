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


if __name__ == "__main__":
    unittest.main()
