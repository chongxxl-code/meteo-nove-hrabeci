#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import (
    DWD_DISTANCE_KM,
    DWD_STATION_ID,
    DWD_STATION_NAME,
    chronological_split,
    evaluate,
    history_cases,
    live_cases,
    parse_dt,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "ml-v1-shadow.json"

MODEL_LEVELS = ("chmi", "dwd", "ec")
FEATURE_NAMES = [
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

MODEL_PARAMS = {
    "loss": "absolute_error",
    "learning_rate": 0.05,
    "max_iter": 200,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": 42,
}


def finite(value):
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def value_or_nan(value):
    return float(value) if finite(value) else float("nan")


def cyclical(value, period):
    if value is None:
        return float("nan"), float("nan")
    angle = 2.0 * math.pi * float(value) / float(period)
    return math.sin(angle), math.cos(angle)


def case_features(case):
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
    wind_sin, wind_cos = cyclical(wind_direction, 360.0) if finite(wind_direction) else (float("nan"), float("nan"))
    if target is not None:
        hour_sin, hour_cos = cyclical(target.hour + target.minute / 60.0, 24.0)
        doy_sin, doy_cos = cyclical(target.timetuple().tm_yday, 365.2425)
    else:
        hour_sin = hour_cos = doy_sin = doy_cos = float("nan")

    return [
        1.0 if model == level else 0.0 for level in MODEL_LEVELS
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


def mean(values):
    return sum(values) / len(values) if values else None


def mae(values):
    return mean([abs(value) for value in values]) if values else None


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


def improvement_pct(reference, candidate):
    if not finite(reference) or float(reference) <= 0 or not finite(candidate):
        return None
    return (float(reference) - float(candidate)) / float(reference) * 100.0


def metrics(rows):
    raw = [row["raw_error_c"] for row in rows]
    deterministic = [row["deterministic_error_c"] for row in rows]
    ml = [row["ml_error_c"] for row in rows]
    raw_mae = mae(raw)
    deterministic_mae = mae(deterministic)
    ml_mae = mae(ml)
    return {
        "n": len(rows),
        "raw_mae_c": rounded(raw_mae),
        "deterministic_mae_c": rounded(deterministic_mae),
        "ml_mae_c": rounded(ml_mae),
        "raw_bias_c": rounded(mean(raw)),
        "deterministic_bias_c": rounded(mean(deterministic)),
        "ml_bias_c": rounded(mean(ml)),
        "deterministic_improvement_vs_raw_pct": rounded(improvement_pct(raw_mae, deterministic_mae), 1),
        "ml_improvement_vs_raw_pct": rounded(improvement_pct(raw_mae, ml_mae), 1),
        "ml_improvement_vs_deterministic_pct": rounded(improvement_pct(deterministic_mae, ml_mae), 1),
    }


def season_name(target_at_utc):
    target = parse_dt(target_at_utc)
    if target is None:
        return "unknown"
    if target.month in (12, 1, 2):
        return "winter_DJF"
    if target.month in (3, 4, 5):
        return "spring_MAM"
    if target.month in (6, 7, 8):
        return "summer_JJA"
    return "autumn_SON"


def write_error(message):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": False,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "error": str(message)[:1000],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


def main():
    try:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
        import sklearn
    except Exception as exc:
        write_error(f"ML dependency unavailable: {exc}")
        return

    try:
        history, history_storage = history_cases(return_storage=True)
        live = live_cases()
        cases = history + live
        cases.sort(key=lambda case: (case["target_at_utc"], case["model"], case["lead_h"], case["source"]))
        train, test, cutoff = chronological_split(cases)
        if len(train) < 500 or len(test) < 200:
            write_error(f"Not enough chronologically split cases: train={len(train)}, test={len(test)}")
            return

        deterministic_rows, deterministic_overall, _, _, _ = evaluate(train, test)

        x_train = np.asarray([case_features(case) for case in train], dtype=float)
        y_train = np.asarray([float(case["error_c"]) for case in train], dtype=float)
        x_test = np.asarray([case_features(case) for case in test], dtype=float)

        model = HistGradientBoostingRegressor(**MODEL_PARAMS)
        model.fit(x_train, y_train)
        predicted_error = model.predict(x_test)

        evaluated = []
        for case, deterministic_row, prediction in zip(test, deterministic_rows, predicted_error):
            raw_error = float(case["error_c"])
            deterministic_error = float(deterministic_row["corrected_error_c"])
            ml_error = raw_error - float(prediction)
            evaluated.append({
                **case,
                "raw_error_c": raw_error,
                "deterministic_error_c": deterministic_error,
                "deterministic_correction_c": float(deterministic_row["correction_c"]),
                "predicted_error_c": float(prediction),
                "ml_error_c": ml_error,
            })

        overall = metrics(evaluated)
        by_model = {
            key: metrics([row for row in evaluated if row.get("model") == key])
            for key in sorted({row.get("model") for row in evaluated if row.get("model")})
        }
        lead_order = ("0–6 h", "6–12 h", "12–24 h", "24–48 h", "48–72 h")
        by_lead = {
            key: metrics([row for row in evaluated if row.get("lead_bin") == key])
            for key in lead_order
            if any(row.get("lead_bin") == key for row in evaluated)
        }
        season_order = ("winter_DJF", "spring_MAM", "summer_JJA", "autumn_SON")
        by_season = {
            key: metrics([row for row in evaluated if season_name(row.get("target_at_utc")) == key])
            for key in season_order
            if any(season_name(row.get("target_at_utc")) == key for row in evaluated)
        }

        deterministic_mae = overall["deterministic_mae_c"]
        ml_mae = overall["ml_mae_c"]
        beats = bool(finite(deterministic_mae) and finite(ml_mae) and float(ml_mae) < float(deterministic_mae))
        meaningful = bool(
            beats
            and float(deterministic_mae) - float(ml_mae) >= 0.02
            and (overall.get("ml_improvement_vs_deterministic_pct") or 0) >= 1.0
        )

        recent = []
        for row in sorted(evaluated, key=lambda item: item["target_at_utc"], reverse=True)[:12]:
            raw_temp = float(row["forecast"]["temperature_c"])
            recent.append({
                "model": row["model"],
                "source": row["source"],
                "target_at_utc": row["target_at_utc"],
                "lead_h": row["lead_h"],
                "raw_temperature_c": rounded(raw_temp, 2),
                "observed_temperature_c": rounded(row["truth_temperature_c"], 2),
                "deterministic_temperature_c": rounded(raw_temp - row["deterministic_correction_c"], 2),
                "ml_temperature_c": rounded(raw_temp - row["predicted_error_c"], 2),
                "raw_error_c": rounded(row["raw_error_c"], 2),
                "deterministic_error_c": rounded(row["deterministic_error_c"], 2),
                "ml_error_c": rounded(row["ml_error_c"], 2),
            })

        payload = {
            "schema": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": True,
            "shadow_only": True,
            "allowed_to_affect_public_forecast": False,
            "allowed_to_affect_alerts": False,
            "production_eligible": False,
            "truth_reference": {
                "station": DWD_STATION_NAME,
                "station_id": DWD_STATION_ID,
                "distance_to_nove_hrabeci_km": DWD_DISTANCE_KM,
                "is_nove_hrabeci_truth": False,
                "warning": "ML is evaluated against the nearby Sohland proxy, not an on-site Nové Hraběcí station.",
            },
            "evaluation": {
                "split": "same chronological target-time split as deterministic shadow benchmark; no random shuffle",
                "cutoff_target_utc": cutoff,
                "history_storage_files": history_storage,
                "history_cases": len(history),
                "live_archive_cases": len(live),
                "train_cases": len(train),
                "test_cases": len(test),
                "train_first_target_utc": train[0]["target_at_utc"] if train else None,
                "train_last_target_utc": train[-1]["target_at_utc"] if train else None,
                "test_first_target_utc": test[0]["target_at_utc"] if test else None,
                "test_last_target_utc": test[-1]["target_at_utc"] if test else None,
            },
            "model": {
                "algorithm": "sklearn HistGradientBoostingRegressor",
                "target": "forecast temperature error in °C; corrected = raw - predicted_error",
                "sklearn_version": sklearn.__version__,
                "params": MODEL_PARAMS,
                "features": FEATURE_NAMES,
                "excluded_as_feature": [
                    "truth observations",
                    "data source/provenance",
                    "fixed NH terrain constants that do not vary between cases",
                ],
            },
            "metrics": {
                "overall": overall,
                "by_model": by_model,
                "by_lead_bin": by_lead,
                "by_season": by_season,
            },
            "deterministic_reference_same_holdout": deterministic_overall,
            "gate": {
                "beats_deterministic_baseline": beats,
                "meaningful_shadow_win": meaningful,
                "meaningful_rule": "ML MAE must be at least 0.02 °C and 1% lower than deterministic MAE on the same chronological holdout.",
                "production_eligible": False,
                "production_blocker": "No on-site Nové Hraběcí truth station yet; shadow results cannot alter public forecast or alerts.",
            },
            "recent_shadow_examples": recent,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"overall": overall, "gate": payload["gate"]}, ensure_ascii=False))
    except Exception as exc:
        write_error(f"ML shadow build failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
