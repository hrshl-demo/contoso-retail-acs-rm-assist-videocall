/* Customer self-service scheduling (Step 7). Plain vanilla; talks to the same
   Video Assist backend. On "Join" it hands off to the existing call app with the
   Teams link prefilled (?link=...), so the live video call code is untouched. */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  // Public path prefix, injected into schedule.html by server.js (sendHtml). Empty when
  // served at the root; '/video' behind Caddy. Never hard-code a leading-slash path below.
  var API_BASE = (window.__VA_BASE__ || '');
  var api = function (p) { return API_BASE + p; };
  var qs = new URLSearchParams(location.search);
  var CID = qs.get('customer_id') || '';
  var selected = null;       // {startIso, time, label}
  var booking = null;
  var pollTimer = null;
  var LS = 'va_booking_' + (CID || 'x');

  function fmtWhen(iso) {
    try { return new Date(iso).toLocaleString('en-IN', { weekday: 'long', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }); }
    catch (e) { return iso; }
  }
  function initials(name) { return (name || 'RM').split(/\s+/).map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase() || 'RM'; }

  /* ---------- availability ---------- */
  function loadAvailability() {
    fetch(api('/availability?days=2')).then(function (r) { return r.json(); }).then(function (data) {
      $('rmName').textContent = data.rm || 'Your Relationship Manager';
      $('rmAv').textContent = initials(data.rm);
      $('rmMeta').textContent = (data.source === 'rm-calendar' ? 'Live calendar availability' : 'Contoso Bank branch · next 2 working days');
      var host = $('avail'); host.innerHTML = '';
      var days = data.days || [];
      if (!days.length) { host.innerHTML = '<div class="muted" style="padding:14px 0">No open slots in the next two days — please call your branch.</div>'; return; }
      days.forEach(function (day) {
        var dh = document.createElement('div'); dh.className = 'day';
        dh.innerHTML = '<span>' + esc(day.label) + '</span>';
        host.appendChild(dh);
        var wrap = document.createElement('div'); wrap.className = 'slots';
        (day.slots || []).forEach(function (s) {
          var b = document.createElement('button'); b.className = 'slot'; b.type = 'button'; b.textContent = s.time;
          b.setAttribute('aria-pressed', 'false');
          if (!s.available) { b.disabled = true; b.title = 'Unavailable'; }
          else b.addEventListener('click', function () { pick(s, day, b); });
          wrap.appendChild(b);
        });
        host.appendChild(wrap);
      });
    }).catch(function (e) { $('avail').innerHTML = '<div class="muted" style="padding:14px 0">Could not load availability: ' + esc(e.message) + '</div>'; });
  }
  function pick(s, day, btn) {
    selected = { startIso: s.startIso, time: s.time, label: day.label };
    Array.prototype.forEach.call(document.querySelectorAll('.slot'), function (el) { el.setAttribute('aria-pressed', 'false'); });
    btn.setAttribute('aria-pressed', 'true');
    $('pickLine').textContent = 'Selected: ' + day.label + ' at ' + s.time;
    $('bookBtn').disabled = false; $('bookHint').textContent = 'We’ll notify your RM the moment you confirm.';
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); }

  /* ---------- booking ---------- */
  $('bookBtn').addEventListener('click', function () {
    if (!selected) return;
    $('bookBtn').disabled = true; $('bookHint').textContent = 'Booking…';
    fetch(api('/bookings'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ customerId: CID, name: $('name').value, contact: $('contact').value, note: $('note').value, slotIso: selected.startIso }) })
      .then(function (r) { return r.json(); }).then(function (b) {
        if (b.error) throw new Error(b.error);
        booking = b; try { localStorage.setItem(LS, b.id); } catch (e) {}
        showConfirm(); startPolling();
      }).catch(function (e) { $('bookBtn').disabled = false; $('bookHint').textContent = 'Could not book: ' + e.message; });
  });

  function showConfirm() {
    $('pick').classList.add('hide'); $('confirm').classList.remove('hide');
    $('cName').textContent = $('rmName').textContent || 'your RM';
    $('cWhen').textContent = fmtWhen(booking.slotIso);
    $('cId').textContent = booking.id;
    renderStatus();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  function renderStatus() {
    var s = $('cStatus');
    if (booking.meetingLink) {
      s.textContent = 'Ready to join'; s.className = 'status ready';
      $('cWait').classList.add('hide'); $('joinBtn').classList.remove('hide');
    } else {
      s.textContent = (booking.status === 'scheduled' ? 'Scheduled' : 'Requested'); s.className = 'status req';
      $('cWait').classList.remove('hide'); $('joinBtn').classList.add('hide');
    }
  }
  function poll() {
    if (!booking) return;
    fetch(api('/bookings/' + encodeURIComponent(booking.id))).then(function (r) { return r.json(); }).then(function (b) {
      if (b && b.id) { booking = b; renderStatus(); if (b.meetingLink) stopPolling(); }
    }).catch(function () {});
  }
  function startPolling() { stopPolling(); pollTimer = setInterval(poll, 6000); }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  /* ---------- join handoff (reuses the existing call app) ---------- */
  function joinWith(link) { if (!link) return; location.href = api('/?customer_id=' + encodeURIComponent(CID) + '&link=' + encodeURIComponent(link)); }
  $('joinBtn').addEventListener('click', function () { joinWith(booking && booking.meetingLink); });
  $('manualJoin').addEventListener('click', function () { joinWith($('manualLink').value.trim()); });
  $('refreshBtn').addEventListener('click', poll);
  $('newBtn').addEventListener('click', function () { try { localStorage.removeItem(LS); } catch (e) {} booking = null; stopPolling(); $('confirm').classList.add('hide'); $('pick').classList.remove('hide'); });

  /* ---------- boot ---------- */
  var existing = null; try { existing = localStorage.getItem(LS); } catch (e) {}
  if (existing) {
    fetch(api('/bookings/' + encodeURIComponent(existing))).then(function (r) { return r.ok ? r.json() : null; }).then(function (b) {
      if (b && b.id) { booking = b; showConfirm(); startPolling(); } else { try { localStorage.removeItem(LS); } catch (e) {} loadAvailability(); }
    }).catch(loadAvailability);
  } else { loadAvailability(); }
})();
