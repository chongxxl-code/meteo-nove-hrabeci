#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
from rasterio.features import geometry_mask, geometry_window
from rasterio.warp import Resampling, reproject, transform_geom

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
ZONES_FILE = ROOT / 'config' / 'zones.geojson'
DATA_DIR = ROOT / 'data' / 'sentinel'
STATUS_FILE = DATA_DIR / 'zone-status.json'
EARTH_SEARCH = 'https://earth-search.aws.element84.com/v1/search'
COLLECTIONS = ['sentinel-2-c1-l2a', 'sentinel-2-l2a']
BBOX = [14.38, 50.98, 14.50, 51.06]
UA = 'nove-hrabeci-observatory/1.0 (+github-actions)'
ANALYSIS_VERSION = 2

os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif,.TIF')
os.environ.setdefault('GDAL_HTTP_MULTIRANGE', 'YES')
os.environ.setdefault('AWS_NO_SIGN_REQUEST', 'YES')


def post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={'User-Agent': UA, 'Accept': 'application/geo+json,application/json', 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def search_collection(collection: str, now: datetime) -> list[dict]:
    start = now - timedelta(days=30)
    base = {
        'collections': [collection],
        'bbox': BBOX,
        'datetime': f'{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}',
        'limit': 50,
    }
    try:
        payload = dict(base)
        payload['query'] = {'eo:cloud_cover': {'lte': 40}}
        payload['sortby'] = [{'field': 'properties.datetime', 'direction': 'desc'}]
        result = post_json(EARTH_SEARCH, payload)
    except Exception:
        result = post_json(EARTH_SEARCH, base)

    candidates = []
    for f in result.get('features') or []:
        p = f.get('properties') or {}
        assets = f.get('assets') or {}
        if not all(k in assets for k in ('red', 'nir', 'swir16')):
            continue
        cloud = p.get('eo:cloud_cover')
        if isinstance(cloud, (int, float)) and cloud > 40:
            continue
        f['_source_collection'] = collection
        candidates.append(f)
    candidates.sort(key=lambda f: (f.get('properties') or {}).get('datetime') or '', reverse=True)
    return candidates


def find_scene(now: datetime) -> dict:
    errors = []
    for collection in COLLECTIONS:
        try:
            candidates = search_collection(collection, now)
            if candidates:
                return candidates[0]
            errors.append(f'{collection}: no usable scene')
        except Exception as exc:
            errors.append(f'{collection}: {exc}')
    raise RuntimeError('Earth Search returned no usable Sentinel-2 L2A scene; ' + ' | '.join(errors))


def asset_info(item: dict, key: str) -> dict:
    a = (item.get('assets') or {}).get(key)
    if not a or not a.get('href'):
        raise KeyError(f'Missing Sentinel asset: {key}')
    return a


def scale_offset(asset: dict) -> tuple[float, float]:
    bands = asset.get('raster:bands') or []
    if bands and isinstance(bands[0], dict):
        scale = bands[0].get('scale', 1.0)
        offset = bands[0].get('offset', 0.0)
        try:
            return float(scale), float(offset)
        except Exception:
            pass
    return 1.0, 0.0


def reproject_to_window(src, dst_shape, dst_transform, dst_crs, resampling: Resampling) -> np.ndarray:
    out = np.full(dst_shape, np.nan, dtype='float32')
    reproject(
        source=rasterio.band(src, 1),
        destination=out,
        src_transform=src.transform,
        src_crs=src.crs,
        src_nodata=src.nodata,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        dst_nodata=np.nan,
        resampling=resampling,
    )
    return out


def stats(values: np.ndarray) -> dict:
    x = values[np.isfinite(values)]
    if x.size == 0:
        return {'valid_pixels': 0, 'mean': None, 'median': None, 'p10': None, 'p90': None}
    return {
        'valid_pixels': int(x.size),
        'mean': round(float(np.mean(x)), 4),
        'median': round(float(np.median(x)), 4),
        'p10': round(float(np.percentile(x, 10)), 4),
        'p90': round(float(np.percentile(x, 90)), 4),
    }


def index_for_zone(a_asset: dict, b_asset: dict, geom_wgs84: dict, scl_asset: dict | None = None) -> dict:
    ahref, bhref = a_asset['href'], b_asset['href']
    ascale, aoffset = scale_offset(a_asset)
    bscale, boffset = scale_offset(b_asset)

    with rasterio.Env(AWS_NO_SIGN_REQUEST='YES'):
        with rasterio.open(bhref) as ref, rasterio.open(ahref) as src:
            gp = transform_geom('EPSG:4326', ref.crs, geom_wgs84, precision=7)
            win = geometry_window(ref, [gp], pad_x=0.2, pad_y=0.2)
            b = ref.read(1, window=win).astype('float32')
            tr = ref.window_transform(win)
            a = reproject_to_window(src, b.shape, tr, ref.crs, Resampling.bilinear)

            if ref.nodata is not None:
                b[b == ref.nodata] = np.nan
            a = a * ascale + aoffset
            b = b * bscale + boffset

            inside = geometry_mask([gp], out_shape=b.shape, transform=tr, invert=True)
            valid = inside & np.isfinite(a) & np.isfinite(b)

            if scl_asset:
                try:
                    with rasterio.open(scl_asset['href']) as sclsrc:
                        scl = reproject_to_window(sclsrc, b.shape, tr, ref.crs, Resampling.nearest)
                    bad = np.isin(np.rint(scl).astype('int16'), [0, 1, 3, 8, 9, 10, 11])
                    valid &= ~bad
                except Exception:
                    pass

            den = a + b
            valid &= np.abs(den) > 1e-8
            idx = np.full(b.shape, np.nan, dtype='float32')
            idx[valid] = (a[valid] - b[valid]) / den[valid]
            idx[(idx < -1.02) | (idx > 1.02)] = np.nan
            result = stats(idx)
            result['total_inside_pixels'] = int(np.count_nonzero(inside))
            result['valid_fraction'] = round(result['valid_pixels'] / max(1, result['total_inside_pixels']), 3)
            return result


def already_analyzed(scene_id: str, archive: Path) -> bool:
    if not archive.exists():
        return False
    for line in archive.read_text(encoding='utf-8').splitlines():
        try:
            obj = json.loads(line)
            if obj.get('scene_id') == scene_id and obj.get('analysis_version') == ANALYSIS_VERSION:
                return True
        except Exception:
            pass
    return False


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    zones = json.loads(ZONES_FILE.read_text(encoding='utf-8'))
    item = find_scene(now)
    props = item.get('properties') or {}
    scene_id = item.get('id') or 'unknown'
    scene_dt = props.get('datetime')
    cloud = props.get('eo:cloud_cover')
    collection = item.get('_source_collection') or item.get('collection')
    assets = item.get('assets') or {}

    red = asset_info(item, 'red')
    nir = asset_info(item, 'nir')
    swir = asset_info(item, 'swir16')
    ndmi_nir_key = 'nir08' if 'nir08' in assets else 'nir'
    ndmi_nir = asset_info(item, ndmi_nir_key)
    scl = assets.get('scl') if (assets.get('scl') or {}).get('href') else None

    results = []
    for feature in zones.get('features') or []:
        geom = feature.get('geometry') or {}
        if geom.get('type') not in ('Polygon', 'MultiPolygon'):
            continue
        p = feature.get('properties') or {}
        zone = {
            'id': p.get('id'),
            'name': p.get('name'),
            'class': p.get('class'),
            'zone_status': p.get('status'),
        }
        try:
            zone['ndvi'] = index_for_zone(nir, red, geom, scl)
            zone['ndmi'] = index_for_zone(ndmi_nir, swir, geom, scl)
            zone['ok'] = True
        except Exception as exc:
            zone['ok'] = False
            zone['error'] = str(exc)
        results.append(zone)

    ndvi_core = [z.get('ndvi', {}) for z in results if z.get('class') in ('open_land', 'forest') and z.get('ok')]
    ndvi_valid = all(x.get('valid_pixels', 0) > 0 and x.get('median') is not None and -1 <= x.get('median') <= 1 for x in ndvi_core) if ndvi_core else False

    record = {
        'analysis_version': ANALYSIS_VERSION,
        'quality_status': 'valid' if ndvi_valid else 'suspect',
        'provider': 'Sentinel-2 L2A via Earth Search / AWS Open Data COG',
        'collection': collection,
        'earth_search_url': EARTH_SEARCH,
        'scene_id': scene_id,
        'scene_datetime': scene_dt,
        'tile_cloud_cover_percent': cloud,
        'platform': props.get('platform'),
        'mgrs_tile': props.get('mgrs:tile') or props.get('s2:mgrs_tile'),
        'computed_at_local': now.astimezone(TZ).isoformat(),
        'zones_version': zones.get('name'),
        'ndvi_formula': '(NIR B08 - Red B04) / (NIR B08 + Red B04)',
        'ndmi_formula': f'({ndmi_nir_key} - SWIR16 B11) / ({ndmi_nir_key} + SWIR16 B11)',
        'cloud_mask': 'SCL classes 0,1,3,8,9,10,11 excluded when SCL is available',
        'zones': results,
        'note': 'Analysis v1 from sentinel-2-l2a is deprecated because radiometric offset metadata produced suspect NDVI for this scene.'
    }

    archive = DATA_DIR / f'zone-stats-v2-{now.astimezone(TZ):%Y}.jsonl'
    is_new = not already_analyzed(scene_id, archive)
    if is_new:
        with archive.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')

    status = dict(record)
    status['ok'] = ndvi_valid and any(z.get('ok') for z in results)
    status['new_scene_saved'] = is_new
    status['archive_file'] = str(archive.relative_to(ROOT)).replace('\\', '/')
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))

    if not ndvi_valid:
        raise RuntimeError('Sentinel zone analysis produced suspect NDVI in core vegetation zones')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if not STATUS_FILE.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                'ok': False,
                'analysis_version': ANALYSIS_VERSION,
                'provider': 'Sentinel-2 L2A via Earth Search / AWS Open Data COG',
                'computed_at_local': datetime.now(TZ).isoformat(),
                'error': str(exc),
            }
            STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        raise
