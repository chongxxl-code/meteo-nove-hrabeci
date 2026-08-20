#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data' / 'terrain'
DERIVED = DATA_DIR / 'derived-drainage.geojson'
OFFICIAL = DATA_DIR / 'zabaged-watercourses.geojson'
OUT = DATA_DIR / 'hydrology-validation.json'


def load_lines(path: Path, predicate=None):
    data = json.loads(path.read_text(encoding='utf-8'))
    lines = []
    props = []
    for f in data.get('features') or []:
        p = f.get('properties') or {}
        if predicate is not None and not predicate(p):
            continue
        g = f.get('geometry')
        if not g:
            continue
        geom = shape(g)
        if geom.is_empty:
            continue
        lines.append(geom)
        props.append(p)
    return lines, props


def to_5514(geoms):
    tr = Transformer.from_crs('EPSG:4326', 'EPSG:5514', always_xy=True)
    return [transform(tr.transform, g) for g in geoms]


def total_length(lines):
    return float(sum(g.length for g in lines))


def within_buffer_fraction(lines, reference_union, distance_m: float):
    if not lines or reference_union.is_empty:
        return None
    corridor = reference_union.buffer(distance_m)
    total = total_length(lines)
    if total <= 0:
        return None
    matched = sum(g.intersection(corridor).length for g in lines)
    return {
        'distance_m': distance_m,
        'matched_length_km': round(float(matched) / 1000.0, 3),
        'total_length_km': round(total / 1000.0, 3),
        'fraction': round(float(matched / total), 4),
        'percent': round(float(matched / total * 100.0), 1),
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    if not DERIVED.exists() or not OFFICIAL.exists():
        raise RuntimeError('Derived drainage and official ZABAGED watercourse files must exist first')

    d4326, dprops = load_lines(DERIVED)
    o4326, oprops = load_lines(OFFICIAL)
    surf4326, surfprops = load_lines(
        OFFICIAL,
        lambda p: str(p.get('typtoku_p') or '').startswith('povrchový'),
    )
    underground_count = sum(1 for p in oprops if str(p.get('typtoku_p') or '').startswith('podzemní'))

    derived = to_5514(d4326)
    official = to_5514(o4326)
    surface = to_5514(surf4326)
    official_union = unary_union(official)
    surface_union = unary_union(surface)
    derived_union = unary_union(derived)

    derived_vs_surface = [within_buffer_fraction(derived, surface_union, d) for d in (25.0, 50.0, 100.0)]
    derived_vs_all = [within_buffer_fraction(derived, official_union, d) for d in (25.0, 50.0, 100.0)]
    official_vs_derived = [within_buffer_fraction(surface, derived_union, d) for d in (25.0, 50.0, 100.0)]

    # Segment-level diagnostic: fraction of each D8 line within 50 m of official surface water.
    corridor50 = surface_union.buffer(50.0)
    diagnostics = []
    for geom, p in zip(derived, dprops):
        length = float(geom.length)
        matched = float(geom.intersection(corridor50).length) if length > 0 else 0.0
        diagnostics.append({
            'upstream_km2': round(float(p.get('upstream_km2') or 0.0), 3),
            'length_m': round(length, 1),
            'matched_within_50m_fraction': round(matched / length, 4) if length > 0 else None,
        })
    diagnostics.sort(key=lambda x: ((x['matched_within_50m_fraction'] if x['matched_within_50m_fraction'] is not None else 1.0), -x['length_m']))

    result = {
        'ok': True,
        'quality_status': 'validation_reference_available',
        'computed_at_local': now.isoformat(),
        'derived_source': 'ČÚZK DMR 4G 5 m + pyflwdir D8, upstream threshold 0.25 km²',
        'reference_source': 'ČÚZK ZABAGED® polohopis, Vodní tok (layer 93)',
        'crs_for_distance': 'EPSG:5514',
        'counts': {
            'derived_segments': len(derived),
            'official_watercourse_features': len(official),
            'official_surface_features': len(surface),
            'official_underground_features': underground_count,
        },
        'lengths_km': {
            'derived_drainage': round(total_length(derived) / 1000.0, 3),
            'official_all': round(total_length(official) / 1000.0, 3),
            'official_surface': round(total_length(surface) / 1000.0, 3),
        },
        'derived_within_official_surface_corridor': derived_vs_surface,
        'derived_within_all_official_corridor': derived_vs_all,
        'official_surface_within_derived_corridor': official_vs_derived,
        'interpretation_note': (
            'The D8 network is intentionally thresholded at 0.25 km² and therefore does not aim to reproduce every small mapped headwater. '
            'Distance agreement is a terrain-model validation metric, not proof that every derived line is a real channel. '
            'ČÚZK DMR coverage ends at the German border, so cross-border upstream geometry is incomplete.'
        ),
        'lowest_agreement_derived_segments_50m': diagnostics[:12],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            'ok': False,
            'computed_at_local': datetime.now(TZ).isoformat(),
            'error': str(exc),
        }
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))
        raise
