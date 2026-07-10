/* Service worker do Hércules
   Estratégia: estáticos em cache-first; páginas em network-first
   (dados financeiros precisam estar atualizados; o cache só segura offline). */
const CACHE = "hercules-v5";
const STATIC_ASSETS = [
  "/static/styles.css?v=6",
  "/static/vendor/lucide.min.js",
  "/static/vendor/chart.umd.min.js",
  "/static/herc.svg?v=2",
  "/static/herc-192.png",
  "/static/herc-512.png",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // Estáticos: cache primeiro, rede como reserva
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return resp;
      }))
    );
    return;
  }

  // Páginas: rede primeiro; se offline, tenta a última versão em cache
  event.respondWith(
    fetch(request)
      .then((resp) => {
        if (resp.ok && url.origin === self.location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return resp;
      })
      .catch(() => caches.match(request))
  );
});
