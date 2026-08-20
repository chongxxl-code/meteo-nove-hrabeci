#!/usr/bin/env python3
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'about.html'
s=p.read_text(encoding='utf-8')
section='''<section class="card wide"><span class="tag">LOKÁLNÍ RIZIKA</span><h2>Varování přímo pro Nové Hraběcí</h2><p>Samostatný varovný engine počítá stav přímo pro NH-REF a jeho bezprostřední okolí. Nejde o obecnou předpověď pro okres. Kombinuje radarovou intenzitu a pohyb srážek, shodu numerických modelů, CAPE a nárazy větru, oficiální CAP výstrahy ČHMÚ, skutečné srážky ze Šluknova a dlouhodobou reakci krajiny ze Sentinelu.</p><div class="principles"><div class="stat"><b>Pevná pravidla</b><span>Barvu rizika určuje měřitelný práh a kombinace zdrojů, ne volná věta AI.</span></div><div class="stat"><b>Lokální paměť</b><span>Prahy se budou s delší historií kalibrovat podle toho, jak skutečně reaguje Nové Hraběcí.</span></div><div class="stat"><b>Oficiální autorita</b><span>Výstraha ČHMÚ má vyšší váhu; observatoř ji pouze zpřesňuje na konkrétní bod a lokální kontext.</span></div></div><p>Bleskový zdroj je připraven jako samostatný provider. Blitzortung se pro rozhodování varování nepoužívá, protože jeho zveřejněné podmínky použití výslovně zakazují použití dat pro bouřkové varovné systémy. Pokud připojíme vhodný licencovaný zdroj blesků, stane se další nezávislou vstupní vrstvou.</p></section>\n'''
marker='<section class="card"><span class="tag">CO AI DĚLÁ</span>'
if 'LOKÁLNÍ RIZIKA</span>' not in s:
    s=s.replace(marker,section+marker,1)
s=s.replace('<li>forecastový archiv přibližně každé 3 hodiny,</li>','<li>lokální rizikový engine přibližně každých 15 minut,</li><li>forecastový archiv přibližně každé 3 hodiny,</li>',1)
p.write_text(s,encoding='utf-8')
print('documented local alerts')
