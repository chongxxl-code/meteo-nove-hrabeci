#!/usr/bin/env python3
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'index.html'
s=p.read_text(encoding='utf-8')
css='''.risks{grid-column:span 12}.riskgrid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-top:12px}.riskitem{background:var(--p2);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:12px;padding:11px;min-width:0}.riskitem.green{border-left-color:var(--g)}.riskitem.yellow{border-left-color:#ffd36a}.riskitem.orange{border-left-color:#ff9d55}.riskitem.red{border-left-color:#ff6f66}.riskname{font-size:12px;color:var(--mu);font-weight:800}.riskheadline{font-weight:900;margin-top:5px;line-height:1.25}.riskmeta{font-size:11px;color:var(--mu);margin-top:5px}.risklink{color:var(--a);text-decoration:none;font-weight:800}@media(max-width:900px){.riskgrid{grid-template-columns:repeat(3,1fr)}}@media(max-width:520px){.riskgrid{grid-template-columns:1fr 1fr}}'''
if '.riskgrid{' not in s:
    s=s.replace('</style>',css+'</style>',1)
html='''<section class="card risks"><div class="sect"><h2>Lokální rizika</h2><a class="risklink" href="./alerts.html">detail →</a></div><div class="riskgrid" id="riskgrid"><div class="muted">Načítám lokální varovný stav…</div></div><div class="note" id="risknote">Varování jsou počítaná přímo pro NH-REF z radaru, modelů, ČHMÚ a paměti krajiny. AI stav sama nevymýšlí.</div></section>\n'''
if 'id="riskgrid"' not in s:
    s=s.replace('<section class="card radar">',html+'<section class="card radar">',1)
js='''async function localAlerts(){try{const d=await j(`./data/alerts.json?_=${Date.now()}`),root=$('riskgrid');root.innerHTML=(d.alerts||[]).map(a=>`<div class="riskitem ${a.level}"><div class="riskname">${a.icon} ${a.name}</div><div class="riskheadline">${a.headline}</div><div class="riskmeta">${(a.reasons||[])[0]||''}</div></div>`).join('');const active=(d.alerts||[]).filter(a=>a.rank>0);$('risknote').textContent=active.length?`Aktivní lokální signály: ${active.map(a=>a.name).join(', ')}. Aktualizace ${new Intl.DateTimeFormat('cs-CZ',{hour:'2-digit',minute:'2-digit'}).format(new Date(d.computed_at_local))}.`:`Bez zvýšeného lokálního rizika · aktualizace ${new Intl.DateTimeFormat('cs-CZ',{hour:'2-digit',minute:'2-digit'}).format(new Date(d.computed_at_local))}.`; }catch(e){$('riskgrid').innerHTML='<div class="muted">Lokální varovný stav se nepodařilo načíst.</div>'}}\n'''
if 'async function localAlerts()' not in s:
    s=s.replace('let map,layer,meta,frames=[];',js+'let map,layer,meta,frames=[];',1)
if 'load();central();landscape();localAlerts();initMap();' not in s:
    s=s.replace('load();central();landscape();initMap();','load();central();landscape();localAlerts();initMap();',1)
p.write_text(s,encoding='utf-8')
print('local risk UI applied')
