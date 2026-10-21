// Registers the service worker and powers the optional install button (#installBtn).
(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    });
  }
  var deferred = null;
  var isStandalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  function btn() { return document.getElementById('installBtn'); }
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault(); deferred = e;
    if (btn() && !isStandalone) btn().hidden = false;
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('#installBtn')) return;
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.then(function () { deferred = null; if (btn()) btn().hidden = true; });
  });
  window.addEventListener('appinstalled', function () { if (btn()) btn().hidden = true; });
  // iPhone: no install prompt exists, show a hint instead
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (ios && !isStandalone) {
    document.addEventListener('DOMContentLoaded', function () {
      var h = document.getElementById('iosInstallHint'); if (h) h.hidden = false;
    });
  }
})();
