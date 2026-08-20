#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / 'data' / 'alerts.json'
STATE = ROOT / 'data' / 'push-state.json'
WEB_URL = 'https://chongxxl-code.github.io/meteo-nove-hrabeci/alerts.html'

MESSAGES = {
    'storm': ('⛈️ Nové Hraběcí — bouřkové riziko', 'Kritická bouřková aktivita v bezprostředním okolí. Otevři detail lokálních rizik.'),
    'runoff': ('🌧️ Nové Hraběcí — přívalový déšť', 'Kritické lokální riziko přívalového deště nebo rychlého odtoku. Otevři detail rizik.'),
    'wind': ('🌬️ Nové Hraběcí — silný vítr', 'Kritické lokální riziko velmi silných nárazů větru. Otevři detail rizik.'),
    'heat': ('🌡️ Nové Hraběcí — extrémní horko', 'Lokální systém vyhodnotil kritickou tepelnou zátěž.'),
    'frost': ('❄️ Nové Hraběcí — silný mráz', 'Lokální systém vyhodnotil kritické riziko silného mrazu.'),
    'dry': ('🔥 Nové Hraběcí — extrémní sucho', 'Lokální systém vyhodnotil extrémní vysychání krajiny.'),
}


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_state(data):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def send(app_id: str, api_key: str, alert: dict):
    title, body = MESSAGES.get(alert['id'], ('⚠️ Nové Hraběcí — kritické riziko', alert.get('headline') or 'Kritický lokální stav.'))
    reasons = alert.get('reasons') or []
    if reasons:
        detail = '; '.join(str(x) for x in reasons[:2])
        if len(body) + len(detail) < 220:
            body = f'{body} {detail}'
    payload = {
        'app_id': app_id,
        'target_channel': 'push',
        'included_segments': ['Subscribed Users'],
        'headings': {'en': title, 'cs': title},
        'contents': {'en': body, 'cs': body},
        'url': WEB_URL,
        'ttl': 1800,
        'priority': 10,
        'name': f"NH critical {alert['id']} {datetime.now(timezone.utc).isoformat()}",
    }
    req = urllib.request.Request(
        'https://api.onesignal.com/notifications',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Key {api_key}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode('utf-8') or '{}')


def main():
    alerts = load(ALERTS, {})
    critical = [a for a in alerts.get('alerts', []) if int(a.get('rank') or 0) >= 3]
    state = load(STATE, {'schema': 1, 'active_critical_ids': [], 'last_sent': None})
    previous = set(state.get('active_critical_ids') or [])
    current = {a['id'] for a in critical}

    # Critical event ended: arm the same category for a future independent event. No all-clear push in v1.
    if not critical:
        if previous:
            state['active_critical_ids'] = []
            state['cleared_at_utc'] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        print(json.dumps({'ok': True, 'sent': False, 'reason': 'no_critical_alert'}, ensure_ascii=False))
        return

    new_alerts = [a for a in critical if a['id'] not in previous]
    if not new_alerts:
        print(json.dumps({'ok': True, 'sent': False, 'reason': 'critical_already_notified', 'active': sorted(current)}, ensure_ascii=False))
        return

    app_id = os.environ.get('ONESIGNAL_APP_ID', '').strip()
    api_key = os.environ.get('ONESIGNAL_REST_API_KEY', '').strip()
    if not app_id or not api_key:
        print(json.dumps({'ok': True, 'sent': False, 'reason': 'onesignal_not_configured', 'would_send': [a['id'] for a in new_alerts]}, ensure_ascii=False))
        return

    sent = []
    responses = []
    for alert in new_alerts:
        try:
            status, response = send(app_id, api_key, alert)
            responses.append({'id': alert['id'], 'status': status, 'response': response})
            # OneSignal can return 200 without an id if there are no subscribed users; don't mark sent in that case.
            if response.get('id'):
                sent.append(alert['id'])
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')[:1000]
            raise RuntimeError(f'OneSignal HTTP {e.code}: {body}') from e

    if sent:
        state['active_critical_ids'] = sorted(current)
        state['last_sent'] = {
            'at_utc': datetime.now(timezone.utc).isoformat(),
            'alert_ids': sent,
            'responses': responses,
        }
        save_state(state)
    print(json.dumps({'ok': True, 'sent': bool(sent), 'sent_ids': sent, 'responses': responses}, ensure_ascii=False))


if __name__ == '__main__':
    main()
