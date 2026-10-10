#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone

from collect_dwd_sohland import (
    DATA,
    DISTANCE_KM,
    STATION_ID,
    STATION_NAME,
    ZIP_URL,
    get_bytes,
    parse_zip,
)

OUTPUT = DATA / "dwd-sohland-live.json"
MAX_POINTS = 144  # roughly one day of 10-minute observations


def main():
    now = datetime.now(timezone.utc)
    points = parse_zip(get_bytes(ZIP_URL))
    recent = points[-MAX_POINTS:]
    latest = recent[-1]
    latest_dt = datetime.fromisoformat(latest["observed_at_utc"].replace("Z", "+00:00"))
    age_min = (now - latest_dt).total_seconds() / 60

    payload = {
        "schema": 1,
        "provider": "DWD CDC 10-minute now",
        "station_id": STATION_ID,
        "station_name": STATION_NAME,
        "distance_to_nove_hrabeci_km": DISTANCE_KM,
        "source_url": ZIP_URL,
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "latest_observed_at_utc": latest["observed_at_utc"],
        "latest_age_minutes": round(age_min, 1),
        "points": recent,
    }

    DATA.mkdir(exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "latest_observed_at_utc": latest["observed_at_utc"],
                "latest_age_minutes": round(age_min, 1),
                "points": len(recent),
                "output": str(OUTPUT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
