#!/usr/bin/env python3
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'scripts'/'build_local_alerts.py'
s=p.read_text(encoding='utf-8')
s=s.replace("return {'rain_7d_mm':round(sums[7],1),'rain_14d_mm':round(sums[14],1),'rain_30d_mm':round(sums[30],1),'latest_observation_utc':latest.isoformat() if latest else None,'interval_counts':counts}", "coverage={days:round(min(1.0, counts[days]/(days*144)),3) for days in sums}\n    return {'rain_7d_mm':round(sums[7],1),'rain_14d_mm':round(sums[14],1),'rain_30d_mm':round(sums[30],1),'latest_observation_utc':latest.isoformat() if latest else None,'interval_counts':counts,'coverage_fraction':coverage}")
old="""    rank=0; reasons=[]; perc=sat.get('seasonal_percentile') if sat.get('ok') else None
    if rain['rain_14d_mm']<8: rank=1; reasons.append(f'jen {rain[\"rain_14d_mm\"]:.1f} mm / 14 dní')
    if rain['rain_30d_mm']<20: rank=max(rank,2); reasons.append(f'jen {rain[\"rain_30d_mm\"]:.1f} mm / 30 dní')
    if finite(perc) and perc<=25: rank=max(rank,1); reasons.append(f'aktuální NDMI travních ploch je v dolní čtvrtině letošních scén ({perc:.0f}. percentil)')
    if finite(perc) and perc<=15 and rain['rain_30d_mm']<25: rank=max(rank,2)
    out.append({'id':'dry','name':'Vysychání krajiny','icon':'🔥','level':level(rank),'rank':rank,'headline':['bez výrazného signálu','zvýšené vysychání','výrazné vysychání','extrémní lokální vysychání'][rank],'reasons':reasons or [f'{rain[\"rain_30d_mm\"]:.1f} mm / 30 dní; Sentinel zatím nedává extrémní lokální signál'],'confidence':'medium','note':'Nejde zatím o oficiální klasifikaci sucha; je to lokální stav observatoře.'})
"""
new="""    rank=0; reasons=[]; perc=sat.get('seasonal_percentile') if sat.get('ok') else None
    cov=(rain.get('coverage_fraction') or {}); cov14=float(cov.get(14,0)); cov30=float(cov.get(30,0))
    if cov14>=0.80 and rain['rain_14d_mm']<8: rank=1; reasons.append(f'jen {rain[\"rain_14d_mm\"]:.1f} mm / 14 dní')
    if cov30>=0.80 and rain['rain_30d_mm']<20: rank=max(rank,2); reasons.append(f'jen {rain[\"rain_30d_mm\"]:.1f} mm / 30 dní')
    if finite(perc) and perc<=25: rank=max(rank,1); reasons.append(f'aktuální NDMI travních ploch je v dolní čtvrtině letošních scén ({perc:.0f}. percentil)')
    if finite(perc) and perc<=15 and cov30>=0.80 and rain['rain_30d_mm']<25: rank=max(rank,2)
    if cov30<0.80: reasons.append(f'30denní surová srážková historie zatím pokrývá jen {cov30*100:.0f} % období — dlouhodobý srážkový práh se proto nepoužívá')
    out.append({'id':'dry','name':'Vysychání krajiny','icon':'🔥','level':level(rank),'rank':rank,'headline':['bez potvrzeného výrazného signálu','zvýšené vysychání','výrazné vysychání','extrémní lokální vysychání'][rank],'reasons':reasons or ['srážková historie a Sentinel zatím nedávají výrazný lokální signál'],'confidence':'medium' if cov30>=0.80 else 'low_to_medium','note':'Nejde o oficiální klasifikaci sucha; je to lokální stav observatoře. Dlouhodobé prahy se aktivují až při dostatečném datovém pokrytí.'})
"""
if old not in s:
    raise SystemExit('dry block not found')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')
print('patched drought coverage guard')
