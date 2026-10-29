/* Valley Lotto store TV: a saved copy of the page, for when the internet isn't up.
 *
 * After a power cut the Fire TV stick usually starts before the store's modem,
 * and AbleSign opens the page straight away. Without this the TV would show the
 * browser's "no internet" error page. With it, once the page has been opened
 * once with the internet working, it opens from this saved copy and shows the
 * last numbers it saved (app.js keeps those; nothing here touches them).
 *
 *   - The page, its script, styles, games.json, background, fonts and logos are
 *     saved. Each is fetched fresh first whenever the internet answers (so a
 *     change on Netlify is used at once) and the saved copy is used only when
 *     it doesn't, or takes longer than WAIT_MS.
 *   - /api/numbers, the hourly "is the site up?" check (?alive=), the demo
 *     numbers and anything with ?... in its address are never saved or
 *     answered from here: they always go to the internet.
 *
 * Plain ES2017, like app.js, for old Android / Fire TV WebViews.
 */
'use strict';

var CACHE = 'vl-tv-page-v1';
var WAIT_MS = 8000;          // a network that hangs (the modem half up): use the copy

// Paths are relative to this file (the site's root).
var PAGE = './';
var FILES = [
  PAGE, 'app.js', 'styles.css', 'games.json', 'rays.svg',
  'fonts/montserrat-latin-600-normal.woff2',
  'fonts/montserrat-latin-800-normal.woff2',
  'fonts/montserrat-latin-900-normal.woff2',
  'fonts/montserrat-latin-900-italic.woff2',
  'fonts/anton-latin-400-normal.woff2'
];

function full(path) { return new URL(path, self.location.href).href; }

// The saved copy of a page that was redirected can't answer for the page
// itself in some browsers, so it's saved as a plain copy.
function plain(res) {
  if (!res.redirected) return Promise.resolve(res);
  return res.blob().then(function (body) {
    return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
  });
}

function save(key, res) {
  return plain(res).then(function (copy) {
    return caches.open(CACHE).then(function (cache) { return cache.put(key, copy); });
  }).catch(function () { /* storage full or blocked: the live site still works */ });
}

function saved(key) {
  return caches.open(CACHE).then(function (cache) {
    return cache.match(key, { ignoreVary: true });
  }).catch(function () { return undefined; });
}

function fetchAndSave(path) {
  return fetch(full(path), { cache: 'no-cache' }).then(function (res) {
    if (res.ok) return save(full(path), res.clone()).then(function () { return res; });
    return res;
  });
}

self.addEventListener('install', function (event) {
  event.waitUntil(
    Promise.all(FILES.map(function (f) { return fetchAndSave(f).catch(function () {}); }))
      .then(function () {
        // and every logo games.json names
        return saved(full('games.json')).then(function (res) {
          return res ? res.json() : { games: {} };
        }).then(function (cfg) {
          var logos = {};
          Object.keys((cfg && cfg.games) || {}).forEach(function (id) {
            var logo = cfg.games[id] && cfg.games[id].logo;
            if (typeof logo === 'string' && logo && logo.indexOf('?') < 0) logos[logo] = true;
          });
          return Promise.all(Object.keys(logos).map(function (l) {
            return fetchAndSave(l).catch(function () {});
          }));
        }).catch(function () {});
      })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (n) {
        return n.indexOf('vl-tv-page-') === 0 && n !== CACHE;
      }).map(function (n) { return caches.delete(n); }));
    }).catch(function () {}).then(function () { return self.clients.claim(); })
  );
});

// Fresh from the internet first; the saved copy when it can't be had.
function freshOrSaved(request, key) {
  return new Promise(function (resolve, reject) {
    var done = false;
    function finish(res) { if (!done) { done = true; resolve(res); } }
    function fail(err) { if (!done) { done = true; reject(err); } }
    var timer = setTimeout(function () {
      saved(key).then(function (hit) { if (hit) finish(hit); });
    }, WAIT_MS);
    fetch(request).then(function (res) {
      clearTimeout(timer);
      if (res.ok) {
        save(key, res.clone());
        finish(res);
      } else if (res.status >= 500) {
        // Netlify or the connection in trouble: the saved copy is better
        saved(key).then(function (hit) { finish(hit || res); });
      } else {
        finish(res);          // a redirect (a new address) or "not found" is the real answer
      }
    }, function (err) {
      clearTimeout(timer);
      saved(key).then(function (hit) { if (hit) finish(hit); else fail(err); });
    });
  });
}

function ours(path) {
  return FILES.indexOf(path) >= 0 || path.indexOf('fonts/') === 0 || path.indexOf('logos/') === 0;
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;
  var url = new URL(request.url);
  var root = new URL(PAGE, self.location.href);
  if (url.origin !== root.origin || url.pathname.indexOf(root.pathname) !== 0) return;
  if (request.mode === 'navigate') {
    // The page itself (with or without ?demo=1 and the like): one saved copy.
    event.respondWith(freshOrSaved(request, full(PAGE)));
    return;
  }
  if (url.search) return;     // /api/numbers?_=..., index.html?alive=...: always live
  var path = url.pathname.slice(root.pathname.length);
  if (!ours(path)) return;    // /api/numbers, sample-numbers.json, sw.js: always live
  event.respondWith(freshOrSaved(request, full(path)));
});
