#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / 'config' / 'open-land-candidates.geojson'
REVIEW = ROOT / 'config' / 'open-land-review-v2.json'
OUT = ROOT / 'config' / 'open-land-provisional.geojson'
STATUS = ROOT / 'data' / 'validation' / 'open-land-provisional-status.json'
TZ = ZoneInfo('Europe/Prague')


def main():
    candidates = json.loads(CANDIDATES.read_text(encoding='utf-8'))
    review = json.loads(REVIEW.read_text(encoding='utf-8'))
    approved = {x['id']: x for x in review.get('provisionally_approved', [])}
    rejected = {x['id'] for x in review.get('visually_rejected', [])}

    features = []
    missing = []
    seen = set()
    for f in candidates.get('features', []):
        p = f.get('properties') or {}
        cid = p.get('id')
        if cid not in approved:
            continue
        seen.add(cid)
        q = dict(p)
        q['status'] = 'provisionally_approved_pending_second_visual_check'
        q['review_label'] = approved[cid].get('label')
        q['review_version'] = review.get('review_version')
        q['reviewed_at_local'] = review.get('reviewed_at_local')
        q['experimental_use'] = 'exploratory_only_until_second_visual_check'
        features.append({'type': 'Feature', 'properties': q, 'geometry': f.get('geometry')})

    for cid in approved:
        if cid not in seen:
            missing.append(cid)

    if missing:
        raise RuntimeError('Approved candidate IDs missing from candidate file: ' + ', '.join(missing))
    overlap = approved.keys() & rejected
    if overlap:
        raise RuntimeError('Candidate appears in both approved and rejected lists: ' + ', '.join(sorted(overlap)))

    role_counts = {}
    for f in features:
        role = f['properties'].get('selection_role')
        role_counts[role] = role_counts.get(role, 0) + 1

    out = {
        'type': 'FeatureCollection',
        'name': 'nove-hrabeci-open-land-provisional-v1',
        'properties': {
            'status': 'provisional_pending_second_visual_check',
            'generated_at_local': datetime.now(TZ).isoformat(),
            'source_candidates': candidates.get('name'),
            'review_version': review.get('review_version'),
            'reviewed_at_local': review.get('reviewed_at_local'),
            'purpose': 'Provisional matched open-land network for exploratory Sentinel/terrain analysis before final second visual approval.',
            'warning': 'Do not treat as permanent experimental zones until the second visual check is complete.',
            'role_counts': role_counts,
        },
        'features': features,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    STATUS.parent.mkdir(parents=True, exist_ok=True)
    status = {
        'ok': True,
        'quality_status': 'provisional_network_ready_for_exploratory_analysis',
        'generated_at_local': out['properties']['generated_at_local'],
        'candidate_source': candidates.get('name'),
        'review_version': review.get('review_version'),
        'approved_count': len(features),
        'rejected_count': len(rejected),
        'role_counts': role_counts,
        'approved_labels': [f['properties'].get('review_label') for f in features],
        'missing_experimental_roles': [r for r in ('low_position', 'south_facing_slope') if role_counts.get(r, 0) == 0],
        'output_file': str(OUT.relative_to(ROOT)).replace('\\', '/'),
        'next_step': 'Second visual check, then finalize network. In parallel, exploratory TWI and high-position analyses may use this provisional file.'
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
