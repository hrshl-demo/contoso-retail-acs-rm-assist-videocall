/* Contoso mobile banking portal (customer side). Logged-in journey for Rakesh Sharma.
   One tap on "Video call your RM" schedules the call CALL_LEAD_SECONDS out; the RM's
   Teams meeting link is provisioned server-side and the customer never sees it. A
   countdown runs, then a "Join call" button hands off to the Video Assist call app
   using only the booking id (?booking=...). */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var qs = new URLSearchParams(location.search);
  var CID = qs.get('customer_id') || qs.get('customerId') || 'CTB-RTL-002';

  var call = null;            // { id, scheduledAt, leadSeconds }
  var tick = null, pollT = null;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); }
  function initials(name) { return (name || 'RM').split(/\s+/).map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase() || 'RM'; }

  /* ---------- icons ---------- */
  var ICON = {
    savings: '<svg viewBox="0 0 24 24"><path d="M4 11a6 5 0 0 1 12 0v3a2 2 0 0 1-2 2h-1v2h-2v-2H8v2H6v-2a2 2 0 0 1-2-2z"/><path d="M16 9h2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-1M8 8V6a2 2 0 0 1 4 0" fill="none"/><circle cx="7.5" cy="11" r="1" fill="currentColor" stroke="none"/></svg>',
    card: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18M7 15h4"/></svg>',
    loan: '<svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6z"/><path d="M9 8h4M9 12h6M9 16h6"/></svg>',
    pay: '<svg viewBox="0 0 24 24"><path d="M6 10h9M6 14h5"/><path d="M4 6h16v12H4z"/></svg>',
    transfer: '<svg viewBox="0 0 24 24"><path d="M4 8h13l-3-3M20 16H7l3 3"/></svg>',
    statements: '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M9 12h6M9 16h6M9 8h3"/></svg>',
    rewards: '<svg viewBox="0 0 24 24"><circle cx="12" cy="9" r="5"/><path d="M9 13l-2 8 5-3 5 3-2-8"/></svg>'
  };
  var QA_ICON = { 'Pay card': ICON.pay, 'Transfer': ICON.transfer, 'Statements': ICON.statements, 'Rewards': ICON.rewards };

  /* ---------- render profile ---------- */
  function render(p) {
    document.title = 'Contoso Bank · ' + (p.greetingName || 'Mobile');
    $('meName').textContent = p.greetingName || p.name || 'there';
    $('meAv').textContent = (p.greetingName || p.name || 'R').charAt(0).toUpperCase();
    $('meTier').textContent = p.tier || 'Contoso Banking';
    $('memberSince').textContent = p.memberSince || '';

    var first = (p.accounts && p.accounts[0]) || null;
    if (first) { $('heroAmt').textContent = first.primaryLabel; $('heroAcc').textContent = first.name + ' ' + (first.mask || ''); }

    var rm = p.rm || {};
    $('rmName').textContent = rm.name || 'Your Relationship Manager';
    $('rmSub').textContent = rm.title || 'Your Relationship Manager';
    $('rmBranch').textContent = rm.branch || '';
    $('rmAv').textContent = initials(rm.name);
    $('callName').textContent = rm.name || 'your RM';
    $('callAv').textContent = initials(rm.name);
    $('readyName').textContent = rm.name || 'Your RM';

    // quick actions
    $('quick').innerHTML = (p.quickActions || []).map(function (q) {
      return '<div class="qa"><span class="qi">' + (QA_ICON[q] || ICON.transfer) + '</span><span>' + esc(q) + '</span></div>';
    }).join('');

    // accounts
    $('accounts').innerHTML = (p.accounts || []).map(function (a) {
      var chip = a.alert ? '<div class="chip ' + (a.kind === 'card' ? 'bad' : 'warn') + '">' + esc(a.alert) + '</div>' : '';
      return '<div class="acct"><div class="ai ' + esc(a.kind) + '">' + (ICON[a.kind] || ICON.savings) + '</div>' +
        '<div class="am"><div class="an">' + esc(a.name) + '</div><div class="as">' + esc(a.mask || '') + ' · ' + esc(a.primarySub || '') + '</div></div>' +
        '<div class="av"><div class="avn">' + esc(a.primaryLabel) + '</div>' + chip + '</div></div>';
    }).join('');

    if (p.creditScore) {
      $('creditScore').textContent = p.creditScore;
      $('creditBand').textContent = p.creditBand || '';
      var pct = Math.max(8, Math.min(96, Math.round(((p.creditScore - 300) / 600) * 100)));
      var g = $('gauge'); g.style.background = 'conic-gradient(var(--amber) 0 ' + pct + '%,var(--track) ' + pct + '% 100%)';
      g.querySelector('span').textContent = p.creditScore;
    }
  }

  function loadProfile() {
    fetch('/me?customer_id=' + encodeURIComponent(CID))
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { /* baked defaults already in the HTML */ });
  }

  /* ---------- instant call ---------- */
  function openOverlay() { var o = $('overlay'); o.classList.remove('hide'); o.setAttribute('aria-hidden', 'false'); }
  function closeOverlay() {
    var o = $('overlay'); o.classList.add('hide'); o.setAttribute('aria-hidden', 'true');
    if (tick) { clearInterval(tick); tick = null; } if (pollT) { clearInterval(pollT); pollT = null; }
    $('phaseWait').classList.remove('hide'); $('phaseReady').classList.add('hide');
  }

  function fmt(ms) { var s = Math.max(0, Math.round(ms / 1000)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }

  function setStep(k) {
    var order = ['notify', 'room', 'ready'];
    var idx = order.indexOf(k);
    Array.prototype.forEach.call($('steps').querySelectorAll('li'), function (li) {
      var i = order.indexOf(li.getAttribute('data-k'));
      li.className = i < idx ? 'done' : (i === idx ? 'doing' : '');
    });
  }
  function allStepsDone() { Array.prototype.forEach.call($('steps').querySelectorAll('li'), function (li) { li.className = 'done'; }); }

  function startCall() {
    if (call) { openOverlay(); return; }
    openOverlay();
    $('phaseWait').classList.remove('hide'); $('phaseReady').classList.add('hide');
    $('ringTime').textContent = '…'; setStep('notify');
    fetch('/call/request', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ customerId: CID }) })
      .then(function (r) { return r.json(); })
      .then(function (c) {
        if (!c || !c.id) throw new Error('no call');
        call = c; runCountdown();
        pollT = setInterval(pollStatus, 3000);
      })
      .catch(function () { $('ringTime').textContent = '!'; $('steps').innerHTML = '<li class="doing" style="color:var(--danger)">Could not reach the call service — please retry.</li>'; });
  }

  function runCountdown() {
    var total = (call.leadSeconds || 60) * 1000;
    var target = new Date(call.scheduledAt).getTime();
    function frame() {
      var rem = target - Date.now();
      if (rem <= 0) { $('ringTime').textContent = '0:00'; $('ring').style.setProperty('--pct', '100%'); allStepsDone(); showReady(); return; }
      $('ringTime').textContent = fmt(rem);
      var pct = Math.min(100, Math.round(((total - rem) / total) * 100));
      $('ring').style.setProperty('--pct', pct + '%');
      if (pct < 40) setStep('notify'); else if (pct < 80) setStep('room'); else setStep('ready');
    }
    frame();
    if (tick) clearInterval(tick);
    tick = setInterval(frame, 1000);
  }

  function pollStatus() {
    if (!call) return;
    fetch('/call/' + encodeURIComponent(call.id)).then(function (r) { return r.json(); }).then(function (s) {
      if (s && s.joinReady) showReady();
    }).catch(function () {});
  }

  function showReady() {
    if (tick) { clearInterval(tick); tick = null; }
    if (pollT) { clearInterval(pollT); pollT = null; }
    $('phaseWait').classList.add('hide'); $('phaseReady').classList.remove('hide');
  }

  function join() {
    if (!call) return;
    location.href = '/?booking=' + encodeURIComponent(call.id) + '&customer_id=' + encodeURIComponent(CID);
  }

  /* ---------- wire ---------- */
  $('callBtn').addEventListener('click', startCall);
  $('tabCall').addEventListener('click', startCall);
  $('joinBtn').addEventListener('click', join);
  $('closeBtn').addEventListener('click', closeOverlay);
  $('cancelBtn').addEventListener('click', function () { call = null; closeOverlay(); });

  loadProfile();
})();
