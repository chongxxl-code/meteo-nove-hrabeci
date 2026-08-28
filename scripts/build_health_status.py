#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OUT = DATA / 'health-status.json'


def read_json(name: str):
    p = DATA / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def parse_dt(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def age_minutes(value, now):
    d = parse_dt(value)
    if not d:
        return None
    return round(max(0.0, (now - d).total_seconds() / 60.0), 1)


def classify(age, warn_min, stale_min, source_ok=True):
    if source_ok is False:
        return 'error'
    if age is None:
        return 'error'
    if age > stale_min:
        return 'stale'
    if age > warn_min:
        return 'delayed'
    return 'ok'


def source(name, label, checked_at, observed_at, warn_min, stale_min, source_ok=True, detail=None):
    now = datetime.now(timezone.utc)
    check_age = age_minutes(checked_at, now)
    obs_age = age_minutes(observed_at, now) if observed_at else None
    state = classify(check_age, warn_min, stale_min, source_ok)
    return {
        'name': name,
        'label': label,
        'status': state,
        'checked_at_utc': None if not parse_dt(checked_at) else parse_dt(checked_at).isoformat().replace('+00:00','Z'),
        'check_age_minutes': check_age,
        'observed_at_utc': None if not parse_dt(observed_at) else parse_dt(observed_at).isoformat().replace('+00:00','Z'),
        'observation_age_minutes': obs_age,
        'expected_max_gap_minutes': stale_min,
        'detail': detail,
    }


def main():
    now = datetime.now(timezone.utc)
    forecast = read_json('status.json') or {}
    chmi = read_json('chmi-status.json') or {}
    sohland = read_json('dwd-sohland-status.json') or {}
    radar = read_json('radar-nowcast.json') or {}
    verification = read_json('forecast-verification.json') or {}

    radar_checked = radar.get('computed_at_utc')
    radar_observed = None
    latest_unix = ((radar.get('frames') or {}).get('latest_unix'))
    if latest_unix:
        try:
            radar_observed = datetime.fromtimestamp(int(latest_unix), timezone.utc).isoformat().replace('+00:00','Z')
        except Exception:
            pass

    sources = [
        source('forecast','Forecasty', forecast.get('collected_at_utc'), forecast.get('collected_at_utc'), 240, 420,
               bool(forecast.get('models')) and all((forecast.get('models',{}).get(k) or {}).get('ok') for k in ('dwd','chmi','ec')),
               f"{forecast.get('total_snapshots',0)} snapshotů"),
        source('sohland','Sohland', sohland.get('checked_at_utc'), sohland.get('observed_at_utc'), 120, 240,
               sohland.get('ok') is True,
               'DWD 10min · teplota/vlhkost'),
        source('chmi','ČHMÚ Šluknov', chmi.get('checked_at_local'), chmi.get('observed_at_utc'), 120, 240,
               chmi.get('ok') is True,
               '10min srážkoměr'),
        source('radar','Radar nowcast', radar_checked, radar_observed, 30, 90,
               radar.get('status') not in (None,'error'),
               f"{radar.get('status','neznámý stav')}"),
        source('verification','Verifikace', verification.get('generated_at_utc'), verification.get('generated_at_utc'), 240, 480,
               bool(verification),
               f"{(verification.get('coverage') or {}).get('temperature_cases',0)} T / {(verification.get('coverage') or {}).get('rain_cases',0)} déšť"),
    ]

    rank = {'ok':0, 'delayed':1, 'stale':2, 'error':3}
    worst = max((s['status'] for s in sources), key=lambda x: rank[x])
    overall = 'ok' if worst == 'ok' else ('degraded' if worst in ('delayed','stale') else 'error')
    payload = {
        'schema': 1,
        'generated_at_utc': now.isoformat().replace('+00:00','Z'),
        'overall_status': overall,
        'sources': sources,
        'policy': {
            'forecast_stale_after_min': 420,
            'regional_observations_stale_after_min': 240,
            'radar_stale_after_min': 90,
            'verification_stale_after_min': 480,
            'note': 'Health stav sleduje čerstvost posledního úspěšného sběru; delayed je varování, stale znamená příliš stará data.'
        }
    }
    DATA.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'overall_status': overall, 'sources': {s['name']: s['status'] for s in sources}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
