/* Service worker do Hércules
   Estratégia: estáticos em cache-first; páginas em network-first
   (dados financeiros precisam estar atualizados; o cache só segura offline). */
/* Uma versao so. Ela ficou em dois lugares e eles desencontraram: o cache subiu
   pra v35 e a lista de pre-cache continuou pedindo o v34, entao o arquivo
   guardado nunca era o que a pagina pedia — e offline a tela ficava sem CSS. */
const V = "54";
const CACHE = "hercules-v" + V;
const STATIC_ASSETS = [
  "/static/styles.css?v=" + V,
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

  // Nada de outro domínio passa por aqui. O conector da Pluggy (877 KB, vindo do
  // cdn.pluggy.ai) atravessava este arquivo sem nenhum motivo — e quando a rede
  // soluçava no meio, o `.catch` abaixo devolvia undefined, que o navegador lê
  // como erro de rede. O conector morria de vez naquele carregamento, sendo que
  // o navegador sozinho teria tentado de novo.
  if (url.origin !== self.location.origin) return;

  // Estáticos: cache primeiro, rede como reserva
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return resp;
      }).catch(() => new Response("", { status: 504 })))
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
      // caches.match devolve undefined quando nada foi guardado, e
      // respondWith(undefined) e' erro de rede. Melhor devolver uma resposta de
      // verdade dizendo que esta offline do que fingir que a rede caiu.
      .catch(() => caches.match(request).then((cache) => cache || new Response(
        "Sem conexão agora. Abra de novo quando a internet voltar.",
        { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }
      )))
  );
});
