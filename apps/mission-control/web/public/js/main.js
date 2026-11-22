// Entry point: start the app, then register the service worker (offline shell, push, notification taps).

import { startApp } from './app.js';

startApp(document.getElementById('app'));

if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
    // No worker: no offline copy and no push. The cards still work.
  });
}
