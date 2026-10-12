// Service Worker: zeigt Push-Benachrichtigungen an und öffnet beim Tippen die App.
// Bewusst kein Zwischenspeichern von Seiten: die Oberfläche soll immer den aktuellen Stand laden.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {body: event.data ? event.data.text() : ''}; }
  const title = data.title || 'MeshCore Remote';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    tag: data.tag || undefined,          // gleicher Kanal ersetzt die vorige Meldung statt zu stapeln
    renotify: !!data.tag,
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    data: {url: data.url || '/'},
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of all) {
      if ('focus' in c) { await c.focus(); if ('navigate' in c) { try { await c.navigate(url); } catch { /* egal */ } } return; }
    }
    if (self.clients.openWindow) await self.clients.openWindow(url);
  })());
});
