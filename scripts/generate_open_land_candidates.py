#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyflwdir
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import rowcol, xy
from rasterio.warp import Resampling, reproject, transform, transform_geom

from analyze_terrain_cuzk import ROOT, ZONES_FILE, SRC_CRS, STUDY_BBOX, fetch_dmr4g, box_mean
from analyze_sentinel_zones import find_scene, asset_info, index_for_zone

TZ = ZoneInfo('Europe/Prague')
OUT_FILE = ROOT / 'config' / 'open-land-candidates.geojson'
STATUS_FILE = ROOT / 'data' / 'validation' / 'open-land-candidates-status.json'
WORLD_COVER_URL = 'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_N51E012_Map.tif'
WORLD_COVER_CLASS_GRASSLAND = 30
HALF_SIZE_M = 60.0
MIN_GRASS_FRACTION = 0.85
MIN_SENTINEL_NDVI = 0.55
MIN_SENTINEL_VALID = 0.80
MIN_CENTER_SPACING_M = 350.0
EXISTING_OPEN_EXCLUSION_M = 250.0
NODATA = -9999.0

os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif,.TIF')
os.environ.setdefault('AWS_NO_SIGN_REQUEST', 'YES')


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


def stats(a: np.ndarray) -> dict:
    x = a[np.isfinite(a)]
    if x.size == 0:
        return {'median': None, 'mean': None, 'p10': None, 'p90': None}
    return {
        'median': round(float(np.median(x)), 3),
        'mean': round(float(np.mean(x)), 3),
        'p10': round(float(np.percentile(x, 10)), 3),
        'p90': round(float(np.percentile(x, 90)), 3),
    }


def square_geom_jtsk(x: float, y: float, half=HALF_SIZE_M) -> dict:
    return {
        'type': 'Polygon',
        'coordinates': [[
            [x-half, y-half], [x+half, y-half], [x+half, y+half], [x-half, y+half], [x-half, y-half]
        ]],
    }


def centroid_existing_open(zones: dict) -> tuple[float, float] | None:
    for f in zones.get('features') or []:
        p = f.get('properties') or {}
        if p.get('id') != 'NH-OPEN-01':
            continue
        g = transform_geom('EPSG:4326', SRC_CRS, f.get('geometry'), precision=3)
        pts = g['coordinates'][0][:-1]
        return float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts]))
    return None


def worldcover_on_dem(dem_shape, dem_transform) -> np.ndarray:
    out = np.zeros(dem_shape, dtype='uint8')
    with rasterio.Env(AWS_NO_SIGN_REQUEST='YES'):
        with rasterio.open(WORLD_COVER_URL) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=out,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=dem_transform,
                dst_crs=SRC_CRS,
                dst_nodata=0,
                resampling=Resampling.nearest,
            )
    return out


def window_mask(shape, r: int, c: int, radius_px: int) -> tuple[slice, slice]:
    return (
        slice(max(0, r-radius_px), min(shape[0], r+radius_px+1)),
        slice(max(0, c-radius_px), min(shape[1], c+radius_px+1)),
    )


def top_indices(score: np.ndarray, valid: np.ndarray, n=8000) -> np.ndarray:
    flat = np.where(valid & np.isfinite(score), score, -np.inf).ravel()
    finite = np.isfinite(flat) & (flat > -np.inf)
    count = int(np.count_nonzero(finite))
    if count == 0:
        return np.array([], dtype='int64')
    k = min(n, count)
    idx = np.argpartition(flat, -k)[-k:]
    return idx[np.argsort(flat[idx])[::-1]]


def candidate_entry(role: str, r: int, c: int, score: float, *, dem, slope, aspect, tpi300, tpi900, twi, wetness, drying, cold, wc, transform_affine, scene, selected_centers, existing_open_center):
    x, y = xy(transform_affine, r, c, offset='center')
    x, y = float(x), float(y)
    if existing_open_center and math.hypot(x-existing_open_center[0], y-existing_open_center[1]) < EXISTING_OPEN_EXCLUSION_M:
        return None
    if any(math.hypot(x-sx, y-sy) < MIN_CENTER_SPACING_M for sx, sy in selected_centers):
        return None

    radius_px = max(1, round(HALF_SIZE_M / abs(float(transform_affine.a))))
    rs, cs = window_mask(dem.shape, r, c, radius_px)
    wc_win = wc[rs, cs]
    valid_wc = wc_win > 0
    if np.count_nonzero(valid_wc) < 50:
        return None
    grass_fraction = float(np.count_nonzero(wc_win == WORLD_COVER_CLASS_GRASSLAND) / np.count_nonzero(valid_wc))
    if grass_fraction < MIN_GRASS_FRACTION:
        return None

    gj = square_geom_jtsk(x, y)
    gw = transform_geom(SRC_CRS, 'EPSG:4326', gj, precision=7)
    assets = scene.get('assets') or {}
    red = asset_info(scene, 'red')
    nir = asset_info(scene, 'nir')
    swir = asset_info(scene, 'swir16')
    ndmi_nir = asset_info(scene, 'nir08' if 'nir08' in assets else 'nir')
    scl = assets.get('scl') if (assets.get('scl') or {}).get('href') else None
    try:
        ndvi = index_for_zone(nir, red, gw, scl)
        ndmi = index_for_zone(ndmi_nir, swir, gw, scl)
    except Exception:
        return None
    if ndvi.get('median') is None or ndvi.get('median') < MIN_SENTINEL_NDVI or ndvi.get('valid_fraction', 0) < MIN_SENTINEL_VALID:
        return None

    local = np.isfinite(dem[rs, cs])
    def med(arr):
        z = arr[rs, cs][local & np.isfinite(arr[rs, cs])]
        return round(float(np.median(z)), 2) if z.size else None

    lon, lat = transform(SRC_CRS, 'EPSG:4326', [x], [y])
    return {
        'center_x_jtsk': round(x, 1),
        'center_y_jtsk': round(y, 1),
        'center_lon': round(float(lon[0]), 6),
        'center_lat': round(float(lat[0]), 6),
        'geometry': gw,
        'properties': {
            'selection_role': role,
            'selection_score': round(float(score), 4),
            'status': 'candidate_needs_visual_check',
            'worldcover_2021_grassland_fraction': round(grass_fraction, 3),
            'sentinel_scene_id': scene.get('id'),
            'sentinel_scene_datetime': (scene.get('properties') or {}).get('datetime'),
            'sentinel_ndvi_median': ndvi.get('median'),
            'sentinel_ndmi_median': ndmi.get('median'),
            'sentinel_valid_fraction': ndvi.get('valid_fraction'),
            'elevation_m_bpv': med(dem),
            'slope_deg': med(slope),
            'aspect_deg': med(aspect),
            'tpi_300m_m': med(tpi300),
            'tpi_900m_m': med(tpi900),
            'twi': med(twi),
            'wetness_score': med(wetness),
            'drying_score': med(drying),
            'cold_pool_score': med(cold),
        },
    }


def main():
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))
    now = datetime.now(TZ)
    dem, tr, export_info = fetch_dmr4g()
    valid_dem = np.isfinite(dem)
    px = (abs(float(tr.a)) + abs(float(tr.e))) / 2.0

    west, south, east, north = STUDY_BBOX
    study_geom = {'type':'Polygon','coordinates':[[[west,south],[east,south],[east,north],[west,north],[west,south]]]}
    study_jtsk = transform_geom('EPSG:4326', SRC_CRS, study_geom, precision=3)
    study_mask = geometry_mask([study_jtsk], out_shape=dem.shape, transform=tr, invert=True) & valid_dem

    grad_south, grad_east = np.gradient(dem, abs(float(tr.e)), abs(float(tr.a)))
    grad_north = -grad_south
    slope_rad = np.arctan(np.hypot(grad_east, grad_north)).astype('float32')
    slope = np.degrees(slope_rad).astype('float32')
    aspect = ((np.degrees(np.arctan2(-grad_east, -grad_north)) + 360.0) % 360.0).astype('float32')

    r300 = max(1, round(300 / px)); r900 = max(1, round(900 / px))
    tpi300 = (dem - box_mean(dem, r300)).astype('float32')
    tpi900 = (dem - box_mean(dem, r900)).astype('float32')

    dem_flow = np.where(valid_dem, dem, NODATA).astype('float32')
    flw = pyflwdir.from_dem(dem_flow, nodata=NODATA, max_depth=-1.0, transform=tr, latlon=False, outlets='edge')
    upstream = flw.upstream_area(unit='m2').astype('float32'); upstream[~valid_dem] = np.nan
    slope_for_twi = np.maximum(slope_rad, math.radians(0.5))
    specific = upstream / max(px, 1e-6)
    twi = np.full(dem.shape, np.nan, dtype='float32')
    ok = valid_dem & np.isfinite(upstream) & (upstream > 0)
    twi[ok] = np.log(np.maximum(specific[ok],1e-6)/np.maximum(np.tan(slope_for_twi[ok]),1e-6))

    wet_twi = robust01(twi, study_mask)
    valley300 = robust01(-tpi300, study_mask); valley900 = robust01(-tpi900, study_mask)
    ridge900 = robust01(tpi900, study_mask)
    convex300 = robust01(tpi300, study_mask)
    slope_norm = robust01(slope, study_mask, lo=2, hi=95)
    low_elev = robust01(dem, study_mask, invert=True)
    flatness = 1.0 - slope_norm
    southness = (1.0 - np.cos(np.deg2rad(aspect))) / 2.0
    northness = (1.0 + np.cos(np.deg2rad(aspect))) / 2.0
    solar = np.clip(southness * np.sin(np.minimum(slope_rad, math.radians(45))) / math.sin(math.radians(45)), 0, 1).astype('float32')
    wetness = 100*(0.60*wet_twi + 0.25*valley300 + 0.15*valley900)
    drying = 100*(0.45*(1-wet_twi) + 0.30*solar + 0.15*convex300 + 0.10*slope_norm)
    cold = 100*(0.35*valley900 + 0.30*valley300 + 0.20*flatness + 0.15*low_elev)

    wc = worldcover_on_dem(dem.shape, tr)
    grass = study_mask & (wc == WORLD_COVER_CLASS_GRASSLAND)
    scene = find_scene(datetime.now(TZ).astimezone(__import__('datetime').timezone.utc))

    role_scores = {
        'low_position': valley900,
        'high_position': ridge900,
        'north_facing_slope': northness * slope_norm,
        'south_facing_slope': southness * slope_norm,
        'high_twi': wet_twi,
        'low_twi': 1.0 - wet_twi,
    }
    role_masks = {
        'low_position': grass,
        'high_position': grass,
        'north_facing_slope': grass & (slope >= 4.0),
        'south_facing_slope': grass & (slope >= 4.0),
        'high_twi': grass,
        'low_twi': grass,
    }

    selected = []
    centers = []
    existing_open = centroid_existing_open(zones)
    missing_roles = []
    for role in ['low_position','high_position','north_facing_slope','south_facing_slope','high_twi','low_twi']:
        found = None
        for idx in top_indices(role_scores[role], role_masks[role], n=10000):
            r, c = np.unravel_index(int(idx), dem.shape)
            found = candidate_entry(role, r, c, role_scores[role][r,c], dem=dem, slope=slope, aspect=aspect, tpi300=tpi300, tpi900=tpi900, twi=twi, wetness=wetness, drying=drying, cold=cold, wc=wc, transform_affine=tr, scene=scene, selected_centers=centers, existing_open_center=existing_open)
            if found:
                break
        if not found:
            missing_roles.append(role)
            continue
        centers.append((found.pop('center_x_jtsk'), found.pop('center_y_jtsk')))
        props = found.pop('properties')
        props['id'] = f"NH-OPEN-C{len(selected)+1:02d}"
        props['name'] = {
            'low_position':'Kandidát – nízká poloha',
            'high_position':'Kandidát – vyšší poloha',
            'north_facing_slope':'Kandidát – severní svah',
            'south_facing_slope':'Kandidát – jižní svah',
            'high_twi':'Kandidát – vysoké TWI',
            'low_twi':'Kandidát – nízké TWI',
        }[role]
        props['class'] = 'open_land_candidate'
        props['center_lat'] = found.pop('center_lat'); props['center_lon'] = found.pop('center_lon')
        selected.append({'type':'Feature','properties':props,'geometry':found['geometry']})

    fc = {
        'type':'FeatureCollection',
        'name':'nove-hrabeci-open-land-candidates-v1',
        'properties':{
            'status':'candidate_needs_visual_check',
            'generated_at_local':now.isoformat(),
            'purpose':'Matched open-land sampling network for testing terrain effects while holding land-cover type more constant.',
            'worldcover_source':'ESA WorldCover 2021 v200, class 30 grassland, 10 m',
            'worldcover_url':WORLD_COVER_URL,
            'selection_rules':{
                'minimum_worldcover_grassland_fraction':MIN_GRASS_FRACTION,
                'minimum_current_sentinel_ndvi_median':MIN_SENTINEL_NDVI,
                'minimum_sentinel_valid_fraction':MIN_SENTINEL_VALID,
                'sampling_square_m':HALF_SIZE_M*2,
                'minimum_center_spacing_m':MIN_CENTER_SPACING_M,
            },
            'note':'Candidates are algorithmically screened but must be visually checked on satellite imagery before promotion into config/zones.geojson.'
        },
        'features':selected,
    }
    OUT_FILE.write_text(json.dumps(fc, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    status = {
        'ok':len(selected)>=4,
        'quality_status':'candidate_network_ready_for_visual_check' if len(selected)>=4 else 'insufficient_candidates',
        'generated_at_local':now.isoformat(),
        'candidate_count':len(selected),
        'missing_roles':missing_roles,
        'roles':[f['properties']['selection_role'] for f in selected],
        'zones_file':str(OUT_FILE.relative_to(ROOT)).replace('\\','/'),
        'sentinel_scene_id':scene.get('id'),
        'worldcover':'ESA WorldCover 2021 v200 class 30 grassland',
        'terrain':'ČÚZK DMR 4G 5 m + derived TPI/TWI',
        'dmr_export':export_info,
        'next_step':'Inspect open-candidates.html over satellite imagery; promote only visually plausible homogeneous grassland candidates.'
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))
    if len(selected) < 4:
        raise RuntimeError('Fewer than four robust open-land candidates found')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not STATUS_FILE.exists():
            STATUS_FILE.write_text(json.dumps({'ok':False,'generated_at_local':datetime.now(TZ).isoformat(),'error':str(exc)},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        raise
