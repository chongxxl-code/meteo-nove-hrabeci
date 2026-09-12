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
# DWD 10-minute observations arrive operationally with a publication delay. Historical
# archives only carry the observation timestamp, not the time the value became public.
# A conservative 60-minute availability lag prevents retrospective backtests from using
# an observation that would not yet have been retrievable when the forecast was issued.
DWD_OPERATIONAL_AVAILABILITY_LAG_MINUTES = 60
DWD_ISSUE_CONTEXT_POLICY_VERSION = "dwd_publication_lag_60m_v1"


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
    """Return the latest observation at or before *target*, never a future sample."""
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
    """Build recent-observation features using only data plausibly available at issuance.

    Archived DWD rows know when an observation was measured but not exactly when DWD
    published it. Therefore an observation is treated as usable only if its timestamp is
    at least DWD_OPERATIONAL_AVAILABILITY_LAG_MINUTES older than the forecast issuance.
    The 1/3/6 h trends are then measured backward from that actually usable observation.
    """
    availability_cutoff = issued_at - timedelta(
        minutes=DWD_OPERATIONAL_AVAILABILITY_LAG_MINUTES
    )
    current = latest_truth_at_or_before(availability_cutoff, truth, times)
    if current is None:
        return None
    observed_at, record, _ = current
    current_temp = record.get("temperature_c")
    if current_temp is None:
        return None

    actual_age_minutes = (issued_at - observed_at).total_seconds() / 60.0
    context = {
        "station_id": DWD_STATION_ID,
        "station_name": DWD_STATION_NAME,
        "observed_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
        "age_minutes_at_issue": round(float(actual_age_minutes), 1),
        "temperature_c": float(current_temp),
        "relative_humidity_pct": record.get("relative_humidity_pct"),
        "operational_availability_lag_minutes_assumed": DWD_OPERATIONAL_AVAILABILITY_LAG_MINUTES,
        "issue_context_policy_version": DWD_ISSUE_CONTEXT_POLICY_VERSION,
        "availability_cutoff_utc": availability_cutoff.isoformat().replace("+00:00", "Z"),
        "leakage_guard": (
            "observation timestamp <= forecast issued_at_utc minus assumed DWD publication lag"
        ),
    }

    for hours in (1, 3, 6):
        target = observed_at - timedelta(hours=hours)
        matched = nearest_truth(target, truth, times)
        lag_temp = None
        lag_stamp = None
        if matched is not None:
            stamp, lag_record, _ = matched
            if stamp <= observed_at and lag_record.get("temperature_c") is not None:
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
