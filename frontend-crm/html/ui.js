/* ============================================================================
   RM Assist — UI v7 interaction toolkit (RX). Zero dependencies, vanilla.
   Provides: command palette (Cmd/Ctrl+K), contextual detail drawer (right
   inspector + mobile bottom sheet), delegated accordions + tooltips, copy,
   confidence/badge/skeleton helpers, toast bridge. a11y: focus trap, ESC,
   ARIA, keyboard nav; honours prefers-reduced-motion.
   Loaded BEFORE app.js; all cross-script globals are accessed defensively.
   ============================================================================ */
(function (w, d) {
  'use strict';
  var RX = w.RX = w.RX || {};
  var esc = RX.escape = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  };
  RX.toast = function (msg) {
    try { if (typeof w.toast === 'function') return w.toast(msg); } catch (e) {}
    var t = d.getElementById('toast');
    if (!t) { t = d.createElement('div'); t.id = 'toast'; t.className = 'toast'; d.body.appendChild(t); }
    t.textContent = msg; t.classList.add('show'); setTimeout(function () { t.classList.remove('show'); }, 2400);
  };
  RX.copy = function (text, el) {
    var done = function () { RX.toast('Copied'); if (el) { el.classList.add('rx-ok-flash'); setTimeout(function () { el.classList.remove('rx-ok-flash'); }, 900); } };
    try { navigator.clipboard.writeText(text).then(done, function () { fallback(); }); } catch (e) { fallback(); }
    function fallback() { try { var ta = d.createElement('textarea'); ta.value = text; d.body.appendChild(ta); ta.select(); d.execCommand('copy'); d.body.removeChild(ta); done(); } catch (_) {} }
  };

  /* ---------- small HTML helpers ---------- */
  RX.confidence = function (score, label) {
    var pct = Math.max(0, Math.min(100, Math.round(score)));
    var cls = pct >= 75 ? '' : (pct >= 45 ? 'med' : 'low');
    return '<span class="rx-conf ' + cls + '" title="Confidence ' + pct + '%" aria-label="Confidence ' + pct + ' percent">'
      + (label ? esc(label) + ' ' : '') + pct + '%<span class="bar"><i style="width:' + pct + '%"></i></span></span>';
  };
  RX.badge = function (text, kind, icon) {
    return '<span class="rx-badge ' + (kind || '') + '">' + (icon ? esc(icon) + ' ' : '') + esc(text) + '</span>';
  };
  RX.skeleton = function (n) {
    var out = ''; for (var i = 0; i < (n || 3); i++) out += '<div class="rx-sk card"></div>'; return out;
  };

  /* ---------- accordion (event-delegated; works on injected HTML) ---------- */
  d.addEventListener('click', function (e) {
    var h = e.target.closest && e.target.closest('.rx-acc > .h');
    if (h && h.parentElement.classList.contains('rx-acc')) {
      var acc = h.parentElement; acc.classList.toggle('open');
      h.setAttribute('aria-expanded', acc.classList.contains('open') ? 'true' : 'false');
    }
  });

  /* ---------- tooltip (delegated, hover + keyboard focus) ---------- */
  var tip = null;
  function ensureTip() { if (!tip) { tip = d.createElement('div'); tip.className = 'rx-tip'; tip.setAttribute('role', 'tooltip'); d.body.appendChild(tip); } return tip; }
  function showTip(el) {
    var txt = el.getAttribute('data-tip'); if (!txt) return;
    var t = ensureTip(); t.textContent = txt;
    var r = el.getBoundingClientRect(); t.style.left = '-9999px'; t.classList.add('show');
    var tw = t.offsetWidth, th = t.offsetHeight;
    var left = Math.min(Math.max(8, r.left + r.width / 2 - tw / 2), w.innerWidth - tw - 8);
    var top = r.top - th - 8; if (top < 8) top = r.bottom + 8;
    t.style.left = left + 'px'; t.style.top = top + 'px';
  }
  function hideTip() { if (tip) tip.classList.remove('show'); }
  d.addEventListener('mouseover', function (e) { var el = e.target.closest && e.target.closest('[data-tip]'); if (el) showTip(el); });
  d.addEventListener('mouseout', function (e) { if (e.target.closest && e.target.closest('[data-tip]')) hideTip(); });
  d.addEventListener('focusin', function (e) { var el = e.target.closest && e.target.closest('[data-tip]'); if (el) showTip(el); });
  d.addEventListener('focusout', hideTip);
  w.addEventListener('scroll', hideTip, true);

  /* ---------- contextual detail drawer (right inspector / mobile sheet) ---------- */
  var drawer = RX.drawer = {
    _el: null, _scrim: null, _lastFocus: null,
    _mount: function () {
      if (this._el) return;
      var scrim = this._scrim = d.createElement('div'); scrim.className = 'rx-scrim'; scrim.addEventListener('click', function () { drawer.close(); });
      var el = this._el = d.createElement('aside'); el.className = 'rx-drawer'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-modal', 'true'); el.setAttribute('aria-label', 'Details');
      d.body.appendChild(scrim); d.body.appendChild(el);
    },
    open: function (cfg) {
      this._mount(); cfg = cfg || {};
      this._lastFocus = d.activeElement;
      var badges = (cfg.badges || []).join(' ');
      var sections = (cfg.sections || []).map(function (s) {
        return '<div class="rx-section"><div class="lbl">' + esc(s.label) + '</div>' + (s.html || '') + '</div>';
      }).join('');
      var actions = (cfg.actions || []).map(function (a, i) {
        return '<button class="rx-btn ' + (a.kind || '') + '" data-act="' + i + '">' + (a.icon ? esc(a.icon) + ' ' : '') + esc(a.label) + '</button>';
      }).join('');
      this._el.innerHTML =
        '<div class="dh"><div class="t"><b>' + esc(cfg.title || 'Details') + '</b>' + (cfg.subtitle ? '<span>' + esc(cfg.subtitle) + '</span>' : '') + '</div>'
        + '<button class="x" aria-label="Close details">&#10005;</button></div>'
        + '<div class="db">' + (badges ? '<div class="rx-section">' + badges + '</div>' : '') + sections + '</div>'
        + (actions ? '<div class="da">' + actions + '</div>' : '');
      var self = this;
      this._el.querySelector('.x').addEventListener('click', function () { self.close(); });
      (cfg.actions || []).forEach(function (a, i) {
        var b = self._el.querySelector('[data-act="' + i + '"]'); if (b && a.onClick) b.addEventListener('click', function () { a.onClick(self); });
      });
      requestAnimationFrame(function () { self._scrim.classList.add('show'); self._el.classList.add('show'); });
      this._trap();
      var focusFirst = this._el.querySelector('.x'); if (focusFirst) focusFirst.focus();
    },
    close: function () {
      if (!this._el) return;
      this._el.classList.remove('show'); this._scrim.classList.remove('show');
      if (this._untrap) this._untrap();
      var lf = this._lastFocus; if (lf && lf.focus) setTimeout(function () { try { lf.focus(); } catch (e) {} }, 0);
    },
    isOpen: function () { return this._el && this._el.classList.contains('show'); },
    _trap: function () {
      var el = this._el, self = this;
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); self.close(); return; }
        if (e.key !== 'Tab') return;
        var f = el.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (!f.length) return; var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && d.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && d.activeElement === last) { e.preventDefault(); first.focus(); }
      }
      d.addEventListener('keydown', onKey);
      this._untrap = function () { d.removeEventListener('keydown', onKey); self._untrap = null; };
    }
  };

  /* ---------- command palette (Cmd/Ctrl+K) ---------- */
  var pal = RX.palette = {
    _wrap: null, _input: null, _list: null, _items: [], _flat: [], _active: 0, _lastFocus: null,
    providers: [],
    register: function (fn) { this.providers.push(fn); },
    _mount: function () {
      if (this._wrap) return;
      var wrap = this._wrap = d.createElement('div'); wrap.className = 'rx-pal-wrap'; wrap.setAttribute('role', 'dialog'); wrap.setAttribute('aria-modal', 'true'); wrap.setAttribute('aria-label', 'Command palette');
      wrap.innerHTML = '<div class="rx-pal"><div class="pin"><span class="ic">&#9906;</span>'
        + '<input type="text" placeholder="Search customers, steps, actions…" aria-label="Command palette search" autocomplete="off" spellcheck="false"/></div>'
        + '<div class="list" role="listbox"></div>'
        + '<div class="pft"><span><span class="rx-kbd">↑</span> <span class="rx-kbd">↓</span> navigate</span><span><span class="rx-kbd">↵</span> open</span><span><span class="rx-kbd">esc</span> close</span></div></div>';
      d.body.appendChild(wrap);
      this._input = wrap.querySelector('input'); this._list = wrap.querySelector('.list');
      var self = this;
      wrap.addEventListener('click', function (e) { if (e.target === wrap) self.close(); });
      this._input.addEventListener('input', function () { self._render(self._input.value); });
      this._input.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); self._move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); self._move(-1); }
        else if (e.key === 'Enter') { e.preventDefault(); self._run(self._active); }
        else if (e.key === 'Escape') { e.preventDefault(); self.close(); }
      });
    },
    open: function () {
      this._mount(); this._lastFocus = d.activeElement;
      this._items = []; var self = this;
      this.providers.forEach(function (fn) { try { (fn() || []).forEach(function (it) { self._items.push(it); }); } catch (e) {} });
      this._wrap.classList.add('show'); this._input.value = ''; this._render('');
      setTimeout(function () { self._input.focus(); }, 30);
    },
    close: function () { if (this._wrap) this._wrap.classList.remove('show'); var lf = this._lastFocus; if (lf && lf.focus) setTimeout(function () { try { lf.focus(); } catch (e) {} }, 0); },
    toggle: function () { if (this._wrap && this._wrap.classList.contains('show')) this.close(); else this.open(); },
    isOpen: function () { return this._wrap && this._wrap.classList.contains('show'); },
    _render: function (q) {
      q = (q || '').trim().toLowerCase();
      var items = this._items.filter(function (it) {
        if (!q) return true;
        return (it.title + ' ' + (it.subtitle || '') + ' ' + (it.keywords || '')).toLowerCase().indexOf(q) >= 0;
      });
      this._flat = items; this._active = 0;
      if (!items.length) { this._list.innerHTML = '<div class="empty">No matches</div>'; return; }
      var groups = {}, order = [];
      items.forEach(function (it) { var g = it.group || 'Actions'; if (!groups[g]) { groups[g] = []; order.push(g); } groups[g].push(it); });
      var html = '', idx = 0;
      order.forEach(function (g) {
        html += '<div class="grp">' + esc(g) + '</div>';
        groups[g].forEach(function (it) {
          html += '<div class="opt" role="option" data-i="' + idx + '"><span class="ic">' + (it.icon || '&#8226;') + '</span>'
            + '<span class="t"><b>' + esc(it.title) + '</b>' + (it.subtitle ? '<span>' + esc(it.subtitle) + '</span>' : '') + '</span></div>';
          idx++;
        });
      });
      this._list.innerHTML = html;
      var self = this;
      // re-map DOM order to flat order (groups preserve push order, so indices align)
      Array.prototype.forEach.call(this._list.querySelectorAll('.opt'), function (opt) {
        var i = +opt.getAttribute('data-i');
        opt.addEventListener('mousemove', function () { self._setActive(i); });
        opt.addEventListener('click', function () { self._run(i); });
      });
      this._setActive(0);
    },
    _setActive: function (i) {
      this._active = i;
      Array.prototype.forEach.call(this._list.querySelectorAll('.opt'), function (opt) {
        var on = +opt.getAttribute('data-i') === i; opt.classList.toggle('active', on); if (on) opt.scrollIntoView({ block: 'nearest' });
      });
    },
    _move: function (delta) { if (!this._flat.length) return; var n = (this._active + delta + this._flat.length) % this._flat.length; this._setActive(n); },
    _run: function (i) { var it = this._flat[i]; if (!it) return; this.close(); try { it.run && it.run(); } catch (e) { console.warn(e); } }
  };

  /* ---------- default palette providers (read app globals defensively) ---------- */
  function call(name, arg) { try { if (typeof w[name] === 'function') w[name](arg); } catch (e) {} }
  pal.register(function () {
    return [
      { group: 'Navigate', icon: '&#9638;', title: 'Daily Briefing', keywords: 'home cockpit', run: function () { call('menuNav', 'briefing'); } },
      { group: 'Navigate', icon: '&#9636;', title: 'Portfolio', keywords: 'queue accounts', run: function () { call('menuNav', 'portfolio'); } },
      { group: 'Navigate', icon: '&#9673;', title: 'Customer 360', run: function () { call('menuNav', 'customer'); } },
      { group: 'Navigate', icon: '&#10022;', title: 'RM Assist Journey', keywords: 'strategy ai steps', run: function () { call('menuNav', 'journey'); } },
      { group: 'Navigate', icon: '&#9655;', title: 'Demo Studio', keywords: 'guided demo video personas presenter', run: function () { call('menuNav', 'demo'); } },
      { group: 'Navigate', icon: '&#9097;', title: 'Audit Trail', keywords: 'glass box events', run: function () { call('toggleDrawer'); } }
    ];
  });
  pal.register(function () { // journey steps when available
    try {
      if (typeof RM_JOURNEY === 'undefined' || !w.__C) return [];
      return RM_JOURNEY.map(function (s, i) {
        return { group: 'Journey steps', icon: '&#10148;', title: 'Step ' + s.n + ' · ' + s.title, subtitle: s.level, keywords: s.key,
          run: function () { call('menuNav', 'journey'); setTimeout(function () { try { w.gotoStep(i); } catch (e) {} }, 30); } };
      });
    } catch (e) { return []; }
  });
  pal.register(function () { // customers from the live priority queue
    var out = [];
    try {
      Array.prototype.forEach.call(d.querySelectorAll('#queue .qcard'), function (card) {
        var cid = card.getAttribute('data-id'); if (!cid) return;
        var nm = (card.querySelector('b, h3, h4, .qc-name') || {}).textContent || cid;
        out.push({ group: 'Customers', icon: '&#9679;', title: nm.trim(), subtitle: cid, keywords: cid,
          run: function () { call('selectCustomer', cid); } });
      });
    } catch (e) {}
    return out;
  });
  pal.register(function () { // contextual actions
    var out = []; var c = w.__C;
    if (c && c.cid) {
      out.push({ group: 'Actions', icon: '&#9851;', title: 'Regenerate strategy', subtitle: c.cid, keywords: 'next best action refresh',
        run: function () { call('menuNav', 'journey'); setTimeout(function () { try { w.loadStrategy(c.cid); } catch (e) {} }, 40); } });
      out.push({ group: 'Actions', icon: '&#9658;', title: 'Start video call (Step 7)', subtitle: c.cid, keywords: 'teams call',
        run: function () { try { w.openVideoCall(c.cid); } catch (e) {} } });
      out.push({ group: 'Actions', icon: '&#9993;', title: 'Draft renewal memo', subtitle: c.cid, keywords: 'memo',
        run: function () { try { w.draftMemo(c.cid); } catch (e) {} } });
    }
    return out;
  });

  pal.register(function () { // live nudges streamed from the video-call app
    var out = [];
    try {
      var store = w.RXI;
      if (store && store.cache && store.cache.size) {
        var latest = store.latest;
        out.push({ group: 'Live call', icon: '&#128161;', title: 'Jump to the latest live nudge',
          subtitle: latest ? (latest.headline || latest.eventId) : 'nothing captured yet',
          keywords: 'nudge teams insight coaching live call',
          run: function () { call('openLatestNudge'); } });
        var seen = 0;
        for (var i = store.order.length - 1; i >= 0 && seen < 6; i--) {
          var e = store.cache.get(store.order[i]);
          if (!e) continue;
          seen++;
          (function (entry) {
            out.push({ group: 'Live call', icon: '&#9679;', title: entry.headline || entry.eventId,
              subtitle: (entry.kind || 'insight').replace(/_/g, ' ') + ' \u00b7 ' + (entry.customerName || entry.customerId || ''),
              keywords: (entry.kind || '') + ' ' + (entry.customerId || '') + ' ' + (entry.body || '').slice(0, 80),
              run: function () { try { w.openInsight(entry); } catch (e2) {} } });
          })(e);
        }
      }
    } catch (e) {}
    return out;
  });

  /* ---------- global keybindings ---------- */
  d.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); pal.toggle(); return; }
    if (e.key === 'Escape') { if (pal.isOpen()) pal.close(); else if (drawer.isOpen()) drawer.close(); }
  });

  /* ---------- staggered reveal helper (used by app.js renders) ---------- */
  RX.reveal = function (root, sel) {
    var scope = typeof root === 'string' ? d.querySelector(root) : (root || d);
    if (!scope) return;
    var els = scope.querySelectorAll(sel || '.rx-reveal');
    Array.prototype.forEach.call(els, function (el, i) { el.style.setProperty('--rx-d', (i * 70) + 'ms'); });
    requestAnimationFrame(function () { requestAnimationFrame(function () { Array.prototype.forEach.call(els, function (el) { el.classList.add('rx-in'); }); }); });
  };

  RX.ready = true;
})(window, document);
