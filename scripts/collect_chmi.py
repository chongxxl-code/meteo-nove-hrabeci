#!/usr/bin/env python3
from __future__ import annotations

import json
import math
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
TARGET_LAT = 51.0162
TARGET_LON = 14.4398
PREFERRED_WSI = '0-203-0-11501049001'
PREFERRED_NAME = 'Šluknov'
BASE = 'https://opendata.chmi.cz/meteorology/climate'
UA = 'nove-hrabeci-meteo/0.9 (+github-actions)'


def get_json(url: str, tries: int = 2):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as exc:
            err = exc
            if i + 1 < tries:
                time.sleep(2)
    raise RuntimeError(f'GET failed: {err}')


def find_table(obj):
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


def table_rows(payload):
    table = find_table(payload)
    if not table:
        raise RuntimeError('JSON table {header, values} not found')
    header, values = table
    cols = [c.strip() for c in header.split(',')]
    rows = []
    for raw in values:
        if isinstance(raw, list):
            padded = raw + [None] * max(0, len(cols) - len(raw))
            rows.append(dict(zip(cols, padded)))
    return cols, rows


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def as_float(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(',', '.'))
        except Exception:
            return None
    return None


def parse_dt(v):
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_station_metadata(now_utc):
    errors = []
    for d in (now_utc.date(), (now_utc - timedelta(days=1)).date()):
        url = f'{BASE}/now/metadata/meta1-{d:%Y%m%d}.json'
        try:
            return get_json(url), url
        except Exception as exc:
            errors.append(f'{url}: {exc}')
    # Static fallback. It contains historical intervals, so we keep only rows active now.
    url = f'{BASE}/historical/metadata/meta1.json'
    try:
        return get_json(url), url
    except Exception as exc:
        errors.append(f'{url}: {exc}')
    raise RuntimeError('; '.join(errors))


def metadata_stations(payload, now_utc):
    _, rows = table_rows(payload)
    stations = {}
    for row in rows:
        # tolerate current and historical meta1 variants
        wsi = row.get('WSI')
        name = row.get('FULL_NAME')
        lon = as_float(row.get('GEOGR1'))
        lat = as_float(row.get('GEOGR2'))
        elev = as_float(row.get('ELEVATION'))
        if not wsi or not name or lat is None or lon is None:
            continue
        end = row.get('END_DATE')
        if end:
            edt = parse_dt(end)
            if edt and edt < now_utc:
                continue
        dist = haversine_km(TARGET_LAT, TARGET_LON, lat, lon)
        candidate = {
            'wsi': str(wsi), 'name': str(name), 'lat': lat, 'lon': lon,
            'elevation_m': elev, 'distance_km': round(dist, 2)
        }
        # multiple historical intervals can share WSI; retain the nearest/current representation
        stations[str(wsi)] = candidate
    out = list(stations.values())
    out.sort(key=lambda s: (0 if s['wsi'] == PREFERRED_WSI else 1, s['distance_km']))
    return out


def extract_sra10m(payload):
    cols, rows = table_rows(payload)
    out = []
    for row in rows:
        raw = list(row.values())
        if not any(str(v).strip().upper() == 'SRA10M' for v in raw if v is not None):
            continue
        observed = next((parse_dt(v) for v in raw if parse_dt(v)), None)
        if not observed:
            continue
        value = None
        for c in cols:
            key = re.sub(r'[^A-Z0-9]', '', c.upper())
            if key in ('VALUE', 'HODNOTA', 'VAL') or 'VALUE' in key:
                value = as_float(row.get(c))
                if value is not None:
                    break
        if value is None:
            # Fallback for schema variants: pick numeric measurement field, skipping metadata/flags.
            for c in cols:
                key = c.upper()
                if any(x in key for x in ('FLAG', 'QUALITY', 'WSI', 'DATE', 'TIME', 'INTERVAL', 'HEIGHT')):
                    continue
                v = row.get(c)
                if isinstance(v, str) and v.strip().upper() == 'SRA10M':
                    continue
                n = as_float(v)
                if n is not None:
                    value = n
                    break
        if value is not None:
            out.append({'observed_at_utc': observed.isoformat().replace('+00:00', 'Z'), 'precipitation_10m_mm': value})
    if not out:
        raise RuntimeError(f'No SRA10M rows parsed; columns={cols!r}; row_count={len(rows)}')
    return [dict(t) for t in {r['observed_at_utc']: r for r in out}.values()]


def try_station(station, now_utc):
    errors = []
    for d in (now_utc.date(), (now_utc - timedelta(days=1)).date()):
        url = f"{BASE}/now/data/10min/10m-{station['wsi']}-{d:%Y%m%d}.json"
        try:
            payload = get_json(url)
            points = extract_sra10m(payload)
            points.sort(key=lambda x: x['observed_at_utc'])
            return points, url
        except Exception as exc:
            errors.append(f'{url}: {exc}')
    raise RuntimeError(' | '.join(errors))


def existing_times(archive):
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

    meta, meta_url = fetch_station_metadata(now_utc)
    candidates = metadata_stations(meta, now_utc)
    if not candidates:
        raise RuntimeError('No ČHMÚ stations parsed from metadata')

    chosen = None
    points = None
    source_url = None
    attempts = []
    # 25 nearest/current candidates is enough for the region; preferred Šluknov is forced first.
    for station in candidates[:25]:
        if station['distance_km'] > 80:
            continue
        try:
            p, url = try_station(station, now_utc)
            chosen, points, source_url = station, p, url
            break
        except Exception as exc:
            attempts.append({'station': station['name'], 'wsi': station['wsi'], 'distance_km': station['distance_km'], 'error': str(exc)[:500]})

    if not chosen or not points:
        raise RuntimeError('No nearby ČHMÚ station with current SRA10M; attempts=' + json.dumps(attempts[:8], ensure_ascii=False))

    latest = points[-1]
    latest_dt = datetime.fromisoformat(latest['observed_at_utc'].replace('Z', '+00:00'))
    archive = OBS / f'chmi-rain-{chosen["wsi"].replace("-", "_")}-{now_local:%Y-%m}.jsonl'
    known = existing_times(archive)
    new_points = [p for p in points if p['observed_at_utc'] not in known]
    if new_points:
        with archive.open('a', encoding='utf-8') as f:
            for p in new_points:
                rec = {
                    'provider': 'ČHMÚ OpenData',
                    'station_name': chosen['name'],
                    'station_wsi': chosen['wsi'],
                    'latitude': chosen['lat'],
                    'longitude': chosen['lon'],
                    'elevation_m': chosen['elevation_m'],
                    'distance_to_nove_hrabeci_km': chosen['distance_km'],
                    **p,
                }
                f.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')

    recent = [p for p in points if datetime.fromisoformat(p['observed_at_utc'].replace('Z', '+00:00')) >= latest_dt - timedelta(minutes=50)]
    rain_60 = round(sum(float(p['precipitation_10m_mm']) for p in recent), 3)

    status = {
        'ok': True,
        'provider': 'ČHMÚ OpenData',
        'preferred_station': PREFERRED_NAME,
        'preferred_station_available': chosen['wsi'] == PREFERRED_WSI,
        'station_name': chosen['name'],
        'station_wsi': chosen['wsi'],
        'latitude': chosen['lat'],
        'longitude': chosen['lon'],
        'elevation_m': chosen['elevation_m'],
        'distance_to_nove_hrabeci_km': chosen['distance_km'],
        'metadata_url': meta_url,
        'source_url': source_url,
        'checked_at_local': now_local.isoformat(),
        'observed_at_utc': latest['observed_at_utc'],
        'observed_at_local': latest_dt.astimezone(TZ).isoformat(),
        'age_minutes': round((now_utc - latest_dt).total_seconds() / 60, 1),
        'precipitation_10m_mm': latest['precipitation_10m_mm'],
        'precipitation_last_60m_mm': rain_60,
        'new_points_saved': len(new_points),
        'available_points_today': len(points),
        'archive_file': f'data/observations/{archive.name}',
        'fallback_attempts_before_success': attempts[:8],
    }
    (DATA / 'chmi-status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


def safe_main():
    try:
        main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        payload = {
            'ok': False,
            'provider': 'ČHMÚ OpenData',
            'preferred_station': PREFERRED_NAME,
            'preferred_station_wsi': PREFERRED_WSI,
            'checked_at_local': datetime.now(TZ).isoformat(),
            'error': str(exc),
        }
        (DATA / 'chmi-status.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    safe_main()
