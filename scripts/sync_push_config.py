#!/usr/bin/env python3
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / 'data' / 'push-config.json'
app_id = os.environ.get('ONESIGNAL_APP_ID', '').strip()
if not app_id:
    print('OneSignal App ID not configured; push client remains disabled.')
    raise SystemExit(0)

try:
    d = json.loads(p.read_text(encoding='utf-8'))
except Exception:
    d = {'schema': 1, 'mode': 'critical_only', 'minimum_rank': 3}
changed = d.get('app_id') != app_id or d.get('enabled') is not True
d['enabled'] = True
d['provider'] = 'OneSignal'
d['app_id'] = app_id
d['mode'] = 'critical_only'
d['minimum_rank'] = 3
d['note'] = 'Push je záměrně omezen na skutečně kritické lokální stavy. Žlutá ani oranžová v první verzi push nespouští.'
if changed:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Public push client config enabled from GitHub secret App ID.')
else:
    print('Push client config already current.')
