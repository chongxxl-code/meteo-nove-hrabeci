#!/usr/bin/env python3
"""Verify EMOS mode DP2 and heat-call DP32 are not conflated with DP1/DP106."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_indoor_emos as c


class EMOSSwitchTests(unittest.TestCase):
    def test_dp1_false_does_not_override_active_control_mode(self):
        self.assertEqual(c.thermostat_operating_state(False, "manual"), "on")
        self.assertEqual(c.thermostat_operating_state(True, "manual"), "on")
        self.assertEqual(c.thermostat_operating_state(False, "off"), "off")
        self.assertEqual(c.thermostat_operating_state(True, "off"), "off")
        self.assertEqual(c.thermostat_operating_state(None, None), "unknown")

    def test_heat_call_maps_direct_dp32(self):
        self.assertEqual(c.thermostat_heat_call_state("heat", "manual"), "heating")
        self.assertEqual(c.thermostat_heat_call_state("off", "manual"), "idle")
        self.assertEqual(c.thermostat_heat_call_state("off", "off"), "idle")
        self.assertEqual(c.thermostat_heat_call_state("heat", "off"), "unknown")
        self.assertEqual(c.thermostat_heat_call_state(None, "manual"), "unknown")
        self.assertEqual(c.thermostat_heat_call_state("unexpected", "manual"), "unknown")

    def test_heat_call_change_must_publish_even_without_temp_change(self):
        now = datetime.now(timezone.utc)
        old = {"latest_indoor_c": 15.1, "latest_setpoint_c": 15.0,
               "dp1_raw": False, "dp2_raw": "manual", "dp106_raw": 100,
               "dp32_raw": "heat",
               "last_timestamp": (now - timedelta(minutes=2)).isoformat()}
        self.assertTrue(c.live_snapshot_should_persist(
            old, now, 15.1, 15.0, "manual", 100, 30, False, "off"))
        self.assertFalse(c.live_snapshot_should_persist(
            old, now, 15.1, 15.0, "manual", 100, 30, False, "heat"))


if __name__ == "__main__":
    unittest.main()
