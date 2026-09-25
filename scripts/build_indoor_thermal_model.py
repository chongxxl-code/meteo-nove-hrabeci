#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OBS = DATA / "observations"
BUCKET_SECONDS = 30 * 60


def as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def quantile(values, p):
    vals = sorted(values)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def rounded(value, digits=3):
    return None if value is None else round(float(value), digits)


def bucket(dt):
    return int(dt.timestamp() // BUCKET_SECONDS)


def load_indoor_bins():
    payload = json.loads((DATA / "indoor-history.json").read_text(encoding="utf-8"))
    grouped = {}
    for row in payload.get("points") or []:
        if not isinstance(row, dict):
            continue
        dt = parse_dt(row.get("timestamp_local"))
        temp = as_float(row.get("indoor_c"))
        if dt is None or temp is None:
            continue
        key = bucket(dt)
        item = grouped.setdefault(key, {"temps": [], "sets": [], "times": []})
        item["temps"].append(temp)
        sp = as_float(row.get("setpoint_c"))
        if sp is not None:
            item["sets"].append(sp)
        item["times"].append(dt)
    out = {}
    for key, item in grouped.items():
        out[key] = {
            "temperature_c": median(item["temps"]),
            "setpoint_c": median(item["sets"]) if item["sets"] else None,
            "timestamp_utc": min(item["times"]).isoformat(),
            "raw_points": len(item["temps"]),
        }
    return out, payload


def weather_paths():
    status_path = DATA / "chmi-status.json"
    status = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            status = {}
    wsi = str(status.get("weather_station_wsi") or "").replace("-", "_")
    if wsi:
        paths = sorted(OBS.glob(f"chmi-weather-{wsi}-*.jsonl"))
        if paths:
            return paths, status
    return sorted(OBS.glob("chmi-weather-*.jsonl")), status


def load_weather_bins():
    paths, status = weather_paths()
    grouped = {}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            dt = parse_dt(row.get("observed_at_utc"))
            temp = as_float(row.get("temperature_c"))
            if dt is None or temp is None:
                continue
            key = bucket(dt)
            item = grouped.setdefault(key, {"temps": [], "winds": []})
            item["temps"].append(temp)
            wind = as_float(row.get("wind_speed_ms"))
            if wind is not None:
                item["winds"].append(wind)
    out = {}
    for key, item in grouped.items():
        out[key] = {
            "temperature_c": median(item["temps"]),
            "wind_speed_ms": median(item["winds"]) if item["winds"] else None,
        }
    return out, status, paths


def fit_line(samples):
    if len(samples) < 2:
        return None
    xs = [s["outside_minus_inside_c"] for s in samples]
    ys = [s["slope_c_per_h"] for s in samples]
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    sxx = sum((x - xbar) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / sxx
    intercept = ybar - slope * xbar
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = None if ss_tot <= 1e-12 else 1 - ss_res / ss_tot
    return intercept, slope, r2


def main():
    indoor, indoor_payload = load_indoor_bins()
    weather, chmi_status, paths = load_weather_bins()
    common = sorted(set(indoor).intersection(weather))

    samples = []
    rejected_warming = 0
    rejected_gap = 0
    rejected_setpoint = 0
    for key in common:
        nxt = key + 1
        if nxt not in indoor or nxt not in weather:
            continue
        a, b = indoor[key], indoor[nxt]
        tin_a = a["temperature_c"]
        tin_b = b["temperature_c"]
        tin = (tin_a + tin_b) / 2
        tout = (weather[key]["temperature_c"] + weather[nxt]["temperature_c"]) / 2
        setpoints = [v for v in (a.get("setpoint_c"), b.get("setpoint_c")) if v is not None]
        setpoint = max(setpoints) if setpoints else None
        if setpoint is not None and setpoint > min(tin_a, tin_b) - 1.5:
            rejected_setpoint += 1
            continue
        gap = tin - tout
        if gap < 2.0:
            rejected_gap += 1
            continue
        slope_c_per_h = (tin_b - tin_a) / 0.5
        # Strong warming is likely sun, stove, occupants or active heating. DP2 is deliberately
        # not used here because its semantics as a boiler/relay state are not yet confirmed.
        if slope_c_per_h > 0.2:
            rejected_warming += 1
            continue
        if slope_c_per_h < -2.0:
            continue
        samples.append({
            "outside_minus_inside_c": tout - tin,
            "inside_minus_outside_c": gap,
            "slope_c_per_h": slope_c_per_h,
            "outside_c": tout,
            "inside_c": tin,
        })

    fit = fit_line(samples)
    cooling = [s["slope_c_per_h"] for s in samples if s["slope_c_per_h"] < 0]
    gaps = [s["inside_minus_outside_c"] for s in samples]
    slopes = [s["slope_c_per_h"] for s in samples]

    first_key = min(common) if common else None
    last_key = max(common) if common else None
    coverage_days = None
    if first_key is not None and last_key is not None:
        coverage_days = (last_key - first_key) * BUCKET_SECONDS / 86400.0

    model = None
    current_estimate = None
    if fit:
        intercept, coupling, r2 = fit
        tau = 1.0 / coupling if coupling > 0 else None
        model = {
            "equation": "dTin/dt = intercept + coupling * (Tout - Tin)",
            "intercept_c_per_h": rounded(intercept),
            "coupling_per_h": rounded(coupling, 5),
            "time_constant_h": rounded(tau, 1) if tau and tau < 1000 else None,
            "r2": rounded(r2, 3),
        }
        latest_path = DATA / "indoor-latest.json"
        if latest_path.exists():
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            tin_now = as_float(latest.get("latest_indoor_c"))
            tout_now = as_float(chmi_status.get("temperature_c"))
            if tin_now is not None and tout_now is not None:
                current_estimate = {
                    "inside_c": tin_now,
                    "outside_c": tout_now,
                    "inside_minus_outside_c": rounded(tin_now - tout_now, 1),
                    "passive_fit_rate_c_per_h": rounded(intercept + coupling * (tout_now - tin_now), 2),
                    "note": "fit only; not a heating-control command",
                }

    sample_count = len(samples)
    if sample_count < 24:
        status = "insufficient_data"
    elif coverage_days is not None and coverage_days < 3:
        status = "early_learning"
    else:
        status = "preliminary_learning"

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "validated_for_heating_control": False,
        "dp2_used_as_heating_state": False,
        "coverage_days": rounded(coverage_days, 2),
        "indoor_raw_points": len(indoor_payload.get("points") or []),
        "indoor_30m_bins": len(indoor),
        "weather_30m_bins": len(weather),
        "paired_candidate_bins": len(common),
        "fit_samples": sample_count,
        "weather_station": {
            "name": chmi_status.get("weather_station_name"),
            "wsi": chmi_status.get("weather_station_wsi"),
            "distance_km": chmi_status.get("weather_station_distance_km"),
            "archives": [str(p.relative_to(ROOT)) for p in paths],
        },
        "empirical": {
            "median_cooling_c_per_h": rounded(median(cooling), 2) if cooling else None,
            "cooling_p25_c_per_h": rounded(quantile(cooling, 0.25), 2),
            "cooling_p75_c_per_h": rounded(quantile(cooling, 0.75), 2),
            "median_all_slope_c_per_h": rounded(median(slopes), 2) if slopes else None,
            "observed_inside_minus_outside_median_c": rounded(median(gaps), 1) if gaps else None,
            "observed_inside_minus_outside_min_c": rounded(min(gaps), 1) if gaps else None,
            "observed_inside_minus_outside_max_c": rounded(max(gaps), 1) if gaps else None,
        },
        "fit": model,
        "current_passive_estimate": current_estimate,
        "filters": {
            "bucket_minutes": 30,
            "minimum_inside_minus_outside_c": 2.0,
            "setpoint_margin_c": 1.5,
            "excluded_strong_warming_intervals": rejected_warming,
            "excluded_low_temperature_gap_intervals": rejected_gap,
            "excluded_possible_thermostat_call_intervals": rejected_setpoint,
        },
        "limitations": [
            "DP2 semantics are not independently verified, so it is not treated as boiler or relay state.",
            "Wood-stove heat, solar gains, occupants, doors and windows are not independently observed yet.",
            "The fit is observational and preliminary; it must not control heating until heating-state evidence and broader weather coverage exist."
        ]
    }
    (DATA / "indoor-thermal-model.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "ok": True,
        "status": status,
        "fit_samples": sample_count,
        "coverage_days": rounded(coverage_days, 2),
        "model": model
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
