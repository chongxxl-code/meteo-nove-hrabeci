#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "data" / "alerts.json"
NOWCAST = ROOT / "data" / "radar-nowcast.json"
MAX_AGE_MIN = 25.0


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def dt_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def finite(v):
    try:
        return float(v) == float(v)
    except Exception:
        return False


def max_model(models, key):
    vals = [float(m[key]) for m in models if m.get("ok") and finite(m.get(key))]
    return max(vals) if vals else None


def find_alert(alerts, alert_id):
    for a in alerts:
        if a.get("id") == alert_id:
            return a
    return None


def bump(alert, rank, reason, confidence="high"):
    if not alert:
        return
    old = int(alert.get("rank") or 0)
    if rank > old:
        alert["rank"] = rank
        alert["level"] = ["green", "yellow", "orange", "red"][rank]
        headlines = {
            "storm": ["bez lokálního signálu", "zvýšená pozornost", "výrazné riziko", "bezprostřední bouřková aktivita"],
            "runoff": ["bez zvýšeného rizika", "sledovat", "zvýšené riziko", "vysoké lokální riziko"],
            "wind": ["bez zvýšeného rizika", "zesílený vítr", "silný vítr", "velmi silný vítr"],
        }
        if alert.get("id") in headlines:
            alert["headline"] = headlines[alert["id"]][rank]
    reasons = alert.setdefault("reasons", [])
    if reason not in reasons:
        reasons.append(reason)
    if rank >= old:
        alert["confidence"] = confidence


def main():
    alerts_doc = load(ALERTS)
    nowcast = load(NOWCAST)
    if not alerts_doc or not nowcast:
        print(json.dumps({"ok": True, "changed": False, "reason": "missing_input"}))
        return

    computed = dt_utc(nowcast.get("computed_at_utc"))
    age_min = (datetime.now(timezone.utc) - computed).total_seconds() / 60.0 if computed else 9999
    if nowcast.get("status") != "operational" or age_min < -2 or age_min > MAX_AGE_MIN:
        alerts_doc.setdefault("sources", {})["radar_nowcast"] = {
            "ok": False,
            "status": "stale_or_unavailable",
            "age_min": round(age_min, 1) if age_min < 9999 else None,
        }
        ALERTS.write_text(json.dumps(alerts_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "changed": True, "reason": "nowcast_stale", "age_min": round(age_min, 1)}))
        return

    nearest = float(nowcast["nearest_precip_km"]) if finite(nowcast.get("nearest_precip_km")) else None
    eta = float(nowcast["eta_min"]) if finite(nowcast.get("eta_min")) else None
    relation = nowcast.get("relation")
    confidence = nowcast.get("confidence") or "medium"
    approaching = relation in {"miri_na_lokalitu", "nad_nebo_u_lokality"}

    models = ((alerts_doc.get("sources") or {}).get("models") or [])
    cape = max_model(models, "max_cape_12h")
    gust = max_model(models, "max_gust_12h")
    legacy_radar = ((alerts_doc.get("sources") or {}).get("radar") or {})
    latest = legacy_radar.get("latest") or {}
    dbz = latest.get("max_dbz") or {}
    r10 = float(dbz["10km"]) if finite(dbz.get("10km")) else None
    r25 = float(dbz["25km"]) if finite(dbz.get("25km")) else None
    r50 = float(dbz["50km"]) if finite(dbz.get("50km")) else None

    src = {
        "ok": True,
        "computed_at_utc": nowcast.get("computed_at_utc"),
        "age_min": round(age_min, 1),
        "relation": relation,
        "confidence": confidence,
        "nearest_precip_km": nearest,
        "eta_min": eta,
        "motion": nowcast.get("motion"),
    }
    alerts_doc.setdefault("sources", {})["radar_nowcast"] = src

    storm = find_alert(alerts_doc.get("alerts") or [], "storm")
    runoff = find_alert(alerts_doc.get("alerts") or [], "runoff")
    wind = find_alert(alerts_doc.get("alerts") or [], "wind")

    close_60 = approaching and ((eta is not None and eta <= 60) or (nearest is not None and nearest <= 50))
    close_30 = approaching and ((eta is not None and eta <= 30) or (nearest is not None and nearest <= 25))
    close_15 = approaching and ((eta is not None and eta <= 15) or (nearest is not None and nearest <= 10))

    if close_60 and ((cape is not None and cape >= 400) or (r50 is not None and r50 >= 40)):
        bump(storm, 1, f"radarový nowcast: srážkový systém míří k lokalitě, vzdálenost {nearest:.1f} km" if nearest is not None else "radarový nowcast: srážkový systém míří k lokalitě")

    if close_30 and ((cape is not None and cape >= 500) or (r25 is not None and r25 >= 40)):
        detail = f"radarový nowcast: aktivní systém do {nearest:.1f} km" if nearest is not None else "radarový nowcast: aktivní systém do 30 min"
        if eta is not None:
            detail += f", ETA ~{eta:.0f} min"
        bump(storm, 2, detail)

    # Red remains deliberately conservative: it requires a very strong nearby echo
    # plus substantial convective instability or dangerous modelled gusts.
    very_strong_echo = (r10 is not None and r10 >= 50) or (r25 is not None and r25 >= 55)
    severe_environment = (cape is not None and cape >= 1000) or (gust is not None and gust >= 75)
    if close_15 and very_strong_echo and severe_environment:
        bump(storm, 3, "velmi silné blízké radarové echo + výrazné konvektivní prostředí")

    if close_30 and ((r25 is not None and r25 >= 45) or (r10 is not None and r10 >= 45)):
        bump(runoff, 2, "silné srážkové echo se podle nowcastu blíží k lokalitě")
    if close_15 and (r10 is not None and r10 >= 50):
        bump(runoff, 3, "velmi silné srážkové echo je bezprostředně u lokality")

    if close_30 and gust is not None and gust >= 60 and ((r25 is not None and r25 >= 40) or (cape is not None and cape >= 500)):
        bump(wind, 2, f"konvektivní systém se blíží; modelové nárazy až ~{gust:.0f} km/h")
    if close_15 and gust is not None and gust >= 75 and very_strong_echo:
        bump(wind, 3, f"bezprostřední konvektivní systém + modelové nárazy ~{gust:.0f} km/h")

    # Recompute fingerprint so the parent workflow sees a meaningful state change.
    import hashlib
    normalized = json.dumps({
        "alerts": alerts_doc.get("alerts"),
        "official": ((alerts_doc.get("sources") or {}).get("official_warnings") or {}).get("active_for_point"),
        "radar_nowcast": src,
    }, sort_keys=True, ensure_ascii=False)
    alerts_doc["_fingerprint"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    ALERTS.write_text(json.dumps(alerts_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "changed": True,
        "age_min": round(age_min, 1),
        "relation": relation,
        "nearest_precip_km": nearest,
        "storm_rank": (storm or {}).get("rank"),
        "runoff_rank": (runoff or {}).get("rank"),
        "wind_rank": (wind or {}).get("rank"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
