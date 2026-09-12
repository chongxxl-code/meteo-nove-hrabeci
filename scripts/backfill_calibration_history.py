#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import math
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from calibration_truth import (
    DWD_DISTANCE_KM,
    DWD_STATION_ID,
    DWD_STATION_NAME,
    nearest_truth,
    truth_metadata,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "calibration"
OUT = OUT_DIR / "history-v0.jsonl"
STATUS = OUT_DIR / "backfill-status.json"

LAT = 51.0162
LON = 14.4398
UA = "nove-hrabeci-calibration/0.2 (+github-actions)"
PAST_DAYS = 92
DWD_RECENT = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    "climate/10_minutes/air_temperature/recent/"
    f"10minutenwerte_TU_{DWD_STATION_ID}_akt.zip"
)
DWD_NOW = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    "climate/10_minutes/air_temperature/now/"
    f"10minutenwerte_TU_{DWD_STATION_ID}_now.zip"
)
OPEN_METEO = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODELS = {
    "dwd": "dwd_icon_d2",
    "chmi": "chmi_aladin_cz_1km",
    "ec": "ecmwf_ifs",
}
LEADS = (1, 2)
CORE_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "weather_code",
    "pressure_msl",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
)


def get_bytes(url: str, tries: int = 4) -> bytes:
    err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read()
        except Exception as exc:
            err = exc
            if attempt + 1 < tries:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"GET failed {url}: {err}")


def get_json(url: str):
    return json.loads(get_bytes(url).decode("utf-8"))


def num(value):
    try:
        n = float(str(value).strip().replace(",", "."))
        return None if not math.isfinite(n) or n <= -999 else n
    except Exception:
        return None


def parse_dwd_time(value):
    s = str(value or "").strip()
    for fmt in ("%Y%m%d%H%M", "%Y%m%d%H"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def parse_dwd_zip(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [
            name for name in archive.namelist()
            if name.lower().endswith(".txt") and "produkt_" in name.lower()
        ]
        if not names:
            raise RuntimeError("DWD ZIP has no product TXT")
        text = archive.read(names[0]).decode("latin-1", errors="replace")

    out = {}
    for raw in csv.DictReader(io.StringIO(text), delimiter=";"):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        stamp = parse_dwd_time(row.get("MESS_DATUM"))
        temp = num(row.get("TT_10"))
        rh = num(row.get("RF_10"))
        if stamp is None or temp is None:
            continue
        out[stamp] = {
            "temperature_c": temp,
            "relative_humidity_pct": rh,
        }
    return out


def load_dwd_truth():
    merged = {}
    sources = []
    for url in (DWD_RECENT, DWD_NOW):
        try:
            points = parse_dwd_zip(get_bytes(url))
            merged.update(points)
            sources.append({"url": url, "points": len(points), "ok": True})
        except Exception as exc:
            sources.append({"url": url, "points": 0, "ok": False, "error": str(exc)[:240]})
    if not merged:
        raise RuntimeError("No DWD Sohland observations available")
    times = sorted(merged)
    return merged, times, sources


def parse_utc_hour(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_query(model_id: str):
    hourly = []
    for variable in CORE_VARIABLES:
        for day in LEADS:
            hourly.append(f"{variable}_previous_day{day}")
    params = {
        "latitude": LAT,
        "longitude": LON,
        "models": model_id,
        "past_days": PAST_DAYS,
        "forecast_days": 1,
        "timezone": "GMT",
        "wind_speed_unit": "kmh",
        "hourly": ",".join(hourly),
    }
    return OPEN_METEO + "?" + urllib.parse.urlencode(params)


def feature_value(hourly, variable, day, index):
    values = hourly.get(f"{variable}_previous_day{day}") or []
    if index >= len(values):
        return None
    return values[index]


def model_rows(model_key, model_id, truth, truth_times, now):
    url = build_query(model_id)
    payload = get_json(url)
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    rows = []

    for index, raw_time in enumerate(times):
        try:
            target = parse_utc_hour(raw_time)
        except Exception:
            continue
        if target > now:
            continue

        matched = nearest_truth(target, truth, truth_times)
        if not matched:
            continue
        observed_at, obs, match_delta = matched

        for day in LEADS:
            temp = feature_value(hourly, "temperature_2m", day, index)
            if temp is None:
                continue
            try:
                forecast_temp = float(temp)
            except Exception:
                continue

            forecast = {
                "temperature_c": forecast_temp,
                "relative_humidity_pct": feature_value(hourly, "relative_humidity_2m", day, index),
                "dew_point_c": feature_value(hourly, "dew_point_2m", day, index),
                "precipitation_mm": feature_value(hourly, "precipitation", day, index),
                "weather_code": feature_value(hourly, "weather_code", day, index),
                "pressure_msl_hpa": feature_value(hourly, "pressure_msl", day, index),
                "cloud_cover_pct": feature_value(hourly, "cloud_cover", day, index),
                "wind_speed_10m_kmh": feature_value(hourly, "wind_speed_10m", day, index),
                "wind_direction_10m_deg": feature_value(hourly, "wind_direction_10m", day, index),
            }
            issued = target - timedelta(days=day)
            row = {
                "schema": 1,
                "source": "open_meteo_previous_runs",
                "model": model_key,
                "model_id": model_id,
                "issued_at_utc": issued.isoformat().replace("+00:00", "Z"),
                "target_at_utc": target.isoformat().replace("+00:00", "Z"),
                "lead_h": day * 24,
                "forecast": forecast,
                "truth": truth_metadata(observed_at, obs, match_delta),
                "error_c": round(forecast_temp - float(obs["temperature_c"]), 3),
                "terrain_nh_ref": {
                    "elevation_m_bpv": 349.87,
                    "slope_deg": 4.4,
                    "aspect_deg": 224.5,
                    "tpi_300m_m": -6.17,
                    "tpi_900m_m": -12.29,
                },
            }
            rows.append(row)
    return rows, url


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    truth, truth_times, dwd_sources = load_dwd_truth()

    all_rows = []
    model_status = {}
    for model_key, model_id in MODELS.items():
        try:
            rows, url = model_rows(model_key, model_id, truth, truth_times, now)
            all_rows.extend(rows)
            model_status[model_key] = {
                "ok": True,
                "model_id": model_id,
                "rows": len(rows),
                "request_url": url,
            }
        except Exception as exc:
            model_status[model_key] = {
                "ok": False,
                "model_id": model_id,
                "rows": 0,
                "error": str(exc)[:500],
            }

    if not all_rows:
        raise RuntimeError("Backfill produced zero calibration rows")

    unique = {}
    for row in all_rows:
        key = (row["model"], row["target_at_utc"], row["lead_h"])
        unique[key] = row
    rows = sorted(unique.values(), key=lambda r: (r["target_at_utc"], r["model"], r["lead_h"]))

    with OUT.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    status = {
        "schema": 1,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "purpose": "shadow calibration only; never changes public forecast or alert ranks",
        "history_window_days_requested": PAST_DAYS,
        "truth_reference": {
            "station": DWD_STATION_NAME,
            "station_id": DWD_STATION_ID,
            "distance_to_nove_hrabeci_km": DWD_DISTANCE_KM,
            "is_nove_hrabeci_truth": False,
            "matching_policy": "canonical nearest UTC Sohland observation within ±20 minutes; no CHMI fallback",
            "warning": "This is a nearby proxy target until an on-site NH station is available.",
        },
        "dwd_sources": dwd_sources,
        "model_status": model_status,
        "rows": len(rows),
        "first_target_utc": rows[0]["target_at_utc"],
        "last_target_utc": rows[-1]["target_at_utc"],
        "output": "data/calibration/history-v0.jsonl",
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
