#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from collect_chmi import get_json, extract_weather, PREFERRED_RAIN_WSI

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
SENTINEL_DIR = ROOT / 'data' / 'sentinel'
VALIDATION_DIR = ROOT / 'data' / 'validation'
BASE = 'https://opendata.chmi.cz/meteorology/climate'
MIN_COVERAGE = 0.85
PREFERRED_WSI = PREFERRED_RAIN_WSI


def extract_sra10m(payload) -> list[dict]:
    points, _ = extract_weather(payload, ['SRA10M'])
    return [
        {
            'observed_at_utc': p['observed_at_utc'],
            'precipitation_10m_mm': p['precipitation_10m_mm'],
        }
        for p in points
        if p.get('precipitation_10m_mm') is not None
    ]


def load_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def month_iter(d0: date, d1: date):
    cur = date(d0.year, d0.month, 1)
    while cur <= d1:
        yield cur.year, cur.month
        cur = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


def fetch_month(year: int, month: int, now: datetime) -> tuple[list[dict], list[str]]:
    errors = []
    points: list[dict] = []
    current_month = (year, month) == (now.year, now.month)

    if not current_month:
        urls = [
            f'{BASE}/recent/data/10min/{month:02d}/10m-{PREFERRED_WSI}-{year}{month:02d}.json',
        ]
        if year < now.year:
            urls.append(f'{BASE}/historical/data/10min/{month:02d}/10m-{PREFERRED_WSI}-{year}{month:02d}.json')
        for url in urls:
            try:
                return extract_sra10m(get_json(url)), errors
            except Exception as exc:
                errors.append(f'{url}: {exc}')

    # Current month, or fallback if the monthly recent file is not available.
    first = date(year, month, 1)
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last = min(now.date(), next_month - timedelta(days=1))
    d = first
    while d <= last:
        urls = [
            f'{BASE}/recent/data/10min/10m-{PREFERRED_WSI}-{d:%Y%m%d}.json',
            f'{BASE}/now/data/10m-{PREFERRED_WSI}-{d:%Y%m%d}.json',
        ]
        got = False
        last_error = 'no URL attempted'
        for url in urls:
            try:
                points.extend(extract_sra10m(get_json(url, tries=1)))
                got = True
                break
            except Exception as exc:
                last_error = f'{url}: {exc}'
        if not got:
            errors.append(last_error)
        d += timedelta(days=1)
    return points, errors


def dt_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)


def window_stats(points: list[dict], scene_dt: datetime, days: int) -> dict:
    start = scene_dt - timedelta(days=days)
    vals = []
    for p in points:
        try:
            t = dt_utc(p['observed_at_utc'])
            if start < t <= scene_dt:
                vals.append(float(p['precipitation_10m_mm']))
        except Exception:
            continue
    expected = days * 24 * 6
    coverage = len(vals) / expected if expected else 0
    total = round(sum(vals), 2) if coverage >= MIN_COVERAGE else None
    return {
        'days': days,
        'precipitation_mm': total,
        'intervals_found': len(vals),
        'intervals_expected': expected,
        'coverage_fraction': round(coverage, 3),
        'quality': 'valid' if coverage >= MIN_COVERAGE else 'incomplete',
    }


def main():
    now = datetime.now(timezone.utc)
    history_file = SENTINEL_DIR / f'history-v3-{now.year}.jsonl'
    records = load_jsonl(history_file)
    if not records:
        raise RuntimeError(f'No Sentinel history records in {history_file}')

    scenes = []
    scene_dts = []
    for rec in records:
        try:
            sd = dt_utc(rec['scene_datetime'])
        except Exception:
            continue
        scenes.append((rec, sd))
        scene_dts.append(sd)
    if not scenes:
        raise RuntimeError('No valid Sentinel scene datetimes')

    d0 = (min(scene_dts) - timedelta(days=30)).date()
    d1 = max(scene_dts).date()
    all_points = []
    fetch_errors = []
    month_summaries = []
    for y, m in month_iter(d0, d1):
        pts, errs = fetch_month(y, m, now)
        all_points.extend(pts)
        fetch_errors.extend(errs)
        month_summaries.append({'year': y, 'month': m, 'points': len(pts), 'errors': len(errs)})

    # Deduplicate exact timestamps, preferring the last retrieved copy.
    unique = {}
    for p in all_points:
        if p.get('observed_at_utc'):
            unique[p['observed_at_utc']] = p
    all_points = [unique[k] for k in sorted(unique)]

    out_scenes = []
    valid_windows = 0
    for rec, sd in scenes:
        w7 = window_stats(all_points, sd, 7)
        w14 = window_stats(all_points, sd, 14)
        w30 = window_stats(all_points, sd, 30)
        valid_windows += sum(w['quality'] == 'valid' for w in (w7, w14, w30))
        out_scenes.append({
            'scene_id': rec.get('scene_id'),
            'scene_datetime': rec.get('scene_datetime'),
            'tile_cloud_cover_percent': rec.get('tile_cloud_cover_percent'),
            'rain_7d': w7,
            'rain_14d': w14,
            'rain_30d': w30,
        })

    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        'ok': True,
        'quality_status': 'valid' if valid_windows == len(out_scenes) * 3 else 'partial',
        'provider': 'ČHMÚ OpenData, station Šluknov',
        'station_wsi': PREFERRED_WSI,
        'computed_at_local': now.astimezone(TZ).isoformat(),
        'sentinel_history_file': str(history_file.relative_to(ROOT)).replace('\\', '/'),
        'scene_count': len(out_scenes),
        'rain_intervals_loaded': len(all_points),
        'minimum_window_coverage': MIN_COVERAGE,
        'month_fetch': month_summaries,
        'fetch_error_count': len(fetch_errors),
        'fetch_errors_sample': fetch_errors[:20],
        'scenes': out_scenes,
        'interpretation': 'Antecedent precipitation context for Sentinel acquisitions; totals are accepted only when at least 85% of expected 10-minute intervals are present.',
    }
    (VALIDATION_DIR / f'sentinel-rain-context-{now.year}.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
