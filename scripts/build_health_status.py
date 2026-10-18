#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
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


def jsonl_timestamps(path_value, timestamp_keys):
    if not path_value:
        return []
    p = ROOT / str(path_value)
    if not p.exists():
        return []
    out = []
    try:
        lines = p.read_text(encoding='utf-8').splitlines()
    except Exception:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        for key in timestamp_keys:
            d = parse_dt(row.get(key))
            if d:
                out.append(d)
                break
    return sorted(set(out))


def indoor_history_timestamps():
    payload = read_json('indoor-history.json') or {}
    out = []
    for row in payload.get('points') or []:
        if not isinstance(row, dict):
            continue
        d = parse_dt(row.get('timestamp_local'))
        if d:
            out.append(d)
    return sorted(set(out))


def recent_coverage(timestamps, now, window_minutes, expected_interval_minutes, min_ratio=0.75, max_internal_gap_minutes=None):
    start = now - timedelta(minutes=window_minutes)
    recent = sorted({d for d in timestamps if start <= d <= now})
    expected = max(1, int(window_minutes // expected_interval_minutes))
    observed = len(recent)
    ratio = round(min(1.0, observed / expected), 3)

    internal_gaps = [
        (b - a).total_seconds() / 60.0
        for a, b in zip(recent, recent[1:])
    ]
    largest_internal_gap = round(max(internal_gaps), 1) if internal_gaps else None
    current_gap = round((now - recent[-1]).total_seconds() / 60.0, 1) if recent else None
    gap_limit = max_internal_gap_minutes or expected_interval_minutes * 2.5
    sufficient = bool(
        ratio >= min_ratio
        and recent
        and (largest_internal_gap is None or largest_internal_gap <= gap_limit)
    )
    return {
        'window_minutes': window_minutes,
        'expected_interval_minutes': expected_interval_minutes,
        'observed_count': observed,
        'expected_count': expected,
        'coverage': ratio,
        'largest_internal_gap_minutes': largest_internal_gap,
        'current_gap_minutes': current_gap,
        'minimum_coverage': min_ratio,
        'maximum_internal_gap_minutes': gap_limit,
        'sufficient': sufficient,
    }


def fixed_coverage(observed, expected, *, min_ratio=0.75, detail=None):
    observed = int(observed or 0)
    expected = int(expected or 0)
    ratio = None if expected <= 0 else round(min(1.0, observed / expected), 3)
    sufficient = bool(expected > 0 and ratio is not None and ratio >= min_ratio)
    return {
        'observed_count': observed,
        'expected_count': expected if expected > 0 else None,
        'coverage': ratio,
        'minimum_coverage': min_ratio,
        'largest_internal_gap_minutes': None,
        'current_gap_minutes': None,
        'sufficient': sufficient,
        'detail': detail,
    }


def classify(*, collector_running, data_fresh, source_ok, coverage_sufficient, check_age, obs_age, warn_min):
    if not collector_running:
        return 'error'
    if source_ok is False:
        return 'error'
    if not data_fresh:
        return 'stale'
    if coverage_sufficient is False:
        return 'delayed'
    if (check_age is not None and check_age > warn_min) or (obs_age is not None and obs_age > warn_min):
        return 'delayed'
    return 'ok'


def source(
    name,
    label,
    checked_at,
    observed_at,
    warn_min,
    stale_min,
    source_ok=True,
    detail=None,
    coverage=None,
    collector_expected_interval_minutes=None,
):
    now = datetime.now(timezone.utc)
    checked_dt = parse_dt(checked_at)
    observed_dt = parse_dt(observed_at)
    check_age = age_minutes(checked_at, now)
    obs_age = age_minutes(observed_at, now) if observed_at else None

    collector_limit = max(
        stale_min,
        (collector_expected_interval_minutes or stale_min) * 2.5,
    )
    collector_running = bool(
        checked_dt
        and check_age is not None
        and check_age <= collector_limit
    )
    data_fresh = bool(
        observed_dt
        and obs_age is not None
        and obs_age <= stale_min
    )
    coverage_sufficient = None if coverage is None else coverage.get('sufficient')
    state = classify(
        collector_running=collector_running,
        data_fresh=data_fresh,
        source_ok=source_ok,
        coverage_sufficient=coverage_sufficient,
        check_age=check_age,
        obs_age=obs_age,
        warn_min=warn_min,
    )
    return {
        'name': name,
        'label': label,
        'status': state,
        'checked_at_utc': None if checked_dt is None else checked_dt.isoformat().replace('+00:00','Z'),
        'check_age_minutes': check_age,
        'observed_at_utc': None if observed_dt is None else observed_dt.isoformat().replace('+00:00','Z'),
        'observation_age_minutes': obs_age,
        'expected_max_gap_minutes': stale_min,
        'collector_running': collector_running,
        'data_fresh': data_fresh,
        'coverage_sufficient': coverage_sufficient,
        'quality': {
            'source': name,
            'last_sample_at_utc': None if observed_dt is None else observed_dt.isoformat().replace('+00:00','Z'),
            'expected_interval_minutes': None if coverage is None else coverage.get('expected_interval_minutes'),
            'observed_count': None if coverage is None else coverage.get('observed_count'),
            'expected_count': None if coverage is None else coverage.get('expected_count'),
            'largest_internal_gap_minutes': None if coverage is None else coverage.get('largest_internal_gap_minutes'),
            'current_gap_minutes': obs_age,
            'stale': not data_fresh,
            'coverage': None if coverage is None else coverage.get('coverage'),
            'window_minutes': None if coverage is None else coverage.get('window_minutes'),
        },
        'detail': detail,
    }


def main():
    now = datetime.now(timezone.utc)
    forecast = read_json('status.json') or {}
    chmi = read_json('chmi-status.json') or {}
    sohland = read_json('dwd-sohland-status.json') or {}
    radar = read_json('radar-nowcast.json') or {}
    verification = read_json('forecast-verification.json') or {}
    indoor = read_json('indoor-latest.json') or {}
    indoor_collector = read_json('indoor-collector-status.json') or {}
    thermal = read_json('indoor-thermal-model.json') or {}
    hdo = read_json('hdo-schedule.json') or {}

    forecast_times = jsonl_timestamps(
        forecast.get('archive_file'),
        ('collected_at_utc', 'collected_at_local'),
    )
    forecast_cov = recent_coverage(
        forecast_times, now, 24 * 60, 180, min_ratio=0.75, max_internal_gap_minutes=420
    )

    dwd_times = jsonl_timestamps(
        sohland.get('archive_file'),
        ('observed_at_utc',),
    )
    dwd_cov = recent_coverage(
        dwd_times, now, 6 * 60, 10, min_ratio=0.75, max_internal_gap_minutes=30
    )

    chmi_times = jsonl_timestamps(
        chmi.get('archive_file'),
        ('observed_at_utc',),
    )
    chmi_cov = recent_coverage(
        chmi_times, now, 6 * 60, 10, min_ratio=0.75, max_internal_gap_minutes=30
    )

    indoor_times = indoor_history_timestamps()
    indoor_cov = recent_coverage(
        indoor_times, now, 6 * 60, 30, min_ratio=0.75, max_internal_gap_minutes=60
    )

    radar_checked = radar.get('computed_at_utc')
    radar_observed = None
    radar_frames = radar.get('frames') or {}
    latest_unix = radar_frames.get('latest_unix')
    if latest_unix:
        try:
            radar_observed = datetime.fromtimestamp(int(latest_unix), timezone.utc).isoformat().replace('+00:00','Z')
        except Exception:
            pass
    radar_frame_times = []
    for item in (radar.get('diagnostics') or {}).get('frame_tiles') or []:
        try:
            radar_frame_times.append(datetime.fromtimestamp(int(item.get('time')), timezone.utc))
        except Exception:
            pass
    radar_expected = 6
    radar_cov = fixed_coverage(
        len(radar_frame_times),
        radar_expected,
        min_ratio=0.8,
        detail='RainViewer recent frames',
    )
    radar_cov['expected_interval_minutes'] = 10
    radar_cov['window_minutes'] = 50
    if len(radar_frame_times) >= 2:
        radar_cov['largest_internal_gap_minutes'] = round(max(
            (b - a).total_seconds() / 60
            for a, b in zip(sorted(radar_frame_times), sorted(radar_frame_times)[1:])
        ), 1)

    ver_cov_raw = verification.get('coverage') or {}
    verification_cov = fixed_coverage(
        min(
            int(ver_cov_raw.get('temperature_cases') or 0),
            int(ver_cov_raw.get('rain_cases') or 0),
        ),
        1,
        min_ratio=1.0,
        detail='At least one matured temperature and rain verification case exists.',
    )
    verification_cov['expected_interval_minutes'] = None
    verification_cov['window_minutes'] = None

    thermal_fit_samples = int(thermal.get('fit_samples') or 0)
    thermal_cov = fixed_coverage(
        thermal_fit_samples,
        10,
        min_ratio=1.0,
        detail='Minimum accepted passive-fit samples for preliminary learning.',
    )
    thermal_cov['expected_interval_minutes'] = 42
    thermal_cov['window_minutes'] = None

    hdo_cov = fixed_coverage(
        len(hdo.get('week_ahead') or []),
        7,
        min_ratio=1.0,
        detail='Seven-day HDO horizon.',
    )
    hdo_cov['expected_interval_minutes'] = 1440
    hdo_cov['window_minutes'] = 7 * 1440

    forecast_models_ok = (
        bool(forecast.get('models'))
        and all((forecast.get('models',{}).get(k) or {}).get('ok') for k in ('dwd','chmi','ec'))
    )
    indoor_source_ok = (
        indoor.get('latest_indoor_c') is not None
        and indoor_collector.get('stage') != 'device_offline'
    )

    sources = [
        source(
            'forecast','Forecasty',
            forecast.get('collected_at_utc'), forecast.get('collected_at_utc'),
            240, 420, forecast_models_ok,
            f"{forecast.get('total_snapshots',0)} snapshotů · 3 modely",
            forecast_cov, 180,
        ),
        source(
            'sohland','Sohland',
            sohland.get('checked_at_utc'), sohland.get('observed_at_utc'),
            120, 240, sohland.get('ok') is True,
            'DWD 10min · teplota/vlhkost',
            dwd_cov, 60,
        ),
        source(
            'chmi','ČHMÚ Šluknov',
            chmi.get('checked_at_local'), chmi.get('observed_at_utc'),
            120, 240, chmi.get('ok') is True,
            '10min srážkoměr',
            chmi_cov, 60,
        ),
        source(
            'radar','Radar nowcast',
            radar_checked, radar_observed,
            30, 90, radar.get('status') not in (None,'error'),
            f"{radar.get('status','neznámý stav')} · {radar_cov.get('observed_count',0)}/{radar_expected} snímků",
            radar_cov, 10,
        ),
        source(
            'verification','Verifikace',
            verification.get('generated_at_utc'), verification.get('generated_at_utc'),
            240, 480, bool(verification),
            f"{ver_cov_raw.get('temperature_cases',0)} T / {ver_cov_raw.get('rain_cases',0)} déšť",
            verification_cov, 180,
        ),
        source(
            'indoor','EMOS uvnitř',
            indoor_collector.get('generated_at') or indoor.get('generated_at'), indoor.get('last_timestamp'),
            20, 45, indoor_source_ok,
            f"{indoor.get('latest_indoor_c','—')} °C · cíl {indoor.get('latest_setpoint_c','—')} °C",
            indoor_cov, 7,
        ),
        source(
            'thermal_model','Tepelný model',
            thermal.get('generated_at'), thermal.get('current_indoor_state',{}).get('observed_at_utc') or indoor.get('last_timestamp'),
            90, 150, bool(thermal),
            f"{thermal_fit_samples} fit oken · prediction {'ano' if thermal.get('prediction_ready') else 'ne'}",
            thermal_cov, 42,
        ),
        source(
            'hdo','HDO ČEZ monitor',
            hdo.get('checked_at') or hdo.get('generated_at'), hdo.get('checked_at') or hdo.get('generated_at'),
            1800, 2880,
            hdo.get('status') in ('verified_export_monitored','live','live_stale','manual_fallback'),
            'ověřený export + denní veřejná kontrola',
            hdo_cov, 1440,
        ),
    ]

    rank = {'ok':0, 'delayed':1, 'stale':2, 'error':3}
    worst = max((s['status'] for s in sources), key=lambda x: rank[x])
    overall = 'ok' if worst == 'ok' else ('degraded' if worst in ('delayed','stale') else 'error')

    thermostat_heat_demand = {
        'status': 'unknown',
        'source': 'EMOS/Tuya cloud datapoints',
        'last_sample_at_utc': indoor.get('last_timestamp'),
        'stale': not next((s['data_fresh'] for s in sources if s['name'] == 'indoor'), False),
        'coverage': None,
        'reason': 'DP2 is thermostat mode, not verified relay/heat-demand state; DP106 is not accepted as a binary relay proof.',
    }

    payload = {
        'schema': 2,
        'generated_at_utc': now.isoformat().replace('+00:00','Z'),
        'overall_status': overall,
        'sources': sources,
        'capabilities': {
            'thermostat_heat_demand': thermostat_heat_demand,
        },
        'policy': {
            'forecast_stale_after_min': 420,
            'regional_observations_stale_after_min': 240,
            'radar_stale_after_min': 90,
            'verification_stale_after_min': 480,
            'indoor_stale_after_min': 45,
            'thermal_model_stale_after_min': 150,
            'hdo_monitor_stale_after_min': 2880,
            'note': 'Collector running, data freshness and coverage sufficiency are independent health dimensions. A fresh check never makes an old or sparse sample stream healthy.'
        }
    }
    DATA.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'overall_status': overall,
        'sources': {
            s['name']: {
                'status': s['status'],
                'collector_running': s['collector_running'],
                'data_fresh': s['data_fresh'],
                'coverage_sufficient': s['coverage_sufficient'],
            }
            for s in sources
        },
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
