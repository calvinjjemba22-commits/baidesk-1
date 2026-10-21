(function () {
  var root = document.documentElement;
  function csrf() { var m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ''; }

  /* ── theme: applied pre-paint in <head>; toggling saves locally and to the user's profile ── */
  window.toggleTheme = function () {
    var next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('baidesk-theme', next); } catch (e) {}
    fetch('/profile/theme', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
      body: JSON.stringify({ theme: next }) }).catch(function () {});
  };

  /* ── unread notification badge (sidebar item + top bell) ── */
  async function refreshBadge() {
    var els = document.querySelectorAll('[data-notif-badge]'); if (!els.length) return;
    try {
      var r = await fetch('/api/notifications/unread', { credentials: 'same-origin' });
      if (!r.ok) return;
      var d = await r.json();
      els.forEach(function (el) {
        el.textContent = d.count > 99 ? '99+' : (d.count ? d.count : '');
        el.style.display = d.count ? 'inline-flex' : 'none';
      });
    } catch (e) {}
  }
  refreshBadge(); setInterval(refreshBadge, 30000);

  /* ── SLA countdown (server sends a UTC timestamp ending in Z) ── */
  window.startSlaClock = function () {
    document.querySelectorAll('[data-due]').forEach(function (el) {
      var due = new Date(el.dataset.due);
      function tick() {
        var ms = due - new Date(), box = el.closest('.sla-box');
        if (ms <= 0) { el.textContent = 'BREACHED'; if (box) box.classList.add('danger'); return; }
        var s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
            m = Math.floor((s % 3600) / 60), sec = s % 60;
        el.textContent = (d ? d + 'd ' : '') + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
      }
      tick(); setInterval(tick, 1000);
    });
  };

  document.addEventListener('DOMContentLoaded', function () {
    if (window.startSlaClock) startSlaClock();

    /* ── keyboard shortcuts ── */
    var dlg = document.getElementById('shortcuts'), gPending = false, gTimer;
    function openHelp() { if (dlg && dlg.showModal && !dlg.open) dlg.showModal(); }
    var helpBtn = document.getElementById('helpBtn'); if (helpBtn) helpBtn.addEventListener('click', openHelp);
    /* ── queue navigation: j / k move through ticket rows, Enter opens ── */
    var kbRow = null;
    function moveRow(d) {
      var rows = Array.prototype.slice.call(document.querySelectorAll('table tbody tr')).filter(function (r) { return r.querySelector('a.strong'); });
      if (!rows.length) return;
      var i = kbRow ? rows.indexOf(kbRow) + d : 0;
      i = Math.max(0, Math.min(rows.length - 1, i));
      if (kbRow) kbRow.classList.remove('kb-focus');
      kbRow = rows[i]; kbRow.classList.add('kb-focus'); kbRow.scrollIntoView({block: 'nearest'});
    }
    var isMac = /Mac|iPhone|iPad/.test(navigator.platform || '');
    if (isMac) document.querySelectorAll('.kbd-hint kbd:first-child').forEach(function (k) { k.textContent = '⌘'; });
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
        var q = document.getElementById('globalSearch');
        if (q) { e.preventDefault(); q.focus(); q.select(); }
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var t = e.target, tag = t.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable) return;
      if (dlg && dlg.open) return;
      var k = e.key;
      if (gPending) {
        gPending = false; clearTimeout(gTimer);
        var a = document.querySelector('[data-go="' + k + '"]');
        if (a) { e.preventDefault(); location.href = a.href; }
        return;
      }
      if (k === '/') { var s = document.getElementById('globalSearch'); if (s) { e.preventDefault(); s.focus(); } }
      else if (k === 'n' && document.body.dataset.newTicket) { location.href = document.body.dataset.newTicket; }
      else if (k === 'g') { gPending = true; gTimer = setTimeout(function () { gPending = false; }, 1200); }
      else if (k === '?') { e.preventDefault(); openHelp(); }
      else if (k === 'j' || k === 'k') { moveRow(k === 'j' ? 1 : -1); e.preventDefault(); }
      else if (k === 'Enter' && kbRow) { var a = kbRow.querySelector('a.strong, td a'); if (a) location.href = a.href; }
      else if (k === 'r') { var rb = document.getElementById('replyBody'); if (rb) { e.preventDefault(); rb.scrollIntoView({block: 'center'}); rb.focus(); } }
    });

    /* ── account menu: close on outside click or Escape ── */
    var uc = document.querySelector('details.user-card');
    if (uc) {
      document.addEventListener('click', function (e) { if (uc.open && !uc.contains(e.target)) uc.open = false; });
      document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && uc.open) { uc.open = false; uc.querySelector('summary').focus(); } });
    }

    /* ── confirm dialogs for risky actions ── */
    document.addEventListener('click', function (e) {
      var b = e.target.closest('[data-confirm]');
      if (b && !window.confirm(b.getAttribute('data-confirm'))) e.preventDefault();
    });

    /* ── ticket queue: bulk selection ── */
    var bulk = document.getElementById('bulkForm');
    if (bulk) {
      var rows = bulk.querySelectorAll('.rowcheck'), all = document.getElementById('selectAll'),
          count = document.getElementById('bulkCount'), action = document.getElementById('bulkAction'),
          bar = document.getElementById('bulkBar');
      function sync() {
        var n = bulk.querySelectorAll('.rowcheck:checked').length;
        if (count) count.textContent = n + ' selected';
        if (bar) bar.classList.toggle('active', n > 0);
        if (all) { all.checked = n > 0 && n === rows.length; all.indeterminate = n > 0 && n < rows.length; }
      }
      function showValue() {
        bulk.querySelectorAll('[data-for]').forEach(function (s) { s.style.display = (action && s.dataset.for === action.value) ? '' : 'none'; });
      }
      if (all) all.addEventListener('change', function () { rows.forEach(function (c) { c.checked = all.checked; }); sync(); });
      rows.forEach(function (c) { c.addEventListener('change', sync); });
      if (action) action.addEventListener('change', showValue);
      showValue(); sync();
    }

    /* ── ticket reply box: public/internal tabs + canned responses ── */
    var form = document.getElementById('replyForm');
    if (form) {
      var type = document.getElementById('commentType'), body = document.getElementById('replyBody'),
          btn = document.getElementById('replyBtn'), tabs = form.querySelectorAll('[role=tab]');
      tabs.forEach(function (tab) {
        tab.addEventListener('click', function () {
          var internal = tab.dataset.type === 'INTERNAL', cust = tab.dataset.type === 'CUSTOMER';
          type.value = tab.dataset.type;
          tabs.forEach(function (t) { t.setAttribute('aria-selected', t === tab ? 'true' : 'false'); });
          form.classList.toggle('internal', internal);
          btn.textContent = internal ? 'Add internal note' : cust ? 'Record customer reply' : 'Send reply';
          body.placeholder = internal ? 'Private note — only your team can see this. Use @Name to mention a colleague.'
                           : cust ? 'Paste or summarise what the customer wrote. The ticket reopens if it was resolved or pending.'
                                  : 'Write a reply… Use @Name to mention a colleague.';
          body.focus();
        });
      });
      var canned = document.getElementById('cannedSelect');
      if (canned) canned.addEventListener('change', function () {
        var opt = canned.options[canned.selectedIndex];
        if (opt && opt.dataset.body) { body.value = body.value ? body.value + '\n\n' + opt.dataset.body : opt.dataset.body; body.focus(); }
        canned.selectedIndex = 0;
      });
    }
  });
})();
