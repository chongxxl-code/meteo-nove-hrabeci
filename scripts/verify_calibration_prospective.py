#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from calibration_truth import DWD_DISTANCE_KM, DWD_STATION_ID, DWD_STATION_NAME, load_local_dwd_truth, nearest_truth
from build_calibration_shadow import CAL, DATA, parse_dt
from build_calibration_ml_shadow import finite, improvement_pct

OUT = CAL / "prospective-summary.json"
MIN_REPORT_N = 30


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
        return {"n": 0, "mae_c": None, "rmse_c": None, "bias_c": None, "p95_abs_error_c": None}
    absolute = [abs(value) for value in errors]
    return {
        "n": len(errors),
        "mae_c": rounded(sum(absolute) / len(absolute)),
        "rmse_c": rounded(math.sqrt(sum(value * value for value in errors) / len(errors))),
        "bias_c": rounded(sum(errors) / len(errors)),
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
    for path in sorted(CAL.glob("prospective-????-??.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("candidate") == "observation_squared_v1":
                    rows.append(row)
            except Exception:
                continue
    return rows


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
    ready = bool(
        overall["n"] >= MIN_REPORT_N
        and finite(overall["ml_improvement_vs_deterministic_pct"])
    )

    payload = {
        "schema": 1,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "ok": True,
        "shadow_only": True,
        "allowed_to_affect_public_forecast": False,
        "allowed_to_affect_alerts": False,
        "production_eligible": False,
        "purpose": "prospective, timestamped verification of observation_squared_v1 forecasts that were written before target truth existed",
        "truth_reference": {
            "station": DWD_STATION_NAME,
            "station_id": DWD_STATION_ID,
            "distance_to_nove_hrabeci_km": DWD_DISTANCE_KM,
            "is_nove_hrabeci_truth": False,
            "warning": "This validates prospective behavior against the Sohland proxy only; it cannot authorize NH production use.",
        },
        "coverage": {
            "prediction_rows": len(predictions),
            "forecast_issue_times": len(issue_times),
            "matured_rows": len(matured),
            "matured_issue_times": len(matured_issue_times),
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
            "minimum_matured_rows_for_signal": MIN_REPORT_N,
            "enough_matured_rows_for_signal": ready,
            "candidate_beating_deterministic_so_far": bool(
                ready and float(overall["ml_improvement_vs_deterministic_pct"]) > 0
            ),
            "production_eligible": False,
            "next_gate": "Accumulate multiple forecast issue times across changing weather regimes, then repeat against an on-site Nové Hraběcí station before any production decision.",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "coverage": payload["coverage"],
                "overall": overall,
                "ready": ready,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
