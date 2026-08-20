#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np

from analyze_sentinel_zones import ROOT, EARTH_SEARCH, post_json, asset_info, index_for_zone

TZ = ZoneInfo('Europe/Prague')
COLLECTION = 'sentinel-2-c1-l2a'
ZONES_FILE = ROOT / 'config' / 'open-land-experimental.geojson'
RAIN_FILE = ROOT / 'data' / 'validation' / 'sentinel-rain-context-2026.json'
OUT_FILE = ROOT / 'data' / 'validation' / 'open-land-experiment-2026.json'
STATUS_FILE = ROOT / 'data' / 'validation' / 'open-land-experiment-status.json'


def lookup_scene(scene_id: str) -> dict:
    result = post_json(EARTH_SEARCH, {
        'collections': [COLLECTION],
        'ids': [scene_id],
        'limit': 1,
    })
    features = result.get('features') or []
    if not features:
        raise RuntimeError(f'Sentinel scene not found: {scene_id}')
    return features[0]


def med(values):
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return float(median(vals)) if vals else None


def corr(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None and math.isfinite(float(x)) and math.isfinite(float(y))]
    if len(pairs) < 4:
        return None
    a = np.array([p[0] for p in pairs], dtype='float64')
    b = np.array([p[1] for p in pairs], dtype='float64')
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 4)


def analyze_scene(item: dict, zones: dict, rain: dict) -> dict:
    assets = item.get('assets') or {}
    red = asset_info(item, 'red')
    nir = asset_info(item, 'nir')
    swir = asset_info(item, 'swir16')
    ndmi_nir_key = 'nir08' if 'nir08' in assets else 'nir'
    ndmi_nir = asset_info(item, ndmi_nir_key)
    scl = assets.get('scl') if (assets.get('scl') or {}).get('href') else None

    results = []
    for feature in zones.get('features') or []:
        p = feature.get('properties') or {}
        geom = feature.get('geometry') or {}
        z = {
            'id': p.get('id'),
            'label': p.get('review_label'),
            'role': p.get('selection_role'),
            'name': p.get('name'),
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

    def values(role: str, index_name: str):
        out = []
        for z in results:
            if z.get('role') != role or not z.get('ok'):
                continue
            value = (z.get(index_name) or {}).get('median')
            if value is not None:
                out.append(value)
        return out

    high_twi_ndmi = med(values('high_twi', 'ndmi'))
    low_twi_ndmi = med(values('low_twi', 'ndmi'))
    high_twi_ndvi = med(values('high_twi', 'ndvi'))
    low_twi_ndvi = med(values('low_twi', 'ndvi'))
    high_pos_ndmi = med(values('high_position', 'ndmi'))

    return {
        'scene_id': item.get('id'),
        'scene_datetime': (item.get('properties') or {}).get('datetime'),
        'tile_cloud_cover_percent': (item.get('properties') or {}).get('eo:cloud_cover'),
        'rain_7d': rain.get('rain_7d'),
        'rain_14d': rain.get('rain_14d'),
        'rain_30d': rain.get('rain_30d'),
        'zones': results,
        'groups': {
            'high_twi_ndmi_median': high_twi_ndmi,
            'low_twi_ndmi_median': low_twi_ndmi,
            'high_twi_ndvi_median': high_twi_ndvi,
            'low_twi_ndvi_median': low_twi_ndvi,
            'high_position_ndmi_median': high_pos_ndmi,
            'high_twi_minus_low_twi_ndmi': None if high_twi_ndmi is None or low_twi_ndmi is None else round(high_twi_ndmi - low_twi_ndmi, 4),
            'high_twi_minus_low_twi_ndvi': None if high_twi_ndvi is None or low_twi_ndvi is None else round(high_twi_ndvi - low_twi_ndvi, 4),
        },
        'quality_status': 'valid' if all(z.get('ok') for z in results) else 'partial',
    }


def main():
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))
    if zones.get('properties', {}).get('status') != 'stable_after_second_visual_check':
        raise RuntimeError('Experimental open-land network is not stable/finalized.')
    rain_data = json.loads(RAIN_FILE.read_text(encoding='utf-8'))
    rain_scenes = rain_data.get('scenes') or []
    if not rain_scenes:
        raise RuntimeError('No Sentinel rainfall scene context found.')

    records = []
    errors = []
    for i, rain in enumerate(rain_scenes, start=1):
        scene_id = rain.get('scene_id')
        print(f'[{i}/{len(rain_scenes)}] {scene_id}', flush=True)
        try:
            item = lookup_scene(scene_id)
            records.append(analyze_scene(item, zones, rain))
        except Exception as exc:
            errors.append({'scene_id': scene_id, 'error': str(exc)})

    contrasts = [r['groups'].get('high_twi_minus_low_twi_ndmi') for r in records]
    contrast_valid = [v for v in contrasts if v is not None]
    positive = sum(1 for v in contrast_valid if v > 0)

    summary = {
        'scene_count': len(records),
        'valid_scene_count': sum(1 for r in records if r.get('quality_status') == 'valid'),
        'twi_contrast_scene_count': len(contrast_valid),
        'high_twi_wetter_ndmi_count': positive,
        'high_twi_wetter_ndmi_fraction': round(positive / len(contrast_valid), 3) if contrast_valid else None,
        'median_high_twi_minus_low_twi_ndmi': round(float(median(contrast_valid)), 4) if contrast_valid else None,
    }

    for days in (7, 14, 30):
        rain_values = [((r.get(f'rain_{days}d') or {}).get('precipitation_mm')) for r in records]
        summary[f'corr_rain_{days}d_vs_twi_ndmi_contrast'] = corr(rain_values, contrasts)

    out = {
        'ok': bool(records),
        'quality_status': 'valid_exploratory_series' if records and not errors else 'partial_exploratory_series',
        'computed_at_local': datetime.now(TZ).isoformat(),
        'network': zones.get('name'),
        'network_review_version': zones.get('properties', {}).get('review_version'),
        'provider': 'Sentinel-2 Collection 1 L2A via Earth Search / AWS Open Data COG + ČHMÚ antecedent rainfall',
        'interpretation_warning': 'NDMI is a spectral moisture signal, not direct soil-moisture measurement. Correlations are exploratory and do not establish causality.',
        'summary': summary,
        'scenes': records,
        'errors': errors,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    status = {
        'ok': bool(records),
        'quality_status': out['quality_status'],
        'computed_at_local': out['computed_at_local'],
        'network': out['network'],
        'scene_count': len(records),
        'failed_scene_count': len(errors),
        'summary': summary,
        'output_file': str(OUT_FILE.relative_to(ROOT)).replace('\\', '/'),
        'next_step': 'Inspect per-scene TWI+ versus TWI- NDMI response, then add replacement low-position and south-facing samples before broader terrain-effect inference.'
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
