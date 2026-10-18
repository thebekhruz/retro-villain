/* Обработчик уведомлений Retro Milliy (Web Push).
 *
 * Только уведомления: запросы страниц не перехватываем и ничего не кешируем,
 * поэтому панель работает так же, как без него. Отдаётся с адреса /sw.js —
 * область действия весь сайт.
 *
 * Safari (Mac, iPhone) отзывает подписку, если push пришёл, а уведомление не
 * показали, поэтому показываем на каждый push, даже пустой. */
'use strict';

const FALLBACK_URL = '/founder';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data = {};
  if (event.data) {
    try { data = event.data.json(); } catch { data = {body: event.data.text()}; }
  }
  const options = {
    body: data.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-maskable-192.png',
    data: {url: data.url || FALLBACK_URL},
    lang: 'ru',
  };
  // Метка у каждого заказа своя: уведомления о разных заказах не заменяют друг друга.
  if (data.tag) options.tag = data.tag;
  event.waitUntil(self.registration.showNotification(data.title || 'Retro Milliy', options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || FALLBACK_URL,
    self.location.origin);
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
    const open = windows.find(client => new URL(client.url).pathname === target.pathname);
    if (open && 'focus' in open) return open.focus();
    return self.clients.openWindow(target.href);
  }));
});

/* Firefox и Chrome иногда сами меняют подписку (истёк срок, сброс службы).
 * Подписываемся заново тем же ключом и сообщаем серверу новый адрес. */
self.addEventListener('pushsubscriptionchange', event => {
  const decode = text => {
    const base64 = (text + '='.repeat((4 - text.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from(atob(base64), char => char.charCodeAt(0));
  };
  event.waitUntil(fetch('/api/founder/push', {credentials: 'same-origin'})
    .then(response => (response.ok ? response.json() : Promise.reject(new Error('push key'))))
    .then(({key}) => self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: decode(key)}))
    .then(subscription => fetch('/api/founder/push/subscribe', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(subscription.toJSON()),
    }))
    .catch(() => {}));
});
