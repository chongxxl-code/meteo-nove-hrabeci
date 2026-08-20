#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OBS = DATA / 'observations'
WSI = '0-203-0-11501049001'
STATION_ID = 'U2SLUK01'
STATION_NAME = 'Šluknov'
STATION_LAT = 51.002722
STATION_LON = 14.45525
STATION_ELEV = 352.0
DISTANCE_KM = 1.85
BASE = 'https://opendata.chmi.cz/meteorology/climate/now/data/10min'
UA = 'nove-hrabeci-meteo/0.8 (+github-actions)'


def get_json(url: str, tries: int = 3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as exc:
            err = exc
            if i + 1 < tries:
                time.sleep(2 + i * 2)
    raise RuntimeError(f'ČHMÚ OpenData fetch failed: {err}')


def source_urls(now_utc: datetime):
    # Around local midnight the current UTC data file can still be the previous date.
    days = [now_utc.date(), (now_utc - timedelta(days=1)).date()]
    return [f'{BASE}/10m-{WSI}-{d:%Y%m%d}.json' for d in days]


def find_table(obj):
    """Find CHMI's nested {header, values} data table without hard-coding wrapper keys."""
    if isinstance(obj, dict):
        if isinstance(obj.get('header'), str) and isinstance(obj.get('values'), list):
            return obj['header'], obj['values']
        for v in obj.values():
            found = find_table(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_table(v)
            if found:
                return found
    return None


def parse_dt(value):
    if not isinstance(value, str):
        return None
    s = value.strip()
    # ISO timestamps used by CHMI; accept Z and explicit offset.
    if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def as_float(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(',', '.'))
        except Exception:
            return None
    return None


def extract_sra10m(payload):
    table = find_table(payload)
    if not table:
        raise RuntimeError('ČHMÚ JSON table {header, values} not found')
    header, values = table
    cols = [c.strip() for c in header.split(',')]
    rows = []
    for raw in values:
        if not isinstance(raw, list):
            continue
        padded = raw + [None] * max(0, len(cols) - len(raw))
        row = dict(zip(cols, padded))
        if not any(str(v).strip().upper() == 'SRA10M' for v in raw if v is not None):
            continue

        observed = None
        for v in raw:
            observed = parse_dt(v)
            if observed:
                break
        if not observed:
            continue

        # Prefer an explicitly named value column. Keep broad fallbacks for schema revisions.
        value = None
        preferred = []
        for c in cols:
            key = re.sub(r'[^A-Z0-9]', '', c.upper())
            if key in ('VALUE', 'HODNOTA', 'VAL') or 'VALUE' in key:
                preferred.append(c)
        for c in preferred:
            value = as_float(row.get(c))
            if value is not None:
                break

        if value is None:
            # Fallback: choose a numeric field that is not a flag/time/station identifier.
            for c in cols:
                key = c.upper()
                if any(x in key for x in ('FLAG', 'QUALITY', 'WSI', 'DATE', 'TIME', 'INTERVAL')):
                    continue
                v = row.get(c)
                if isinstance(v, str) and v.strip().upper() == 'SRA10M':
                    continue
                n = as_float(v)
                if n is not None:
                    value = n
                    break

        if value is not None:
            rows.append({'observed_at_utc': observed.isoformat().replace('+00:00', 'Z'), 'precipitation_10m_mm': value})

    if not rows:
        raise RuntimeError(f'No SRA10M rows parsed; header={header!r}; values_count={len(values)}')

    # Deduplicate identical timestamps and sort chronologically.
    unique = {r['observed_at_utc']: r for r in rows}
    return [unique[k] for k in sorted(unique)]


def existing_times(archive: Path):
    out = set()
    if not archive.exists():
        return out
    for line in archive.read_text(encoding='utf-8').splitlines():
        try:
            obj = json.loads(line)
            if obj.get('observed_at_utc'):
                out.add(obj['observed_at_utc'])
        except Exception:
            pass
    return out


def main():
    DATA.mkdir(exist_ok=True)
    OBS.mkdir(exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)

    payload = None
    used_url = None
    errors = []
    for url in source_urls(now_utc):
        try:
            payload = get_json(url)
            used_url = url
            break
        except Exception as exc:
            errors.append(f'{url}: {exc}')
    if payload is None:
        raise RuntimeError('; '.join(errors))

    points = extract_sra10m(payload)
    latest = points[-1]
    latest_dt = datetime.fromisoformat(latest['observed_at_utc'].replace('Z', '+00:00'))

    archive = OBS / f'chmi-sluknov-{now_local:%Y-%m}.jsonl'
    known = existing_times(archive)
    new_points = [p for p in points if p['observed_at_utc'] not in known]
    if new_points:
        with archive.open('a', encoding='utf-8') as f:
            for p in new_points:
                rec = {
                    'provider': 'ČHMÚ OpenData',
                    'station_name': STATION_NAME,
                    'station_id': STATION_ID,
                    'station_wsi': WSI,
                    'latitude': STATION_LAT,
                    'longitude': STATION_LON,
                    'elevation_m': STATION_ELEV,
                    'distance_to_nove_hrabeci_km': DISTANCE_KM,
                    **p,
                }
                f.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')

    recent = [p for p in points if datetime.fromisoformat(p['observed_at_utc'].replace('Z', '+00:00')) >= latest_dt - timedelta(minutes=50)]
    rain_60 = round(sum(float(p['precipitation_10m_mm']) for p in recent), 3)

    status = {
        'ok': True,
        'provider': 'ČHMÚ OpenData',
        'station_name': STATION_NAME,
        'station_id': STATION_ID,
        'station_wsi': WSI,
        'latitude': STATION_LAT,
        'longitude': STATION_LON,
        'elevation_m': STATION_ELEV,
        'distance_to_nove_hrabeci_km': DISTANCE_KM,
        'source_url': used_url,
        'checked_at_local': now_local.isoformat(),
        'observed_at_utc': latest['observed_at_utc'],
        'observed_at_local': latest_dt.astimezone(TZ).isoformat(),
        'age_minutes': round((now_utc - latest_dt).total_seconds() / 60, 1),
        'precipitation_10m_mm': latest['precipitation_10m_mm'],
        'precipitation_last_60m_mm': rain_60,
        'new_points_saved': len(new_points),
        'available_points_today': len(points),
        'archive_file': f'data/observations/{archive.name}',
    }
    (DATA / 'chmi-status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


def safe_main():
    try:
        main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        now = datetime.now(TZ)
        payload = {
            'ok': False,
            'provider': 'ČHMÚ OpenData',
            'station_name': STATION_NAME,
            'station_id': STATION_ID,
            'station_wsi': WSI,
            'distance_to_nove_hrabeci_km': DISTANCE_KM,
            'checked_at_local': now.isoformat(),
            'error': str(exc),
        }
        (DATA / 'chmi-status.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    safe_main()
