#!/usr/bin/env python3
"""Verify EMOS on/off status is never replaced with a remembered setpoint."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_indoor_emos as c


class EMOSSwitchTests(unittest.TestCase):
    def test_explicit_off_wins_over_manual_setpoint(self):
        self.assertEqual(c.thermostat_operating_state(False, "manual"), "off")
        self.assertEqual(c.thermostat_operating_state(True, "manual"), "on")
        self.assertEqual(c.thermostat_operating_state(True, "off"), "off")
        self.assertEqual(c.thermostat_operating_state(False, "off"), "off")

    def test_unknown_does_not_infer_power_from_setpoint(self):
        self.assertEqual(c.thermostat_operating_state(None, "manual"), "unknown")
        self.assertEqual(c.thermostat_operating_state(0, "manual"), "unknown")
        self.assertEqual(c.thermostat_operating_state(None, None), "unknown")

    def test_switch_change_must_publish_even_without_temp_change(self):
        now = datetime.now(timezone.utc)
        old = {
            "latest_indoor_c": 15.1, "latest_setpoint_c": 15.0,
            "dp1_raw": True, "dp2_raw": "manual", "dp106_raw": 100,
            "last_timestamp": (now - timedelta(minutes=2)).isoformat(),
        }
        self.assertTrue(c.live_snapshot_should_persist(
            old, now, 15.1, 15.0, "manual", 100, 30, False
        ))
        self.assertFalse(c.live_snapshot_should_persist(
            old, now, 15.1, 15.0, "manual", 100, 30, True
        ))


if __name__ == "__main__":
    unittest.main()
