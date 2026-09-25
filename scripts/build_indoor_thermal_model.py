#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OBS = DATA / "observations"
TZ = ZoneInfo("Europe/Prague")
BUCKET_SECONDS = 30 * 60
WINDOW_HOURS = 6
WINDOW_BINS = int(WINDOW_HOURS * 3600 / BUCKET_SECONDS)


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


def bucket(dt):
    return int(dt.timestamp() // BUCKET_SECONDS)


def q(values, p):
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


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_indoor_bins():
    payload = load_json(DATA / "indoor-history.json", {"points": []})
    grouped = {}
    for row in payload.get("points") or []:
        if not isinstance(row, dict):
            continue
        dt = parse_dt(row.get("timestamp_local"))
        temp = as_float(row.get("indoor_c"))
        if dt is None or temp is None:
            continue
        item = grouped.setdefault(bucket(dt), {"temps": [], "sets": []})
        item["temps"].append(temp)
        sp = as_float(row.get("setpoint_c"))
        if sp is not None:
            item["sets"].append(sp)
    out = {}
    for key, item in grouped.items():
        out[key] = {
            "temperature_c": median(item["temps"]),
            "setpoint_c": median(item["sets"]) if item["sets"] else None,
            "raw_points": len(item["temps"]),
        }
    return out, payload


def load_weather_archive(paths):
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
            grouped.setdefault(bucket(dt), []).append(temp)
    return {key: median(vals) for key, vals in grouped.items() if vals}


def weather_bins():
    chmi_status = load_json(DATA / "chmi-status.json", {})
    chmi_wsi = str(chmi_status.get("weather_station_wsi") or "").replace("-", "_")
    chmi_paths = sorted(OBS.glob(f"chmi-weather-{chmi_wsi}-*.jsonl")) if chmi_wsi else []
    if not chmi_paths:
        chmi_paths = sorted(OBS.glob("chmi-weather-*.jsonl"))
    dwd_paths = sorted(OBS.glob("dwd-sohland-06129-*.jsonl"))

    chmi = load_weather_archive(chmi_paths)
    dwd = load_weather_archive(dwd_paths)
    keys = sorted(set(chmi) | set(dwd))
    merged = {}
    source_counts = {"DWD Sohland": 0, "ČHMÚ Varnsdorf": 0}
    for key in keys:
        if key in dwd:
            merged[key] = {"temperature_c": dwd[key], "source": "DWD Sohland"}
            source_counts["DWD Sohland"] += 1
        elif key in chmi:
            merged[key] = {"temperature_c": chmi[key], "source": "ČHMÚ Varnsdorf"}
            source_counts["ČHMÚ Varnsdorf"] += 1
    return merged, chmi_status, chmi_paths, dwd_paths, source_counts


def build_windows(indoor, weather):
    windows = []
    rejected = {"gap": 0, "setpoint": 0, "warming": 0, "coverage": 0}
    common = sorted(set(indoor) & set(weather))
    common_set = set(common)
    for start in common:
        if start % WINDOW_BINS != 0:
            continue
        end = start + WINDOW_BINS
        if end not in indoor:
            continue
        weather_keys = [key for key in range(start, end + 1) if key in weather]
        if len(weather_keys) < WINDOW_BINS - 2:
            rejected["coverage"] += 1
            continue
        a, b = indoor[start], indoor[end]
        tin_start = a["temperature_c"]
        tin_end = b["temperature_c"]
        tin_mean = (tin_start + tin_end) / 2
        tout_values = [weather[key]["temperature_c"] for key in weather_keys]
        tout_mean = sum(tout_values) / len(tout_values)
        gap = tin_mean - tout_mean

        setpoints = [v for v in (a.get("setpoint_c"), b.get("setpoint_c")) if v is not None]
        setpoint = max(setpoints) if setpoints else None
        if setpoint is not None and setpoint > min(tin_start, tin_end) - 1.5:
            rejected["setpoint"] += 1
            continue
        if gap < 2.0:
            rejected["gap"] += 1
            continue

        rate = (tin_end - tin_start) / WINDOW_HOURS
        if rate > 0.08:
            rejected["warming"] += 1
            continue

        midpoint = datetime.fromtimestamp(
            (start * BUCKET_SECONDS) + (WINDOW_HOURS * 1800),
            timezone.utc,
        ).astimezone(TZ)
        night = midpoint.hour >= 21 or midpoint.hour < 6
        source_names = [weather[key]["source"] for key in weather_keys]
        windows.append({
            "start_bucket": start,
            "midpoint_local": midpoint.isoformat(),
            "night": night,
            "inside_c": tin_mean,
            "outside_c": tout_mean,
            "inside_minus_outside_c": gap,
            "slope_c_per_h": rate,
            "dominant_weather_source": max(set(source_names), key=source_names.count),
        })
    return windows, rejected, len(common_set)


def passive_fit(samples):
    if len(samples) < 3:
        return None
    ratios = []
    for sample in samples:
        gap = sample["inside_minus_outside_c"]
        if gap <= 0:
            continue
        ratios.append(-sample["slope_c_per_h"] / gap)
    if len(ratios) < 3:
        return None
    coupling = median(ratios)
    if coupling <= 0:
        return None
    predictions = [-coupling * s["inside_minus_outside_c"] for s in samples]
    errors = [abs(s["slope_c_per_h"] - pred) for s, pred in zip(samples, predictions)]
    return {
        "equation": "dTin/dt = coupling * (Tout - Tin)",
        "method": f"median through-origin slope on {WINDOW_HOURS}h windows",
        "coupling_per_h": rounded(coupling, 5),
        "time_constant_h": rounded(1.0 / coupling, 1),
        "mae_c_per_h": rounded(sum(errors) / len(errors), 3),
        "median_abs_error_c_per_h": rounded(median(errors), 3),
        "coupling_p25_per_h": rounded(q(ratios, 0.25), 5),
        "coupling_p75_per_h": rounded(q(ratios, 0.75), 5),
        "sample_count": len(samples),
    }


def main():
    indoor, indoor_payload = load_indoor_bins()
    weather, chmi_status, chmi_paths, dwd_paths, source_counts = weather_bins()
    windows, rejected, paired_bins = build_windows(indoor, weather)

    night = [w for w in windows if w["night"]]
    fit_basis = night if len(night) >= 6 else windows
    fit = passive_fit(fit_basis)

    rates = [w["slope_c_per_h"] for w in fit_basis]
    cooling_rates = [r for r in rates if r < -0.01]
    gaps = [w["inside_minus_outside_c"] for w in fit_basis]

    first_key = min(set(indoor) & set(weather)) if set(indoor) & set(weather) else None
    last_key = max(set(indoor) & set(weather)) if set(indoor) & set(weather) else None
    coverage_days = None if first_key is None else (last_key - first_key) * BUCKET_SECONDS / 86400.0

    if fit and len(fit_basis) >= 10 and coverage_days and coverage_days >= 5:
        status = "preliminary_learning"
    elif fit and len(fit_basis) >= 6:
        status = "early_learning"
    else:
        status = "insufficient_data"

    current = None
    latest = load_json(DATA / "indoor-latest.json", {})
    tin_now = as_float(latest.get("latest_indoor_c"))
    tout_now = as_float(chmi_status.get("temperature_c"))
    if fit and tin_now is not None and tout_now is not None:
        gap_now = tin_now - tout_now
        current = {
            "inside_c": tin_now,
            "outside_c": tout_now,
            "outside_source": chmi_status.get("weather_station_name") or "ČHMÚ",
            "inside_minus_outside_c": rounded(gap_now, 1),
            "passive_fit_rate_c_per_h": rounded(-fit["coupling_per_h"] * gap_now, 2),
            "note": "passive nighttime loss estimate; not a heating-control command",
        }

    output = {
        "schema": 2,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "validated_for_heating_control": False,
        "dp2_used_as_heating_state": False,
        "analysis_window_hours": WINDOW_HOURS,
        "fit_basis": "nighttime windows" if fit_basis is night else "all passive windows fallback",
        "coverage_days": rounded(coverage_days, 2),
        "indoor_raw_points": len(indoor_payload.get("points") or []),
        "indoor_30m_bins": len(indoor),
        "weather_30m_bins": len(weather),
        "paired_candidate_bins": paired_bins,
        "fit_samples": len(fit_basis),
        "all_accepted_windows": len(windows),
        "night_windows": len(night),
        "weather_sources": {
            "preferred_training_temperature": "DWD Sohland/Spree when available; ČHMÚ Varnsdorf fallback",
            "DWD_Sohland_distance_km": 4.89,
            "CHMI_Varnsdorf_distance_km": chmi_status.get("weather_station_distance_km"),
            "bin_counts": source_counts,
            "dwd_archives": [str(p.relative_to(ROOT)) for p in dwd_paths],
            "chmi_archives": [str(p.relative_to(ROOT)) for p in chmi_paths],
        },
        "empirical": {
            "median_cooling_c_per_h": rounded(median(cooling_rates), 3) if cooling_rates else None,
            "cooling_p25_c_per_h": rounded(q(cooling_rates, 0.25), 3),
            "cooling_p75_c_per_h": rounded(q(cooling_rates, 0.75), 3),
            "median_passive_window_rate_c_per_h": rounded(median(rates), 3) if rates else None,
            "inside_minus_outside_median_c": rounded(median(gaps), 1) if gaps else None,
            "inside_minus_outside_min_c": rounded(min(gaps), 1) if gaps else None,
            "inside_minus_outside_max_c": rounded(max(gaps), 1) if gaps else None,
        },
        "fit": fit,
        "current_passive_estimate": current,
        "filters": {
            "bucket_minutes": 30,
            "window_hours": WINDOW_HOURS,
            "minimum_inside_minus_outside_c": 2.0,
            "setpoint_margin_c": 1.5,
            "maximum_allowed_warming_c_per_h": 0.08,
            "night_midpoint_hours_local": "21:00-05:59",
            "rejected": rejected,
        },
        "limitations": [
            "DP2 semantics are not independently verified, so it is not treated as boiler or relay state.",
            "The passive fit preferentially uses nighttime 6-hour windows to reduce solar-gain and 0.1 °C sensor-quantization noise.",
            "Wood-stove heat, occupants, open doors/windows and other heat gains are not independently observed yet.",
            "DWD Sohland is preferred for historical outdoor temperature because it is closer; ČHMÚ Varnsdorf fills missing periods.",
            "The fit is observational and preliminary; it must not control heating until heating-state evidence and more seasonal data exist."
        ],
    }
    (DATA / "indoor-thermal-model.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "schema": 2,
        "status": status,
        "fit_basis": output["fit_basis"],
        "fit_samples": len(fit_basis),
        "coverage_days": output["coverage_days"],
        "fit": fit,
        "current": current,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
