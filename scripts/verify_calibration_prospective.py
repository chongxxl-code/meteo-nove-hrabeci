#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from calibration_truth import (
    DWD_DISTANCE_KM,
    DWD_STATION_ID,
    DWD_STATION_NAME,
    load_local_dwd_truth,
    nearest_truth,
)
from build_calibration_shadow import CAL, DATA, parse_dt
from build_calibration_ml_shadow import finite, improvement_pct

OUT = CAL / "prospective-summary.json"
CANDIDATE_ID = "pooled_squared_v1"
PREDICTION_GLOB = "prospective-pooled-????-??.jsonl"
MIN_EARLY_SIGNAL_ROWS = 100
MIN_EARLY_SIGNAL_ISSUE_TIMES = 5
MIN_CONFIRMATORY_ROWS = 500
MIN_CONFIRMATORY_ISSUE_TIMES = 20
MIN_CONFIRMATORY_SPAN_DAYS = 30.0
MIN_CONFIRMATORY_ROWS_PER_MODEL = 100


def rounded(value, digits=3):
    if value is None or not finite(value):
        return None
    return round(float(value), digits)


def percentile(values, q):
    values = sorted(float(value) for value in values if finite(value))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    weight = pos - lo
    return values[lo] * (1.0 - weight) + values[hi] * weight


def score(errors):
    errors = [float(value) for value in errors if finite(value)]
    if not errors:
        return {
            "n": 0,
            "mae_c": None,
            "rmse_c": None,
            "bias_c": None,
            "abs_bias_c": None,
            "p95_abs_error_c": None,
        }
    absolute = [abs(value) for value in errors]
    bias = sum(errors) / len(errors)
    return {
        "n": len(errors),
        "mae_c": rounded(sum(absolute) / len(absolute)),
        "rmse_c": rounded(math.sqrt(sum(value * value for value in errors) / len(errors))),
        "bias_c": rounded(bias),
        "abs_bias_c": rounded(abs(bias)),
        "p95_abs_error_c": rounded(percentile(absolute, 0.95)),
    }


def summarize(rows):
    raw = score([row["raw_error_c"] for row in rows])
    deterministic = score([row["deterministic_error_c"] for row in rows])
    ml = score([row["ml_error_c"] for row in rows])
    return {
        "n": len(rows),
        "raw": raw,
        "deterministic": deterministic,
        "ml": ml,
        "ml_improvement_vs_raw_pct": rounded(improvement_pct(raw["mae_c"], ml["mae_c"]), 1),
        "ml_improvement_vs_deterministic_pct": rounded(
            improvement_pct(deterministic["mae_c"], ml["mae_c"]), 1
        ),
    }


def load_predictions():
    rows = []
    for path in sorted(CAL.glob(PREDICTION_GLOB)):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("candidate") == CANDIDATE_ID:
                    rows.append(row)
            except Exception:
                continue
    rows.sort(
        key=lambda row: (
            row.get("issued_at_utc") or "",
            row.get("target_at_utc") or "",
            row.get("model") or "",
        )
    )
    return rows


def issue_span_days(issue_times):
    parsed = [parse_dt(value) for value in issue_times]
    parsed = [value for value in parsed if value is not None]
    if len(parsed) < 2:
        return 0.0
    return (max(parsed) - min(parsed)).total_seconds() / 86400.0


def not_worse_pct(candidate, reference, allowance_pct):
    if not finite(candidate) or not finite(reference):
        return False
    reference = float(reference)
    candidate = float(candidate)
    if reference <= 0:
        return candidate <= reference
    return candidate <= reference * (1.0 + allowance_pct / 100.0)


def main():
    predictions = load_predictions()
    truth, truth_times = load_local_dwd_truth(DATA / "observations")
    now = datetime.now(timezone.utc)
    matured = []
    pending = 0
    unmatched = 0

    for row in predictions:
        target = parse_dt(row.get("target_at_utc"))
        if target is None or target > now:
            pending += 1
            continue
        matched = nearest_truth(target, truth, truth_times)
        if matched is None:
            unmatched += 1
            continue
        observed_at, observation, delta = matched
        actual = float(observation["temperature_c"])
        matured.append(
            {
                **row,
                "truth_temperature_c": actual,
                "truth_observed_at_utc": observed_at.isoformat().replace("+00:00", "Z"),
                "truth_match_delta_minutes": round(float(delta), 1),
                "raw_error_c": float(row["raw_temperature_c"]) - actual,
                "deterministic_error_c": float(row["deterministic_temperature_c"]) - actual,
                "ml_error_c": float(row["ml_temperature_c"]) - actual,
            }
        )

    overall = summarize(matured)
    by_model = {}
    for model in sorted({row.get("model") for row in matured if row.get("model")}):
        by_model[model] = summarize([row for row in matured if row.get("model") == model])

    by_lead = {}
    for bucket in ("0–6 h", "6–12 h", "12–24 h", "24–48 h", "48–72 h"):
        subset = [row for row in matured if row.get("lead_bin") == bucket]
        if subset:
            by_lead[bucket] = summarize(subset)

    issue_times = sorted({row.get("issued_at_utc") for row in predictions if row.get("issued_at_utc")})
    matured_issue_times = sorted({row.get("issued_at_utc") for row in matured if row.get("issued_at_utc")})
    matured_span_days = issue_span_days(matured_issue_times)

    early_ready = bool(
        overall["n"] >= MIN_EARLY_SIGNAL_ROWS
        and len(matured_issue_times) >= MIN_EARLY_SIGNAL_ISSUE_TIMES
        and finite(overall["ml_improvement_vs_deterministic_pct"])
    )

    model_coverage_pass = all(
        model in by_model and by_model[model]["n"] >= MIN_CONFIRMATORY_ROWS_PER_MODEL
        for model in ("chmi", "dwd", "ec")
    )
    per_model_nonnegative = bool(
        model_coverage_pass
        and all(
            finite(by_model[model]["ml_improvement_vs_deterministic_pct"])
            and float(by_model[model]["ml_improvement_vs_deterministic_pct"]) >= 0.0
            for model in ("chmi", "dwd", "ec")
        )
    )

    enough_confirmatory_data = bool(
        overall["n"] >= MIN_CONFIRMATORY_ROWS
        and len(matured_issue_times) >= MIN_CONFIRMATORY_ISSUE_TIMES
        and matured_span_days >= MIN_CONFIRMATORY_SPAN_DAYS
        and model_coverage_pass
    )
    confirmatory_metrics_pass = bool(
        enough_confirmatory_data
        and finite(overall["ml_improvement_vs_deterministic_pct"])
        and float(overall["ml_improvement_vs_deterministic_pct"]) > 0.0
        and not_worse_pct(overall["ml"]["rmse_c"], overall["deterministic"]["rmse_c"], 0.0)
        and not_worse_pct(overall["ml"]["p95_abs_error_c"], overall["deterministic"]["p95_abs_error_c"], 2.0)
        and finite(overall["ml"]["abs_bias_c"])
        and finite(overall["deterministic"]["abs_bias_c"])
        and float(overall["ml"]["abs_bias_c"])
        <= float(overall["deterministic"]["abs_bias_c"]) + 0.15
        and per_model_nonnegative
    )

    payload = {
        "schema": 2,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "candidate": CANDIDATE_ID,
        "purpose": (
            "prospective timestamped verification of the frozen pooled squared-error candidate; "
            "predictions are persisted before target truth exists"
        ),
        "truth_reference": {
            "station": DWD_STATION_NAME,
            "station_id": DWD_STATION_ID,
            "distance_to_nove_hrabeci_km": DWD_DISTANCE_KM,
            "is_nove_hrabeci_truth": False,
            "warning": (
                "This is independent prospective evidence only against the Sohland proxy; "
                "it cannot authorize Nové Hraběcí production use."
            ),
        },
        "coverage": {
            "prediction_rows": len(predictions),
            "forecast_issue_times": len(issue_times),
            "matured_rows": len(matured),
            "matured_issue_times": len(matured_issue_times),
            "matured_issue_span_days": rounded(matured_span_days, 2),
            "pending_rows": pending,
            "truth_unmatched_rows": unmatched,
            "first_issue_utc": issue_times[0] if issue_times else None,
            "last_issue_utc": issue_times[-1] if issue_times else None,
        },
        "prospective_metrics": {
            "overall": overall,
            "by_model": by_model,
            "by_lead_bin": by_lead,
        },
        "readiness": {
            "early_signal": {
                "minimum_matured_rows": MIN_EARLY_SIGNAL_ROWS,
                "minimum_matured_issue_times": MIN_EARLY_SIGNAL_ISSUE_TIMES,
                "ready": early_ready,
                "candidate_beating_deterministic_so_far": bool(
                    early_ready
                    and float(overall["ml_improvement_vs_deterministic_pct"]) > 0.0
                ),
            },
            "proxy_confirmatory_gate": {
                "minimum_matured_rows": MIN_CONFIRMATORY_ROWS,
                "minimum_matured_issue_times": MIN_CONFIRMATORY_ISSUE_TIMES,
                "minimum_issue_span_days": MIN_CONFIRMATORY_SPAN_DAYS,
                "minimum_rows_per_model": MIN_CONFIRMATORY_ROWS_PER_MODEL,
                "enough_data": enough_confirmatory_data,
                "all_models_covered": model_coverage_pass,
                "all_models_nonnegative_vs_deterministic": per_model_nonnegative,
                "metrics_pass": confirmatory_metrics_pass,
                "rule": (
                    "After >=30 days and adequate coverage, ML must beat deterministic MAE, not worsen RMSE, "
                    "keep p95 within +2%, abs bias within +0.15 °C, and be non-worse in each NWP source."
                ),
                "production_eligible": False,
            },
            "next_gate": (
                "Accumulate untouched future forecast issues for at least 30 days. Even a proxy-confirmatory "
                "pass remains shadow-only until repeated against an on-site Nové Hraběcí truth station."
            ),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "candidate": CANDIDATE_ID,
                "coverage": payload["coverage"],
                "overall": overall,
                "early_ready": early_ready,
                "proxy_confirmatory_pass": confirmatory_metrics_pass,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
