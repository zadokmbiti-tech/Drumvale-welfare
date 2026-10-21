// Drumvale PWA: registers the service worker and shows an always-visible "Install App" button.
(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    });
  }

  var deferred = null;
  var standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  var ua = navigator.userAgent;
  var isIOS = /iphone|ipad|ipod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  window.addEventListener('beforeinstallprompt', function (e) { e.preventDefault(); deferred = e; });
  window.addEventListener('appinstalled', function () { deferred = null; var b = document.getElementById('installBtn'); if (b) b.remove(); });

  function steps() {
    if (isIOS) return 'Tap the <b>Share</b> icon in Safari, then choose <b>Add to Home Screen</b>.';
    if (/firefox/i.test(ua)) return 'Open the browser menu and choose <b>Install</b> (or use Chrome/Edge for the best experience).';
    return 'Open your browser menu (&#8942;) and choose <b>Install app</b> or <b>Add to Home screen</b>. If you do not see it, open this site in Chrome.';
  }

  function showHelp() {
    if (document.getElementById('installHelp')) return;
    var o = document.createElement('div');
    o.id = 'installHelp';
    o.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.6);display:grid;place-items:center;padding:20px';
    o.innerHTML = '<div style="max-width:360px;background:#0d1b2a;color:#eaeef1;border:1px solid #2cb4e0;border-radius:14px;padding:22px;font-family:system-ui,sans-serif;line-height:1.5">' +
      '<h3 style="margin:0 0 10px;font-size:1.1rem">Install Drumvale</h3><p style="margin:0 0 16px">' + steps() + '</p>' +
      '<button type="button" style="padding:10px 20px;border:0;border-radius:8px;background:#2cb4e0;color:#082d47;font-weight:600;cursor:pointer">Got it</button></div>';
    o.addEventListener('click', function (e) { if (e.target === o || e.target.tagName === 'BUTTON') o.remove(); });
    document.body.appendChild(o);
  }

  function onInstallClick() {
    if (deferred) {
      deferred.prompt();
      deferred.userChoice.then(function () { deferred = null; });
    } else {
      showHelp();
    }
  }

  function addButton() {
    if (standalone || document.getElementById('installBtn')) return;
    var b = document.createElement('button');
    b.id = 'installBtn';
    b.type = 'button';
    b.textContent = '⬇ Install App';
    b.setAttribute('aria-label', 'Install Drumvale app');
    b.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483000;padding:12px 18px;border:0;border-radius:999px;' +
      'background:#2cb4e0;color:#082d47;font:600 .95rem system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.35);cursor:pointer';
    b.addEventListener('click', onInstallClick);
    document.body.appendChild(b);
  }

  if (document.body) addButton(); else document.addEventListener('DOMContentLoaded', addButton);
})();