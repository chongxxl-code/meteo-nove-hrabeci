#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data' / 'validation' / 'open-land-experiment-2026.json'
OUT = ROOT / 'data' / 'validation' / 'open-land-disturbance-2026.json'
STATUS = ROOT / 'data' / 'validation' / 'open-land-disturbance-status.json'
TZ = ZoneInfo('Europe/Prague')

MIN_PRE_NDVI = 0.50
DROP_THRESHOLD = -0.12
STRONG_DROP_THRESHOLD = -0.20
RECOVERY_GAIN = 0.10
MAX_RECOVERY_SCENES = 2


def main():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    scenes = data.get('scenes') or []
    by_sample = {}
    for scene in scenes:
        sid = scene.get('scene_id')
        dt = scene.get('scene_datetime')
        for z in scene.get('zones') or []:
            if not z.get('ok'):
                continue
            ndvi = (z.get('ndvi') or {}).get('median')
            if ndvi is None:
                continue
            by_sample.setdefault(z.get('id'), {'label': z.get('label'), 'role': z.get('role'), 'series': []})['series'].append({
                'scene_id': sid, 'scene_datetime': dt, 'ndvi': float(ndvi)
            })

    events = []
    disturbed_keys = set()
    sample_summary = []
    for sample_id, obj in by_sample.items():
        series = sorted(obj['series'], key=lambda x: x['scene_datetime'] or '')
        sample_events = []
        for i in range(1, len(series)):
            prev, cur = series[i-1], series[i]
            delta = cur['ndvi'] - prev['ndvi']
            if prev['ndvi'] < MIN_PRE_NDVI or delta > DROP_THRESHOLD:
                continue
            recovery = None
            recovery_idx = None
            for j in range(i+1, min(len(series), i+1+MAX_RECOVERY_SCENES)):
                gain = series[j]['ndvi'] - cur['ndvi']
                if gain >= RECOVERY_GAIN:
                    recovery = {
                        'scene_id': series[j]['scene_id'],
                        'scene_datetime': series[j]['scene_datetime'],
                        'ndvi': round(series[j]['ndvi'], 4),
                        'gain_from_drop': round(gain, 4),
                    }
                    recovery_idx = j
                    break
            ev = {
                'sample_id': sample_id,
                'label': obj.get('label'),
                'role': obj.get('role'),
                'previous_scene_id': prev['scene_id'],
                'previous_datetime': prev['scene_datetime'],
                'previous_ndvi': round(prev['ndvi'], 4),
                'event_scene_id': cur['scene_id'],
                'event_datetime': cur['scene_datetime'],
                'event_ndvi': round(cur['ndvi'], 4),
                'delta_ndvi': round(delta, 4),
                'severity': 'strong' if delta <= STRONG_DROP_THRESHOLD else 'moderate',
                'recovery_within_2_scenes': recovery,
                'classification': 'possible_management_or_disturbance',
                'interpretation': 'Abrupt NDVI decline; may reflect mowing/grazing or another vegetation disturbance. It is not proof of management.'
            }
            events.append(ev); sample_events.append(ev)
            # Conservative filter window: event acquisition itself, plus following acquisition
            # only when it remains before a detected recovery.
            disturbed_keys.add((sample_id, cur['scene_id']))
            if recovery_idx is not None:
                for k in range(i+1, recovery_idx):
                    disturbed_keys.add((sample_id, series[k]['scene_id']))
        sample_summary.append({
            'sample_id': sample_id,
            'label': obj.get('label'),
            'role': obj.get('role'),
            'valid_observations': len(series),
            'event_count': len(sample_events),
            'strong_event_count': sum(1 for e in sample_events if e['severity'] == 'strong'),
        })

    out = {
        'ok': True,
        'quality_status': 'candidate_disturbance_flags_only',
        'computed_at_local': datetime.now(TZ).isoformat(),
        'source_network': data.get('network'),
        'scene_count': len(scenes),
        'sample_count': len(by_sample),
        'method': {
            'minimum_previous_ndvi': MIN_PRE_NDVI,
            'drop_threshold': DROP_THRESHOLD,
            'strong_drop_threshold': STRONG_DROP_THRESHOLD,
            'recovery_gain': RECOVERY_GAIN,
            'max_recovery_scenes': MAX_RECOVERY_SCENES,
        },
        'warning': 'Flags are candidate disturbances, not verified mowing/grazing events. Drought, cloud-mask edge effects, crop/phenology changes or other causes can produce abrupt NDVI changes.',
        'event_count': len(events),
        'strong_event_count': sum(1 for e in events if e['severity'] == 'strong'),
        'events': events,
        'disturbed_observations_for_sensitivity_test': [
            {'sample_id': a, 'scene_id': b} for a, b in sorted(disturbed_keys)
        ],
        'samples': sorted(sample_summary, key=lambda x: (-(x['event_count']), str(x['label']))),
        'next_step': 'Recompute terrain-gradient relationships after excluding flagged observations as a sensitivity test; compare with the full-data result rather than treating the filtered result as truth.'
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    status = {k: out[k] for k in ('ok','quality_status','computed_at_local','source_network','scene_count','sample_count','event_count','strong_event_count','warning','next_step')}
    status['output_file'] = str(OUT.relative_to(ROOT)).replace('\\','/')
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
