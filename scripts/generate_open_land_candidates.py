#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pyflwdir
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import xy
from rasterio.warp import Resampling, reproject, transform, transform_geom

from analyze_terrain_cuzk import ROOT, ZONES_FILE, SRC_CRS, STUDY_BBOX, fetch_dmr4g, box_mean
from analyze_sentinel_zones import find_scene, asset_info, index_for_zone

TZ = ZoneInfo('Europe/Prague')
OUT_FILE = ROOT / 'config' / 'open-land-candidates.geojson'
STATUS_FILE = ROOT / 'data' / 'validation' / 'open-land-candidates-status.json'

WORLD_COVER_URL = 'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_N51E012_Map.tif'
WORLD_COVER_CLASS_GRASSLAND = 30

# v2: smaller analytical core plus a wider guard ring. The sample itself must be
# almost pure grassland and the wider neighbourhood must still be overwhelmingly
# grassland, which pushes candidates away from forest/field/settlement edges.
HALF_SIZE_M = 40.0
CORE_HALF_SIZE_M = 20.0
GUARD_HALF_SIZE_M = 70.0
MIN_CORE_GRASS_FRACTION = 1.00
MIN_SAMPLE_GRASS_FRACTION = 0.98
MIN_GUARD_GRASS_FRACTION = 0.94

MIN_SENTINEL_NDVI = 0.58
MIN_SENTINEL_NDVI_P10 = 0.45
MAX_SENTINEL_NDVI_SPREAD = 0.22
MIN_SENTINEL_VALID = 0.95

CANDIDATES_PER_ROLE = 3
MIN_CENTER_SPACING_M = 220.0
EXISTING_OPEN_EXCLUSION_M = 180.0

MIN_ASPECT_SLOPE_DEG = 5.0
MAX_ASPECT_SECTOR_ERROR_DEG = 40.0
MIN_ASPECT_COHERENCE = 0.65

MIN_LOW_TPI900_M = -8.0
MIN_HIGH_TPI900_M = 8.0
MIN_HIGH_TWI = 6.0
MAX_LOW_TWI = 5.6

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


def square_geom_jtsk(x: float, y: float, half=HALF_SIZE_M) -> dict:
    return {
        'type': 'Polygon',
        'coordinates': [[
            [x-half, y-half], [x+half, y-half], [x+half, y+half],
            [x-half, y+half], [x-half, y-half]
        ]],
    }


def centroid_existing_open(zones: dict) -> tuple[float, float] | None:
    for f in zones.get('features') or []:
        if (f.get('properties') or {}).get('id') != 'NH-OPEN-01':
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


def window_slices(shape, r: int, c: int, radius_px: int) -> tuple[slice, slice]:
    return (
        slice(max(0, r-radius_px), min(shape[0], r+radius_px+1)),
        slice(max(0, c-radius_px), min(shape[1], c+radius_px+1)),
    )


def landcover_fraction(wc: np.ndarray, rs: slice, cs: slice, klass: int) -> tuple[float | None, int]:
    win = wc[rs, cs]
    valid = win > 0
    n = int(np.count_nonzero(valid))
    if n == 0:
        return None, 0
    return float(np.count_nonzero(win[valid] == klass) / n), n


def top_indices(score: np.ndarray, valid: np.ndarray, n=30000) -> np.ndarray:
    flat = np.where(valid & np.isfinite(score), score, -np.inf).ravel()
    count = int(np.count_nonzero(flat > -np.inf))
    if count == 0:
        return np.array([], dtype='int64')
    k = min(n, count)
    idx = np.argpartition(flat, -k)[-k:]
    return idx[np.argsort(flat[idx])[::-1]]


def angular_distance(a: float, b: float) -> float:
    return abs((a-b+180.0) % 360.0 - 180.0)


def circular_aspect(aspect: np.ndarray, slope: np.ndarray) -> tuple[float | None, float | None]:
    valid = np.isfinite(aspect) & np.isfinite(slope) & (slope >= MIN_ASPECT_SLOPE_DEG)
    if np.count_nonzero(valid) < 20:
        return None, None
    rad = np.deg2rad(aspect[valid])
    s = float(np.mean(np.sin(rad)))
    c = float(np.mean(np.cos(rad)))
    strength = math.hypot(s, c)
    if strength < 1e-9:
        return None, round(strength, 3)
    deg = (math.degrees(math.atan2(s, c)) + 360.0) % 360.0
    return round(deg, 1), round(strength, 3)


def candidate_entry(
    role: str, r: int, c: int, score: float, *,
    dem, slope, aspect, tpi300, tpi900, twi, wetness, drying, cold,
    wc, transform_affine, scene, selected_centers, existing_open_center
):
    x, y = xy(transform_affine, r, c, offset='center')
    x, y = float(x), float(y)

    if existing_open_center and math.hypot(x-existing_open_center[0], y-existing_open_center[1]) < EXISTING_OPEN_EXCLUSION_M:
        return None
    if any(math.hypot(x-sx, y-sy) < MIN_CENTER_SPACING_M for sx, sy in selected_centers):
        return None

    px = abs(float(transform_affine.a))
    core_radius = max(1, round(CORE_HALF_SIZE_M / px))
    sample_radius = max(1, round(HALF_SIZE_M / px))
    guard_radius = max(1, round(GUARD_HALF_SIZE_M / px))

    crs, ccs = window_slices(dem.shape, r, c, core_radius)
    srs, scs = window_slices(dem.shape, r, c, sample_radius)
    grs, gcs = window_slices(dem.shape, r, c, guard_radius)

    core_grass, core_n = landcover_fraction(wc, crs, ccs, WORLD_COVER_CLASS_GRASSLAND)
    sample_grass, sample_n = landcover_fraction(wc, srs, scs, WORLD_COVER_CLASS_GRASSLAND)
    guard_grass, guard_n = landcover_fraction(wc, grs, gcs, WORLD_COVER_CLASS_GRASSLAND)
    if min(core_n, sample_n, guard_n) < 25:
        return None
    if core_grass is None or core_grass < MIN_CORE_GRASS_FRACTION:
        return None
    if sample_grass is None or sample_grass < MIN_SAMPLE_GRASS_FRACTION:
        return None
    if guard_grass is None or guard_grass < MIN_GUARD_GRASS_FRACTION:
        return None

    local = np.isfinite(dem[srs, scs])

    def med(arr):
        sub = arr[srs, scs]
        z = sub[local & np.isfinite(sub)]
        return round(float(np.median(z)), 2) if z.size else None

    aspect_mean, aspect_strength = circular_aspect(aspect[srs, scs], slope[srs, scs])
    if role == 'north_facing_slope':
        if (
            aspect_mean is None
            or angular_distance(aspect_mean, 0.0) > MAX_ASPECT_SECTOR_ERROR_DEG
            or (aspect_strength or 0) < MIN_ASPECT_COHERENCE
        ):
            return None
    if role == 'south_facing_slope':
        if (
            aspect_mean is None
            or angular_distance(aspect_mean, 180.0) > MAX_ASPECT_SECTOR_ERROR_DEG
            or (aspect_strength or 0) < MIN_ASPECT_COHERENCE
        ):
            return None

    tpi900_med = med(tpi900)
    twi_med = med(twi)
    if role == 'low_position' and (tpi900_med is None or tpi900_med >= MIN_LOW_TPI900_M):
        return None
    if role == 'high_position' and (tpi900_med is None or tpi900_med <= MIN_HIGH_TPI900_M):
        return None
    if role == 'high_twi' and (twi_med is None or twi_med < MIN_HIGH_TWI):
        return None
    if role == 'low_twi' and (twi_med is None or twi_med > MAX_LOW_TWI):
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

    ndvi_median = ndvi.get('median')
    ndvi_p10 = ndvi.get('p10')
    ndvi_p90 = ndvi.get('p90')
    ndvi_valid = ndvi.get('valid_fraction', 0)
    if ndvi_median is None or ndvi_median < MIN_SENTINEL_NDVI or ndvi_valid < MIN_SENTINEL_VALID:
        return None
    if ndvi_p10 is None or ndvi_p10 < MIN_SENTINEL_NDVI_P10:
        return None
    if ndvi_p90 is None or (ndvi_p90 - ndvi_p10) > MAX_SENTINEL_NDVI_SPREAD:
        return None

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
            'worldcover_core_grassland_fraction': round(core_grass, 3),
            'worldcover_sample_grassland_fraction': round(sample_grass, 3),
            'worldcover_guard_grassland_fraction': round(guard_grass, 3),
            'sample_square_m': HALF_SIZE_M * 2,
            'guard_square_m': GUARD_HALF_SIZE_M * 2,
            'sentinel_scene_id': scene.get('id'),
            'sentinel_scene_datetime': (scene.get('properties') or {}).get('datetime'),
            'sentinel_ndvi_median': ndvi_median,
            'sentinel_ndvi_p10': ndvi_p10,
            'sentinel_ndvi_p90': ndvi_p90,
            'sentinel_ndvi_spread_p90_p10': round(float(ndvi_p90 - ndvi_p10), 4),
            'sentinel_ndmi_median': ndmi.get('median'),
            'sentinel_valid_fraction': ndvi_valid,
            'elevation_m_bpv': med(dem),
            'slope_deg': med(slope),
            'dominant_aspect_deg': aspect_mean,
            'aspect_coherence': aspect_strength,
            'tpi_300m_m': med(tpi300),
            'tpi_900m_m': tpi900_med,
            'twi': twi_med,
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
    study_geom = {'type': 'Polygon', 'coordinates': [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}
    study_jtsk = transform_geom('EPSG:4326', SRC_CRS, study_geom, precision=3)
    study_mask = geometry_mask([study_jtsk], out_shape=dem.shape, transform=tr, invert=True) & valid_dem

    grad_south, grad_east = np.gradient(dem, abs(float(tr.e)), abs(float(tr.a)))
    grad_north = -grad_south
    slope_rad = np.arctan(np.hypot(grad_east, grad_north)).astype('float32')
    slope = np.degrees(slope_rad).astype('float32')
    aspect = ((np.degrees(np.arctan2(-grad_east, -grad_north)) + 360.0) % 360.0).astype('float32')

    r300 = max(1, round(300 / px))
    r900 = max(1, round(900 / px))
    tpi300 = (dem - box_mean(dem, r300)).astype('float32')
    tpi900 = (dem - box_mean(dem, r900)).astype('float32')

    dem_flow = np.where(valid_dem, dem, NODATA).astype('float32')
    flw = pyflwdir.from_dem(dem_flow, nodata=NODATA, max_depth=-1.0, transform=tr, latlon=False, outlets='edge')
    upstream = flw.upstream_area(unit='m2').astype('float32')
    upstream[~valid_dem] = np.nan
    slope_for_twi = np.maximum(slope_rad, math.radians(0.5))
    specific = upstream / max(px, 1e-6)
    twi = np.full(dem.shape, np.nan, dtype='float32')
    ok = valid_dem & np.isfinite(upstream) & (upstream > 0)
    twi[ok] = np.log(np.maximum(specific[ok], 1e-6) / np.maximum(np.tan(slope_for_twi[ok]), 1e-6))

    wet_twi = robust01(twi, study_mask)
    valley300 = robust01(-tpi300, study_mask)
    valley900 = robust01(-tpi900, study_mask)
    ridge900 = robust01(tpi900, study_mask)
    convex300 = robust01(tpi300, study_mask)
    slope_norm = robust01(slope, study_mask, lo=2, hi=95)
    low_elev = robust01(dem, study_mask, invert=True)
    flatness = 1.0 - slope_norm

    southness = (1.0 - np.cos(np.deg2rad(aspect))) / 2.0
    northness = (1.0 + np.cos(np.deg2rad(aspect))) / 2.0
    solar = np.clip(
        southness * np.sin(np.minimum(slope_rad, math.radians(45))) / math.sin(math.radians(45)),
        0, 1
    ).astype('float32')

    wetness = 100 * (0.60*wet_twi + 0.25*valley300 + 0.15*valley900)
    drying = 100 * (0.45*(1-wet_twi) + 0.30*solar + 0.15*convex300 + 0.10*slope_norm)
    cold = 100 * (0.35*valley900 + 0.30*valley300 + 0.20*flatness + 0.15*low_elev)

    wc = worldcover_on_dem(dem.shape, tr)
    grass = study_mask & (wc == WORLD_COVER_CLASS_GRASSLAND)
    scene = find_scene(datetime.now(timezone.utc))

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
        'north_facing_slope': grass & (slope >= MIN_ASPECT_SLOPE_DEG),
        'south_facing_slope': grass & (slope >= MIN_ASPECT_SLOPE_DEG),
        'high_twi': grass,
        'low_twi': grass,
    }

    selected = []
    centers = []
    existing_open = centroid_existing_open(zones)
    role_counts = {}
    missing_roles = []

    order = ['low_position', 'high_position', 'north_facing_slope', 'south_facing_slope', 'high_twi', 'low_twi']
    labels = {
        'low_position': 'Nízká poloha',
        'high_position': 'Vyšší poloha',
        'north_facing_slope': 'Severní svah',
        'south_facing_slope': 'Jižní svah',
        'high_twi': 'Vysoké TWI',
        'low_twi': 'Nízké TWI',
    }

    serial = 0
    for role in order:
        found_for_role = 0
        for idx in top_indices(role_scores[role], role_masks[role], n=30000):
            r, c = np.unravel_index(int(idx), dem.shape)
            found = candidate_entry(
                role, r, c, role_scores[role][r, c],
                dem=dem, slope=slope, aspect=aspect, tpi300=tpi300, tpi900=tpi900,
                twi=twi, wetness=wetness, drying=drying, cold=cold, wc=wc,
                transform_affine=tr, scene=scene, selected_centers=centers,
                existing_open_center=existing_open,
            )
            if not found:
                continue

            serial += 1
            found_for_role += 1
            centers.append((found.pop('center_x_jtsk'), found.pop('center_y_jtsk')))
            props = found.pop('properties')
            props['candidate_rank'] = found_for_role
            props['id'] = f'NH-OPEN-V2-{serial:02d}'
            props['name'] = f"{labels[role]} · varianta {found_for_role}"
            props['class'] = 'open_land_candidate_v2'
            props['center_lat'] = found.pop('center_lat')
            props['center_lon'] = found.pop('center_lon')
            selected.append({'type': 'Feature', 'properties': props, 'geometry': found['geometry']})

            if found_for_role >= CANDIDATES_PER_ROLE:
                break

        role_counts[role] = found_for_role
        if found_for_role == 0:
            missing_roles.append(role)

    fc = {
        'type': 'FeatureCollection',
        'name': 'nove-hrabeci-open-land-candidates-v2',
        'properties': {
            'status': 'candidate_needs_visual_check',
            'generated_at_local': now.isoformat(),
            'purpose': 'Alternative matched open-land sampling cores for terrain-response tests, screened more strictly after visual edge contamination in v1.',
            'worldcover_source': 'ESA WorldCover 2021 v200, class 30 grassland, 10 m',
            'worldcover_url': WORLD_COVER_URL,
            'selection_rules': {
                'core_square_m': CORE_HALF_SIZE_M * 2,
                'sampling_square_m': HALF_SIZE_M * 2,
                'guard_square_m': GUARD_HALF_SIZE_M * 2,
                'minimum_core_grassland_fraction': MIN_CORE_GRASS_FRACTION,
                'minimum_sample_grassland_fraction': MIN_SAMPLE_GRASS_FRACTION,
                'minimum_guard_grassland_fraction': MIN_GUARD_GRASS_FRACTION,
                'minimum_current_sentinel_ndvi_median': MIN_SENTINEL_NDVI,
                'minimum_current_sentinel_ndvi_p10': MIN_SENTINEL_NDVI_P10,
                'maximum_current_sentinel_ndvi_p90_p10_spread': MAX_SENTINEL_NDVI_SPREAD,
                'minimum_sentinel_valid_fraction': MIN_SENTINEL_VALID,
                'candidates_per_role_target': CANDIDATES_PER_ROLE,
                'minimum_center_spacing_m': MIN_CENTER_SPACING_M,
                'north_south_aspect_max_error_deg': MAX_ASPECT_SECTOR_ERROR_DEG,
                'minimum_aspect_coherence': MIN_ASPECT_COHERENCE,
                'low_position_tpi900_max_m': MIN_LOW_TPI900_M,
                'high_position_tpi900_min_m': MIN_HIGH_TPI900_M,
                'high_twi_minimum': MIN_HIGH_TWI,
                'low_twi_maximum': MAX_LOW_TWI,
            },
            'note': 'v2 deliberately favours clean interior grassland over extreme terrain scores. Candidates still require visual satellite approval before promotion to experimental zones.',
        },
        'features': selected,
    }
    OUT_FILE.write_text(json.dumps(fc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    status = {
        'ok': len(selected) >= 6,
        'quality_status': 'candidate_network_v2_ready_for_visual_check' if len(selected) >= 6 else 'candidate_network_v2_sparse',
        'generated_at_local': now.isoformat(),
        'candidate_count': len(selected),
        'role_counts': role_counts,
        'missing_roles': missing_roles,
        'zones_file': str(OUT_FILE.relative_to(ROOT)).replace('\\', '/'),
        'sentinel_scene_id': scene.get('id'),
        'worldcover': 'ESA WorldCover 2021 v200 class 30 grassland',
        'terrain': 'ČÚZK DMR 4G 5 m + derived TPI/TWI',
        'screening_version': 2,
        'sample_square_m': HALF_SIZE_M * 2,
        'guard_square_m': GUARD_HALF_SIZE_M * 2,
        'dmr_export': export_info,
        'next_step': 'Inspect all v2 alternatives in open-candidates.html; visually approve only candidates fully inside homogeneous meadow/pasture.',
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not STATUS_FILE.exists():
            STATUS_FILE.write_text(
                json.dumps({'ok': False, 'generated_at_local': datetime.now(TZ).isoformat(), 'error': str(exc)}, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
        raise
