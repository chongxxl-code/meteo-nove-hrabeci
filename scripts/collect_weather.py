#!/usr/bin/env python3
from __future__ import annotations
import json,time,urllib.parse,urllib.request
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LAT=51.0162; LON=14.4398; TZ='Europe/Prague'
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/'data'; ARCHIVE=DATA/'archive'
HOURLY=['temperature_2m','precipitation','cloud_cover','wind_speed_10m','wind_gusts_10m']
CURRENT=['temperature_2m','relative_humidity_2m','precipitation','cloud_cover','pressure_msl','wind_speed_10m','wind_gusts_10m']
UA='nove-hrabeci-meteo/0.4 (+github-actions)'

def get_json(url,timeout=25,tries=3):
    err=None
    for attempt in range(tries):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
            with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode('utf-8'))
        except Exception as exc:
            err=exc
            if attempt+1<tries:time.sleep(2+attempt*2)
    raise RuntimeError(f'GET failed: {err}')

def make_url(base,params):return base+'?'+urllib.parse.urlencode(params,doseq=True)
def forecast_url(model=None):
    p={'latitude':LAT,'longitude':LON,'timezone':TZ,'forecast_days':3,'hourly':','.join(HOURLY)}
    if model:p['models']=model
    return make_url('https://api.open-meteo.com/v1/forecast',p)
def ecmwf_url():return make_url('https://api.open-meteo.com/v1/ecmwf',{'latitude':LAT,'longitude':LON,'timezone':TZ,'forecast_days':3,'hourly':','.join(HOURLY)})
def current_url():return make_url('https://api.open-meteo.com/v1/forecast',{'latitude':LAT,'longitude':LON,'timezone':TZ,'forecast_days':1,'current':','.join(CURRENT)})
def fetch_with_fallback(urls):
    last=None
    for u in urls:
        try:return get_json(u)
        except Exception as exc:last=exc
    raise RuntimeError(last)
def trim_hourly(payload,hours=48):
    h=payload.get('hourly') or {}; times=h.get('time') or []
    key=datetime.now(ZoneInfo(TZ)).replace(minute=0,second=0,microsecond=0).strftime('%Y-%m-%dT%H:00')
    try:start=times.index(key)
    except ValueError:start=0
    end=min(len(times),start+hours); out={'time':times[start:end]}
    for k in HOURLY:
        vals=h.get(k)
        if isinstance(vals,list):out[k]=vals[start:end]
    return out

def main():
    DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
    now_utc=datetime.now(timezone.utc); now_local=now_utc.astimezone(ZoneInfo(TZ)); errors=[]
    try:current=get_json(current_url()).get('current')
    except Exception as exc:current=None; errors.append(f'current: {exc}')
    models={}
    jobs={
      'dwd':[forecast_url('dwd_icon_d2'),forecast_url('dwd_icon_seamless')],
      'chmi':[forecast_url('chmi_aladin_cz_1km'),forecast_url('chmi_aladin_seamless')],
      'ec':[ecmwf_url()]}
    for name,urls in jobs.items():
        try:models[name]=fetch_with_fallback(urls)
        except Exception as exc:errors.append(f'{name}: {exc}')
    try:rv=get_json('https://api.rainviewer.com/public/weather-maps.json')
    except Exception as exc:rv=None; errors.append(f'rainviewer: {exc}')
    if not models:raise SystemExit('No forecast model could be collected.')
    snapshot={'collected_at_utc':now_utc.isoformat().replace('+00:00','Z'),'collected_at_local':now_local.isoformat(),'location':{'name':'Nové Hraběcí','latitude':LAT,'longitude':LON,'timezone':TZ},'current':current,'models':{n:trim_hourly(p) for n,p in models.items()}}
    month=ARCHIVE/f'{now_local:%Y-%m}.jsonl'
    with month.open('a',encoding='utf-8') as f:f.write(json.dumps(snapshot,ensure_ascii=False,separators=(',',':'))+'\n')
    status_path=DATA/'status.json'; prev=0
    if status_path.exists():
        try:prev=int(json.loads(status_path.read_text(encoding='utf-8')).get('total_snapshots',0))
        except Exception:pass
    frames=((rv or {}).get('radar') or {}).get('past') or []; frames=frames[-12:]; latest=frames[-1].get('time') if frames else None
    model_status={}
    for name in ('dwd','chmi','ec'):
        p=models.get(name); vals=((p or {}).get('hourly') or {}).get('temperature_2m') or []
        model_status[name]={'ok':bool(p),'temperature_2m':vals[0] if vals else None}
    status={'schema':1,'collected_at_utc':snapshot['collected_at_utc'],'collected_at_local':snapshot['collected_at_local'],'total_snapshots':prev+1,'archive_file':f'data/archive/{month.name}','models':model_status,'current':current,'radar':{'rainviewer_frames':len(frames),'rainviewer_latest_unix':latest,'rainviewer_latest_local':datetime.fromtimestamp(latest,timezone.utc).astimezone(ZoneInfo(TZ)).strftime('%d.%m.%Y %H:%M') if latest else None},'errors':errors,'note':'Forecast snapshots only; no local ground-truth station is connected yet.'}
    status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'models':list(models),'archive':str(month),'errors':errors},ensure_ascii=False))
if __name__=='__main__':main()
