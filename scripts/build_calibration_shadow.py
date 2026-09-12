#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from calibration_truth import (
    DWD_DISTANCE_KM,
    DWD_MATCH_MINUTES,
    DWD_STATION_ID,
    DWD_STATION_NAME,
    load_local_dwd_truth,
    nearest_truth,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CAL = DATA / "calibration"
LEGACY_HISTORY = CAL / "history-v0.jsonl"
OUT = CAL / "shadow-summary.json"
TZ = ZoneInfo("Europe/Prague")

MIN_GROUP_N = 30
TRAIN_FRACTION = 0.75

FORECAST_FIELDS = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "relative_humidity_pct",
    "dew_point_2m": "dew_point_c",
    "precipitation": "precipitation_mm",
    "cloud_cover": "cloud_cover_pct",
    "pressure_msl": "pressure_msl_hpa",
    "wind_speed_10m": "wind_speed_10m_kmh",
    "wind_direction_10m": "wind_direction_10m_deg",
    "wind_gusts_10m": "wind_gusts_10m_kmh",
    "cape": "cape_jkg",
    "weather_code": "weather_code",
}


def parse_dt(value):
    if not value:
        return None
    raw = str(value)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(timezone.utc)


def finite(value):
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def load_jsonl(path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_jsonl_dir(folder, pattern):
    rows = []
    if not folder.exists():
        return rows
    for path in sorted(folder.glob(pattern)):
        rows.extend(load_jsonl(path))
    return rows


def load_history_rows():
    rows = []
    shards = sorted(CAL.glob("history-????-??.jsonl.gz"))
    if shards:
        for path in shards:
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                continue
        return rows, [path.name for path in shards]
    return load_jsonl(LEGACY_HISTORY), ([LEGACY_HISTORY.name] if LEGACY_HISTORY.exists() else [])


def lead_bin(hours):
    if hours < 0:
        return None
    if hours <= 6:
        return "0–6 h"
    if hours <= 12:
        return "6–12 h"
    if hours <= 24:
        return "12–24 h"
    if hours <= 48:
        return "24–48 h"
    if hours <= 72:
        return "48–72 h"
    return None


def hour_block(target):
    hour = target.hour
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def live_cases():
    snapshots = load_jsonl_dir(DATA / "archive", "*.jsonl")
    dwd_truth, dwd_times = load_local_dwd_truth(DATA / "observations")
    now = datetime.now(timezone.utc)
    cases = []

    for snapshot in snapshots:
        issued = parse_dt(snapshot.get("collected_at_utc"))
        if issued is None:
            continue
        for model, payload in (snapshot.get("models") or {}).items():
            times = payload.get("time") or []
            temps = payload.get("temperature_2m") or []
            for index, raw_target in enumerate(times):
                if index >= len(temps) or temps[index] is None:
                    continue
                target = parse_dt(raw_target)
                if target is None or target > now:
                    continue
                lead_h = (target - issued).total_seconds() / 3600
                bucket = lead_bin(lead_h)
                if bucket is None:
                    continue

                matched = nearest_truth(target, dwd_truth, dwd_times)
                if matched is None:
                    continue
                observed_at, obs, match_delta = matched

                forecast_temp = float(temps[index])
                forecast = {}
                for source_field, target_field in FORECAST_FIELDS.items():
                    values = payload.get(source_field) or []
                    value = values[index] if index < len(values) else None
                    forecast[target_field] = value

                cases.append({
                    "source": "local_snapshot",
                    "model": model,
                    "issued_at_utc": issued.isoformat().replace("+00:00", "Z"),
                    "target_at_utc": target.isoformat().replace("+00:00", "Z"),
                    "lead_h": round(lead_h, 3),
                    "lead_bin": bucket,
                    "hour_block": hour_block(target),
                    "forecast": forecast,
                    "truth_temperature_c": float(obs["temperature_c"]),
                    "truth_source": "dwd_sohland_10min",
                    "truth_station": DWD_STATION_NAME,
                    "truth_station_distance_km": DWD_DISTANCE_KM,
                    "truth_observed_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
                    "truth_match_delta_minutes": round(match_delta, 1),
                    "is_nove_hrabeci_truth": False,
                    "error_c": forecast_temp - float(obs["temperature_c"]),
                })
    return cases


def history_cases(return_storage=False):
    cases = []
    rows, storage = load_history_rows()
    for row in rows:
        target = parse_dt(row.get("target_at_utc"))
        forecast = row.get("forecast") or {}
        truth = row.get("truth") or {}
        temp = forecast.get("temperature_c")
        observed = truth.get("temperature_c")
        lead_h = float(row.get("lead_h") or 0)
        bucket = lead_bin(lead_h)
        if target is None or bucket is None or not finite(temp) or not finite(observed):
            continue
        cases.append({
            "source": "open_meteo_previous_runs",
            "model": row.get("model"),
            "issued_at_utc": row.get("issued_at_utc"),
            "target_at_utc": row.get("target_at_utc"),
            "lead_h": lead_h,
            "lead_bin": bucket,
            "hour_block": hour_block(target),
            "forecast": forecast,
            "truth_temperature_c": float(observed),
            "truth_source": "dwd_sohland_10min",
            "truth_station": truth.get("station_name"),
            "truth_station_distance_km": truth.get("distance_to_nove_hrabeci_km"),
            "truth_observed_at_utc": truth.get("observed_at_utc"),
            "truth_match_delta_minutes": truth.get("match_delta_minutes"),
            "is_nove_hrabeci_truth": False,
            "error_c": float(temp) - float(observed),
        })
    if return_storage:
        return cases, storage
    return cases


def mean(values):
    return sum(values) / len(values) if values else None


def mae(values):
    return mean([abs(v) for v in values]) if values else None


def rounded(value, digits=3):
    return None if value is None or not math.isfinite(value) else round(value, digits)


def chronological_split(cases):
    targets = sorted({case["target_at_utc"] for case in cases})
    if len(targets) < 8:
        return [], cases, None
    cut_index = min(len(targets) - 1, max(1, int(len(targets) * TRAIN_FRACTION)))
    cutoff = targets[cut_index]
    train = [case for case in cases if case["target_at_utc"] < cutoff]
    test = [case for case in cases if case["target_at_utc"] >= cutoff]
    return train, test, cutoff


def build_bias_tables(train):
    tables = {
        "regime": defaultdict(list),
        "lead": defaultdict(list),
        "model": defaultdict(list),
        "global": [],
    }
    for case in train:
        err = case["error_c"]
        model = case["model"]
        lead = case["lead_bin"]
        block = case["hour_block"]
        tables["regime"][(model, lead, block)].append(err)
        tables["lead"][(model, lead)].append(err)
        tables["model"][model].append(err)
        tables["global"].append(err)
    return tables


def correction_for(case, tables):
    options = [
        ("model+lead+daypart", tables["regime"].get((case["model"], case["lead_bin"], case["hour_block"]), [])),
        ("model+lead", tables["lead"].get((case["model"], case["lead_bin"]), [])),
        ("model", tables["model"].get(case["model"], [])),
        ("global", tables["global"]),
    ]
    for label, values in options:
        threshold = MIN_GROUP_N if label != "global" else 1
        if len(values) >= threshold:
            return mean(values), label, len(values)
    return 0.0, "none", 0


def evaluate(train, test):
    tables = build_bias_tables(train)
    rows = []
    for case in test:
        raw_error = case["error_c"]
        correction, level, n = correction_for(case, tables)
        corrected_error = raw_error - correction
        rows.append({
            **case,
            "raw_error_c": raw_error,
            "correction_c": correction,
            "corrected_error_c": corrected_error,
            "correction_level": level,
            "correction_training_n": n,
        })

    def metrics(subset):
        raw = [row["raw_error_c"] for row in subset]
        corrected = [row["corrected_error_c"] for row in subset]
        raw_mae = mae(raw)
        corrected_mae = mae(corrected)
        improvement = None
        if raw_mae and raw_mae > 0 and corrected_mae is not None:
            improvement = (raw_mae - corrected_mae) / raw_mae * 100
        return {
            "n": len(subset),
            "raw_mae_c": rounded(raw_mae, 3),
            "raw_bias_c": rounded(mean(raw), 3),
            "bias_corrected_mae_c": rounded(corrected_mae, 3),
            "bias_corrected_bias_c": rounded(mean(corrected), 3),
            "mae_improvement_pct": rounded(improvement, 1),
        }

    by_model = {}
    for model in sorted({row["model"] for row in rows if row.get("model")}):
        by_model[model] = metrics([row for row in rows if row["model"] == model])

    by_source = {}
    for source in sorted({row["source"] for row in rows}):
        by_source[source] = metrics([row for row in rows if row["source"] == source])

    by_lead = {}
    for bucket in ("0–6 h", "6–12 h", "12–24 h", "24–48 h", "48–72 h"):
        subset = [row for row in rows if row["lead_bin"] == bucket]
        if subset:
            by_lead[bucket] = metrics(subset)

    return rows, metrics(rows), by_model, by_source, by_lead


def main():
    CAL.mkdir(parents=True, exist_ok=True)
    history, history_storage = history_cases(return_storage=True)
    live = live_cases()
    cases = history + live
    if not cases:
        payload = {
            "schema": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "error": "No calibration cases available.",
            "shadow_only": True,
        }
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return

    cases.sort(key=lambda case: (case["target_at_utc"], case["model"], case["lead_h"], case["source"]))
    train, test, cutoff = chronological_split(cases)
    evaluated, overall, by_model, by_source, by_lead = evaluate(train, test)

    useful = overall["n"] >= 200 and overall["mae_improvement_pct"] is not None
    summary = {
        "schema": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "target_truth": {
            "primary": "DWD Sohland/Spree 10-minute",
            "station_id": DWD_STATION_ID,
            "distance_to_nove_hrabeci_km": DWD_DISTANCE_KM,
            "matching_tolerance_minutes": DWD_MATCH_MINUTES,
            "matching_policy": "same canonical nearest-UTC matcher for historical and live calibration cases; no CHMI fallback",
            "is_nove_hrabeci_truth": False,
            "warning": "Benchmark measures correction against nearby proxy observations, not an on-site Nové Hraběcí station.",
        },
        "method": {
            "split": "chronological by target time; no random shuffle",
            "train_fraction_target_times": TRAIN_FRACTION,
            "cutoff_target_utc": cutoff,
            "bias_hierarchy": [
                f"model + lead bin + daypart (minimum {MIN_GROUP_N} training cases)",
                f"model + lead bin (minimum {MIN_GROUP_N})",
                f"model (minimum {MIN_GROUP_N})",
                "global mean error fallback",
            ],
            "correction_formula": "corrected_temperature = raw_temperature - mean_training_error",
            "note": "This is a deterministic benchmark, not ML. It exists to give any later ML model a baseline it must beat.",
        },
        "coverage": {
            "history_backfill_cases": len(history),
            "history_storage_files": history_storage,
            "live_archive_cases": len(live),
            "all_cases": len(cases),
            "train_cases": len(train),
            "test_cases": len(test),
            "first_target_utc": cases[0]["target_at_utc"],
            "last_target_utc": cases[-1]["target_at_utc"],
        },
        "test_metrics": {
            "overall": overall,
            "by_model": by_model,
            "by_source": by_source,
            "by_lead_bin": by_lead,
        },
        "readiness": {
            "enough_cases_for_useful_shadow_benchmark": useful,
            "production_eligible": False,
            "next_gate": "Compare deterministic bias baseline with ML v1 on the same later chronological holdout; on-site NH station is still required before production use.",
        },
        "recent_shadow_examples": [
            {
                "model": row["model"],
                "source": row["source"],
                "target_at_utc": row["target_at_utc"],
                "lead_h": row["lead_h"],
                "raw_temperature_c": row["forecast"].get("temperature_c"),
                "observed_temperature_c": row["truth_temperature_c"],
                "truth_observed_at_utc": row.get("truth_observed_at_utc"),
                "truth_match_delta_minutes": row.get("truth_match_delta_minutes"),
                "correction_c": rounded(row["correction_c"], 2),
                "corrected_temperature_c": rounded(
                    float(row["forecast"].get("temperature_c")) - row["correction_c"], 2
                ),
                "raw_error_c": rounded(row["raw_error_c"], 2),
                "corrected_error_c": rounded(row["corrected_error_c"], 2),
                "correction_level": row["correction_level"],
            }
            for row in sorted(evaluated, key=lambda item: item["target_at_utc"], reverse=True)[:12]
        ],
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["coverage"], ensure_ascii=False))
    print(json.dumps(summary["test_metrics"]["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
