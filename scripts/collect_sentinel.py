#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
ARCHIVE = DATA / 'sentinel'
STATUS = DATA / 'sentinel-status.json'
STAC_URL = 'https://stac.dataspace.copernicus.eu/v1/search'
COLLECTION = 'sentinel-2-l2a'
BBOX = [14.38, 50.98, 14.50, 51.06]
UA = 'nove-hrabeci-observatory/1.0 (+github-actions)'


def post_json(url: str, payload: dict):
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            'User-Agent': UA,
            'Accept': 'application/geo+json,application/json',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode('utf-8'))


def existing_ids(path: Path):
    ids = set()
    if not path.exists():
        return ids
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            obj = json.loads(line)
            if obj.get('id'):
                ids.add(obj['id'])
        except Exception:
            pass
    return ids


def compact_item(feature: dict):
    props = feature.get('properties') or {}
    links = feature.get('links') or []
    self_url = next((x.get('href') for x in links if x.get('rel') == 'self'), None)
    product_url = next((x.get('href') for x in links if x.get('rel') in ('product', 'alternate')), None)
    return {
        'id': feature.get('id'),
        'collection': feature.get('collection'),
        'datetime': props.get('datetime'),
        'cloud_cover_percent': props.get('eo:cloud_cover'),
        'snow_cover_percent': props.get('eo:snow_cover'),
        'platform': props.get('platform'),
        'constellation': props.get('constellation'),
        'mgrs_tile': props.get('s2:mgrs_tile') or props.get('mgrs:utm_zone'),
        'bbox': feature.get('bbox'),
        'self_url': self_url,
        'product_url': product_url,
    }


def main():
    DATA.mkdir(exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=45)
    payload = {
        'collections': [COLLECTION],
        'bbox': BBOX,
        'datetime': f'{start:%Y-%m-%dT%H:%M:%SZ}/{now:%Y-%m-%dT%H:%M:%SZ}',
        'query': {'eo:cloud_cover': {'lte': 90}},
        'sortby': [{'field': 'properties.datetime', 'direction': 'desc'}],
        'limit': 30,
    }
    result = post_json(STAC_URL, payload)
    features = result.get('features') or []
    items = [compact_item(f) for f in features if f.get('id')]
    items.sort(key=lambda x: x.get('datetime') or '', reverse=True)

    archive = ARCHIVE / f'scenes-{now.astimezone(TZ):%Y-%m}.jsonl'
    known = existing_ids(archive)
    new_items = [x for x in items if x['id'] not in known]
    if new_items:
        with archive.open('a', encoding='utf-8') as f:
            for item in reversed(new_items):
                f.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')

    usable = [x for x in items if isinstance(x.get('cloud_cover_percent'), (int, float))]
    best = min(usable, key=lambda x: (x['cloud_cover_percent'], -(datetime.fromisoformat(x['datetime'].replace('Z', '+00:00')).timestamp()))) if usable else (items[0] if items else None)
    latest = items[0] if items else None

    status = {
        'ok': bool(items),
        'provider': 'Copernicus Data Space Ecosystem STAC',
        'collection': COLLECTION,
        'stac_url': STAC_URL,
        'study_bbox': BBOX,
        'reference_point': {'lat': 51.0162, 'lon': 14.4398},
        'checked_at_local': now.astimezone(TZ).isoformat(),
        'search_window_days': 45,
        'items_found': len(items),
        'new_items_saved': len(new_items),
        'archive_file': f'data/sentinel/{archive.name}',
        'latest_scene': latest,
        'best_low_cloud_scene': best,
        'note': 'Scene metadata only. NDVI/NDMI and landscape-zone statistics are the next step.',
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        payload = {
            'ok': False,
            'provider': 'Copernicus Data Space Ecosystem STAC',
            'collection': COLLECTION,
            'stac_url': STAC_URL,
            'study_bbox': BBOX,
            'checked_at_local': datetime.now(TZ).isoformat(),
            'error': str(exc),
        }
        STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))
