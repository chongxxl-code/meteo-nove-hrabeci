#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import math
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
from pyproj import CRS, Transformer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'chmi-radar.json'
LAT, LON = 51.0162, 14.4398
UA = 'nove-hrabeci-chmi-radar/1.0 (+github-actions)'

PRODUCTS = {
    'maxz': 'https://opendata.chmi.cz/meteorology/weather/radar/composite/maxz/hdf5/',
    'pseudocappi2km': 'https://opendata.chmi.cz/meteorology/weather/radar/composite/pseudocappi2km/hdf5/',
}


def get_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def as_text(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='replace')
    if isinstance(v, np.bytes_):
        return bytes(v).decode('utf-8', errors='replace')
    return str(v)


def latest_hdf(base: str):
    html = get_bytes(base).decode('utf-8', errors='ignore')
    names = re.findall(r'href=["\']([^"\']+\.hdf)["\']', html, flags=re.I)
    if not names:
        raise RuntimeError(f'ČHMÚ index neobsahuje HDF soubor: {base}')
    candidates = []
    for name in names:
        m = re.search(r'(20\d{12})', name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            candidates.append((ts, name))
        except ValueError:
            pass
    if not candidates:
        raise RuntimeError(f'ČHMÚ HDF soubory nemají rozpoznatelný čas: {base}')
    return max(candidates, key=lambda x: x[0])


def find_data_group(h5):
    found = []
    def visitor(name, obj):
        if isinstance(obj, h5py.Group) and 'data' in obj and isinstance(obj['data'], h5py.Dataset):
            q = None
            if 'what' in obj:
                q = obj['what'].attrs.get('quantity')
            found.append((name, obj, as_text(q) if q is not None else ''))
    h5.visititems(visitor)
    if not found:
        raise RuntimeError('ODIM HDF neobsahuje datovou vrstvu')
    for item in found:
        if item[2].upper() in {'DBZH', 'TH', 'RATE'}:
            return item[1]
    return found[0][1]


def attr_float(attrs, name, default=None):
    try:
        return float(attrs[name])
    except Exception:
        return default


def extract_grid(raw_bytes: bytes):
    with h5py.File(io.BytesIO(raw_bytes), 'r') as h5:
        dg = find_data_group(h5)
        arr = np.asarray(dg['data'])
        what = dg['what'].attrs if 'what' in dg else {}
        gain = attr_float(what, 'gain', 1.0)
        offset = attr_float(what, 'offset', 0.0)
        nodata = attr_float(what, 'nodata', None)
        undetect = attr_float(what, 'undetect', None)
        quantity = as_text(what.get('quantity', 'unknown')) if hasattr(what, 'get') else 'unknown'

        values = arr.astype(np.float32) * gain + offset
        valid = np.ones(arr.shape, dtype=bool)
        if nodata is not None:
            valid &= arr != nodata
        if undetect is not None:
            valid &= arr != undetect
        values[~valid] = np.nan

        if 'where' not in h5:
            raise RuntimeError('ODIM HDF postrádá /where metadata')
        where = h5['where'].attrs
        projdef = as_text(where.get('projdef', ''))
        if not projdef:
            raise RuntimeError('ODIM HDF postrádá projdef')
        xscale = attr_float(where, 'xscale')
        yscale = attr_float(where, 'yscale')
        if not xscale or not yscale:
            raise RuntimeError('ODIM HDF postrádá xscale/yscale')

        corners = {}
        for c in ('LL', 'LR', 'UL', 'UR'):
            lon = attr_float(where, c + '_lon')
            lat = attr_float(where, c + '_lat')
            if lon is not None and lat is not None:
                corners[c] = (lon, lat)
        if len(corners) < 2:
            raise RuntimeError('ODIM HDF postrádá georeferenční rohy')

        crs = CRS.from_user_input(projdef)
        transformer = Transformer.from_crs('EPSG:4326', crs, always_xy=True)
        xy = {k: transformer.transform(*v) for k, v in corners.items()}
        xs = [p[0] for p in xy.values()]
        ys = [p[1] for p in xy.values()]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        x, y = transformer.transform(LON, LAT)

        col = int(round((x - xmin) / xscale - 0.5))
        row = int(round((ymax - y) / yscale - 0.5))
        if not (0 <= row < values.shape[0] and 0 <= col < values.shape[1]):
            raise RuntimeError(f'NH leží mimo ODIM grid: row={row}, col={col}, shape={values.shape}')

        return values, row, col, float(abs(xscale)), float(abs(yscale)), quantity, projdef


def local_metrics(values, row, col, xscale_m, yscale_m):
    out = {}
    yy, xx = np.indices(values.shape)
    dy = (yy - row) * yscale_m
    dx = (xx - col) * xscale_m
    dist_km = np.hypot(dx, dy) / 1000.0
    for radius in (7, 10, 25, 50):
        mask = dist_km <= radius
        vals = values[mask]
        finite = vals[np.isfinite(vals)]
        out[f'max_dbz_{radius}km'] = round(float(np.max(finite)), 1) if finite.size else None
    strong = np.isfinite(values) & (values >= 35.0)
    out['nearest_35dbz_km'] = round(float(np.min(dist_km[strong])), 1) if np.any(strong) else None
    local = values[row, col]
    out['point_dbz'] = round(float(local), 1) if np.isfinite(local) else None
    return out


def read_product(name, base):
    ts, filename = latest_hdf(base)
    raw = get_bytes(base + filename)
    values, row, col, xs, ys, quantity, projdef = extract_grid(raw)
    metrics = local_metrics(values, row, col, xs, ys)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    return {
        'ok': True,
        'product': name,
        'source_file': filename,
        'observed_at_utc': ts.isoformat().replace('+00:00', 'Z'),
        'age_min': round(age_min, 1),
        'quantity': quantity,
        'grid_shape': [int(values.shape[0]), int(values.shape[1])],
        'grid_resolution_m': [round(xs, 1), round(ys, 1)],
        'metrics': metrics,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    products = {}
    errors = []
    for name, base in PRODUCTS.items():
        try:
            products[name] = read_product(name, base)
        except Exception as exc:
            products[name] = {'ok': False, 'product': name, 'error': str(exc)[:240]}
            errors.append(f'{name}: {exc}')

    good = [p for p in products.values() if p.get('ok')]
    latest = max((p.get('observed_at_utc') for p in good), default=None)
    status = 'operational' if len(good) == len(PRODUCTS) else ('partial' if good else 'error')
    payload = {
        'schema': 1,
        'computed_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'location': {'name': 'Nové Hraběcí', 'lat': LAT, 'lon': LON},
        'provider': 'ČHMÚ OpenData / CZRAD',
        'status': status,
        'latest_observed_at_utc': latest,
        'products': products,
        'method_note': 'Oficiální radarový kompozit ČHMÚ. MAX_Z sleduje maximum odrazivosti ve sloupci; PseudoCAPPI 2 km je používán jako bližší indikace srážek u zemského povrchu. Lokální metriky jsou počítány z ODIM HDF5 gridu 1 km.',
        'source_urls': PRODUCTS,
    }
    if errors:
        payload['errors'] = errors
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
