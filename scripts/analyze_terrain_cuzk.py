#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import rowcol, xy
from rasterio.warp import transform_bounds, transform_geom

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
ZONES_FILE = ROOT / 'config' / 'zones.geojson'
DATA_DIR = ROOT / 'data' / 'terrain'
STATUS_FILE = DATA_DIR / 'terrain-cuzk-status.json'

STUDY_BBOX = [14.38, 50.98, 14.50, 51.06]
SRC_CRS = 'EPSG:5514'  # S-JTSK / Krovak East North
PIXEL_M = 5.0
CONTEXT_MARGIN_M = 1100.0  # enough context for 900 m TPI
SERVICE = 'https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr4g/ImageServer'
EXPORT_URL = SERVICE + '/exportImage'
UA = 'nove-hrabeci-observatory/1.0 (+github-actions)'


def get_json(url: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + '?' + q, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))


def download(url: str, path: Path) -> None:
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=180) as r, path.open('wb') as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


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
    return round(deg, 1), sectors[int((deg + 22.5) // 45) % 8]


def terrain_position(tpi900: float | None, slope_med: float | None) -> str:
    if tpi900 is None:
        return 'unknown'
    if tpi900 >= 12:
        return 'ridge/high position'
    if tpi900 <= -12:
        return 'valley/low position'
    if slope_med is not None and slope_med >= 8:
        return 'slope'
    return 'neutral/rolling terrain'


def candidate(metric: np.ndarray, dem: np.ndarray, slope: np.ndarray, transform, mask: np.ndarray, mode: str) -> dict:
    valid = mask & np.isfinite(metric) & np.isfinite(dem) & np.isfinite(slope)
    work = np.where(valid, metric, np.nan)
    idx = np.nanargmax(work) if mode == 'max' else np.nanargmin(work)
    r, c = np.unravel_index(idx, work.shape)
    x, y = xy(transform, r, c, offset='center')
    from rasterio.warp import transform as crs_transform
    lon, lat = crs_transform(SRC_CRS, 'EPSG:4326', [x], [y])
    return {
        'lat': round(float(lat[0]), 6),
        'lon': round(float(lon[0]), 6),
        'elevation_m_bpv': round(float(dem[r, c]), 1),
        'slope_deg': round(float(slope[r, c]), 1),
        'tpi_900m_m': round(float(metric[r, c]), 1),
    }


def fetch_dmr4g() -> tuple[np.ndarray, object, dict]:
    xmin, ymin, xmax, ymax = transform_bounds('EPSG:4326', SRC_CRS, *STUDY_BBOX, densify_pts=41)
    xmin -= CONTEXT_MARGIN_M
    ymin -= CONTEXT_MARGIN_M
    xmax += CONTEXT_MARGIN_M
    ymax += CONTEXT_MARGIN_M

    width = int(math.ceil((xmax - xmin) / PIXEL_M))
    height = int(math.ceil((ymax - ymin) / PIXEL_M))
    if width > 15000 or height > 4100:
        raise RuntimeError(f'Requested image too large for CUZK service: {width}x{height}')

    params = {
        'bbox': f'{xmin},{ymin},{xmax},{ymax}',
        'bboxSR': 5514,
        'imageSR': 5514,
        'size': f'{width},{height}',
        'format': 'tiff',
        'pixelType': 'F32',
        'interpolation': 'RSP_BilinearInterpolation',
        'returnSquarePixels': 'true',
        'f': 'json',
    }
    meta = get_json(EXPORT_URL, params)
    if 'error' in meta:
        raise RuntimeError('CUZK exportImage error: ' + json.dumps(meta['error'], ensure_ascii=False))
    href = meta.get('href')
    if not href:
        raise RuntimeError('CUZK exportImage returned no href: ' + json.dumps(meta, ensure_ascii=False)[:1000])

    with tempfile.TemporaryDirectory() as td:
        tif = Path(td) / 'dmr4g.tif'
        download(href, tif)
        with rasterio.open(tif) as ds:
            dem = ds.read(1).astype('float32')
            transform = ds.transform
            crs = ds.crs
            nodata = ds.nodata
            if nodata is not None:
                dem[dem == nodata] = np.nan
            if crs is None:
                raise RuntimeError('CUZK output TIFF has no CRS')
            epsg = crs.to_epsg()
            crs_text = str(crs) + ' ' + crs.to_wkt()
            if epsg != 5514 and '5514' not in crs_text:
                raise RuntimeError(f'Unexpected CUZK output CRS: {crs}')
            actual_px = (abs(float(transform.a)) + abs(float(transform.e))) / 2.0
            info = {
                'requested_size_px': [width, height],
                'returned_size_px': [ds.width, ds.height],
                'actual_pixel_m': round(actual_px, 3),
                'reported_crs': str(crs),
                'export_href_host': urllib.parse.urlparse(href).netloc,
            }
            return dem, transform, info


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))
    now = datetime.now(TZ)

    dem, transform, export_info = fetch_dmr4g()
    px_x = abs(float(transform.a))
    px_y = abs(float(transform.e))

    grad_south, grad_east = np.gradient(dem, px_y, px_x)
    grad_north = -grad_south
    slope = np.degrees(np.arctan(np.hypot(grad_east, grad_north))).astype('float32')
    aspect = ((np.degrees(np.arctan2(-grad_east, -grad_north)) + 360.0) % 360.0).astype('float32')
    aspect[slope < 1.0] = np.nan

    r300 = max(1, round(300 / ((px_x + px_y) / 2)))
    r900 = max(1, round(900 / ((px_x + px_y) / 2)))
    tpi300 = (dem - box_mean(dem, r300)).astype('float32')
    tpi900 = (dem - box_mean(dem, r900)).astype('float32')

    west, south, east, north = STUDY_BBOX
    study_geom = {'type': 'Polygon', 'coordinates': [[[west,south],[east,south],[east,north],[west,north],[west,south]]]}
    study_jtsk = transform_geom('EPSG:4326', SRC_CRS, study_geom, precision=3)
    study_mask = geometry_mask([study_jtsk], out_shape=dem.shape, transform=transform, invert=True) & np.isfinite(dem)

    results = []
    for feature in zones.get('features') or []:
        p = feature.get('properties') or {}
        geom = feature.get('geometry') or {}
        zid = p.get('id')
        if geom.get('type') == 'Point':
            gp = transform_geom('EPSG:4326', SRC_CRS, geom, precision=3)
            x, y = gp['coordinates']
            r, c = rowcol(transform, x, y)
            if 0 <= r < dem.shape[0] and 0 <= c < dem.shape[1]:
                results.append({
                    'id': zid, 'name': p.get('name'), 'class': p.get('class'), 'geometry': 'Point',
                    'elevation_m_bpv': round(float(dem[r,c]), 2) if np.isfinite(dem[r,c]) else None,
                    'slope_deg': round(float(slope[r,c]), 2) if np.isfinite(slope[r,c]) else None,
                    'aspect_deg': round(float(aspect[r,c]), 1) if np.isfinite(aspect[r,c]) else None,
                    'tpi_300m_m': round(float(tpi300[r,c]), 2) if np.isfinite(tpi300[r,c]) else None,
                    'tpi_900m_m': round(float(tpi900[r,c]), 2) if np.isfinite(tpi900[r,c]) else None,
                })
            continue
        if geom.get('type') not in ('Polygon', 'MultiPolygon'):
            continue
        gp = transform_geom('EPSG:4326', SRC_CRS, geom, precision=3)
        mask = geometry_mask([gp], out_shape=dem.shape, transform=transform, invert=True) & np.isfinite(dem)
        elev = basic_stats(np.where(mask, dem, np.nan))
        sl = basic_stats(np.where(mask, slope, np.nan))
        t300 = basic_stats(np.where(mask, tpi300, np.nan))
        t900 = basic_stats(np.where(mask, tpi900, np.nan))
        adeg, asector = circular_aspect(aspect[mask], slope[mask])
        results.append({
            'id': zid, 'name': p.get('name'), 'class': p.get('class'), 'zone_status': p.get('status'),
            'geometry': geom.get('type'), 'elevation_m_bpv': elev, 'slope_deg': sl,
            'dominant_aspect_deg': adeg, 'dominant_aspect_sector': asector,
            'tpi_300m_m': t300, 'tpi_900m_m': t900,
            'terrain_position': terrain_position(t900.get('median'), sl.get('median')),
        })

    ridge = candidate(tpi900, dem, slope, transform, study_mask, 'max')
    valley = candidate(tpi900, dem, slope, transform, study_mask, 'min')
    study_dem = np.where(study_mask, dem, np.nan)

    status = {
        'ok': bool(results),
        'quality_status': 'valid' if results else 'no_data',
        'provider': 'ČÚZK DMR 4G ImageServer',
        'service_url': SERVICE,
        'source_type': 'DMR/terrain relief model from airborne laser scanning; regular 5 x 5 m grid',
        'vertical_reference': 'Balt po vyrovnání (Bpv)',
        'nominal_resolution_m': 5.0,
        'analysis_crs': SRC_CRS,
        'study_bbox_wgs84': STUDY_BBOX,
        'computed_at_local': now.isoformat(),
        'export': export_info,
        'study_area': {
            'elevation_m_bpv': basic_stats(study_dem),
            'relief_range_m': round(float(np.nanmax(study_dem) - np.nanmin(study_dem)), 2),
            'strongest_tpi_ridge_candidate': ridge,
            'strongest_tpi_valley_candidate': valley,
        },
        'tpi': {
            'local_radius_m': round(r300 * ((px_x + px_y) / 2), 1),
            'broad_radius_m': round(r900 * ((px_x + px_y) / 2), 1),
            'interpretation': 'positive = above surrounding terrain; negative = below surrounding terrain',
        },
        'zones_version': zones.get('name'),
        'zones': results,
        'next_step': 'If DMR4G validation is plausible, add depression handling, D8 flow direction/accumulation and TWI on the 5 m terrain grid.',
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
            'provider': 'ČÚZK DMR 4G ImageServer',
            'service_url': SERVICE,
            'computed_at_local': datetime.now(TZ).isoformat(),
            'error': str(exc),
        }
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))
        raise
