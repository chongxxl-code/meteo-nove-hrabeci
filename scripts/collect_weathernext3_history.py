#!/usr/bin/env python3
"""Read WeatherNext 3 historical forecast values for Nové Hraběcí.

PUBLIC REPOSITORY LICENSING BOUNDARY:
Only predictions whose forecast VALID time is at least one hour in the past
may be written to the public repository. Never persist live/future predictions
here; those fall under GDM Real-Time Experimental Data terms, not CC BY 4.0.
The existing production forecast and heating model are unaffected.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE_DIR = DATA / "weathernext3-history"
STATUS_PATH = DATA / "weathernext3-status.json"
PROJECT = "weathernext-hrabeci"
COLLECTION_ID = "projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p05deg"
LATITUDE = 51.016900
LONGITUDE = 14.424857
LEADS = (6, 12, 18)
BANDS = (
    "station_head_temperature_2m_mean",
    "station_head_temperature_2m_p10",
    "station_head_temperature_2m_p90",
    "station_head_dewpoint_temperature_2m_mean",
)
ATTRIBUTION = (
    "© 2026 DeepMind Technologies Limited's machine learning models used to "
    "create experimental data at "
    "https://developers.google.com/earth-engine/datasets/catalog/"
    "projects_gcp-public-data-weathernext_assets_weathernext_3_0_0_0p05deg "
    "under CC BY 4.0. Experimental data, not intended, validated or approved "
    "for real-world safety-critical use."
)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def choose_run(now: datetime) -> datetime:
    """Pick a six-hourly forecast run at least 24h old (all chosen leads matured)."""
    now = now.astimezone(timezone.utc)
    past = now - timedelta(hours=24)
    return past.replace(hour=(past.hour // 6) * 6, minute=0, second=0, microsecond=0)


def historical_valid_times(run_init: datetime, now: datetime) -> list[tuple[int, datetime]]:
    """Fail closed if *any* selected prediction is not old enough for CC BY 4.0."""
    now = now.astimezone(timezone.utc)
    result = []
    for lead in LEADS:
        valid_time = run_init + timedelta(hours=lead)
        if valid_time > now - timedelta(hours=1):
            raise ValueError("Refusing to publish real-time WeatherNext 3 data")
        result.append((lead, valid_time))
    return result


def read_earth_engine(run_init: datetime, lead: int) -> dict:
    # Import only for a live authenticated collection; unit tests need no credentials.
    import ee
    collection = ee.ImageCollection(COLLECTION_ID).filter(
        ee.Filter.eq("start_time", utc_iso(run_init))
    ).filter(ee.Filter.eq("forecast_hour", lead))
    image = ee.Image(collection.first())
    return image.select(list(BANDS)).reduceRegion(
        reducer=ee.Reducer.first(),
        geometry=ee.Geometry.Point([LONGITUDE, LATITUDE]),
        scale=5000,
        maxPixels=100,
    ).getInfo() or {}


def normalise_point(raw: dict, lead: int, valid_time: datetime) -> dict:
    point = {"lead_hours": lead, "valid_at_utc": utc_iso(valid_time)}
    for band in BANDS:
        value = raw.get(band)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"WeatherNext 3 missing/invalid band: {band}")
        point[band.replace("station_head_", "") + "_c"] = round(float(value) - 273.15, 3)
    lo = point["temperature_2m_p10_c"]
    md = point["temperature_2m_mean_c"]
    hi = point["temperature_2m_p90_c"]
    if not lo <= hi or not (lo - 20 <= md <= hi + 20):
        raise ValueError("Invalid WeatherNext 3 temperature statistics")
    return point


def build_record(now: datetime, read_fn=read_earth_engine) -> dict:
    run = choose_run(now)
    pairs = historical_valid_times(run, now)
    points = [normalise_point(read_fn(run, lead), lead, valid) for lead, valid in pairs]
    return {
        "schema": 1,
        "mode": "historical_shadow_only",
        "model": "google_weathernext3_0p05deg",
        "dataset": COLLECTION_ID,
        "location": {"name": "Nové Hraběcí", "latitude": LATITUDE, "longitude": LONGITUDE},
        "run_initialization_utc": utc_iso(run),
        "collected_at_utc": utc_iso(now),
        "forecast_points": points,
        "licence": "CC BY 4.0, historical values only (valid at least 1 hour ago)",
        "attribution": ATTRIBUTION,
        "production_use": False,
        "heating_control_use": False,
    }


def append_once(path: Path, record: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            existing = json.loads(line)
            if existing.get("run_initialization_utc") == record["run_initialization_utc"]:
                return False
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def main() -> None:
    import ee
    import google.auth

    now = datetime.now(timezone.utc)
    # Authentication happens in GitHub Actions using Google's Workload Identity.
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    ee.Initialize(credentials=credentials, project=PROJECT)
    record = build_record(now)
    run_month = record["run_initialization_utc"][:7]
    archive_path = ARCHIVE_DIR / f"{run_month}.jsonl"
    appended = append_once(archive_path, record)
    DATA.mkdir(parents=True, exist_ok=True)
    status = {
        "schema": 1,
        "ok": True,
        "mode": "historical_shadow_only",
        "model": "google_weathernext3_0p05deg",
        "checked_at_utc": utc_iso(now),
        "run_initialization_utc": record["run_initialization_utc"],
        "archived_points": len(record["forecast_points"]),
        "new_run_appended": appended,
        "archive_file": str(archive_path.relative_to(ROOT)),
        "real_time_forecasts_published": False,
        "production_use": False,
        "heating_control_use": False,
        "licence": "CC BY 4.0, historical values only",
        "attribution": ATTRIBUTION,
    }
    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("WeatherNext 3 historical shadow OK:", utc_iso(now),
          "run:", record["run_initialization_utc"],
          "matured_steps:", len(record["forecast_points"]),
          "new_archive:", appended)


if __name__ == "__main__":
    main()
