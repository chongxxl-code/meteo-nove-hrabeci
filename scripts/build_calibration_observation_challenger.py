#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from calibration_truth import issue_observation_context, load_local_dwd_truth
from build_calibration_shadow import (
    DATA,
    evaluate,
    history_cases,
    live_cases,
    load_history_rows,
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
from diagnose_calibration_bias import rows_for_predictions, summarize

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "calibration" / "observation-challenger.json"

OBS_FEATURE_NAMES = [
    "obs_issue_temperature_c",
    "obs_issue_relative_humidity_pct",
    "obs_issue_age_minutes",
    "obs_issue_temperature_1h_ago_c",
    "obs_issue_temperature_3h_ago_c",
    "obs_issue_temperature_6h_ago_c",
    "obs_issue_temperature_change_1h_c",
    "obs_issue_temperature_change_3h_c",
    "obs_issue_temperature_change_6h_c",
    "forecast_minus_obs_issue_temperature_c",
]

MIN_CONTEXT_CASES = 10_000
MIN_ADDED_VALUE_PCT = 1.0
MAX_FOLD_DEGRADATION_PCT = 5.0


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


def case_key(case):
    return (
        case.get("model"),
        case.get("target_at_utc"),
        round(float(case.get("lead_h") or 0.0), 3),
    )


def context_is_safe(case):
    context = case.get("observed_context_at_issue") or {}
    if not finite(context.get("temperature_c")):
        return False
    issued = parse_dt(case.get("issued_at_utc"))
    observed = parse_dt(context.get("observed_at_utc"))
    if issued is None or observed is None or observed > issued:
        return False
    for hours in (1, 3, 6):
        stamp = parse_dt(context.get(f"temperature_{hours}h_ago_at_utc"))
        if stamp is not None and stamp > issued:
            return False
    return True


def attach_issue_context(history, live):
    raw_rows, storage = load_history_rows()
    raw_context = {
        case_key(row): row.get("observed_context_at_issue")
        for row in raw_rows
        if row.get("observed_context_at_issue")
    }

    enriched_history = []
    history_with_context = 0
    history_unsafe = 0
    for case in history:
        enriched = {**case, "observed_context_at_issue": raw_context.get(case_key(case))}
        if context_is_safe(enriched):
            history_with_context += 1
        elif enriched.get("observed_context_at_issue"):
            history_unsafe += 1
        enriched_history.append(enriched)

    truth, truth_times = load_local_dwd_truth(DATA / "observations")
    enriched_live = []
    live_with_context = 0
    live_unsafe = 0
    for case in live:
        issued = parse_dt(case.get("issued_at_utc"))
        context = issue_observation_context(issued, truth, truth_times) if issued else None
        enriched = {**case, "observed_context_at_issue": context}
        if context_is_safe(enriched):
            live_with_context += 1
        elif context:
            live_unsafe += 1
        enriched_live.append(enriched)

    return enriched_history, enriched_live, {
        "history_storage_files": storage,
        "history_cases": len(history),
        "history_context_cases": history_with_context,
        "history_context_unsafe_rejected": history_unsafe,
        "live_cases": len(live),
        "live_context_cases": live_with_context,
        "live_context_unsafe_rejected": live_unsafe,
    }


def observation_features(case):
    context = case.get("observed_context_at_issue") or {}
    forecast_temp = (case.get("forecast") or {}).get("temperature_c")
    issue_temp = context.get("temperature_c")
    delta = None
    if finite(forecast_temp) and finite(issue_temp):
        delta = float(forecast_temp) - float(issue_temp)
    return [
        context.get("temperature_c"),
        context.get("relative_humidity_pct"),
        context.get("age_minutes_at_issue"),
        context.get("temperature_1h_ago_c"),
        context.get("temperature_3h_ago_c"),
        context.get("temperature_6h_ago_c"),
        context.get("temperature_change_1h_c"),
        context.get("temperature_change_3h_c"),
        context.get("temperature_change_6h_c"),
        delta,
    ]


def challenger_features(case):
    return case_features(case) + observation_features(case)


def prepare_xy(train, test, feature_fn, feature_names):
    import numpy as np

    x_train = np.asarray([feature_fn(case) for case in train], dtype=float)
    y_train = np.asarray([float(case["error_c"]) for case in train], dtype=float)
    x_test = np.asarray([feature_fn(case) for case in test], dtype=float)
    minimum = max(MIN_FEATURE_FINITE_N, int(len(train) * MIN_FEATURE_FINITE_FRACTION))
    finite_counts = np.isfinite(x_train).sum(axis=0)
    keep_mask = finite_counts >= minimum
    if not np.any(keep_mask):
        raise RuntimeError("No usable challenger features after coverage filtering")
    used = [name for name, keep in zip(feature_names, keep_mask) if bool(keep)]
    dropped = [
        {"feature": name, "finite_train_n": int(count)}
        for name, count, keep in zip(feature_names, finite_counts, keep_mask)
        if not bool(keep)
    ]
    return x_train[:, keep_mask], y_train, x_test[:, keep_mask], used, dropped


def fit_predict(train, test, feature_fn, feature_names):
    from sklearn.ensemble import HistGradientBoostingRegressor

    x_train, y_train, x_test, used, dropped = prepare_xy(
        train, test, feature_fn, feature_names
    )
    params = dict(MODEL_PARAMS)
    params["loss"] = "squared_error"
    model = HistGradientBoostingRegressor(**params)
    model.fit(x_train, y_train)
    return model.predict(x_test), used, dropped


def fold_gate(folds, variant):
    improvements = []
    wins = 0
    for fold in folds:
        value = fold[variant]["metrics"]["ml_improvement_vs_deterministic_pct"]["mae"]
        if finite(value):
            value = float(value)
            improvements.append(value)
            if value > 0:
                wins += 1
    required = math.ceil(len(improvements) * 0.80) if improvements else 0
    worst = min(improvements) if improvements else None
    return {
        "fold_wins_vs_deterministic": wins,
        "required_fold_wins": required,
        "worst_fold_improvement_vs_deterministic_pct": rounded(worst, 1),
        "stable_vs_deterministic": bool(
            improvements
            and wins >= required
            and finite(worst)
            and float(worst) > -MAX_FOLD_DEGRADATION_PCT
        ),
    }


def observation_vs_control_gate(folds, control_summary, observation_summary):
    control_mae = control_summary["metrics"]["ml"]["mae_c"]
    observation_mae = observation_summary["metrics"]["ml"]["mae_c"]
    aggregate_added = improvement_pct(control_mae, observation_mae)

    fold_values = []
    fold_wins = 0
    for fold in folds:
        control = fold["control_squared"]["metrics"]["ml"]["mae_c"]
        observation = fold["observation_squared"]["metrics"]["ml"]["mae_c"]
        value = improvement_pct(control, observation)
        if finite(value):
            value = float(value)
            fold_values.append(value)
            if value > 0:
                fold_wins += 1
    required = math.ceil(len(fold_values) * 0.80) if fold_values else 0
    worst = min(fold_values) if fold_values else None

    control_chmi = (
        control_summary["by_model"].get("chmi", {}).get(
            "ml_abs_bias_change_vs_deterministic_c"
        )
    )
    observation_chmi = (
        observation_summary["by_model"].get("chmi", {}).get(
            "ml_abs_bias_change_vs_deterministic_c"
        )
    )
    chmi_bias_change_vs_control = None
    if finite(control_chmi) and finite(observation_chmi):
        chmi_bias_change_vs_control = float(observation_chmi) - float(control_chmi)

    meaningful = bool(finite(aggregate_added) and float(aggregate_added) >= MIN_ADDED_VALUE_PCT)
    stable = bool(
        fold_values
        and fold_wins >= required
        and finite(worst)
        and float(worst) > -MAX_FOLD_DEGRADATION_PCT
    )
    chmi_not_worse = bool(
        chmi_bias_change_vs_control is not None and chmi_bias_change_vs_control <= 0.05
    )
    return {
        "aggregate_mae_added_value_vs_control_pct": rounded(aggregate_added, 1),
        "minimum_meaningful_added_value_pct": MIN_ADDED_VALUE_PCT,
        "meaningful_added_value": meaningful,
        "fold_wins_vs_control": fold_wins,
        "required_fold_wins": required,
        "worst_fold_added_value_vs_control_pct": rounded(worst, 1),
        "stable_added_value": stable,
        "chmi_abs_bias_change_vs_control_c": rounded(chmi_bias_change_vs_control),
        "chmi_bias_not_worse_by_more_than_c": {"limit_c": 0.05, "pass": chmi_not_worse},
        "pass": bool(meaningful and stable and chmi_not_worse),
    }


def main():
    import sklearn

    history, _ = history_cases(return_storage=True)
    live = live_cases()
    history, live, coverage = attach_issue_context(history, live)
    all_cases = history + live
    all_cases.sort(
        key=lambda case: (
            case["target_at_utc"], case["model"], case["lead_h"], case["source"]
        )
    )
    eligible = [case for case in all_cases if context_is_safe(case)]
    coverage["all_cases"] = len(all_cases)
    coverage["context_eligible_cases"] = len(eligible)
    coverage["context_coverage_pct"] = rounded(
        len(eligible) / len(all_cases) * 100.0 if all_cases else 0.0, 1
    )

    if len(eligible) < MIN_CONTEXT_CASES:
        raise RuntimeError(
            f"Insufficient leakage-safe issue context: {len(eligible)} cases; "
            f"need at least {MIN_CONTEXT_CASES}. Run the historical backfill first."
        )

    targets = sorted({case["target_at_utc"] for case in all_cases})
    folds = []
    control_rows_all = []
    observation_rows_all = []

    for fold_number, (start_index, end_index) in enumerate(fold_ranges(targets), start=1):
        test_start = targets[start_index]
        test_end = targets[end_index] if end_index < len(targets) else None
        test = [
            case for case in eligible
            if case["target_at_utc"] >= test_start
            and (test_end is None or case["target_at_utc"] < test_end)
        ]
        as_of = earliest_test_issue(test)
        if as_of is None:
            continue
        train = []
        for case in eligible:
            available = truth_available_at(case)
            if available is not None and available <= as_of:
                train.append(case)
        if len(train) < MIN_TRAIN_CASES or len(test) < MIN_TEST_CASES:
            continue

        deterministic_rows, _, _, _, _ = evaluate(train, test)
        control_predictions, control_used, control_dropped = fit_predict(
            train, test, case_features, FEATURE_NAMES
        )
        observation_predictions, observation_used, observation_dropped = fit_predict(
            train,
            test,
            challenger_features,
            FEATURE_NAMES + OBS_FEATURE_NAMES,
        )

        control_rows = rows_for_predictions(test, deterministic_rows, control_predictions)
        observation_rows = rows_for_predictions(
            test, deterministic_rows, observation_predictions
        )
        control_summary = summarize(control_rows)
        observation_summary = summarize(observation_rows)
        control_rows_all.extend(control_rows)
        observation_rows_all.extend(observation_rows)

        folds.append(
            {
                "fold": fold_number,
                "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
                "test_start_utc": test_start,
                "test_last_target_utc": test[-1]["target_at_utc"],
                "train_cases": len(train),
                "test_cases": len(test),
                "control_squared": {
                    "features_used": control_used,
                    "features_dropped": control_dropped,
                    "metrics": control_summary["metrics"],
                    "by_model": control_summary["by_model"],
                },
                "observation_squared": {
                    "features_used": observation_used,
                    "features_dropped": observation_dropped,
                    "metrics": observation_summary["metrics"],
                    "by_model": observation_summary["by_model"],
                },
            }
        )

    if len(folds) < 3:
        raise RuntimeError(f"Too few observation-challenger folds: {len(folds)}")

    control_summary = summarize(control_rows_all)
    observation_summary = summarize(observation_rows_all)
    control_stability = fold_gate(folds, "control_squared")
    observation_stability = fold_gate(folds, "observation_squared")
    added_value = observation_vs_control_gate(
        folds, control_summary, observation_summary
    )

    observation_aggregate_improvement = observation_summary["metrics"][
        "ml_improvement_vs_deterministic_pct"
    ]["mae"]
    observation_stability["aggregate_improvement_vs_deterministic_pct"] = (
        observation_aggregate_improvement
    )
    observation_stability["minimum_aggregate_improvement_pct"] = 3.0
    observation_stability["stable_vs_deterministic"] = bool(
        observation_stability["stable_vs_deterministic"]
        and finite(observation_aggregate_improvement)
        and float(observation_aggregate_improvement) >= 3.0
    )

    strict_pass = bool(
        observation_summary["strict_guard"]["strict_holdout_pass"]
    )
    candidate_pass = bool(
        strict_pass
        and observation_stability["stable_vs_deterministic"]
        and added_value["pass"]
    )

    payload = {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "purpose": (
            "test whether leakage-safe observations available at forecast issuance "
            "improve pooled squared-error temperature calibration and stabilize future folds"
        ),
        "method": {
            "control": "pooled HistGradientBoosting squared-error using forecast/time/model features only",
            "challenger": (
                "same estimator and chronological folds plus DWD Sohland observation at issue time, "
                "1/3/6 h temperature history and trends"
            ),
            "leakage_guard": (
                "issue observation timestamp and all lag timestamps must be <= forecast issued_at_utc; "
                "forecast target truth remains unavailable to training until its observation timestamp"
            ),
            "comparison_universe": "control and observation challenger use the exact same context-eligible cases",
        },
        "coverage": coverage,
        "control_squared": {
            **control_summary,
            "stability_gate": control_stability,
        },
        "observation_squared": {
            **observation_summary,
            "stability_gate": observation_stability,
            "strict_aggregate_guard_pass": strict_pass,
        },
        "added_value_gate": added_value,
        "candidate_pass": candidate_pass,
        "decision": (
            "promising_shadow_candidate" if candidate_pass else "reject_or_keep_shadow"
        ),
        "production_blocker": (
            "No on-site Nové Hraběcí truth station yet; this remains a Sohland-proxy shadow experiment "
            "even if all challenger gates pass."
        ),
        "folds": folds,
        "sklearn_version": sklearn.__version__,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "coverage": coverage,
                "control_mae_c": control_summary["metrics"]["ml"]["mae_c"],
                "observation_mae_c": observation_summary["metrics"]["ml"]["mae_c"],
                "observation_vs_control": added_value,
                "observation_stability": observation_stability,
                "strict_pass": strict_pass,
                "candidate_pass": candidate_pass,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
