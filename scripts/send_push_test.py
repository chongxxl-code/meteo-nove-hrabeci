#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

APP_ID = os.environ.get('ONESIGNAL_APP_ID', '').strip()
API_KEY = os.environ.get('ONESIGNAL_REST_API_KEY', '').strip()

if not APP_ID or not API_KEY:
    print('OneSignal secrets are missing.')
    raise SystemExit(2)

payload = {
    'app_id': APP_ID,
    'target_channel': 'push',
    'included_segments': ['Subscribed Users'],
    'headings': {'en': '✅ Nové Hraběcí — test upozornění', 'cs': '✅ Nové Hraběcí — test upozornění'},
    'contents': {
        'en': 'Nouzová upozornění jsou správně nastavená. Toto je jediný test.',
        'cs': 'Nouzová upozornění jsou správně nastavená. Toto je jediný test.'
    },
    'url': 'https://chongxxl-code.github.io/meteo-nove-hrabeci/alerts.html',
    'ttl': 900,
    'priority': 10,
    'name': 'NH end-to-end push test'
}

req = urllib.request.Request(
    'https://api.onesignal.com/notifications',
    data=json.dumps(payload).encode('utf-8'),
    headers={
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': f'Key {API_KEY}',
    },
    method='POST',
)

try:
    with urllib.request.urlopen(req, timeout=30) as r:
        response = json.loads(r.read().decode('utf-8') or '{}')
except urllib.error.HTTPError as e:
    body = e.read().decode('utf-8', errors='replace')[:1000]
    print(f'OneSignal HTTP {e.code}: {body}')
    raise SystemExit(3)

print(json.dumps({'http_status': r.status, 'response': response}, ensure_ascii=False))
if r.status != 200 or not response.get('id'):
    print('No notification id returned. Most likely there is no subscribed device yet.')
    raise SystemExit(4)

print('Test push accepted by OneSignal.')
