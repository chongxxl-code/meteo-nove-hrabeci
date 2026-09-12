#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from build_calibration_shadow import (
    chronological_split,
    evaluate,
    history_cases,
    live_cases,
    parse_dt,
)
from build_calibration_ml_shadow import (
    FEATURE_NAMES,
    MODEL_PARAMS,
    case_features,
    finite,
    improvement_pct,
)
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

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "strict-validation.json"

MIN_EXTREME_CASES = 100
MAX_BIAS_DEGRADATION_C = 0.15
MAX_TAIL_DEGRADATION_PCT = 2.0
MAX_EXTREME_MAE_DEGRADATION_PCT = 5.0
MIN_TARGET_BALANCED_IMPROVEMENT_PCT = 3.0
MIN_FOLD_BALANCED_IMPROVEMENT_PCT = 3.0


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


def mean(values):
    return sum(values) / len(values) if values else None


def rmse(values):
    if not values:
        return None
    return math.sqrt(sum(float(v) ** 2 for v in values) / len(values))


def percentile(values, q):
    if not values:
        return None
    values = sorted(float(v) for v in values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * float(q)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def method_metrics(errors):
    errors = [float(value) for value in errors]
    absolute = [abs(value) for value in errors]
    return {
        "n": len(errors),
        "mae_c": rounded(mean(absolute)),
        "rmse_c": rounded(rmse(errors)),
        "bias_c": rounded(mean(errors)),
        "abs_bias_c": rounded(abs(mean(errors))) if errors else None,
        "p90_abs_error_c": rounded(percentile(absolute, 0.90)),
        "p95_abs_error_c": rounded(percentile(absolute, 0.95)),
        "max_abs_error_c": rounded(max(absolute) if absolute else None),
    }


def comparison_metrics(rows):
    raw = method_metrics([row["raw_error_c"] for row in rows])
    deterministic = method_metrics([row["deterministic_error_c"] for row in rows])
    ml = method_metrics([row["ml_error_c"] for row in rows])
    return {
        "n": len(rows),
        "raw": raw,
        "deterministic": deterministic,
        "ml": ml,
        "ml_improvement_vs_deterministic_pct": {
            "mae": rounded(improvement_pct(deterministic["mae_c"], ml["mae_c"]), 1),
            "rmse": rounded(improvement_pct(deterministic["rmse_c"], ml["rmse_c"]), 1),
            "p90_abs_error": rounded(
                improvement_pct(deterministic["p90_abs_error_c"], ml["p90_abs_error_c"]), 1
            ),
            "p95_abs_error": rounded(
                improvement_pct(deterministic["p95_abs_error_c"], ml["p95_abs_error_c"]), 1
            ),
        },
        "ml_abs_bias_change_vs_deterministic_c": rounded(
            (ml["abs_bias_c"] or 0.0) - (deterministic["abs_bias_c"] or 0.0)
        ),
    }


def target_balanced_metrics(rows):
    grouped = defaultdict(list)
    for row in rows:
        target = row.get("target_at_utc")
        if target:
            grouped[target].append(row)

    def balanced(method):
        abs_means = []
        sq_means = []
        signed_means = []
        for group in grouped.values():
            errors = [float(item[f"{method}_error_c"]) for item in group]
            abs_means.append(mean([abs(value) for value in errors]))
            sq_means.append(mean([value * value for value in errors]))
            signed_means.append(mean(errors))
        return {
            "mae_c": rounded(mean(abs_means)),
            "rmse_c": rounded(math.sqrt(mean(sq_means))) if sq_means else None,
            "bias_c": rounded(mean(signed_means)),
            "abs_bias_c": rounded(abs(mean(signed_means))) if signed_means else None,
            "p90_target_mean_abs_error_c": rounded(percentile(abs_means, 0.90)),
            "p95_target_mean_abs_error_c": rounded(percentile(abs_means, 0.95)),
        }

    raw = balanced("raw")
    deterministic = balanced("deterministic")
    ml = balanced("ml")
    return {
        "unique_target_times": len(grouped),
        "raw": raw,
        "deterministic": deterministic,
        "ml": ml,
        "ml_improvement_vs_deterministic_pct": {
            "mae": rounded(improvement_pct(deterministic["mae_c"], ml["mae_c"]), 1),
            "rmse": rounded(improvement_pct(deterministic["rmse_c"], ml["rmse_c"]), 1),
        },
    }


def threshold_skill(rows, threshold_c, direction):
    if direction not in {"le", "ge"}:
        raise ValueError(direction)

    def is_event(value):
        value = float(value)
        return value <= threshold_c if direction == "le" else value >= threshold_c

    truth_events = sum(1 for row in rows if is_event(row["truth_temperature_c"]))
    out = {"threshold_c": threshold_c, "direction": direction, "truth_event_cases": truth_events}
    for method in ("raw", "deterministic", "ml"):
        hits = misses = false_alarms = correct_negatives = 0
        for row in rows:
            truth_event = is_event(row["truth_temperature_c"])
            forecast_event = is_event(row[f"{method}_temperature_c"])
            if truth_event and forecast_event:
                hits += 1
            elif truth_event and not forecast_event:
                misses += 1
            elif not truth_event and forecast_event:
                false_alarms += 1
            else:
                correct_negatives += 1
        pod = hits / (hits + misses) if hits + misses else None
        far = false_alarms / (hits + false_alarms) if hits + false_alarms else None
        csi = hits / (hits + misses + false_alarms) if hits + misses + false_alarms else None
        out[method] = {
            "hits": hits,
            "misses": misses,
            "false_alarms": false_alarms,
            "correct_negatives": correct_negatives,
            "pod": rounded(pod, 3),
            "far": rounded(far, 3),
            "csi": rounded(csi, 3),
        }
    return out


def extreme_subset(rows, predicate):
    subset = [row for row in rows if predicate(float(row["truth_temperature_c"]))]
    return comparison_metrics(subset) if subset else {"n": 0}


def evaluate_ml(train, test):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor

    deterministic_rows, _, _, _, _ = evaluate(train, test)
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
        raise RuntimeError("No usable ML features after coverage filtering")

    model = HistGradientBoostingRegressor(**MODEL_PARAMS)
    model.fit(x_train[:, keep_mask], y_train)
    predicted_error = model.predict(x_test[:, keep_mask])

    evaluated = []
    for case, deterministic_row, prediction in zip(test, deterministic_rows, predicted_error):
        raw_error = float(case["error_c"])
        deterministic_error = float(deterministic_row["corrected_error_c"])
        ml_error = raw_error - float(prediction)
        raw_temp = float(case["forecast"]["temperature_c"])
        truth_temp = float(case["truth_temperature_c"])
        deterministic_temp = raw_temp - float(deterministic_row["correction_c"])
        ml_temp = raw_temp - float(prediction)
        evaluated.append(
            {
                **case,
                "truth_temperature_c": truth_temp,
                "raw_temperature_c": raw_temp,
                "deterministic_temperature_c": deterministic_temp,
                "ml_temperature_c": ml_temp,
                "raw_error_c": raw_error,
                "deterministic_error_c": deterministic_error,
                "ml_error_c": ml_error,
            }
        )

    features_used = [name for name, keep in zip(FEATURE_NAMES, keep_mask) if bool(keep)]
    return evaluated, features_used


def subgroup_diagnostics(rows):
    models = sorted({row.get("model") for row in rows if row.get("model")})
    by_model = {
        key: comparison_metrics([row for row in rows if row.get("model") == key])
        for key in models
    }
    lead_bins = ("0–6 h", "6–12 h", "12–24 h", "24–48 h", "48–72 h")
    by_lead = {
        key: comparison_metrics([row for row in rows if row.get("lead_bin") == key])
        for key in lead_bins
        if any(row.get("lead_bin") == key for row in rows)
    }
    return by_model, by_lead


def guard_result(metrics, balanced, extremes, by_model):
    det = metrics["deterministic"]
    ml = metrics["ml"]
    target_improvement = balanced["ml_improvement_vs_deterministic_pct"]["mae"]

    rmse_guard = bool(
        finite(det.get("rmse_c")) and finite(ml.get("rmse_c")) and ml["rmse_c"] <= det["rmse_c"]
    )
    p95_guard = bool(
        finite(det.get("p95_abs_error_c"))
        and finite(ml.get("p95_abs_error_c"))
        and (metrics["ml_improvement_vs_deterministic_pct"].get("p95_abs_error") or 0)
        >= -MAX_TAIL_DEGRADATION_PCT
    )
    bias_guard = bool(
        finite(det.get("abs_bias_c"))
        and finite(ml.get("abs_bias_c"))
        and ml["abs_bias_c"] <= det["abs_bias_c"] + MAX_BIAS_DEGRADATION_C
    )
    target_guard = bool(
        finite(target_improvement) and target_improvement >= MIN_TARGET_BALANCED_IMPROVEMENT_PCT
    )

    extreme_guards = {}
    for name, result in extremes.items():
        n = int(result.get("n") or 0)
        improvement = (result.get("ml_improvement_vs_deterministic_pct") or {}).get("mae")
        extreme_guards[name] = {
            "n": n,
            "enforced": n >= MIN_EXTREME_CASES,
            "ml_improvement_vs_deterministic_pct": improvement,
            "pass": True if n < MIN_EXTREME_CASES else bool(
                finite(improvement) and improvement >= -MAX_EXTREME_MAE_DEGRADATION_PCT
            ),
        }

    model_bias_guards = {}
    for model, result in by_model.items():
        change = result.get("ml_abs_bias_change_vs_deterministic_c")
        model_bias_guards[model] = {
            "abs_bias_change_c": change,
            "pass": bool(finite(change) and float(change) <= 0.40),
        }

    strict_pass = all(
        [
            rmse_guard,
            p95_guard,
            bias_guard,
            target_guard,
            all(item["pass"] for item in extreme_guards.values()),
            all(item["pass"] for item in model_bias_guards.values()),
        ]
    )
    return {
        "rmse_not_worse": rmse_guard,
        "p95_tail_not_worse_by_more_than_pct": {
            "limit_pct": MAX_TAIL_DEGRADATION_PCT,
            "pass": p95_guard,
        },
        "abs_bias_not_worse_by_more_than_c": {
            "limit_c": MAX_BIAS_DEGRADATION_C,
            "pass": bias_guard,
        },
        "target_balanced_mae_improvement": {
            "minimum_pct": MIN_TARGET_BALANCED_IMPROVEMENT_PCT,
            "actual_pct": target_improvement,
            "pass": target_guard,
        },
        "extreme_temperature_guards": extreme_guards,
        "per_model_abs_bias_guard": {
            "max_degradation_c": 0.40,
            "models": model_bias_guards,
            "pass": all(item["pass"] for item in model_bias_guards.values()),
        },
        "strict_holdout_pass": strict_pass,
    }


def main():
    try:
        import sklearn
    except Exception as exc:
        raise SystemExit(f"scikit-learn unavailable: {exc}")

    history, storage = history_cases(return_storage=True)
    live = live_cases()
    cases = history + live
    cases.sort(key=lambda case: (case["target_at_utc"], case["model"], case["lead_h"], case["source"]))

    train, test, cutoff = chronological_split(cases)
    holdout_rows, holdout_features = evaluate_ml(train, test)
    overall = comparison_metrics(holdout_rows)
    balanced = target_balanced_metrics(holdout_rows)
    by_model, by_lead = subgroup_diagnostics(holdout_rows)
    extremes = {
        "frost_truth_le_0c": extreme_subset(holdout_rows, lambda value: value <= 0.0),
        "hot_truth_ge_25c": extreme_subset(holdout_rows, lambda value: value >= 25.0),
    }
    events = {
        "frost_0c": threshold_skill(holdout_rows, 0.0, "le"),
        "hot_25c": threshold_skill(holdout_rows, 25.0, "ge"),
    }
    holdout_guard = guard_result(overall, balanced, extremes, by_model)

    targets = sorted({case["target_at_utc"] for case in cases})
    fold_summaries = []
    all_walk_rows = []
    for fold_number, (start_index, end_index) in enumerate(fold_ranges(targets), start=1):
        test_start = targets[start_index]
        test_end_exclusive = targets[end_index] if end_index < len(targets) else None
        if test_end_exclusive is None:
            fold_test = [case for case in cases if case["target_at_utc"] >= test_start]
        else:
            fold_test = [
                case for case in cases
                if test_start <= case["target_at_utc"] < test_end_exclusive
            ]
        as_of = earliest_test_issue(fold_test)
        if as_of is None:
            continue
        fold_train = []
        for case in cases:
            available = truth_available_at(case)
            if available is not None and available <= as_of:
                fold_train.append(case)
        if len(fold_train) < MIN_TRAIN_CASES or len(fold_test) < MIN_TEST_CASES:
            continue
        fold_rows, features_used = evaluate_ml(fold_train, fold_test)
        fold_metric = comparison_metrics(fold_rows)
        fold_balanced = target_balanced_metrics(fold_rows)
        fold_summaries.append(
            {
                "fold": fold_number,
                "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                "test_start_utc": test_start,
                "test_last_target_utc": fold_test[-1]["target_at_utc"],
                "train_cases": len(fold_train),
                "test_cases": len(fold_test),
                "features_used": features_used,
                "metrics": fold_metric,
                "target_balanced": fold_balanced,
            }
        )
        all_walk_rows.extend(fold_rows)

    if len(fold_summaries) < 3:
        raise RuntimeError(f"Too few strict walk-forward folds: {len(fold_summaries)}")

    walk_overall = comparison_metrics(all_walk_rows)
    walk_balanced = target_balanced_metrics(all_walk_rows)
    walk_by_model, walk_by_lead = subgroup_diagnostics(all_walk_rows)
    walk_extremes = {
        "frost_truth_le_0c": extreme_subset(all_walk_rows, lambda value: value <= 0.0),
        "hot_truth_ge_25c": extreme_subset(all_walk_rows, lambda value: value >= 25.0),
    }
    walk_events = {
        "frost_0c": threshold_skill(all_walk_rows, 0.0, "le"),
        "hot_25c": threshold_skill(all_walk_rows, 25.0, "ge"),
    }

    fold_det_mae = [fold["metrics"]["deterministic"]["mae_c"] for fold in fold_summaries]
    fold_ml_mae = [fold["metrics"]["ml"]["mae_c"] for fold in fold_summaries]
    fold_balanced_summary = {
        "successful_folds": len(fold_summaries),
        "deterministic_mean_fold_mae_c": rounded(mean(fold_det_mae)),
        "ml_mean_fold_mae_c": rounded(mean(fold_ml_mae)),
        "ml_improvement_vs_deterministic_pct": rounded(
            improvement_pct(mean(fold_det_mae), mean(fold_ml_mae)), 1
        ),
    }
    walk_guard = guard_result(walk_overall, walk_balanced, walk_extremes, walk_by_model)
    fold_guard = bool(
        finite(fold_balanced_summary["ml_improvement_vs_deterministic_pct"])
        and fold_balanced_summary["ml_improvement_vs_deterministic_pct"]
        >= MIN_FOLD_BALANCED_IMPROVEMENT_PCT
    )

    payload = {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "purpose": "validation hardening before any production use: RMSE, bias, tails, extremes, target balancing and fold balancing",
        "data": {
            "history_storage_files": storage,
            "history_cases": len(history),
            "live_archive_cases": len(live),
            "all_cases": len(cases),
        },
        "holdout": {
            "cutoff_target_utc": cutoff,
            "train_cases": len(train),
            "test_cases": len(test),
            "features_used": holdout_features,
            "metrics": overall,
            "target_balanced_metrics": balanced,
            "by_model": by_model,
            "by_lead_bin": by_lead,
            "extreme_temperature": extremes,
            "threshold_event_skill": events,
            "guard": holdout_guard,
        },
        "walk_forward": {
            "folds_requested": FOLDS,
            "successful_folds": len(fold_summaries),
            "metrics": walk_overall,
            "target_balanced_metrics": walk_balanced,
            "fold_balanced_metrics": fold_balanced_summary,
            "by_model": walk_by_model,
            "by_lead_bin": walk_by_lead,
            "extreme_temperature": walk_extremes,
            "threshold_event_skill": walk_events,
            "folds": fold_summaries,
            "guard": {
                **walk_guard,
                "fold_balanced_mae_improvement": {
                    "minimum_pct": MIN_FOLD_BALANCED_IMPROVEMENT_PCT,
                    "actual_pct": fold_balanced_summary["ml_improvement_vs_deterministic_pct"],
                    "pass": fold_guard,
                },
                "strict_walk_forward_pass": bool(walk_guard["strict_holdout_pass"] and fold_guard),
            },
        },
        "research_gate": {
            "strict_validation_pass": bool(
                holdout_guard["strict_holdout_pass"]
                and walk_guard["strict_holdout_pass"]
                and fold_guard
            ),
            "production_eligible": False,
            "production_blocker": "No on-site Nové Hraběcí truth station; strict validation is research-only and cannot alter the public forecast or alerts.",
            "next_if_pass": "Add leakage-safe recent observations and build multi-model Fusion v2 shadow.",
            "next_if_fail": "Inspect failed guard(s); do not add model complexity until the failure is understood.",
        },
        "rules": {
            "max_abs_bias_degradation_c": MAX_BIAS_DEGRADATION_C,
            "max_p95_tail_degradation_pct": MAX_TAIL_DEGRADATION_PCT,
            "max_extreme_mae_degradation_pct": MAX_EXTREME_MAE_DEGRADATION_PCT,
            "min_target_balanced_mae_improvement_pct": MIN_TARGET_BALANCED_IMPROVEMENT_PCT,
            "min_fold_balanced_mae_improvement_pct": MIN_FOLD_BALANCED_IMPROVEMENT_PCT,
            "min_extreme_cases_for_enforcement": MIN_EXTREME_CASES,
        },
        "sklearn_version": sklearn.__version__,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "research_gate": payload["research_gate"],
        "holdout_guard": holdout_guard,
        "walk_forward_guard": payload["walk_forward"]["guard"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
