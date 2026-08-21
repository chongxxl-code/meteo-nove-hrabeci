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
UA = 'nove-hrabeci-meteo/1.1 (+github-actions)'
WANTED = {
    'T': 'temperature_c',
    'H': 'relative_humidity_pct',
    'F': 'wind_speed_ms',
    'Fmax': 'wind_gust_ms',
    'D': 'wind_direction_deg',
    'SRA10M': 'precipitation_10m_mm',
}


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
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
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
        stations[str(wsi)] = {
            'wsi': str(wsi), 'name': str(name), 'lat': lat, 'lon': lon,
            'elevation_m': elev, 'distance_km': round(dist, 2),
        }
    out = list(stations.values())
    out.sort(key=lambda s: (0 if s['wsi'] == PREFERRED_WSI else 1, s['distance_km']))
    return out


def row_element(row):
    for v in row.values():
        if isinstance(v, str):
            s = v.strip()
            if s in WANTED:
                return s
    return None


def row_value(cols, row, element):
    for c in cols:
        key = re.sub(r'[^A-Z0-9]', '', c.upper())
        if key in ('VALUE', 'HODNOTA', 'VAL') or 'VALUE' in key:
            n = as_float(row.get(c))
            if n is not None:
                return n
    for c in cols:
        key = c.upper()
        if any(x in key for x in ('FLAG', 'QUALITY', 'WSI', 'DATE', 'TIME', 'INTERVAL', 'HEIGHT')):
            continue
        v = row.get(c)
        if isinstance(v, str) and v.strip() == element:
            continue
        n = as_float(v)
        if n is not None:
            return n
    return None


def extract_weather(payload):
    cols, rows = table_rows(payload)
    by_time = {}
    available = set()
    for row in rows:
        element = row_element(row)
        if not element:
            continue
        observed = next((parse_dt(v) for v in row.values() if parse_dt(v)), None)
        value = row_value(cols, row, element)
        if not observed or value is None:
            continue
        available.add(element)
        key = observed.isoformat().replace('+00:00', 'Z')
        rec = by_time.setdefault(key, {'observed_at_utc': key})
        rec[WANTED[element]] = value
    points = [by_time[k] for k in sorted(by_time)]
    if not points or 'SRA10M' not in available:
        raise RuntimeError(f'No usable SRA10M observations; columns={cols!r}; row_count={len(rows)}')
    return points, sorted(available)


def try_station(station, now_utc):
    errors = []
    for d in (now_utc.date(), (now_utc - timedelta(days=1)).date()):
        url = f"{BASE}/now/data/10m-{station['wsi']}-{d:%Y%m%d}.json"
        try:
            payload = get_json(url)
            points, available = extract_weather(payload)
            return points, available, url
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


def append_unique(archive, records):
    known = existing_times(archive)
    new = [r for r in records if r.get('observed_at_utc') not in known]
    if new:
        with archive.open('a', encoding='utf-8') as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n')
    return len(new)


def main():
    DATA.mkdir(exist_ok=True)
    OBS.mkdir(exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)

    meta, meta_url = fetch_station_metadata(now_utc)
    candidates = metadata_stations(meta, now_utc)
    if not candidates:
        raise RuntimeError('No ČHMÚ stations parsed from metadata')

    chosen = points = source_url = available = None
    attempts = []
    for station in candidates[:25]:
        if station['distance_km'] > 80:
            continue
        try:
            p, elems, url = try_station(station, now_utc)
            chosen, points, available, source_url = station, p, elems, url
            break
        except Exception as exc:
            attempts.append({'station': station['name'], 'wsi': station['wsi'],
                             'distance_km': station['distance_km'], 'error': str(exc)[:500]})

    if not chosen or not points:
        raise RuntimeError('No nearby ČHMÚ station with current 10m data; attempts=' + json.dumps(attempts[:8], ensure_ascii=False))

    base_meta = {
        'provider': 'ČHMÚ OpenData', 'station_name': chosen['name'], 'station_wsi': chosen['wsi'],
        'latitude': chosen['lat'], 'longitude': chosen['lon'], 'elevation_m': chosen['elevation_m'],
        'distance_to_nove_hrabeci_km': chosen['distance_km'],
    }

    weather_archive = OBS / f'chmi-weather-{chosen["wsi"].replace("-", "_")}-{now_local:%Y-%m}.jsonl'
    weather_records = [{**base_meta, **p} for p in points]
    weather_new = append_unique(weather_archive, weather_records)

    # Preserve the original rain-only archive for existing drought/landscape consumers.
    rain_archive = OBS / f'chmi-rain-{chosen["wsi"].replace("-", "_")}-{now_local:%Y-%m}.jsonl'
    rain_records = [{**base_meta, 'observed_at_utc': p['observed_at_utc'],
                     'precipitation_10m_mm': p['precipitation_10m_mm']}
                    for p in points if p.get('precipitation_10m_mm') is not None]
    rain_new = append_unique(rain_archive, rain_records)

    latest = points[-1]
    latest_dt = datetime.fromisoformat(latest['observed_at_utc'].replace('Z', '+00:00'))
    recent = [p for p in points if datetime.fromisoformat(p['observed_at_utc'].replace('Z', '+00:00')) >= latest_dt - timedelta(minutes=50)]
    rain_60 = round(sum(float(p.get('precipitation_10m_mm') or 0) for p in recent), 3)

    status = {
        'ok': True, 'provider': 'ČHMÚ OpenData', 'preferred_station': PREFERRED_NAME,
        'preferred_station_available': chosen['wsi'] == PREFERRED_WSI,
        'station_name': chosen['name'], 'station_wsi': chosen['wsi'],
        'latitude': chosen['lat'], 'longitude': chosen['lon'], 'elevation_m': chosen['elevation_m'],
        'distance_to_nove_hrabeci_km': chosen['distance_km'], 'metadata_url': meta_url,
        'source_url': source_url, 'checked_at_local': now_local.isoformat(),
        'observed_at_utc': latest['observed_at_utc'], 'observed_at_local': latest_dt.astimezone(TZ).isoformat(),
        'age_minutes': round((now_utc - latest_dt).total_seconds() / 60, 1),
        'available_elements': available,
        'temperature_c': latest.get('temperature_c'),
        'relative_humidity_pct': latest.get('relative_humidity_pct'),
        'wind_speed_ms': latest.get('wind_speed_ms'),
        'wind_gust_ms': latest.get('wind_gust_ms'),
        'wind_direction_deg': latest.get('wind_direction_deg'),
        'precipitation_10m_mm': latest.get('precipitation_10m_mm'),
        'precipitation_last_60m_mm': rain_60,
        'new_weather_points_saved': weather_new, 'new_rain_points_saved': rain_new,
        'available_points_today': len(points),
        'weather_archive_file': f'data/observations/{weather_archive.name}',
        'archive_file': f'data/observations/{rain_archive.name}',
        'fallback_attempts_before_success': attempts[:8],
    }
    (DATA / 'chmi-status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


def safe_main():
    try:
        main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        payload = {'ok': False, 'provider': 'ČHMÚ OpenData', 'preferred_station': PREFERRED_NAME,
                   'preferred_station_wsi': PREFERRED_WSI, 'checked_at_local': datetime.now(TZ).isoformat(),
                   'error': str(exc)}
        (DATA / 'chmi-status.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    safe_main()
