#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

LAT = 51.0162
LON = 14.4398
ZOOM = 7
TILE = 256
GRID = 3
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'radar-nowcast.json'
UA = 'nove-hrabeci-radar-nowcast/0.2 (+github-actions)'
MAX_SHIFT_PX = 55
TARGET_RADIUS_KM = 7.0
MAX_ETA_MIN = 120.0
MIN_RAIN_PIXELS = 120


def get_bytes(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url: str):
    return json.loads(get_bytes(url).decode('utf-8'))


def lonlat_to_tile_px(lat: float, lon: float, z: int):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    latr = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(latr)) / math.pi) / 2.0 * n
    return x, y


def meters_per_pixel(lat: float, z: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


def fetch_frame(host: str, path: str):
    tx, ty = lonlat_to_tile_px(LAT, LON, ZOOM)
    cx, cy = int(math.floor(tx)), int(math.floor(ty))
    x0, y0 = cx - 1, cy - 1
    canvas = Image.new('RGBA', (GRID * TILE, GRID * TILE), (0, 0, 0, 0))
    for gy in range(GRID):
        for gx in range(GRID):
            x, y = x0 + gx, y0 + gy
            url = f'{host}{path}/256/{ZOOM}/{x}/{y}/2/1_1.png'
            tile = Image.open(io.BytesIO(get_bytes(url))).convert('RGBA')
            canvas.paste(tile, (gx * TILE, gy * TILE))
    arr = np.asarray(canvas, dtype=np.float32)
    alpha = arr[:, :, 3] / 255.0
    rgb = arr[:, :, :3]
    chroma = np.max(rgb, axis=2) - np.min(rgb, axis=2)
    intensity = alpha * np.clip((chroma + 20.0) / 180.0, 0.0, 1.0)
    intensity[intensity < 0.08] = 0.0
    target_x = (tx - x0) * TILE
    target_y = (ty - y0) * TILE
    return intensity, float(target_x), float(target_y)


def phase_shift(a: np.ndarray, b: np.ndarray):
    win_y = np.hanning(a.shape[0])[:, None]
    win_x = np.hanning(a.shape[1])[None, :]
    aw = (a - a.mean()) * win_y * win_x
    bw = (b - b.mean()) * win_y * win_x
    fa = np.fft.fft2(aw)
    fb = np.fft.fft2(bw)
    cross = fb * np.conj(fa)
    denom = np.abs(cross)
    cross /= np.where(denom == 0, 1.0, denom)
    corr = np.abs(np.fft.ifft2(cross))
    py, px = np.unravel_index(np.argmax(corr), corr.shape)
    h, w = corr.shape
    dy = float(py if py <= h // 2 else py - h)
    dx = float(px if px <= w // 2 else px - w)
    peak = float(corr[py, px])
    corr2 = corr.copy()
    y1, y2 = max(0, py - 3), min(h, py + 4)
    x1, x2 = max(0, px - 3), min(w, px + 4)
    corr2[y1:y2, x1:x2] = 0
    second = float(np.max(corr2))
    ratio = peak / max(second, 1e-9)
    return dx, dy, ratio


def angle_diff_deg(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def compass_from_vector(dx, dy):
    bearing = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
    names = ['S', 'SV', 'V', 'JV', 'J', 'JZ', 'Z', 'SZ']
    return bearing, names[int((bearing + 22.5) // 45) % 8]


def classify(latest, tx, ty, vx_px_min, vy_px_min, mpp):
    yy, xx = np.nonzero(latest >= 0.10)
    if len(xx) < MIN_RAIN_PIXELS:
        return {'relation': 'bez_srazek_v_dosahu', 'eta_min': None, 'nearest_km': None}
    vals = latest[yy, xx]
    dx = tx - xx.astype(float)
    dy = ty - yy.astype(float)
    dist_px = np.hypot(dx, dy)
    nearest_km = float(np.min(dist_px) * mpp / 1000.0)
    speed_px_min = math.hypot(vx_px_min, vy_px_min)
    if speed_px_min < 0.02:
        return {'relation': 'pohyb_neurcity', 'eta_min': None, 'nearest_km': round(nearest_km, 1)}
    ux, uy = vx_px_min / speed_px_min, vy_px_min / speed_px_min
    along = dx * ux + dy * uy
    cross = np.abs(dx * (-uy) + dy * ux)
    radius_px = TARGET_RADIUS_KM * 1000.0 / mpp
    candidates = (along >= 0) & (cross <= radius_px) & (vals >= 0.10)
    eta = along[candidates] / speed_px_min if np.any(candidates) else np.array([])
    eta = eta[(eta >= 0) & (eta <= MAX_ETA_MIN)]
    if len(eta):
        return {'relation': 'miri_na_lokalitu', 'eta_min': int(round(float(np.min(eta)))), 'nearest_km': round(nearest_km, 1)}
    if nearest_km <= TARGET_RADIUS_KM:
        return {'relation': 'nad_nebo_u_lokality', 'eta_min': 0, 'nearest_km': round(nearest_km, 1)}
    return {'relation': 'miji_lokalitu', 'eta_min': None, 'nearest_km': round(nearest_km, 1)}


def main():
    OUT.parent.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    out = {
        'schema': 1,
        'computed_at_utc': now.isoformat().replace('+00:00', 'Z'),
        'location': {'lat': LAT, 'lon': LON},
        'status': 'insufficient_data',
        'confidence': 'low',
        'eta_min': None,
        'relation': 'neurceno',
        'note': 'ETA se zveřejní jen při stabilním radarovém pohybu.'
    }
    try:
        meta = get_json('https://api.rainviewer.com/public/weather-maps.json')
        host = meta.get('host') or 'https://tilecache.rainviewer.com'
        frames = ((meta.get('radar') or {}).get('past') or [])[-6:]
        if len(frames) < 4:
            raise RuntimeError('méně než 4 radarové snímky')

        imgs = []
        times = []
        tx = ty = None
        for frame in frames:
            img, tx, ty = fetch_frame(host, frame['path'])
            imgs.append(img)
            times.append(int(frame['time']))

        shifts = []
        for i in range(1, len(imgs)):
            dt_min = (times[i] - times[i - 1]) / 60.0
            if dt_min <= 0:
                continue
            if np.count_nonzero(imgs[i - 1] > 0.08) < MIN_RAIN_PIXELS or np.count_nonzero(imgs[i] > 0.08) < MIN_RAIN_PIXELS:
                continue
            dx, dy, ratio = phase_shift(imgs[i - 1], imgs[i])
            if math.hypot(dx, dy) > MAX_SHIFT_PX:
                continue
            shifts.append({'dx': dx, 'dy': dy, 'dt_min': dt_min, 'ratio': ratio})

        if len(shifts) < 3:
            out.update(status='no_reliable_motion', relation='pohyb_neurcity', note='Radar je načtený, ale zatím není dost stabilních dvojic snímků pro spolehlivý pohyb.')
        else:
            vx = np.array([s['dx'] / s['dt_min'] for s in shifts])
            vy = np.array([s['dy'] / s['dt_min'] for s in shifts])
            ratios = np.array([s['ratio'] for s in shifts])
            mvx, mvy = float(np.median(vx)), float(np.median(vy))
            speeds = np.hypot(vx, vy)
            speed_med = float(np.median(speeds))
            speed_spread = float(np.median(np.abs(speeds - speed_med)))
            angles = [(math.degrees(math.atan2(x, -y)) + 360.0) % 360.0 for x, y in zip(vx, vy)]
            angle_med = float(np.median(angles))
            angle_spread = float(np.median([angle_diff_deg(a, angle_med) for a in angles]))
            mpp = meters_per_pixel(LAT, ZOOM)
            speed_kmh = math.hypot(mvx, mvy) * mpp * 60.0 / 1000.0
            bearing, compass = compass_from_vector(mvx, mvy)
            relation = classify(imgs[-1], tx, ty, mvx, mvy, mpp)
            score = 0
            if float(np.median(ratios)) >= 1.08: score += 1
            if angle_spread <= 25.0: score += 1
            if speed_spread <= max(0.18 * speed_med, 0.08): score += 1
            if 3.0 <= speed_kmh <= 120.0: score += 1
            confidence = 'high' if score == 4 else ('medium' if score >= 3 else 'low')
            reliable = confidence in ('medium', 'high')
            eta = relation['eta_min'] if reliable and relation['relation'] == 'miri_na_lokalitu' else None
            out.update(
                status='operational' if reliable else 'motion_uncertain',
                confidence=confidence,
                relation=relation['relation'],
                eta_min=eta,
                nearest_precip_km=relation['nearest_km'],
                motion={
                    'bearing_deg': round(bearing, 1),
                    'compass': compass,
                    'speed_kmh': round(speed_kmh, 1),
                    'pair_count': len(shifts),
                    'angle_spread_deg': round(angle_spread, 1),
                    'speed_spread_px_min': round(speed_spread, 3),
                    'phase_peak_ratio_median': round(float(np.median(ratios)), 3),
                },
                frames={'count': len(frames), 'first_unix': times[0], 'latest_unix': times[-1]},
                note='ETA je zobrazena jen při stabilním směru a rychlosti.' if reliable else 'Pohyb je zatím příliš proměnlivý; ETA se nezobrazuje.'
            )
    except Exception as exc:
        out.update(status='error', relation='neurceno', error=str(exc), note='Nowcast se nepodařilo spočítat; veřejný radar na webu zůstává nezávisle funkční.')

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    main()
