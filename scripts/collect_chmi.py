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
PREFERRED_RAIN_WSI = '0-203-0-11501049001'
PREFERRED_RAIN_NAME = 'Šluknov'
BASE = 'https://opendata.chmi.cz/meteorology/climate'
UA = 'nove-hrabeci-meteo/1.2 (+github-actions)'
WANTED = {
    'T': 'temperature_c', 'H': 'relative_humidity_pct', 'F': 'wind_speed_ms',
    'Fmax': 'wind_gust_ms', 'D': 'wind_direction_deg', 'SRA10M': 'precipitation_10m_mm',
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
            if found: return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_table(v)
            if found: return found
    return None


def table_rows(payload):
    table = find_table(payload)
    if not table: raise RuntimeError('JSON table {header, values} not found')
    header, values = table
    cols = [c.strip() for c in header.split(',')]
    rows = []
    for raw in values:
        if isinstance(raw, list):
            rows.append(dict(zip(cols, raw + [None] * max(0, len(cols) - len(raw)))))
    return cols, rows


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def as_float(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool): return float(v)
    if isinstance(v, str):
        try: return float(v.strip().replace(',', '.'))
        except Exception: return None
    return None


def parse_dt(v):
    if not isinstance(v, str): return None
    s = v.strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', s): return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: return None


def fetch_metadata(name, now_utc):
    errors = []
    for d in (now_utc.date(), (now_utc-timedelta(days=1)).date()):
        url = f'{BASE}/now/metadata/{name}-{d:%Y%m%d}.json'
        try: return get_json(url), url
        except Exception as exc: errors.append(f'{url}: {exc}')
    url = f'{BASE}/historical/metadata/{name}.json'
    try: return get_json(url), url
    except Exception as exc: errors.append(f'{url}: {exc}')
    raise RuntimeError('; '.join(errors))


def metadata_stations(payload, now_utc):
    _, rows = table_rows(payload)
    stations = {}
    for row in rows:
        wsi, name = row.get('WSI'), row.get('FULL_NAME')
        lon, lat, elev = as_float(row.get('GEOGR1')), as_float(row.get('GEOGR2')), as_float(row.get('ELEVATION'))
        if not wsi or not name or lat is None or lon is None: continue
        end = row.get('END_DATE')
        if end:
            edt = parse_dt(end)
            if edt and edt < now_utc: continue
        stations[str(wsi)] = {
            'wsi': str(wsi), 'name': str(name), 'lat': lat, 'lon': lon, 'elevation_m': elev,
            'distance_km': round(haversine_km(TARGET_LAT, TARGET_LON, lat, lon), 2),
        }
    return list(stations.values())


def metadata_elements(payload):
    _, rows = table_rows(payload)
    out = {}
    for row in rows:
        wsi = row.get('WSI')
        el = row.get('EG_EL_ABBREVIATION') or row.get('EL_ABBREVIATION')
        if wsi and el:
            out.setdefault(str(wsi), set()).add(str(el).strip())
    return out


def row_element(row):
    for v in row.values():
        if isinstance(v, str) and v.strip() in WANTED: return v.strip()
    return None


def row_value(cols, row, element):
    for c in cols:
        key = re.sub(r'[^A-Z0-9]', '', c.upper())
        if key in ('VALUE','HODNOTA','VAL') or 'VALUE' in key:
            n = as_float(row.get(c))
            if n is not None: return n
    for c in cols:
        key = c.upper()
        if any(x in key for x in ('FLAG','QUALITY','WSI','DATE','TIME','INTERVAL','HEIGHT')): continue
        v = row.get(c)
        if isinstance(v, str) and v.strip() == element: continue
        n = as_float(v)
        if n is not None: return n
    return None


def extract_weather(payload, required):
    cols, rows = table_rows(payload)
    by_time, available = {}, set()
    for row in rows:
        element = row_element(row)
        if not element: continue
        observed = next((parse_dt(v) for v in row.values() if parse_dt(v)), None)
        value = row_value(cols, row, element)
        if not observed or value is None: continue
        available.add(element)
        key = observed.isoformat().replace('+00:00','Z')
        by_time.setdefault(key, {'observed_at_utc': key})[WANTED[element]] = value
    if required and not set(required).issubset(available):
        raise RuntimeError(f'missing required {sorted(set(required)-available)}; available={sorted(available)}')
    return [by_time[k] for k in sorted(by_time)], sorted(available)


def try_station(station, now_utc, required):
    errors = []
    for d in (now_utc.date(), (now_utc-timedelta(days=1)).date()):
        url = f"{BASE}/now/data/10m-{station['wsi']}-{d:%Y%m%d}.json"
        try:
            points, available = extract_weather(get_json(url), required)
            if not points: raise RuntimeError('no usable observations')
            return points, available, url
        except Exception as exc: errors.append(f'{url}: {exc}')
    raise RuntimeError(' | '.join(errors))


def choose_station(stations, elements, required, now_utc, preferred_wsi=None, max_km=80):
    eligible = [s for s in stations if s['distance_km'] <= max_km and set(required).issubset(elements.get(s['wsi'], set()))]
    eligible.sort(key=lambda s: (0 if preferred_wsi and s['wsi']==preferred_wsi else 1, s['distance_km']))
    attempts = []
    for s in eligible[:30]:
        try:
            pts, avail, url = try_station(s, now_utc, required)
            return s, pts, avail, url, attempts
        except Exception as exc:
            attempts.append({'station':s['name'],'wsi':s['wsi'],'distance_km':s['distance_km'],'error':str(exc)[:350]})
    return None, None, None, None, attempts


def existing_times(path):
    out=set()
    if not path.exists(): return out
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            x=json.loads(line)
            if x.get('observed_at_utc'): out.add(x['observed_at_utc'])
        except Exception: pass
    return out


def append_unique(path, records):
    known=existing_times(path)
    new=[r for r in records if r.get('observed_at_utc') not in known]
    if new:
        with path.open('a',encoding='utf-8') as f:
            for r in new: f.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
    return len(new)


def station_meta(s):
    return {'provider':'ČHMÚ OpenData','station_name':s['name'],'station_wsi':s['wsi'],
            'latitude':s['lat'],'longitude':s['lon'],'elevation_m':s['elevation_m'],
            'distance_to_nove_hrabeci_km':s['distance_km']}


def main():
    DATA.mkdir(exist_ok=True); OBS.mkdir(exist_ok=True)
    now_utc=datetime.now(timezone.utc); now_local=now_utc.astimezone(TZ)
    meta1, meta1_url=fetch_metadata('meta1',now_utc)
    meta2, meta2_url=fetch_metadata('meta2',now_utc)
    stations=metadata_stations(meta1,now_utc); elements=metadata_elements(meta2)

    rain_s, rain_pts, rain_avail, rain_url, rain_attempts = choose_station(
        stations,elements,['SRA10M'],now_utc,preferred_wsi=PREFERRED_RAIN_WSI)
    if not rain_s: raise RuntimeError('No nearby ČHMÚ SRA10M station: '+json.dumps(rain_attempts[:6],ensure_ascii=False))

    temp_s, temp_pts, temp_avail, temp_url, temp_attempts = choose_station(
        stations,elements,['T'],now_utc)

    rain_archive=OBS/f'chmi-rain-{rain_s["wsi"].replace("-","_")}-{now_local:%Y-%m}.jsonl'
    rain_records=[{**station_meta(rain_s),'observed_at_utc':p['observed_at_utc'],'precipitation_10m_mm':p['precipitation_10m_mm']}
                  for p in rain_pts if p.get('precipitation_10m_mm') is not None]
    rain_new=append_unique(rain_archive,rain_records)

    weather_new=0; weather_archive=None
    if temp_s and temp_pts:
        weather_archive=OBS/f'chmi-weather-{temp_s["wsi"].replace("-","_")}-{now_local:%Y-%m}.jsonl'
        weather_new=append_unique(weather_archive,[{**station_meta(temp_s),**p} for p in temp_pts])

    rain_latest=rain_pts[-1]
    rain_dt=datetime.fromisoformat(rain_latest['observed_at_utc'].replace('Z','+00:00'))
    recent=[p for p in rain_pts if datetime.fromisoformat(p['observed_at_utc'].replace('Z','+00:00'))>=rain_dt-timedelta(minutes=50)]
    rain60=round(sum(float(p.get('precipitation_10m_mm') or 0) for p in recent),3)

    temp_latest=temp_pts[-1] if temp_pts else None
    temp_dt=datetime.fromisoformat(temp_latest['observed_at_utc'].replace('Z','+00:00')) if temp_latest else None
    status={
        'ok':True,'provider':'ČHMÚ OpenData','metadata_url':meta1_url,'elements_metadata_url':meta2_url,
        'preferred_station':PREFERRED_RAIN_NAME,'preferred_station_available':rain_s['wsi']==PREFERRED_RAIN_WSI,
        'station_name':rain_s['name'],'station_wsi':rain_s['wsi'],'latitude':rain_s['lat'],'longitude':rain_s['lon'],
        'elevation_m':rain_s['elevation_m'],'distance_to_nove_hrabeci_km':rain_s['distance_km'],
        'source_url':rain_url,'checked_at_local':now_local.isoformat(),'observed_at_utc':rain_latest['observed_at_utc'],
        'observed_at_local':rain_dt.astimezone(TZ).isoformat(),'age_minutes':round((now_utc-rain_dt).total_seconds()/60,1),
        'available_elements':rain_avail,'precipitation_10m_mm':rain_latest.get('precipitation_10m_mm'),
        'precipitation_last_60m_mm':rain60,'new_rain_points_saved':rain_new,
        'archive_file':f'data/observations/{rain_archive.name}','fallback_attempts_before_success':rain_attempts[:8],
        'weather_station_available':bool(temp_s),'weather_station_name':None if not temp_s else temp_s['name'],
        'weather_station_wsi':None if not temp_s else temp_s['wsi'],
        'weather_station_distance_km':None if not temp_s else temp_s['distance_km'],
        'weather_source_url':temp_url,'weather_available_elements':temp_avail or [],
        'weather_observed_at_utc':None if not temp_latest else temp_latest['observed_at_utc'],
        'weather_age_minutes':None if not temp_dt else round((now_utc-temp_dt).total_seconds()/60,1),
        'temperature_c':None if not temp_latest else temp_latest.get('temperature_c'),
        'relative_humidity_pct':None if not temp_latest else temp_latest.get('relative_humidity_pct'),
        'wind_speed_ms':None if not temp_latest else temp_latest.get('wind_speed_ms'),
        'wind_gust_ms':None if not temp_latest else temp_latest.get('wind_gust_ms'),
        'wind_direction_deg':None if not temp_latest else temp_latest.get('wind_direction_deg'),
        'new_weather_points_saved':weather_new,
        'weather_archive_file':None if not weather_archive else f'data/observations/{weather_archive.name}',
        'weather_fallback_attempts':temp_attempts[:8],
    }
    (DATA/'chmi-status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(status,ensure_ascii=False))


def safe_main():
    try: main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        payload={'ok':False,'provider':'ČHMÚ OpenData','preferred_station':PREFERRED_RAIN_NAME,
                 'preferred_station_wsi':PREFERRED_RAIN_WSI,'checked_at_local':datetime.now(TZ).isoformat(),'error':str(exc)}
        (DATA/'chmi-status.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False))


if __name__=='__main__': safe_main()
