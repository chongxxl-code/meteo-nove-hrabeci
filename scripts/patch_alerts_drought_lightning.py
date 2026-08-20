#!/usr/bin/env python3
from pathlib import Path

p = Path(__file__).resolve().parents[1] / 'scripts' / 'build_local_alerts.py'
s = p.read_text(encoding='utf-8')

needle = "def build_alerts(models,rain,sat,radar,cap,lightning):"
insert = '''def drought_context():
    p = ROOT / 'data' / 'drought-context.json'
    if not p.exists():
        return {'ok': False, 'status': 'not_built'}
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        d['ok'] = d.get('quality_status') == 'operational_local_drought_v1'
        return d
    except Exception as e:
        return {'ok': False, 'status': 'read_error', 'error': str(e)[:180]}


def chmi_lightning_public():
    return {
        'ok': True,
        'provider': 'ČHMÚ / LINET public visualization',
        'url': 'https://produkty.chmi.cz/radar/',
        'automated_decision_input': False,
        'note': 'ČHMÚ veřejně zobrazuje aktuální detekci blesků LINET s přibližně kilometrovou přesností. Kvůli licencovanému původu LINET dat ji zatím používáme jako oficiální živou kontrolní vrstvu, ne jako strojově parsovaný feed.'
    }


def build_alerts(models,rain,sat,radar,cap,lightning,drought):'''
if needle in s:
    s = s.replace(needle, insert, 1)

old = '''    # Drying / drought memory
    rank=0; reasons=[]; perc=sat.get('seasonal_percentile') if sat.get('ok') else None
    cov=(rain.get('coverage_fraction') or {}); cov14=float(cov.get(14,0)); cov30=float(cov.get(30,0))
    if cov14>=0.80 and rain['rain_14d_mm']<8: rank=1; reasons.append(f'jen {rain["rain_14d_mm"]:.1f} mm / 14 dní')
    if cov30>=0.80 and rain['rain_30d_mm']<20: rank=max(rank,2); reasons.append(f'jen {rain["rain_30d_mm"]:.1f} mm / 30 dní')
    if finite(perc) and perc<=25: rank=max(rank,1); reasons.append(f'aktuální NDMI travních ploch je v dolní čtvrtině letošních scén ({perc:.0f}. percentil)')
    if finite(perc) and perc<=15 and cov30>=0.80 and rain['rain_30d_mm']<25: rank=max(rank,2)
    if cov30<0.80: reasons.append(f'30denní surová srážková historie zatím pokrývá jen {cov30*100:.0f} % období — dlouhodobý srážkový práh se proto nepoužívá')
    out.append({'id':'dry','name':'Vysychání krajiny','icon':'🔥','level':level(rank),'rank':rank,'headline':['bez potvrzeného výrazného signálu','zvýšené vysychání','výrazné vysychání','extrémní lokální vysychání'][rank],'reasons':reasons or ['srážková historie a Sentinel zatím nedávají výrazný lokální signál'],'confidence':'medium' if cov30>=0.80 else 'low_to_medium','note':'Nejde o oficiální klasifikaci sucha; je to lokální stav observatoře. Dlouhodobé prahy se aktivují až při dostatečném datovém pokrytí.'})
'''
new = '''    # Drying / drought memory: dedicated daily local drought context first.
    if drought.get('ok') and isinstance(drought.get('state'), dict):
        ds=drought['state']; rank=int(ds.get('rank') or 0)
        out.append({'id':'dry','name':'Vysychání krajiny','icon':'🔥','level':ds.get('level') or level(rank),'rank':rank,'headline':ds.get('headline') or 'stav sucha','reasons':ds.get('reasons') or ['lokální drought-context je dostupný'],'confidence':ds.get('confidence') or 'medium','note':'Lokální provozní index kombinuje P−ET₀, půdní vlhkost a Sentinel NDMI. Nejde o oficiální klasifikaci sucha.'})
    else:
        rank=0; reasons=[]; perc=sat.get('seasonal_percentile') if sat.get('ok') else None
        cov=(rain.get('coverage_fraction') or {}); cov30=float(cov.get(30,0))
        if finite(perc) and perc<=25: rank=1; reasons.append(f'aktuální NDMI travních ploch je v dolní čtvrtině letošních scén ({perc:.0f}. percentil)')
        reasons.append('dedikovaný drought-context zatím není dostupný; používá se pouze konzervativní Sentinel fallback')
        out.append({'id':'dry','name':'Vysychání krajiny','icon':'🔥','level':level(rank),'rank':rank,'headline':['bez potvrzeného výrazného signálu','zvýšené vysychání','výrazné vysychání','extrémní lokální vysychání'][rank],'reasons':reasons,'confidence':'low_to_medium','note':'Nejde o oficiální klasifikaci sucha.'})
'''
if old in s:
    s = s.replace(old, new, 1)

s = s.replace(
    "models=model_forecasts(); rain=rain_totals(); sat=sentinel_context(); radar=radar_local(); cap=cap_official(); lightning=lightning_optional()\n    alerts=build_alerts(models,rain,sat,radar,cap,lightning)",
    "models=model_forecasts(); rain=rain_totals(); sat=sentinel_context(); radar=radar_local(); cap=cap_official(); lightning=lightning_optional(); drought=drought_context(); chmi_lightning=chmi_lightning_public()\n    alerts=build_alerts(models,rain,sat,radar,cap,lightning,drought)",
    1,
)
s = s.replace(
    "'official_warnings':cap,'lightning':lightning}",
    "'official_warnings':cap,'lightning':lightning,'chmi_lightning':chmi_lightning,'drought':drought}",
    1,
)
s = s.replace(
    "'lightning':lightning.get('status')}",
    "'lightning':lightning.get('status'),'drought':(drought.get('state') or {}).get('rank')}",
    1,
)

p.write_text(s, encoding='utf-8')
print('patched local alerts for drought context + CHMI lightning layer')
