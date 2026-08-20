#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DROUGHT = ROOT / 'data' / 'drought-context.json'
COP = ROOT / 'data' / 'copernicus-soil.json'


def finite(v):
    try:
        return v is not None and float(v) == float(v)
    except Exception:
        return False


def main():
    if not DROUGHT.exists():
        raise SystemExit('drought-context.json missing')
    d = json.loads(DROUGHT.read_text(encoding='utf-8'))
    if not COP.exists():
        d['copernicus_1km'] = {'ok': False, 'status': 'not_available'}
        DROUGHT.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return

    c = json.loads(COP.read_text(encoding='utf-8'))
    products = c.get('products') or {}
    ssm = products.get('ssm') or {}
    swi = products.get('swi') or {}
    ssmv = ((ssm.get('primary') or {}).get('value_percent'))
    swi20 = ((swi.get('primary') or {}).get('value_percent'))
    swi60 = ((swi.get('swi_060') or {}).get('value_percent'))
    ok = bool(ssm.get('ok') and swi.get('ok'))

    evidence = 'neutral_or_uncalibrated'
    notes = []
    if finite(ssmv):
        notes.append(f'Copernicus SSM 1 km: povrchová vlhkost {float(ssmv):.1f} %')
    if finite(swi60):
        notes.append(f'Copernicus SWI T60 1 km: profilový vláhový index {float(swi60):.1f} %')
    if ok and finite(ssmv) and finite(swi60):
        if float(ssmv) <= 20 and float(swi60) <= 35:
            evidence = 'supports_drying'
        elif float(ssmv) >= 50 and float(swi60) >= 50:
            evidence = 'argues_against_drying'
        else:
            evidence = 'mixed'

    d['copernicus_1km'] = {
        'ok': ok,
        'quality_status': c.get('quality_status'),
        'nominal_date_ssm': ssm.get('nominal_date'),
        'nominal_date_swi': swi.get('nominal_date'),
        'surface_soil_moisture_percent': ssmv,
        'soil_water_index_t20_percent': swi20,
        'soil_water_index_t60_percent': swi60,
        'independent_evidence': evidence,
        'note': 'CLMS 1 km satellite control; kept as independent evidence and does not change the warning rank until a local archive is calibrated.'
    }

    state = d.get('state') or {}
    reasons = list(state.get('reasons') or [])
    # Keep the operational level conservative: add independent evidence, but do not change rank yet.
    if evidence == 'supports_drying':
        reasons.extend(x for x in notes if x not in reasons)
        state['confidence'] = 'medium_high' if state.get('rank', 0) > 0 else state.get('confidence', 'medium')
    elif evidence == 'argues_against_drying':
        reasons.append('Copernicus 1 km půdní vlhkost aktuálně nepodporuje výrazné vysychání')
    elif evidence == 'mixed':
        reasons.append('Copernicus 1 km dává smíšený půdně-vlhkostní signál')
    state['reasons'] = reasons
    d['state'] = state

    d['method_note'] = 'Operational local drought signal: P-ET0 water balance + reanalysis soil-moisture percentiles + Sentinel-2 NDMI + independent Copernicus CLMS 1 km SSM/SWI control. It is not an official drought classification.'
    limits = list(d.get('known_limitations') or [])
    limits = [x for x in limits if 'Copernicus 1 km' not in x or 'planned' not in x]
    new_limit = 'Copernicus SSM/SWI has 1 km resolution; it strengthens independent validation but cannot by itself resolve field-scale differences inside Nové Hraběcí.'
    if new_limit not in limits:
        limits.append(new_limit)
    d['known_limitations'] = limits

    DROUGHT.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'copernicus': ok, 'evidence': evidence, 'ssm': ssmv, 'swi60': swi60}, ensure_ascii=False))


if __name__ == '__main__':
    main()
