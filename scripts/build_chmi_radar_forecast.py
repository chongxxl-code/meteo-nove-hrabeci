#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from build_chmi_radar import get_bytes, extract_grid, local_metrics

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'chmi-radar-forecast.json'

PRODUCTS = {
    'maxz': 'https://opendata.chmi.cz/meteorology/weather/radar/composite/fct_maxz/hdf5/',
    'pseudocappi2km': 'https://opendata.chmi.cz/meteorology/weather/radar/composite/fct_pseudocappi2km/hdf5/',
}


def latest_tar(base: str):
    html = get_bytes(base).decode('utf-8', errors='ignore')
    names = re.findall(r'href=["\']([^"\']+\.tar)["\']', html, flags=re.I)
    candidates = []
    for name in names:
        m = re.search(r'(20\d{6})\.(\d{4})\.ft60s10\.tar$', name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
            candidates.append((ts, name))
        except ValueError:
            pass
    if not candidates:
        raise RuntimeError(f'ČHMÚ index neobsahuje rozpoznatelný forecast TAR: {base}')
    return max(candidates, key=lambda x: x[0])


def member_lead_min(member_name: str, calc_ts: datetime):
    # Current CHMI archives encode the forecast valid time in the member filename.
    # Prefer that over a particular suffix spelling; it is also consistent with the
    # published specification and survives filename-format variations.
    stamps = re.findall(r'(20\d{12})', member_name)
    for stamp in reversed(stamps):
        try:
            valid = datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lead = int(round((valid - calc_ts).total_seconds() / 60.0))
        if lead in (10, 20, 30, 40, 50, 60):
            return lead, valid

    # Fallback for archives that explicitly encode fct10/fct20 etc.
    m = re.search(r'(?:fct|ft)[_.-]?(10|20|30|40|50|60)(?:\D|$)', member_name, flags=re.I)
    if m:
        lead = int(m.group(1))
        valid = datetime.fromtimestamp(calc_ts.timestamp() + lead * 60, tz=timezone.utc)
        return lead, valid
    return None, None


def read_tar_product(name: str, base: str):
    calc_ts, filename = latest_tar(base)
    raw_tar = get_bytes(base + filename, timeout=35)
    horizons = []
    member_names = []
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode='r:*') as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        member_names = [m.name for m in members[:12]]
        for member in members:
            if not member.name.lower().endswith('.hdf'):
                continue
            lead, valid_ts = member_lead_min(member.name, calc_ts)
            if lead is None:
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            hdf = fh.read()
            values, row, col, proj_x, proj_y, ground_x, ground_y, quantity, projdef = extract_grid(hdf)
            metrics = local_metrics(values, row, col, ground_x, ground_y)
            horizons.append({
                'lead_min': lead,
                'valid_at_utc': valid_ts.isoformat().replace('+00:00', 'Z'),
                'quantity': quantity,
                'metrics': metrics,
            })
    # De-duplicate if an archive happens to contain multiple representations.
    by_lead = {h['lead_min']: h for h in horizons}
    horizons = [by_lead[k] for k in sorted(by_lead)]
    if len(horizons) < 4:
        sample = ', '.join(member_names[:6])
        raise RuntimeError(f'{name}: forecast TAR obsahuje jen {len(horizons)} použitelných horizontů; sample members: {sample}')

    first_echo = next((h['lead_min'] for h in horizons if (h['metrics'].get('max_dbz_7km') or -99) >= 35), None)
    first_strong = next((h['lead_min'] for h in horizons if (h['metrics'].get('max_dbz_10km') or -99) >= 45), None)
    peak_25 = max((h['metrics'].get('max_dbz_25km') for h in horizons if h['metrics'].get('max_dbz_25km') is not None), default=None)
    age_min = (datetime.now(timezone.utc) - calc_ts).total_seconds() / 60.0
    return {
        'ok': True,
        'product': name,
        'source_file': filename,
        'calculation_time_utc': calc_ts.isoformat().replace('+00:00', 'Z'),
        'age_min': round(age_min, 1),
        'first_35dbz_within_7km_lead_min': first_echo,
        'first_45dbz_within_10km_lead_min': first_strong,
        'peak_dbz_25km_next60': round(float(peak_25), 1) if peak_25 is not None else None,
        'horizons': horizons,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    products = {}
    errors = []
    for name, base in PRODUCTS.items():
        try:
            products[name] = read_tar_product(name, base)
        except Exception as exc:
            products[name] = {'ok': False, 'product': name, 'error': str(exc)[:500]}
            errors.append(f'{name}: {exc}')

    good = [p for p in products.values() if p.get('ok')]
    status = 'operational' if len(good) == len(PRODUCTS) else ('partial' if good else 'error')
    calc_times = [p.get('calculation_time_utc') for p in good if p.get('calculation_time_utc')]
    payload = {
        'schema': 1,
        'computed_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'provider': 'ČHMÚ OpenData / CZRAD COTREC',
        'status': status,
        'latest_calculation_time_utc': max(calc_times) if calc_times else None,
        'products': products,
        'method_note': 'Oficiální ČHMÚ extrapolační radarový forecast COTREC na +10 až +60 min. Přesouvá poslední radarová echa podle pohybových vektorů; nepředpovídá změny jejich intenzity. Používá se jako nezávislá kontrola našeho RainViewer ETA, ne jako jeho náhrada.',
        'source_urls': PRODUCTS,
    }
    if errors:
        payload['errors'] = errors
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()
