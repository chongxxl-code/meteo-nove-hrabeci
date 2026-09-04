(() => {
  const LIVE_BASE = 'https://raw.githubusercontent.com/chongxxl-code/meteo-nove-hrabeci/main/';
  const fmtAge = m => {
    if (m == null) return '—';
    if (m < 90) return `${Math.round(m)} min`;
    return `${(m/60).toFixed(m >= 600 ? 0 : 1)} h`;
  };
  const labels = {ok:'OK', delayed:'ZPOŽDĚNÍ', stale:'STALE', error:'CHYBA'};
  const colors = {ok:'var(--g)', delayed:'var(--w)', stale:'#ff9d55', error:'#ff6f66'};
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  async function liveJson(path) {
    const ts = Date.now();
    try {
      const r = await fetch(`${LIVE_BASE}${path}?ts=${ts}`, {cache:'no-store', mode:'cors'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      const r = await fetch(`./${path}?ts=${ts}`, {cache:'no-store'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    }
  }

  async function loadHealth(){
    const central = document.querySelector('.central');
    if (!central) return;
    let host = document.getElementById('healthgrid');
    if (!host) {
      host = document.createElement('div');
      host.id = 'healthgrid';
      host.className = 'centralgrid';
      host.style.marginTop = '10px';
      const note = document.getElementById('centralnote');
      (note || central.lastElementChild)?.after(host);
    }
    try {
      const h = await liveJson('data/health-status.json');
      host.innerHTML = '';
      (h.sources || []).forEach(s => {
        const d = document.createElement('div');
        d.className = 'stat';
        const c = colors[s.status] || 'var(--mu)';
        d.innerHTML = `<b style="color:${c}">${labels[s.status] || esc(s.status)}</b><span>${esc(s.label)} · sběr ${fmtAge(s.check_age_minutes)}${s.observation_age_minutes != null ? ` · data ${fmtAge(s.observation_age_minutes)}` : ''}</span>`;
        host.appendChild(d);
      });
      const note = document.getElementById('centralnote');
      if (note) {
        note.textContent = h.overall_status === 'ok'
          ? 'Health monitor: všechny sledované zdroje jsou v povoleném intervalu čerstvosti.'
          : 'Health monitor zachytil zpožděný nebo zastaralý zdroj. Detail je vidět ve stavech výše.';
      }
    } catch (e) {
      host.innerHTML = '<div class="stat"><b style="color:#ff6f66">CHYBA</b><span>Health monitor není dostupný</span></div>';
    }
  }

  async function loadLiveAlerts() {
    const root = document.getElementById('riskgrid');
    const note = document.getElementById('risknote');
    if (!root) return;
    try {
      const d = await liveJson('data/alerts.json');
      root.innerHTML = (d.alerts || []).map(a =>
        `<div class="riskitem ${esc(a.level)}"><div class="riskname">${esc(a.icon)} ${esc(a.name)}</div><div class="riskheadline">${esc(a.headline)}</div><div class="riskmeta">${esc((a.reasons || [])[0] || '')}</div></div>`
      ).join('');
      const active = (d.alerts || []).filter(a => Number(a.rank) > 0);
      const stamp = d.computed_at_local
        ? new Intl.DateTimeFormat('cs-CZ',{hour:'2-digit',minute:'2-digit'}).format(new Date(d.computed_at_local))
        : '—';
      if (note) note.textContent = active.length
        ? `Aktivní lokální signály: ${active.map(a => a.name).join(', ')}. Aktualizace ${stamp} · živá data z main.`
        : `Bez zvýšeného lokálního rizika · aktualizace ${stamp} · živá data z main.`;
    } catch (e) {
      // Keep the page-rendered value as a fallback; do not replace it with an error if Pages data exists.
    }
  }

  function refreshAll() {
    loadHealth();
    loadLiveAlerts();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refreshAll);
  else refreshAll();
  setTimeout(loadLiveAlerts, 1500);
  setInterval(refreshAll, 60000);
  window.addEventListener('focus', refreshAll);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });
})();
