#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / 'alerts.html'
s = p.read_text(encoding='utf-8')

old = "const s=d.sources||{},r=s.radar||{},rr=(r.latest||{}).max_dbz||{},rain=s.rain||{},sat=s.sentinel||{},cap=s.official_warnings||{},li=s.lightning||{};"
new = "const s=d.sources||{},r=s.radar||{},rr=(r.latest||{}).max_dbz||{},rain=s.rain||{},sat=s.sentinel||{},cap=s.official_warnings||{},li=s.lightning||{},cl=s.chmi_lightning||{},dr=s.drought||{},ds=dr.state||{},dbg=dr.background||{},dw=(dbg.windows||{})['30']||{},soil=dbg.latest_soil||{};"
if old in s:
    s = s.replace(old, new, 1)

old_cards = "<div class=\"source\"><b>Blesky</b><span class=\"muted\">${li.ok?'aktivní '+li.provider:'samostatný provider zatím není nakonfigurován'}</span></div><div class=\"source\"><b>Princip</b><span class=\"muted\">pevná lokální pravidla → archiv → kalibrace podle skutečného chování Nového Hraběcí</span></div>"
new_cards = "<div class=\"source\"><b>ČHMÚ blesky · LINET</b><span class=\"muted\">živá oficiální vizualizace · <a href=\"${cl.url||'https://produkty.chmi.cz/radar/'}\" target=\"_blank\" rel=\"noopener\">otevřít blesky ↗</a><br>poloha výbojů cca 1 km; zatím vizuální potvrzující vrstva</span></div><div class=\"source\"><b>Lokální sucho</b><span class=\"muted\">${ds.headline||'–'} · P−ET₀ / 30 d: ${f(dw.p_minus_et0_mm)} mm · kořenová vlhkost: ${f(soil.rootzone_percentile_1y,0)}. percentil</span></div><div class=\"source\"><b>Strojový bleskový feed</b><span class=\"muted\">${li.ok?'aktivní '+li.provider:'zatím nepřipojen; ČHMÚ LINET zůstává živou vizuální kontrolou'}</span></div><div class=\"source\"><b>Princip</b><span class=\"muted\">pevná lokální pravidla → archiv → kalibrace podle skutečného chování Nového Hraběcí</span></div>"
if old_cards in s:
    s = s.replace(old_cards, new_cards, 1)

p.write_text(s, encoding='utf-8')

# Keep methodology aligned with the operational implementation.
p = ROOT / 'about.html'
s = p.read_text(encoding='utf-8')
s = s.replace(
    'Bleskový zdroj je připraven jako samostatný provider. Blitzortung se pro rozhodování varování nepoužívá, protože jeho zveřejněné podmínky použití výslovně zakazují použití dat pro bouřkové varovné systémy. Pokud připojíme vhodný licencovaný zdroj blesků, stane se další nezávislou vstupní vrstvou.',
    'ČHMÚ blesky LINET používáme jako živou oficiální vizuální kontrolní vrstvu. ČHMÚ uvádí přibližně kilometrovou přesnost lokalizace, samotná LINET data ale pocházejí z komerčně licencované sítě, proto je zatím automaticky nestahujeme jako strojový feed. Pro automatické počítání blesků zůstává připraven samostatný licencovaný provider slot.',
    1,
)
needle = '<p>Bleskový zdroj je připraven jako samostatný provider.'
# If the earlier sentence already changed or differs, append drought paragraph once within the local-risks section.
if 'P−ET₀' not in s:
    marker = '</section>\n<section class="card"><span class="tag">CO AI DĚLÁ</span>'
    extra = '<p>Vysychání krajiny má vlastní denní kontext: počítá srážky minus referenční evapotranspiraci P−ET₀ za 7/14/30 dní, povrchovou a kořenovou půdní vlhkost a jejich roční percentil. Výsledek zpřesňuje Sentinel NDMI na ručně kontrolované síti ploch. Jakmile vlastní ČHMÚ srážkový archiv dosáhne dostatečné délky, dostane v bilanci přednost před reanalýzním mostem.</p>'
    if marker in s:
        s = s.replace(marker, extra + '</section>\n<section class="card"><span class="tag">CO AI DĚLÁ</span>', 1)
p.write_text(s, encoding='utf-8')
print('drought + CHMI lightning UI documented')
