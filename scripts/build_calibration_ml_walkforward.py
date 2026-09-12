#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import evaluate, history_cases, live_cases
from build_calibration_ml_shadow import (
    FEATURE_NAMES,
    MODEL_PARAMS,
    case_features,
    finite,
    metrics,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "ml-v1-walkforward.json"

FOLDS = 5
INITIAL_TRAIN_FRACTION = 0.40
MIN_TRAIN_CASES = 5000
MIN_TEST_CASES = 500
MIN_FEATURE_FINITE_FRACTION = 0.01
MIN_FEATURE_FINITE_N = 30


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


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


def fold_ranges(targets):
    if len(targets) < 20:
        return []
    initial_index = max(1, int(len(targets) * INITIAL_TRAIN_FRACTION))
    remaining = len(targets) - initial_index
    if remaining < FOLDS:
        return []

    ranges = []
    for fold in range(FOLDS):
        start = initial_index + (remaining * fold) // FOLDS
        end = initial_index + (remaining * (fold + 1)) // FOLDS
        if fold == FOLDS - 1:
            end = len(targets)
        if start >= end:
            continue
        ranges.append((start, end))
    return ranges


def main():
    try:
        import numpy as np
        import sklearn
        from sklearn.ensemble import HistGradientBoostingRegressor
    except Exception as exc:
        write_error(f"ML dependency unavailable: {exc}")
        return

    try:
        history, history_storage = history_cases(return_storage=True)
        live = live_cases()
        cases = history + live
        cases.sort(
            key=lambda case: (
                case["target_at_utc"],
                case["model"],
                case["lead_h"],
                case["source"],
            )
        )
        targets = sorted({case["target_at_utc"] for case in cases})
        ranges = fold_ranges(targets)
        if not ranges:
            write_error(f"Not enough target timestamps for {FOLDS}-fold walk-forward backtest.")
            return

        fold_summaries = []
        all_evaluated = []

        for fold_number, (start_index, end_index) in enumerate(ranges, start=1):
            test_start = targets[start_index]
            test_end_exclusive = targets[end_index] if end_index < len(targets) else None

            train = [case for case in cases if case["target_at_utc"] < test_start]
            if test_end_exclusive is None:
                test = [case for case in cases if case["target_at_utc"] >= test_start]
            else:
                test = [
                    case
                    for case in cases
                    if test_start <= case["target_at_utc"] < test_end_exclusive
                ]

            if len(train) < MIN_TRAIN_CASES or len(test) < MIN_TEST_CASES:
                fold_summaries.append(
                    {
                        "fold": fold_number,
                        "ok": False,
                        "test_start_utc": test_start,
                        "test_end_exclusive_utc": test_end_exclusive,
                        "train_cases": len(train),
                        "test_cases": len(test),
                        "error": "insufficient train/test cases",
                    }
                )
                continue

            deterministic_rows, deterministic_overall, _, _, _ = evaluate(train, test)

            x_train = np.asarray([case_features(case) for case in train], dtype=float)
            y_train = np.asarray([float(case["error_c"]) for case in train], dtype=float)
            x_test = np.asarray([case_features(case) for case in test], dtype=float)

            minimum_finite_training_cases = max(
                MIN_FEATURE_FINITE_N,
                int(len(train) * MIN_FEATURE_FINITE_FRACTION),
            )
            finite_counts = np.isfinite(x_train).sum(axis=0)
            keep_mask = finite_counts >= minimum_finite_training_cases
            if not np.any(keep_mask):
                fold_summaries.append(
                    {
                        "fold": fold_number,
                        "ok": False,
                        "test_start_utc": test_start,
                        "test_end_exclusive_utc": test_end_exclusive,
                        "train_cases": len(train),
                        "test_cases": len(test),
                        "error": "no usable ML features after coverage filtering",
                    }
                )
                continue

            features_used = [
                name for name, keep in zip(FEATURE_NAMES, keep_mask) if bool(keep)
            ]
            features_dropped = [
                {"feature": name, "finite_train_n": int(count)}
                for name, count, keep in zip(FEATURE_NAMES, finite_counts, keep_mask)
                if not bool(keep)
            ]

            model = HistGradientBoostingRegressor(**MODEL_PARAMS)
            model.fit(x_train[:, keep_mask], y_train)
            predicted_error = model.predict(x_test[:, keep_mask])

            evaluated = []
            for case, deterministic_row, prediction in zip(
                test, deterministic_rows, predicted_error
            ):
                raw_error = float(case["error_c"])
                deterministic_error = float(deterministic_row["corrected_error_c"])
                ml_error = raw_error - float(prediction)
                evaluated.append(
                    {
                        **case,
                        "raw_error_c": raw_error,
                        "deterministic_error_c": deterministic_error,
                        "ml_error_c": ml_error,
                    }
                )

            fold_metrics = metrics(evaluated)
            ml_vs_det = fold_metrics.get("ml_improvement_vs_deterministic_pct")
            fold_summaries.append(
                {
                    "fold": fold_number,
                    "ok": True,
                    "train_first_target_utc": train[0]["target_at_utc"],
                    "train_last_target_utc": train[-1]["target_at_utc"],
                    "test_start_utc": test_start,
                    "test_last_target_utc": test[-1]["target_at_utc"],
                    "train_cases": len(train),
                    "test_cases": len(test),
                    "features_used": features_used,
                    "features_dropped_for_training_coverage": features_dropped,
                    "minimum_finite_training_cases_per_feature": minimum_finite_training_cases,
                    "deterministic_reference_same_fold": deterministic_overall,
                    "metrics": fold_metrics,
                    "ml_beats_deterministic": bool(
                        finite(fold_metrics.get("ml_mae_c"))
                        and finite(fold_metrics.get("deterministic_mae_c"))
                        and float(fold_metrics["ml_mae_c"])
                        < float(fold_metrics["deterministic_mae_c"])
                    ),
                    "ml_improvement_vs_deterministic_pct": rounded(ml_vs_det, 1),
                }
            )
            all_evaluated.extend(evaluated)

        successful = [fold for fold in fold_summaries if fold.get("ok")]
        if len(successful) < 3 or not all_evaluated:
            write_error(
                f"Too few successful walk-forward folds: {len(successful)}/{len(fold_summaries)}"
            )
            return

        aggregate = metrics(all_evaluated)
        wins = sum(1 for fold in successful if fold.get("ml_beats_deterministic"))
        improvements = [
            float(fold["ml_improvement_vs_deterministic_pct"])
            for fold in successful
            if finite(fold.get("ml_improvement_vs_deterministic_pct"))
        ]
        worst_fold_improvement = min(improvements) if improvements else None
        required_wins = math.ceil(len(successful) * 0.80)
        aggregate_improvement = aggregate.get("ml_improvement_vs_deterministic_pct")
        stable_win = bool(
            wins >= required_wins
            and finite(aggregate_improvement)
            and float(aggregate_improvement) >= 3.0
            and finite(worst_fold_improvement)
            and float(worst_fold_improvement) > -5.0
        )

        payload = {
            "schema": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": True,
            "shadow_only": True,
            "allowed_to_affect_public_forecast": False,
            "allowed_to_affect_alerts": False,
            "production_eligible": False,
            "purpose": "expanding-window walk-forward test; each fold trains only on earlier target times and tests on the next unseen block",
            "data": {
                "history_storage_files": history_storage,
                "history_cases": len(history),
                "live_archive_cases": len(live),
                "all_cases": len(cases),
                "first_target_utc": cases[0]["target_at_utc"] if cases else None,
                "last_target_utc": cases[-1]["target_at_utc"] if cases else None,
            },
            "method": {
                "algorithm": "sklearn HistGradientBoostingRegressor",
                "sklearn_version": sklearn.__version__,
                "folds_requested": FOLDS,
                "successful_folds": len(successful),
                "initial_train_fraction_of_unique_target_times": INITIAL_TRAIN_FRACTION,
                "window": "expanding training window; contiguous non-overlapping future test blocks",
                "params": MODEL_PARAMS,
                "feature_coverage_rule": f"keep feature when finite in at least max({MIN_FEATURE_FINITE_N}, {MIN_FEATURE_FINITE_FRACTION:.0%} of training cases)",
                "leakage_guard": "fold boundaries use target_at_utc; no future target time is present in that fold's training set",
            },
            "aggregate_metrics": aggregate,
            "folds": fold_summaries,
            "gate": {
                "fold_wins": wins,
                "required_fold_wins": required_wins,
                "worst_fold_improvement_vs_deterministic_pct": rounded(worst_fold_improvement, 1),
                "aggregate_improvement_vs_deterministic_pct": rounded(aggregate_improvement, 1),
                "stable_walk_forward_win": stable_win,
                "rule": "ML must beat deterministic baseline in at least 80% of successful folds, improve aggregate MAE by at least 3%, and no fold may be worse by 5% or more.",
                "production_eligible": False,
                "production_blocker": "No on-site Nové Hraběcí truth station yet; walk-forward results remain shadow-only.",
            },
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"aggregate": aggregate, "gate": payload["gate"]}, ensure_ascii=False))
    except Exception as exc:
        write_error(f"Walk-forward ML build failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
