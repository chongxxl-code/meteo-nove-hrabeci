(() => {
  const headline = document.getElementById('headline');
  if (!headline) return;

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

  function render(n) {
    if (!n) return;
    last = n;
    if (n.status === 'error') {
      headline.innerHTML = '<b style="color:var(--a)">Radarový nowcast:</b> serverový výpočet je dočasně nedostupný. <span class="muted">Radarová mapa zůstává funkční.</span>';
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

  function load() {
    fetch('./data/radar-nowcast.json?ts=' + Date.now(), {cache:'no-store'})
      .then(r => r.ok ? r.json() : Promise.reject(new Error('nowcast unavailable')))
      .then(render)
      .catch(() => {});
  }

  // The original radar loader also writes into #headline asynchronously.
  // Re-apply the server result after it finishes, then refresh periodically.
  load();
  setTimeout(load, 1500);
  setTimeout(load, 5000);
  setInterval(load, 60000);

  // If another script overwrites the headline later, restore the last server result once.
  const observer = new MutationObserver(() => {
    if (!last) return;
    observer.disconnect();
    setTimeout(() => render(last), 0);
    setTimeout(() => observer.observe(headline, {childList:true, subtree:true, characterData:true}), 50);
  });
  observer.observe(headline, {childList:true, subtree:true, characterData:true});
})();
