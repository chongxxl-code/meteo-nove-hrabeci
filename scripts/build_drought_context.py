#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'drought-context.json'
LAT, LON = 51.0162, 14.4398
UA = 'nove-hrabeci-local-observatory/1.0'


def get_json(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode('utf-8'))


def finite(v):
    try:
        return math.isfinite(float(v))
    except Exception:
        return False


def pct_rank(values, current):
    vals = sorted(float(v) for v in values if finite(v))
    if not vals or not finite(current):
        return None
    return round(100.0 * sum(v <= float(current) for v in vals) / len(vals), 1)


def sentinel_context():
    p = ROOT / 'data' / 'validation' / 'open-land-experiment-2026.json'
    if not p.exists():
        return {'ok': False}
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {'ok': False}
    scenes = []
    for s in d.get('scenes') or []:
        vals = []
        for z in s.get('zones') or []:
            ndmi = (z.get('ndmi') or {}).get('median')
            if z.get('ok') and finite(ndmi):
                vals.append(float(ndmi))
        if vals:
            scenes.append({'date': (s.get('scene_datetime') or '')[:10], 'median_ndmi': median(vals)})
    if not scenes:
        return {'ok': False}
    cur = scenes[-1]['median_ndmi']
    return {
        'ok': True,
        'latest_scene_date': scenes[-1]['date'],
        'median_open_land_ndmi': round(cur, 4),
        'seasonal_percentile': pct_rank([x['median_ndmi'] for x in scenes], cur),
        'scene_count': len(scenes),
    }


def chmi_coverage():
    now = datetime.now(timezone.utc)
    counts = {7: 0, 14: 0, 30: 0}
    sums = {7: 0.0, 14: 0.0, 30: 0.0}
    latest = None
    for path in sorted((ROOT / 'data' / 'observations').glob('chmi-rain-*.jsonl')):
        for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            try:
                d = json.loads(line)
                ts = datetime.fromisoformat(d['observed_at_utc'].replace('Z', '+00:00'))
                mm = float(d.get('precipitation_10m_mm') or 0.0)
            except Exception:
                continue
            if latest is None or ts > latest:
                latest = ts
            age = (now - ts).total_seconds() / 86400.0
            for days in counts:
                if 0 <= age <= days:
                    counts[days] += 1
                    sums[days] += mm
    coverage = {str(days): round(min(1.0, counts[days] / (days * 144)), 3) for days in counts}
    return {
        'latest_observation_utc': latest.isoformat() if latest else None,
        'coverage_fraction': coverage,
        'rain_mm': {str(k): round(v, 1) for k, v in sums.items()},
    }


def archive_background():
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=369)
    params = {
        'latitude': LAT,
        'longitude': LON,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'timezone': 'Europe/Prague',
        'daily': 'precipitation_sum,et0_fao_evapotranspiration',
        'hourly': 'soil_moisture_0_to_7cm,soil_moisture_7_to_28cm,soil_moisture_28_to_100cm',
    }
    url = 'https://archive-api.open-meteo.com/v1/archive?' + urllib.parse.urlencode(params)
    d = get_json(url)
    daily = d.get('daily') or {}
    hourly = d.get('hourly') or {}

    # Daily soil state = median of available hourly values for each calendar date.
    by_day = {}
    times = hourly.get('time') or []
    for i, ts in enumerate(times):
        day = ts[:10]
        row = by_day.setdefault(day, {'s0': [], 's1': [], 's2': []})
        for key, outk in [('soil_moisture_0_to_7cm', 's0'), ('soil_moisture_7_to_28cm', 's1'), ('soil_moisture_28_to_100cm', 's2')]:
            arr = hourly.get(key) or []
            if i < len(arr) and finite(arr[i]):
                row[outk].append(float(arr[i]))

    records = []
    dates = daily.get('time') or []
    p = daily.get('precipitation_sum') or []
    et0 = daily.get('et0_fao_evapotranspiration') or []
    for i, day in enumerate(dates):
        sm = by_day.get(day) or {}
        s0 = median(sm.get('s0') or []) if sm.get('s0') else None
        s1 = median(sm.get('s1') or []) if sm.get('s1') else None
        s2 = median(sm.get('s2') or []) if sm.get('s2') else None
        root = None
        if all(finite(x) for x in [s0, s1, s2]):
            # approximate 0-100 cm storage signal, weighted by layer thickness
            root = (7 * s0 + 21 * s1 + 72 * s2) / 100.0
        records.append({
            'date': day,
            'precip_mm': float(p[i]) if i < len(p) and finite(p[i]) else None,
            'et0_mm': float(et0[i]) if i < len(et0) and finite(et0[i]) else None,
            'soil_surface': s0,
            'soil_rootzone': root,
        })
    records = [r for r in records if finite(r.get('soil_surface')) or finite(r.get('precip_mm'))]
    if not records:
        raise RuntimeError('Open-Meteo archive returned no usable drought records')
    latest = records[-1]

    def window(days):
        rr = records[-days:]
        ps = sum(r['precip_mm'] for r in rr if finite(r.get('precip_mm')))
        es = sum(r['et0_mm'] for r in rr if finite(r.get('et0_mm')))
        return {
            'days': days,
            'precip_mm': round(ps, 1),
            'et0_mm': round(es, 1),
            'p_minus_et0_mm': round(ps - es, 1),
            'record_days': len(rr),
        }

    surface_series = [r['soil_surface'] for r in records if finite(r.get('soil_surface'))]
    root_series = [r['soil_rootzone'] for r in records if finite(r.get('soil_rootzone'))]
    return {
        'provider': 'Open-Meteo historical Best Match / ERA5-family background',
        'spatial_note': 'Background soil-moisture field is kilometre-scale to ~9 km, so Sentinel and terrain layers provide the local refinement.',
        'period_start': records[0]['date'],
        'period_end': latest['date'],
        'data_age_days': (date.today() - date.fromisoformat(latest['date'])).days,
        'windows': {str(n): window(n) for n in (7, 14, 30)},
        'latest_soil': {
            'surface_m3m3': round(latest['soil_surface'], 4) if finite(latest.get('soil_surface')) else None,
            'rootzone_0_100cm_m3m3': round(latest['soil_rootzone'], 4) if finite(latest.get('soil_rootzone')) else None,
            'surface_percentile_1y': pct_rank(surface_series, latest.get('soil_surface')),
            'rootzone_percentile_1y': pct_rank(root_series, latest.get('soil_rootzone')),
        },
    }


def classify(bg, sat, chmi):
    score = 0
    reasons = []
    w30 = (bg.get('windows') or {}).get('30') or {}
    bal30 = w30.get('p_minus_et0_mm')
    soil = bg.get('latest_soil') or {}
    rootp = soil.get('rootzone_percentile_1y')
    surfp = soil.get('surface_percentile_1y')
    satp = sat.get('seasonal_percentile') if sat.get('ok') else None

    if finite(bal30):
        if bal30 <= -60:
            score += 2
            reasons.append(f'30denní vodní bilance P−ET₀ {bal30:.1f} mm')
        elif bal30 <= -35:
            score += 1
            reasons.append(f'30denní vodní bilance P−ET₀ {bal30:.1f} mm')
    if finite(rootp):
        if rootp <= 10:
            score += 2
            reasons.append(f'kořenová půdní vlhkost je jen na {rootp:.0f}. percentilu posledního roku')
        elif rootp <= 25:
            score += 1
            reasons.append(f'kořenová půdní vlhkost je na {rootp:.0f}. percentilu posledního roku')
    if finite(surfp) and surfp <= 15:
        score += 1
        reasons.append(f'povrchová půdní vlhkost je na {surfp:.0f}. percentilu posledního roku')
    if finite(satp) and satp <= 25:
        score += 1
        reasons.append(f'Sentinel NDMI travních ploch je na {satp:.0f}. percentilu letošních scén')

    rank = 0 if score <= 1 else 1 if score <= 3 else 2 if score <= 5 else 3
    levels = ['green', 'yellow', 'orange', 'red']
    headlines = ['bez potvrzeného výrazného vysychání', 'zvýšené vysychání', 'výrazné lokální sucho', 'extrémní lokální sucho']
    cov30 = float(((chmi.get('coverage_fraction') or {}).get('30') or 0))
    if cov30 < 0.8:
        reasons.append(f'vlastní ČHMÚ 30denní archiv má zatím {cov30*100:.0f} % pokrytí; používá se reanalýzní vodní bilance jako most')
    if not reasons:
        reasons.append('vodní bilance, půdní vlhkost a Sentinel nyní nedávají výrazný společný signál')
    return {
        'rank': rank,
        'level': levels[rank],
        'headline': headlines[rank],
        'score': score,
        'reasons': reasons,
        'confidence': 'medium',
    }


def main():
    sat = sentinel_context()
    chmi = chmi_coverage()
    try:
        bg = archive_background()
        state = classify(bg, sat, chmi)
        ok = True
        err = None
    except Exception as e:
        bg = {'ok': False}
        state = {'rank': 0, 'level': 'green', 'headline': 'stav zatím nelze spolehlivě určit', 'score': 0, 'reasons': [str(e)[:200]], 'confidence': 'low'}
        ok = False
        err = str(e)[:300]
    payload = {
        'schema': 1,
        'location': {'name': 'Nové Hraběcí', 'lat': LAT, 'lon': LON},
        'computed_at_utc': datetime.now(timezone.utc).isoformat(),
        'quality_status': 'operational_local_drought_v1' if ok else 'degraded',
        'state': state,
        'background': bg,
        'chmi_local_rain': chmi,
        'sentinel_local_landscape': sat,
        'method_note': 'Operational local drought signal: P-ET0 water balance + soil-moisture percentiles + Sentinel NDMI. It is not an official drought classification.',
        'known_limitations': [
            'Reanalysis soil moisture is coarser than the local terrain; Sentinel and the reviewed landscape network provide the local spatial refinement.',
            'The first-year percentile is an operational baseline, not yet a long-term local climatology.',
            'Copernicus 1 km Surface Soil Moisture / Soil Water Index is planned as an additional independent satellite layer.'
        ],
    }
    if err:
        payload['error'] = err
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': ok, 'level': state['level'], 'rank': state['rank'], 'output': str(OUT.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
