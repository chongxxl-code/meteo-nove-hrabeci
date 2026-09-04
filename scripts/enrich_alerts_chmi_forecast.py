#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / 'data' / 'alerts.json'
FCT = ROOT / 'data' / 'chmi-radar-forecast.json'
MAX_AGE_MIN = 25.0


def load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def dt_utc(v):
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def find_alert(doc, aid):
    for a in doc.get('alerts') or []:
        if a.get('id') == aid:
            return a
    return None


def bump(alert, rank, reason, confidence='high'):
    if not alert:
        return
    old = int(alert.get('rank') or 0)
    if rank > old:
        alert['rank'] = rank
        alert['level'] = ['green', 'yellow', 'orange', 'red'][rank]
        if alert.get('id') == 'storm':
            alert['headline'] = ['bez lokálního signálu', 'zvýšená pozornost', 'výrazné riziko', 'bezprostřední bouřková aktivita'][rank]
        elif alert.get('id') == 'runoff':
            alert['headline'] = ['bez zvýšeného rizika', 'sledovat', 'zvýšené riziko', 'vysoké lokální riziko'][rank]
    if reason not in alert.setdefault('reasons', []):
        alert['reasons'].append(reason)
    if rank >= old:
        alert['confidence'] = confidence


def main():
    doc = load(ALERTS)
    fct = load(FCT)
    if not doc or not fct:
        print(json.dumps({'ok': True, 'changed': False, 'reason': 'missing_input'}))
        return

    calc = dt_utc(fct.get('latest_calculation_time_utc'))
    age = (datetime.now(timezone.utc) - calc).total_seconds() / 60 if calc else 9999
    source = {'ok': False, 'status': 'stale_or_unavailable', 'age_min': round(age, 1) if age < 9999 else None}
    if fct.get('status') not in ('operational', 'partial') or age < -2 or age > MAX_AGE_MIN:
        doc.setdefault('sources', {})['chmi_radar_forecast'] = source
        ALERTS.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'ok': True, 'changed': True, 'reason': 'forecast_stale', 'age_min': source['age_min']}))
        return

    p = (fct.get('products') or {}).get('pseudocappi2km') or {}
    m = (fct.get('products') or {}).get('maxz') or {}
    chmi_eta = p.get('first_35dbz_within_7km_lead_min') if p.get('ok') else None
    strong_eta = p.get('first_45dbz_within_10km_lead_min') if p.get('ok') else None
    peak25 = p.get('peak_dbz_25km_next60') if p.get('ok') else None
    if chmi_eta is None and m.get('ok'):
        chmi_eta = m.get('first_35dbz_within_7km_lead_min')
    rv = (doc.get('sources') or {}).get('radar_nowcast') or {}
    rv_eta = rv.get('eta_min') if rv.get('ok') else None
    rv_relation = rv.get('relation') if rv.get('ok') else None
    rv_approaching = rv_relation in ('miri_na_lokalitu', 'nad_nebo_u_lokality')

    agreement = 'indeterminate'
    eta_diff = None
    if rv_approaching and rv_eta is not None and chmi_eta is not None:
        eta_diff = abs(float(rv_eta) - float(chmi_eta))
        agreement = 'agree' if eta_diff <= 20 else 'disagree'
    elif rv_approaching and rv_eta is not None and chmi_eta is None:
        agreement = 'disagree'
    elif chmi_eta is not None:
        agreement = 'chmi_only'
    else:
        agreement = 'no_arrival_signal'

    source = {
        'ok': True,
        'provider': 'ČHMÚ OpenData / CZRAD COTREC',
        'age_min': round(age, 1),
        'chmi_arrival_lead_min': chmi_eta,
        'chmi_strong_arrival_lead_min': strong_eta,
        'peak_dbz_25km_next60': peak25,
        'rainviewer_eta_min': rv_eta,
        'agreement': agreement,
        'eta_difference_min': round(eta_diff, 1) if eta_diff is not None else None,
        'note': 'ČHMÚ COTREC je kontrolní extrapolace; při shodě zvyšuje důvěru, při rozporu ji snižuje. Samostatně nepřebíjí přímé měřené radarové varování.'
    }
    doc.setdefault('sources', {})['chmi_radar_forecast'] = source

    storm = find_alert(doc, 'storm')
    runoff = find_alert(doc, 'runoff')

    if agreement == 'agree':
        if chmi_eta is not None and float(chmi_eta) <= 60:
            bump(storm, max(1, int((storm or {}).get('rank') or 0)), f'ČHMÚ COTREC potvrzuje příchod systému přibližně za {int(chmi_eta)} min; shoda s RainViewer ETA ±{int(round(eta_diff or 0))} min', 'high')
        if chmi_eta is not None and float(chmi_eta) <= 30 and peak25 is not None and float(peak25) >= 40:
            bump(storm, 2, f'dva nezávislé nowcasty se shodují a ČHMÚ čeká aktivní echo do 30 min', 'high')
        if strong_eta is not None and float(strong_eta) <= 30:
            bump(runoff, 2, f'ČHMÚ COTREC očekává silné echo ≥45 dBZ do 10 km za ~{int(strong_eta)} min', 'high')
    elif agreement == 'disagree':
        if storm:
            reason = 'radarové nowcasty se rozcházejí: RainViewer míří k NH, ČHMÚ COTREC nepotvrzuje podobný příchod do 60 min' if chmi_eta is None else f'radarové ETA se rozcházejí o ~{int(round(eta_diff))} min'
            if reason not in storm.setdefault('reasons', []):
                storm['reasons'].append(reason)
            if int(storm.get('rank') or 0) < 3:
                storm['confidence'] = 'medium'
    elif agreement == 'chmi_only' and chmi_eta is not None:
        if float(chmi_eta) <= 30 and peak25 is not None and float(peak25) >= 40:
            bump(storm, 1, f'ČHMÚ COTREC samostatně naznačuje příchod aktivního echa za ~{int(chmi_eta)} min; RainViewer směr zatím nepotvrdil', 'medium')

    normalized = json.dumps({
        'alerts': doc.get('alerts'),
        'official': ((doc.get('sources') or {}).get('official_warnings') or {}).get('active_for_point'),
        'radar_nowcast': rv,
        'chmi_radar_forecast': source,
    }, sort_keys=True, ensure_ascii=False)
    doc['_fingerprint'] = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    ALERTS.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'changed': True, 'agreement': agreement, 'chmi_eta': chmi_eta, 'rainviewer_eta': rv_eta, 'eta_diff': eta_diff, 'storm_rank': (storm or {}).get('rank')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
