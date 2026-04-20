{% verbatim %}
const CACHE_NAME = 'monkey-island-v1';
const ASSETS = [
    '/dashboard/',
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
});

// Активация: чистим старый кеш
self.addEventListener('activate', (event) => {
    console.log('Service Worker activated');
});

// Перехват запросов (нужен для работы PWA офлайн)
self.addEventListener('fetch', (event) => {
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});
{% endverbatim %}