// Service Worker — B3 Push Notifications
self.addEventListener('push', function(event) {
  const data = event.data ? event.data.json() : {};
  const title = data.title || '🚨 Alerta B3';
  const options = {
    body: data.body || 'Um alerta foi disparado',
    icon: data.icon || '/icon-192.png',
    badge: '/icon-72.png',
    tag: data.tag || 'b3-alerta',
    requireInteraction: true,
    data: { url: data.url || '/' },
    actions: [
      { action: 'ver', title: '📊 Ver no app' },
      { action: 'fechar', title: 'Fechar' }
    ]
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  if (event.action === 'ver' || !event.action) {
    event.waitUntil(clients.openWindow(event.notification.data.url || '/'));
  }
});

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
