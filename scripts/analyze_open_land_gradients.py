#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
NETWORK_FILE = ROOT / 'config' / 'open-land-experimental.geojson'
EXPERIMENT_FILE = ROOT / 'data' / 'validation' / 'open-land-experiment-2026.json'
OUT_FILE = ROOT / 'data' / 'validation' / 'open-land-gradient-2026.json'
STATUS_FILE = ROOT / 'data' / 'validation' / 'open-land-gradient-status.json'
TZ = ZoneInfo('Europe/Prague')
ANALYSIS_VERSION = 1


def finite(v):
    try:
        return math.isfinite(float(v))
    except Exception:
        return False


def pearson(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if finite(x) and finite(y)]
    if len(pairs) < 4:
        return None
    x = np.array([p[0] for p in pairs], dtype='float64')
    y = np.array([p[1] for p in pairs], dtype='float64')
    if np.std(x) <= 1e-12 or np.std(y) <= 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def ranks(a):
    a = np.asarray(a, dtype='float64')
    order = np.argsort(a, kind='mergesort')
    r = np.empty(len(a), dtype='float64')
    i = 0
    while i < len(a):
        j = i + 1
        while j < len(a) and a[order[j]] == a[order[i]]:
            j += 1
        rank = (i + j - 1) / 2.0 + 1.0
        r[order[i:j]] = rank
        i = j
    return r


def spearman(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if finite(x) and finite(y)]
    if len(pairs) < 4:
        return None
    x = np.array([p[0] for p in pairs], dtype='float64')
    y = np.array([p[1] for p in pairs], dtype='float64')
    return pearson(ranks(x), ranks(y))


def med(vals):
    v = [float(x) for x in vals if finite(x)]
    return float(median(v)) if v else None


def rounded(v, n=4):
    return round(float(v), n) if finite(v) else None


def terrain_by_id(network):
    out = {}
    for f in network.get('features') or []:
        p = f.get('properties') or {}
        aspect = p.get('dominant_aspect_deg')
        coherence = p.get('aspect_coherence')
        slope = p.get('slope_deg')
        southness = None
        if finite(aspect) and finite(coherence) and finite(slope) and float(coherence) >= 0.65 and float(slope) >= 4.0:
            southness = math.cos(math.radians(float(aspect) - 180.0))
        out[p.get('id')] = {
            'label': p.get('review_label') or p.get('display_label'),
            'role': p.get('selection_role'),
            'twi': p.get('twi'),
            'tpi900': p.get('tpi_900m_m'),
            'elevation': p.get('elevation_m_bpv'),
            'slope': p.get('slope_deg'),
            'southness': southness,
            'aspect_deg': aspect,
            'aspect_coherence': coherence,
        }
    return out


def scene_metric(scene, terrain, predictor, index_name):
    xs, ys, labels = [], [], []
    for z in scene.get('zones') or []:
        if not z.get('ok'):
            continue
        t = terrain.get(z.get('id')) or {}
        x = t.get(predictor)
        y = (z.get(index_name) or {}).get('median')
        if not finite(x) or not finite(y):
            continue
        xs.append(float(x)); ys.append(float(y)); labels.append(t.get('label') or z.get('id'))
    return {
        'n': len(xs),
        'pearson_r': rounded(pearson(xs, ys)),
        'spearman_rho': rounded(spearman(xs, ys)),
        'x_min': rounded(min(xs), 3) if xs else None,
        'x_max': rounded(max(xs), 3) if xs else None,
        'labels': labels,
    }


def rain_mm(scene, days):
    d = scene.get(f'rain_{days}d') or {}
    return d.get('precipitation_mm') if d.get('quality') == 'valid' else None


def predictor_summary(scene_rows, predictor, index_name, expected_sign):
    vals = []
    rain = {7: [], 14: [], 30: []}
    for row in scene_rows:
        m = ((row.get(index_name) or {}).get(predictor) or {})
        r = m.get('pearson_r')
        if not finite(r):
            continue
        vals.append(float(r))
        for days in rain:
            rain[days].append((rain_mm(row, days), float(r)))
    if expected_sign > 0:
        aligned = sum(1 for v in vals if v > 0)
    elif expected_sign < 0:
        aligned = sum(1 for v in vals if v < 0)
    else:
        aligned = None
    out = {
        'scene_count': len(vals),
        'median_pearson_r': rounded(med(vals)),
        'positive_fraction': rounded(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None,
        'expected_direction': expected_sign,
        'expected_direction_fraction': rounded(aligned / len(vals), 3) if vals and aligned is not None else None,
    }
    for days, pairs in rain.items():
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        out[f'corr_rain_{days}d_vs_scene_pearson'] = rounded(pearson(xs, ys))
    return out


def main():
    network = json.loads(NETWORK_FILE.read_text(encoding='utf-8'))
    exp = json.loads(EXPERIMENT_FILE.read_text(encoding='utf-8'))
    if not exp.get('ok') or int(exp.get('analysis_version') or 0) < 2:
        raise RuntimeError('Expanded open-land experiment v2 is not ready.')
    terrain = terrain_by_id(network)
    predictors = ['twi', 'tpi900', 'elevation', 'slope', 'southness']
    expected = {
        'twi': 1,
        'tpi900': -1,
        'elevation': 0,
        'slope': -1,
        'southness': -1,
    }

    rows = []
    for scene in exp.get('scenes') or []:
        row = {
            'scene_id': scene.get('scene_id'),
            'scene_datetime': scene.get('scene_datetime'),
            'rain_7d': scene.get('rain_7d'),
            'rain_14d': scene.get('rain_14d'),
            'rain_30d': scene.get('rain_30d'),
            'ndmi': {},
            'ndvi': {},
        }
        for idx in ('ndmi', 'ndvi'):
            for predictor in predictors:
                row[idx][predictor] = scene_metric(scene, terrain, predictor, idx)
        rows.append(row)

    summary = {'ndmi': {}, 'ndvi': {}}
    for idx in ('ndmi', 'ndvi'):
        for predictor in predictors:
            summary[idx][predictor] = predictor_summary(rows, predictor, idx, expected[predictor])

    out = {
        'ok': True,
        'quality_status': 'valid_within_scene_gradient_analysis',
        'analysis_version': ANALYSIS_VERSION,
        'computed_at_local': datetime.now(TZ).isoformat(),
        'network': network.get('name'),
        'sample_count': len(terrain),
        'scene_count': len(rows),
        'method': 'For each Sentinel acquisition independently, correlate cross-sectional NDMI/NDVI across reviewed grassland samples with static terrain predictors. This removes the common seasonal/weather level for that date. Then summarize the per-scene correlations and explore whether their strength changes with antecedent rainfall.',
        'interpretation_warning': 'Exploratory associations only. Predictors are correlated with one another and management/soil differences remain possible confounders. Southness uses only samples with slope >=4° and aspect coherence >=0.65.',
        'predictors': {
            'twi': 'Topographic Wetness Index; expected NDMI direction positive.',
            'tpi900': 'TPI at 900 m; positive=ridge, negative=low position; expected NDMI direction negative.',
            'elevation': 'Elevation Bpv; no a priori sign assigned.',
            'slope': 'Slope degrees; weak drying expectation negative.',
            'southness': 'cos(aspect-180°): +1 south, -1 north; expected NDMI direction negative.',
        },
        'summary': summary,
        'scenes': rows,
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    status = {
        'ok': True,
        'quality_status': out['quality_status'],
        'analysis_version': ANALYSIS_VERSION,
        'computed_at_local': out['computed_at_local'],
        'network': out['network'],
        'sample_count': out['sample_count'],
        'scene_count': out['scene_count'],
        'summary': summary,
        'output_file': str(OUT_FILE.relative_to(ROOT)).replace('\\','/'),
        'next_step': 'Use repeated within-scene terrain gradients to decide which relationships deserve stronger hypotheses; add more north-facing samples if aspect remains promising.'
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
