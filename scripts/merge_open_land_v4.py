#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'config' / 'open-land-experimental.geojson'
TARGET = ROOT / 'config' / 'open-land-north-candidates-v4.geojson'
REVIEW = ROOT / 'config' / 'open-land-review-v4.json'
STATUS = ROOT / 'data' / 'validation' / 'open-land-experimental-status.json'
TZ = ZoneInfo('Europe/Prague')


def main():
    base = json.loads(BASE.read_text(encoding='utf-8'))
    target = json.loads(TARGET.read_text(encoding='utf-8'))
    review = json.loads(REVIEW.read_text(encoding='utf-8'))

    approved = {x['id']: x for x in review.get('approved', [])}
    rejected = {x['id'] for x in review.get('rejected', [])}
    overlap = approved.keys() & rejected
    if overlap:
        raise RuntimeError('IDs in approved and rejected: ' + ', '.join(sorted(overlap)))

    target_by_id = {(f.get('properties') or {}).get('id'): f for f in target.get('features', [])}
    missing = [cid for cid in approved if cid not in target_by_id]
    if missing:
        raise RuntimeError('Approved v4 IDs missing: ' + ', '.join(missing))

    features = []
    seen = set()
    for f in base.get('features', []):
        p = f.get('properties') or {}
        cid = p.get('id')
        if cid in approved or cid in seen:
            continue
        seen.add(cid)
        features.append(f)

    for cid, decision in approved.items():
        f = target_by_id[cid]
        p = dict(f.get('properties') or {})
        p['status'] = 'approved_after_visual_check'
        p['review_label'] = decision.get('label')
        p['review_version'] = review.get('review_version')
        p['reviewed_at_local'] = review.get('reviewed_at_local')
        p['experimental_use'] = 'stable_open_land_network'
        features.append({'type': 'Feature', 'properties': p, 'geometry': f.get('geometry')})
        seen.add(cid)

    counts = Counter((f.get('properties') or {}).get('selection_role') for f in features)
    role_counts = {k: v for k, v in counts.items() if k}
    required = ['low_position','high_position','north_facing_slope','south_facing_slope','high_twi','low_twi']
    missing_roles = [r for r in required if role_counts.get(r, 0) == 0]
    now = datetime.now(TZ).isoformat()

    previous_versions = base.get('properties', {}).get('review_versions') or []
    if isinstance(previous_versions, str):
        previous_versions = [previous_versions]
    review_versions = [x for x in previous_versions if x] + [review.get('review_version')]

    out = {
        'type': 'FeatureCollection',
        'name': 'nove-hrabeci-open-land-experimental-v3',
        'properties': {
            'status': 'stable_after_v4_visual_review',
            'generated_at_local': now,
            'review_versions': review_versions,
            'purpose': 'Stable visually reviewed matched open-land network for Sentinel/terrain response experiments.',
            'role_counts': role_counts,
            'known_gaps': missing_roles,
            'sample_count': len(features),
            'note': 'Network extends experimental v2 with three manually approved independent north-facing controls N4-N6; N7-N8 remain rejected.'
        },
        'features': features,
    }
    BASE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    STATUS.parent.mkdir(parents=True, exist_ok=True)
    status = {
        'ok': not missing_roles,
        'quality_status': 'stable_experimental_network_ready' if not missing_roles else 'stable_network_with_role_gaps',
        'generated_at_local': now,
        'network': out['name'],
        'sample_count': len(features),
        'role_counts': role_counts,
        'approved_v4_labels': [x.get('label') for x in review.get('approved', [])],
        'rejected_v4_labels': [x.get('label') for x in review.get('rejected', [])],
        'missing_roles': missing_roles,
        'output_file': str(BASE.relative_to(ROOT)).replace('\\','/'),
        'next_step': 'Recompute seasonal Sentinel response and north-vs-south comparisons with four independent north-facing samples.'
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
