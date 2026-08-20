#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'alerts.json'
LAT, LON = 51.0162, 14.4398
TZ = ZoneInfo('Europe/Prague')
UA = 'nove-hrabeci-local-observatory/1.0'


def get(url, timeout=25, headers=None):
    h={'User-Agent':UA}
    if headers: h.update(headers)
    req=urllib.request.Request(url,headers=h)
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return r.read(), dict(r.headers)


def get_json(url):
    b,_=get(url)
    return json.loads(b.decode('utf-8'))


def finite(v):
    try: return math.isfinite(float(v))
    except Exception: return False


def hav_km(lat1,lon1,lat2,lon2):
    r=6371.0088
    a1,a2=math.radians(lat1),math.radians(lat2)
    da=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(da/2)**2+math.cos(a1)*math.cos(a2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def point_in_poly(lat,lon,poly):
    x,y=lon,lat; inside=False
    for i in range(len(poly)):
        x1,y1=poly[i-1][1],poly[i-1][0]; x2,y2=poly[i][1],poly[i][0]
        if ((y1>y)!=(y2>y)):
            xin=(x2-x1)*(y-y1)/(y2-y1+1e-15)+x1
            if x<xin: inside=not inside
    return inside


def model_forecasts():
    hourly='temperature_2m,precipitation,wind_gusts_10m,cape,weather_code'
    base=f'https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&timezone=Europe%2FPrague&forecast_days=2&hourly={hourly}'
    urls={
        'ICON-D2': base+'&models=dwd_icon_d2',
        'ALADIN': base+'&models=chmi_aladin_cz_1km',
        'ECMWF': f'https://api.open-meteo.com/v1/ecmwf?latitude={LAT}&longitude={LON}&timezone=Europe%2FPrague&forecast_days=2&hourly={hourly}',
    }
    now=datetime.now(TZ).replace(minute=0,second=0,microsecond=0)
    rows=[]
    for name,url in urls.items():
        try:
            d=get_json(url); h=d.get('hourly') or {}; times=h.get('time') or []
            start=0
            key=now.strftime('%Y-%m-%dT%H:00')
            if key in times: start=times.index(key)
            def vals(k,n=12): return [(h.get(k) or [])[i] for i in range(start,min(start+n,len(times))) if i<len(h.get(k) or [])]
            t=[float(x) for x in vals('temperature_2m') if finite(x)]
            p=[float(x) for x in vals('precipitation') if finite(x)]
            g=[float(x) for x in vals('wind_gusts_10m') if finite(x)]
            c=[float(x) for x in vals('cape') if finite(x)]
            rows.append({'name':name,'ok':True,'max_temp_12h':max(t) if t else None,'min_temp_12h':min(t) if t else None,'max_precip_1h_12h':max(p) if p else None,'precip_sum_6h':sum(p[:6]) if p else None,'max_gust_12h':max(g) if g else None,'max_cape_12h':max(c) if c else None})
        except Exception as e:
            rows.append({'name':name,'ok':False,'error':str(e)[:180]})
    return rows


def rain_totals():
    now=datetime.now(timezone.utc); sums={7:0.0,14:0.0,30:0.0}; counts={7:0,14:0,30:0}
    latest=None
    for path in sorted((ROOT/'data'/'observations').glob('chmi-rain-*.jsonl')):
        for line in path.read_text(encoding='utf-8',errors='ignore').splitlines():
            try: d=json.loads(line); ts=datetime.fromisoformat(d['observed_at_utc'].replace('Z','+00:00')); mm=float(d.get('precipitation_10m_mm') or 0)
            except Exception: continue
            if latest is None or ts>latest: latest=ts
            age=(now-ts).total_seconds()/86400
            for days in sums:
                if 0<=age<=days: sums[days]+=mm; counts[days]+=1
    coverage={days:round(min(1.0, counts[days]/(days*144)),3) for days in sums}
    return {'rain_7d_mm':round(sums[7],1),'rain_14d_mm':round(sums[14],1),'rain_30d_mm':round(sums[30],1),'latest_observation_utc':latest.isoformat() if latest else None,'interval_counts':counts,'coverage_fraction':coverage}


def sentinel_context():
    p=ROOT/'data'/'validation'/'open-land-experiment-2026.json'
    if not p.exists(): return {'ok':False}
    try: d=json.loads(p.read_text(encoding='utf-8'))
    except Exception: return {'ok':False}
    series=[]
    for s in d.get('scenes') or []:
        vals=[]
        for z in s.get('zones') or []:
            if z.get('ok') and finite((z.get('ndmi') or {}).get('median')): vals.append(float(z['ndmi']['median']))
        if vals: series.append({'datetime':s.get('scene_datetime'),'median_ndmi':median(vals)})
    if not series: return {'ok':False}
    cur=series[-1]['median_ndmi']; sortedv=sorted(x['median_ndmi'] for x in series); rank=sum(1 for v in sortedv if v<=cur)/len(sortedv)
    return {'ok':True,'latest_scene':series[-1]['datetime'],'current_open_land_median_ndmi':round(cur,4),'seasonal_percentile':round(rank*100,1),'scene_count':len(series)}


def rainviewer_palette():
    b,_=get('https://www.rainviewer.com/files/rainviewer_api_colors_table.csv')
    rows=list(csv.reader(io.StringIO(b.decode('utf-8'))))
    # Universal Blue is column 3; unsmoothed tiles use exact palette colours.
    out={}
    for r in rows[1:]:
        try: dbz=int(r[0]); hx=r[3].lstrip('#'); rgba=tuple(int(hx[i:i+2],16) for i in (0,2,4,6)); out[rgba[:3]]=dbz
        except Exception: pass
    return out


def radar_local():
    try:
        meta=get_json('https://api.rainviewer.com/public/weather-maps.json'); frames=(meta.get('radar') or {}).get('past') or []
        if not frames: return {'ok':False,'error':'no radar frames'}
        palette=rainviewer_palette(); host=meta['host']; use=frames[-4:]
        samples=[]
        from PIL import Image
        for f in use:
            url=f"{host}{f['path']}/256/7/{LAT}/{LON}/2/0_0.png"
            b,_=get(url); im=Image.open(io.BytesIO(b)).convert('RGBA'); w,h=im.size; cx,cy=w//2,h//2
            # At z7 here ~0.77 km/pixel. Use conservative radii.
            radii={'10km':13,'25km':33,'50km':65}; rr={}
            nearest35=None
            for name,rpx in radii.items():
                mx=-32
                for y in range(max(0,cy-rpx),min(h,cy+rpx+1)):
                    for x in range(max(0,cx-rpx),min(w,cx+rpx+1)):
                        if (x-cx)**2+(y-cy)**2>rpx*rpx: continue
                        px=im.getpixel((x,y)); dbz=palette.get(px[:3],-32) if px[3]>0 else -32
                        if dbz>mx: mx=dbz
                        if dbz>=35:
                            dist=math.hypot(x-cx,y-cy)*0.77
                            if nearest35 is None or dist<nearest35: nearest35=dist
                rr[name]=mx
            samples.append({'time_unix':f['time'],'time_local':datetime.fromtimestamp(f['time'],TZ).isoformat(),'max_dbz':rr,'nearest_35dbz_km':round(nearest35,1) if nearest35 is not None else None})
        latest=samples[-1]; trend='unknown'
        ds=[x['nearest_35dbz_km'] for x in samples if x['nearest_35dbz_km'] is not None]
        if len(ds)>=2:
            if ds[-1] <= ds[0]-5: trend='approaching'
            elif ds[-1] >= ds[0]+5: trend='moving_away'
            else: trend='steady_or_lateral'
        return {'ok':True,'provider':'RainViewer','latest':latest,'trend_30m':trend,'frames':samples}
    except Exception as e:
        return {'ok':False,'provider':'RainViewer','error':str(e)[:200]}


def cap_official():
    base='https://opendata.chmi.cz/meteorology/weather/alerts/cap/'
    try:
        html=get(base)[0].decode('utf-8',errors='ignore')
        pat=re.compile(r'href="(alert_cap_50_[^"]+\.xml)"[^\n]*?(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})')
        found=[]
        for fn,ds in pat.findall(html):
            try: found.append((datetime.strptime(ds,'%d-%b-%Y %H:%M').replace(tzinfo=timezone.utc),fn))
            except Exception: pass
        found.sort(reverse=True)
        now=datetime.now(timezone.utc); active=[]; inspected=[]
        for _,fn in found[:4]:
            b,_=get(base+fn); root=ET.fromstring(b); inspected.append(fn)
            for info in root.findall('.//{*}info'):
                event=(info.findtext('{*}event') or '').strip(); severity=(info.findtext('{*}severity') or '').strip(); certainty=(info.findtext('{*}certainty') or '').strip(); onset=info.findtext('{*}onset') or info.findtext('{*}effective'); expires=info.findtext('{*}expires')
                try: on=datetime.fromisoformat(onset) if onset else None; ex=datetime.fromisoformat(expires) if expires else None
                except Exception: on=ex=None
                if ex and ex.astimezone(timezone.utc)<now: continue
                hit=False; areas=[]
                for area in info.findall('{*}area'):
                    desc=area.findtext('{*}areaDesc') or ''
                    for pt in area.findall('{*}polygon'):
                        poly=[]
                        for pair in (pt.text or '').split():
                            try: a,b=pair.split(','); poly.append((float(a),float(b)))
                            except Exception: pass
                        if len(poly)>=3 and point_in_poly(LAT,LON,poly): hit=True
                    for c in area.findall('{*}circle'):
                        try:
                            center,rad=(c.text or '').split(); a,b=center.split(',');
                            if hav_km(LAT,LON,float(a),float(b))<=float(rad): hit=True
                        except Exception: pass
                    if hit: areas.append(desc)
                if hit: active.append({'event':event,'severity':severity,'certainty':certainty,'onset':onset,'expires':expires,'areas':areas,'source_file':fn})
        return {'ok':True,'provider':'ČHMÚ CAP','active_for_point':active,'inspected_files':inspected}
    except Exception as e:
        return {'ok':False,'provider':'ČHMÚ CAP','error':str(e)[:220]}


def lightning_optional():
    # Blitzortung is intentionally NOT used: its published terms prohibit use for storm warning systems.
    user=os.getenv('METEOMATICS_USER'); pw=os.getenv('METEOMATICS_PASSWORD')
    if not user or not pw:
        return {'ok':False,'status':'not_configured','provider':'none','note':'Blitzortung is excluded from warning decisions by its terms. Optional licensed lightning provider slot is ready.'}
    try:
        import base64
        token=base64.b64encode(f'{user}:{pw}'.encode()).decode()
        now=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        url=f'https://api.meteomatics.com/{now}/lightning_strikes_20km_10min:x/{LAT},{LON}/json'
        d=get_json_auth(url,token)
        val=d['data'][0]['coordinates'][0]['dates'][0]['value']
        return {'ok':True,'status':'active','provider':'Meteomatics','strikes_20km_10min':val}
    except Exception as e:
        return {'ok':False,'status':'configured_error','provider':'Meteomatics','error':str(e)[:180]}


def get_json_auth(url,token):
    b,_=get(url,headers={'Authorization':'Basic '+token}); return json.loads(b.decode())


def level(rank): return ['green','yellow','orange','red'][max(0,min(3,rank))]


def build_alerts(models,rain,sat,radar,cap,lightning):
    good=[m for m in models if m.get('ok')]
    def cons(field,mode='max'):
        vals=[m[field] for m in good if finite(m.get(field))]
        if not vals: return None
        return median(vals) if mode=='median' else max(vals)
    gust=cons('max_gust_12h'); cape=cons('max_cape_12h'); p1=cons('max_precip_1h_12h'); p6=cons('precip_sum_6h','median'); tmax=cons('max_temp_12h'); tmin=min([m['min_temp_12h'] for m in good if finite(m.get('min_temp_12h'))],default=None)
    official=cap.get('active_for_point') or []
    official_text='; '.join(x.get('event','') for x in official).lower()
    r10=((radar.get('latest') or {}).get('max_dbz') or {}).get('10km') if radar.get('ok') else None
    r25=((radar.get('latest') or {}).get('max_dbz') or {}).get('25km') if radar.get('ok') else None
    r50=((radar.get('latest') or {}).get('max_dbz') or {}).get('50km') if radar.get('ok') else None
    nearest=((radar.get('latest') or {}).get('nearest_35dbz_km')) if radar.get('ok') else None

    out=[]
    # Thunderstorm / convection
    rank=0; reasons=[]
    if 'bouř' in official_text: rank=max(rank,2); reasons.append('aktivní výstraha ČHMÚ pro bod')
    if lightning.get('ok') and finite(lightning.get('strikes_20km_10min')) and float(lightning['strikes_20km_10min'])>0: rank=max(rank,3); reasons.append(f"blesky do 20 km: {lightning['strikes_20km_10min']}")
    if finite(r25) and r25>=45 and finite(cape) and cape>=500: rank=max(rank,2); reasons.append(f'radar do 25 km max {r25} dBZ + CAPE ~{cape:.0f} J/kg')
    elif finite(r50) and r50>=40 and finite(cape) and cape>=400: rank=max(rank,1); reasons.append(f'silnější radarová aktivita do 50 km ({r50} dBZ) + nestabilita')
    if radar.get('trend_30m')=='approaching' and rank>0: reasons.append('silnější echo se přibližuje')
    out.append({'id':'storm','name':'Bouřky','icon':'⛈️','level':level(rank),'rank':rank,'headline':['bez lokálního signálu','zvýšená pozornost','výrazné riziko','bezprostřední bouřková aktivita'][rank],'reasons':reasons or ['radar/modely nyní nedávají silný lokální signál'],'confidence':'high' if lightning.get('ok') or 'bouř' in official_text else 'medium' if rank>=1 else 'medium'})

    # Heavy rain / runoff
    rank=0; reasons=[]
    if finite(r10) and r10>=50: rank=3; reasons.append(f'velmi silné radarové echo do 10 km ({r10} dBZ)')
    elif finite(r25) and r25>=45: rank=2; reasons.append(f'silné radarové echo do 25 km ({r25} dBZ)')
    elif finite(p1) and p1>=5: rank=1; reasons.append(f'modelový konsenzus max ~{p1:.1f} mm/h')
    if finite(p6) and p6>=15: rank=max(rank,2); reasons.append(f'cca {p6:.1f} mm / 6 h v modelovém konsenzu')
    if rain['rain_7d_mm']>=50 and rank>0: rank=min(3,rank+1); reasons.append(f'krajina už dostala {rain["rain_7d_mm"]:.1f} mm / 7 dní')
    out.append({'id':'runoff','name':'Přívalový déšť / odtok','icon':'🌧️','level':level(rank),'rank':rank,'headline':['bez zvýšeného rizika','sledovat','zvýšené riziko','vysoké lokální riziko'][rank],'reasons':reasons or ['radar ani krátkodobé modely nyní neukazují výraznou srážkovou zátěž'],'confidence':'medium'})

    # Wind
    rank=0; reasons=[]
    if finite(gust):
        if gust>=75: rank=3
        elif gust>=60: rank=2
        elif gust>=45: rank=1
        reasons.append(f'modelový konsenzus nárazů do 12 h ~{gust:.0f} km/h')
    if 'vítr' in official_text: rank=max(rank,2); reasons.append('aktivní výstraha ČHMÚ pro bod')
    out.append({'id':'wind','name':'Silný vítr','icon':'🌬️','level':level(rank),'rank':rank,'headline':['bez zvýšeného rizika','zesílený vítr','silný vítr','velmi silný vítr'][rank],'reasons':reasons or ['modely nyní neukazují nebezpečné nárazy'],'confidence':'high' if 'vítr' in official_text else 'medium'})

    # Drying / drought memory
    rank=0; reasons=[]; perc=sat.get('seasonal_percentile') if sat.get('ok') else None
    cov=(rain.get('coverage_fraction') or {}); cov14=float(cov.get(14,0)); cov30=float(cov.get(30,0))
    if cov14>=0.80 and rain['rain_14d_mm']<8: rank=1; reasons.append(f'jen {rain["rain_14d_mm"]:.1f} mm / 14 dní')
    if cov30>=0.80 and rain['rain_30d_mm']<20: rank=max(rank,2); reasons.append(f'jen {rain["rain_30d_mm"]:.1f} mm / 30 dní')
    if finite(perc) and perc<=25: rank=max(rank,1); reasons.append(f'aktuální NDMI travních ploch je v dolní čtvrtině letošních scén ({perc:.0f}. percentil)')
    if finite(perc) and perc<=15 and cov30>=0.80 and rain['rain_30d_mm']<25: rank=max(rank,2)
    if cov30<0.80: reasons.append(f'30denní surová srážková historie zatím pokrývá jen {cov30*100:.0f} % období — dlouhodobý srážkový práh se proto nepoužívá')
    out.append({'id':'dry','name':'Vysychání krajiny','icon':'🔥','level':level(rank),'rank':rank,'headline':['bez potvrzeného výrazného signálu','zvýšené vysychání','výrazné vysychání','extrémní lokální vysychání'][rank],'reasons':reasons or ['srážková historie a Sentinel zatím nedávají výrazný lokální signál'],'confidence':'medium' if cov30>=0.80 else 'low_to_medium','note':'Nejde o oficiální klasifikaci sucha; je to lokální stav observatoře. Dlouhodobé prahy se aktivují až při dostatečném datovém pokrytí.'})

    # Heat
    rank=0; reasons=[]
    if finite(tmax):
        if tmax>=35: rank=3
        elif tmax>=32: rank=2
        elif tmax>=29: rank=1
        reasons.append(f'modelový konsenzus maxima do 12 h ~{tmax:.1f} °C')
    if 'teplot' in official_text or 'hork' in official_text: rank=max(rank,2); reasons.append('aktivní výstraha ČHMÚ pro bod')
    out.append({'id':'heat','name':'Horko','icon':'🌡️','level':level(rank),'rank':rank,'headline':['bez zvýšeného rizika','teplá zátěž','výrazné horko','extrémní horko'][rank],'reasons':reasons or ['modely nyní neukazují výrazné horko'],'confidence':'medium'})

    # Frost
    rank=0; reasons=[]
    if finite(tmin):
        if tmin<=-3: rank=3
        elif tmin<=0: rank=2
        elif tmin<=2: rank=1
        reasons.append(f'nejnižší modelový scénář do 12 h ~{tmin:.1f} °C')
    out.append({'id':'frost','name':'Mráz','icon':'❄️','level':level(rank),'rank':rank,'headline':['bez rizika','pozor na chlad','riziko mrazu','silný mráz'][rank],'reasons':reasons or ['bez lokálního mrazového signálu'],'confidence':'medium','note':'Později se práh upraví podle lokální chladové predispozice NH-REF.'})
    return out


def main():
    now=datetime.now(TZ)
    models=model_forecasts(); rain=rain_totals(); sat=sentinel_context(); radar=radar_local(); cap=cap_official(); lightning=lightning_optional()
    alerts=build_alerts(models,rain,sat,radar,cap,lightning)
    payload={'schema':1,'location':{'name':'Nové Hraběcí','lat':LAT,'lon':LON},'computed_at_local':now.isoformat(),'alerts':alerts,'sources':{'models':models,'rain':rain,'sentinel':sat,'radar':radar,'official_warnings':cap,'lightning':lightning},'method_note':'Deterministic local rules first; AI may explain these outputs but does not invent the warning state. Thresholds will be calibrated against the local archive over time.','safety_note':'This is an experimental local observatory, not a replacement for official emergency warnings.'}
    # Avoid noisy commits: persist immediately when alert levels/reasons change, otherwise at least hourly.
    normalized=json.dumps({'alerts':alerts,'radar_state':{'r10':((radar.get('latest') or {}).get('max_dbz') or {}).get('10km'),'r25':((radar.get('latest') or {}).get('max_dbz') or {}).get('25km'),'trend':radar.get('trend_30m')},'official':cap.get('active_for_point'),'lightning':lightning.get('status')},sort_keys=True,ensure_ascii=False)
    old=None
    if OUT.exists():
        try: old=json.loads(OUT.read_text(encoding='utf-8'))
        except Exception: old=None
    oldnorm=(old or {}).get('_fingerprint'); oldtime=None
    try: oldtime=datetime.fromisoformat((old or {}).get('computed_at_local'))
    except Exception: pass
    fp=hashlib.sha256(normalized.encode('utf-8')).hexdigest(); payload['_fingerprint']=fp
    force=oldtime is None or (now-oldtime).total_seconds()>=3600
    if oldnorm==fp and not force:
        print(json.dumps({'ok':True,'changed':False,'active':[a['id'] for a in alerts if a['rank']>0]},ensure_ascii=False)); return
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'changed':True,'active':[a['id'] for a in alerts if a['rank']>0],'output':'data/alerts.json'},ensure_ascii=False))

if __name__=='__main__': main()
