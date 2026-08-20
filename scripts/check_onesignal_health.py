#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'push-health.json'


def main():
    app_id = os.environ.get('ONESIGNAL_APP_ID', '').strip()
    api_key = os.environ.get('ONESIGNAL_REST_API_KEY', '').strip()
    result = {
        'schema': 1,
        'provider': 'OneSignal',
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'app_id_configured': bool(app_id),
        'api_key_configured': bool(api_key),
        'api_key_valid': False,
        'status': 'not_configured',
    }
    if app_id and api_key:
        url = 'https://api.onesignal.com/notifications?' + urllib.parse.urlencode({'app_id': app_id, 'limit': 1, 'offset': 0})
        req = urllib.request.Request(url, headers={'Authorization': f'Key {api_key}'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode('utf-8') or '{}')
                result['api_key_valid'] = r.status == 200
                result['status'] = 'operational' if r.status == 200 else f'http_{r.status}'
                result['message_count_visible'] = len(body.get('notifications') or [])
        except urllib.error.HTTPError as e:
            result['status'] = f'http_{e.code}'
        except Exception as e:
            result['status'] = 'error'
            result['error_type'] = type(e).__name__
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
