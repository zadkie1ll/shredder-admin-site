{% verbatim %}
const CACHE_PREFIX = 'monkey-island-';
const CACHE_NAME = 'monkey-island-v3';
const OFFLINE_URL = '/static/pwa/offline.html';
const ASSETS = [
    OFFLINE_URL,
    '/manifest.json',
    '/static/icons/icon-192x192.png',
    '/static/icons/icon-512x512.png'
];

// Установка: кешируем базовые страницы
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS);
        })
    );
    self.skipWaiting();
});

// Активация: чистим старый кеш
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) =>
            Promise.all(
                cacheNames
                    .filter((cacheName) => cacheName.startsWith(CACHE_PREFIX) && cacheName !== CACHE_NAME)
                    .map((cacheName) => caches.delete(cacheName))
            )
        )
    );
    self.clients.claim();
    console.log('Service Worker activated');
});

// Перехват запросов (нужен для работы PWA офлайн)
self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;

    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(() => caches.match(OFFLINE_URL))
        );
        return;
    }

    const url = new URL(event.request.url);
    if (url.origin === self.location.origin && ASSETS.includes(url.pathname)) {
        event.respondWith(
            caches.match(event.request).then((cached) => cached || fetch(event.request))
        );
    }
});
{% endverbatim %}
