#!/usr/bin/env python3
from __future__ import annotations

import bisect
import json
from datetime import datetime, timezone
from pathlib import Path

DWD_STATION_ID = "06129"
DWD_STATION_NAME = "Sohland/Spree"
DWD_DISTANCE_KM = 4.89
DWD_MATCH_MINUTES = 20


def parse_utc(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def nearest_truth(target, truth, times, tolerance_minutes=DWD_MATCH_MINUTES):
    """Canonical calibration truth matcher used by both backfill and live archives.

    Forecast targets are UTC instants. DWD MESS_DATUM/observed_at_utc is also treated
    as a UTC instant and the nearest 10-minute Sohland observation is accepted only
    inside one shared tolerance. No secondary station is substituted in calibration.
    """
    if not times:
        return None
    index = bisect.bisect_left(times, target)
    candidates = []
    if index < len(times):
        candidates.append(times[index])
    if index > 0:
        candidates.append(times[index - 1])
    if not candidates:
        return None
    stamp = min(candidates, key=lambda value: abs((value - target).total_seconds()))
    delta = abs((stamp - target).total_seconds()) / 60.0
    if delta > tolerance_minutes:
        return None
    return stamp, truth[stamp], delta


def load_local_dwd_truth(observations_dir: Path):
    truth = {}
    if not observations_dir.exists():
        return truth, []
    for path in sorted(observations_dir.glob(f"dwd-sohland-{DWD_STATION_ID}-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                if str(record.get("station_id")) != DWD_STATION_ID:
                    continue
                stamp = parse_utc(record.get("observed_at_utc"))
                temp = record.get("temperature_c")
                if stamp is None or temp is None:
                    continue
                truth[stamp] = {
                    **record,
                    "temperature_c": float(temp),
                }
            except Exception:
                continue
    return truth, sorted(truth)


def truth_metadata(observed_at, record, match_delta_minutes):
    return {
        "source": "DWD CDC 10-minute",
        "station_id": DWD_STATION_ID,
        "station_name": DWD_STATION_NAME,
        "distance_to_nove_hrabeci_km": DWD_DISTANCE_KM,
        "observed_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
        "match_delta_minutes": round(float(match_delta_minutes), 1),
        "temperature_c": record.get("temperature_c"),
        "relative_humidity_pct": record.get("relative_humidity_pct"),
        "is_nove_hrabeci_truth": False,
        "matching_policy": f"nearest UTC observation within ±{DWD_MATCH_MINUTES} min",
    }
