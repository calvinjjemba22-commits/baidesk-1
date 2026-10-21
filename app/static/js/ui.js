/* BaiDesk interface layer: command palette (Ctrl/⌘+K), ticket preview drawer, toasts, collapsible menu.
   No libraries. Everything degrades gracefully: without JavaScript the normal pages and the search form still work. */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function typing(el) { return el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable); }
  var ICONS = {
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>', inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>', plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    home: '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>', clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    chart: '<path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-6"/>', bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>', moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    award: '<circle cx="12" cy="8" r="6"/><path d="M15.48 12.89 17 22l-5-3-5 3 1.52-9.11"/>', 'calendar-check': '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/><path d="m9 16 2 2 4-4"/>',
    command: '<path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"/>', ticket: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    'search-x': '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>'
  };
  function svg(name, size) { return '<svg class="i" width="' + (size || 18) + '" height="' + (size || 18) + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || ICONS.command) + '</svg>'; }

  /* ───────── toasts: server-side flash messages become floating notifications ───────── */
  var tbox = $('#toasts');
  function toast(msg, kind, ms) {
    if (!tbox) return;
    var t = document.createElement('div');
    t.className = 'toast ' + (kind || 'info'); t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    t.innerHTML = '<span class="t-bar"></span><span class="t-msg"></span><button type="button" aria-label="Dismiss">×</button>';
    $('.t-msg', t).textContent = msg;
    tbox.appendChild(t);
    var timer, life = ms || (kind === 'error' ? 9000 : 5000);
    function close() { clearTimeout(timer); t.classList.add('out'); setTimeout(function () { t.remove(); }, reduce ? 0 : 220); }
    function arm() { timer = setTimeout(close, life); }
    $('button', t).addEventListener('click', close);
    t.addEventListener('mouseenter', function () { clearTimeout(timer); });
    t.addEventListener('mouseleave', arm);
    arm();
  }
  window.baiToast = toast;
  $$('.main > .flash:not([data-persist])').forEach(function (f) {
    var kind = /error/.test(f.className) ? 'error' : /success/.test(f.className) ? 'success' : 'info';
    toast(f.textContent.trim(), kind); f.remove();
  });

  /* ───────── collapsible menu ───────── */
  var navBtn = $('#navToggle');
  if (navBtn) navBtn.addEventListener('click', function () {
    var h = document.documentElement, mini = h.dataset.nav === 'mini';
    if (mini) { delete h.dataset.nav; } else { h.dataset.nav = 'mini'; }
    try { localStorage.setItem('baidesk-nav', mini ? '' : 'mini'); } catch (e) {}
  });

  /* ───────── command palette ───────── */
  var dlg = $('#palette'), input = $('#palInput'), list = $('#palList');
  var commands = []; try { commands = JSON.parse(($('#palette-commands') || {}).textContent || '[]'); } catch (e) {}
  var items = [], cur = 0, seq = 0, timer = null, remote = {tickets: [], customers: []}, lastQ = '';
  function recent() { try { return JSON.parse(localStorage.getItem('baidesk-recent') || '[]'); } catch (e) { return []; } }
  function remember(it) {
    try { var r = recent().filter(function (x) { return x.url !== it.url; }); r.unshift({label: it.label, hint: it.hint, icon: it.icon, url: it.url}); localStorage.setItem('baidesk-recent', JSON.stringify(r.slice(0, 5))); } catch (e) {}
  }
  function build(q) {
    var out = [], ql = q.toLowerCase().trim();
    if (!ql) {
      recent().forEach(function (r) { out.push({group: 'Recent', label: r.label, hint: r.hint, icon: r.icon || 'ticket', url: r.url}); });
      commands.forEach(function (c) { out.push({group: 'Go to & do', label: c.label, hint: c.hint, icon: c.icon, url: c.url, run: c.run}); });
      return out;
    }
    var words = ql.split(/\s+/);
    commands.forEach(function (c) { var l = c.label.toLowerCase(); if (words.every(function (w) { return l.indexOf(w) > -1; })) out.push({group: 'Commands', label: c.label, hint: c.hint, icon: c.icon, url: c.url, run: c.run}); });
    remote.tickets.forEach(function (t) { out.push({group: 'Tickets', label: t.number + '  ' + t.subject, hint: t.customer + ' · ' + t.status, icon: 'ticket', url: t.url, remember: true}); });
    remote.customers.forEach(function (c) { out.push({group: 'Customers', label: c.name, hint: (c.phone ? c.phone + ' · ' : '') + c.number, icon: 'users', url: c.url, remember: true}); });
    if (ql.length >= 2) out.push({group: 'Everywhere', label: 'Search all records for “' + q + '”', icon: 'search', url: '/search?q=' + encodeURIComponent(q)});
    return out;
  }
  function render() {
    var html = '', last = '';
    items.forEach(function (it, i) {
      if (it.group !== last) { html += '<div class="pal-group">' + esc(it.group) + '</div>'; last = it.group; }
      html += '<div class="pal-item' + (i === cur ? ' on' : '') + '" role="option" id="pal-' + i + '" aria-selected="' + (i === cur) + '" data-i="' + i + '">' + svg(it.icon, 17) +
        '<span class="pal-label">' + esc(it.label) + '</span>' + (it.hint ? '<span class="pal-hint">' + esc(it.hint) + '</span>' : '') + '</div>';
    });
    if (!items.length) html = '<div class="pal-empty">' + svg('search-x', 22) + '<b>Nothing found</b><span>Try a ticket number, a customer name or a phone number.</span></div>';
    list.innerHTML = html;
    input.setAttribute('aria-activedescendant', items.length ? 'pal-' + cur : '');
    var on = $('.pal-item.on', list); if (on) on.scrollIntoView({block: 'nearest'});
  }
  function refresh() { items = build(input.value); cur = Math.min(cur, Math.max(0, items.length - 1)); render(); }
  function fetchRemote(q) {
    var my = ++seq;
    if (q.trim().length < 2) { remote = {tickets: [], customers: []}; refresh(); return; }
    fetch('/api/palette?q=' + encodeURIComponent(q), {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (my !== seq || !d) return; remote = d; refresh();
    }).catch(function () {});
  }
  function activate(it) {
    if (!it) return;
    if (it.remember) remember(it);
    dlg.close();
    if (it.run === 'theme') { if (window.toggleTheme) window.toggleTheme(); }
    else if (it.run === 'help') { var h = $('#helpBtn'); if (h) h.click(); }
    else if (it.run === 'away') { var f = $('.user-menu form[action*="availability"]'); if (f) f.submit(); }
    else if (it.url) { location.href = it.url; }
  }
  function openPalette(prefill) {
    if (!dlg || !dlg.showModal) return;
    if (!dlg.open) dlg.showModal();
    input.value = prefill || ''; cur = 0; remote = {tickets: [], customers: []}; lastQ = '';
    refresh(); if (input.value) fetchRemote(input.value);
    setTimeout(function () { input.focus(); input.select(); }, 0);
  }
  if (dlg) {
    input.addEventListener('input', function () { cur = 0; refresh(); clearTimeout(timer); var q = input.value; timer = setTimeout(function () { fetchRemote(q); }, 130); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) { cur = (cur + 1) % items.length; render(); } }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { cur = (cur - 1 + items.length) % items.length; render(); } }
      else if (e.key === 'Enter') { e.preventDefault(); activate(items[cur]); }
    });
    list.addEventListener('mousemove', function (e) { var n = e.target.closest('.pal-item'); if (n && +n.dataset.i !== cur) { cur = +n.dataset.i; $$('.pal-item', list).forEach(function (x, i) { x.classList.toggle('on', i === cur); x.setAttribute('aria-selected', i === cur); }); } });
    list.addEventListener('click', function (e) { var n = e.target.closest('.pal-item'); if (n) activate(items[+n.dataset.i]); });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
    var top = $('#globalSearch');
    if (top) {
      top.addEventListener('focus', function (e) { e.preventDefault(); top.blur(); openPalette(top.value); });
      top.addEventListener('click', function () { top.blur(); openPalette(top.value); });
    }
  }
  window.baiPalette = openPalette;

  /* ───────── ticket preview drawer ───────── */
  var peek = $('#peek'), scrim = $('#peekScrim'), peekFrom = null, peekSeq = 0;
  function closePeek() {
    if (!peek || !peek.classList.contains('open')) return;
    peek.classList.remove('open'); peek.setAttribute('aria-hidden', 'true'); if (scrim) scrim.hidden = true;
    if (peekFrom && peekFrom.focus) { try { peekFrom.focus(); } catch (e) {} }
  }
  function openPeek(id, from) {
    if (!peek) return;
    var my = ++peekSeq; peekFrom = from || document.activeElement;
    peek.classList.add('open'); peek.setAttribute('aria-hidden', 'false'); if (scrim) scrim.hidden = false;
    $('.peek-body', peek).innerHTML = '<div class="peek-skel"><i></i><i></i><i></i><i></i></div>';
    fetch('/tickets/' + id + '/peek', {credentials: 'same-origin'}).then(function (r) {
      if (!r.ok) throw new Error(r.status); return r.text();
    }).then(function (html) {
      if (my !== peekSeq) return;
      $('.peek-body', peek).innerHTML = html;
      var c = $('[data-peek-close]', peek); if (c) c.focus();
    }).catch(function () { closePeek(); toast('Could not load that ticket preview.', 'error'); });
  }
  window.baiPeek = openPeek;
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-peek]');
    if (b) { e.preventDefault(); var m = b.closest('details'); if (m) m.open = false; openPeek(b.getAttribute('data-peek'), b); return; }
    if (e.target.closest('[data-peek-close]') || e.target === scrim) closePeek();
  });

  /* ───────── keyboard (capture phase so it wins over the older handlers in app.js) ───────── */
  document.addEventListener('keydown', function (e) {
    var k = e.key;
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (k === 'k' || k === 'K')) { e.preventDefault(); e.stopImmediatePropagation(); if (dlg && dlg.open) dlg.close(); else openPalette(''); return; }
    if (k === 'Escape' && peek && peek.classList.contains('open') && !(dlg && dlg.open)) { closePeek(); return; }
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (k === '/' && dlg) { e.preventDefault(); e.stopImmediatePropagation(); openPalette(''); return; }
    if ((k === ' ' || k === 'Spacebar') && $('tr.kb-focus [data-peek]')) { e.preventDefault(); e.stopImmediatePropagation(); var b = $('tr.kb-focus [data-peek]'); openPeek(b.getAttribute('data-peek'), b); }
  }, true);
})();

/* ───────── conversation page: filters, send shortcut, auto-grow, scroll to latest ───────── */
(function () {
  var stream = document.getElementById('stream');
  if (!stream) return;
  var filters = document.querySelectorAll('.tk-filters button');
  Array.prototype.forEach.call(filters, function (b) {
    b.addEventListener('click', function () {
      Array.prototype.forEach.call(filters, function (x) { x.classList.toggle('on', x === b); });
      stream.dataset.filter = b.dataset.filter;
    });
  });
  var form = document.getElementById('replyForm'), body = document.getElementById('replyBody');
  if (form && body) {
    function grow() { body.style.height = 'auto'; body.style.height = Math.min(240, body.scrollHeight + 2) + 'px'; }
    body.addEventListener('input', grow);
    body.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); if (body.value.trim() && form.reportValidity()) form.requestSubmit(); }
    });
    var tabs = form.querySelectorAll('.reply-tabs [data-type]');
    Array.prototype.forEach.call(tabs, function (t) { t.addEventListener('click', function () { form.classList.toggle('internal', t.dataset.type === 'INTERNAL'); body.focus(); }); });
    form.addEventListener('submit', function () { var b = document.getElementById('replyBtn'); if (b) { b.disabled = true; b.textContent = 'Sending…'; } });
  }
  // open at the newest message (only when there is a real conversation and the person did not jump to a section)
  var items = stream.querySelectorAll('.it:not(.day)');
  if (items.length > 4 && !location.hash) {
    var last = items[items.length - 1];
    requestAnimationFrame(function () { last.scrollIntoView({block: 'center'}); });
  }
})();
