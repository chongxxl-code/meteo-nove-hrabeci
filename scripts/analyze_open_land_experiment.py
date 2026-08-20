#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
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
ANALYSIS_VERSION = 2


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
    pairs = [
        (float(x), float(y)) for x, y in zip(xs, ys)
        if x is not None and y is not None
        and math.isfinite(float(x)) and math.isfinite(float(y))
    ]
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
            'label': p.get('review_label') or p.get('display_label'),
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

    def role_median(role: str, index_name: str):
        return med(values(role, index_name))

    def contrast(a: float | None, b: float | None):
        return None if a is None or b is None else round(a - b, 4)

    roles = [
        'high_twi', 'low_twi',
        'low_position', 'high_position',
        'south_facing_slope', 'north_facing_slope',
    ]
    role_stats = {}
    for role in roles:
        ndmi_vals = values(role, 'ndmi')
        ndvi_vals = values(role, 'ndvi')
        role_stats[role] = {
            'valid_samples': len(ndmi_vals),
            'ndmi_median': med(ndmi_vals),
            'ndvi_median': med(ndvi_vals),
        }

    groups = {
        'high_twi_minus_low_twi_ndmi': contrast(role_stats['high_twi']['ndmi_median'], role_stats['low_twi']['ndmi_median']),
        'high_twi_minus_low_twi_ndvi': contrast(role_stats['high_twi']['ndvi_median'], role_stats['low_twi']['ndvi_median']),
        'low_minus_high_position_ndmi': contrast(role_stats['low_position']['ndmi_median'], role_stats['high_position']['ndmi_median']),
        'low_minus_high_position_ndvi': contrast(role_stats['low_position']['ndvi_median'], role_stats['high_position']['ndvi_median']),
        'south_minus_north_ndmi': contrast(role_stats['south_facing_slope']['ndmi_median'], role_stats['north_facing_slope']['ndmi_median']),
        'south_minus_north_ndvi': contrast(role_stats['south_facing_slope']['ndvi_median'], role_stats['north_facing_slope']['ndvi_median']),
    }

    return {
        'scene_id': item.get('id'),
        'scene_datetime': (item.get('properties') or {}).get('datetime'),
        'tile_cloud_cover_percent': (item.get('properties') or {}).get('eo:cloud_cover'),
        'rain_7d': rain.get('rain_7d'),
        'rain_14d': rain.get('rain_14d'),
        'rain_30d': rain.get('rain_30d'),
        'zones': results,
        'role_stats': role_stats,
        'groups': groups,
        'quality_status': 'valid' if all(z.get('ok') for z in results) else 'partial',
    }


def contrast_summary(records: list[dict], key: str, positive_label: str) -> dict:
    vals = [(r.get('groups') or {}).get(key) for r in records]
    valid = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    positive = sum(1 for v in valid if v > 0)
    out = {
        'scene_count': len(valid),
        'positive_count': positive,
        'positive_fraction': round(positive / len(valid), 3) if valid else None,
        'median_contrast': round(float(median(valid)), 4) if valid else None,
        'positive_means': positive_label,
    }
    for days in (7, 14, 30):
        rain_values = [((r.get(f'rain_{days}d') or {}).get('precipitation_mm')) for r in records]
        out[f'corr_rain_{days}d_vs_contrast'] = corr(rain_values, vals)
    return out


def main():
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))
    props = zones.get('properties', {})
    network_status = str(props.get('status') or '')
    if not network_status.startswith('stable_'):
        raise RuntimeError(f'Experimental open-land network is not stable/finalized: {network_status!r}')

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

    summary = {
        'scene_count': len(records),
        'fully_valid_scene_count': sum(1 for r in records if r.get('quality_status') == 'valid'),
        'sample_count': len(zones.get('features') or []),
        'role_counts': props.get('role_counts') or {},
        'twi_ndmi': contrast_summary(records, 'high_twi_minus_low_twi_ndmi', 'high-TWI group has higher NDMI'),
        'position_ndmi': contrast_summary(records, 'low_minus_high_position_ndmi', 'low-position group has higher NDMI'),
        'aspect_ndmi': contrast_summary(records, 'south_minus_north_ndmi', 'south-facing group has higher NDMI'),
        'twi_ndvi': contrast_summary(records, 'high_twi_minus_low_twi_ndvi', 'high-TWI group has higher NDVI'),
        'position_ndvi': contrast_summary(records, 'low_minus_high_position_ndvi', 'low-position group has higher NDVI'),
        'aspect_ndvi': contrast_summary(records, 'south_minus_north_ndvi', 'south-facing group has higher NDVI'),
    }

    out = {
        'ok': bool(records),
        'quality_status': 'valid_exploratory_series_v2' if records and not errors else 'partial_exploratory_series_v2',
        'analysis_version': ANALYSIS_VERSION,
        'computed_at_local': datetime.now(TZ).isoformat(),
        'network': zones.get('name'),
        'network_status': network_status,
        'network_review_versions': props.get('review_versions') or props.get('review_version'),
        'provider': 'Sentinel-2 Collection 1 L2A via Earth Search / AWS Open Data COG + ČHMÚ antecedent rainfall',
        'interpretation_warning': 'NDMI is a spectral vegetation/water-content signal, not direct soil-moisture measurement. Group contrasts are exploratory; land management, phenology and sample imbalance can confound them. Correlations do not establish causality.',
        'summary': summary,
        'scenes': records,
        'errors': errors,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    status = {
        'ok': bool(records),
        'quality_status': out['quality_status'],
        'analysis_version': ANALYSIS_VERSION,
        'computed_at_local': out['computed_at_local'],
        'network': out['network'],
        'network_status': network_status,
        'scene_count': len(records),
        'failed_scene_count': len(errors),
        'summary': summary,
        'output_file': str(OUT_FILE.relative_to(ROOT)).replace('\\', '/'),
        'next_step': 'Inspect replicated TWI, relative-position and aspect contrasts across the seasonal series; treat north-facing inference cautiously because only one north sample is available.'
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
