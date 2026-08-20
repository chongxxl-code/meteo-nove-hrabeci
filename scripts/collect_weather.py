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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LAT = 51.0162
LON = 14.4398
TZ = 'Europe/Prague'
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
ARCHIVE = DATA / 'archive'
HOURLY = ['temperature_2m', 'precipitation', 'cloud_cover', 'wind_speed_10m', 'wind_gusts_10m']
CURRENT = ['temperature_2m', 'relative_humidity_2m', 'precipitation', 'cloud_cover', 'pressure_msl', 'wind_speed_10m', 'wind_gusts_10m']
UA = 'nove-hrabeci-meteo/0.5 (+github-actions)'
DWD_ROOT = 'https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly'


def get_bytes(url: str, timeout: int = 30, tries: int = 3) -> bytes:
    err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            err = exc
            if attempt + 1 < tries:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f'GET failed {url}: {err}')


def get_json(url: str, timeout: int = 25, tries: int = 3):
    return json.loads(get_bytes(url, timeout=timeout, tries=tries).decode('utf-8'))


def make_url(base, params):
    return base + '?' + urllib.parse.urlencode(params, doseq=True)


def forecast_url(model=None):
    p = {
        'latitude': LAT,
        'longitude': LON,
        'timezone': TZ,
        'forecast_days': 3,
        'hourly': ','.join(HOURLY),
    }
    if model:
        p['models'] = model
    return make_url('https://api.open-meteo.com/v1/forecast', p)


def ecmwf_url():
    return make_url('https://api.open-meteo.com/v1/ecmwf', {
        'latitude': LAT,
        'longitude': LON,
        'timezone': TZ,
        'forecast_days': 3,
        'hourly': ','.join(HOURLY),
    })


def current_url():
    return make_url('https://api.open-meteo.com/v1/forecast', {
        'latitude': LAT,
        'longitude': LON,
        'timezone': TZ,
        'forecast_days': 1,
        'current': ','.join(CURRENT),
    })


def fetch_with_fallback(urls):
    last = None
    for u in urls:
        try:
            return get_json(u)
        except Exception as exc:
            last = exc
    raise RuntimeError(last)


def trim_hourly(payload, hours=48):
    h = payload.get('hourly') or {}
    times = h.get('time') or []
    key = datetime.now(ZoneInfo(TZ)).replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:00')
    try:
        start = times.index(key)
    except ValueError:
        start = 0
    end = min(len(times), start + hours)
    out = {'time': times[start:end]}
    for k in HOURLY:
        vals = h.get(k)
        if isinstance(vals, list):
            out[k] = vals[start:end]
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dwd_station_candidates(description_url: str):
    raw = get_bytes(description_url).decode('latin-1', errors='replace')
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).strftime('%Y%m%d')
    for line in raw.splitlines():
        parts = line.split(maxsplit=6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        sid, start, end, height, lat, lon, name = parts
        try:
            if end < cutoff:
                continue
            item = {
                'station_id': int(sid),
                'station_id_str': f'{int(sid):05d}',
                'start': start,
                'end': end,
                'elevation_m': float(height),
                'latitude': float(lat),
                'longitude': float(lon),
                'name': name.strip(),
            }
            item['distance_km'] = haversine_km(LAT, LON, item['latitude'], item['longitude'])
            out.append(item)
        except Exception:
            continue
    out.sort(key=lambda x: x['distance_km'])
    return out


def normalize_row(row):
    return {(k or '').strip(): (v or '').strip() for k, v in row.items()}


def valid_number(value):
    try:
        n = float(str(value).replace(',', '.'))
        return None if n <= -999 else n
    except Exception:
        return None


def parse_dwd_zip(blob: bytes, kind: str):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith('.txt') and 'produkt_' in n.lower()]
        if not members:
            raise RuntimeError('DWD ZIP has no product TXT')
        text = zf.read(members[0]).decode('latin-1', errors='replace')
    rows = [normalize_row(r) for r in csv.DictReader(io.StringIO(text), delimiter=';')]
    rows = [r for r in rows if r.get('MESS_DATUM')]
    if not rows:
        raise RuntimeError('DWD product contains no rows')
    for row in reversed(rows):
        stamp = row.get('MESS_DATUM', '')
        try:
            dt = datetime.strptime(stamp, '%Y%m%d%H').replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if kind == 'tu':
            temp = valid_number(row.get('TT_TU'))
            rh = valid_number(row.get('RF_TU'))
            if temp is None and rh is None:
                continue
            return {
                'observed_at_utc': dt.isoformat().replace('+00:00', 'Z'),
                'temperature_c': temp,
                'relative_humidity_pct': rh,
            }
        if kind == 'rr':
            rain = valid_number(row.get('R1'))
            if rain is None:
                continue
            return {
                'observed_at_utc': dt.isoformat().replace('+00:00', 'Z'),
                'precipitation_1h_mm': rain,
            }
    raise RuntimeError('No valid DWD observation row found')


def nearest_dwd_observation(kind: str):
    if kind == 'tu':
        sub = 'air_temperature/recent'
        desc = 'TU_Stundenwerte_Beschreibung_Stationen.txt'
        pattern = 'stundenwerte_TU_{sid}_akt.zip'
    elif kind == 'rr':
        sub = 'precipitation/recent'
        desc = 'RR_Stundenwerte_Beschreibung_Stationen.txt'
        pattern = 'stundenwerte_RR_{sid}_akt.zip'
    else:
        raise ValueError(kind)

    base = f'{DWD_ROOT}/{sub}'
    candidates = dwd_station_candidates(f'{base}/{desc}')
    last_error = None
    for station in candidates[:30]:
        url = f"{base}/{pattern.format(sid=station['station_id_str'])}"
        try:
            obs = parse_dwd_zip(get_bytes(url), kind)
            return {
                'provider': 'DWD CDC',
                'element': kind,
                'station_id': station['station_id_str'],
                'station_name': station['name'],
                'latitude': station['latitude'],
                'longitude': station['longitude'],
                'elevation_m': station['elevation_m'],
                'distance_km': round(station['distance_km'], 2),
                'source_url': url,
                **obs,
            }
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f'No active DWD {kind} station could be read: {last_error}')


def main():
    DATA.mkdir(exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(TZ))
    errors = []

    try:
        current = get_json(current_url()).get('current')
    except Exception as exc:
        current = None
        errors.append(f'current: {exc}')

    models = {}
    jobs = {
        'dwd': [forecast_url('dwd_icon_d2'), forecast_url('dwd_icon_seamless')],
        'chmi': [forecast_url('chmi_aladin_cz_1km'), forecast_url('chmi_aladin_seamless')],
        'ec': [ecmwf_url()],
    }
    for name, urls in jobs.items():
        try:
            models[name] = fetch_with_fallback(urls)
        except Exception as exc:
            errors.append(f'{name}: {exc}')

    try:
        rv = get_json('https://api.rainviewer.com/public/weather-maps.json')
    except Exception as exc:
        rv = None
        errors.append(f'rainviewer: {exc}')

    observations = {'dwd': {}}
    for kind in ('tu', 'rr'):
        try:
            observations['dwd'][kind] = nearest_dwd_observation(kind)
        except Exception as exc:
            observations['dwd'][kind] = {'ok': False, 'error': str(exc)}
            errors.append(f'dwd_observation_{kind}: {exc}')

    if not models:
        raise SystemExit('No forecast model could be collected.')

    snapshot = {
        'collected_at_utc': now_utc.isoformat().replace('+00:00', 'Z'),
        'collected_at_local': now_local.isoformat(),
        'location': {'name': 'Nové Hraběcí', 'latitude': LAT, 'longitude': LON, 'timezone': TZ},
        'current': current,
        'observations': observations,
        'models': {n: trim_hourly(p) for n, p in models.items()},
    }

    month = ARCHIVE / f'{now_local:%Y-%m}.jsonl'
    with month.open('a', encoding='utf-8') as f:
        f.write(json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')) + '\n')

    status_path = DATA / 'status.json'
    prev = 0
    if status_path.exists():
        try:
            prev = int(json.loads(status_path.read_text(encoding='utf-8')).get('total_snapshots', 0))
        except Exception:
            pass

    frames = ((rv or {}).get('radar') or {}).get('past') or []
    frames = frames[-12:]
    latest = frames[-1].get('time') if frames else None

    model_status = {}
    for name in ('dwd', 'chmi', 'ec'):
        p = models.get(name)
        vals = ((p or {}).get('hourly') or {}).get('temperature_2m') or []
        model_status[name] = {'ok': bool(p), 'temperature_2m': vals[0] if vals else None}

    status = {
        'schema': 2,
        'collected_at_utc': snapshot['collected_at_utc'],
        'collected_at_local': snapshot['collected_at_local'],
        'total_snapshots': prev + 1,
        'archive_file': f'data/archive/{month.name}',
        'models': model_status,
        'current': current,
        'observations': observations,
        'radar': {
            'rainviewer_frames': len(frames),
            'rainviewer_latest_unix': latest,
            'rainviewer_latest_local': datetime.fromtimestamp(latest, timezone.utc).astimezone(ZoneInfo(TZ)).strftime('%d.%m.%Y %H:%M') if latest else None,
        },
        'errors': errors,
        'note': 'Forecasts plus nearest active official DWD observations. ČHMÚ Šluknov rain gauge integration is next.',
    }
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'models': list(models), 'observations': observations, 'archive': str(month), 'errors': errors}, ensure_ascii=False))


if __name__ == '__main__':
    main()
