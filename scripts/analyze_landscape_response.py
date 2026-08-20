#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = ROOT / 'data' / 'sentinel' / 'history-v3-2026.jsonl'
RAIN = ROOT / 'data' / 'validation' / 'sentinel-rain-context-2026.json'
PRED = ROOT / 'data' / 'terrain' / 'predisposition' / 'layers.json'
OUT = ROOT / 'data' / 'validation' / 'landscape-response-2026.json'

ZONE_IDS = ['NH-OPEN-01', 'NH-FOREST-01', 'NH-RIDGE-01', 'NH-VALLEY-01']


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def zone_value(scene: dict, zid: str, index: str = 'ndmi') -> float | None:
    for z in scene.get('zones') or []:
        if z.get('id') != zid or not z.get('ok'):
            continue
        v = (z.get(index) or {}).get('median')
        return float(v) if isinstance(v, (int, float)) else None
    return None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(x*x for x in dx) * sum(y*y for y in dy))
    if den == 0:
        return None
    return sum(a*b for a, b in zip(dx, dy)) / den


def rankdata(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        v = vals[order[i]]
        while j + 1 < len(order) and vals[order[j + 1]] == v:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return pearson(rankdata(xs), rankdata(ys))


def rounded(v, d=3):
    return round(float(v), d) if v is not None and math.isfinite(v) else None


def main():
    scenes = load_jsonl(SENTINEL)
    rain = json.loads(RAIN.read_text(encoding='utf-8'))
    pred = json.loads(PRED.read_text(encoding='utf-8'))
    rain_by_id = {r['scene_id']: r for r in rain.get('scenes') or []}
    pred_by_id = {z['id']: z for z in pred.get('zones') or []}

    joined = []
    for s in scenes:
        rr = rain_by_id.get(s.get('scene_id'))
        if not rr:
            continue
        row = {
            'scene_id': s.get('scene_id'),
            'scene_datetime': s.get('scene_datetime'),
            'quality_status': s.get('quality_status'),
            'rain_7d_mm': (rr.get('rain_7d') or {}).get('precipitation_mm'),
            'rain_14d_mm': (rr.get('rain_14d') or {}).get('precipitation_mm'),
            'rain_30d_mm': (rr.get('rain_30d') or {}).get('precipitation_mm'),
            'zones': {},
        }
        for zid in ZONE_IDS:
            row['zones'][zid] = {
                'ndmi': zone_value(s, zid, 'ndmi'),
                'ndvi': zone_value(s, zid, 'ndvi'),
            }
        va = row['zones']['NH-VALLEY-01']['ndmi']
        ri = row['zones']['NH-RIDGE-01']['ndmi']
        fo = row['zones']['NH-FOREST-01']['ndmi']
        op = row['zones']['NH-OPEN-01']['ndmi']
        row['valley_minus_ridge_ndmi'] = (va - ri) if va is not None and ri is not None else None
        row['forest_minus_open_ndmi'] = (fo - op) if fo is not None and op is not None else None
        joined.append(row)

    correlations = {}
    for zid in ZONE_IDS:
        correlations[zid] = {}
        for days in (7, 14, 30):
            xs, ys = [], []
            rk = f'rain_{days}d_mm'
            for r in joined:
                x = r.get(rk)
                y = r['zones'][zid]['ndmi']
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    xs.append(float(x)); ys.append(float(y))
            correlations[zid][f'rain_{days}d_vs_ndmi'] = {
                'n': len(xs),
                'pearson_r': rounded(pearson(xs, ys)),
                'spearman_rho': rounded(spearman(xs, ys)),
            }

    vr = [r['valley_minus_ridge_ndmi'] for r in joined if r['valley_minus_ridge_ndmi'] is not None]
    fo = [r['forest_minus_open_ndmi'] for r in joined if r['forest_minus_open_ndmi'] is not None]

    # Compare driest and wettest thirds by 14-day antecedent rain. This is descriptive only.
    complete = [r for r in joined if isinstance(r.get('rain_14d_mm'), (int, float))]
    complete.sort(key=lambda r: r['rain_14d_mm'])
    k = max(1, len(complete) // 3)
    dry = complete[:k]
    wet = complete[-k:]

    def contrast_summary(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return {
            'n': len(vals),
            'median': rounded(statistics.median(vals)) if vals else None,
            'mean': rounded(statistics.mean(vals)) if vals else None,
        }

    payload = {
        'ok': True,
        'quality_status': 'exploratory_not_causal',
        'computed_at': datetime.now().astimezone().isoformat(),
        'scene_count': len(joined),
        'zones_version': scenes[-1].get('zones_version') if scenes else None,
        'method_note': 'Exploratory relationships from 10 seasonal Sentinel scenes. Phenology, land cover and other confounders remain; correlations are not causal validation.',
        'terrain_scores': {
            zid: {
                'wetness_median': (pred_by_id.get(zid, {}).get('wetness') or {}).get('median'),
                'drying_median': (pred_by_id.get(zid, {}).get('drying') or {}).get('median'),
                'cold_pool_median': (pred_by_id.get(zid, {}).get('cold_pool') or {}).get('median'),
            } for zid in ZONE_IDS
        },
        'correlations': correlations,
        'contrasts': {
            'valley_minus_ridge': {
                'valid_scenes': len(vr),
                'positive_scenes': sum(v > 0 for v in vr),
                'positive_fraction': rounded(sum(v > 0 for v in vr) / len(vr)) if vr else None,
                'median_ndmi_difference': rounded(statistics.median(vr)) if vr else None,
                'mean_ndmi_difference': rounded(statistics.mean(vr)) if vr else None,
            },
            'forest_minus_open': {
                'valid_scenes': len(fo),
                'positive_scenes': sum(v > 0 for v in fo),
                'positive_fraction': rounded(sum(v > 0 for v in fo) / len(fo)) if fo else None,
                'median_ndmi_difference': rounded(statistics.median(fo)) if fo else None,
                'mean_ndmi_difference': rounded(statistics.mean(fo)) if fo else None,
            },
            'dry_vs_wet_thirds_by_rain14d': {
                'dry_rain14d_range_mm': [dry[0]['rain_14d_mm'], dry[-1]['rain_14d_mm']] if dry else None,
                'wet_rain14d_range_mm': [wet[0]['rain_14d_mm'], wet[-1]['rain_14d_mm']] if wet else None,
                'dry_valley_minus_ridge': contrast_summary(dry, 'valley_minus_ridge_ndmi'),
                'wet_valley_minus_ridge': contrast_summary(wet, 'valley_minus_ridge_ndmi'),
                'dry_forest_minus_open': contrast_summary(dry, 'forest_minus_open_ndmi'),
                'wet_forest_minus_open': contrast_summary(wet, 'forest_minus_open_ndmi'),
            },
        },
        'scenes': joined,
        'next_step': 'Increase comparable open-land sampling zones and continue the time series; then test terrain wetness/drying scores within the same land-cover class rather than across mixed surfaces.',
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
