#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LAT = 51.0162
LON = 14.4398
TZ = "Europe/Prague"
MODEL = "google_weathernext2_ensemble"
SOURCE = "Open-Meteo Ensemble API / Google WeatherNext 2"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE_DIR = DATA / "weathernext2-shadow"
STATUS_PATH = DATA / "weathernext2-status.json"
LATEST_PATH = DATA / "weathernext2-latest.json"
META_URL = f"https://ensemble-api.open-meteo.com/data/{MODEL}/static/meta.json"
API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
UA = "nove-hrabeci-meteo-weathernext-shadow/1.0 (+github-actions)"
MAX_LEAD_HOURS = 72
EXPECTED_MEMBERS = 64


def get_json(url: str, timeout: int = 35):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def iso_utc_from_unix(value) -> str:
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")


def parse_api_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def percentile(values: list[float], q: float) -> float | None:
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def member_temperature_keys(hourly: dict) -> list[str]:
    keys = [
        key for key in hourly
        if re.fullmatch(r"temperature_2m(?:_member\d+)?", key)
        and isinstance(hourly.get(key), list)
    ]
    return sorted(keys, key=lambda key: (key != "temperature_2m", key))


def make_api_url() -> str:
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "temperature_2m",
        "models": MODEL,
        "past_days": 2,
        "forecast_days": 5,
        "timezone": "UTC",
        "temporal_resolution": "native",
    }
    return API_URL + "?" + urllib.parse.urlencode(params)


def build_run_record(meta: dict, payload: dict, collected_at: datetime | None = None) -> dict:
    collected_at = collected_at or datetime.now(timezone.utc)
    run_init = datetime.fromtimestamp(int(meta["last_run_initialisation_time"]), timezone.utc)
    available_at = datetime.fromtimestamp(int(meta["last_run_availability_time"]), timezone.utc)

    if int(meta.get("temporal_resolution_seconds", 0)) != 6 * 3600:
        raise RuntimeError(f"Unexpected WeatherNext native temporal resolution: {meta.get('temporal_resolution_seconds')}")
    if int(meta.get("update_interval_seconds", 0)) != 12 * 3600:
        raise RuntimeError(f"Unexpected WeatherNext update interval: {meta.get('update_interval_seconds')}")

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    member_keys = member_temperature_keys(hourly)
    if len(member_keys) != EXPECTED_MEMBERS:
        raise RuntimeError(f"Expected {EXPECTED_MEMBERS} WeatherNext members, got {len(member_keys)}")
    if any(len(hourly[key]) != len(times) for key in member_keys):
        raise RuntimeError("WeatherNext member arrays do not match the time axis")

    points = []
    for idx, raw_time in enumerate(times):
        valid_at = parse_api_utc(raw_time)
        lead_h = int(round((valid_at - run_init).total_seconds() / 3600))
        if lead_h < 6 or lead_h > MAX_LEAD_HOURS or lead_h % 6:
            continue
        members = [
            float(hourly[key][idx])
            for key in member_keys
            if hourly[key][idx] is not None and math.isfinite(float(hourly[key][idx]))
        ]
        if len(members) < EXPECTED_MEMBERS:
            raise RuntimeError(f"WeatherNext lead +{lead_h} h has only {len(members)}/{EXPECTED_MEMBERS} members")
        points.append({
            "valid_at_utc": valid_at.isoformat().replace("+00:00", "Z"),
            "model_lead_hours": lead_h,
            "available_before_valid": available_at <= valid_at,
            "operational_lead_hours_at_availability": round((valid_at - available_at).total_seconds() / 3600, 2),
            "temperature_2m_mean_c": round(statistics.fmean(members), 3),
            "temperature_2m_p10_c": round(percentile(members, 0.10), 3),
            "temperature_2m_p50_c": round(percentile(members, 0.50), 3),
            "temperature_2m_p90_c": round(percentile(members, 0.90), 3),
            "frost_probability": round(sum(v < 0 for v in members) / len(members), 4),
            "member_count": len(members),
        })

    expected_leads = list(range(6, MAX_LEAD_HOURS + 1, 6))
    got_leads = [point["model_lead_hours"] for point in points]
    missing = [lead for lead in expected_leads if lead not in got_leads]
    if missing:
        raise RuntimeError(f"Missing native WeatherNext lead times: {missing}")

    return {
        "schema": 1,
        "mode": "shadow_only",
        "model": MODEL,
        "provider": SOURCE,
        "license": "Open-Meteo free API: non-commercial use; weather data CC BY 4.0 attribution required",
        "requested_location": {"name": "Nové Hraběcí", "latitude": LAT, "longitude": LON, "timezone": TZ},
        "grid": {
            "latitude": payload.get("latitude"),
            "longitude": payload.get("longitude"),
            "elevation_m": payload.get("elevation"),
        },
        "run_initialization_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_available_utc": available_at.isoformat().replace("+00:00", "Z"),
        "collected_at_utc": collected_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "native_temporal_resolution_hours": 6,
        "update_interval_hours": 12,
        "ensemble_members": EXPECTED_MEMBERS,
        "forecast_points": points,
        "interpretation": {
            "production_use": False,
            "thermal_model_use": False,
            "calibration_use": False,
            "note": "Native 6-hour WeatherNext values only. Open-Meteo hourly interpolation is not used. Points published after their valid time are retained for retrospective model-skill diagnostics but must not be counted as operational forecasts.",
        },
    }


def existing_run_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            run_id = json.loads(line).get("run_initialization_utc")
            if run_id:
                out.add(str(run_id))
        except Exception:
            pass
    return out


def append_run_once(path: Path, record: dict) -> bool:
    run_id = record["run_initialization_utc"]
    if run_id in existing_run_ids(path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def main():
    DATA.mkdir(exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    status = {
        "schema": 1,
        "mode": "shadow_only",
        "model": MODEL,
        "provider": SOURCE,
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "ok": False,
    }

    try:
        meta = get_json(META_URL)
        payload = get_json(make_api_url())
        record = build_run_record(meta, payload, collected_at=now)
        run_month = record["run_initialization_utc"][:7]
        archive_path = ARCHIVE_DIR / f"{run_month}.jsonl"
        appended = append_run_once(archive_path, record)
        LATEST_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        status.update({
            "ok": True,
            "stage": "completed",
            "run_initialization_utc": record["run_initialization_utc"],
            "run_available_utc": record["run_available_utc"],
            "archive_file": str(archive_path.relative_to(ROOT)).replace("\\", "/"),
            "latest_file": str(LATEST_PATH.relative_to(ROOT)).replace("\\", "/"),
            "new_run_appended": appended,
            "native_temporal_resolution_hours": 6,
            "ensemble_members": EXPECTED_MEMBERS,
            "forecast_points": len(record["forecast_points"]),
            "operationally_available_points": sum(1 for p in record["forecast_points"] if p["available_before_valid"]),
            "grid": record["grid"],
            "source_url": make_api_url(),
            "metadata_url": META_URL,
            "license_note": "Free Open-Meteo API is non-commercial only; CC BY 4.0 attribution required.",
        })
    except Exception as exc:
        status.update({"stage": "error", "error": str(exc)})

    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))
    if not status["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
