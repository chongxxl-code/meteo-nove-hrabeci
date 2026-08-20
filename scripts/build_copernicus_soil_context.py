#!/usr/bin/env python3
from __future__ import annotations

import csv, io, json, math, os, tempfile, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'copernicus-soil.json'
LAT,LON=51.0162,14.4398
UA='nove-hrabeci-local-observatory/1.0'
ENDPOINT='https://eodata.dataspace.copernicus.eu'
CATALOGS={
 'ssm':('https://s3.waw3-1.cloudferro.com/swift/v1/CatalogueCSV/bio-geophysical/surface_soil_moisture/ssm_europe_1km_daily_v1/ssm_europe_1km_daily_v1_cog.csv','surface_soil_moisture'),
 'swi':('https://s3.waw3-1.cloudferro.com/swift/v1/CatalogueCSV/bio-geophysical/soil_water_index/swi_europe_1km_daily_v2/swi_europe_1km_daily_v2_cog.csv','soil_water_index'),
}

def finite(v):
    try:return math.isfinite(float(v))
    except:return False

def get_text(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=60) as r:return r.read().decode('utf-8','replace')

def latest_product(url):
    rows=list(csv.DictReader(io.StringIO(get_text(url)),delimiter=';'))
    rows=[r for r in rows if r.get('nominal_date') and r.get('s3_path')]
    rows.sort(key=lambda r:r.get('nominal_date') or '')
    if not rows:raise RuntimeError('catalog has no products')
    return rows[-1]

def s3_client():
    import boto3
    from botocore.client import Config
    from botocore import UNSIGNED
    key=os.getenv('CDSE_S3_ACCESS_KEY'); secret=os.getenv('CDSE_S3_SECRET_KEY')
    if key and secret:
        return boto3.client('s3',endpoint_url=ENDPOINT,aws_access_key_id=key,aws_secret_access_key=secret,region_name='default',config=Config(signature_version='s3v4',s3={'addressing_style':'path'})), 'signed'
    return boto3.client('s3',endpoint_url=ENDPOINT,region_name='default',config=Config(signature_version=UNSIGNED,s3={'addressing_style':'path'})), 'unsigned'

def list_tiffs(client,s3_path):
    prefix=s3_path.replace('s3://eodata/','').rstrip('/')+'/'
    out=[]; token=None
    while True:
        kw={'Bucket':'eodata','Prefix':prefix,'MaxKeys':1000}
        if token:kw['ContinuationToken']=token
        d=client.list_objects_v2(**kw)
        out.extend(x['Key'] for x in d.get('Contents',[]) if x['Key'].lower().endswith(('.tif','.tiff')))
        if not d.get('IsTruncated'):break
        token=d.get('NextContinuationToken')
    return out

def choose_key(keys,product):
    low=[(k,k.lower()) for k in keys]
    if product=='ssm':
        good=[k for k,l in low if ('-ssm_' in l or '_ssm_' in l) and 'noise' not in l and 'flag' not in l and '_ql_' not in l]
        return good[0] if good else (keys[0] if keys else None)
    # SWI: T=20 gives a useful intermediate profile-memory indicator; also sample 60 if present later.
    for token in ('swi_020','-swi020','swi020'):
        good=[k for k,l in low if token in l and 'qflag' not in l and '_ql_' not in l]
        if good:return good[0]
    return keys[0] if keys else None

def choose_swi60(keys):
    low=[(k,k.lower()) for k in keys]
    for token in ('swi_060','-swi060','swi060'):
        good=[k for k,l in low if token in l and 'qflag' not in l and '_ql_' not in l]
        if good:return good[0]
    return None

def sample_tiff(client,key):
    import rasterio
    with tempfile.NamedTemporaryFile(suffix='.tiff') as tmp:
        client.download_fileobj('eodata',key,tmp)
        tmp.flush()
        with rasterio.open(tmp.name) as ds:
            vals=list(ds.sample([(LON,LAT)]))[0]
            raw=float(vals[0]) if len(vals) else None
            nodata=ds.nodata
            desc=ds.descriptions[0] if ds.descriptions else None
            scale=(ds.scales[0] if ds.scales and finite(ds.scales[0]) else 1.0)
            offset=(ds.offsets[0] if ds.offsets and finite(ds.offsets[0]) else 0.0)
            physical=None
            if finite(raw) and (nodata is None or raw!=nodata) and raw!=255:
                physical=raw*scale+offset
                # Legacy CLMS COGs sometimes omit scale metadata; official SSM/SWI encoding is 0.5.
                if scale==1.0 and offset==0.0 and 0<=raw<=200:physical=raw*0.5
            return {'key':key,'raw':raw,'value_percent':round(physical,2) if finite(physical) else None,'band_description':desc,'scale_used':scale if scale!=1.0 else (0.5 if finite(raw) and 0<=raw<=200 else scale),'nodata':nodata,'crs':str(ds.crs),'resolution':[abs(ds.transform.a),abs(ds.transform.e)]}

def product_context(name,catalog_url):
    prod=latest_product(catalog_url)
    client,mode=s3_client()
    result={'ok':False,'product':name,'nominal_date':prod.get('nominal_date'),'name':prod.get('name'),'id':prod.get('id'),'s3_path':prod.get('s3_path'),'access_mode':mode}
    try:
        keys=list_tiffs(client,prod['s3_path'])
        result['tiff_count']=len(keys)
        key=choose_key(keys,name)
        if not key:raise RuntimeError('no TIFF found in product folder')
        result['primary']=sample_tiff(client,key)
        if name=='swi':
            key60=choose_swi60(keys)
            if key60 and key60!=key:result['swi_060']=sample_tiff(client,key60)
        result['ok']=finite((result.get('primary') or {}).get('value_percent'))
        if not result['ok']:result['quality_note']='NH-REF pixel is masked/no-data in latest product'
    except Exception as e:
        result['error']=str(e)[:400]
        if mode=='unsigned':result['access_status']='catalog_public_but_eodata_credentials_required_or_unsigned_denied'
    return result

def main():
    products={}
    for k,(url,_) in CATALOGS.items():
        try:products[k]=product_context(k,url)
        except Exception as e:products[k]={'ok':False,'product':k,'error':str(e)[:400]}
    ok_any=any(v.get('ok') for v in products.values())
    payload={'schema':1,'location':{'name':'Nové Hraběcí','lat':LAT,'lon':LON},'computed_at_utc':datetime.now(timezone.utc).isoformat(),'quality_status':'operational_copernicus_soil_1km' if ok_any else 'catalog_connected_pixel_access_pending','products':products,'method_note':'Independent CLMS 1 km satellite soil-moisture control. SSM is top-soil percent saturation; SWI is profile soil-moisture index. It supplements, not replaces, CHMI rain, P-ET0 and Sentinel-2 NDMI.'}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok_any':ok_any,'status':payload['quality_status']},ensure_ascii=False))
if __name__=='__main__':main()
