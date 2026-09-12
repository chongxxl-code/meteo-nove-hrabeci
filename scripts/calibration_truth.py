#!/usr/bin/env python3
from __future__ import annotations

import bisect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DWD_STATION_ID = "06129"
DWD_STATION_NAME = "Sohland/Spree"
DWD_DISTANCE_KM = 4.89
DWD_MATCH_MINUTES = 20
DWD_ISSUE_MAX_AGE_MINUTES = 30


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


def latest_truth_at_or_before(target, truth, times, max_age_minutes=DWD_ISSUE_MAX_AGE_MINUTES):
    """Return only an observation that was already available at *target*.

    This differs intentionally from nearest_truth(): the nearest observation may be
    a future sample. Calibration features representing the situation at forecast
    issuance must never see such a sample, so this matcher only searches backward.
    """
    if not times:
        return None
    index = bisect.bisect_right(times, target) - 1
    if index < 0:
        return None
    stamp = times[index]
    age = (target - stamp).total_seconds() / 60.0
    if age < 0 or age > max_age_minutes:
        return None
    return stamp, truth[stamp], age


def issue_observation_context(issued_at, truth, times):
    """Build compact, leakage-safe recent-observation features at issuance time."""
    current = latest_truth_at_or_before(issued_at, truth, times)
    if current is None:
        return None
    observed_at, record, age_minutes = current
    current_temp = record.get("temperature_c")
    if current_temp is None:
        return None

    context = {
        "station_id": DWD_STATION_ID,
        "station_name": DWD_STATION_NAME,
        "observed_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
        "age_minutes_at_issue": round(float(age_minutes), 1),
        "temperature_c": float(current_temp),
        "relative_humidity_pct": record.get("relative_humidity_pct"),
        "leakage_guard": "current observation timestamp <= forecast issued_at_utc",
    }

    for hours in (1, 3, 6):
        target = issued_at - timedelta(hours=hours)
        matched = nearest_truth(target, truth, times)
        lag_temp = None
        lag_stamp = None
        if matched is not None:
            stamp, lag_record, _ = matched
            # The entire lag-search window is in the past, but retain an explicit
            # guard in case the lag definition changes later.
            if stamp <= issued_at and lag_record.get("temperature_c") is not None:
                lag_stamp = stamp
                lag_temp = float(lag_record["temperature_c"])
        context[f"temperature_{hours}h_ago_c"] = lag_temp
        context[f"temperature_{hours}h_ago_at_utc"] = (
            lag_stamp.isoformat().replace("+00:00", "Z") if lag_stamp else None
        )
        context[f"temperature_change_{hours}h_c"] = (
            round(float(current_temp) - lag_temp, 3) if lag_temp is not None else None
        )

    return context


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
