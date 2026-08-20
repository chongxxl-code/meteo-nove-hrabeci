#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
SRC = DATA / 'radar-nowcast.json'
ARCHIVE = DATA / 'radar-observations'
LOCAL_RADIUS_KM = 7.0


def main():
    if not SRC.exists():
        raise SystemExit('radar-nowcast.json missing')
    n = json.loads(SRC.read_text(encoding='utf-8'))
    frames = n.get('frames') or {}
    ts = frames.get('latest_unix')
    if not ts:
        print('No usable radar timestamp; nothing archived.')
        return

    nearest = n.get('nearest_precip_km')
    status = n.get('status')
    local_rain = None
    if status in ('operational', 'motion_uncertain', 'no_reliable_motion'):
        if nearest is not None:
            local_rain = float(nearest) <= LOCAL_RADIUS_KM
        elif n.get('relation') == 'bez_srazek_v_dosahu':
            local_rain = False

    record = {
        'schema': 1,
        'radar_unix': int(ts),
        'radar_time_utc': datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace('+00:00', 'Z'),
        'computed_at_utc': n.get('computed_at_utc'),
        'status': status,
        'local_radius_km': LOCAL_RADIUS_KM,
        'local_rain_signal': local_rain,
        'nearest_precip_km': nearest,
        'relation': n.get('relation'),
        'confidence': n.get('confidence'),
        'source': 'RainViewer qualitative radar signal; not rain-gauge millimetres'
    }

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    dt = datetime.fromtimestamp(int(ts), timezone.utc)
    path = ARCHIVE / f'{dt:%Y-%m}.jsonl'
    if path.exists():
        for line in reversed(path.read_text(encoding='utf-8').splitlines()[-30:]):
            try:
                if int(json.loads(line).get('radar_unix', -1)) == int(ts):
                    print('Radar observation already archived.')
                    return
            except Exception:
                pass
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    main()
