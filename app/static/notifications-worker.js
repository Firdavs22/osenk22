self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const tabs = await self.clients.matchAll({type:'window',includeUncontrolled:true});
    const tab = tabs.find(t => new URL(t.url).pathname.startsWith('/admin'));
    const url = '/admin/orders';
    if (tab) { await tab.navigate(url); await tab.focus(); }
    else await self.clients.openWindow(url);
  })());
});
