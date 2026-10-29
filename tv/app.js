/* Valley Lotto store TV: PA winning numbers, retail point-of-sale style.
 *
 * Plain ES2017 for old Android / Fire TV WebViews: no frameworks, no
 * modules, no optional chaining, no object spread. Animations are CSS
 * (transform + opacity only); this file builds the slides, cycles them,
 * keeps the scrolling ticker filled and keeps the data fresh.
 *
 *   data:      GET /api/numbers every 2 minutes (?api=<url> overrides),
 *              last good copy kept in localStorage and shown at once on load.
 *   ?demo=1    preview with the bundled sample-numbers.json (never default).
 *   ?now=...   (demo only) pretend it is this ISO time, for TONIGHT checks.
 *   reload:    the whole page reloads once an hour (backstop in index.html),
 *              only when the site answers, so an outage keeps the numbers up.
 *   offline:   sw.js keeps a copy of the page itself, so a TV that starts
 *              with no internet still opens it and shows the saved numbers.
 *   logos:     PA's official game logos (logos/, named in games.json) on white
 *              plates, sized to the same visual area whatever their shape.
 */
(function () {
  'use strict';

  var STAGE_W = 1920, STAGE_H = 1080;
  var POLL_MS = 2 * 60 * 1000;
  var RETRY_MS = 30 * 1000;
  var RELOAD_MS = 60 * 60 * 1000;
  var FETCH_TIMEOUT_MS = 20 * 1000;
  var TZ = 'America/New_York';

  var params = parseQuery(window.location.search);
  var DEMO = params.demo === '1';
  var DATA_URL = DEMO ? 'sample-numbers.json' : (params.api || '/api/numbers');
  var CACHE_KEY = DEMO ? 'vl-tv-demo-numbers-v1' : 'vl-tv-numbers-v1';

  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var STOCK_COLORS = ['#234090', '#ab1e2c', '#00796b', '#5b2a86', '#d84315', '#0b6fa4', '#c2185b', '#2e7d32'];

  var stage = document.getElementById('stage');
  var slidesEl = document.getElementById('slides');
  var dividerEl = document.getElementById('divider');
  var flashEl = document.getElementById('divider-flash');
  var trackEl = document.getElementById('track');
  var updatedEl = document.getElementById('updated');
  var emptyEl = document.getElementById('empty');

  var config = { settings: {}, extras: {}, games: {} };
  var state = {
    data: null,          // last good feed
    slides: [],          // slide plans built from data
    index: -1,
    needsRebuild: false,
    tickerSig: '',
    current: null        // current slide element
  };
  var timers = { slide: 0, poll: 0, reload: 0, slideJobs: [] };

  /* ------------------------------------------------------------ helpers */

  function parseQuery(q) {
    var out = {};
    (q || '').replace(/^\?/, '').split('&').forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf('=');
      var k = decodeURIComponent(i < 0 ? kv : kv.slice(0, i));
      out[k] = i < 0 ? '' : decodeURIComponent(kv.slice(i + 1).replace(/\+/g, ' '));
    });
    return out;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function getJSON(url, cb) {
    var done = false;
    var xhr = new XMLHttpRequest();
    function finish(err, data) { if (!done) { done = true; cb(err, data); } }
    try {
      xhr.open('GET', url, true);
      xhr.timeout = FETCH_TIMEOUT_MS;
      xhr.setRequestHeader('Accept', 'application/json');
    } catch (e) { finish(e); return; }
    xhr.onload = function () {
      // status 0 is what file:// gives for a local preview
      if ((xhr.status >= 200 && xhr.status < 300) || (xhr.status === 0 && xhr.responseText)) {
        try { finish(null, JSON.parse(xhr.responseText)); } catch (e) { finish(e); }
      } else { finish(new Error('HTTP ' + xhr.status)); }
    };
    xhr.onerror = function () { finish(new Error('network')); };
    xhr.ontimeout = function () { finish(new Error('timeout')); };
    try { xhr.send(); } catch (e) { finish(e); }
  }

  function store(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage full or blocked */ }
  }
  function load(key) {
    try { var s = window.localStorage.getItem(key); return s ? JSON.parse(s) : null; } catch (e) { return null; }
  }

  function hexToRgb(hex) {
    var h = String(hex || '').replace('#', '');
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    var n = parseInt(h, 16);
    if (isNaN(n) || h.length !== 6) return [35, 64, 144];
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function mix(hex, withHex, amt) {
    var a = hexToRgb(hex), b = hexToRgb(withHex);
    var r = a.map(function (v, i) { return Math.round(v + (b[i] - v) * amt); });
    return '#' + r.map(function (v) { var s = v.toString(16); return s.length < 2 ? '0' + s : s; }).join('');
  }

  /* ------------------------------------------------------- time (New York) */

  function now() {
    if (DEMO) {
      if (params.now) { var d = new Date(params.now); if (!isNaN(d)) return d; }
      if (state.data && state.data.generated_at) { var g = new Date(state.data.generated_at); if (!isNaN(g)) return g; }
    }
    return new Date();
  }

  // Wall-clock parts of a moment in America/New_York.
  function nyParts(date) {
    try {
      var f = new Intl.DateTimeFormat('en-US', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
      var m = /(\d+)\/(\d+)\/(\d+),?\s+(\d+):(\d+)/.exec(f.format(date));
      if (m) return { y: +m[3], mo: +m[1], d: +m[2], h: (+m[4]) % 24, mi: +m[5] };
    } catch (e) { /* old WebView without time zone data: fall through */ }
    // Fallback: US Eastern rules (DST from 2nd Sunday of March to 1st Sunday of November).
    var y = date.getUTCFullYear();
    var mar = new Date(Date.UTC(y, 2, 1)); var dstStart = Date.UTC(y, 2, 1 + ((7 - mar.getUTCDay()) % 7) + 7, 7);
    var nov = new Date(Date.UTC(y, 10, 1)); var dstEnd = Date.UTC(y, 10, 1 + ((7 - nov.getUTCDay()) % 7), 6);
    var t = date.getTime();
    var off = (t >= dstStart && t < dstEnd) ? -4 : -5;
    var l = new Date(t + off * 3600000);
    return { y: l.getUTCFullYear(), mo: l.getUTCMonth() + 1, d: l.getUTCDate(), h: l.getUTCHours(), mi: l.getUTCMinutes() };
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function isoDay(p) { return p.y + '-' + pad2(p.mo) + '-' + pad2(p.d); }
  function todayNY() { return isoDay(nyParts(now())); }
  function addDays(iso, n) {
    var p = iso.split('-');
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + n));
    return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
  }
  function dayInfo(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    if (!m) return null;
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return { y: +m[1], month: MONTHS[+m[2] - 1], mon: MONTHS[+m[2] - 1].slice(0, 3), d: +m[3], weekday: DAYS[d.getUTCDay()], wd: DAYS[d.getUTCDay()].slice(0, 3) };
  }
  function clock12(p) {
    var h = p.h % 12; if (h === 0) h = 12;
    return h + ':' + pad2(p.mi) + (p.h < 12 ? ' AM' : ' PM');
  }

  /* --------------------------------------------------------- game look */

  function baseTitle(title) {
    return String(title || '').replace(/\s*\((day|evening|midday|night)\)\s*$/i, '').trim();
  }
  function drawOf(title) {
    var m = /\((day|evening|midday|night)\)\s*$/i.exec(String(title || ''));
    return m ? m[1].charAt(0).toUpperCase() + m[1].slice(1).toLowerCase() : '';
  }
  function hashIndex(s, n) {
    var h = 0; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h) % n;
  }

  // A plain wordmark for a game with no games.json entry: long names go on
  // two balanced lines.
  function autoWordmark(title) {
    var t = String(title || '').toUpperCase();
    var words = t.split(/\s+/);
    if (t.length <= 14 || words.length < 2) return [{ t: t, s: 'lg' }];
    var best = 1, bestDiff = 1e9;
    for (var i = 1; i < words.length; i++) {
      var d = Math.abs(words.slice(0, i).join(' ').length - words.slice(i).join(' ').length);
      if (d < bestDiff) { bestDiff = d; best = i; }
    }
    return [{ t: words.slice(0, best).join(' '), s: 'md' }, { t: words.slice(best).join(' '), s: 'md' }];
  }

  function lookOf(game) {
    var c = config.games[String(game.id)] || {};
    var name = c.name || baseTitle(game.title);
    var draw = c.draw || drawOf(game.title);
    var group = c.group || (draw ? 'auto-' + baseTitle(game.title).toLowerCase() : '');
    var color = c.color || STOCK_COLORS[hashIndex(baseTitle(game.title), STOCK_COLORS.length)];
    return {
      id: String(game.id),
      name: name,
      tickerName: c.tickerName || name.toUpperCase(),
      draw: draw,
      group: group,
      color: color,
      logo: c.logo || '',
      logoSize: (c.logoSize && c.logoSize.length === 2 && c.logoSize[0] > 0 && c.logoSize[1] > 0) ? c.logoSize : null,
      logoScale: typeof c.logoScale === 'number' ? c.logoScale : 1,
      logoLabel: c.logoLabel || '',
      wordmark: c.wordmark || autoWordmark(name),
      price: c.price || null,
      rank: typeof c.rank === 'number' ? c.rank : 100 + (game.position || 0),
      hero: !!c.hero,
      ticker: c.ticker != null ? !!c.ticker : !!draw,
      jackpotLabel: c.jackpotLabel || 'Jackpot',
      cashLabel: c.cashLabel || 'Cash Value'
    };
  }

  function setColorVars(node, color) {
    node.style.setProperty('--c', color);
    node.style.setProperty('--cd', mix(color, '#000000', 0.45));
  }
  function setBonusVars(node, color, ink) {
    node.style.setProperty('--b', color);
    node.style.setProperty('--bl', mix(color, '#ffffff', 0.45));
    node.style.setProperty('--bd', mix(color, '#000000', 0.32));
    node.style.setProperty('--bi', ink || '#ffffff');
  }

  /* ------------------------------------------------------- data shaping */

  // The feed with any unreadable game left out (one odd entry must not keep
  // every other game off the screen), or null when nothing in it is usable.
  function cleanData(d) {
    if (!d || typeof d !== 'object' || !Array.isArray(d.games)) return null;
    var games = [];
    d.games.forEach(function (g) {
      if (!g || typeof g !== 'object' || g.id == null || !Array.isArray(g.numbers) || !g.numbers.length) return;
      if (g.extras != null && !Array.isArray(g.extras)) {
        var copy = {};
        Object.keys(g).forEach(function (k) { copy[k] = g[k]; });
        copy.extras = [];
        g = copy;
      }
      games.push(g);
    });
    if (!games.length) return null;
    if (games.length === d.games.length && games.every(function (g, i) { return g === d.games[i]; })) return d;
    var out = {};
    Object.keys(d).forEach(function (k) { out[k] = d[k]; });
    out.games = games;
    return out;
  }

  // Numbers are shown exactly as PA publishes them ("01" stays "01").
  function ballText(v) {
    return String(v == null ? '' : v).trim();
  }

  // A tag's value. A multiplier is printed the way it reads on the ticket:
  // PA's feed sends Power Play 3 as "03", which is shown as "3X". Anything that
  // isn't plain digits is shown exactly as PA sent it.
  function tagText(v, c) {
    var t = ballText(v);
    if (c.multiplier && /^[0-9]+$/.test(t)) return String(parseInt(t, 10)) + 'X';
    return t + (c.suffix || '');
  }

  // Bonus balls and tags (Power Play) from a draw's extras.
  function splitExtras(game, look) {
    var balls = [], tags = [];
    (game.extras || []).forEach(function (x) {
      if (!x || x.value == null || String(x.value).trim() === '') return;
      var c = config.extras[x.code] || {};
      if (c.kind === 'tag') {
        tags.push({ label: c.label || x.label || x.code, value: tagText(x.value, c) });
      } else {
        balls.push({ label: c.label || x.label || x.code, value: ballText(x.value), color: c.color || look.color, ink: c.ink || '#ffffff' });
      }
    });
    return { balls: balls, tags: tags };
  }

  function commas(n) { return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function trimNum(x) { return String(Math.round(x * 100) / 100); }

  // A money value as {num: "$716", unit: "Million", rest: ""}; never invents
  // a figure: the amount is only shortened when it is an exact multiple.
  function money(m) {
    if (!m) return null;
    var a = m.amount;
    if (typeof a === 'number' && isFinite(a) && a > 0) {
      if (a >= 1e9 && a % 1e7 === 0) return { num: '$' + trimNum(a / 1e9), unit: 'Billion', rest: '' };
      if (a >= 1e6 && a % 1e4 === 0) return { num: '$' + trimNum(a / 1e6), unit: 'Million', rest: '' };
      return { num: '$' + commas(a), unit: '', rest: '' };
    }
    var t = String(m.text || '').trim();
    if (!t) return null;
    var r = /^(\$[\d,.]+)\s*(million|billion)?\s*(.*)$/i.exec(t);
    if (r) return { num: r[1], unit: r[2] ? r[2].charAt(0).toUpperCase() + r[2].slice(1).toLowerCase() : '', rest: r[3] || '' };
    return { num: t, unit: '', rest: '' };
  }
  function moneyText(mo) { return mo ? mo.num + (mo.unit ? ' ' + mo.unit : '') + (mo.rest ? ' ' + mo.rest : '') : ''; }

  // Units: one per game, except Day/Evening draws of a game share one.
  function buildUnits(data) {
    var units = [], groups = {};
    data.games.forEach(function (g) {
      var look = lookOf(g);
      if (look.group) {
        var u = groups[look.group];
        if (!u) {
          u = groups[look.group] = { kind: 'pick', key: look.group, look: look, draws: [], rank: look.rank, pos: g.position || 0 };
          units.push(u);
        }
        u.draws.push({ game: g, look: look });
      } else {
        units.push({ kind: 'jackpot', key: look.id, look: look, game: g, rank: look.rank, pos: g.position || 0 });
      }
    });
    var order = { Day: 0, Midday: 0, Evening: 1, Night: 1 };
    units.forEach(function (u) {
      if (u.draws) u.draws.sort(function (a, b) { return (order[a.look.draw] || 0) - (order[b.look.draw] || 0); });
    });
    units.sort(function (a, b) { return (a.rank - b.rank) || (a.pos - b.pos); });
    return units;
  }

  function pairUp(list) {
    var out = [];
    for (var i = 0; i < list.length; i += 2) out.push(list.slice(i, i + 2));
    return out;
  }

  // Slides: hero pair (Mega Millions + Powerball) comes back every few slides,
  // like-shaped halves are paired so their bands line up.
  function buildSlides(data) {
    var units = buildUnits(data);
    var heroes = units.filter(function (u) { return u.look.hero; });
    var jack = units.filter(function (u) { return !u.look.hero && u.kind === 'jackpot'; });
    var picks = units.filter(function (u) { return u.kind === 'pick'; });
    var heroSlides = pairUp(heroes);
    var jackSlides = pairUp(jack);
    var pickSlides = pairUp(picks);
    var lastJ = jackSlides.length && jackSlides[jackSlides.length - 1].length === 1 ? jackSlides.pop() : null;
    var lastP = pickSlides.length && pickSlides[pickSlides.length - 1].length === 1 ? pickSlides.pop() : null;
    var rest = jackSlides.concat(pickSlides);
    if (lastJ && lastP) rest.push(lastJ.concat(lastP));
    else if (lastJ) rest.push(lastJ);
    else if (lastP) rest.push(lastP);

    var every = config.settings.heroEvery || 3;
    var out = [];
    if (!heroSlides.length) out = rest.map(function (s) { return { units: s, hero: false }; });
    else {
      var hi = 0;
      out.push({ units: heroSlides[0], hero: true });
      hi = 1;
      rest.forEach(function (s, i) {
        out.push({ units: s, hero: false });
        if ((i + 1) % every === 0 && i + 1 < rest.length) {
          out.push({ units: heroSlides[hi % heroSlides.length], hero: true });
          hi++;
        }
      });
      // any further hero pairs that did not get a turn
      for (; hi < heroSlides.length; hi++) out.push({ units: heroSlides[hi], hero: true });
    }
    return out;
  }

  /* ------------------------------------------------------------ fitting */

  // Shrink an element's font until its content fits the given width.
  function fitWidth(node, maxW, minPx) {
    if (!node) return;
    var cs = window.getComputedStyle(node);
    var size = parseFloat(cs.fontSize);
    var w = node.scrollWidth;
    var guard = 0;
    while (w > maxW && size > (minPx || 12) && guard < 12) {
      size = Math.max(minPx || 12, Math.floor(size * maxW / w * 0.99));
      node.style.fontSize = size + 'px';
      w = node.scrollWidth;
      guard++;
    }
  }

  /* ------------------------------------------------------------ builders */

  function buildWordmark(look) {
    var wm = el('div', 'wm');
    look.wordmark.forEach(function (line) {
      var ln = el('div', 'wm-line wm-' + (line.s || 'lg'));
      var t = String(line.t || ''), re = /\[([^\]]+)\]/g, last = 0, m;
      while ((m = re.exec(t))) {
        if (m.index > last) ln.appendChild(document.createTextNode(t.slice(last, m.index)));
        var b = el('span', 'wm-ball'); b.appendChild(el('span', null, m[1])); ln.appendChild(b);
        last = re.lastIndex;
      }
      if (last < t.length) ln.appendChild(document.createTextNode(t.slice(last)));
      wm.appendChild(ln);
    });
    return wm;
  }

  /* PA's official logo on a white plate, the way palottery.pa.gov shows it:
     every logo keeps its true colours (dark lettering included) on any
     colour block. Logos have very different shapes, so each is sized to the
     same visual area rather than the same width or height. */
  function logoBox(look, area, maxW, maxH) {
    var sz = look.logoSize || [2, 1];
    var r = sz[0] / sz[1];
    var w = Math.sqrt(area * r) * look.logoScale, h = w / r;
    if (w > maxW) { w = maxW; h = w / r; }
    if (h > maxH) { h = maxH; w = h * r; }
    return { w: Math.round(w), h: Math.round(h) };
  }

  function buildPlate(look, cls) {
    var plate = el('span', 'plate ' + cls);
    var img = el('img');
    img.alt = look.name;
    img.setAttribute('draggable', 'false');
    img.src = look.logo;
    plate.appendChild(img);
    if (look.logoLabel) plate.appendChild(el('span', 'plate-label', look.logoLabel));
    plate._img = img;
    return plate;
  }

  function sizePlate(plate, look, area, maxW, maxH) {
    var img = plate._img;
    function apply(sizeLook) {
      var b = logoBox(sizeLook, area, maxW, maxH);
      img.style.width = b.w + 'px';
      img.style.height = b.h + 'px';
    }
    if (look.logoSize) { apply(look); return; }
    // a logo added to games.json without its size: size it once it loads
    img.style.maxWidth = maxW + 'px'; img.style.maxHeight = maxH + 'px';
    function natural() {
      img.onload = null;
      if (img.naturalWidth && img.naturalHeight) {
        img.style.maxWidth = ''; img.style.maxHeight = '';
        apply({ logoSize: [img.naturalWidth, img.naturalHeight], logoScale: look.logoScale });
      }
    }
    if (img.complete && img.naturalWidth) natural();
    else img.onload = natural;
  }

  function buildHead(look) {
    var head = el('div', 'h-head');
    var logo = el('div', 'h-logo');
    if (look.logo) {
      logo.appendChild(buildPlate(look, 'plate--panel' + (look.logoLabel ? ' has-label' : '')));
    } else {
      logo.appendChild(buildWordmark(look));
    }
    head.appendChild(logo);
    if (look.price && look.price.big) {
      var pr = el('div', 'h-price');
      var big = el('span', 'p-big');
      String(look.price.big).split(/\s+(or|to|\/)\s+/i).forEach(function (part, i) {
        if (i % 2) big.appendChild(el('small', null, part));
        else big.appendChild(document.createTextNode(part));
      });
      pr.appendChild(big);
      if (look.price.small) pr.appendChild(el('span', 'p-small', look.price.small));
      if (look.price.note) pr.appendChild(el('span', 'p-note', look.price.note));
      pr.appendChild(sparkle('c', 2400));
      head.appendChild(pr);
    }
    return head;
  }

  function buildBalls(numbers, bonus, maxSize, avail) {
    var row = el('div', 'balls');
    var n = numbers.length + bonus.length;
    var gap = n > 6 ? 12 : 18, bonusGap = bonus.length ? 26 : 0;
    var size = Math.floor((avail - gap * (n - 1) - bonusGap) / n);
    if (size > maxSize) size = maxSize;
    var fs = Math.round(size * 0.5);
    function one(text, cls, i) {
      var w = el('div', 'ball-wrap');
      w.style.marginLeft = (i === 0 ? 0 : gap) + 'px';
      var b = el('div', 'ball ' + cls);
      b.style.width = size + 'px'; b.style.height = size + 'px';
      b.style.fontSize = (String(text).length > 2 ? Math.round(fs * 0.78) : fs) + 'px';
      var s = el('span', null, text);
      s.style.lineHeight = '1'; s.style.marginTop = '-0.5em';
      b.appendChild(s);
      w.appendChild(b);
      return w;
    }
    var i = 0;
    numbers.forEach(function (v) { row.appendChild(one(ballText(v), '', i++)); });
    bonus.forEach(function (bb, k) {
      var w = one(bb.value, 'ball--bonus', i++);
      if (k === 0) w.style.marginLeft = (gap + bonusGap) + 'px';
      setBonusVars(w.firstChild, bb.color, bb.ink);
      var lab = el('div', 'ball-label');
      lab.appendChild(el('span', null, bb.label));
      w.appendChild(lab);
      row.appendChild(w);
    });
    row._count = n;
    row._size = size;
    return row;
  }

  // A starburst stretched along an ellipse: spikes evenly spaced along the
  // outline and all the same depth, so the long ends do not turn to needles.
  function burstPoints(cx, cy, rx, ry, spikes, depth) {
    var N = 720, t, i, acc = [0], pts = [];
    var prev = [rx, 0];
    for (i = 1; i <= N; i++) {
      t = 2 * Math.PI * i / N;
      var p = [rx * Math.cos(t), ry * Math.sin(t)];
      acc.push(acc[i - 1] + Math.sqrt(Math.pow(p[0] - prev[0], 2) + Math.pow(p[1] - prev[1], 2)));
      prev = p;
    }
    var total = acc[N], j = 0;
    for (var k = 0; k < spikes * 2; k++) {
      var want = total * k / (spikes * 2);
      while (j < N && acc[j + 1] < want) j++;
      t = 2 * Math.PI * j / N;
      var x = rx * Math.cos(t), y = ry * Math.sin(t);
      if (k % 2) {
        var nx = Math.cos(t) / rx, ny = Math.sin(t) / ry, nl = Math.sqrt(nx * nx + ny * ny);
        x -= nx / nl * depth; y -= ny / nl * depth;
      }
      pts.push((cx + x).toFixed(1) + ',' + (cy + y).toFixed(1));
    }
    return pts.join(' ');
  }

  function buildTonight() {
    var wrap = el('div', 'tonight');
    var inner = el('div', 'tonight-in');
    var pulse = el('div', 'tonight-pulse');
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 500 136');
    var shadow = document.createElementNS(NS, 'polygon');
    shadow.setAttribute('points', burstPoints(256, 74, 240, 64, 32, 16));
    shadow.setAttribute('fill', 'rgba(0,0,0,0.30)');
    var body = document.createElementNS(NS, 'polygon');
    body.setAttribute('points', burstPoints(250, 68, 240, 64, 32, 16));
    body.setAttribute('fill', '#ffcd02');
    body.setAttribute('stroke', '#ffffff');
    body.setAttribute('stroke-width', '5');
    body.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(shadow); svg.appendChild(body);
    pulse.appendChild(svg);
    pulse.appendChild(el('div', 'tonight-word', 'Tonight!'));
    inner.appendChild(pulse);
    wrap.appendChild(inner);
    return wrap;
  }

  function buildNext(game) {
    var iso = game.next_draw_date;
    var info = dayInfo(iso);
    if (!info) return null;
    var next = el('div', 'next');
    next.appendChild(el('div', 'next-label', 'Next Drawing:'));
    var today = todayNY();
    if (iso === today) {
      next.className = 'next is-tonight';
      next.appendChild(buildTonight());
      next._tonight = true;
    } else if (iso < today) {
      next.appendChild(el('div', 'next-val', 'Results coming soon'));
    } else {
      var v = el('div', 'next-val');
      if (iso === addDays(today, 1)) {
        v.appendChild(document.createTextNode('Tomorrow'));
        v.appendChild(el('small', null, info.wd + ', ' + info.mon + ' ' + info.d));
      } else {
        v.appendChild(document.createTextNode(info.weekday + ', ' + info.month + ' ' + info.d));
      }
      next.appendChild(v);
    }
    return next;
  }

  // Jackpot digits become odometer columns that roll into place.
  function buildOdometer(target, text) {
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (/\d/.test(ch)) {
        var col = el('span', 'odo');
        col.appendChild(el('span', 'odo-ghost', ch));
        var strip = el('span', 'odo-strip');
        strip.appendChild(el('i', null, '\u00a0'));   // starts blank, then rolls
        for (var k = 0; k < 20; k++) strip.appendChild(el('i', null, String(k % 10)));
        strip.appendChild(el('i', null, ch));
        strip._final = 21;
        col.appendChild(strip);
        target.appendChild(col);
      } else {
        target.appendChild(document.createTextNode(ch));
      }
    }
  }

  function buildJackpot(game, look) {
    var main = money(game.jackpot), cash = money(game.cash_value), label = look.jackpotLabel;
    if (!main && cash) { main = cash; cash = null; label = look.cashLabel; }
    if (!main) return null;
    var wrap = el('div', 'jp-wrap');
    var box = el('div', 'jp');
    wrap.appendChild(el('div', 'jp-tab', label));
    var m = el('div', 'jp-main');
    var amount = el('span', 'jp-amt');
    buildOdometer(amount, main.num);
    m.appendChild(amount);
    if (main.unit) m.appendChild(el('span', 'unit', main.unit.toUpperCase()));
    box.appendChild(m);
    var subText = main.rest ? main.rest.toUpperCase() : '';
    if (subText || cash) {
      var sub = el('div', 'jp-sub');
      if (subText) sub.appendChild(document.createTextNode(subText));
      if (cash) {
        if (subText) sub.appendChild(document.createTextNode('  ·  '));
        sub.appendChild(document.createTextNode('Cash Value '));
        sub.appendChild(el('b', null, moneyText(cash).toUpperCase()));
      }
      box.appendChild(sub);
      box.className += ' has-sub';
    }
    var clip = el('div', 'jp-clip');
    clip.appendChild(el('div', 'jp-glint'));
    box.appendChild(clip);
    box.appendChild(sparkle('a', 1700));
    box.appendChild(sparkle('b', 3100));
    wrap.appendChild(box);
    return wrap;
  }

  function bandFor(game, look, opts) {
    var band = el('div', 'band');
    var date = el('div', 'band-date');
    var info = dayInfo(game.draw_date);
    if (opts.drawTag) date.appendChild(el('span', 'draw-tag', look.draw ? look.draw.toUpperCase() : ''));
    if (info) {
      date.appendChild(document.createTextNode((opts.short ? info.weekday + ', ' + info.month + ' ' + info.d : info.weekday + ', ' + info.month + ' ' + info.d + ', ' + info.y).toUpperCase()));
      if (!opts.drawTag) date.appendChild(el('b', null, 'DRAWING'));
    }
    band.appendChild(date);
    var ex = splitExtras(game, look);
    var balls = buildBalls(game.numbers, ex.balls, opts.maxBall, opts.avail);
    band.appendChild(balls);
    ex.tags.forEach(function (t) {
      var tag = el('div', 'pp-tag', t.label);
      tag.appendChild(el('b', null, t.value));
      band.appendChild(tag);
    });
    if (opts.bandHeight) {
      // centre the row in the band, always leaving room for a bonus label
      // so balls of the same size line up across the two halves
      var free = opts.bandHeight - opts.dateHeight - balls._size - 34;
      balls.style.paddingTop = Math.max(0, Math.floor(free / 2)) + 'px';
    }
    band._balls = balls;
    band._date = date;
    return band;
  }

  function buildHalf(unit, side) {
    var look = unit.look;
    var half = el('section', 'half half--' + side + ' kind-' + unit.kind);
    setColorVars(half, look.color);
    var bg = el('div', 'half-bg');
    bg.appendChild(el('div', 'half-rays'));
    half.appendChild(bg);
    var streak = el('div', 'streak');
    streak.style.animationDelay = streak.style.webkitAnimationDelay = (side === 'right' ? '-4.4s' : side === 'solo' ? '-1s' : '-0.2s');
    half.appendChild(streak);

    var inner = el('div', 'half-in');
    inner.appendChild(buildHead(look));
    inner.appendChild(el('h2', 'h-title', 'Winning Numbers'));

    var bandWidth = side === 'solo' ? 1920 : 952;
    var avail = Math.min(bandWidth - 60, 1300);
    var parts = { bands: [] };
    if (unit.kind === 'pick') {
      unit.draws.forEach(function (d) {
        var b = bandFor(d.game, d.look, { drawTag: true, short: true, maxBall: 172, avail: avail, bandHeight: 296, dateHeight: 74 });
        inner.appendChild(b);
        parts.bands.push(b);
      });
    } else {
      var g = unit.game;
      var band = bandFor(g, look, { drawTag: false, short: false, maxBall: 130, avail: avail, bandHeight: 250, dateHeight: 60 });
      inner.appendChild(band);
      parts.bands.push(band);
      var next = buildNext(g);
      if (next) { inner.appendChild(next); parts.next = next; }
      var jp = buildJackpot(g, look);
      if (jp) { inner.appendChild(jp); parts.jp = jp; }
    }
    half.appendChild(inner);
    half.appendChild(el('div', 'timer'));
    half._parts = parts;
    half._side = side;
    half._look = look;
    return half;
  }

  // Bonus-ball labels, right to left: each is centred under its ball; one
  // wider than its ball runs leftwards under the row, but never past the
  // row's end or into the label to its right (two bonus balls side by side).
  function placeLabels(balls) {
    var wraps = balls.querySelectorAll('.ball-wrap');
    if (!wraps.length) return;
    var last = wraps[wraps.length - 1];
    var limit = last.offsetLeft + last.offsetWidth + 4;
    for (var i = wraps.length - 1; i >= 0; i--) {
      var lab = wraps[i].querySelector('.ball-label');
      if (!lab) continue;
      var tw = lab.firstChild.offsetWidth;
      var left = wraps[i].offsetLeft + (wraps[i].offsetWidth - tw) / 2;
      if (left + tw > limit) left = limit - tw;
      lab.style.left = Math.round(left - wraps[i].offsetLeft) + 'px';
      limit = left - 18;
    }
  }

  // Size text to fit once the slide is in the DOM (fonts are loaded by then).
  function fitHalf(half) {
    var side = half._side;
    var innerW = side === 'solo' ? 1920 - 240 : 952 - 60;
    var head = half.querySelector('.h-head');
    var price = half.querySelector('.h-price');
    var priceW = 0;
    if (price) {
      var big = price.querySelector('.p-big');
      fitWidth(big, 300, 40);
      priceW = Math.max(price.offsetWidth, 200);
    }
    var logoMax = side === 'solo' ? 900 : innerW - priceW - 40;
    var wm = half.querySelector('.wm');
    if (wm) {
      var lines = wm.querySelectorAll('.wm-line');
      for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        ln.style.display = 'inline-block';
        fitWidth(ln, logoMax, 24);
        ln.style.display = '';
      }
      // keep the whole wordmark inside the header height
      var h = wm.offsetHeight;
      if (h > 174) {
        var s = 174 / h;
        for (var j = 0; j < lines.length; j++) {
          lines[j].style.fontSize = Math.floor(parseFloat(window.getComputedStyle(lines[j]).fontSize) * s) + 'px';
        }
      }
    }
    var plate = half.querySelector('.plate--panel');
    if (plate) {
      var look = half._look;
      // plate padding: 22px each side, 11px top and bottom (30px under a
      // logo that carries a label)
      var maxH = look.logoLabel ? 118 : 170;
      sizePlate(plate, look, 60000, Math.min(logoMax, side === 'solo' ? 760 : 580) - 44, maxH);
    }

    var title = half.querySelector('.h-title');
    title.style.display = 'inline-block'; title.style.alignSelf = 'center'; title.style.webkitAlignSelf = 'center';
    fitWidth(title, innerW, 40);

    half._parts.bands.forEach(function (b) {
      placeLabels(b._balls);
      var d = b._date;
      d.style.display = 'inline-block';
      fitWidth(d, innerW + 20, 20);
      d.style.display = 'block';
    });
    if (half._parts.next) {
      var nv = half._parts.next.querySelector('.next-val');
      if (nv) { nv.style.display = 'inline-block'; fitWidth(nv, innerW, 30); }
    }
    if (half._parts.jp) {
      var main = half._parts.jp.querySelector('.jp-main');
      var box = half._parts.jp.querySelector('.jp');
      var sub = half._parts.jp.querySelector('.jp-sub');
      var hasSub = !!sub;
      if (hasSub) main.style.fontSize = '104px';
      main.style.display = 'inline-block'; main.style.left = 'auto'; main.style.right = 'auto';
      fitWidth(main, 760, 40);
      if (sub) { sub.style.display = 'inline-block'; sub.style.left = 'auto'; sub.style.right = 'auto'; fitWidth(sub, 760, 14); }
      // centre the lines inside the box
      var boxH = box.clientHeight;
      var mh = main.offsetHeight;
      var sh = sub ? sub.offsetHeight + 8 : 0;
      var top = Math.round((boxH - mh - sh) / 2);
      main.style.top = top + 'px';
      main.style.left = Math.round((box.clientWidth - main.offsetWidth) / 2) + 'px';
      if (sub) {
        sub.style.top = (top + mh + 8 - 4) + 'px'; sub.style.bottom = 'auto';
        sub.style.left = Math.round((box.clientWidth - sub.offsetWidth) / 2) + 'px';
      }
    }
  }

  /* ------------------------------------------------------- choreography */

  function delay(node, ms) {
    if (!node) return;
    node.style.animationDelay = ms + 'ms';
    node.style.webkitAnimationDelay = ms + 'ms';
  }

  function stage2(half, base) {
    delay(half.querySelector('.h-logo'), base + 380);
    delay(half.querySelector('.h-price'), base + 560);
    delay(half.querySelector('.h-title'), base + 300);
    var t = base + 520;
    half._parts.bands.forEach(function (b, bi) {
      delay(b._date, t + bi * 120);
      var wraps = b._balls.querySelectorAll('.ball-wrap');
      for (var i = 0; i < wraps.length; i++) delay(wraps[i], t + 120 + bi * 260 + i * 110);
      var labels = b._balls.querySelectorAll('.ball-label');
      for (var k = 0; k < labels.length; k++) delay(labels[k], t + 600 + bi * 260 + wraps.length * 110);
      var tag = b.querySelector('.pp-tag');
      if (tag) delay(tag, t + 700 + wraps.length * 110);
    });
    if (half._parts.next) {
      delay(half._parts.next, base + 1050);
      delay(half._parts.next.querySelector('.tonight-in'), base + 1250);
      delay(half._parts.next.querySelector('.tonight-pulse'), base + 1950);
    }
    if (half._parts.jp) {
      delay(half._parts.jp, base + 1350);
      delay(half._parts.jp.querySelector('.jp-glint'), base + 1600);
      delay(half._parts.jp.querySelector('.jp-sub'), base + 2600);
    }
  }

  function rollOdometers(slide) {
    var strips = slide.querySelectorAll('.odo-strip');
    var n = strips.length;
    for (var i = 0; i < n; i++) {
      var s = strips[i];
      var dur = 1400 + (n - i) * 90;
      s.style.transitionDuration = s.style.webkitTransitionDuration = dur + 'ms';
      var v = 'translateY(' + (-1.12 * s._final) + 'em)';
      s.style.transform = v; s.style.webkitTransform = v;
    }
  }

  function clearSlideJobs() {
    timers.slideJobs.forEach(function (t) { clearTimeout(t); });
    timers.slideJobs = [];
  }
  function job(fn, ms) { timers.slideJobs.push(setTimeout(fn, ms)); }

  function renderSlide(plan) {
    var slide = el('div', 'slide');
    var units = plan.units;
    if (units.length === 1) {
      slide.appendChild(buildHalf(units[0], 'solo'));
    } else {
      slide.appendChild(buildHalf(units[0], 'left'));
      slide.appendChild(buildHalf(units[1], 'right'));
    }
    return slide;
  }

  function holdSeconds(plan) {
    var s = config.settings;
    return plan.hero ? (s.heroSeconds || 16) : (s.slideSeconds || 12);
  }

  function showSlide(i) {
    clearTimeout(timers.slide);
    clearSlideJobs();
    if (!state.slides.length) return;
    state.index = ((i % state.slides.length) + state.slides.length) % state.slides.length;
    var plan = state.slides[state.index];
    var slide = renderSlide(plan);
    var halves = slide.querySelectorAll('.half');
    var holdMs = holdSeconds(plan) * 1000;

    slidesEl.appendChild(slide);
    for (var h = 0; h < halves.length; h++) {
      fitHalf(halves[h]);
      stage2(halves[h], h * 110);
      var tm = halves[h].querySelector('.timer');
      tm.style.animationDuration = tm.style.webkitAnimationDuration = (holdMs - 400) + 'ms';
      delay(tm, 400);
    }
    dividerEl.className = halves.length === 1 ? 'is-solo' : '';

    // retire the old slide (and anything else left behind)
    var old = state.current;
    state.current = slide;
    var kids = slidesEl.children;
    for (var k = 0; k < kids.length; k++) {
      if (kids[k] !== slide) kids[k].className = 'slide is-out';
    }
    void slide.offsetWidth; // commit styles before starting the entrance
    slide.className = 'slide is-in';
    job(function () {
      var leftovers = slidesEl.children;
      for (var j = leftovers.length - 1; j >= 0; j--) {
        if (leftovers[j] !== slide) slidesEl.removeChild(leftovers[j]);
      }
    }, 800);
    if (halves.length > 1) {
      job(function () { flashEl.className = ''; void flashEl.offsetWidth; flashEl.className = 'go'; }, 430);
    }
    job(function () { rollOdometers(slide); }, 1250);
    old = null;

    timers.slide = setTimeout(function () {
      if (state.needsRebuild) {
        var key = plan.units.map(function (u) { return u.key; }).join('|');
        rebuildSlides();
        // carry on from where we were in the cycle
        var at = -1;
        for (var s = 0; s < state.slides.length; s++) {
          if (state.slides[s].units.map(function (u) { return u.key; }).join('|') === key) { at = s; break; }
        }
        showSlide(at + 1);
      } else {
        showSlide(state.index + 1);
      }
    }, holdMs);
  }

  function rebuildSlides() {
    state.needsRebuild = false;
    state.slides = state.data ? buildSlides(state.data) : [];
  }

  /* ------------------------------------------------------------- ticker */

  function sparkle(kind, delayMs) {
    var NS = 'http://www.w3.org/2000/svg';
    var wrap = el('span', 'spark spark--' + kind);
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 40 40');
    var p = document.createElementNS(NS, 'path');
    p.setAttribute('d', 'M20 0 C21 13 27 19 40 20 C27 21 21 27 20 40 C19 27 13 21 0 20 C13 19 19 13 20 0 Z');
    p.setAttribute('fill', '#ffffff');
    svg.appendChild(p);
    wrap.appendChild(svg);
    wrap.style.animationDelay = wrap.style.webkitAnimationDelay = delayMs + 'ms';
    return wrap;
  }

  function star() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 40 40');
    var p = document.createElementNS(NS, 'path');
    p.setAttribute('d', 'M20 0 L25 15 L40 20 L25 25 L20 40 L15 25 L0 20 L15 15 Z');
    p.setAttribute('fill', '#ffcd02');
    svg.appendChild(p);
    var wrap = el('span', 'tk-sep');
    wrap.appendChild(svg);
    return wrap;
  }

  function tickerItems(data) {
    var items = [];
    var today = todayNY();
    data.games.forEach(function (g) {
      var look = lookOf(g);
      if (!look.ticker) return;
      var ex = splitExtras(g, look);
      var info = dayInfo(g.draw_date);
      items.push({
        type: 'draw', color: look.color, name: look.tickerName, look: look,
        sub: ((look.draw ? look.draw + ' · ' : '') + (info ? info.mon + ' ' + info.d : '')).toUpperCase(),
        t1: (look.draw || (info ? info.mon + ' ' + info.d : '')).toUpperCase(),
        t2: (look.draw ? (info ? info.mon + ' ' + info.d : '') : (info ? info.weekday : '')).toUpperCase(),
        numbers: g.numbers.map(ballText),
        bonus: ex.balls,
        jackpot: moneyText(money(g.jackpot))
      });
    });
    buildUnits(data).forEach(function (u) {
      if (u.kind !== 'jackpot' || u.look.ticker) return;
      var g = u.game;
      var main = money(g.jackpot), label = u.look.jackpotLabel.replace(/^estimated\s+/i, '');
      if (!main && g.cash_value) { main = money(g.cash_value); label = u.look.cashLabel; }
      if (!main) return;
      var when = '';
      var info = dayInfo(g.next_draw_date);
      if (info && g.next_draw_date === today) when = 'Drawing tonight';
      else if (info && g.next_draw_date === addDays(today, 1)) when = 'Next drawing tomorrow';
      else if (info && g.next_draw_date > today) when = 'Next drawing ' + info.wd + ', ' + info.mon + ' ' + info.d;
      items.push({
        type: 'jackpot', color: u.look.color, name: u.look.tickerName, look: u.look, label: label, value: moneyText(main), when: when,
        t1: (u.look.logoLabel || label).toUpperCase(), t2: u.look.logoLabel ? label.toUpperCase() : ''
      });
    });
    return items;
  }

  function buildTickerSet(items) {
    var set = el('div', 'tk-set');
    items.forEach(function (it) {
      var item = el('div', 'tk-item');
      setColorVars(item, it.color);
      var chip = el('div', 'tk-chip');
      if (it.look.logo) {
        // the game's official logo on a small plate, with its draw / prize on a colour tab
        chip.className = 'tk-chip has-logo';
        var plate = buildPlate({ name: it.look.name, logo: it.look.logo, logoLabel: '' }, 'plate--tk');
        sizePlate(plate, it.look, 8000, 196, 56);
        chip.appendChild(plate);
        var txt = el('span', 'tk-txt' + (it.t2 ? '' : ' one-line'));
        txt.appendChild(el('span', 'tk-t1', it.t1));
        if (it.t2) txt.appendChild(el('span', 'tk-t2', it.t2));
        chip.appendChild(txt);
      } else {
        chip.appendChild(el('span', 'tk-game', it.name));
        chip.appendChild(el('span', 'tk-draw', it.type === 'draw' ? it.sub : it.label));
      }
      item.appendChild(chip);
      if (it.type === 'draw') {
        var balls = el('div', 'tk-balls');
        it.numbers.forEach(function (n) { balls.appendChild(el('span', 'tball', n)); });
        it.bonus.forEach(function (b) {
          var parts = String(b.label).toUpperCase().split(' ');
          var lab = el('span', 'tk-bl');
          lab.appendChild(document.createTextNode(parts.slice(0, -1).join(' ') || parts[0]));
          if (parts.length > 1) { lab.appendChild(el('br')); lab.appendChild(document.createTextNode(parts[parts.length - 1])); }
          balls.appendChild(lab);
          var ball = el('span', 'tball tball--bonus', b.value);
          setBonusVars(ball, b.color, b.ink);
          balls.appendChild(ball);
        });
        item.appendChild(balls);
        if (it.jackpot) {
          var jp = el('div', 'tk-jp');
          jp.style.marginLeft = '12px';
          jp.appendChild(el('span', 'tk-jp-l', 'Jackpot'));
          jp.appendChild(el('span', 'tk-jp-v', it.jackpot.toUpperCase()));
          item.appendChild(jp);
        }
      } else {
        var j = el('div', 'tk-jp');
        j.appendChild(el('span', 'tk-jp-l', it.when || it.label));
        j.appendChild(el('span', 'tk-jp-v', it.value.toUpperCase()));
        item.appendChild(j);
      }
      set.appendChild(item);
      set.appendChild(star());
    });
    return set;
  }

  function renderTicker(data) {
    var items = tickerItems(data);
    var sig = JSON.stringify(items);
    if (sig === state.tickerSig) return;
    state.tickerSig = sig;
    trackEl.className = 'tk-track';
    while (trackEl.firstChild) trackEl.removeChild(trackEl.firstChild);
    if (!items.length) return;
    var a = buildTickerSet(items);
    trackEl.appendChild(a);
    var setW = a.offsetWidth;
    var viewW = 1920 - 300;
    // repeat the items so one set is always wider than the screen
    var copies = Math.max(1, Math.ceil(viewW / Math.max(setW, 1)));
    for (var c = 1; c < copies; c++) a.appendChild(buildTickerSet(items));
    setW = a.offsetWidth;
    trackEl.appendChild(a.cloneNode(true));
    var speed = config.settings.tickerPixelsPerSecond || 130;
    var dur = Math.max(10, setW / speed);
    trackEl.style.animationDuration = trackEl.style.webkitAnimationDuration = dur.toFixed(2) + 's';
    void trackEl.offsetWidth;
    trackEl.className = 'tk-track run';
  }

  function renderUpdated(data) {
    var stamp = data && (data.generated_at ? new Date(data.generated_at) : null);
    if (!stamp || isNaN(stamp)) { updatedEl.textContent = ''; return; }
    var p = nyParts(stamp);
    var label = 'Updated ' + clock12(p);
    if (isoDay(p) !== todayNY()) label = 'Updated ' + MONTHS[p.mo - 1].slice(0, 3) + ' ' + p.d + ', ' + clock12(p);
    updatedEl.textContent = label;
  }

  /* --------------------------------------------------------------- data */

  function applyData(data) {
    var first = !state.data;
    state.data = data;
    emptyEl.className = 'hidden';
    renderTicker(data);
    renderUpdated(data);
    if (first || !state.slides.length) {
      rebuildSlides();
      showSlide(0);
    } else {
      state.needsRebuild = true;
    }
  }

  function showEmpty() {
    if (!state.data) emptyEl.className = '';
  }

  function poll() {
    clearTimeout(timers.poll);
    var url = DATA_URL;
    if (!DEMO) url += (url.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now();
    getJSON(url, function (err, raw) {
      var data = err ? null : cleanData(raw);
      if (data) {
        var changed = !state.data || JSON.stringify(state.data.games) !== JSON.stringify(data.games);
        store(CACHE_KEY, { saved: Date.now(), data: data });
        if (changed) applyData(data);
        else {
          state.data = data;
          renderTicker(data);   // no-op unless its wording changed (e.g. "tonight" after midnight)
          renderUpdated(data);
        }
        timers.poll = setTimeout(poll, POLL_MS);
      } else {
        showEmpty();
        if (state.data) renderTicker(state.data);
        timers.poll = setTimeout(poll, RETRY_MS);
      }
    });
  }

  function scheduleReload() {
    clearTimeout(timers.reload);
    timers.reload = setTimeout(function tryReload() {
      if (window.location.protocol === 'file:') { window.location.reload(); return; }
      // only reload when the site answers, so a dropped connection never
      // swaps the numbers for a browser error page
      var xhr = new XMLHttpRequest();
      var page = window.location.pathname || '/';
      xhr.open('GET', page + (page.indexOf('?') < 0 ? '?' : '&') + 'alive=' + Date.now(), true);
      xhr.timeout = 15000;
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 400) window.location.reload();
        else timers.reload = setTimeout(tryReload, 60000);
      };
      xhr.onerror = xhr.ontimeout = function () { timers.reload = setTimeout(tryReload, 60000); };
      try { xhr.send(); } catch (e) { timers.reload = setTimeout(tryReload, 60000); }
    }, RELOAD_MS);
  }

  /* -------------------------------------------------------------- stage */

  function fitStage() {
    var w = window.innerWidth || document.documentElement.clientWidth || STAGE_W;
    var h = window.innerHeight || document.documentElement.clientHeight || STAGE_H;
    var s = Math.min(w / STAGE_W, h / STAGE_H);
    var x = Math.round((w - STAGE_W * s) / 2), y = Math.round((h - STAGE_H * s) / 2);
    var t = 'translate(' + x + 'px,' + y + 'px) scale(' + s + ')';
    stage.style.transform = t; stage.style.webkitTransform = t;
  }

  /* -------------------------------------------------------------- start */

  // Keep every logo decoded in memory so a slide never waits for one.
  var logoCache = [];
  function preloadLogos() {
    var seen = {};
    Object.keys(config.games).forEach(function (id) {
      var src = config.games[id] && config.games[id].logo;
      if (!src || seen[src]) return;
      seen[src] = true;
      var im = new Image();
      im.src = src;
      logoCache.push(im);
    });
  }

  function start() {
    if (DEMO) document.getElementById('demo-flag').className = 'on';
    preloadLogos();
    var cached = load(CACHE_KEY);
    var cachedData = cached ? cleanData(cached.data) : null;
    if (cachedData) applyData(cachedData);
    else if (!DEMO) showEmpty();
    poll();
    scheduleReload();
  }

  function whenFontsReady(cb) {
    var called = false;
    function go() { if (!called) { called = true; cb(); } }
    try {
      if (document.fonts && document.fonts.load) {
        Promise.all([
          document.fonts.load('900 40px "VL Montserrat"'),
          document.fonts.load('italic 900 40px "VL Montserrat"'),
          document.fonts.load('800 40px "VL Montserrat"'),
          document.fonts.load('600 40px "VL Montserrat"'),
          document.fonts.load('40px "VL Anton"')
        ]).then(go, go);
      } else { setTimeout(go, 600); }
    } catch (e) { setTimeout(go, 600); }
    setTimeout(go, 2500);
  }

  // A saved copy of the page (sw.js), so a TV that starts while the internet is
  // down (after a power cut the stick boots before the modem) still opens it
  // and shows the last numbers saved here, instead of the browser's error page.
  function keepOfflineCopy() {
    try {
      if ('serviceWorker' in navigator && window.location.protocol !== 'file:') {
        navigator.serviceWorker.register('sw.js').catch(function () { /* not allowed here: the live page still works */ });
      }
    } catch (e) { /* an old WebView without it: the live page still works */ }
  }

  keepOfflineCopy();
  fitStage();
  window.addEventListener('resize', fitStage);
  // games.json (logos, colours, prices); the last good copy is kept so a
  // failed fetch at an hourly reload does not strip them for an hour.
  getJSON('games.json', function (err, cfg) {
    if (!err && cfg && cfg.games) store('vl-tv-games-v1', cfg);
    else cfg = load('vl-tv-games-v1');
    if (cfg && cfg.games) {
      config.settings = cfg.settings || {};
      config.extras = cfg.extras || {};
      config.games = cfg.games || {};
    }
    whenFontsReady(start);
  });

  // test hook for screenshots: window.__tv.go(n) jumps to slide n
  window.__tv = {
    go: function (n) { showSlide(n); },
    count: function () { return state.slides.length; },
    plan: function () { return state.slides.map(function (s) { return s.units.map(function (u) { return u.look.name; }).join(' + '); }); }
  };
})();
