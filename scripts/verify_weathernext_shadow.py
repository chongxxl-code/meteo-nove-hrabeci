#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE_DIR = DATA / "weathernext2-shadow"
OBS_DIR = DATA / "observations"
OUT = DATA / "weathernext2-verification.json"
TZ = ZoneInfo("Europe/Prague")
TARGET_LEADS = (6, 12, 24, 48, 72)
SOHLAND_MATCH_MIN = 45
CHMI_MATCH_MIN = 60
MIN_CASES_FOR_CONCLUSION = 20


def dt(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_jsonl(pattern: str) -> list[dict]:
    rows = []
    for path in sorted(ROOT.glob(pattern)):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def load_temperature_observations(pattern: str):
    rows = []
    seen = set()
    for path in sorted(OBS_DIR.glob(pattern)):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("temperature_c") is None:
                    continue
                observed_at = dt(item.get("observed_at_utc"))
                key = (item.get("station_id") or item.get("station_wsi"), item.get("observed_at_utc"))
                if observed_at and key not in seen:
                    seen.add(key)
                    rows.append((observed_at, float(item["temperature_c"]), item))
            except Exception:
                pass
    rows.sort(key=lambda row: row[0])
    return rows


def nearest_observation(target, rows, tolerance_min):
    best = None
    for item in rows:
        delta = abs((item[0] - target).total_seconds()) / 60
        if delta <= tolerance_min and (best is None or delta < best[0]):
            best = (delta, item)
    return best[1] if best else None


def summarize_temperature(cases: list[dict]) -> dict:
    if not cases:
        return {"n": 0, "enough_data": False, "mae_c": None, "bias_c": None}
    errors = [float(case["error_c"]) for case in cases]
    return {
        "n": len(errors),
        "enough_data": len(errors) >= MIN_CASES_FOR_CONCLUSION,
        "mae_c": round(sum(abs(v) for v in errors) / len(errors), 3),
        "bias_c": round(sum(errors) / len(errors), 3),
    }


def summarize_frost(cases: list[dict]) -> dict:
    usable = [case for case in cases if case.get("frost_probability") is not None]
    if not usable:
        return {
            "n": 0,
            "enough_data": False,
            "observed_frost_events": 0,
            "brier_score": None,
            "accuracy_at_50pct": None,
        }
    brier = []
    correct = 0
    frost_events = 0
    for case in usable:
        p = float(case["frost_probability"])
        observed = 1.0 if float(case["observed_c"]) < 0 else 0.0
        frost_events += int(observed)
        brier.append((p - observed) ** 2)
        correct += int((p >= 0.5) == bool(observed))
    return {
        "n": len(usable),
        "enough_data": len(usable) >= MIN_CASES_FOR_CONCLUSION,
        "observed_frost_events": frost_events,
        "brier_score": round(sum(brier) / len(brier), 4),
        "accuracy_at_50pct": round(correct / len(usable), 3),
    }


def build_verification(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    runs = load_jsonl("data/weathernext2-shadow/*.jsonl")
    sohland = load_temperature_observations("dwd-sohland-*.jsonl")
    chmi = load_temperature_observations("chmi-weather-*.jsonl")
    cases = []

    for run in runs:
        run_init = dt(run.get("run_initialization_utc"))
        run_available = dt(run.get("run_available_utc"))
        if not run_init or not run_available:
            continue
        for point in run.get("forecast_points") or []:
            target = dt(point.get("valid_at_utc"))
            if not target or target > now:
                continue
            lead = int(point.get("model_lead_hours", -1))
            if lead not in TARGET_LEADS:
                continue

            obs = nearest_observation(target, sohland, SOHLAND_MATCH_MIN)
            source = "dwd_sohland_10min"
            if not obs:
                obs = nearest_observation(target, chmi, CHMI_MATCH_MIN)
                source = "chmi_nearest_temperature_station"
            if not obs:
                continue

            forecast = float(point["temperature_2m_mean_c"])
            observed = float(obs[1])
            local_target = target.astimezone(TZ)
            operational = bool(point.get("available_before_valid")) and run_available <= target
            cases.append({
                "model": "google_weathernext2_ensemble_mean_shadow",
                "model_run_initialization_utc": run_init.isoformat().replace("+00:00", "Z"),
                "model_run_available_utc": run_available.isoformat().replace("+00:00", "Z"),
                "target_at_utc": target.isoformat().replace("+00:00", "Z"),
                "model_lead_h": lead,
                "operational_lead_h_at_availability": round((target - run_available).total_seconds() / 3600, 2),
                "operationally_scoreable": operational,
                "forecast_c": forecast,
                "observed_c": observed,
                "error_c": round(forecast - observed, 3),
                "frost_probability": point.get("frost_probability"),
                "night_native_sample": local_target.hour >= 18 or local_target.hour < 8,
                "verification_source": source,
                "station": obs[2].get("station_name"),
                "station_distance_km": obs[2].get("distance_to_nove_hrabeci_km", obs[2].get("distance_km")),
            })

    lead_summary = {}
    for lead in TARGET_LEADS:
        all_cases = [case for case in cases if case["model_lead_h"] == lead]
        operational_cases = [case for case in all_cases if case["operationally_scoreable"]]
        night_cases = [case for case in operational_cases if case["night_native_sample"]]
        lead_summary[str(lead)] = {
            "retrospective_model_lead": summarize_temperature(all_cases),
            "operational": summarize_temperature(operational_cases),
            "operational_night_native_samples": summarize_temperature(night_cases),
            "frost_probability_operational": summarize_frost(operational_cases),
            "note": (
                "Operational metrics include only forecasts already available before their valid time. "
                "Retrospective model-lead metrics are kept separate and must not be presented as operational skill."
            ),
        }

    operational_all = [case for case in cases if case["operationally_scoreable"]]
    night_all = [case for case in operational_all if case["night_native_sample"]]
    return {
        "schema": 1,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "status": "collecting" if len(operational_all) < MIN_CASES_FOR_CONCLUSION else "evaluation_ready",
        "mode": "shadow_only",
        "model": "google_weathernext2_ensemble",
        "method": {
            "forecast_value": "mean of 64 native WeatherNext 2 ensemble members computed by the collector",
            "native_temporal_resolution_hours": 6,
            "target_model_leads_hours": list(TARGET_LEADS),
            "temperature_truth_priority": "DWD Sohland/Spree 10-minute temperature, then nearest ČHMÚ temperature station",
            "operational_gate": "run_available_utc must be <= valid_at_utc",
            "minimum_cases_before_quality_conclusion": MIN_CASES_FOR_CONCLUSION,
            "night_definition_local": "18:00-07:59 Europe/Prague; native 6-hour samples only",
            "warning": "Native WeatherNext 2 does not resolve the true continuous nightly minimum. Do not treat native-night samples as an interpolated hourly minimum.",
        },
        "coverage": {
            "forecast_runs": len(runs),
            "matched_cases": len(cases),
            "operational_cases": len(operational_all),
            "retrospective_only_cases": len(cases) - len(operational_all),
            "operational_night_native_cases": len(night_all),
            "sohland_temperature_observations": len(sohland),
            "chmi_temperature_observations": len(chmi),
        },
        "overall_operational_temperature": summarize_temperature(operational_all),
        "overall_operational_night_native": summarize_temperature(night_all),
        "overall_operational_frost_probability": summarize_frost(operational_all),
        "lead_hours": lead_summary,
        "recent_cases": sorted(cases, key=lambda case: (case["target_at_utc"], case["model_run_initialization_utc"]), reverse=True)[:80],
        "production_effect": "none",
    }


def main():
    output = build_verification()
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output["coverage"], ensure_ascii=False))


if __name__ == "__main__":
    main()
