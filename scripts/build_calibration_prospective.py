#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from calibration_truth import issue_observation_context, load_local_dwd_truth
from build_calibration_shadow import (
    CAL,
    DATA,
    FORECAST_FIELDS,
    build_bias_tables,
    correction_for,
    history_cases,
    lead_bin,
    live_cases,
    load_jsonl_dir,
    parse_dt,
)
from build_calibration_ml_shadow import FEATURE_NAMES, finite
from build_calibration_ml_walkforward import truth_available_at
from build_calibration_observation_challenger import (
    OBS_FEATURE_NAMES,
    attach_issue_context,
    challenger_features,
    context_is_safe,
    fit_predict,
)

CANDIDATE = CAL / "observation-challenger.json"
DESIRED_LEADS_H = (1, 3, 6, 12, 24, 36, 48, 60, 72)
MAX_LEAD_DISTANCE_H = 1.1
MIN_TRAIN_CASES = 5000


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def latest_snapshot():
    snapshots = load_jsonl_dir(DATA / "archive", "*.jsonl")
    ranked = []
    for snapshot in snapshots:
        stamp = parse_dt(snapshot.get("collected_at_utc"))
        if stamp is not None and snapshot.get("models"):
            ranked.append((stamp, snapshot))
    if not ranked:
        raise RuntimeError("No forecast snapshot available for prospective shadow run")
    return max(ranked, key=lambda item: item[0])


def build_future_cases(snapshot, issued, context):
    candidates = []
    for model, payload in (snapshot.get("models") or {}).items():
        times = payload.get("time") or []
        temperatures = payload.get("temperature_2m") or []
        for index, raw_target in enumerate(times):
            if index >= len(temperatures) or not finite(temperatures[index]):
                continue
            target = parse_dt(raw_target)
            if target is None:
                continue
            lead_h = (target - issued).total_seconds() / 3600.0
            bucket = lead_bin(lead_h)
            if bucket is None or lead_h <= 0:
                continue
            forecast = {}
            for source_field, target_field in FORECAST_FIELDS.items():
                values = payload.get(source_field) or []
                forecast[target_field] = values[index] if index < len(values) else None
            candidates.append(
                {
                    "source": "prospective_snapshot",
                    "model": model,
                    "issued_at_utc": iso_utc(issued),
                    "target_at_utc": iso_utc(target),
                    "lead_h": round(lead_h, 3),
                    "lead_bin": bucket,
                    "forecast": forecast,
                    "observed_context_at_issue": context,
                }
            )

    selected = []
    for model in sorted({case["model"] for case in candidates}):
        model_cases = [case for case in candidates if case["model"] == model]
        used_targets = set()
        for desired in DESIRED_LEADS_H:
            if not model_cases:
                continue
            case = min(model_cases, key=lambda item: abs(float(item["lead_h"]) - desired))
            distance = abs(float(case["lead_h"]) - desired)
            if distance > MAX_LEAD_DISTANCE_H or case["target_at_utc"] in used_targets:
                continue
            selected.append(case)
            used_targets.add(case["target_at_utc"])
    selected.sort(key=lambda case: (case["target_at_utc"], case["model"]))
    return selected


def existing_keys(path):
    keys = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            keys.add((row.get("issued_at_utc"), row.get("model"), row.get("target_at_utc")))
        except Exception:
            continue
    return keys


def main():
    if not CANDIDATE.exists():
        raise RuntimeError("Observation challenger result is missing; run calibration backfill first")
    candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    if not candidate.get("candidate_pass"):
        print(json.dumps({"ok": True, "skipped": True, "reason": "observation challenger gate is not passing"}))
        return

    issued, snapshot = latest_snapshot()
    truth, truth_times = load_local_dwd_truth(DATA / "observations")
    context = issue_observation_context(issued, truth, truth_times)
    context_probe = {"issued_at_utc": iso_utc(issued), "observed_context_at_issue": context}
    if not context_is_safe(context_probe):
        raise RuntimeError(
            "No sufficiently fresh leakage-safe DWD issue observation for latest forecast snapshot"
        )

    future = build_future_cases(snapshot, issued, context)
    if not future:
        raise RuntimeError("Latest snapshot has no usable future cases in requested lead set")

    history, _ = history_cases(return_storage=True)
    live = live_cases()
    history, live, coverage = attach_issue_context(history, live)
    train = []
    for case in history + live:
        if not context_is_safe(case):
            continue
        case_issue = parse_dt(case.get("issued_at_utc"))
        available = truth_available_at(case)
        if case_issue is None or case_issue > issued:
            continue
        if available is None or available > issued:
            continue
        train.append(case)
    if len(train) < MIN_TRAIN_CASES:
        raise RuntimeError(f"Only {len(train)} leakage-safe prospective training cases")

    predictions, features_used, features_dropped = fit_predict(
        train,
        future,
        challenger_features,
        FEATURE_NAMES + OBS_FEATURE_NAMES,
    )
    bias_tables = build_bias_tables(train)

    month = issued.strftime("%Y-%m")
    out = CAL / f"prospective-{month}.jsonl"
    known = existing_keys(out)
    rows = []
    for case, predicted_error in zip(future, predictions):
        key = (case["issued_at_utc"], case["model"], case["target_at_utc"])
        if key in known:
            continue
        raw_temperature = float(case["forecast"]["temperature_c"])
        deterministic_correction, correction_level, correction_n = correction_for(case, bias_tables)
        row = {
            "schema": 1,
            "shadow_only": True,
            "allowed_to_affect_public_forecast": False,
            "allowed_to_affect_alerts": False,
            "candidate": "observation_squared_v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_commit": os.environ.get("GITHUB_SHA"),
            "model": case["model"],
            "issued_at_utc": case["issued_at_utc"],
            "target_at_utc": case["target_at_utc"],
            "lead_h": case["lead_h"],
            "lead_bin": case["lead_bin"],
            "raw_temperature_c": round(raw_temperature, 3),
            "deterministic_correction_c": round(float(deterministic_correction), 3),
            "deterministic_temperature_c": round(raw_temperature - float(deterministic_correction), 3),
            "ml_predicted_error_c": round(float(predicted_error), 3),
            "ml_temperature_c": round(raw_temperature - float(predicted_error), 3),
            "issue_observation": {
                "station_id": context.get("station_id"),
                "station_name": context.get("station_name"),
                "observed_at_utc": context.get("observed_at_utc"),
                "age_minutes_at_issue": context.get("age_minutes_at_issue"),
                "temperature_c": context.get("temperature_c"),
                "relative_humidity_pct": context.get("relative_humidity_pct"),
                "temperature_change_1h_c": context.get("temperature_change_1h_c"),
                "temperature_change_3h_c": context.get("temperature_change_3h_c"),
                "temperature_change_6h_c": context.get("temperature_change_6h_c"),
            },
            "training": {
                "as_of_utc": case["issued_at_utc"],
                "cases": len(train),
                "features_used": features_used,
                "features_dropped": features_dropped,
                "deterministic_correction_level": correction_level,
                "deterministic_correction_training_n": correction_n,
                "history_context_cases": coverage.get("history_context_cases"),
                "live_context_cases": coverage.get("live_context_cases"),
            },
            "truth": None,
        }
        rows.append(row)

    if rows:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(
        json.dumps(
            {
                "ok": True,
                "candidate_pass": True,
                "snapshot_issued_at_utc": iso_utc(issued),
                "training_cases": len(train),
                "future_cases_selected": len(future),
                "new_predictions_written": len(rows),
                "output": str(out.relative_to(ROOT)),
                "issue_observation_age_min": context.get("age_minutes_at_issue"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
