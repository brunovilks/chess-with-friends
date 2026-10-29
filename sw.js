// Gambit service worker: lietotnes ikonai un ātrākai ielādei.
// Lapa un API vienmēr nāk no tīkla (spēle ir tiešsaistē); figūras, skaņas un ikonas tiek kešotas.
const CACHE = 'gambit-static-v1';
const STATIC = /^\/(p\/|s\/|icon-)/;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())
));

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (STATIC.test(url.pathname)) {
    e.respondWith(caches.open(CACHE).then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
    return;
  }
  if (e.request.mode === 'navigate') {
    // bez interneta parāda vienkāršu paziņojumu, nevis pārlūka kļūdu
    e.respondWith(fetch(e.request).catch(() => new Response(
      '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<body style="background:#302e2b;color:#fff;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0;text-align:center">' +
      '<div><div style="font-size:56px">♞</div><h2>No internet connection</h2><p>Gambit needs internet to play with friends.</p>' +
      '<button onclick="location.reload()" style="padding:12px 22px;border:0;border-radius:10px;background:#81b64c;color:#fff;font-weight:700">Try again</button></div>',
      {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
  }
});
