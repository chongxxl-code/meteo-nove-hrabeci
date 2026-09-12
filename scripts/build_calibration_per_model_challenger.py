#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import evaluate, history_cases, live_cases
from build_calibration_ml_shadow import FEATURE_NAMES, MODEL_PARAMS, case_features, finite, improvement_pct
from build_calibration_ml_walkforward import (
    MIN_FEATURE_FINITE_FRACTION,
    MIN_FEATURE_FINITE_N,
    MIN_TEST_CASES,
    MIN_TRAIN_CASES,
    earliest_test_issue,
    fold_ranges,
    truth_available_at,
)
from diagnose_calibration_bias import rows_for_predictions, summarize

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "per-model-challenger.json"

# The first three case_features are one-hot source-model flags. They are constant inside
# a source-specific estimator, so the per-model challenger deliberately excludes them.
PER_MODEL_FEATURE_NAMES = FEATURE_NAMES[3:]
MODELS = ("chmi", "dwd", "ec")
MIN_MODEL_TRAIN_CASES = 1000
MIN_MODEL_TEST_CASES = 100


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


def model_features(case):
    return case_features(case)[3:]


def prepare_xy(train, test):
    import numpy as np

    x_train = np.asarray([model_features(case) for case in train], dtype=float)
    y_train = np.asarray([float(case["error_c"]) for case in train], dtype=float)
    x_test = np.asarray([model_features(case) for case in test], dtype=float)

    minimum = max(MIN_FEATURE_FINITE_N, int(len(train) * MIN_FEATURE_FINITE_FRACTION))
    finite_counts = np.isfinite(x_train).sum(axis=0)
    keep = []
    unique_counts = []
    for column, finite_count in enumerate(finite_counts):
        finite_values = x_train[np.isfinite(x_train[:, column]), column]
        unique_count = len(np.unique(finite_values)) if len(finite_values) else 0
        unique_counts.append(unique_count)
        keep.append(bool(finite_count >= minimum and unique_count >= 2))
    keep_mask = np.asarray(keep, dtype=bool)
    if not np.any(keep_mask):
        raise RuntimeError("No usable non-degenerate features for per-model estimator")

    used = [name for name, flag in zip(PER_MODEL_FEATURE_NAMES, keep_mask) if bool(flag)]
    dropped = [
        {
            "feature": name,
            "finite_train_n": int(finite_count),
            "unique_finite_train_values": int(unique_count),
        }
        for name, finite_count, unique_count, flag in zip(
            PER_MODEL_FEATURE_NAMES, finite_counts, unique_counts, keep_mask
        )
        if not bool(flag)
    ]
    return x_train[:, keep_mask], y_train, x_test[:, keep_mask], used, dropped, minimum


def fit_predict(train, test):
    from sklearn.ensemble import HistGradientBoostingRegressor

    x_train, y_train, x_test, used, dropped, minimum = prepare_xy(train, test)
    params = dict(MODEL_PARAMS)
    params["loss"] = "absolute_error"
    model = HistGradientBoostingRegressor(**params)
    model.fit(x_train, y_train)
    return model.predict(x_test), used, dropped, minimum


def fold_model_stability(folds, model_name):
    values = []
    wins = 0
    for fold in folds:
        model = (fold.get("by_model") or {}).get(model_name) or {}
        value = ((model.get("ml_improvement_vs_deterministic_pct") or {}).get("mae"))
        if finite(value):
            value = float(value)
            values.append(value)
            if value > 0:
                wins += 1
    required = math.ceil(len(values) * 0.60) if values else 0
    worst = min(values) if values else None
    return {
        "folds_evaluated": len(values),
        "fold_wins_vs_deterministic": wins,
        "required_fold_wins": required,
        "worst_fold_improvement_vs_deterministic_pct": rounded(worst, 1),
        "development_stability_pass": bool(
            values and wins >= required and finite(worst) and float(worst) > -10.0
        ),
    }


def main():
    import sklearn

    history, storage = history_cases(return_storage=True)
    live = live_cases()
    cases = history + live
    cases.sort(
        key=lambda case: (
            case["target_at_utc"], case["model"], case["lead_h"], case["source"]
        )
    )
    targets = sorted({case["target_at_utc"] for case in cases})

    folds = []
    all_rows = []

    for fold_number, (start_index, end_index) in enumerate(fold_ranges(targets), start=1):
        test_start = targets[start_index]
        test_end = targets[end_index] if end_index < len(targets) else None
        test = [
            case for case in cases
            if case["target_at_utc"] >= test_start
            and (test_end is None or case["target_at_utc"] < test_end)
        ]
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
        indices_by_model = {
            model_name: [i for i, case in enumerate(test) if case.get("model") == model_name]
            for model_name in MODELS
        }

        fold_rows = []
        fit_info = {}
        complete = True
        for model_name in MODELS:
            train_model = [case for case in train if case.get("model") == model_name]
            indices = indices_by_model[model_name]
            test_model = [test[i] for i in indices]
            deterministic_model = [deterministic_rows[i] for i in indices]
            if len(train_model) < MIN_MODEL_TRAIN_CASES or len(test_model) < MIN_MODEL_TEST_CASES:
                fit_info[model_name] = {
                    "ok": False,
                    "train_cases": len(train_model),
                    "test_cases": len(test_model),
                    "error": "insufficient source-specific train/test cases",
                }
                complete = False
                continue

            try:
                predictions, used, dropped, minimum = fit_predict(train_model, test_model)
            except Exception as exc:
                fit_info[model_name] = {
                    "ok": False,
                    "train_cases": len(train_model),
                    "test_cases": len(test_model),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
                complete = False
                continue

            rows = rows_for_predictions(test_model, deterministic_model, predictions)
            fold_rows.extend(rows)
            fit_info[model_name] = {
                "ok": True,
                "train_cases": len(train_model),
                "test_cases": len(test_model),
                "features_used": used,
                "features_dropped": dropped,
                "minimum_finite_training_cases_per_feature": minimum,
            }

        if not complete or not fold_rows:
            folds.append(
                {
                    "fold": fold_number,
                    "ok": False,
                    "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                    "test_start_utc": test_start,
                    "test_last_target_utc": test[-1]["target_at_utc"] if test else None,
                    "source_models": fit_info,
                    "error": "not all source-specific estimators were available",
                }
            )
            continue

        fold_rows.sort(
            key=lambda row: (row["target_at_utc"], row["model"], row["lead_h"], row["source"])
        )
        summary = summarize(fold_rows)
        improvement = summary["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]
        folds.append(
            {
                "fold": fold_number,
                "ok": True,
                "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                "test_start_utc": test_start,
                "test_last_target_utc": test[-1]["target_at_utc"],
                "train_cases": len(train),
                "test_cases": len(test),
                "source_models": fit_info,
                "metrics": summary["metrics"],
                "by_model": summary["by_model"],
                "extreme_temperature": summary["extreme_temperature"],
                "strict_guard": summary["strict_guard"],
                "ml_improvement_vs_deterministic_pct": rounded(improvement, 1),
            }
        )
        all_rows.extend(fold_rows)

    successful = [fold for fold in folds if fold.get("ok")]
    if len(successful) < 3 or not all_rows:
        raise RuntimeError(f"Too few successful per-model challenger folds: {len(successful)}")

    overall = summarize(all_rows)
    improvements = [
        float(fold["ml_improvement_vs_deterministic_pct"])
        for fold in successful
        if finite(fold.get("ml_improvement_vs_deterministic_pct"))
    ]
    wins = sum(1 for value in improvements if value > 0)
    required_wins = math.ceil(len(improvements) * 0.80)
    worst = min(improvements) if improvements else None
    aggregate_improvement = overall["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]

    model_stability = {
        model_name: fold_model_stability(successful, model_name)
        for model_name in MODELS
    }
    temporal_pass = bool(
        improvements
        and wins >= required_wins
        and finite(worst)
        and float(worst) > -5.0
        and finite(aggregate_improvement)
        and float(aggregate_improvement) >= 3.0
    )
    strict_pass = bool(overall["strict_guard"]["strict_holdout_pass"])
    per_model_temporal_pass = all(
        item["development_stability_pass"] for item in model_stability.values()
    )
    development_signal = bool(temporal_pass and strict_pass and per_model_temporal_pass)

    payload = {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "confirmatory_valid": False,
        "purpose": (
            "exploratory test of separate source-model temperature calibrators after pooled-model "
            "validation exposed model-specific bias"
        ),
        "method": {
            "algorithm": "one sklearn HistGradientBoostingRegressor per NWP source model",
            "loss": "absolute_error",
            "sklearn_version": sklearn.__version__,
            "params": {**MODEL_PARAMS, "loss": "absolute_error"},
            "models": list(MODELS),
            "features": PER_MODEL_FEATURE_NAMES,
            "window": "same expanding as-of-time walk-forward folds as ML v1",
            "leakage_guard": (
                "training truth must be observable by earliest issuance in each future fold; "
                "each estimator trains only on its own NWP source"
            ),
            "development_only_warning": (
                "This architecture was chosen after inspecting previous results on these historical folds. "
                "These folds are therefore exploratory development data, not an independent confirmation."
            ),
        },
        "data": {
            "history_storage_files": storage,
            "history_cases": len(history),
            "live_archive_cases": len(live),
            "all_cases": len(cases),
            "successful_folds": len(successful),
        },
        "aggregate": overall,
        "folds": folds,
        "development_gate": {
            "aggregate_improvement_vs_deterministic_pct": rounded(aggregate_improvement, 1),
            "fold_wins_vs_deterministic": wins,
            "required_fold_wins": required_wins,
            "worst_fold_improvement_vs_deterministic_pct": rounded(worst, 1),
            "temporal_pass": temporal_pass,
            "strict_guard_pass": strict_pass,
            "per_model_temporal_stability": model_stability,
            "development_signal": development_signal,
            "confirmatory_pass": False,
            "rule": (
                "A development signal requires >=80% aggregate fold wins, >=3% aggregate MAE gain, "
                "no aggregate fold <= -5%, strict guard pass, and each source model to win >=60% "
                "of folds with no source-model fold <= -10%."
            ),
        },
        "next_if_development_signal": (
            "Freeze this architecture and start timestamped prospective predictions; only future unseen "
            "forecast issues may provide confirmatory evidence."
        ),
        "next_if_no_signal": (
            "Keep pooled ML v1 as shadow benchmark and do not add more complexity from the same holdout."
        ),
        "production_blocker": (
            "No on-site Nové Hraběcí truth station and no independent prospective confirmation."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "aggregate_mae_c": overall["metrics"]["ml"]["mae_c"],
                "deterministic_mae_c": overall["metrics"]["deterministic"]["mae_c"],
                "aggregate_improvement_vs_deterministic_pct": rounded(aggregate_improvement, 1),
                "strict_guard_pass": strict_pass,
                "fold_wins": wins,
                "worst_fold_improvement_pct": rounded(worst, 1),
                "per_model_temporal_stability": model_stability,
                "development_signal": development_signal,
                "confirmatory_valid": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
