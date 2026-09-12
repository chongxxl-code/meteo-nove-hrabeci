#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import evaluate, history_cases, live_cases
from build_calibration_ml_shadow import FEATURE_NAMES, MODEL_PARAMS, case_features, finite
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

PER_MODEL_FEATURE_NAMES = FEATURE_NAMES[3:]
MODELS = ("chmi", "dwd", "ec")
MIN_MODEL_TRAIN_CASES = 1000
MIN_MODEL_TEST_CASES = 100
MIN_SUCCESSFUL_MODEL_FOLDS = 3


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


def fold_stability(folds):
    values = [
        float(fold["ml_improvement_vs_deterministic_pct"])
        for fold in folds
        if fold.get("ok") and finite(fold.get("ml_improvement_vs_deterministic_pct"))
    ]
    wins = sum(1 for value in values if value > 0)
    required = math.ceil(len(values) * 0.60) if values else 0
    worst = min(values) if values else None
    return {
        "folds_evaluated": len(values),
        "fold_wins_vs_deterministic": wins,
        "required_fold_wins": required,
        "worst_fold_improvement_vs_deterministic_pct": rounded(worst, 1),
        "development_stability_pass": bool(
            len(values) >= MIN_SUCCESSFUL_MODEL_FOLDS
            and wins >= required
            and finite(worst)
            and float(worst) > -10.0
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

    model_results = {}
    all_rows = []

    for model_name in MODELS:
        model_cases = [case for case in cases if case.get("model") == model_name]
        model_targets = sorted({case["target_at_utc"] for case in model_cases})
        ranges = fold_ranges(model_targets)
        model_folds = []
        model_rows = []

        for fold_number, (start_index, end_index) in enumerate(ranges, start=1):
            test_start = model_targets[start_index]
            test_end = model_targets[end_index] if end_index < len(model_targets) else None
            test = [
                case
                for case in model_cases
                if case["target_at_utc"] >= test_start
                and (test_end is None or case["target_at_utc"] < test_end)
            ]
            as_of = earliest_test_issue(test)
            if as_of is None:
                model_folds.append(
                    {
                        "fold": fold_number,
                        "ok": False,
                        "test_start_utc": test_start,
                        "test_cases": len(test),
                        "error": "test cases have no usable issuance timestamps",
                    }
                )
                continue

            train = []
            for case in model_cases:
                available = truth_available_at(case)
                if available is not None and available <= as_of:
                    train.append(case)

            if len(train) < MIN_MODEL_TRAIN_CASES or len(test) < MIN_MODEL_TEST_CASES:
                model_folds.append(
                    {
                        "fold": fold_number,
                        "ok": False,
                        "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                        "test_start_utc": test_start,
                        "test_last_target_utc": test[-1]["target_at_utc"] if test else None,
                        "train_cases": len(train),
                        "test_cases": len(test),
                        "error": "insufficient source-specific train/test cases",
                    }
                )
                continue

            deterministic_rows, _, _, _, _ = evaluate(train, test)
            try:
                predictions, used, dropped, minimum = fit_predict(train, test)
            except Exception as exc:
                model_folds.append(
                    {
                        "fold": fold_number,
                        "ok": False,
                        "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                        "test_start_utc": test_start,
                        "test_last_target_utc": test[-1]["target_at_utc"] if test else None,
                        "train_cases": len(train),
                        "test_cases": len(test),
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
                continue

            rows = rows_for_predictions(test, deterministic_rows, predictions)
            rows.sort(
                key=lambda row: (
                    row["target_at_utc"], row["model"], row["lead_h"], row["source"]
                )
            )
            summary = summarize(rows)
            improvement = summary["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]
            model_folds.append(
                {
                    "fold": fold_number,
                    "ok": True,
                    "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                    "train_first_target_utc": train[0]["target_at_utc"],
                    "train_last_target_utc": train[-1]["target_at_utc"],
                    "test_start_utc": test_start,
                    "test_last_target_utc": test[-1]["target_at_utc"],
                    "train_cases": len(train),
                    "test_cases": len(test),
                    "features_used": used,
                    "features_dropped": dropped,
                    "minimum_finite_training_cases_per_feature": minimum,
                    "metrics": summary["metrics"],
                    "extreme_temperature": summary["extreme_temperature"],
                    "strict_guard": summary["strict_guard"],
                    "ml_improvement_vs_deterministic_pct": rounded(improvement, 1),
                }
            )
            model_rows.extend(rows)

        successful = [fold for fold in model_folds if fold.get("ok")]
        if model_rows:
            model_summary = summarize(model_rows)
            aggregate_improvement = (
                model_summary["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]
            )
            strict_pass = bool(model_summary["strict_guard"]["strict_holdout_pass"])
        else:
            model_summary = None
            aggregate_improvement = None
            strict_pass = False

        stability = fold_stability(successful)
        enough_folds = len(successful) >= MIN_SUCCESSFUL_MODEL_FOLDS
        development_signal = bool(
            enough_folds
            and stability["development_stability_pass"]
            and strict_pass
            and finite(aggregate_improvement)
            and float(aggregate_improvement) >= 3.0
        )
        model_results[model_name] = {
            "cases": len(model_cases),
            "unique_target_times": len(model_targets),
            "folds_requested": len(ranges),
            "successful_folds": len(successful),
            "folds": model_folds,
            "aggregate": model_summary,
            "stability": stability,
            "aggregate_improvement_vs_deterministic_pct": rounded(
                aggregate_improvement, 1
            ),
            "strict_guard_pass": strict_pass,
            "development_signal": development_signal,
        }
        all_rows.extend(model_rows)

    usable_models = [
        model_name
        for model_name, result in model_results.items()
        if result["successful_folds"] >= MIN_SUCCESSFUL_MODEL_FOLDS
    ]
    if len(usable_models) != len(MODELS) or not all_rows:
        payload = {
            "schema": 2,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "shadow_only": True,
            "allowed_to_affect_public_forecast": False,
            "allowed_to_affect_alerts": False,
            "production_eligible": False,
            "confirmatory_valid": False,
            "error": "insufficient source-specific walk-forward evidence",
            "data": {
                "history_storage_files": storage,
                "history_cases": len(history),
                "live_archive_cases": len(live),
                "all_cases": len(cases),
            },
            "model_results": model_results,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": False,
                    "usable_models": usable_models,
                    "required_models": list(MODELS),
                    "successful_folds_by_model": {
                        name: model_results[name]["successful_folds"] for name in MODELS
                    },
                },
                ensure_ascii=False,
            )
        )
        return

    all_rows.sort(
        key=lambda row: (
            row["target_at_utc"], row["model"], row["lead_h"], row["source"]
        )
    )
    overall = summarize(all_rows)
    strict_pass = bool(overall["strict_guard"]["strict_holdout_pass"])
    per_model_pass = all(model_results[name]["development_signal"] for name in MODELS)
    development_signal = bool(strict_pass and per_model_pass)

    model_balanced_det_mae = sum(
        float(model_results[name]["aggregate"]["metrics"]["deterministic"]["mae_c"])
        for name in MODELS
    ) / len(MODELS)
    model_balanced_ml_mae = sum(
        float(model_results[name]["aggregate"]["metrics"]["ml"]["mae_c"])
        for name in MODELS
    ) / len(MODELS)
    model_balanced_improvement = (
        (model_balanced_det_mae - model_balanced_ml_mae) / model_balanced_det_mae * 100.0
        if model_balanced_det_mae
        else None
    )

    payload = {
        "schema": 2,
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
            "window": (
                "independent source-specific expanding as-of-time walk-forward folds; "
                "each NWP source is evaluated only across its own available history"
            ),
            "leakage_guard": (
                "training truth must be observable by earliest issuance in each future fold; "
                "each estimator trains only on its own NWP source"
            ),
            "development_only_warning": (
                "This architecture was chosen after inspecting previous results on these historical folds. "
                "These folds are exploratory development data, not an independent confirmation."
            ),
        },
        "data": {
            "history_storage_files": storage,
            "history_cases": len(history),
            "live_archive_cases": len(live),
            "all_cases": len(cases),
            "evaluated_rows": len(all_rows),
        },
        "model_results": model_results,
        "aggregate": overall,
        "model_balanced": {
            "deterministic_mae_c": rounded(model_balanced_det_mae),
            "ml_mae_c": rounded(model_balanced_ml_mae),
            "improvement_vs_deterministic_pct": rounded(model_balanced_improvement, 1),
        },
        "development_gate": {
            "strict_guard_pass": strict_pass,
            "all_source_models_pass": per_model_pass,
            "per_model_development_signal": {
                name: model_results[name]["development_signal"] for name in MODELS
            },
            "model_balanced_improvement_vs_deterministic_pct": rounded(
                model_balanced_improvement, 1
            ),
            "development_signal": development_signal,
            "confirmatory_pass": False,
            "rule": (
                "Each source needs >=3 successful source-specific folds, >=60% fold wins, no fold <= -10%, "
                ">=3% aggregate MAE gain and strict guard pass. Combined development signal also requires "
                "the aggregate strict guard to pass. Historical results remain development-only."
            ),
        },
        "next_if_development_signal": (
            "Freeze this architecture and start timestamped prospective predictions; only future unseen "
            "forecast issues may provide confirmatory evidence."
        ),
        "next_if_no_signal": (
            "Keep pooled squared-error ML as the current shadow research leader and do not add more "
            "complexity from the same historical folds."
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
                "model_balanced_improvement_vs_deterministic_pct": rounded(
                    model_balanced_improvement, 1
                ),
                "strict_guard_pass": strict_pass,
                "per_model_development_signal": {
                    name: model_results[name]["development_signal"] for name in MODELS
                },
                "successful_folds_by_model": {
                    name: model_results[name]["successful_folds"] for name in MODELS
                },
                "development_signal": development_signal,
                "confirmatory_valid": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
