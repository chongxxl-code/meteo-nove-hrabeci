#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pyflwdir
from rasterio.features import geometry_mask
from rasterio.warp import transform, transform_geom

import generate_open_land_candidates as base
from analyze_terrain_cuzk import ROOT, ZONES_FILE, SRC_CRS, STUDY_BBOX, fetch_dmr4g, box_mean
from analyze_sentinel_zones import find_scene

TZ = ZoneInfo('Europe/Prague')
OUT_FILE = ROOT / 'config' / 'open-land-target-candidates-v3.geojson'
STATUS_FILE = ROOT / 'data' / 'validation' / 'open-land-target-candidates-v3-status.json'
OLD_CANDIDATES_FILE = ROOT / 'config' / 'open-land-candidates.geojson'
EXPERIMENTAL_FILE = ROOT / 'config' / 'open-land-experimental.geojson'

TARGET_ROLES = ('low_position', 'south_facing_slope', 'high_twi')
CANDIDATES_PER_ROLE = 5
FIRST_DISPLAY_RANK = 4

# v3 deliberately sacrifices sample size for cleaner real-world placement.
SAMPLE_HALF_M = 35.0   # 70 x 70 m analysis polygon
CORE_HALF_M = 20.0     # 40 x 40 m must be entirely grassland
GUARD_HALF_M = 85.0    # 170 x 170 m wider safety neighbourhood
MIN_OLD_CENTER_DISTANCE_M = 260.0

# Tightened screening after two rounds of manual satellite inspection.
base.HALF_SIZE_M = SAMPLE_HALF_M
base.CORE_HALF_SIZE_M = CORE_HALF_M
base.GUARD_HALF_SIZE_M = GUARD_HALF_M
base.MIN_CORE_GRASS_FRACTION = 1.00
base.MIN_SAMPLE_GRASS_FRACTION = 1.00
base.MIN_GUARD_GRASS_FRACTION = 0.98
base.MIN_SENTINEL_NDVI = 0.60
base.MIN_SENTINEL_NDVI_P10 = 0.50
base.MAX_SENTINEL_NDVI_SPREAD = 0.18
base.MIN_SENTINEL_VALID = 0.98
base.MIN_CENTER_SPACING_M = MIN_OLD_CENTER_DISTANCE_M
base.EXISTING_OPEN_EXCLUSION_M = 220.0
base.MIN_ASPECT_SLOPE_DEG = 5.0
base.MAX_ASPECT_SECTOR_ERROR_DEG = 35.0
base.MIN_ASPECT_COHERENCE = 0.75
base.MIN_LOW_TPI900_M = -10.0
base.MIN_HIGH_TWI = 6.5

# candidate_entry() calls square_geom_jtsk() without an explicit half-size. The
# original function captured v2's default at definition time, so replace it for v3.
def square_geom_jtsk_v3(x: float, y: float, half: float = SAMPLE_HALF_M) -> dict:
    return {
        'type': 'Polygon',
        'coordinates': [[[x-half, y-half], [x+half, y-half], [x+half, y+half], [x-half, y+half], [x-half, y-half]]],
    }

base.square_geom_jtsk = square_geom_jtsk_v3


def old_centers_jtsk() -> list[tuple[float, float]]:
    centers: list[tuple[float, float]] = []
    for path in (OLD_CANDIDATES_FILE, EXPERIMENTAL_FILE):
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding='utf-8'))
        lons, lats = [], []
        for f in doc.get('features') or []:
            p = f.get('properties') or {}
            lon, lat = p.get('center_lon'), p.get('center_lat')
            if lon is None or lat is None:
                geom = f.get('geometry') or {}
                if geom.get('type') == 'Polygon':
                    pts = (geom.get('coordinates') or [[]])[0][:-1]
                    if pts:
                        lon = sum(float(q[0]) for q in pts) / len(pts)
                        lat = sum(float(q[1]) for q in pts) / len(pts)
            if lon is not None and lat is not None:
                lons.append(float(lon)); lats.append(float(lat))
        if lons:
            xs, ys = transform('EPSG:4326', SRC_CRS, lons, lats)
            centers.extend((float(x), float(y)) for x, y in zip(xs, ys))
    # de-duplicate near-identical centers from experimental subset of v2.
    unique: list[tuple[float, float]] = []
    for x, y in centers:
        if not any(math.hypot(x-a, y-b) < 2.0 for a, b in unique):
            unique.append((x, y))
    return unique


def main() -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))

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

    dem_flow = np.where(valid_dem, dem, base.NODATA).astype('float32')
    flw = pyflwdir.from_dem(dem_flow, nodata=base.NODATA, max_depth=-1.0, transform=tr, latlon=False, outlets='edge')
    upstream = flw.upstream_area(unit='m2').astype('float32')
    upstream[~valid_dem] = np.nan
    slope_for_twi = np.maximum(slope_rad, math.radians(0.5))
    specific = upstream / max(px, 1e-6)
    twi = np.full(dem.shape, np.nan, dtype='float32')
    ok = valid_dem & np.isfinite(upstream) & (upstream > 0)
    twi[ok] = np.log(np.maximum(specific[ok], 1e-6) / np.maximum(np.tan(slope_for_twi[ok]), 1e-6))

    wet_twi = base.robust01(twi, study_mask)
    valley300 = base.robust01(-tpi300, study_mask)
    valley900 = base.robust01(-tpi900, study_mask)
    convex300 = base.robust01(tpi300, study_mask)
    slope_norm = base.robust01(slope, study_mask, lo=2, hi=95)
    low_elev = base.robust01(dem, study_mask, invert=True)
    flatness = 1.0 - slope_norm
    southness = (1.0 - np.cos(np.deg2rad(aspect))) / 2.0
    solar = np.clip(southness * np.sin(np.minimum(slope_rad, math.radians(45))) / math.sin(math.radians(45)), 0, 1).astype('float32')

    wetness = 100 * (0.60*wet_twi + 0.25*valley300 + 0.15*valley900)
    drying = 100 * (0.45*(1-wet_twi) + 0.30*solar + 0.15*convex300 + 0.10*slope_norm)
    cold = 100 * (0.35*valley900 + 0.30*valley300 + 0.20*flatness + 0.15*low_elev)

    wc = base.worldcover_on_dem(dem.shape, tr)
    grass = study_mask & (wc == base.WORLD_COVER_CLASS_GRASSLAND)
    scene = find_scene(datetime.now(timezone.utc))

    role_scores = {
        # favour both relative depression and genuinely lower elevation within study area
        'low_position': 0.78 * valley900 + 0.22 * low_elev,
        'south_facing_slope': southness * slope_norm,
        'high_twi': 0.85 * wet_twi + 0.15 * valley300,
    }
    role_masks = {
        'low_position': grass,
        'south_facing_slope': grass & (slope >= base.MIN_ASPECT_SLOPE_DEG),
        'high_twi': grass,
    }

    labels = {
        'low_position': ('Nízká poloha', 'L'),
        'south_facing_slope': ('Jižní svah', 'S'),
        'high_twi': ('Vysoké TWI – záloha', 'T+'),
    }

    exclusion_centers = old_centers_jtsk()
    selected_centers = list(exclusion_centers)
    existing_open = base.centroid_existing_open(zones)
    selected = []
    role_counts = {}

    for role in TARGET_ROLES:
        found_for_role = 0
        for idx in base.top_indices(role_scores[role], role_masks[role], n=70000):
            r, c = np.unravel_index(int(idx), dem.shape)
            found = base.candidate_entry(
                role, r, c, role_scores[role][r, c],
                dem=dem, slope=slope, aspect=aspect, tpi300=tpi300, tpi900=tpi900,
                twi=twi, wetness=wetness, drying=drying, cold=cold, wc=wc,
                transform_affine=tr, scene=scene, selected_centers=selected_centers,
                existing_open_center=existing_open,
            )
            if not found:
                continue

            found_for_role += 1
            x = found.pop('center_x_jtsk'); y = found.pop('center_y_jtsk')
            selected_centers.append((x, y))
            props = found.pop('properties')
            rank = FIRST_DISPLAY_RANK + found_for_role - 1
            name, short = labels[role]
            safe = {'low_position':'L', 'south_facing_slope':'S', 'high_twi':'TP'}[role]
            props['candidate_rank'] = rank
            props['display_label'] = f'{short}{rank}'
            props['id'] = f'NH-OPEN-V3-{safe}{rank:02d}'
            props['name'] = f'{name} · varianta {rank}'
            props['class'] = 'open_land_target_candidate_v3'
            props['screening_version'] = 3
            props['center_lat'] = found.pop('center_lat')
            props['center_lon'] = found.pop('center_lon')
            props['distance_rule'] = f'>={MIN_OLD_CENTER_DISTANCE_M:.0f} m from every v2 candidate center'
            selected.append({'type':'Feature','properties':props,'geometry':found['geometry']})
            if found_for_role >= CANDIDATES_PER_ROLE:
                break
        role_counts[role] = found_for_role

    missing = [r for r in TARGET_ROLES if role_counts.get(r, 0) == 0]
    fc = {
        'type':'FeatureCollection',
        'name':'nove-hrabeci-open-land-target-candidates-v3',
        'properties':{
            'status':'target_candidates_need_visual_check',
            'generated_at_local':now.isoformat(),
            'target_roles':list(TARGET_ROLES),
            'purpose':'Targeted replacements for experimental-network gaps after two manual satellite-review passes.',
            'selection_rules':{
                'core_square_m':CORE_HALF_M*2,
                'sampling_square_m':SAMPLE_HALF_M*2,
                'guard_square_m':GUARD_HALF_M*2,
                'minimum_core_grassland_fraction':base.MIN_CORE_GRASS_FRACTION,
                'minimum_sample_grassland_fraction':base.MIN_SAMPLE_GRASS_FRACTION,
                'minimum_guard_grassland_fraction':base.MIN_GUARD_GRASS_FRACTION,
                'minimum_ndvi_median':base.MIN_SENTINEL_NDVI,
                'minimum_ndvi_p10':base.MIN_SENTINEL_NDVI_P10,
                'maximum_ndvi_p90_p10_spread':base.MAX_SENTINEL_NDVI_SPREAD,
                'minimum_sentinel_valid_fraction':base.MIN_SENTINEL_VALID,
                'minimum_distance_from_any_v2_candidate_m':MIN_OLD_CENTER_DISTANCE_M,
                'south_aspect_max_error_deg':base.MAX_ASPECT_SECTOR_ERROR_DEG,
                'minimum_aspect_coherence':base.MIN_ASPECT_COHERENCE,
                'low_position_tpi900_max_m':base.MIN_LOW_TPI900_M,
                'high_twi_minimum':base.MIN_HIGH_TWI,
                'candidates_per_role_target':CANDIDATES_PER_ROLE,
            },
            'old_centers_excluded':len(exclusion_centers),
            'note':'These are deliberately new locations, not recycled v2 candidates. Manual satellite approval remains mandatory.',
        },
        'features':selected,
    }
    OUT_FILE.write_text(json.dumps(fc, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

    status = {
        'ok': bool(selected),
        'quality_status':'target_candidates_v3_ready_for_visual_check' if not missing else 'target_candidates_v3_partial',
        'generated_at_local':now.isoformat(),
        'candidate_count':len(selected),
        'role_counts':role_counts,
        'missing_roles':missing,
        'excluded_old_candidate_centers':len(exclusion_centers),
        'output_file':str(OUT_FILE.relative_to(ROOT)).replace('\\','/'),
        'sentinel_scene_id':scene.get('id'),
        'screening_version':3,
        'sample_square_m':SAMPLE_HALF_M*2,
        'guard_square_m':GUARD_HALF_M*2,
        'dmr_export':export_info,
        'next_step':'Inspect target-candidates.html on satellite imagery and approve only fully homogeneous meadow/pasture cores.',
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
