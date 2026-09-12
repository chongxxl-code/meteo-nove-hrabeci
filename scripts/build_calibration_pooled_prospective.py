#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import (
    CAL,
    DATA,
    FORECAST_FIELDS,
    build_bias_tables,
    correction_for,
    history_cases,
    lead_bin,
    live_cases,
    load_jsonl_dir,
    parse_dt,
)
from build_calibration_ml_shadow import finite
from build_calibration_ml_walkforward import truth_available_at

CANDIDATE_ID = "pooled_squared_v1"
CANDIDATE_FROZEN_AT_UTC = "2026-09-12T19:40:24Z"
FROZEN_MODEL_LEVELS = ("chmi", "dwd", "ec")
FROZEN_FEATURE_NAMES = [
    "model_chmi",
    "model_dwd",
    "model_ec",
    "lead_h",
    "temperature_c",
    "relative_humidity_pct",
    "dew_point_c",
    "dewpoint_depression_c",
    "precipitation_mm",
    "pressure_msl_hpa",
    "cloud_cover_pct",
    "wind_speed_10m_kmh",
    "wind_gusts_10m_kmh",
    "cape_jkg",
    "weather_code",
    "wind_direction_sin",
    "wind_direction_cos",
    "target_hour_sin",
    "target_hour_cos",
    "target_doy_sin",
    "target_doy_cos",
]
FROZEN_MODEL_PARAMS = {
    "loss": "squared_error",
    "learning_rate": 0.05,
    "max_iter": 200,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": 42,
}
DESIRED_LEADS_H = (1, 3, 6, 12, 24, 36, 48, 60, 72)
MAX_LEAD_DISTANCE_H = 1.1
MIN_TRAIN_CASES = 5000
MIN_FEATURE_FINITE_FRACTION = 0.01
MIN_FEATURE_FINITE_N = 30


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def skip(reason, **extra):
    print(json.dumps({"ok": True, "skipped": True, "reason": reason, **extra}, ensure_ascii=False))


def value_or_nan(value):
    return float(value) if finite(value) else float("nan")


def cyclical(value, period):
    if value is None:
        return float("nan"), float("nan")
    angle = 2.0 * math.pi * float(value) / float(period)
    return math.sin(angle), math.cos(angle)


def frozen_case_features(case):
    forecast = case.get("forecast") or {}
    model = case.get("model")
    target = parse_dt(case.get("target_at_utc"))

    temperature = value_or_nan(forecast.get("temperature_c"))
    dewpoint = value_or_nan(forecast.get("dew_point_c"))
    depression = (
        temperature - dewpoint
        if math.isfinite(temperature) and math.isfinite(dewpoint)
        else float("nan")
    )

    wind_direction = forecast.get("wind_direction_10m_deg")
    wind_sin, wind_cos = (
        cyclical(wind_direction, 360.0)
        if finite(wind_direction)
        else (float("nan"), float("nan"))
    )
    if target is not None:
        hour_sin, hour_cos = cyclical(target.hour + target.minute / 60.0, 24.0)
        doy_sin, doy_cos = cyclical(target.timetuple().tm_yday, 365.2425)
    else:
        hour_sin = hour_cos = doy_sin = doy_cos = float("nan")

    return [
        1.0 if model == level else 0.0 for level in FROZEN_MODEL_LEVELS
    ] + [
        value_or_nan(case.get("lead_h")),
        temperature,
        value_or_nan(forecast.get("relative_humidity_pct")),
        dewpoint,
        depression,
        value_or_nan(forecast.get("precipitation_mm")),
        value_or_nan(forecast.get("pressure_msl_hpa")),
        value_or_nan(forecast.get("cloud_cover_pct")),
        value_or_nan(forecast.get("wind_speed_10m_kmh")),
        value_or_nan(forecast.get("wind_gusts_10m_kmh")),
        value_or_nan(forecast.get("cape_jkg")),
        value_or_nan(forecast.get("weather_code")),
        wind_sin,
        wind_cos,
        hour_sin,
        hour_cos,
        doy_sin,
        doy_cos,
    ]


def latest_snapshot():
    snapshots = load_jsonl_dir(DATA / "archive", "*.jsonl")
    ranked = []
    for snapshot in snapshots:
        stamp = parse_dt(snapshot.get("collected_at_utc"))
        if stamp is not None and snapshot.get("models"):
            ranked.append((stamp, snapshot))
    if not ranked:
        return None, None
    return max(ranked, key=lambda item: item[0])


def build_future_cases(snapshot, issued):
    candidates = []
    for model, payload in (snapshot.get("models") or {}).items():
        if model not in FROZEN_MODEL_LEVELS:
            continue
        times = payload.get("time") or []
        temperatures = payload.get("temperature_2m") or []
        for index, raw_target in enumerate(times):
            if index >= len(temperatures) or not finite(temperatures[index]):
                continue
            target = parse_dt(raw_target)
            if target is None:
                continue
            lead_h = (target - issued).total_seconds() / 3600.0
            bucket = lead_bin(lead_h)
            if bucket is None or lead_h <= 0:
                continue
            forecast = {}
            for source_field, target_field in FORECAST_FIELDS.items():
                values = payload.get(source_field) or []
                forecast[target_field] = values[index] if index < len(values) else None
            candidates.append(
                {
                    "source": "prospective_snapshot",
                    "model": model,
                    "issued_at_utc": iso_utc(issued),
                    "target_at_utc": iso_utc(target),
                    "lead_h": round(lead_h, 3),
                    "lead_bin": bucket,
                    "forecast": forecast,
                }
            )

    selected = []
    for model in FROZEN_MODEL_LEVELS:
        model_cases = [case for case in candidates if case["model"] == model]
        used_targets = set()
        for desired in DESIRED_LEADS_H:
            if not model_cases:
                continue
            case = min(model_cases, key=lambda item: abs(float(item["lead_h"]) - desired))
            distance = abs(float(case["lead_h"]) - desired)
            if distance > MAX_LEAD_DISTANCE_H or case["target_at_utc"] in used_targets:
                continue
            selected.append(case)
            used_targets.add(case["target_at_utc"])
    selected.sort(key=lambda case: (case["target_at_utc"], case["model"]))
    return selected


def existing_keys(path):
    keys = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("candidate") != CANDIDATE_ID:
                continue
            keys.add((row.get("issued_at_utc"), row.get("model"), row.get("target_at_utc")))
        except Exception:
            continue
    return keys


def fit_frozen_candidate(train, future):
    import numpy as np
    import sklearn
    from sklearn.ensemble import HistGradientBoostingRegressor

    x_train = np.asarray([frozen_case_features(case) for case in train], dtype=float)
    y_train = np.asarray([float(case["error_c"]) for case in train], dtype=float)
    x_future = np.asarray([frozen_case_features(case) for case in future], dtype=float)

    minimum = max(MIN_FEATURE_FINITE_N, int(len(train) * MIN_FEATURE_FINITE_FRACTION))
    finite_counts = np.isfinite(x_train).sum(axis=0)
    keep_mask = finite_counts >= minimum
    if not np.any(keep_mask):
        raise RuntimeError("no usable frozen candidate features after coverage filtering")

    used = [name for name, keep in zip(FROZEN_FEATURE_NAMES, keep_mask) if bool(keep)]
    dropped = [
        {"feature": name, "finite_train_n": int(count)}
        for name, count, keep in zip(FROZEN_FEATURE_NAMES, finite_counts, keep_mask)
        if not bool(keep)
    ]

    model = HistGradientBoostingRegressor(**FROZEN_MODEL_PARAMS)
    model.fit(x_train[:, keep_mask], y_train)
    predictions = model.predict(x_future[:, keep_mask])
    return predictions, used, dropped, minimum, sklearn.__version__


def main():
    try:
        import sklearn  # noqa: F401
    except Exception as exc:
        skip("ML dependency unavailable for frozen prospective candidate", error=str(exc)[:300])
        return

    issued, snapshot = latest_snapshot()
    if issued is None or snapshot is None:
        skip("no forecast snapshot available")
        return

    future = build_future_cases(snapshot, issued)
    if not future:
        skip("latest snapshot has no usable future cases in requested lead set")
        return

    history, history_storage = history_cases(return_storage=True)
    live = live_cases()
    train = []
    for case in history + live:
        if case.get("model") not in FROZEN_MODEL_LEVELS:
            continue
        case_issue = parse_dt(case.get("issued_at_utc"))
        available = truth_available_at(case)
        if case_issue is None or case_issue > issued:
            continue
        if available is None or available > issued:
            continue
        train.append(case)
    train.sort(
        key=lambda case: (
            case["target_at_utc"], case["model"], case["lead_h"], case["source"]
        )
    )
    if len(train) < MIN_TRAIN_CASES:
        skip("insufficient leakage-safe prospective training cases", training_cases=len(train))
        return

    try:
        predictions, features_used, features_dropped, finite_minimum, sklearn_version = (
            fit_frozen_candidate(train, future)
        )
    except Exception as exc:
        skip("frozen prospective candidate could not be fitted", error=f"{type(exc).__name__}: {exc}"[:500])
        return

    bias_tables = build_bias_tables(train)
    month = issued.strftime("%Y-%m")
    out = CAL / f"prospective-pooled-{month}.jsonl"
    known = existing_keys(out)
    rows = []

    for case, predicted_error in zip(future, predictions):
        key = (case["issued_at_utc"], case["model"], case["target_at_utc"])
        if key in known:
            continue
        raw_temperature = float(case["forecast"]["temperature_c"])
        deterministic_correction, correction_level, correction_n = correction_for(case, bias_tables)
        row = {
            "schema": 1,
            "shadow_only": True,
            "allowed_to_affect_public_forecast": False,
            "allowed_to_affect_alerts": False,
            "candidate": CANDIDATE_ID,
            "candidate_frozen_at_utc": CANDIDATE_FROZEN_AT_UTC,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_commit": os.environ.get("GITHUB_SHA"),
            "model": case["model"],
            "issued_at_utc": case["issued_at_utc"],
            "target_at_utc": case["target_at_utc"],
            "lead_h": case["lead_h"],
            "lead_bin": case["lead_bin"],
            "raw_temperature_c": round(raw_temperature, 3),
            "deterministic_correction_c": round(float(deterministic_correction), 3),
            "deterministic_temperature_c": round(raw_temperature - float(deterministic_correction), 3),
            "ml_predicted_error_c": round(float(predicted_error), 3),
            "ml_temperature_c": round(raw_temperature - float(predicted_error), 3),
            "training": {
                "candidate_policy": "frozen architecture; expanding leakage-safe training window",
                "as_of_utc": case["issued_at_utc"],
                "cases": len(train),
                "first_target_utc": train[0]["target_at_utc"],
                "last_target_utc": train[-1]["target_at_utc"],
                "history_storage_files": history_storage,
                "features_used": features_used,
                "features_dropped": features_dropped,
                "minimum_finite_training_cases_per_feature": finite_minimum,
                "model_params": FROZEN_MODEL_PARAMS,
                "sklearn_version": sklearn_version,
                "deterministic_correction_level": correction_level,
                "deterministic_correction_training_n": correction_n,
                "leakage_guard": "training truth observation must be available no later than forecast issuance",
            },
            "truth": None,
        }
        rows.append(row)

    if rows:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(
        json.dumps(
            {
                "ok": True,
                "candidate": CANDIDATE_ID,
                "candidate_frozen_at_utc": CANDIDATE_FROZEN_AT_UTC,
                "snapshot_issued_at_utc": iso_utc(issued),
                "training_cases": len(train),
                "future_cases_selected": len(future),
                "new_predictions_written": len(rows),
                "output": str(out.relative_to(Path(__file__).resolve().parents[1])),
                "features_used": len(features_used),
                "features_dropped": [item["feature"] for item in features_dropped],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
