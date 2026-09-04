(() => {
  const headline = document.getElementById('headline');
  if (!headline) return;

  const LIVE_URL = 'https://raw.githubusercontent.com/chongxxl-code/meteo-nove-hrabeci/main/data/radar-nowcast.json';
  const STALE_MIN = 20;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const relationText = {
    miri_na_lokalitu: 'srážky míří k Novému Hraběcí',
    miji_lokalitu: 'sledované srážky podle aktuálního pohybu lokalitu míjejí',
    nad_nebo_u_lokality: 'srážky jsou už nad lokalitou nebo v jejím těsném okolí',
    bez_srazek_v_dosahu: 'v analyzovaném okolí není dostatečný srážkový signál',
    pohyb_neurcity: 'směr srážek zatím není dostatečně stabilní',
    neurceno: 'pohyb zatím nelze spolehlivě určit'
  };

  let last = null;

  function ageMin(n) {
    const t = Date.parse(n?.computed_at_utc || '');
    if (!Number.isFinite(t)) return null;
    return Math.max(0, Math.floor((Date.now() - t) / 60000));
  }

  function ageText(age) {
    if (!Number.isFinite(age)) return '';
    if (age < 1) return 'aktualizováno právě teď';
    if (age === 1) return 'aktualizováno před 1 min';
    return `aktualizováno před ${age} min`;
  }

  function render(n) {
    if (!n) return;
    last = n;
    const age = ageMin(n);
    const stamp = ageText(age);

    if (Number.isFinite(age) && age > STALE_MIN) {
      headline.innerHTML = `<b style="color:var(--a)">Radarový nowcast:</b> poslední serverový výpočet je zastaralý. <span class="muted">${esc(stamp)} · čekám na nová radarová data.</span>`;
      return;
    }
    if (n.status === 'error') {
      headline.innerHTML = `<b style="color:var(--a)">Radarový nowcast:</b> serverový výpočet je dočasně nedostupný.${stamp ? ` <span class="muted">${esc(stamp)} · radarová mapa zůstává funkční.</span>` : ' <span class="muted">Radarová mapa zůstává funkční.</span>'}`;
      return;
    }
    if (n.status === 'initializing') {
      headline.innerHTML = '<b style="color:var(--a)">Radarový nowcast:</b> serverová analýza se inicializuje. <span class="muted">ETA zatím není k dispozici.</span>';
      return;
    }

    const relation = relationText[n.relation] || relationText.neurceno;
    const motion = n.motion || {};
    const bits = [];
    if (motion.compass && Number.isFinite(Number(motion.speed_kmh))) {
      bits.push(`pohyb ${esc(motion.compass)} · ${Number(motion.speed_kmh).toFixed(0)} km/h`);
    }
    if (n.confidence) bits.push(`jistota ${esc(n.confidence)}`);
    if (stamp) bits.push(esc(stamp));
    bits.push('živá data z main');

    let main;
    if (Number.isFinite(Number(n.eta_min)) && n.relation === 'miri_na_lokalitu') {
      const eta = Math.max(0, Math.round(Number(n.eta_min)));
      main = `<b style="color:var(--a)">Radarový nowcast:</b> ${esc(relation)} · odhad příchodu přibližně za <b>${eta} min</b>.`;
    } else {
      main = `<b style="color:var(--a)">Radarový nowcast:</b> ${esc(relation)}.`;
    }
    const suffix = bits.length ? ` <span class="muted">${bits.join(' · ')}</span>` : '';
    headline.innerHTML = main + suffix;
  }

  async function load() {
    const ts = Date.now();
    try {
      let r;
      try {
        r = await fetch(`${LIVE_URL}?ts=${ts}`, {cache:'no-store', mode:'cors'});
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
      } catch (e) {
        r = await fetch(`./data/radar-nowcast.json?ts=${ts}`, {cache:'no-store'});
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
      }
      render(await r.json());
    } catch (e) {
      if (last) render(last);
      else headline.innerHTML = '<b style="color:var(--a)">Radarový nowcast:</b> serverový výpočet je dočasně nedostupný. <span class="muted">Radarová mapa zůstává funkční.</span>';
    }
  }

  load();
  setTimeout(load, 1500);
  setTimeout(load, 5000);
  setInterval(() => {
    if (last) render(last);
    load();
  }, 60000);

  window.addEventListener('focus', load);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) load();
  });

  const observer = new MutationObserver(() => {
    if (!last) return;
    observer.disconnect();
    setTimeout(() => render(last), 0);
    setTimeout(() => observer.observe(headline, {childList:true, subtree:true, characterData:true}), 50);
  });
  observer.observe(headline, {childList:true, subtree:true, characterData:true});
})();
