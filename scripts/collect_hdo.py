#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASELINE = DATA / "hdo-manual-baseline.json"
OUTPUT = DATA / "hdo-schedule.json"
TZ = ZoneInfo("Europe/Prague")

API_ENDPOINTS = [
    "https://www.cez.cz/edee/content/sysutf/ds3/data/hdo_data.json",
    "https://www.cezdistribuce.cz/edee/content/sysutf/ds3/data/hdo_data.json",
]
NOTICE_URL = (
    "https://www.cezdistribuce.cz/pro-zakazniky/potrebuji-vyresit/"
    "stavajici-pripojeni/casy-spinani-nizkeho-tarifu"
)
WEEKDAY_KEYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]
WEEKDAY_CS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]
DAY_ALIASES = {
    "po": 0, "pondeli": 0,
    "ut": 1, "utery": 1,
    "st": 2, "streda": 2,
    "ct": 3, "ctvrtek": 3,
    "pa": 4, "patek": 4,
    "so": 5, "sobota": 5,
    "ne": 6, "nedele": 6,
}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def strip_accents(value: str) -> str:
    import unicodedata
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value or "")
        if not unicodedata.combining(ch)
    ).lower()


def minute_of_day(value: str) -> int:
    h, m = [int(x) for x in value.split(":")]
    return h * 60 + m


def hhmm(minutes: int) -> str:
    if minutes >= 1440:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_time(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    h, minute = int(m.group(1)), int(m.group(2))
    if h == 24 and minute == 0:
        return "24:00"
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        return None
    return f"{h:02d}:{minute:02d}"


def normalize_windows(windows):
    out = []
    for item in windows or []:
        if isinstance(item, dict):
            start, end = normalize_time(item.get("start")), normalize_time(item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end = normalize_time(item[0]), normalize_time(item[1])
        else:
            continue
        if start and end and minute_of_day(end) > minute_of_day(start):
            out.append([start, end])
    out.sort(key=lambda x: minute_of_day(x[0]))
    return out


def build_day(windows):
    nt = normalize_windows(windows)
    timeline = []
    cursor = 0
    for start, end in nt:
        a, b = minute_of_day(start), minute_of_day(end)
        if a > cursor:
            timeline.append({"tariff": "VT", "start": hhmm(cursor), "end": start, "minutes": a - cursor})
        timeline.append({"tariff": "NT", "start": start, "end": end, "minutes": b - a})
        cursor = b
    if cursor < 1440:
        timeline.append({"tariff": "VT", "start": hhmm(cursor), "end": "24:00", "minutes": 1440 - cursor})
    return {
        "nt_windows": [{"start": a, "end": b} for a, b in nt],
        "timeline": timeline,
        "nt_minutes": sum(x["minutes"] for x in timeline if x["tariff"] == "NT"),
        "vt_minutes": sum(x["minutes"] for x in timeline if x["tariff"] == "VT"),
    }


def parse_validity_days(value: str):
    text = strip_accents(value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    # Common CEZ forms: "Po - Pá", "So - Ne", individual weekday, comma lists.
    for sep in ("–", "—"):
        text = text.replace(sep, "-")
    parts = [p.strip() for p in re.split(r"[,;/]", text) if p.strip()]
    result = set()

    for part in parts:
        if "-" in part:
            a, b = [x.strip() for x in part.split("-", 1)]
            a = DAY_ALIASES.get(a)
            b = DAY_ALIASES.get(b)
            if a is None or b is None:
                continue
            i = a
            while True:
                result.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
                if len(result) > 7:
                    break
        else:
            idx = DAY_ALIASES.get(part)
            if idx is not None:
                result.add(idx)

    if not result:
        # Some payloads use longer Czech names embedded in text.
        for alias, idx in DAY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", text):
                result.add(idx)
    return sorted(result)


def row_windows(row):
    windows = []
    for i in range(1, 11):
        start = normalize_time(row.get(f"casZap{i}"))
        end = normalize_time(row.get(f"casVyp{i}"))
        if not start or not end:
            continue
        if end == "23:59":
            end = "24:00"
        if minute_of_day(end) > minute_of_day(start):
            windows.append([start, end])
    return windows


def fetch_json(url: str, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Meteo-Nove-Hrabeci/1.0 (+GitHub Actions; daily HDO check)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def fetch_text(url: str, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Meteo-Nove-Hrabeci/1.0 (+GitHub Actions; daily HDO check)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def live_days_from_rows(rows, rate):
    candidates = [
        row for row in rows
        if not rate or str(row.get("sazba") or "").strip().casefold() == rate.casefold()
    ]
    if not candidates:
        candidates = list(rows)

    per_day = {key: [] for key in WEEKDAY_KEYS}
    metadata = []
    for row in candidates:
        windows = row_windows(row)
        if not windows:
            continue
        day_indexes = parse_validity_days(str(row.get("platnost") or ""))
        if not day_indexes and len(candidates) == 1:
            day_indexes = list(range(7))
        if not day_indexes:
            continue
        for idx in day_indexes:
            per_day[WEEKDAY_KEYS[idx]] = windows
        metadata.append({
            "valid_from": row.get("validFrom"),
            "valid_to": row.get("validTo"),
            "command": row.get("kodPovelu"),
            "command_code": row.get("povel"),
            "rate": row.get("sazba"),
            "validity": row.get("platnost"),
            "hours": row.get("doba"),
            "description": row.get("description"),
            "date": row.get("date"),
        })

    complete = all(per_day[key] for key in WEEKDAY_KEYS)
    return per_day if complete else None, metadata


def try_live_schedule(command, region, rate):
    errors = []
    for endpoint in API_ENDPOINTS:
        query = urllib.parse.urlencode({"code": command, f"region{region}": "1"})
        url = f"{endpoint}?{query}"
        try:
            payload = fetch_json(url)
            if not isinstance(payload, list) or not payload:
                raise ValueError("empty or non-list response")
            days, metadata = live_days_from_rows(payload, rate)
            if not days:
                raise ValueError("response did not map to a complete 7-day schedule")
            return {
                "ok": True,
                "endpoint": endpoint,
                "url": url,
                "rows_received": len(payload),
                "days": days,
                "metadata": metadata,
                "error": None,
            }
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    return {
        "ok": False,
        "endpoint": API_ENDPOINTS[0],
        "url": None,
        "rows_received": 0,
        "days": None,
        "metadata": [],
        "error": " | ".join(errors),
    }


def extract_notices(page_text):
    # Server-rendered CEZ page contains current operational notices.
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", page_text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()

    sunday_override_dates = []
    for match in re.finditer(
        r"Dne\s+(\d{2}\.\d{2}\.\d{4}).{0,500}?ned[eě]ln[ií]ch\s+pl[aá]n",
        text,
        flags=re.I,
    ):
        raw = match.group(1)
        try:
            sunday_override_dates.append(datetime.strptime(raw, "%d.%m.%Y").date().isoformat())
        except ValueError:
            pass

    operational = []
    for match in re.finditer(
        r"(Od\s+\d{2}\.\d{2}\..{0,250}?operativn[ií]\s+zm[eě]n[^.]{0,180}\.)",
        text,
        flags=re.I,
    ):
        operational.append(match.group(1).strip())

    return {
        "sunday_override_dates": sorted(set(sunday_override_dates)),
        "operational_change_notices": operational[:5],
    }


def build_week(days, override_dates, start: date):
    week = []
    for offset in range(7):
        d = start + timedelta(days=offset)
        weekday_idx = d.weekday()
        schedule_key = "sunday" if d.isoformat() in override_dates else WEEKDAY_KEYS[weekday_idx]
        entry = dict(days[schedule_key])
        entry.update({
            "date": d.isoformat(),
            "weekday": WEEKDAY_KEYS[weekday_idx],
            "weekday_cs": WEEKDAY_CS[weekday_idx],
            "schedule_used": schedule_key,
            "special_sunday_plan": schedule_key == "sunday" and weekday_idx != 6,
        })
        week.append(entry)
    return week


def checksum_days(days):
    canonical = json.dumps(days, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def checksum_payload(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="Use the manual baseline without network calls.")
    args = parser.parse_args()

    baseline = load_json(BASELINE, {})
    if not baseline:
        raise SystemExit(f"Missing baseline: {BASELINE}")

    old = load_json(OUTPUT, {})
    region = str(baseline.get("distribution_area") or "Sever")
    rate = str(baseline.get("distribution_rate") or "D45d")
    command = str(baseline.get("primary_command") or "A1B6DP01")
    reference_command = str(baseline.get("legacy_reference_command") or command)

    baseline_windows = baseline.get("nt_windows_by_weekday") or {}
    fallback_days = {
        key: build_day(baseline_windows.get(key) or [])
        for key in WEEKDAY_KEYS
    }
    if not all(day["nt_windows"] for day in fallback_days.values()):
        raise SystemExit("Manual HDO baseline does not cover all weekdays.")

    now = datetime.now(TZ)
    live = {
        "ok": False,
        "endpoint": API_ENDPOINTS[0],
        "url": None,
        "rows_received": 0,
        "days": None,
        "metadata": [],
        "error": "offline mode",
    } if args.offline else try_live_schedule(reference_command, region, rate)

    notices = {
        "sunday_override_dates": [],
        "operational_change_notices": [],
        "status": "not_checked_offline" if args.offline else "unavailable",
        "error": None,
    }
    if not args.offline:
        try:
            parsed = extract_notices(fetch_text(NOTICE_URL))
            notices.update(parsed)
            notices["status"] = "ok"
        except Exception as exc:
            notices["error"] = f"{type(exc).__name__}: {exc}"

    # The current CEZ DIP export supplied by the user is authoritative for the
    # actual command timing. The older public command endpoint is useful as a
    # zero-cost change monitor, but its metadata can lag behind the current DIP
    # schedule (for example, it can still expose an older long-lived program).
    # Therefore it must never silently overwrite the newer export.
    days = fallback_days
    schedule_source = "cez_dip_export_monitored_daily"
    status = "verified_export_monitored"

    reference_days = (
        {key: build_day(live["days"][key]) for key in WEEKDAY_KEYS}
        if live["ok"] else None
    )
    reference_digest = checksum_days(reference_days) if reference_days else None
    baseline_digest = checksum_days(fallback_days)
    reference_differs = bool(reference_digest and reference_digest != baseline_digest)

    previous_monitor = old.get("daily_monitor") if isinstance(old.get("daily_monitor"), dict) else {}
    previous_reference_digest = previous_monitor.get("reference_schedule_checksum")
    reference_changed = bool(
        reference_digest
        and previous_reference_digest
        and reference_digest != previous_reference_digest
    )

    notice_basis = {
        "sunday_override_dates": notices.get("sunday_override_dates") or [],
        "operational_change_notices": notices.get("operational_change_notices") or [],
    }
    notices_digest = checksum_payload(notice_basis)
    previous_notices_digest = previous_monitor.get("public_notices_checksum")
    notices_changed = bool(
        previous_notices_digest
        and notices_digest != previous_notices_digest
    )

    override_dates = notices.get("sunday_override_dates") or []
    week = build_week(days, override_dates, now.date())
    digest = checksum_days(days)
    previous_digest = old.get("schedule_checksum")

    output = {
        "schema": "2026-09-29.hdo-schedule-v2",
        "generated_at": now.isoformat(),
        "checked_at": None if args.offline else now.isoformat(),
        "status": status,
        "schedule_source": schedule_source,
        "authoritative_export_date": ((baseline.get("source") or {}).get("supplied_date")),
        "schedule_scope": "Customer-specific CEZ export pattern projected by weekday; date-specific public CEZ notices are applied when detected.",
        "distribution_area": region,
        "distribution_rate": rate,
        "primary_command": command,
        "equivalent_tariff_commands": baseline.get("equivalent_tariff_commands") or [],
        "api_cost_czk": 0,
        "paid_provider_calls": 0,
        "daily_public_http_requests": 0 if args.offline else 2,
        "automatic_check_frequency": "daily",
        "days": days,
        "week_ahead": week,
        "special_schedule": notices,
        "daily_monitor": {
            "status": "ok" if live["ok"] else "partial",
            "reference_endpoint": live.get("endpoint"),
            "reference_rows_received": live.get("rows_received"),
            "reference_metadata": live.get("metadata"),
            "reference_last_error": live.get("error"),
            "reference_schedule_checksum": reference_digest,
            "previous_reference_schedule_checksum": previous_reference_digest,
            "reference_changed_since_previous_check": reference_changed,
            "baseline_schedule_checksum": baseline_digest,
            "reference_differs_from_current_export": reference_differs,
            "public_notices_checksum": notices_digest,
            "previous_public_notices_checksum": previous_notices_digest,
            "public_notices_changed_since_previous_check": notices_changed,
            "exact_customer_schedule_auto_refresh": False,
            "exact_customer_schedule_auto_refresh_blocker": "ČEZ per-customer DIP endpoint requires CAPTCHA; no third-party OCR provider is used.",
            "note": "The older public command endpoint and CEZ public notices are monitored for changes. The customer-specific CEZ DIP export remains authoritative; exact per-customer refresh is not automated because the current endpoint is CAPTCHA-protected."
        },
        "schedule_checksum": digest,
        "schedule_changed_since_previous_check": bool(previous_digest and previous_digest != digest),
        "fallback": {
            "available": True,
            "source": "data/hdo-manual-baseline.json",
            "note": "The current CEZ DIP export is kept locally and combined with daily public CEZ change notices. No paid provider is required.",
        },
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"HDO status={status} source={schedule_source} "
        f"checksum={digest[:12]} monitor_diff={reference_differs} "
        f"week={week[0]['date']}..{week[-1]['date']}"
    )
    if live.get("error"):
        print(f"Live API note: {live['error']}")


if __name__ == "__main__":
    main()
