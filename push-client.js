(() => {
  const CONFIG = './data/push-config.json';

  function addButton() {
    const sect = document.querySelector('.risks .sect');
    if (!sect || document.getElementById('pushCriticalBtn')) return null;
    const b = document.createElement('button');
    b.id = 'pushCriticalBtn';
    b.type = 'button';
    b.textContent = '🔔 Nouzová upozornění';
    b.style.cssText = 'border:1px solid #2c5962;background:#0a1a21;color:#edf8fb;border-radius:10px;padding:8px 10px;font-weight:800;cursor:pointer;margin-left:auto';
    b.title = 'Pouze skutečně kritické lokální výstrahy';
    sect.appendChild(b);
    return b;
  }

  function showError(btn, prefix, err) {
    const msg = String(err?.message || err || 'Neznámá chyba');
    btn.disabled = false;
    btn.textContent = '🔕 ' + prefix;
    btn.title = msg;
    let d = document.getElementById('pushCriticalError');
    if (!d) {
      d = document.createElement('div');
      d.id = 'pushCriticalError';
      d.style.cssText = 'margin-top:8px;color:#ff9d55;font-size:12px;line-height:1.4;word-break:break-word';
      btn.closest('.risks')?.appendChild(d);
    }
    d.textContent = 'Push chyba: ' + msg;
    console.warn(prefix, err);
  }

  async function init() {
    let cfg;
    try {
      cfg = await fetch(`${CONFIG}?_=${Date.now()}`, { cache: 'no-store' }).then(r => r.json());
    } catch (_) { return; }
    if (!cfg.enabled || !cfg.app_id) return;

    const btn = addButton();
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = '🔔 Připravuji upozornění…';

    window.OneSignalDeferred = window.OneSignalDeferred || [];
    let callbackStarted = false;
    const sdkTimeout = setTimeout(() => {
      if (!callbackStarted) showError(btn, 'Push SDK selhal', new Error('OneSignal SDK se do 12 s nespustilo'));
    }, 12000);

    window.OneSignalDeferred.push(async function(OneSignal) {
      callbackStarted = true;
      clearTimeout(sdkTimeout);
      try {
        await OneSignal.init({
          appId: cfg.app_id,
          serviceWorkerPath: 'push/onesignal/OneSignalSDKWorker.js',
          serviceWorkerParam: { scope: '/meteo-nove-hrabeci/push/onesignal/' },
          notifyButton: { enable: false },
          welcomeNotification: { disable: true }
        });

        const refresh = () => {
          const on = !!OneSignal.User.PushSubscription.optedIn;
          btn.disabled = false;
          btn.textContent = on ? '🔔 Kritické push: zapnuto' : '🔔 Zapnout kritické push';
          btn.style.borderColor = on ? '#70e2a1' : '#2c5962';
          const d = document.getElementById('pushCriticalError');
          if (d) d.remove();
        };
        refresh();
        OneSignal.User.PushSubscription.addEventListener('change', refresh);

        btn.addEventListener('click', async () => {
          const isiOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
          const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
          if (isiOS && !standalone) {
            alert('Na iPhonu musí být observatoř nejdřív přidaná na plochu a otevřená z ikony. Potom lze upozornění povolit.');
            return;
          }
          try {
            if (OneSignal.User.PushSubscription.optedIn) {
              const off = confirm('Kritická upozornění jsou zapnutá. Chceš je vypnout?');
              if (off) await OneSignal.User.PushSubscription.optOut();
            } else {
              await OneSignal.User.PushSubscription.optIn();
            }
            refresh();
          } catch (e) {
            alert('Upozornění se nepodařilo nastavit: ' + (e?.message || e));
          }
        });
      } catch (e) {
        showError(btn, 'Push init selhal', e);
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
