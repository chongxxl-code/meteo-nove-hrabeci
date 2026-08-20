#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyflwdir
import rasterio
from PIL import Image
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds, rowcol
from rasterio.warp import Resampling, reproject, transform_geom

from analyze_terrain_cuzk import ROOT, ZONES_FILE, SRC_CRS, STUDY_BBOX, fetch_dmr4g, box_mean

TZ = ZoneInfo('Europe/Prague')
OUT_DIR = ROOT / 'data' / 'terrain' / 'predisposition'
STATUS_FILE = OUT_DIR / 'status.json'
META_FILE = OUT_DIR / 'layers.json'
NODATA = -9999.0
OUT_W = 1200
OUT_H = 900
MIN_SLOPE_DEG_FOR_TWI = 0.5


def robust01(a: np.ndarray, mask: np.ndarray, lo=2.0, hi=98.0, invert=False) -> np.ndarray:
    x = a[mask & np.isfinite(a)]
    out = np.full(a.shape, np.nan, dtype='float32')
    if x.size == 0:
        return out
    p0, p1 = np.percentile(x, [lo, hi])
    if p1 <= p0:
        out[mask & np.isfinite(a)] = 0.5
        return out
    v = np.clip((a - p0) / (p1 - p0), 0.0, 1.0)
    if invert:
        v = 1.0 - v
    out[mask & np.isfinite(a)] = v[mask & np.isfinite(a)]
    return out


def zone_stats(values: np.ndarray) -> dict:
    x = values[np.isfinite(values)]
    if x.size == 0:
        return {'pixels': 0, 'median': None, 'mean': None, 'p90': None, 'max': None}
    return {
        'pixels': int(x.size),
        'median': round(float(np.median(x)), 1),
        'mean': round(float(np.mean(x)), 1),
        'p90': round(float(np.percentile(x, 90)), 1),
        'max': round(float(np.max(x)), 1),
    }


def reproject_score(score: np.ndarray, src_transform) -> np.ndarray:
    west, south, east, north = STUDY_BBOX
    dst_transform = from_bounds(west, south, east, north, OUT_W, OUT_H)
    dst = np.full((OUT_H, OUT_W), np.nan, dtype='float32')
    reproject(
        source=score.astype('float32'),
        destination=dst,
        src_transform=src_transform,
        src_crs=SRC_CRS,
        dst_transform=dst_transform,
        dst_crs='EPSG:4326',
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return dst


def rgba_gradient(score: np.ndarray, stops: list[tuple[float, tuple[int,int,int]]]) -> np.ndarray:
    rgba = np.zeros((score.shape[0], score.shape[1], 4), dtype=np.uint8)
    valid = np.isfinite(score)
    if not np.any(valid):
        return rgba
    v = np.clip(score, 0, 100)
    xs = np.array([s[0] for s in stops], dtype='float32')
    cols = np.array([s[1] for s in stops], dtype='float32')
    for ch in range(3):
        rgba[..., ch][valid] = np.interp(v[valid], xs, cols[:, ch]).astype(np.uint8)
    # Slightly transparent so satellite/map remains legible.
    rgba[..., 3][valid] = 178
    return rgba


def save_layer(name: str, score: np.ndarray, src_transform, stops) -> None:
    out = reproject_score(score, src_transform)
    rgba = rgba_gradient(out, stops)
    Image.fromarray(rgba, mode='RGBA').save(OUT_DIR / f'{name}.png', optimize=True)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))

    dem, transform, export_info = fetch_dmr4g()
    valid_dem = np.isfinite(dem)
    px_x = abs(float(transform.a))
    px_y = abs(float(transform.e))
    cell_m = (px_x + px_y) / 2.0

    west, south, east, north = STUDY_BBOX
    study_geom = {'type':'Polygon','coordinates':[[[west,south],[east,south],[east,north],[west,north],[west,south]]]}
    study_jtsk = transform_geom('EPSG:4326', SRC_CRS, study_geom, precision=3)
    study_mask = geometry_mask([study_jtsk], out_shape=dem.shape, transform=transform, invert=True) & valid_dem

    grad_south, grad_east = np.gradient(dem, px_y, px_x)
    grad_north = -grad_south
    slope_rad = np.arctan(np.hypot(grad_east, grad_north)).astype('float32')
    slope_deg = np.degrees(slope_rad).astype('float32')
    aspect = ((np.degrees(np.arctan2(-grad_east, -grad_north)) + 360.0) % 360.0).astype('float32')

    r300 = max(1, round(300 / cell_m))
    r900 = max(1, round(900 / cell_m))
    tpi300 = (dem - box_mean(dem, r300)).astype('float32')
    tpi900 = (dem - box_mean(dem, r900)).astype('float32')

    dem_for_flow = np.where(valid_dem, dem, NODATA).astype('float32')
    flw = pyflwdir.from_dem(dem_for_flow, nodata=NODATA, max_depth=-1.0, transform=transform, latlon=False, outlets='edge')
    upstream_m2 = flw.upstream_area(unit='m2').astype('float32')
    upstream_m2[~valid_dem] = np.nan

    min_slope = math.radians(MIN_SLOPE_DEG_FOR_TWI)
    slope_for_twi = np.maximum(slope_rad, min_slope)
    specific_area = upstream_m2 / max(cell_m, 1e-6)
    twi = np.full(dem.shape, np.nan, dtype='float32')
    ok = valid_dem & np.isfinite(upstream_m2) & (upstream_m2 > 0)
    twi[ok] = np.log(np.maximum(specific_area[ok],1e-6) / np.maximum(np.tan(slope_for_twi[ok]),1e-6)).astype('float32')

    # Component normalizations are calculated only within the study area so 0-100 is local.
    wet_twi = robust01(twi, study_mask)
    valley300 = robust01(-tpi300, study_mask)
    valley900 = robust01(-tpi900, study_mask)
    convex300 = robust01(tpi300, study_mask)
    slope_norm = robust01(slope_deg, study_mask, lo=2, hi=95)
    flatness = 1.0 - slope_norm
    low_elev = robust01(dem, study_mask, invert=True)

    # South-facing exposure matters progressively more on steeper slopes; flat ground gets no aspect bonus.
    southness = (1.0 - np.cos(np.deg2rad(aspect))) / 2.0
    solar_exposure = np.clip(southness * np.sin(np.minimum(slope_rad, math.radians(45))) / math.sin(math.radians(45)), 0, 1).astype('float32')
    solar_exposure[~study_mask] = np.nan

    # v1 terrain-only explanatory scores. They are predispositions, not observations or forecasts.
    wetness = 100.0 * (0.60 * wet_twi + 0.25 * valley300 + 0.15 * valley900)
    drying = 100.0 * (0.45 * (1.0 - wet_twi) + 0.30 * solar_exposure + 0.15 * convex300 + 0.10 * slope_norm)
    cold = 100.0 * (0.35 * valley900 + 0.30 * valley300 + 0.20 * flatness + 0.15 * low_elev)

    for arr in (wetness, drying, cold):
        arr[~study_mask] = np.nan
        np.clip(arr, 0, 100, out=arr, where=np.isfinite(arr))

    save_layer('wetness', wetness, transform, [(0,(88,66,43)),(30,(178,158,92)),(55,(89,160,130)),(75,(54,151,190)),(100,(37,87,170))])
    save_layer('drying', drying, transform, [(0,(44,118,121)),(30,(116,170,123)),(55,(215,187,87)),(75,(224,121,53)),(100,(174,49,48))])
    save_layer('cold', cold, transform, [(0,(72,73,76)),(30,(76,128,142)),(55,(87,173,190)),(75,(149,211,225)),(100,(226,244,248))])

    zone_results = []
    for feature in zones.get('features') or []:
        p = feature.get('properties') or {}
        geom = feature.get('geometry') or {}
        entry = {'id':p.get('id'),'name':p.get('name'),'class':p.get('class'),'geometry':geom.get('type')}
        if geom.get('type') == 'Point':
            gp = transform_geom('EPSG:4326', SRC_CRS, geom, precision=3)
            r,c = rowcol(transform, *gp['coordinates'])
            if 0 <= r < dem.shape[0] and 0 <= c < dem.shape[1] and study_mask[r,c]:
                entry['wetness_score'] = round(float(wetness[r,c]),1) if np.isfinite(wetness[r,c]) else None
                entry['drying_score'] = round(float(drying[r,c]),1) if np.isfinite(drying[r,c]) else None
                entry['cold_pool_score'] = round(float(cold[r,c]),1) if np.isfinite(cold[r,c]) else None
            zone_results.append(entry)
            continue
        if geom.get('type') in ('Polygon','MultiPolygon'):
            gp = transform_geom('EPSG:4326', SRC_CRS, geom, precision=3)
            mask = geometry_mask([gp], out_shape=dem.shape, transform=transform, invert=True) & study_mask
            entry['wetness'] = zone_stats(np.where(mask, wetness, np.nan))
            entry['drying'] = zone_stats(np.where(mask, drying, np.nan))
            entry['cold_pool'] = zone_stats(np.where(mask, cold, np.nan))
        zone_results.append(entry)

    layers = {
        'version':'terrain-predisposition-v1',
        'status':'terrain_only_explanatory_model',
        'computed_at_local':now.isoformat(),
        'bounds_wgs84':[[south,west],[north,east]],
        'image_size_px':[OUT_W,OUT_H],
        'source':{
            'terrain':'ČÚZK DMR 4G 5 m',
            'hydrology':'D8 flow accumulation + TWI derived from DMR 4G',
            'export':export_info,
        },
        'layers':{
            'wetness':{
                'file':'data/terrain/predisposition/wetness.png',
                'label':'Predispozice k zadržování vody',
                'score_range':[0,100],
                'formula':'60% local TWI + 25% valley position TPI300 + 15% valley position TPI900',
                'interpretation':'Higher score = terrain is more predisposed to concentrate and retain water. Not measured soil moisture.'
            },
            'drying':{
                'file':'data/terrain/predisposition/drying.png',
                'label':'Predispozice k vysychání',
                'score_range':[0,100],
                'formula':'45% inverse TWI + 30% south-facing solar exposure + 15% convexity TPI300 + 10% slope exposure',
                'interpretation':'Higher score = terrain-only tendency to drain/heat/dry faster. Vegetation, soil type and weather are not yet included.'
            },
            'cold':{
                'file':'data/terrain/predisposition/cold.png',
                'label':'Predispozice k nočnímu hromadění chladu',
                'score_range':[0,100],
                'formula':'35% broad valley position TPI900 + 30% local depression TPI300 + 20% flatness + 15% low relative elevation',
                'interpretation':'Higher score = terrain-only propensity for cold-air pooling under suitable clear/calm weather. Not a temperature forecast.'
            }
        },
        'limitations':[
            'ČÚZK DMR 4G ends at the Czech border; terrain context and flow contributions from Germany are missing.',
            'Scores are locally normalized for this Nové Hraběcí study area and should not be compared numerically with another region.',
            'v1 uses terrain only. Land cover, canopy, soil, buildings, Sentinel vegetation/moisture response and observed weather will be added later.',
            'Cold-air pooling requires suitable meteorological conditions; terrain predisposition alone does not guarantee a colder night.'
        ],
        'zones':zone_results,
    }
    META_FILE.write_text(json.dumps(layers,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    status = {
        'ok':True,
        'quality_status':'valid_terrain_only_v1_with_border_limitation',
        'computed_at_local':now.isoformat(),
        'layers_file':str(META_FILE.relative_to(ROOT)).replace('\\','/'),
        'png_files':['wetness.png','drying.png','cold.png'],
        'zone_count':len(zone_results),
        'next_step':'Inspect maps, then calibrate v2 with Sentinel NDMI/NDVI, observed rain and eventually local station temperature/moisture observations.'
    }
    STATUS_FILE.write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(status,ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        OUT_DIR.mkdir(parents=True,exist_ok=True)
        payload={'ok':False,'computed_at_local':datetime.now(TZ).isoformat(),'error':str(exc)}
        STATUS_FILE.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False))
        raise
