#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import evaluate, history_cases, live_cases
from build_calibration_ml_shadow import MODEL_PARAMS, finite
from build_calibration_ml_walkforward import (
    FOLDS,
    MIN_TEST_CASES,
    MIN_TRAIN_CASES,
    earliest_test_issue,
    fold_ranges,
    truth_available_at,
)
from diagnose_calibration_bias import prepare_xy, rows_for_predictions, summarize

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "bias-per-model-diagnostic.json"
VARIANTS = ("per_model_absolute", "per_model_squared")
MIN_PER_MODEL_TRAIN = 500
MIN_PER_MODEL_TEST = 100


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


def fit_per_model(train, test, loss):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor

    predictions = [float("nan")] * len(test)
    used = {}
    for model_key in sorted({case.get("model") for case in test if case.get("model")}):
        train_idx = [i for i, case in enumerate(train) if case.get("model") == model_key]
        test_idx = [i for i, case in enumerate(test) if case.get("model") == model_key]
        model_train = [train[i] for i in train_idx]
        model_test = [test[i] for i in test_idx]
        if len(model_train) < MIN_PER_MODEL_TRAIN or len(model_test) < MIN_PER_MODEL_TEST:
            raise RuntimeError(
                f"Insufficient per-model cases for {model_key}: train={len(model_train)} test={len(model_test)}"
            )
        x_train, y_train, x_test, features = prepare_xy(model_train, model_test)
        params = dict(MODEL_PARAMS)
        params["loss"] = loss
        reg = HistGradientBoostingRegressor(**params)
        reg.fit(x_train, y_train)
        pred = reg.predict(x_test)
        for position, value in zip(test_idx, pred):
            predictions[position] = float(value)
        used[model_key] = {
            "train_cases": len(model_train),
            "test_cases": len(model_test),
            "features_used": features,
        }

    if not all(np.isfinite(value) for value in predictions):
        raise RuntimeError("Per-model predictions contain missing values")
    return np.asarray(predictions, dtype=float), used


def stability_gate(folds, aggregate_summary):
    improvements = []
    wins = 0
    for fold in folds:
        value = fold["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]
        if finite(value):
            improvements.append(float(value))
            if float(value) > 0:
                wins += 1
    required = math.ceil(len(folds) * 0.80)
    worst = min(improvements) if improvements else None
    aggregate = aggregate_summary["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]
    stable = bool(
        wins >= required
        and finite(aggregate)
        and float(aggregate) >= 3.0
        and finite(worst)
        and float(worst) > -5.0
    )
    return {
        "fold_wins": wins,
        "required_fold_wins": required,
        "worst_fold_improvement_vs_deterministic_pct": rounded(worst, 1),
        "aggregate_improvement_vs_deterministic_pct": rounded(aggregate, 1),
        "stable_walk_forward_win": stable,
        "strict_aggregate_guard_pass": bool(
            aggregate_summary["strict_guard"]["strict_holdout_pass"]
        ),
        "candidate_pass": bool(
            stable and aggregate_summary["strict_guard"]["strict_holdout_pass"]
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

    all_rows = {name: [] for name in VARIANTS}
    fold_results = {name: [] for name in VARIANTS}
    skipped_folds = []

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

        train_counts = Counter(case.get("model") for case in train)
        test_counts = Counter(case.get("model") for case in test)
        insufficient = {
            model: {
                "train_cases": int(train_counts.get(model, 0)),
                "test_cases": int(test_counts.get(model, 0)),
            }
            for model in sorted(test_counts)
            if test_counts.get(model, 0) >= MIN_PER_MODEL_TEST
            and train_counts.get(model, 0) < MIN_PER_MODEL_TRAIN
        }
        if insufficient:
            skipped_folds.append(
                {
                    "fold": fold_number,
                    "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                    "test_start_utc": test_start,
                    "test_last_target_utc": test[-1]["target_at_utc"],
                    "reason": "per-model challenger cannot be trained fairly before every tested model has enough prior history",
                    "insufficient_models": insufficient,
                }
            )
            continue

        deterministic_rows, _, _, _, _ = evaluate(train, test)
        for variant, loss in (
            ("per_model_absolute", "absolute_error"),
            ("per_model_squared", "squared_error"),
        ):
            predictions, model_training = fit_per_model(train, test, loss)
            rows = rows_for_predictions(test, deterministic_rows, predictions)
            all_rows[variant].extend(rows)
            summary = summarize(rows)
            fold_results[variant].append(
                {
                    "fold": fold_number,
                    "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                    "test_start_utc": test_start,
                    "test_last_target_utc": test[-1]["target_at_utc"],
                    "train_cases": len(train),
                    "test_cases": len(test),
                    "model_training": model_training,
                    "metrics": summary["metrics"],
                    "by_model": summary["by_model"],
                    "strict_guard": summary["strict_guard"],
                }
            )

    if min(len(items) for items in fold_results.values()) < 3:
        raise RuntimeError(
            "Too few successful per-model diagnostic folds after fair-history filtering: "
            f"successful={min(len(items) for items in fold_results.values())}, skipped={len(skipped_folds)}"
        )

    candidates = {}
    ranking = []
    for variant in VARIANTS:
        summary = summarize(all_rows[variant])
        gate = stability_gate(fold_results[variant], summary)
        candidates[variant] = {
            **summary,
            "stability_gate": gate,
            "folds": fold_results[variant],
        }
        ranking.append(
            {
                "variant": variant,
                "mae_c": summary["metrics"]["ml"]["mae_c"],
                "rmse_c": summary["metrics"]["ml"]["rmse_c"],
                "abs_bias_c": summary["metrics"]["ml"]["abs_bias_c"],
                "p95_abs_error_c": summary["metrics"]["ml"]["p95_abs_error_c"],
                "chmi_abs_bias_change_c": summary["by_model"].get("chmi", {}).get(
                    "ml_abs_bias_change_vs_deterministic_c"
                ),
                **gate,
            }
        )

    ranking.sort(
        key=lambda item: (
            not item["candidate_pass"],
            float(item["mae_c"]) if finite(item["mae_c"]) else 999.0,
        )
    )

    payload = {
        "schema": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "purpose": "test whether model-specific regressors remove pooled-model bias without sacrificing future-fold stability",
        "data": {
            "history_storage_files": storage,
            "history_cases": len(history),
            "live_archive_cases": len(live),
            "all_cases": len(cases),
            "folds_requested": FOLDS,
            "successful_folds": min(len(items) for items in fold_results.values()),
            "skipped_folds": skipped_folds,
            "minimum_per_model_train_cases": MIN_PER_MODEL_TRAIN,
            "minimum_per_model_test_cases": MIN_PER_MODEL_TEST,
        },
        "ranking": ranking,
        "candidates": candidates,
        "decision_rule": "A challenger must pass both the strict aggregate guards and the original future-fold stability rule. Folds before a tested model has enough prior history are excluded rather than borrowing information from another model. No production use is allowed.",
        "sklearn_version": sklearn.__version__,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ranking": ranking, "skipped_folds": skipped_folds}, ensure_ascii=False))


if __name__ == "__main__":
    main()
