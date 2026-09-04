#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / 'data' / 'alerts.json'
CHMI = ROOT / 'data' / 'chmi-radar.json'
MAX_AGE_MIN = 20.0


def load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def finite(v):
    try:
        return float(v) == float(v)
    except Exception:
        return False


def find_alert(alerts, aid):
    for a in alerts:
        if a.get('id') == aid:
            return a
    return None


def _drop_contradictory_reasons(alert):
    phrases = {
        'storm': ('nedávají silný lokální signál', 'bez lokálního signálu'),
        'runoff': ('bez zvýšeného rizika',),
        'wind': ('bez zvýšeného rizika',),
    }.get(alert.get('id'), ())
    if phrases:
        alert['reasons'] = [r for r in (alert.get('reasons') or []) if not any(p in str(r).lower() for p in phrases)]


def bump(alert, rank, reason, confidence='high'):
    if not alert:
        return
    old = int(alert.get('rank') or 0)
    if rank > old:
        _drop_contradictory_reasons(alert)
        alert['rank'] = rank
        alert['level'] = ['green', 'yellow', 'orange', 'red'][rank]
        heads = {
            'storm': ['bez lokálního signálu', 'zvýšená pozornost', 'výrazné riziko', 'bezprostřední bouřková aktivita'],
            'runoff': ['bez zvýšeného rizika', 'sledovat', 'zvýšené riziko', 'vysoké lokální riziko'],
            'wind': ['bez zvýšeného rizika', 'zesílený vítr', 'silný vítr', 'velmi silný vítr'],
        }
        if alert.get('id') in heads:
            alert['headline'] = heads[alert['id']][rank]
    reasons = alert.setdefault('reasons', [])
    if reason not in reasons:
        reasons.append(reason)
    if rank >= old:
        alert['confidence'] = confidence


def metric(product, key):
    try:
        v = product['metrics'][key]
        return float(v) if finite(v) else None
    except Exception:
        return None


def fresh_product(p):
    return bool(p and p.get('ok') and finite(p.get('age_min')) and -2 <= float(p['age_min']) <= MAX_AGE_MIN)


def main():
    doc = load(ALERTS)
    chmi = load(CHMI)
    if not doc or not chmi:
        print(json.dumps({'ok': True, 'changed': False, 'reason': 'missing_input'}))
        return

    products = chmi.get('products') or {}
    maxz = products.get('maxz') or {}
    pc = products.get('pseudocappi2km') or {}
    maxz_ok, pc_ok = fresh_product(maxz), fresh_product(pc)
    source_ok = maxz_ok or pc_ok

    source = {
        'ok': source_ok,
        'provider': 'ČHMÚ OpenData / CZRAD',
        'status': chmi.get('status'),
        'computed_at_utc': chmi.get('computed_at_utc'),
        'latest_observed_at_utc': chmi.get('latest_observed_at_utc'),
        'max_age_min': MAX_AGE_MIN,
        'products': products,
    }
    doc.setdefault('sources', {})['chmi_radar'] = source

    if not source_ok:
        ALERTS.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'ok': True, 'changed': True, 'reason': 'chmi_radar_stale_or_unavailable'}))
        return

    ch10 = max([x for x in [metric(maxz, 'max_dbz_10km') if maxz_ok else None, metric(pc, 'max_dbz_10km') if pc_ok else None] if x is not None], default=None)
    ch25 = max([x for x in [metric(maxz, 'max_dbz_25km') if maxz_ok else None, metric(pc, 'max_dbz_25km') if pc_ok else None] if x is not None], default=None)
    ch50 = max([x for x in [metric(maxz, 'max_dbz_50km') if maxz_ok else None, metric(pc, 'max_dbz_50km') if pc_ok else None] if x is not None], default=None)
    pc10 = metric(pc, 'max_dbz_10km') if pc_ok else None
    pc25 = metric(pc, 'max_dbz_25km') if pc_ok else None

    nowcast = (doc.get('sources') or {}).get('radar_nowcast') or {}
    approaching = nowcast.get('ok') and nowcast.get('relation') in {'miri_na_lokalitu', 'nad_nebo_u_lokality'}
    nearest = float(nowcast['nearest_precip_km']) if finite(nowcast.get('nearest_precip_km')) else None
    eta = float(nowcast['eta_min']) if finite(nowcast.get('eta_min')) else None
    close50 = approaching and ((nearest is not None and nearest <= 50) or (eta is not None and eta <= 60))
    close25 = approaching and ((nearest is not None and nearest <= 25) or (eta is not None and eta <= 30))
    close10 = approaching and ((nearest is not None and nearest <= 10) or (eta is not None and eta <= 15))

    legacy = (doc.get('sources') or {}).get('radar') or {}
    legacy_dbz = ((legacy.get('latest') or {}).get('max_dbz') or {})
    rv10 = float(legacy_dbz['10km']) if finite(legacy_dbz.get('10km')) else None
    rv25 = float(legacy_dbz['25km']) if finite(legacy_dbz.get('25km')) else None

    models = (doc.get('sources') or {}).get('models') or []
    capes = [float(m['max_cape_12h']) for m in models if m.get('ok') and finite(m.get('max_cape_12h'))]
    gusts = [float(m['max_gust_12h']) for m in models if m.get('ok') and finite(m.get('max_gust_12h'))]
    cape = max(capes) if capes else None
    gust = max(gusts) if gusts else None

    alerts = doc.get('alerts') or []
    storm = find_alert(alerts, 'storm')
    runoff = find_alert(alerts, 'runoff')
    wind = find_alert(alerts, 'wind')

    if close50 and ch50 is not None and ch50 >= 40:
        bump(storm, 1, f'ČHMÚ radar potvrzuje aktivní echo do 50 km ({ch50:.0f} dBZ); RainViewer ukazuje pohyb k NH', 'high')

    dual_strong = close25 and ch25 is not None and ch25 >= 40 and ((rv25 is not None and rv25 >= 40) or (pc25 is not None and pc25 >= 40))
    if dual_strong:
        detail = f'dva radarové zdroje potvrzují silný systém do 25 km (ČHMÚ {ch25:.0f} dBZ)'
        if eta is not None:
            detail += f', ETA ~{eta:.0f} min'
        bump(storm, 2, detail, 'high')

    if close25 and pc25 is not None and pc25 >= 45:
        bump(runoff, 2, f'ČHMÚ PseudoCAPPI 2 km potvrzuje silné srážkové echo do 25 km ({pc25:.0f} dBZ)', 'high')

    severe_env = (cape is not None and cape >= 1000) or (gust is not None and gust >= 75)
    dual_very_strong = close10 and ch10 is not None and ch10 >= 50 and rv10 is not None and rv10 >= 50
    if dual_very_strong and severe_env:
        bump(storm, 3, 'ČHMÚ i RainViewer potvrzují velmi silné echo bezprostředně u NH + nebezpečné konvektivní prostředí', 'high')
        if pc10 is not None and pc10 >= 50:
            bump(runoff, 3, f'ČHMÚ PseudoCAPPI potvrzuje velmi silné srážky do 10 km ({pc10:.0f} dBZ)', 'high')
        if gust is not None and gust >= 75:
            bump(wind, 3, f'dvojí radarové potvrzení konvektivního systému + modelové nárazy ~{gust:.0f} km/h', 'high')

    normalized = json.dumps({
        'alerts': alerts,
        'official': ((doc.get('sources') or {}).get('official_warnings') or {}).get('active_for_point'),
        'radar_nowcast': nowcast,
        'chmi_radar': source,
    }, sort_keys=True, ensure_ascii=False)
    doc['_fingerprint'] = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    ALERTS.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'ok': True,
        'changed': True,
        'chmi_max_10km_dbz': ch10,
        'chmi_max_25km_dbz': ch25,
        'chmi_max_50km_dbz': ch50,
        'storm_rank': (storm or {}).get('rank'),
        'runoff_rank': (runoff or {}).get('rank'),
        'wind_rank': (wind or {}).get('rank'),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
