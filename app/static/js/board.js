/* Live queue board: quiet auto-refresh and drag-and-drop assignment. Without JavaScript the dropdown/“Best agent” forms still work. */
(function () {
  'use strict';
  var root = function () { return document.getElementById('boardRoot'); };
  var dragging = null, stamp = Date.now(), busy = false;
  function csrf() { var i = document.querySelector('#csrfHolder [name=_csrf]'); return i ? i.value : ''; }
  function toast(m, k) { if (window.baiToast) window.baiToast(m, k); }

  function refresh() {
    if (document.hidden || dragging || busy || document.querySelector('#peek.open, #palette[open]')) return;
    fetch('/board', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (!r.ok || r.redirected && /login/.test(r.url)) throw new Error('stop'); return r.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html'), n = doc.getElementById('boardRoot'), o = root();
      if (n && o) { o.innerHTML = n.innerHTML; stamp = Date.now(); }
    }).catch(function () { var c = document.getElementById('boardLive'); if (c) c.hidden = true; clearInterval(window.__bdTimer); });
  }

  function assign(ticketId, agentId, card) {
    if (card) card.classList.add('dropping');
    busy = true;
    var body = new URLSearchParams({_csrf: csrf(), user_id: agentId});
    fetch('/tickets/' + ticketId + '/assign', {method: 'POST', body: body, credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (x) { toast(x.d.message, x.ok ? 'success' : 'error'); })
      .catch(function () { toast('Could not assign that ticket. Check your connection.', 'error'); })
      .then(function () { busy = false; dragging = null; refresh(); });
  }

  document.addEventListener('dragstart', function (e) {
    var c = e.target.closest && e.target.closest('.bq-card'); if (!c) return;
    dragging = c.dataset.ticket; c.classList.add('drag');
    e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragging);
    document.body.classList.add('is-dragging');
  });
  document.addEventListener('dragend', function () {
    dragging = null; document.body.classList.remove('is-dragging');
    Array.prototype.forEach.call(document.querySelectorAll('.drag,.over'), function (n) { n.classList.remove('drag', 'over'); });
  });
  document.addEventListener('dragover', function (e) {
    var a = e.target.closest && e.target.closest('.ag-card'); if (!a || !dragging) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
    Array.prototype.forEach.call(document.querySelectorAll('.ag-card.over'), function (n) { if (n !== a) n.classList.remove('over'); });
    a.classList.add('over');
  });
  document.addEventListener('drop', function (e) {
    var a = e.target.closest && e.target.closest('.ag-card'); if (!a || !dragging) return;
    e.preventDefault(); var id = dragging; a.classList.remove('over'); assign(id, a.dataset.agent, a);
  });

  function ago() {
    var s = Math.round((Date.now() - stamp) / 1000), el = document.getElementById('boardAgo');
    if (el) el.textContent = s < 8 ? 'just now' : s < 60 ? s + 's ago' : Math.floor(s / 60) + ' min ago';
  }
  var chip = document.getElementById('boardLive');
  if (chip && window.fetch) { chip.hidden = false; window.__bdTimer = setInterval(refresh, 15000); setInterval(ago, 5000); }
})();
