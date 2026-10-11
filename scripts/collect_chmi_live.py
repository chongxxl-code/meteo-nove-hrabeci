#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from collect_chmi import (
    DATA,
    PREFERRED_RAIN_NAME,
    PREFERRED_RAIN_WSI,
    TZ,
    choose_station,
    fetch_metadata,
    metadata_elements,
    metadata_stations,
)

OUTPUT = DATA / "chmi-live.json"


def main():
    DATA.mkdir(exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)

    meta1, _ = fetch_metadata("meta1", now_utc)
    meta2, _ = fetch_metadata("meta2", now_utc)
    stations = metadata_stations(meta1, now_utc)
    elements = metadata_elements(meta2)

    station, points, available, source_url, attempts = choose_station(
        stations,
        elements,
        ["SRA10M"],
        now_utc,
        preferred_wsi=PREFERRED_RAIN_WSI,
    )
    if not station or not points:
        raise RuntimeError(
            "No nearby CHMI rain station: "
            + json.dumps(attempts[:6], ensure_ascii=False)
        )

    latest = points[-1]
    latest_dt = datetime.fromisoformat(
        latest["observed_at_utc"].replace("Z", "+00:00")
    )
    recent = [
        p
        for p in points
        if datetime.fromisoformat(
            p["observed_at_utc"].replace("Z", "+00:00")
        )
        >= latest_dt - timedelta(minutes=50)
    ]
    rain60 = round(
        sum(float(p.get("precipitation_10m_mm") or 0) for p in recent), 3
    )

    payload = {
        "schema": 1,
        "ok": True,
        "provider": "CHMI OpenData",
        "preferred_station": PREFERRED_RAIN_NAME,
        "preferred_station_available": station["wsi"] == PREFERRED_RAIN_WSI,
        "station_name": station["name"],
        "station_wsi": station["wsi"],
        "latitude": station["lat"],
        "longitude": station["lon"],
        "elevation_m": station["elevation_m"],
        "distance_to_nove_hrabeci_km": station["distance_km"],
        "source_url": source_url,
        "checked_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "checked_at_local": now_local.isoformat(),
        "observed_at_utc": latest["observed_at_utc"],
        "observed_at_local": latest_dt.astimezone(TZ).isoformat(),
        "age_minutes": round((now_utc - latest_dt).total_seconds() / 60, 1),
        "available_elements": available,
        "precipitation_10m_mm": latest.get("precipitation_10m_mm"),
        "precipitation_last_60m_mm": rain60,
    }

    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
