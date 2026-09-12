#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import evaluate, history_cases, live_cases
from build_calibration_ml_shadow import FEATURE_NAMES, MODEL_PARAMS, case_features, finite
from build_calibration_ml_walkforward import (
    FOLDS,
    MIN_FEATURE_FINITE_FRACTION,
    MIN_FEATURE_FINITE_N,
    MIN_TEST_CASES,
    MIN_TRAIN_CASES,
    earliest_test_issue,
    fold_ranges,
    truth_available_at,
)
from validate_calibration_strict import (
    comparison_metrics,
    extreme_subset,
    guard_result,
    subgroup_diagnostics,
    target_balanced_metrics,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "bias-diagnostic.json"

VARIANTS = (
    "absolute_current",
    "absolute_model_residual_centered",
    "squared_error",
)


def mean(values):
    return sum(values) / len(values) if values else 0.0


def model_residual_offsets(train, y_train, predicted_train):
    grouped = defaultdict(list)
    for case, truth_error, prediction in zip(train, y_train, predicted_train):
        grouped[case.get("model")].append(float(truth_error) - float(prediction))
    return {model: mean(values) for model, values in grouped.items()}


def prepare_xy(train, test):
    import numpy as np

    x_train = np.asarray([case_features(case) for case in train], dtype=float)
    y_train = np.asarray([float(case["error_c"]) for case in train], dtype=float)
    x_test = np.asarray([case_features(case) for case in test], dtype=float)
    minimum = max(MIN_FEATURE_FINITE_N, int(len(train) * MIN_FEATURE_FINITE_FRACTION))
    finite_counts = np.isfinite(x_train).sum(axis=0)
    keep_mask = finite_counts >= minimum
    if not np.any(keep_mask):
        raise RuntimeError("No usable ML features after coverage filtering")
    used = [name for name, keep in zip(FEATURE_NAMES, keep_mask) if bool(keep)]
    return x_train[:, keep_mask], y_train, x_test[:, keep_mask], used


def rows_for_predictions(test, deterministic_rows, predictions):
    rows = []
    for case, deterministic_row, prediction in zip(test, deterministic_rows, predictions):
        raw_error = float(case["error_c"])
        raw_temp = float(case["forecast"]["temperature_c"])
        predicted_error = float(prediction)
        rows.append(
            {
                **case,
                "truth_temperature_c": float(case["truth_temperature_c"]),
                "raw_temperature_c": raw_temp,
                "deterministic_temperature_c": raw_temp - float(deterministic_row["correction_c"]),
                "ml_temperature_c": raw_temp - predicted_error,
                "raw_error_c": raw_error,
                "deterministic_error_c": float(deterministic_row["corrected_error_c"]),
                "ml_error_c": raw_error - predicted_error,
            }
        )
    return rows


def summarize(rows):
    by_model, by_lead = subgroup_diagnostics(rows)
    overall = comparison_metrics(rows)
    balanced = target_balanced_metrics(rows)
    extremes = {
        "frost_truth_le_0c": extreme_subset(rows, lambda value: value <= 0.0),
        "hot_truth_ge_25c": extreme_subset(rows, lambda value: value >= 25.0),
    }
    return {
        "metrics": overall,
        "target_balanced_metrics": balanced,
        "by_model": by_model,
        "by_lead_bin": by_lead,
        "extreme_temperature": extremes,
        "strict_guard": guard_result(overall, balanced, extremes, by_model),
    }


def main():
    import numpy as np
    import sklearn
    from sklearn.ensemble import HistGradientBoostingRegressor

    history, storage = history_cases(return_storage=True)
    live = live_cases()
    cases = history + live
    cases.sort(key=lambda case: (case["target_at_utc"], case["model"], case["lead_h"], case["source"]))
    targets = sorted({case["target_at_utc"] for case in cases})

    all_rows = {name: [] for name in VARIANTS}
    folds = []

    for fold_number, (start_index, end_index) in enumerate(fold_ranges(targets), start=1):
        test_start = targets[start_index]
        test_end_exclusive = targets[end_index] if end_index < len(targets) else None
        if test_end_exclusive is None:
            test = [case for case in cases if case["target_at_utc"] >= test_start]
        else:
            test = [case for case in cases if test_start <= case["target_at_utc"] < test_end_exclusive]

        as_of = earliest_test_issue(test)
        if as_of is None:
            continue
        train = []
        for case in cases:
            available = truth_available_at(case)
            if available is not None and available <= as_of:
                train.append(case)
        if len(train) < MIN_TRAIN_CASES or len(test) < MIN_TEST_CASES:
            continue

        deterministic_rows, _, _, _, _ = evaluate(train, test)
        x_train, y_train, x_test, features_used = prepare_xy(train, test)

        absolute_params = dict(MODEL_PARAMS)
        absolute_params["loss"] = "absolute_error"
        absolute_model = HistGradientBoostingRegressor(**absolute_params)
        absolute_model.fit(x_train, y_train)
        absolute_train_pred = absolute_model.predict(x_train)
        absolute_test_pred = absolute_model.predict(x_test)

        offsets = model_residual_offsets(train, y_train, absolute_train_pred)
        centered_pred = np.asarray(
            [
                float(prediction) + float(offsets.get(case.get("model"), 0.0))
                for case, prediction in zip(test, absolute_test_pred)
            ],
            dtype=float,
        )

        squared_params = dict(MODEL_PARAMS)
        squared_params["loss"] = "squared_error"
        squared_model = HistGradientBoostingRegressor(**squared_params)
        squared_model.fit(x_train, y_train)
        squared_test_pred = squared_model.predict(x_test)

        variant_predictions = {
            "absolute_current": absolute_test_pred,
            "absolute_model_residual_centered": centered_pred,
            "squared_error": squared_test_pred,
        }

        fold_entry = {
            "fold": fold_number,
            "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
            "test_start_utc": test_start,
            "test_last_target_utc": test[-1]["target_at_utc"],
            "train_cases": len(train),
            "test_cases": len(test),
            "features_used": features_used,
            "absolute_model_training_residual_offsets_c": {
                key: round(float(value), 3) for key, value in sorted(offsets.items())
            },
            "variants": {},
        }

        for name, predictions in variant_predictions.items():
            rows = rows_for_predictions(test, deterministic_rows, predictions)
            all_rows[name].extend(rows)
            summary = summarize(rows)
            fold_entry["variants"][name] = {
                "metrics": summary["metrics"],
                "by_model": summary["by_model"],
            }
        folds.append(fold_entry)

    if len(folds) < 3:
        raise RuntimeError(f"Too few diagnostic folds: {len(folds)}")

    candidates = {name: summarize(rows) for name, rows in all_rows.items()}
    ranking = sorted(
        (
            {
                "variant": name,
                "mae_c": summary["metrics"]["ml"]["mae_c"],
                "rmse_c": summary["metrics"]["ml"]["rmse_c"],
                "abs_bias_c": summary["metrics"]["ml"]["abs_bias_c"],
                "p95_abs_error_c": summary["metrics"]["ml"]["p95_abs_error_c"],
                "strict_guard_pass": summary["strict_guard"]["strict_holdout_pass"],
                "chmi_abs_bias_change_c": (
                    summary["by_model"].get("chmi", {}).get("ml_abs_bias_change_vs_deterministic_c")
                ),
            }
            for name, summary in candidates.items()
        ),
        key=lambda item: (
            not item["strict_guard_pass"],
            float(item["mae_c"]) if finite(item["mae_c"]) else 999.0,
        ),
    )

    payload = {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "purpose": "diagnose the strict-validation failure caused by model-specific bias without changing production behavior",
        "data": {
            "history_storage_files": storage,
            "history_cases": len(history),
            "live_archive_cases": len(live),
            "all_cases": len(cases),
            "successful_folds": len(folds),
        },
        "variants": {
            "absolute_current": "current HistGradientBoosting absolute-error loss",
            "absolute_model_residual_centered": "same fitted absolute-error model plus mean training residual correction learned separately for each NWP model",
            "squared_error": "same HistGradientBoosting structure using squared-error loss to target conditional mean rather than median",
        },
        "ranking": ranking,
        "candidates": candidates,
        "folds": folds,
        "decision_rule": "Do not change ML v1 merely to pass a gate. A candidate is interesting only if it fixes CHMI bias while preserving/improving MAE, RMSE and tail behavior across future folds.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ranking": ranking}, ensure_ascii=False))


if __name__ == "__main__":
    main()
