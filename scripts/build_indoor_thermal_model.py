#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
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
    rejected = {"gap": 0, "setpoint": 0, "warming": 0, "heat_gain": 0, "coverage": 0}
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

        indoor_path = [
            indoor[key]["temperature_c"]
            for key in range(start, end + 1)
            if key in indoor
        ]
        if len(indoor_path) >= 2:
            rises = [b - a for a, b in zip(indoor_path, indoor_path[1:])]
            max_rise_30m = max(rises)
            cumulative_rise = sum(max(0.0, rise) for rise in rises)
            if max_rise_30m > 0.2 or cumulative_rise > 0.4:
                rejected["heat_gain"] += 1
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



def load_latest_forecast_snapshot():
    status = load_json(DATA / "status.json", {})
    archive_rel = status.get("archive_file")
    if not archive_rel:
        return None
    path = ROOT / archive_rel
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            item = json.loads(line)
            if isinstance(item, dict) and isinstance(item.get("models"), dict):
                return item
        except Exception:
            continue
    return None


def parse_forecast_local(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except ValueError:
        return None


def build_passive_forecast(latest, fit):
    snapshot = load_latest_forecast_snapshot()
    if not snapshot or not fit:
        return None
    start_temp = as_float(latest.get("latest_indoor_c"))
    start_dt = parse_dt(latest.get("last_timestamp") or latest.get("generated_at"))
    if start_temp is None or start_dt is None:
        return None
    start_local = start_dt.astimezone(TZ)

    k_values = [
        as_float(fit.get("coupling_p25_per_h")),
        as_float(fit.get("coupling_per_h")),
        as_float(fit.get("coupling_p75_per_h")),
    ]
    k_values = sorted({k for k in k_values if k is not None and k > 0})
    if not k_values:
        return None

    scenarios_by_time = {}
    outside_by_time = {}
    used_models = []
    for model_name, payload in (snapshot.get("models") or {}).items():
        if not isinstance(payload, dict):
            continue
        times = payload.get("time") or []
        temps = payload.get("temperature_2m") or []
        series = []
        for raw_time, raw_temp in zip(times, temps):
            dt = parse_forecast_local(raw_time)
            temp = as_float(raw_temp)
            if dt is None or temp is None or dt <= start_local:
                continue
            series.append((dt, temp))
        if not series:
            continue
        used_models.append(model_name)
        for dt, temp in series:
            outside_by_time.setdefault(dt.isoformat(), []).append(temp)

        for coupling in k_values:
            tin = start_temp
            prev_dt = start_local
            for dt, tout in series:
                dt_h = (dt - prev_dt).total_seconds() / 3600.0
                if dt_h <= 0 or dt_h > 3.0:
                    prev_dt = dt
                    continue
                decay = math.exp(-coupling * dt_h)
                tin = tout + (tin - tout) * decay
                scenarios_by_time.setdefault(dt.isoformat(), []).append(tin)
                prev_dt = dt

    if not scenarios_by_time:
        return None

    horizons = []
    keys = sorted(scenarios_by_time, key=lambda x: parse_forecast_local(x))
    for hours in (12, 24, 36, 48):
        target = start_local + timedelta(hours=hours)
        key = min(keys, key=lambda x: abs((parse_forecast_local(x) - target).total_seconds()))
        dt = parse_forecast_local(key)
        if abs((dt - target).total_seconds()) > 5400:
            continue
        vals = scenarios_by_time[key]
        outside_vals = outside_by_time.get(key) or []
        horizons.append({
            "hours": hours,
            "timestamp_local": key,
            "inside_median_c": rounded(median(vals), 1),
            "inside_q25_c": rounded(q(vals, 0.25), 1),
            "inside_q75_c": rounded(q(vals, 0.75), 1),
            "inside_min_scenario_c": rounded(min(vals), 1),
            "inside_max_scenario_c": rounded(max(vals), 1),
            "outside_model_median_c": rounded(median(outside_vals), 1) if outside_vals else None,
            "scenario_count": len(vals),
        })

    return {
        "mode": "no_active_heating",
        "start_timestamp_local": start_local.isoformat(),
        "start_inside_c": start_temp,
        "weather_models": sorted(set(used_models)),
        "coupling_candidates_per_h": [rounded(k, 5) for k in k_values],
        "horizons": horizons,
        "range_note": "q25-q75 reflects weather-model and fitted-coupling spread only; it is not a full prediction interval",
        "assumptions": [
            "No active central heating or wood-stove heat is added.",
            "No major door/window ventilation or unusual internal heat gain occurs.",
            "Passive thermal response remains similar to the learned nighttime behaviour.",
        ],
    }


def load_house_energy_context():
    return (
        load_json(DATA / "house-profile.json", {}),
        load_json(DATA / "house-state.json", {}),
    )


def boiler_sized_heat_loss_prior(profile):
    heating = profile.get("heating_system") if isinstance(profile, dict) else {}
    boiler = heating.get("primary_electric_boiler") if isinstance(heating, dict) else {}
    cfg = profile.get("heating_energy_model") if isinstance(profile, dict) else {}
    if not isinstance(boiler, dict) or not isinstance(cfg, dict):
        return None

    boiler_kw = as_float(boiler.get("rated_heat_output_kw")) or as_float(boiler.get("electrical_input_kw"))
    indoor_design = as_float(cfg.get("design_indoor_c"))
    outdoor_design = as_float(cfg.get("design_outdoor_c"))
    factors = cfg.get("boiler_oversizing_factor") or {}
    if boiler_kw is None or indoor_design is None or outdoor_design is None or not isinstance(factors, dict):
        return None
    design_delta = indoor_design - outdoor_design
    if design_delta <= 0:
        return None

    mapping = {
        "low": as_float(factors.get("low_heat_loss_case")),
        "central": as_float(factors.get("central_case")),
        "high": as_float(factors.get("high_heat_loss_case")),
    }
    out = {}
    for name, factor in mapping.items():
        if factor is None or factor <= 0:
            continue
        out[name] = boiler_kw * 1000.0 / (design_delta * factor)
    if set(out) != {"low", "central", "high"}:
        return None
    return {
        "method": "installed boiler capacity / design temperature difference / oversizing factor",
        "design_indoor_c": indoor_design,
        "design_outdoor_c": outdoor_design,
        "design_delta_k": rounded(design_delta, 1),
        "boiler_thermal_output_kw": rounded(boiler_kw, 2),
        "w_per_k": {key: rounded(value, 1) for key, value in out.items()},
        "absolute_scale_calibrated": False,
    }


def forecast_degree_hours(snapshot, start_local, target_c, horizon_h=24):
    if not snapshot or not isinstance(snapshot.get("models"), dict):
        return {}
    end_local = start_local + timedelta(hours=horizon_h)
    by_model = {}
    for model_name, payload in snapshot["models"].items():
        if not isinstance(payload, dict):
            continue
        pairs = []
        for raw_time, raw_temp in zip(payload.get("time") or [], payload.get("temperature_2m") or []):
            dt = parse_forecast_local(raw_time)
            temp = as_float(raw_temp)
            if dt is None or temp is None:
                continue
            if dt < start_local - timedelta(hours=2) or dt > end_local + timedelta(hours=2):
                continue
            pairs.append((dt, temp))
        pairs.sort(key=lambda item: item[0])
        if not pairs:
            continue

        future = [(dt, temp) for dt, temp in pairs if dt >= start_local]
        if not future:
            continue
        prev_dt = start_local
        prev_temp = future[0][1]
        degree_hours = 0.0
        covered_h = 0.0
        for dt, temp in future:
            if dt <= prev_dt:
                prev_temp = temp
                continue
            seg_end = min(dt, end_local)
            dt_h = (seg_end - prev_dt).total_seconds() / 3600.0
            if dt_h > 0:
                avg_out = (prev_temp + temp) / 2.0
                degree_hours += max(0.0, target_c - avg_out) * dt_h
                covered_h += dt_h
            prev_dt = dt
            prev_temp = temp
            if dt >= end_local:
                break

        if prev_dt < end_local:
            dt_h = (end_local - prev_dt).total_seconds() / 3600.0
            if 0 < dt_h <= 3.1:
                degree_hours += max(0.0, target_c - prev_temp) * dt_h
                covered_h += dt_h

        if covered_h >= min(20.0, horizon_h * 0.8):
            by_model[model_name] = {
                "degree_hours_kh": rounded(degree_hours, 2),
                "covered_hours": rounded(covered_h, 2),
            }
    return by_model


def energy_from_degree_hours(degree_hours, heat_loss_w_per_k, efficiency):
    if degree_hours is None or heat_loss_w_per_k is None or efficiency is None or efficiency <= 0:
        return None
    return (heat_loss_w_per_k / 1000.0) * degree_hours / efficiency


def build_hdo_context(start_local, horizon_h=24):
    payload = load_json(DATA / "hdo-schedule.json", {})
    week = payload.get("week_ahead") if isinstance(payload, dict) else None
    if not isinstance(week, list) or not week:
        return None

    end_local = start_local + timedelta(hours=horizon_h)
    segments = []
    for day in week:
        if not isinstance(day, dict):
            continue
        raw_date = day.get("date")
        try:
            day_date = datetime.fromisoformat(str(raw_date)).date()
        except ValueError:
            continue
        for seg in day.get("timeline") or []:
            if not isinstance(seg, dict):
                continue
            start_text = str(seg.get("start") or "")
            end_text = str(seg.get("end") or "")
            try:
                sh, sm = [int(x) for x in start_text.split(":")]
                eh, em = [int(x) for x in end_text.split(":")]
            except Exception:
                continue
            seg_start = datetime(day_date.year, day_date.month, day_date.day, 0, 0, tzinfo=TZ) + timedelta(hours=sh, minutes=sm)
            seg_end = datetime(day_date.year, day_date.month, day_date.day, 0, 0, tzinfo=TZ) + timedelta(hours=eh, minutes=em)
            if end_text == "24:00":
                seg_end = datetime(day_date.year, day_date.month, day_date.day, 0, 0, tzinfo=TZ) + timedelta(days=1)
            overlap_start = max(start_local, seg_start)
            overlap_end = min(end_local, seg_end)
            if overlap_end <= overlap_start:
                continue
            segments.append({
                "tariff": str(seg.get("tariff") or ""),
                "start_local": overlap_start.isoformat(),
                "end_local": overlap_end.isoformat(),
                "hours": rounded((overlap_end - overlap_start).total_seconds() / 3600.0, 3),
            })

    if not segments:
        return None

    current = next(
        (seg for seg in segments if parse_dt(seg["start_local"]) <= start_local.astimezone(timezone.utc) < parse_dt(seg["end_local"])),
        segments[0],
    )
    nt_h = sum(as_float(seg.get("hours")) or 0.0 for seg in segments if seg.get("tariff") == "NT")
    vt_h = sum(as_float(seg.get("hours")) or 0.0 for seg in segments if seg.get("tariff") == "VT")

    return {
        "status": payload.get("status"),
        "schedule_source": payload.get("schedule_source"),
        "checked_at": payload.get("checked_at"),
        "primary_command": payload.get("primary_command"),
        "distribution_area": payload.get("distribution_area"),
        "horizon_hours": horizon_h,
        "low_tariff_hours": rounded(nt_h, 2),
        "high_tariff_hours": rounded(vt_h, 2),
        "current_tariff": current.get("tariff"),
        "next_transition_local": current.get("end_local"),
        "segments": segments,
        "note": "The current CEZ DIP export is used as the HDO availability schedule and public CEZ sources are checked daily for changes. HDO timing is an input to tempering scheduling; it does not change the total heat-loss estimate by itself.",
    }


def forecast_outside_min(snapshot, start_local, horizon_h=24):
    if not snapshot or not isinstance(snapshot.get("models"), dict):
        return None
    end_local = start_local + timedelta(hours=horizon_h)
    values = []
    for payload in snapshot["models"].values():
        if not isinstance(payload, dict):
            continue
        for raw_time, raw_temp in zip(payload.get("time") or [], payload.get("temperature_2m") or []):
            dt = parse_forecast_local(raw_time)
            temp = as_float(raw_temp)
            if dt is None or temp is None:
                continue
            if start_local <= dt <= end_local:
                values.append(temp)
    return min(values) if values else None


def passive_threshold_crossings(snapshot, start_local, start_temp, threshold_c, fit, horizon_h=24):
    if not snapshot or not fit or start_temp is None or threshold_c is None:
        return []
    couplings = sorted({
        value for value in (
            as_float(fit.get("coupling_p25_per_h")),
            as_float(fit.get("coupling_per_h")),
            as_float(fit.get("coupling_p75_per_h")),
        )
        if value is not None and value > 0
    })
    if not couplings:
        return []

    end_local = start_local + timedelta(hours=horizon_h)
    crossings = []
    for model_name, payload in (snapshot.get("models") or {}).items():
        if not isinstance(payload, dict):
            continue
        series = []
        for raw_time, raw_temp in zip(payload.get("time") or [], payload.get("temperature_2m") or []):
            dt = parse_forecast_local(raw_time)
            temp = as_float(raw_temp)
            if dt is None or temp is None or dt <= start_local or dt > end_local + timedelta(hours=2):
                continue
            series.append((dt, temp))
        series.sort(key=lambda item: item[0])
        if not series:
            continue

        for coupling in couplings:
            tin = start_temp
            prev_dt = start_local
            for dt, tout in series:
                dt_h = (dt - prev_dt).total_seconds() / 3600.0
                if dt_h <= 0:
                    continue
                if dt_h > 3.1:
                    prev_dt = dt
                    continue
                decay = math.exp(-coupling * dt_h)
                next_tin = tout + (tin - tout) * decay
                if tin > threshold_c and next_tin <= threshold_c:
                    denom = tin - next_tin
                    frac = 1.0 if denom <= 0 else max(0.0, min(1.0, (tin - threshold_c) / denom))
                    crossing = prev_dt + timedelta(seconds=(dt - prev_dt).total_seconds() * frac)
                    crossings.append({
                        "weather_model": model_name,
                        "coupling_per_h": rounded(coupling, 5),
                        "timestamp_local": crossing.isoformat(),
                        "hours_from_start": rounded((crossing - start_local).total_seconds() / 3600.0, 2),
                    })
                    break
                tin = next_tin
                prev_dt = dt
    return crossings


def build_actual_tempering_need(latest, snapshot, start_local, fit, prediction_ready, band, price):
    if not isinstance(band, list) or len(band) < 2:
        return None
    inside_now = as_float(latest.get("latest_indoor_c")) if isinstance(latest, dict) else None
    lower_c = as_float(band[0])
    upper_c = as_float(band[1])
    outside_min = forecast_outside_min(snapshot, start_local, 24)
    if inside_now is None or lower_c is None or upper_c is None or outside_min is None:
        return {
            "status": "insufficient_inputs",
            "actual_kwh_estimate_available": False,
            "actual_cost_estimate_available": False,
        }

    base = {
        "horizon_hours": 24,
        "inside_now_c": rounded(inside_now, 1),
        "lower_switch_c": rounded(lower_c, 1),
        "upper_switch_c": rounded(upper_c, 1),
        "forecast_min_outside_c": rounded(outside_min, 1),
        "price_czk_per_kwh": rounded(price, 5) if price is not None else None,
    }

    if inside_now <= lower_c:
        return {
            **base,
            "status": "heating_threshold_already_reached",
            "actual_kwh_estimate_available": False,
            "actual_cost_estimate_available": False,
            "reason": "The control temperature is already at or below the lower hysteresis threshold. Active-cycle energy is not yet calibrated.",
        }

    # This conclusion does not depend on the fitted thermal time constant:
    # in a passive first-order system starting above the threshold, if outdoor
    # temperature never falls below that threshold, the indoor temperature
    # cannot cross it from above.
    if outside_min >= lower_c:
        return {
            **base,
            "status": "no_heating_call_possible_from_forecast_bound",
            "actual_kwh_estimate_available": True,
            "actual_cost_estimate_available": True,
            "electricity_kwh": 0.0,
            "incremental_cost_czk": 0.0,
            "confidence": "high_for_no-call_condition",
            "reason": "Outdoor forecast remains at or above the lower thermostat threshold, while the house starts above it.",
        }

    if not prediction_ready or not fit:
        return {
            **base,
            "status": "learning_threshold_timing",
            "actual_kwh_estimate_available": False,
            "actual_cost_estimate_available": False,
            "confidence": "insufficient_nighttime_passive_windows",
            "reason": "Outdoor temperature can fall below the lower threshold, but the passive model is not yet prediction-ready.",
        }

    crossings = passive_threshold_crossings(
        snapshot,
        start_local,
        inside_now,
        lower_c,
        fit,
        24,
    )
    if not crossings:
        return {
            **base,
            "status": "no_threshold_crossing_predicted",
            "actual_kwh_estimate_available": True,
            "actual_cost_estimate_available": True,
            "electricity_kwh": 0.0,
            "incremental_cost_czk": 0.0,
            "confidence": "preliminary_passive_model",
            "crossing_scenarios": 0,
            "reason": "No weather-model / passive-coupling scenario reaches the lower hysteresis threshold within 24 hours.",
        }

    crossing_hours = [as_float(item.get("hours_from_start")) for item in crossings]
    crossing_hours = [value for value in crossing_hours if value is not None]
    return {
        **base,
        "status": "threshold_crossing_predicted",
        "actual_kwh_estimate_available": False,
        "actual_cost_estimate_available": False,
        "confidence": "preliminary_passive_model",
        "crossing_scenarios": len(crossings),
        "earliest_crossing_h": rounded(min(crossing_hours), 2) if crossing_hours else None,
        "median_crossing_h": rounded(median(crossing_hours), 2) if crossing_hours else None,
        "latest_crossing_h": rounded(max(crossing_hours), 2) if crossing_hours else None,
        "reason": "The passive model predicts reaching the lower threshold, but active heating-cycle energy is not calibrated yet.",
    }


def build_hdo_resilience(hdo_context, snapshot, start_local, fit, band, prediction_ready=False, extra_nt_block_h=0.0):
    if not hdo_context or not isinstance(band, list) or len(band) < 2:
        return None
    vt_hours = [
        as_float(seg.get("hours"))
        for seg in hdo_context.get("segments") or []
        if isinstance(seg, dict) and seg.get("tariff") == "VT"
    ]
    vt_hours = [value for value in vt_hours if value is not None and value > 0]
    lower_c = as_float(band[0])
    outside_min = forecast_outside_min(snapshot, start_local, 24)
    if not vt_hours or lower_c is None or outside_min is None:
        return None

    longest_vt_h = max(vt_hours)
    conservative_unavailability_h = longest_vt_h + max(0.0, as_float(extra_nt_block_h) or 0.0)

    if outside_min >= lower_c:
        return {
            "horizon_hours": 24,
            "longest_vt_block_h": rounded(longest_vt_h, 2),
            "conservative_unavailability_h": rounded(conservative_unavailability_h, 2),
            "forecast_min_outside_c": rounded(outside_min, 1),
            "reference_inside_c": rounded(lower_c, 1),
            "passive_drop_during_longest_vt_central_c": 0.0,
            "passive_drop_during_longest_vt_conservative_c": 0.0,
            "impact_level": "negligible",
            "basis": "forecast_bound",
            "hdo_only_preheat_indication": "No HDO-only preheating is indicated.",
            "principle": "Outdoor temperature stays above the lower thermostat threshold, so an HDO block cannot by itself drive the control temperature below that threshold.",
        }

    if not prediction_ready or not fit:
        return {
            "horizon_hours": 24,
            "longest_vt_block_h": rounded(longest_vt_h, 2),
            "conservative_unavailability_h": rounded(conservative_unavailability_h, 2),
            "forecast_min_outside_c": rounded(outside_min, 1),
            "reference_inside_c": rounded(lower_c, 1),
            "impact_level": "learning",
            "basis": "insufficient_prediction_ready_passive_data",
            "hdo_only_preheat_indication": "No automatic HDO-only preheating recommendation yet.",
            "principle": "When outdoor temperature can fall below the lower threshold, HDO preheating is not recommended until the passive model is prediction-ready.",
        }

    k_mid = as_float(fit.get("coupling_per_h"))
    k_fast = as_float(fit.get("coupling_p75_per_h")) or k_mid
    if k_mid is None or k_mid <= 0:
        return None

    def passive_drop(k):
        if k is None or k <= 0 or outside_min >= lower_c:
            return 0.0
        end_c = outside_min + (lower_c - outside_min) * math.exp(-k * conservative_unavailability_h)
        return max(0.0, lower_c - end_c)

    central_drop = passive_drop(k_mid)
    faster_drop = passive_drop(k_fast)
    conservative_drop = max(central_drop, faster_drop)

    if conservative_drop < 0.2:
        level = "negligible"
        advice = "No HDO-only preheating is indicated; the building inertia should comfortably bridge the longest VT block."
    elif conservative_drop < 0.5:
        level = "small"
        advice = "HDO-only preheating is usually unnecessary; re-evaluate only near the lower thermostat threshold."
    else:
        level = "material"
        advice = "The HDO block can cause a material drop near the lower threshold; evaluate limited preheating within the 6–8 °C band."

    return {
        "horizon_hours": 24,
        "longest_vt_block_h": rounded(longest_vt_h, 2),
        "conservative_unavailability_h": rounded(conservative_unavailability_h, 2),
        "forecast_min_outside_c": rounded(outside_min, 1),
        "reference_inside_c": rounded(lower_c, 1),
        "passive_drop_during_longest_vt_central_c": rounded(central_drop, 2),
        "passive_drop_during_longest_vt_conservative_c": rounded(conservative_drop, 2),
        "impact_level": level,
        "hdo_only_preheat_indication": advice,
        "principle": "Preheating solely because VT is approaching is avoided unless the learned passive model predicts a meaningful temperature drop during the block.",
    }


def build_heating_energy_model(latest, fit=None, prediction_ready=False):
    profile, state = load_house_energy_context()
    cfg = profile.get("heating_energy_model") if isinstance(profile, dict) else {}
    pricing = state.get("energy_pricing") if isinstance(state, dict) else {}
    boiler = ((profile.get("heating_system") or {}).get("primary_electric_boiler") or {}) if isinstance(profile, dict) else {}
    if not isinstance(cfg, dict) or not isinstance(pricing, dict) or not isinstance(boiler, dict):
        return None

    target_c = as_float(cfg.get("tempering_setpoint_c"))
    hysteresis_c = as_float(cfg.get("thermostat_hysteresis_c"))
    price = as_float(pricing.get("tempering_incremental_price_czk_per_kwh"))
    current_prices = pricing.get("current_variable_price_czk_per_kwh_vat") if isinstance(pricing.get("current_variable_price_czk_per_kwh_vat"), dict) else {}
    high_tariff_price = as_float(current_prices.get("high_tariff"))
    efficiency_pct = as_float(boiler.get("label_efficiency_percent"))
    prior = boiler_sized_heat_loss_prior(profile)
    if target_c is None or price is None or prior is None:
        return None
    efficiency = (efficiency_pct / 100.0) if efficiency_pct and efficiency_pct > 0 else 1.0

    start_dt = parse_dt(latest.get("last_timestamp") or latest.get("generated_at")) if isinstance(latest, dict) else None
    start_local = (start_dt.astimezone(TZ) if start_dt else datetime.now(TZ))
    snapshot = load_latest_forecast_snapshot()
    hdo_context = build_hdo_context(start_local, 24)
    degree_by_model = forecast_degree_hours(snapshot, start_local, target_c, 24)
    dh_values = [as_float(item.get("degree_hours_kh")) for item in degree_by_model.values()]
    dh_values = [value for value in dh_values if value is not None]

    forecast = None
    if dh_values:
        heat_loss = prior["w_per_k"]
        dh_low = min(dh_values)
        dh_mid = median(dh_values)
        dh_high = max(dh_values)
        e_low = energy_from_degree_hours(dh_low, as_float(heat_loss["low"]), efficiency)
        e_mid = energy_from_degree_hours(dh_mid, as_float(heat_loss["central"]), efficiency)
        e_high = energy_from_degree_hours(dh_high, as_float(heat_loss["high"]), efficiency)
        forecast = {
            "horizon_hours": 24,
            "start_timestamp_local": start_local.isoformat(),
            "weather_models": sorted(degree_by_model),
            "degree_hours_by_model": degree_by_model,
            "degree_hours_median_kh": rounded(dh_mid, 2),
            "electricity_kwh": {
                "low": rounded(e_low, 1),
                "central": rounded(e_mid, 1),
                "high": rounded(e_high, 1),
            },
            "incremental_cost_czk": {
                "low": rounded(e_low * price, 0),
                "central": rounded(e_mid * price, 0),
                "high": rounded(e_high * price, 0),
            },
            "scope": f"steady-state reference energy to maintain approximately {target_c:.1f} °C over 24 h after the house is already in the tempering band; not actual next-24h consumption and not direct metering",
        }

    references = []
    heat_loss = prior["w_per_k"]
    for outside_c in (5.0, 0.0, -5.0, -10.0, -15.0):
        dh = max(0.0, target_c - outside_c) * 24.0
        e_low = energy_from_degree_hours(dh, as_float(heat_loss["low"]), efficiency)
        e_mid = energy_from_degree_hours(dh, as_float(heat_loss["central"]), efficiency)
        e_high = energy_from_degree_hours(dh, as_float(heat_loss["high"]), efficiency)
        references.append({
            "outside_c": outside_c,
            "electricity_kwh_per_day": {
                "low": rounded(e_low, 1),
                "central": rounded(e_mid, 1),
                "high": rounded(e_high, 1),
            },
            "cost_czk_per_day": {
                "low": rounded(e_low * price, 0),
                "central": rounded(e_mid * price, 0),
                "high": rounded(e_high * price, 0),
            },
        })

    band = None
    if hysteresis_c is not None:
        band = [rounded(target_c - hysteresis_c, 1), rounded(target_c + hysteresis_c, 1)]

    actual_need = build_actual_tempering_need(
        latest,
        snapshot,
        start_local,
        fit,
        prediction_ready,
        band,
        price,
    )

    tariff_policy = (((cfg.get("optimization_policy") or {}).get("tariff_control") or {}) if isinstance(cfg, dict) else {})
    extra_nt_block_h = as_float((((tariff_policy.get("additional_nt_heating_blocking") or {}).get("max_single_block_hours")))) or 0.0

    hdo_resilience = build_hdo_resilience(
        hdo_context,
        snapshot,
        start_local,
        fit,
        band,
        prediction_ready,
        extra_nt_block_h,
    )

    return {
        "status": "provisional_physics_prior",
        "absolute_energy_scale_calibrated": False,
        "tempering_setpoint_c": target_c,
        "thermostat_hysteresis_c": hysteresis_c,
        "expected_thermostat_band_c": band,
        "boiler_efficiency_fraction": rounded(efficiency, 4),
        "price_czk_per_kwh": price,
        "high_tariff_price_czk_per_kwh": high_tariff_price,
        "price_basis": pricing.get("tempering_price_basis"),
        "distribution_rate": pricing.get("distribution_rate"),
        "hdo_boiler_blocking": pricing.get("hdo_boiler_blocking"),
        "heat_loss_prior": prior,
        "steady_state_maintenance_24h": forecast,
        "forecast_next_24h": forecast,
        "actual_tempering_need_next_24h": actual_need,
        "hdo_next_24h": hdo_context,
        "hdo_resilience": hdo_resilience,
        "reference_daily_costs": references,
        "learning_note": "Weather and passive cooling are measured. The absolute kWh scale is still a boiler-sizing prior and will be narrowed when cold-season heating response provides calibration evidence.",
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

    prediction_ready = bool(
        fit
        and fit_basis is night
        and len(night) >= 6
        and coverage_days is not None
        and coverage_days >= 5
    )
    prediction_blocker = None
    if not prediction_ready:
        if len(night) < 6:
            prediction_blocker = f"need at least 6 accepted nighttime windows; have {len(night)}"
        elif coverage_days is None or coverage_days < 5:
            prediction_blocker = f"need at least 5 days of paired coverage; have {rounded(coverage_days, 2)}"
        else:
            prediction_blocker = "passive fit is not based on nighttime windows"

    passive_forecast = build_passive_forecast(latest, fit) if prediction_ready else None
    heating_energy_model = build_heating_energy_model(latest, fit, prediction_ready)
    indoor_points = indoor_payload.get("points") or []

    output = {
        "schema": 5,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "validated_for_heating_control": False,
        "prediction_ready": prediction_ready,
        "prediction_blocker": prediction_blocker,
        "dp2_used_as_heating_state": False,
        "analysis_window_hours": WINDOW_HOURS,
        "fit_basis": "nighttime windows" if fit_basis is night else "all passive windows fallback",
        "coverage_days": rounded(coverage_days, 2),
        "indoor_raw_points": len(indoor_points),
        "indoor_history_first_timestamp": indoor_points[0].get("timestamp_local") if indoor_points else None,
        "indoor_history_last_timestamp": indoor_points[-1].get("timestamp_local") if indoor_points else None,
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
        "passive_forecast": passive_forecast,
        "heating_energy_model": heating_energy_model,
        "filters": {
            "bucket_minutes": 30,
            "window_hours": WINDOW_HOURS,
            "minimum_inside_minus_outside_c": 2.0,
            "setpoint_margin_c": 1.5,
            "maximum_allowed_warming_c_per_h": 0.08,
            "maximum_single_30m_rise_c": 0.2,
            "maximum_cumulative_positive_rise_per_window_c": 0.4,
            "night_midpoint_hours_local": "21:00-05:59",
            "rejected": rejected,
        },
        "limitations": [
            "DP2 semantics are not independently verified, so it is not treated as boiler or relay state.",
            "The passive fit preferentially uses nighttime 6-hour windows to reduce solar-gain and 0.1 °C sensor-quantization noise.",
            "Windows with strong short-term indoor warming are excluded to reduce contamination from wood-stove heat, solar gain and occupants.",
            "Wood-stove heat is not independently measured yet; the filter detects heat-gain signatures rather than proving their source.",
            "DWD Sohland is preferred for historical outdoor temperature because it is closer; ČHMÚ Varnsdorf fills missing periods.",
            "A numeric passive forecast is withheld until there are at least 6 accepted nighttime windows and 5 days of paired coverage.",
            "The fit is observational and preliminary; it must not control heating until heating-state evidence and more seasonal data exist."
        ],
    }
    (DATA / "indoor-thermal-model.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "schema": 5,
        "status": status,
        "fit_basis": output["fit_basis"],
        "fit_samples": len(fit_basis),
        "coverage_days": output["coverage_days"],
        "fit": fit,
        "current": current,
        "passive_forecast_horizons": None if passive_forecast is None else passive_forecast.get("horizons"),
        "heating_energy_forecast": None if heating_energy_model is None else heating_energy_model.get("forecast_next_24h"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
