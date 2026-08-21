#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import math
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OBS = DATA / 'observations'
TZ = ZoneInfo('Europe/Prague')
TARGET_LAT = 51.0162
TARGET_LON = 14.4398
STATION_ID = '06129'
STATION_NAME = 'Sohland/Spree'
BASE = 'https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now'
ZIP_URL = f'{BASE}/10minutenwerte_TU_{STATION_ID}_now.zip'
DESC_URL = f'{BASE}/zehn_st_now_tu_Beschreibung_Stationen.txt'
UA = 'nove-hrabeci-meteo/1.0 (+github-actions)'
MAX_AGE_MIN = 90


def get_bytes(url: str, tries: int = 3) -> bytes:
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as exc:
            err = exc
            if i + 1 < tries:
                time.sleep(2 + i * 2)
    raise RuntimeError(f'GET failed {url}: {err}')


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def station_meta():
    raw = get_bytes(DESC_URL).decode('latin-1', errors='replace')
    for line in raw.splitlines():
        parts = line.split(maxsplit=6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        if f'{int(parts[0]):05d}' != STATION_ID:
            continue
        _, start, end, height, lat, lon, rest = parts
        latf, lonf = float(lat), float(lon)
        name = rest.strip().split('  ')[0].strip() or STATION_NAME
        return {
            'provider':'DWD CDC 10-minute now',
            'station_id':STATION_ID,
            'station_name':name,
            'latitude':latf,
            'longitude':lonf,
            'elevation_m':float(height),
            'distance_to_nove_hrabeci_km':round(haversine_km(TARGET_LAT,TARGET_LON,latf,lonf),2),
            'station_start':start,
            'station_end':end,
        }
    return {
        'provider':'DWD CDC 10-minute now',
        'station_id':STATION_ID,
        'station_name':STATION_NAME,
        'distance_to_nove_hrabeci_km':4.89,
    }


def num(v):
    try:
        n = float(str(v).strip().replace(',', '.'))
        return None if n <= -999 else n
    except Exception:
        return None


def parse_time(s):
    s = str(s or '').strip()
    for fmt in ('%Y%m%d%H%M','%Y%m%d%H'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def parse_zip(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith('.txt') and 'produkt_' in n.lower()]
        if not names:
            raise RuntimeError('DWD ZIP has no product TXT')
        text = zf.read(names[0]).decode('latin-1', errors='replace')
    rows = csv.DictReader(io.StringIO(text), delimiter=';')
    out = []
    for raw in rows:
        row = {(k or '').strip():(v or '').strip() for k,v in raw.items()}
        t = parse_time(row.get('MESS_DATUM'))
        temp = num(row.get('TT_10'))
        rh = num(row.get('RF_10'))
        if not t or (temp is None and rh is None):
            continue
        out.append({
            'observed_at_utc':t.isoformat().replace('+00:00','Z'),
            'temperature_c':temp,
            'relative_humidity_pct':rh,
        })
    if not out:
        raise RuntimeError('No valid 10-minute temperature rows parsed')
    unique = {x['observed_at_utc']:x for x in out}
    return [unique[k] for k in sorted(unique)]


def existing_times(path):
    out=set()
    if not path.exists(): return out
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            x=json.loads(line)
            if x.get('observed_at_utc'): out.add(x['observed_at_utc'])
        except Exception: pass
    return out


def main():
    DATA.mkdir(exist_ok=True); OBS.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    meta = station_meta()
    points = parse_zip(get_bytes(ZIP_URL))
    latest = points[-1]
    latest_dt = datetime.fromisoformat(latest['observed_at_utc'].replace('Z','+00:00'))
    age_min = (now-latest_dt).total_seconds()/60
    if age_min > MAX_AGE_MIN:
        raise RuntimeError(f'Sohland 10-minute data stale: {age_min:.1f} min')

    archive = OBS/f'dwd-sohland-{STATION_ID}-{now.astimezone(TZ):%Y-%m}.jsonl'
    known = existing_times(archive)
    new = []
    for p in points:
        if p['observed_at_utc'] in known: continue
        new.append({**meta, **p, 'source_url':ZIP_URL})
    if new:
        with archive.open('a',encoding='utf-8') as f:
            for rec in new:
                f.write(json.dumps(rec,ensure_ascii=False,separators=(',',':'))+'\n')

    status = {
        'ok':True,
        **meta,
        'source_url':ZIP_URL,
        'checked_at_utc':now.isoformat().replace('+00:00','Z'),
        'observed_at_utc':latest['observed_at_utc'],
        'age_minutes':round(age_min,1),
        'temperature_c':latest.get('temperature_c'),
        'relative_humidity_pct':latest.get('relative_humidity_pct'),
        'new_points_saved':len(new),
        'available_points_now':len(points),
        'archive_file':f'data/observations/{archive.name}',
    }
    (DATA/'dwd-sohland-status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(status,ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        payload={'ok':False,'provider':'DWD CDC 10-minute now','station_id':STATION_ID,'station_name':STATION_NAME,
                 'checked_at_utc':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'error':str(exc)}
        (DATA/'dwd-sohland-status.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False))
