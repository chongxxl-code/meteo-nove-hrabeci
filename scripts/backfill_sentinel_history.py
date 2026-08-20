#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analyze_sentinel_zones import (
    ROOT,
    ZONES_FILE,
    DATA_DIR,
    EARTH_SEARCH,
    BBOX,
    post_json,
    asset_info,
    index_for_zone,
)

TZ = ZoneInfo('Europe/Prague')
COLLECTION = 'sentinel-2-c1-l2a'
MAX_TILE_CLOUD = 35.0
BIN_DAYS = 14
HISTORY_VERSION = 3


def seasonal_start(now: datetime) -> datetime:
    april = datetime(now.year, 4, 1, tzinfo=timezone.utc)
    if now >= april:
        return april
    return datetime(now.year, 1, 1, tzinfo=timezone.utc)


def search_scenes(start: datetime, end: datetime) -> list[dict]:
    payload = {
        'collections': [COLLECTION],
        'bbox': BBOX,
        'datetime': f'{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}',
        'limit': 100,
        'query': {'eo:cloud_cover': {'lte': MAX_TILE_CLOUD}},
        'sortby': [{'field': 'properties.datetime', 'direction': 'asc'}],
    }
    result = post_json(EARTH_SEARCH, payload)
    out = []
    for item in result.get('features') or []:
        assets = item.get('assets') or {}
        if not all(k in assets for k in ('red', 'nir', 'swir16')):
            continue
        dt_txt = (item.get('properties') or {}).get('datetime')
        if not dt_txt:
            continue
        try:
            dt = datetime.fromisoformat(dt_txt.replace('Z', '+00:00')).astimezone(timezone.utc)
        except Exception:
            continue
        item['_dt'] = dt
        out.append(item)
    return out


def choose_bin_scenes(items: list[dict], start: datetime) -> list[dict]:
    bins: dict[int, list[dict]] = defaultdict(list)
    for item in items:
        idx = int((item['_dt'] - start).total_seconds() // (BIN_DAYS * 86400))
        bins[idx].append(item)

    selected = []
    for idx in sorted(bins):
        candidates = bins[idx]
        candidates.sort(
            key=lambda x: (
                float((x.get('properties') or {}).get('eo:cloud_cover') or 999.0),
                abs(((x['_dt'] - start).days % BIN_DAYS) - BIN_DAYS / 2),
            )
        )
        selected.append(candidates[0])
    return selected


def analyze_item(item: dict, zones: dict) -> dict:
    props = item.get('properties') or {}
    assets = item.get('assets') or {}
    red = asset_info(item, 'red')
    nir = asset_info(item, 'nir')
    swir = asset_info(item, 'swir16')
    ndmi_nir_key = 'nir08' if 'nir08' in assets else 'nir'
    ndmi_nir = asset_info(item, ndmi_nir_key)
    scl = assets.get('scl') if (assets.get('scl') or {}).get('href') else None

    results = []
    for feature in zones.get('features') or []:
        geom = feature.get('geometry') or {}
        if geom.get('type') not in ('Polygon', 'MultiPolygon'):
            continue
        p = feature.get('properties') or {}
        z = {
            'id': p.get('id'),
            'name': p.get('name'),
            'class': p.get('class'),
            'zone_status': p.get('status'),
        }
        try:
            z['ndvi'] = index_for_zone(nir, red, geom, scl)
            z['ndmi'] = index_for_zone(ndmi_nir, swir, geom, scl)
            z['ok'] = bool(
                z['ndvi'].get('valid_pixels', 0) > 0
                and z['ndmi'].get('valid_pixels', 0) > 0
                and z['ndvi'].get('valid_fraction', 0) >= 0.35
                and z['ndmi'].get('valid_fraction', 0) >= 0.35
            )
        except Exception as exc:
            z['ok'] = False
            z['error'] = str(exc)
        results.append(z)

    core = [z for z in results if z.get('class') in ('open_land', 'forest')]
    core_ok = all(z.get('ok') for z in core) if core else False
    return {
        'history_version': HISTORY_VERSION,
        'analysis_version': 2,
        'quality_status': 'valid' if core_ok else 'partial',
        'provider': 'Sentinel-2 Collection 1 L2A via Earth Search / AWS Open Data COG',
        'collection': COLLECTION,
        'scene_id': item.get('id'),
        'scene_datetime': props.get('datetime'),
        'tile_cloud_cover_percent': props.get('eo:cloud_cover'),
        'platform': props.get('platform'),
        'zones_version': zones.get('name'),
        'ndvi_formula': '(NIR B08 - Red B04) / (NIR B08 + Red B04)',
        'ndmi_formula': f'({ndmi_nir_key} - SWIR16 B11) / ({ndmi_nir_key} + SWIR16 B11)',
        'cloud_mask': 'SCL classes 0,1,3,8,9,10,11 excluded when available',
        'zones': results,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    start = seasonal_start(now)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))

    items = search_scenes(start, now)
    selected = choose_bin_scenes(items, start)
    if not selected:
        raise RuntimeError('No Sentinel-2 Collection 1 L2A scenes selected for seasonal history')

    records = []
    errors = []
    for i, item in enumerate(selected, start=1):
        scene_id = item.get('id') or f'scene-{i}'
        print(f'[{i}/{len(selected)}] {scene_id}', flush=True)
        try:
            rec = analyze_item(item, zones)
            records.append(rec)
        except Exception as exc:
            errors.append({'scene_id': scene_id, 'error': str(exc)})

    history_file = DATA_DIR / f'history-v3-{now.year}.jsonl'
    with history_file.open('w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')

    status = {
        'ok': bool(records),
        'quality_status': 'valid_history' if records and not errors else 'partial_history',
        'history_version': HISTORY_VERSION,
        'computed_at_local': now.astimezone(TZ).isoformat(),
        'period_start': start.date().isoformat(),
        'period_end': now.date().isoformat(),
        'bin_days': BIN_DAYS,
        'max_tile_cloud_percent': MAX_TILE_CLOUD,
        'candidate_scenes': len(items),
        'selected_scenes': len(selected),
        'successful_scenes': len(records),
        'failed_scenes': len(errors),
        'zones_version': zones.get('name'),
        'history_file': str(history_file.relative_to(ROOT)).replace('\\', '/'),
        'errors': errors[:20],
        'next_step': 'Join each acquisition with antecedent rainfall and compare repeated NDMI/NDVI response against terrain predisposition classes.',
    }
    (DATA_DIR / 'history-status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
