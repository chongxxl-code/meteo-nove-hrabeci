#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.transform import from_origin, rowcol, xy
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
ZONES_FILE = ROOT / 'config' / 'zones.geojson'
DATA_DIR = ROOT / 'data' / 'terrain'
STATUS_FILE = DATA_DIR / 'terrain-status.json'

# Wider than the Sentinel study bbox so local-relief calculations have context.
STUDY_BBOX = [14.38, 50.98, 14.50, 51.06]
MARGIN_DEG = 0.025
DST_CRS = 'EPSG:32633'  # UTM 33N, metres
RESOLUTION_M = 30.0
COPDEM_BASE = 'https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com'
SOURCE_RELEASE = 'Copernicus DEM GLO-30 Public, 2021 release'

os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif,.TIF')
os.environ.setdefault('GDAL_HTTP_MULTIRANGE', 'YES')
os.environ.setdefault('AWS_NO_SIGN_REQUEST', 'YES')


def tile_name(lat_deg: int, lon_deg: int) -> str:
    ns = 'N' if lat_deg >= 0 else 'S'
    ew = 'E' if lon_deg >= 0 else 'W'
    return f'Copernicus_DSM_COG_10_{ns}{abs(lat_deg):02d}_00_{ew}{abs(lon_deg):03d}_00_DEM'


def tile_url(lat_deg: int, lon_deg: int) -> str:
    name = tile_name(lat_deg, lon_deg)
    return f'{COPDEM_BASE}/{name}/{name}.tif'


def needed_tiles(bounds: list[float]) -> list[tuple[int, int]]:
    west, south, east, north = bounds
    lat0 = math.floor(south)
    lat1 = math.floor(np.nextafter(north, -np.inf))
    lon0 = math.floor(west)
    lon1 = math.floor(np.nextafter(east, -np.inf))
    out = []
    for lat in range(lat0, lat1 + 1):
        for lon in range(lon0, lon1 + 1):
            out.append((lat, lon))
    return out


def box_mean(arr: np.ndarray, radius_px: int) -> np.ndarray:
    valid = np.isfinite(arr)
    values = np.where(valid, arr, 0.0).astype('float64')
    counts = valid.astype('float64')

    def window_sum(a: np.ndarray) -> np.ndarray:
        p = np.pad(a, radius_px, mode='constant', constant_values=0)
        cs = np.pad(p, ((1, 0), (1, 0)), mode='constant').cumsum(0).cumsum(1)
        k = radius_px * 2 + 1
        return cs[k:, k:] - cs[:-k, k:] - cs[k:, :-k] + cs[:-k, :-k]

    sums = window_sum(values)
    nums = window_sum(counts)
    out = np.full(arr.shape, np.nan, dtype='float32')
    ok = nums > 0
    out[ok] = (sums[ok] / nums[ok]).astype('float32')
    return out


def basic_stats(values: np.ndarray) -> dict:
    x = values[np.isfinite(values)]
    if x.size == 0:
        return {'pixels': 0, 'mean': None, 'median': None, 'min': None, 'max': None, 'p10': None, 'p90': None}
    return {
        'pixels': int(x.size),
        'mean': round(float(np.mean(x)), 2),
        'median': round(float(np.median(x)), 2),
        'min': round(float(np.min(x)), 2),
        'max': round(float(np.max(x)), 2),
        'p10': round(float(np.percentile(x, 10)), 2),
        'p90': round(float(np.percentile(x, 90)), 2),
    }


def circular_aspect(values: np.ndarray, slope_values: np.ndarray) -> tuple[float | None, str | None]:
    valid = np.isfinite(values) & np.isfinite(slope_values) & (slope_values >= 3.0)
    x = values[valid]
    if x.size == 0:
        return None, None
    rad = np.deg2rad(x)
    east = float(np.mean(np.sin(rad)))
    north = float(np.mean(np.cos(rad)))
    if abs(east) < 1e-9 and abs(north) < 1e-9:
        return None, None
    deg = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
    sectors = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    sector = sectors[int((deg + 22.5) // 45) % 8]
    return round(deg, 1), sector


def terrain_position(tpi_900_median: float | None, slope_median: float | None) -> str:
    if tpi_900_median is None:
        return 'unknown'
    if tpi_900_median >= 12:
        return 'ridge/high position'
    if tpi_900_median <= -12:
        return 'valley/low position'
    if slope_median is not None and slope_median >= 8:
        return 'slope'
    return 'neutral/rolling terrain'


def candidate_from_grid(metric: np.ndarray, dem: np.ndarray, slope: np.ndarray, transform, mode: str, study_mask: np.ndarray) -> dict:
    valid = study_mask & np.isfinite(metric) & np.isfinite(dem) & np.isfinite(slope)
    if not np.any(valid):
        return {}
    work = np.where(valid, metric, np.nan)
    flat_index = np.nanargmax(work) if mode == 'max' else np.nanargmin(work)
    r, c = np.unravel_index(flat_index, work.shape)
    x, y = xy(transform, r, c, offset='center')
    # transform back to WGS84 with rasterio
    from rasterio.warp import transform as crs_transform
    lon, lat = crs_transform(DST_CRS, 'EPSG:4326', [x], [y])
    return {
        'lat': round(float(lat[0]), 6),
        'lon': round(float(lon[0]), 6),
        'elevation_m': round(float(dem[r, c]), 1),
        'slope_deg': round(float(slope[r, c]), 1),
        'tpi_900m_m': round(float(metric[r, c]), 1),
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))

    west, south, east, north = STUDY_BBOX
    fetch_bounds = [west - MARGIN_DEG, south - MARGIN_DEG, east + MARGIN_DEG, north + MARGIN_DEG]
    tile_pairs = needed_tiles(fetch_bounds)
    urls = [tile_url(lat, lon) for lat, lon in tile_pairs]

    datasets = []
    try:
        with rasterio.Env(AWS_NO_SIGN_REQUEST='YES'):
            for u in urls:
                datasets.append(rasterio.open(u))
            mosaic, src_transform = merge(datasets, bounds=fetch_bounds, nodata=np.nan)
            src = mosaic[0].astype('float32')
            src_crs = datasets[0].crs
    finally:
        for ds in datasets:
            ds.close()

    dst_bounds = transform_bounds(src_crs, DST_CRS, *fetch_bounds, densify_pts=21)
    xmin, ymin, xmax, ymax = dst_bounds
    width = max(1, int(math.ceil((xmax - xmin) / RESOLUTION_M)))
    height = max(1, int(math.ceil((ymax - ymin) / RESOLUTION_M)))
    dst_transform = from_origin(xmin, ymax, RESOLUTION_M, RESOLUTION_M)
    dem = np.full((height, width), np.nan, dtype='float32')
    reproject(
        source=src,
        destination=dem,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=DST_CRS,
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )

    # Terrain derivatives in metres. Row index grows southward, hence the minus sign for north derivative.
    grad_south, grad_east = np.gradient(dem, RESOLUTION_M, RESOLUTION_M)
    grad_north = -grad_south
    slope = np.degrees(np.arctan(np.hypot(grad_east, grad_north))).astype('float32')
    # Downslope direction, degrees clockwise from north.
    aspect = ((np.degrees(np.arctan2(-grad_east, -grad_north)) + 360.0) % 360.0).astype('float32')
    aspect[slope < 1.0] = np.nan

    r300 = max(1, round(300 / RESOLUTION_M))
    r900 = max(1, round(900 / RESOLUTION_M))
    tpi300 = (dem - box_mean(dem, r300)).astype('float32')
    tpi900 = (dem - box_mean(dem, r900)).astype('float32')

    study_geom = {
        'type': 'Polygon',
        'coordinates': [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }
    study_utm = transform_geom('EPSG:4326', DST_CRS, study_geom, precision=3)
    study_mask = geometry_mask([study_utm], out_shape=dem.shape, transform=dst_transform, invert=True)

    zone_results = []
    for feature in zones.get('features') or []:
        props = feature.get('properties') or {}
        geom = feature.get('geometry') or {}
        zid = props.get('id')
        if geom.get('type') == 'Point':
            point_utm = transform_geom('EPSG:4326', DST_CRS, geom, precision=3)
            x, y = point_utm['coordinates']
            r, c = rowcol(dst_transform, x, y)
            if 0 <= r < dem.shape[0] and 0 <= c < dem.shape[1]:
                zone_results.append({
                    'id': zid,
                    'name': props.get('name'),
                    'class': props.get('class'),
                    'geometry': 'Point',
                    'elevation_m': round(float(dem[r, c]), 1) if np.isfinite(dem[r, c]) else None,
                    'slope_deg': round(float(slope[r, c]), 1) if np.isfinite(slope[r, c]) else None,
                    'aspect_deg': round(float(aspect[r, c]), 1) if np.isfinite(aspect[r, c]) else None,
                    'tpi_300m_m': round(float(tpi300[r, c]), 1) if np.isfinite(tpi300[r, c]) else None,
                    'tpi_900m_m': round(float(tpi900[r, c]), 1) if np.isfinite(tpi900[r, c]) else None,
                })
            continue
        if geom.get('type') not in ('Polygon', 'MultiPolygon'):
            continue
        gp = transform_geom('EPSG:4326', DST_CRS, geom, precision=3)
        mask = geometry_mask([gp], out_shape=dem.shape, transform=dst_transform, invert=True)
        mask &= np.isfinite(dem)
        elev_stats = basic_stats(np.where(mask, dem, np.nan))
        slope_stats = basic_stats(np.where(mask, slope, np.nan))
        tpi300_stats = basic_stats(np.where(mask, tpi300, np.nan))
        tpi900_stats = basic_stats(np.where(mask, tpi900, np.nan))
        aspect_deg, aspect_sector = circular_aspect(aspect[mask], slope[mask])
        pos = terrain_position(tpi900_stats.get('median'), slope_stats.get('median'))
        zone_results.append({
            'id': zid,
            'name': props.get('name'),
            'class': props.get('class'),
            'zone_status': props.get('status'),
            'geometry': geom.get('type'),
            'elevation_m': elev_stats,
            'slope_deg': slope_stats,
            'dominant_aspect_deg': aspect_deg,
            'dominant_aspect_sector': aspect_sector,
            'tpi_300m_m': tpi300_stats,
            'tpi_900m_m': tpi900_stats,
            'terrain_position': pos,
        })

    study_dem = np.where(study_mask, dem, np.nan)
    study_slope = np.where(study_mask, slope, np.nan)
    study_tpi900 = np.where(study_mask, tpi900, np.nan)
    ridge = candidate_from_grid(study_tpi900, dem, slope, dst_transform, 'max', study_mask)
    valley = candidate_from_grid(study_tpi900, dem, slope, dst_transform, 'min', study_mask)

    status = {
        'ok': bool(zone_results),
        'quality_status': 'valid' if zone_results else 'no_data',
        'provider': SOURCE_RELEASE,
        'source_type': 'DSM (surface model: terrain plus vegetation/buildings), not bare-earth DTM',
        'source_registry': 'https://registry.opendata.aws/copernicus-dem/',
        'source_bucket': 's3://copernicus-dem-30m/',
        'resolution_m': RESOLUTION_M,
        'analysis_crs': DST_CRS,
        'study_bbox_wgs84': STUDY_BBOX,
        'computed_at_local': now.isoformat(),
        'tiles': [tile_name(lat, lon) for lat, lon in tile_pairs],
        'study_area': {
            'elevation_m': basic_stats(study_dem),
            'slope_deg': basic_stats(study_slope),
            'relief_range_m': round(float(np.nanmax(study_dem) - np.nanmin(study_dem)), 1),
            'strongest_tpi_ridge_candidate': ridge,
            'strongest_tpi_valley_candidate': valley,
        },
        'tpi': {
            'local_radius_m': int(r300 * RESOLUTION_M),
            'broad_radius_m': int(r900 * RESOLUTION_M),
            'interpretation': 'positive = above surrounding terrain; negative = below surrounding terrain',
        },
        'zones_version': zones.get('name'),
        'zones': zone_results,
        'next_step': 'Use DEM evidence to confirm/refine NH-RIDGE-01 and NH-VALLEY-01 before adding hydrologic flow/TWI.',
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
            'provider': SOURCE_RELEASE,
            'computed_at_local': datetime.now(TZ).isoformat(),
            'error': str(exc),
        }
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))
        raise
