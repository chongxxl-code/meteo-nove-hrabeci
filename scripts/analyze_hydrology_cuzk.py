#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyflwdir
from rasterio.features import geometry_mask
from rasterio.transform import rowcol
from rasterio.warp import transform_geom

from analyze_terrain_cuzk import (
    ROOT,
    ZONES_FILE,
    SRC_CRS,
    STUDY_BBOX,
    fetch_dmr4g,
    basic_stats,
)

TZ = ZoneInfo('Europe/Prague')
DATA_DIR = ROOT / 'data' / 'terrain'
STATUS_FILE = DATA_DIR / 'hydrology-status.json'
DRAINAGE_FILE = DATA_DIR / 'derived-drainage.geojson'
NODATA = -9999.0
MIN_SLOPE_DEG_FOR_TWI = 0.5
STREAM_THRESHOLD_M2 = 250_000.0  # 0.25 km2 contributing area

COVERAGE_LIMITATION = (
    'ČÚZK DMR 4G covers Czech territory only. The study bbox reaches the German border, '
    'so upstream contributions originating in Germany are absent and flow accumulation '
    'near the state/data edge can be truncated. Derived drainage is a Czech-side terrain model, '
    'not an official hydrographic network.'
)


def finite_stats(values: np.ndarray) -> dict:
    return basic_stats(values)


def percentile_rank(reference: np.ndarray, value: float | None) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    x = reference[np.isfinite(reference)]
    if x.size == 0:
        return None
    return round(float(np.count_nonzero(x <= value) / x.size * 100.0), 1)


def hydro_stats(mask: np.ndarray, upstream_m2: np.ndarray, twi: np.ndarray, study_twi: np.ndarray) -> dict:
    valid = mask & np.isfinite(upstream_m2) & np.isfinite(twi)
    if not np.any(valid):
        return {
            'pixels': 0,
            'upstream_area_m2': finite_stats(np.array([], dtype='float32')),
            'twi': finite_stats(np.array([], dtype='float32')),
            'twi_median_study_percentile': None,
            'channel_fraction_ge_1ha': None,
            'channel_fraction_ge_10ha': None,
        }
    ua = np.where(valid, upstream_m2, np.nan)
    tv = np.where(valid, twi, np.nan)
    tstats = finite_stats(tv)
    n = int(np.count_nonzero(valid))
    return {
        'pixels': n,
        'upstream_area_m2': finite_stats(ua),
        'twi': tstats,
        'twi_median_study_percentile': percentile_rank(study_twi, tstats.get('median')),
        'channel_fraction_ge_1ha': round(float(np.count_nonzero(valid & (upstream_m2 >= 10_000.0)) / n), 4),
        'channel_fraction_ge_10ha': round(float(np.count_nonzero(valid & (upstream_m2 >= 100_000.0)) / n), 4),
    }


def point_hydro(r: int, c: int, upstream_m2: np.ndarray, twi: np.ndarray, study_twi: np.ndarray) -> dict:
    ua = float(upstream_m2[r, c]) if np.isfinite(upstream_m2[r, c]) else None
    tv = float(twi[r, c]) if np.isfinite(twi[r, c]) else None
    return {
        'upstream_area_m2': round(ua, 1) if ua is not None else None,
        'upstream_area_ha': round(ua / 10_000.0, 3) if ua is not None else None,
        'twi': round(tv, 3) if tv is not None else None,
        'twi_study_percentile': percentile_rank(study_twi, tv),
    }


def drainage_geojson(flw, upstream_m2: np.ndarray, study_mask: np.ndarray) -> dict:
    stream_mask = study_mask & np.isfinite(upstream_m2) & (upstream_m2 >= STREAM_THRESHOLD_M2)
    features = flw.streams(
        mask=stream_mask,
        max_len=400,
        upstream_km2=(upstream_m2 / 1_000_000.0).astype('float32'),
    )
    out = []
    for feat in features:
        geom = feat.get('geometry')
        if not geom:
            continue
        try:
            geom4326 = transform_geom(SRC_CRS, 'EPSG:4326', geom, precision=6)
        except Exception:
            continue
        props = dict(feat.get('properties') or {})
        # Convert NumPy scalar types so json.dumps always succeeds.
        for k, v in list(props.items()):
            if isinstance(v, np.generic):
                props[k] = v.item()
        props['source'] = 'terrain-derived D8 drainage candidate'
        props['official_watercourse'] = False
        props['threshold_upstream_km2'] = STREAM_THRESHOLD_M2 / 1_000_000.0
        out.append({'type': 'Feature', 'properties': props, 'geometry': geom4326})
    return {
        'type': 'FeatureCollection',
        'name': 'nove-hrabeci-derived-drainage',
        'properties': {
            'source': 'ČÚZK DMR 4G 5 m + pyflwdir D8',
            'status': 'terrain-derived candidates; not official watercourses',
            'threshold_upstream_km2': STREAM_THRESHOLD_M2 / 1_000_000.0,
            'coverage_limitation': COVERAGE_LIMITATION,
        },
        'features': out,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))

    dem, transform, export_info = fetch_dmr4g()
    valid_dem = np.isfinite(dem)
    cell_x = abs(float(transform.a))
    cell_y = abs(float(transform.e))
    cell_m = (cell_x + cell_y) / 2.0

    # pyflwdir expects a numeric nodata value. By default max_depth=-1 fills all inland
    # depressions to their lowest pour point; outlets='edge' allows flow to leave at the valid-data edge.
    dem_for_flow = np.where(valid_dem, dem, NODATA).astype('float32')
    flw = pyflwdir.from_dem(
        dem_for_flow,
        nodata=NODATA,
        max_depth=-1.0,
        transform=transform,
        latlon=False,
        outlets='edge',
    )
    upstream_m2 = flw.upstream_area(unit='m2').astype('float32')
    upstream_m2[~valid_dem] = np.nan

    # TWI = ln(specific catchment area / tan(beta)). We use original DMR4G slope,
    # while flow routing uses depression filling. A 0.5-degree floor prevents singularities on flats.
    grad_south, grad_east = np.gradient(dem, cell_y, cell_x)
    grad_north = -grad_south
    slope_rad = np.arctan(np.hypot(grad_east, grad_north)).astype('float32')
    min_slope_rad = math.radians(MIN_SLOPE_DEG_FOR_TWI)
    slope_for_twi = np.maximum(slope_rad, min_slope_rad)
    specific_catchment_area_m = upstream_m2 / max(cell_m, 1e-6)
    twi = np.full(dem.shape, np.nan, dtype='float32')
    twi_valid = valid_dem & np.isfinite(upstream_m2) & (upstream_m2 > 0)
    twi[twi_valid] = np.log(
        np.maximum(specific_catchment_area_m[twi_valid], 1e-6)
        / np.maximum(np.tan(slope_for_twi[twi_valid]), 1e-6)
    ).astype('float32')

    west, south, east, north = STUDY_BBOX
    study_geom = {
        'type': 'Polygon',
        'coordinates': [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }
    study_jtsk = transform_geom('EPSG:4326', SRC_CRS, study_geom, precision=3)
    study_mask = geometry_mask([study_jtsk], out_shape=dem.shape, transform=transform, invert=True) & valid_dem
    study_twi = np.where(study_mask, twi, np.nan)
    study_upstream = np.where(study_mask, upstream_m2, np.nan)

    zone_results = []
    for feature in zones.get('features') or []:
        p = feature.get('properties') or {}
        geom = feature.get('geometry') or {}
        entry = {
            'id': p.get('id'),
            'name': p.get('name'),
            'class': p.get('class'),
            'zone_status': p.get('status'),
            'geometry': geom.get('type'),
        }
        if geom.get('type') == 'Point':
            gp = transform_geom('EPSG:4326', SRC_CRS, geom, precision=3)
            x, y = gp['coordinates']
            r, c = rowcol(transform, x, y)
            if 0 <= r < dem.shape[0] and 0 <= c < dem.shape[1] and valid_dem[r, c]:
                entry.update(point_hydro(r, c, upstream_m2, twi, study_twi))
            zone_results.append(entry)
            continue
        if geom.get('type') not in ('Polygon', 'MultiPolygon'):
            zone_results.append(entry)
            continue
        gp = transform_geom('EPSG:4326', SRC_CRS, geom, precision=3)
        mask = geometry_mask([gp], out_shape=dem.shape, transform=transform, invert=True) & valid_dem
        entry.update(hydro_stats(mask, upstream_m2, twi, study_twi))
        zone_results.append(entry)

    drainage = drainage_geojson(flw, upstream_m2, study_mask)
    DRAINAGE_FILE.write_text(json.dumps(drainage, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')

    valid_study_count = int(np.count_nonzero(study_mask))
    edge_warning = True  # explicit because study area touches international/data coverage boundary
    status = {
        'ok': bool(zone_results) and np.any(np.isfinite(study_twi)),
        'quality_status': 'valid_with_border_limitation',
        'provider': 'ČÚZK DMR 4G 5 m + pyflwdir D8',
        'computed_at_local': now.isoformat(),
        'analysis_crs': SRC_CRS,
        'vertical_reference': 'Balt po vyrovnání (Bpv)',
        'cell_size_m': round(cell_m, 3),
        'source_export': export_info,
        'flow_method': {
            'routing': 'D8 steepest-gradient flow direction',
            'depression_handling': 'all inland depressions filled to lowest pour point for routing (max_depth=-1)',
            'outlets': 'valid-data edge',
            'upstream_area_unit': 'm2',
        },
        'twi_method': {
            'formula': 'ln((upstream_area_m2 / cell_width_m) / tan(slope_rad))',
            'slope_source': 'original DMR4G surface before depression filling',
            'minimum_slope_deg': MIN_SLOPE_DEG_FOR_TWI,
            'interpretation': 'terrain wetness predisposition only; not observed soil moisture',
        },
        'coverage_limitation': COVERAGE_LIMITATION,
        'edge_truncation_warning': edge_warning,
        'study_area': {
            'valid_pixels': valid_study_count,
            'upstream_area_m2': finite_stats(study_upstream),
            'twi': finite_stats(study_twi),
            'derived_drainage_threshold_km2': STREAM_THRESHOLD_M2 / 1_000_000.0,
            'derived_drainage_segments': len(drainage['features']),
        },
        'zones_version': zones.get('name'),
        'zones': zone_results,
        'derived_drainage_file': str(DRAINAGE_FILE.relative_to(ROOT)).replace('\\', '/'),
        'next_step': 'Validate terrain-derived drainage against mapped watercourses/wet areas, then derive practical wetness/drying and cold-air terrain predisposition layers.',
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            'ok': False,
            'provider': 'ČÚZK DMR 4G 5 m + pyflwdir D8',
            'computed_at_local': datetime.now(TZ).isoformat(),
            'coverage_limitation': COVERAGE_LIMITATION,
            'error': str(exc),
        }
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))
        raise
