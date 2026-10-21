/* Dashboard visuals: KPI sparklines and the 7-day ticket volume chart. No libraries, no network. */
(function () {
  var NS = 'http://www.w3.org/2000/svg';

  /* ── sparklines ── (null = no data that day; the line skips it) */
  function spark(svg) {
    var vals; try { vals = JSON.parse(svg.dataset.spark || '[]'); } catch (e) { return; }
    var pts = vals.map(function (v, i) { return v == null ? null : [i, v]; }).filter(Boolean);
    if (pts.length < 2) return;
    var W = 120, H = 34, pad = 4;
    var ys = pts.map(function (p) { return p[1]; }), lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    var span = hi - lo;
    function x(i) { return (i / (vals.length - 1)) * W; }
    function y(v) { return span ? H - pad - ((v - lo) / span) * (H - pad * 2) : H / 2; }
    var line = pts.map(function (p, k) { return (k ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1); }).join(' ');
    var area = line + ' L' + x(pts[pts.length - 1][0]).toFixed(1) + ' ' + H + ' L' + x(pts[0][0]).toFixed(1) + ' ' + H + ' Z';
    var id = 'sg' + Math.random().toString(36).slice(2, 8);
    svg.innerHTML = '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="currentColor" stop-opacity=".22"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + area + '" fill="url(#' + id + ')"/>' +
      '<path d="' + line + '" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
    try { var p = svg.querySelector('path:last-child'); p.style.setProperty('--len', Math.ceil(p.getTotalLength() * 4)); } catch (e) {}   // non-scaling stroke: be generous
  }

  /* ── line chart ── */
  function niceStep(max) {
    var rough = Math.max(1, max) / 4, mag = Math.pow(10, Math.floor(Math.log10(rough))), n = rough / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
  }
  function el(tag, attrs) { var n = document.createElementNS(NS, tag); for (var k in attrs) n.setAttribute(k, attrs[k]); return n; }

  function chart(box) {
    var d; try { d = JSON.parse(box.dataset.chart); } catch (e) { return; }
    var series = [{ name: 'New', cls: 'a', v: d.new }, { name: 'Resolved', cls: 'b', v: d.resolved }];
    var W = Math.max(280, box.clientWidth), H = 236, m = { t: 14, r: 16, b: 30, l: 38 };
    var max = Math.max.apply(null, d.new.concat(d.resolved, [1])), step = niceStep(max), top = Math.ceil(max / step) * step;
    var n = d.labels.length, iw = W - m.l - m.r, ih = H - m.t - m.b;
    function X(i) { return m.l + (n > 1 ? i / (n - 1) : 0.5) * iw; }
    function Y(v) { return m.t + ih - (v / top) * ih; }

    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img',
      'aria-label': 'New and resolved tickets per day for the last ' + n + ' days' });
    var defs = el('defs', {});
    series.forEach(function (s) {
      var g = el('linearGradient', { id: 'vg-' + s.cls, x1: 0, y1: 0, x2: 0, y2: 1 });
      g.appendChild(el('stop', { offset: 0, 'stop-color': 'var(--c-' + s.cls + ')', 'stop-opacity': .16 }));
      g.appendChild(el('stop', { offset: 1, 'stop-color': 'var(--c-' + s.cls + ')', 'stop-opacity': 0 }));
      defs.appendChild(g);
    });
    svg.appendChild(defs);

    for (var v = 0; v <= top; v += step) {
      svg.appendChild(el('line', { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: 'grid' }));
      var t = el('text', { x: m.l - 10, y: Y(v) + 4, class: 'ax', 'text-anchor': 'end' }); t.textContent = v; svg.appendChild(t);
    }
    d.labels.forEach(function (lb, i) {
      var t = el('text', { x: X(i), y: H - 8, class: 'ax', 'text-anchor': 'middle' }); t.textContent = lb; svg.appendChild(t);
      svg.appendChild(el('line', { x1: X(i), x2: X(i), y1: m.t + ih, y2: m.t + ih + 4, class: 'tick' }));
    });
    series.forEach(function (s) {
      var pts = s.v.map(function (val, i) { return [X(i), Y(val)]; });
      var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
      svg.appendChild(el('path', { d: line + ' L' + pts[n - 1][0] + ' ' + (m.t + ih) + ' L' + pts[0][0] + ' ' + (m.t + ih) + ' Z', fill: 'url(#vg-' + s.cls + ')' }));
      svg.appendChild(el('path', { d: line, class: 'ln ln-' + s.cls, fill: 'none' }));
      s.dots = pts.map(function (p) { var c = el('circle', { cx: p[0], cy: p[1], r: 3.6, class: 'pt pt-' + s.cls }); svg.appendChild(c); return c; });
    });
    var cross = el('line', { y1: m.t, y2: m.t + ih, class: 'cross', visibility: 'hidden' }); svg.appendChild(cross);
    var hit = el('rect', { x: m.l, y: m.t, width: iw, height: ih, fill: 'transparent' }); svg.appendChild(hit);

    var tip = document.createElement('div'); tip.className = 'chart-tip'; tip.hidden = true;
    var rows = d.labels.map(function (lb, i) { return '<tr><th scope="row">' + lb + '</th><td>' + d.new[i] + '</td><td>' + d.resolved[i] + '</td></tr>'; }).join('');
    var sr = document.createElement('table'); sr.className = 'sr-only';
    sr.innerHTML = '<caption>Tickets per day</caption><thead><tr><th>Day</th><th>New</th><th>Resolved</th></tr></thead><tbody>' + rows + '</tbody>';

    function show(i) {
      cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.setAttribute('visibility', 'visible');
      series.forEach(function (s) { s.dots.forEach(function (c, j) { c.classList.toggle('on', j === i); }); });
      tip.innerHTML = '<b>' + d.labels[i] + '</b><span><i class="dot a"></i>New<em>' + d.new[i] + '</em></span><span><i class="dot b"></i>Resolved<em>' + d.resolved[i] + '</em></span>';
      tip.hidden = false;
      var left = X(i) + 14; if (left + tip.offsetWidth > W - 4) left = X(i) - tip.offsetWidth - 14;
      tip.style.left = Math.max(4, left) + 'px'; tip.style.top = (m.t + 6) + 'px';
    }
    function hide() { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; series.forEach(function (s) { s.dots.forEach(function (c) { c.classList.remove('on'); }); }); }
    hit.addEventListener('mousemove', function (e) {
      var r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * (W / r.width);
      show(Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / iw) * (n - 1)))));
    });
    hit.addEventListener('mouseleave', hide);

    box.textContent = ''; box.appendChild(svg); box.appendChild(tip); box.appendChild(sr);
    $$('.ln', svg).forEach(function (p) { try { p.style.setProperty('--len', Math.ceil(p.getTotalLength()) + 2); } catch (e) {} });
  }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }

  /* numbers count up once on the first paint */
  function countUp() {
    if (!document.documentElement.classList.contains('anim')) return;
    $$('.kpi-value').forEach(function (el) {
      var m = /^(\d+(?:\.\d+)?)(%?)$/.exec(el.textContent.trim()); if (!m) return;
      var to = parseFloat(m[1]), dec = (m[1].split('.')[1] || '').length, t0 = null;
      function tick(t) {
        t0 = t0 || t; var p = Math.min(1, (t - t0) / 800), e = 1 - Math.pow(1 - p, 3);
        el.textContent = (to * e).toFixed(dec) + m[2]; if (p < 1) requestAnimationFrame(tick); else el.textContent = m[1] + m[2];
      }
      el.textContent = '0' + m[2]; requestAnimationFrame(tick);
    });
  }

  /* quiet live refresh: swap the data panels every minute (paused while you are interacting with them) */
  var SWAP = ['.kpi-row', '.dash-row-main', '.dash-row-lists', 'section.activity'], stamp = Date.now(), chip = null;
  function busy() { return document.querySelector('.row-menu[open], #peek.open, #palette[open], .toast') && !document.querySelector('.toast.success') ? true : !!document.querySelector('.row-menu[open], #peek.open, #palette[open]'); }
  function refreshLive() {
    if (document.hidden || busy()) return;
    fetch(location.pathname, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (!r.ok || r.redirected && /login/.test(r.url)) throw new Error('stop'); return r.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      SWAP.forEach(function (sel) { var a = document.querySelector(sel), b = doc.querySelector(sel); if (a && b) a.replaceWith(b); });
      stamp = Date.now(); init(false);
    }).catch(function () { if (chip) chip.hidden = true; clearInterval(window.__baiLive); });
  }
  function tickAgo() {
    var s = Math.round((Date.now() - stamp) / 1000), el = document.getElementById('liveAgo');
    if (el) el.textContent = s < 10 ? 'just now' : s < 60 ? s + 's ago' : Math.floor(s / 60) + ' min ago';
  }

  function init(first) {
    document.querySelectorAll('svg.spark').forEach(spark);
    if (first !== false) countUp();
    var box = document.getElementById('volumeChart');
    if (!box) return;
    chart(box);
    var last = box.clientWidth, raf;
    if (window.ResizeObserver) new ResizeObserver(function () {
      if (Math.abs(box.clientWidth - last) < 2) return;
      last = box.clientWidth; cancelAnimationFrame(raf); raf = requestAnimationFrame(function () { chart(box); });
    }).observe(box);
  }
  function boot() {
    init(true);
    chip = document.getElementById('liveChip');
    if (chip && 'fetch' in window) { chip.hidden = false; window.__baiLive = setInterval(refreshLive, 60000); setInterval(tickAgo, 10000); document.addEventListener('visibilitychange', function () { if (!document.hidden && Date.now() - stamp > 60000) refreshLive(); }); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
