#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
FORECAST_ARCHIVE = DATA / 'archive'
RADAR_ARCHIVE = DATA / 'radar-observations'
OUT = DATA / 'forecast-verification.json'
TZ = ZoneInfo('Europe/Prague')
RAIN_FORECAST_THRESHOLD_MM = 0.1
RAIN_OBS_THRESHOLD_MM = 0.1
RADAR_WINDOW_MIN = 40
OBS_MATCH_MIN = 90


def dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.astimezone(timezone.utc)


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def rounded(v, n=3):
    return None if v is None or not math.isfinite(v) else round(v, n)


def lead_bin(h):
    if h < 0: return None
    if h <= 6: return '0–6 h'
    if h <= 12: return '6–12 h'
    if h <= 24: return '12–24 h'
    if h <= 48: return '24–48 h'
    return '48–72 h'


def load_forecasts():
    snapshots = []
    if not FORECAST_ARCHIVE.exists():
        return snapshots
    for p in sorted(FORECAST_ARCHIVE.glob('*.jsonl')):
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                snapshots.append(json.loads(line))
            except Exception:
                pass
    return snapshots


def load_radar():
    out = []
    if not RADAR_ARCHIVE.exists():
        return out
    for p in sorted(RADAR_ARCHIVE.glob('*.jsonl')):
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                r = json.loads(line)
                t = dt(r.get('radar_time_utc'))
                if t:
                    out.append((t, r))
            except Exception:
                pass
    out.sort(key=lambda x: x[0])
    return out


def build_dwd_observations(snapshots):
    temp, rain = [], []
    seen = set()
    for s in snapshots:
        dwd = ((s.get('observations') or {}).get('dwd') or {})
        tu = dwd.get('tu') or {}
        t = dt(tu.get('observed_at_utc'))
        if t and tu.get('temperature_c') is not None:
            k = ('t', t.isoformat(), tu.get('station_id'))
            if k not in seen:
                seen.add(k)
                temp.append((t, float(tu['temperature_c']), tu))
        rr = dwd.get('rr') or {}
        t = dt(rr.get('observed_at_utc'))
        if t and rr.get('precipitation_1h_mm') is not None:
            k = ('r', t.isoformat(), rr.get('station_id'))
            if k not in seen:
                seen.add(k)
                rain.append((t, float(rr['precipitation_1h_mm']), rr))
    temp.sort(key=lambda x: x[0]); rain.sort(key=lambda x: x[0])
    return temp, rain


def nearest_obs(target, arr, tol_min=OBS_MATCH_MIN):
    best = None
    for item in arr:
        delta = abs((item[0] - target).total_seconds()) / 60
        if delta <= tol_min and (best is None or delta < best[0]):
            best = (delta, item)
    return best[1] if best else None


def radar_obs(target, radar):
    vals = []
    for t, r in radar:
        delta = abs((t - target).total_seconds()) / 60
        if delta <= RADAR_WINDOW_MIN and r.get('local_rain_signal') is not None:
            vals.append(bool(r['local_rain_signal']))
    if not vals:
        return None, 0
    return any(vals), len(vals)


def main():
    snapshots = load_forecasts()
    radar = load_radar()
    temp_obs, rain_obs = build_dwd_observations(snapshots)
    now = datetime.now(timezone.utc)

    temp_cases = []
    rain_cases = []

    for s in snapshots:
        issued = dt(s.get('collected_at_utc'))
        if not issued:
            continue
        for model, m in (s.get('models') or {}).items():
            times = m.get('time') or []
            temps = m.get('temperature_2m') or []
            rains = m.get('precipitation') or []
            for i, ts in enumerate(times):
                target = dt(ts)
                if not target or target > now:
                    continue
                lead_h = (target - issued).total_seconds() / 3600
                bucket = lead_bin(lead_h)
                if bucket is None or lead_h > 72:
                    continue

                if i < len(temps) and temps[i] is not None:
                    obs = nearest_obs(target, temp_obs)
                    if obs:
                        observed = obs[1]
                        forecast = float(temps[i])
                        temp_cases.append({
                            'model': model, 'lead_bin': bucket, 'lead_h': round(lead_h, 2),
                            'issued_at_utc': issued.isoformat().replace('+00:00','Z'),
                            'target_at_utc': target.isoformat().replace('+00:00','Z'),
                            'forecast_c': forecast, 'observed_c': observed,
                            'error_c': round(forecast - observed, 3),
                            'station': obs[2].get('station_name'), 'station_distance_km': obs[2].get('distance_km')
                        })

                if i < len(rains) and rains[i] is not None:
                    forecast_mm = float(rains[i])
                    forecast_rain = forecast_mm >= RAIN_FORECAST_THRESHOLD_MM
                    local, samples = radar_obs(target, radar)
                    station = nearest_obs(target, rain_obs)
                    station_rain = None if not station else station[1] >= RAIN_OBS_THRESHOLD_MM
                    if local is not None or station is not None:
                        observed_rain = local if local is not None else station_rain
                        outcome = ('hit' if forecast_rain and observed_rain else
                                   'false_alarm' if forecast_rain and not observed_rain else
                                   'miss' if (not forecast_rain) and observed_rain else 'correct_dry')
                        rain_cases.append({
                            'model': model, 'lead_bin': bucket, 'lead_h': round(lead_h, 2),
                            'issued_at_utc': issued.isoformat().replace('+00:00','Z'),
                            'target_at_utc': target.isoformat().replace('+00:00','Z'),
                            'forecast_mm': forecast_mm, 'forecast_rain': forecast_rain,
                            'observed_rain': observed_rain, 'outcome': outcome,
                            'verification_source': 'local_radar' if local is not None else 'dwd_station',
                            'radar_samples': samples,
                            'station_rain_mm': None if not station else station[1],
                            'station': None if not station else station[2].get('station_name'),
                            'station_distance_km': None if not station else station[2].get('distance_km')
                        })

    summary = {}
    models = sorted({c['model'] for c in temp_cases + rain_cases})
    bins = ['0–6 h','6–12 h','12–24 h','24–48 h','48–72 h']
    for model in models:
        summary[model] = {}
        for b in bins:
            tc = [x for x in temp_cases if x['model']==model and x['lead_bin']==b]
            rc = [x for x in rain_cases if x['model']==model and x['lead_bin']==b]
            errs = [x['error_c'] for x in tc]
            hits = sum(x['outcome']=='hit' for x in rc)
            fa = sum(x['outcome']=='false_alarm' for x in rc)
            misses = sum(x['outcome']=='miss' for x in rc)
            dry = sum(x['outcome']=='correct_dry' for x in rc)
            total = len(rc)
            summary[model][b] = {
                'temperature': {
                    'n': len(tc),
                    'mae_c': rounded(mean([abs(e) for e in errs]), 2),
                    'bias_c': rounded(mean(errs), 2)
                },
                'rain': {
                    'n': total, 'hits': hits, 'false_alarms': fa, 'misses': misses, 'correct_dry': dry,
                    'accuracy': rounded((hits+dry)/total, 3) if total else None,
                    'pod': rounded(hits/(hits+misses), 3) if hits+misses else None,
                    'false_alarm_ratio': rounded(fa/(hits+fa), 3) if hits+fa else None
                }
            }

    recent_rain = sorted(rain_cases, key=lambda x: (x['target_at_utc'], x['issued_at_utc']), reverse=True)[:80]
    recent_temp = sorted(temp_cases, key=lambda x: (x['target_at_utc'], x['issued_at_utc']), reverse=True)[:40]
    out = {
        'schema': 1,
        'generated_at_utc': now.isoformat().replace('+00:00','Z'),
        'method': {
            'temperature_truth': 'nearest sufficiently fresh DWD hourly air-temperature observation; not a sensor in Nové Hraběcí',
            'rain_truth_priority': 'qualitative RainViewer signal within 7 km of NH when archived; otherwise nearest fresh DWD hourly rain observation',
            'rain_forecast_threshold_mm': RAIN_FORECAST_THRESHOLD_MM,
            'note': 'Radar is used only for rain/no-rain verification, not as gauge millimetres. Model weights are not changed automatically.'
        },
        'coverage': {
            'forecast_snapshots': len(snapshots), 'dwd_temperature_observations': len(temp_obs),
            'dwd_rain_observations': len(rain_obs), 'radar_observations': len(radar),
            'temperature_cases': len(temp_cases), 'rain_cases': len(rain_cases)
        },
        'summary': summary,
        'recent_rain_cases': recent_rain,
        'recent_temperature_cases': recent_temp
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(out['coverage'], ensure_ascii=False))


if __name__ == '__main__':
    main()
