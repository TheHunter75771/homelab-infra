// Entry point: start the app, then register the service worker (shell + last summary for the offline view).

import { startApp } from './app.js';

startApp(document.getElementById('app'));

if ('serviceWorker' in navigator && window.isSecureContext) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
      // No worker means no offline view; the app itself works the same.
    });
  });
}
