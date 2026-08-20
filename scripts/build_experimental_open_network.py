#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / 'config' / 'open-land-candidates.geojson'
REVIEW = ROOT / 'config' / 'open-land-review-v2.json'
OUT = ROOT / 'config' / 'open-land-experimental.geojson'
STATUS = ROOT / 'data' / 'validation' / 'open-land-experimental-status.json'
TZ = ZoneInfo('Europe/Prague')


def main():
    candidates = json.loads(CANDIDATES.read_text(encoding='utf-8'))
    review = json.loads(REVIEW.read_text(encoding='utf-8'))
    if review.get('status') != 'second_visual_check_complete':
        raise RuntimeError('Second visual review is not complete.')

    approved = {x['id']: x for x in review.get('provisionally_approved', [])}
    rejected = {x['id'] for x in review.get('visually_rejected', [])}
    overlap = set(approved) & rejected
    if overlap:
        raise RuntimeError('Approved/rejected overlap: ' + ', '.join(sorted(overlap)))

    features = []
    seen = set()
    for f in candidates.get('features', []):
        p = f.get('properties') or {}
        cid = p.get('id')
        if cid not in approved:
            continue
        seen.add(cid)
        q = dict(p)
        q['status'] = 'approved_after_second_visual_check'
        q['review_label'] = approved[cid].get('label')
        q['review_version'] = review.get('review_version')
        q['reviewed_at_local'] = review.get('reviewed_at_local')
        q['experimental_use'] = 'stable_open_land_network'
        features.append({'type':'Feature','properties':q,'geometry':f.get('geometry')})

    missing = sorted(set(approved) - seen)
    if missing:
        raise RuntimeError('Approved candidate IDs missing: ' + ', '.join(missing))

    role_counts = {}
    for f in features:
        role = f['properties'].get('selection_role')
        role_counts[role] = role_counts.get(role, 0) + 1

    now = datetime.now(TZ).isoformat()
    out = {
        'type':'FeatureCollection',
        'name':'nove-hrabeci-open-land-experimental-v1',
        'properties':{
            'status':'stable_after_second_visual_check',
            'generated_at_local':now,
            'source_candidates':candidates.get('name'),
            'review_version':review.get('review_version'),
            'reviewed_at_local':review.get('reviewed_at_local'),
            'purpose':'Stable visually reviewed open-land sampling network for Sentinel/terrain response experiments.',
            'role_counts':role_counts,
            'known_gaps':['low_position','south_facing_slope','high_twi_backup'],
        },
        'features':features,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    STATUS.parent.mkdir(parents=True, exist_ok=True)
    status = {
        'ok':True,
        'quality_status':'stable_experimental_network_ready',
        'generated_at_local':now,
        'review_version':review.get('review_version'),
        'approved_count':len(features),
        'rejected_count':len(rejected),
        'role_counts':role_counts,
        'approved_labels':[f['properties'].get('review_label') for f in features],
        'known_gaps':['low_position','south_facing_slope','high_twi_backup'],
        'output_file':str(OUT.relative_to(ROOT)).replace('\\','/'),
        'next_step':'Backfill Sentinel NDVI/NDMI for the seasonal scene set and compare high-TWI versus low-TWI grassland response; continue targeted search for missing roles.'
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
