#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data' / 'terrain'
OUT = DATA_DIR / 'zabaged-watercourses.geojson'
STATUS = DATA_DIR / 'zabaged-watercourses-status.json'
BBOX = [14.38, 50.98, 14.50, 51.06]
LAYER = 'https://ags.cuzk.gov.cz/arcgis/rest/services/ZABAGED_POLOHOPIS/MapServer/93'
QUERY = LAYER + '/query'
UA = 'nove-hrabeci-observatory/1.0 (+github-actions)'


def get_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + '?' + query, headers={'User-Agent': UA, 'Accept': 'application/geo+json,application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    params = {
        'where': '1=1',
        'geometry': ','.join(str(x) for x in BBOX),
        'geometryType': 'esriGeometryEnvelope',
        'inSR': 4326,
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': 'OBJECTID,fid_zbg,jmeno,typtoku_p,vydattok_p,idvt',
        'returnGeometry': 'true',
        'outSR': 4326,
        'returnZ': 'false',
        'returnM': 'false',
        'f': 'geojson',
    }
    data = get_json(QUERY, params)
    if data.get('error'):
        raise RuntimeError('ZABAGED query error: ' + json.dumps(data['error'], ensure_ascii=False))
    feats = data.get('features') or []
    fc = {
        'type': 'FeatureCollection',
        'name': 'zabaged-watercourses-nove-hrabeci',
        'properties': {
            'provider': 'ČÚZK ZABAGED® – polohopis, vrstva Vodní tok (93)',
            'source_layer': LAYER,
            'bbox_wgs84': BBOX,
            'retrieved_at_local': now.isoformat(),
            'status': 'official topographic watercourse reference used to validate terrain-derived D8 drainage',
        },
        'features': feats,
    }
    OUT.write_text(json.dumps(fc, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    names = sorted({(f.get('properties') or {}).get('jmeno') for f in feats if (f.get('properties') or {}).get('jmeno')})
    types = {}
    for f in feats:
        p = f.get('properties') or {}
        key = f"{p.get('typtoku_p') or 'neuvedený typ'} / {p.get('vydattok_p') or 'neuvedená vydatnost'}"
        types[key] = types.get(key, 0) + 1
    status = {
        'ok': True,
        'provider': 'ČÚZK ZABAGED® – Vodní tok (93)',
        'source_layer': LAYER,
        'retrieved_at_local': now.isoformat(),
        'bbox_wgs84': BBOX,
        'features_found': len(feats),
        'named_watercourses': names,
        'type_counts': types,
        'output_file': str(OUT.relative_to(ROOT)).replace('\\', '/'),
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            'ok': False,
            'provider': 'ČÚZK ZABAGED® – Vodní tok (93)',
            'source_layer': LAYER,
            'retrieved_at_local': datetime.now(TZ).isoformat(),
            'error': str(exc),
        }
        STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))
        raise
