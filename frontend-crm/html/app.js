/* app.js — Contoso MSME RM Assist premium cockpit logic */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
const fmtINR = (n) => {
  n = Number(n) || 0;
  if (Math.abs(n) >= 1e7) return "₹" + (n / 1e7).toFixed(2) + " Cr";
  if (Math.abs(n) >= 1e5) return "₹" + (n / 1e5).toFixed(2) + " L";
  return "₹" + n.toLocaleString("en-IN");
};
const short = (s, n = 92) => (String(s || "").length > n ? String(s).slice(0, n - 1) + "…" : String(s || ""));

async function api(path, opts = {}) {
  const r = await fetch(TOOLAPI_URL + path, {
    ...opts,
    headers: { "Authorization": "Bearer " + BEARER, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!r.ok) throw new Error(path + " -> " + r.status + " " + await r.text().catch(()=>""));
  return r.json();
}
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2600); }
function toggleDrawer() { $("drawer").classList.toggle("open"); refreshAudit(); }
let CURRENT = null;
let LAST_DOSSIER = null;
let QUEUE_CACHE = [];          // full queue, for client-side filter/search
let QUEUE_FILTER = "all";      // all | risk | growth | renewal
let QUEUE_SEARCH = "";

/* ---------- top menubar wiring ---------- */
function setMenu(active){
  ["briefing","journeys","rawdata","portfolio","customer","journey","demo","audit"].forEach(k=>{
    const el = $("mbi-"+k); if(el) el.classList.toggle("active", k===active);
  });
}
function setCrumb(label){ const el=$("crumbCurrent"); if(el) el.textContent = label; }
function menuNav(which){
  if(which==='briefing'){ setMenu('briefing'); setCrumb('Daily Briefing'); loadBriefing(false); }
  else if(which==='journeys'){ setMenu('journeys'); setCrumb('Journeys'); renderJourneySelection(); }
  else if(which==='rawdata'){ setMenu('rawdata'); setCrumb('Raw Data'); renderRawData(); }
  // legacy targets retained for backward-compatibility (no menubar buttons in the lean build)
  else if(which==='portfolio'){ setMenu('journeys'); setCrumb('Journeys'); renderJourneySelection(); }
  else if(which==='customer'){ setMenu('journeys'); if(CURRENT){ setCrumb('Customer 360'); selectCustomer(CURRENT); } else { renderJourneySelection(); } }
  else if(which==='journey'){ setMenu('journeys'); setCrumb('RM Assist Journey'); loadJourneyFlow(); }
}

/* ---------- lean demo flow: the Rakesh Sharma RM Assist journey ---------- */
const DEMO_JOURNEYS = [
  { cid:"CTB-RTL-002", tone:"assist", tag:"Everyday RM Assist", name:"Rakesh Sharma",
    title:"Full RM Assist Journey", icon:"✦",
    blurb:"The complete Customer 360: relationship dossier, early-warning signals, next-best-action with eligibility gates and customer-safe conversation coaching — ending in a live, AI-coached video call.",
    proof:"Service recovery and grounded, guardrailed recommendations." },
];
function renderJourneySelection(){
  setMenu('journeys'); setCrumb('Journeys');
  const cards = DEMO_JOURNEYS.map((j,i)=>`
    <button class="lj-card tone-${j.tone}" style="animation-delay:${i*0.06}s" onclick="selectCustomer('${j.cid}')">
      <div class="lj-top"><span class="lj-ic">${j.icon}</span><span class="lj-num">0${i+1}</span></div>
      <span class="lj-tag">${esc(j.tag)}</span>
      <h3>${esc(j.title)}</h3>
      <div class="lj-name">${esc(j.name)} · ${esc(j.cid)}</div>
      <p>${esc(j.blurb)}</p>
      <footer><span>${esc(j.proof)}</span><b>Open journey →</b></footer>
    </button>`).join("");
  $("content").innerHTML = `
    <section class="lj-hero">
      <div class="eyebrow">Retail RM Assist · guided demo</div>
      <h2>Rakesh Sharma — RM Assist journey</h2>
      <p>The complete operational workspace for one everyday relationship-manager journey — not a summary. Start from the Daily Briefing, then step into the full Customer 360 and the live, AI-coached video call.</p>
    </section>
    <div class="lj-grid">${cards}</div>`;
  window.scrollTo({top:0,behavior:'smooth'});
}

/* ---------- lightweight inline SVG charts (no external library) ---------- */
function lcColor(i){ return ['#287c78','#245abb','#a87531','#a94a43','#3c8065','#6b3fa0','#8a5c18','#17324f'][i%8]; }
function lrMoney(n){ n=+n||0; if(Math.abs(n)>=1e7) return '₹'+(n/1e7).toFixed(2)+' Cr'; if(Math.abs(n)>=1e5) return '₹'+(n/1e5).toFixed(1)+' L'; return '₹'+Math.round(n).toLocaleString('en-IN'); }
function svgDonut(segs,{size=140,thick=20,center='',sub=''}={}){
  const total=segs.reduce((s,x)=>s+(+x.value||0),0)||1, r=(size-thick)/2, c=size/2, C=2*Math.PI*r; let off=0;
  const arcs=segs.filter(s=>+s.value>0).map(s=>{const len=(+s.value/total)*C, el=`<circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="${s.color}" stroke-width="${thick}" stroke-dasharray="${len.toFixed(2)} ${(C-len).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 ${c} ${c})"/>`;off+=len;return el;}).join('');
  return `<svg viewBox="0 0 ${size} ${size}" class="lc">${`<circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="#eef2f7" stroke-width="${thick}"/>`}${arcs}${center?`<text x="${c}" y="${c-2}" text-anchor="middle" class="lc-donut-b">${esc(center)}</text>`:''}${sub?`<text x="${c}" y="${c+14}" text-anchor="middle" class="lc-donut-s">${esc(sub)}</text>`:''}</svg>`;
}
function svgBars(data,{w=340,h=160,fmt=(v)=>v}={}){
  if(!data.length) return '<div class="lc-empty">No data</div>';
  const max=Math.max(1,...data.map(d=>+d.value||0)), n=data.length, bw=Math.max(12,Math.min(46,(w-24)/n-10)), gap=((w-24)-bw*n)/(n+1);
  return `<svg viewBox="0 0 ${w} ${h}" class="lc">${data.map((d,i)=>{const bh=Math.round((+d.value||0)/max*(h-44)),x=12+gap+i*(bw+gap),y=h-24-bh;return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1,bh)}" rx="4" fill="${d.color||lcColor(i)}"/><text x="${(x+bw/2).toFixed(1)}" y="${h-9}" text-anchor="middle" class="lc-x">${esc(d.label)}</text><text x="${(x+bw/2).toFixed(1)}" y="${(y-5).toFixed(1)}" text-anchor="middle" class="lc-v">${esc(fmt(d.value))}</text>`;}).join('')}</svg>`;
}
function svgGrouped(rows,{w=460,h=180,ca='#245abb',cb='#c3d2ea'}={}){
  if(!rows.length) return '<div class="lc-empty">No data</div>';
  const max=Math.max(1,...rows.flatMap(r=>[+r.a||0,+r.b||0])), n=rows.length, gw=(w-24)/n, bw=Math.min(18,gw/3);
  return `<svg viewBox="0 0 ${w} ${h}" class="lc">${rows.map((r,i)=>{const gx=12+i*gw+gw/2, ha=Math.round((+r.a||0)/max*(h-46)), hb=Math.round((+r.b||0)/max*(h-46));return `<rect x="${(gx-bw-2).toFixed(1)}" y="${(h-26-ha).toFixed(1)}" width="${bw}" height="${Math.max(1,ha)}" rx="3" fill="${ca}"/><rect x="${(gx+2).toFixed(1)}" y="${(h-26-hb).toFixed(1)}" width="${bw}" height="${Math.max(1,hb)}" rx="3" fill="${cb}"/><text x="${gx.toFixed(1)}" y="${h-9}" text-anchor="middle" class="lc-x">${esc(r.label)}</text>`;}).join('')}</svg>`;
}
function svgLine(pts,{w=580,h=200,color='#287c78',fmt=(v)=>v}={}){
  if(!pts.length) return '<div class="lc-empty">No series data available</div>';
  const ys=pts.map(p=>+p.value||0), max=Math.max(...ys), min=Math.min(...ys), span=Math.max(1,max-min);
  const px=(i)=>18+i/Math.max(1,pts.length-1)*(w-36), py=(v)=>h-26-((v-min)/span)*(h-54);
  const line=pts.map((p,i)=>`${i?'L':'M'}${px(i).toFixed(1)} ${py(+p.value||0).toFixed(1)}`).join(' ');
  const area=`${line} L ${px(pts.length-1).toFixed(1)} ${(h-26).toFixed(1)} L ${px(0).toFixed(1)} ${(h-26).toFixed(1)} Z`;
  const last=pts[pts.length-1];
  return `<svg viewBox="0 0 ${w} ${h}" class="lc"><path d="${area}" fill="${color}1f"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2.5"/><circle cx="${px(pts.length-1).toFixed(1)}" cy="${py(+last.value||0).toFixed(1)}" r="4" fill="${color}"/><text x="18" y="${h-8}" class="lc-x">${esc(pts[0].label||'')}</text><text x="${w-18}" y="${h-8}" text-anchor="end" class="lc-x">${esc(last.label||'')}</text><text x="${w-18}" y="15" text-anchor="end" class="lc-v">${esc(fmt(last.value))}</text></svg>`;
}
/* ---------- queue filter + search (sliders/filters that re-query the view) ---------- */
function bucketKey(b){ return (b==="Risk Watch" || b==="Customer Intervention") ? "risk" : b==="Growth" ? "growth" : "renewal"; }
function setQueueFilter(f, btn){
  QUEUE_FILTER = f;
  document.querySelectorAll(".qf-btn").forEach(b=>b.classList.toggle("on", b===btn));
  renderQueue();
}
function filterQueueBySearch(v){ QUEUE_SEARCH = String(v||"").toLowerCase().trim(); renderQueue(); }

/* ---------- animated number counter ---------- */
function animateCounters(scope){
  (scope||document).querySelectorAll("[data-count]").forEach(el=>{
    const target = parseFloat(el.getAttribute("data-count")); if(isNaN(target)) return;
    const suffix = el.getAttribute("data-suffix")||""; const prefix = el.getAttribute("data-prefix")||"";
    const dec = parseInt(el.getAttribute("data-dec")||"0",10);
    const dur = 760; const t0 = performance.now();
    const step = (t)=>{ const k = Math.min(1,(t-t0)/dur); const e = 1-Math.pow(1-k,3);
      el.textContent = prefix + (target*e).toLocaleString("en-IN",{minimumFractionDigits:dec,maximumFractionDigits:dec}) + suffix;
      if(k<1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  });
  // fill progress rings (deferred so the CSS transition runs from 0)
  (scope||document).querySelectorAll("[data-dash]").forEach(el=>{
    const target = el.getAttribute("data-dash");
    requestAnimationFrame(()=>requestAnimationFrame(()=>{ el.style.strokeDasharray = target; }));
  });
  // fill horizontal gauge bars
  (scope||document).querySelectorAll("[data-w]").forEach(el=>{
    const target = el.getAttribute("data-w");
    requestAnimationFrame(()=>requestAnimationFrame(()=>{ el.style.width = target; }));
  });
}

/* ---------- skeleton loaders ---------- */
function skelCards(n){ let s=""; for(let i=0;i<(n||3);i++){ s+=`<div class="skel-card"><div class="skel skel-line" style="width:42%"></div><div class="skel skel-line" style="width:88%"></div><div class="skel skel-line" style="width:70%"></div></div>`; } return s; }
function skelKpis(){ return `<div class="skel-row">${Array(4).fill('<div class="skel skel-card" style="height:78px"></div>').join("")}</div>`; }

/* Skeleton for a tool-API fetch that previously rendered a plain text
   `<div class="loading">`. Same wording, but the shape of what is coming is
   visible while it loads. */
function skelPanel(label, lines){
  const n = lines || 3;
  let rows = '';
  for(let i=0;i<n;i++){
    const w = [92, 78, 64, 84, 70][i % 5];
    rows += `<div class="skel skel-line" style="width:${w}%"></div>`;
  }
  return `<div class="rx-skel-panel" role="status" aria-live="polite">
    <div class="rx-skel-head"><span class="rx-spin" aria-hidden="true"></span><span>${esc(label||'Loading…')}</span></div>
    <div class="rx-skel-body">${rows}</div></div>`;
}
function skelInto(el, label, lines){
  const node = typeof el === 'string' ? $(el) : el;
  if(node) node.innerHTML = skelPanel(label, lines);
}

/* ---------- token-by-token reveal for AI narration ----------
   The text is already fully in hand; revealing it word-by-word makes the
   grounded narration read as generated rather than pasted. Falls back to an
   instant set when the user prefers reduced motion. */
const RX_REDUCED_MOTION = (function(){
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch(e){ return false; }
})();
function rxStreamText(el, text, opts){
  const node = typeof el === 'string' ? $(el) : el;
  if(!node) return Promise.resolve();
  const full = String(text||'');
  opts = opts || {};
  if(RX_REDUCED_MOTION || !full){ node.textContent = full; return Promise.resolve(); }
  if(node.__rxStreamStop){ node.__rxStreamStop(); }
  const tokens = full.split(/(\s+)/);
  const perTick = Math.max(1, Math.ceil(tokens.length / Math.max(8, Math.min(90, opts.ticks || 46))));
  node.textContent = '';
  node.classList.add('rx-streaming');
  let i = 0, timer = 0, done = false;
  return new Promise((resolve)=>{
    const stop = () => { if(done) return; done = true; clearInterval(timer); node.classList.remove('rx-streaming'); node.textContent = full; resolve(); };
    node.__rxStreamStop = stop;
    timer = setInterval(()=>{
      if(i >= tokens.length){ stop(); return; }
      node.textContent += tokens.slice(i, i+perTick).join('');
      i += perTick;
    }, opts.intervalMs || 26);
  });
}
// Stream every [data-rx-stream] node inside a freshly rendered scope.
function rxStreamScope(scope){
  const root = typeof scope === 'string' ? $(scope) : (scope || document);
  if(!root || !root.querySelectorAll) return;
  root.querySelectorAll('[data-rx-stream]').forEach((el, idx)=>{
    const text = el.getAttribute('data-rx-stream');
    if(text == null) return;
    el.removeAttribute('data-rx-stream');
    setTimeout(()=>rxStreamText(el, text), idx * 120);
  });
}

/* ---------- live-nudge cue (sound + Notification) ----------
   Driven by the Phase 2 SSE channel. Only fires when the cockpit tab is NOT
   focused, so it never interrupts an RM who is already looking at the screen. */
const RX_CUE = { enabled:true, ctx:null, lastAt:0 };
function rxCueSound(){
  if(!RX_CUE.enabled || RX_REDUCED_MOTION) return;
  try{
    const AC = window.AudioContext || window.webkitAudioContext;
    if(!AC) return;
    const ctx = RX_CUE.ctx || (RX_CUE.ctx = new AC());
    if(ctx.state === 'suspended') ctx.resume().catch(()=>{});
    // Two short sine blips — synthesised, so there is no audio asset to ship.
    [0, 0.13].forEach((offset, i)=>{
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = i === 0 ? 784 : 1046;   // G5 -> C6
      const t0 = ctx.currentTime + offset;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.09, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.11);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(t0); osc.stop(t0 + 0.13);
    });
  }catch(e){}
}
function rxRequestNotifyPermission(){
  try{
    if(typeof Notification === 'undefined' || Notification.permission !== 'default') return;
    Notification.requestPermission().catch(()=>{});
  }catch(e){}
}
// Called by RXI.put() for every insight that arrives on the SSE channel.
window.rxNotifyInsight = function(entry){
  if(!entry || entry.kind !== 'live_nudge') return;
  const now = Date.now();
  if(now - RX_CUE.lastAt < 1500) return;      // de-dupe replay bursts
  RX_CUE.lastAt = now;
  if(!document.hidden) return;                // RM is already watching
  rxCueSound();
  try{
    if(typeof Notification !== 'undefined' && Notification.permission === 'granted'){
      const n = new Notification(entry.headline || 'Live nudge', {
        body: (entry.body || '').slice(0, 180),
        tag: entry.eventId, renotify: false,
      });
      n.onclick = ()=>{ try{ window.focus(); openInsight(entry); n.close(); }catch(e){} };
    }
  }catch(e){}
};
// Jump straight to the newest live nudge (command palette + Notification click).
function openLatestNudge(){
  const latest = RXI.latest || RXI.cache.get([...RXI.order].reverse().find(id=>{
    const e = RXI.cache.get(id); return e && e.kind === 'live_nudge';
  }));
  if(latest) return openInsight(latest);
  RX.toast('No live nudge captured yet in this session.');
}

/* ============================================================================
   LIVE INSIGHT DEEP LINK — Teams nudge → this cockpit's detail drawer.

   A Teams card carries ?customer=<id>&focus=<eventId>&kind=<kind>. On boot we
   select that customer through the EXISTING selectCustomer() path and open the
   insight in the EXISTING RX.drawer, using the same {title, subtitle, badges,
   sections, actions} shape as openStrategyPlay(). No new UI surface.

   The SSE channel means an already-open cockpit tab has cached the payload
   before the RM can physically click the link, so the drawer is instant. A cold
   tab falls back to a direct fetch.
   ============================================================================ */
const RXI = {
  cache: new Map(),        // eventId -> insight
  order: [],               // eventIds, oldest first
  latest: null,            // most recent live_nudge (for the palette jump)
  _es: null, _customerId: null,

  base(){ return (typeof VIDEOASSIST_URL !== 'undefined' ? VIDEOASSIST_URL : '').replace(/\/+$/, ''); },
  ready(){ const b = this.base(); return !!b && !b.includes('invalid.local'); },

  put(entry){
    if(!entry || !entry.eventId) return;
    if(!this.cache.has(entry.eventId)) this.order.push(entry.eventId);
    this.cache.set(entry.eventId, entry);
    while(this.order.length > 200){ this.cache.delete(this.order.shift()); }
    if(entry.kind === 'live_nudge') this.latest = entry;
    try{ onLiveInsight(entry); }catch(e){}
  },

  /* Pre-cache insights as they happen. Auto-reconnects via EventSource, and we
     re-open on hard errors so a container restart doesn't silently kill it. */
  connect(cid){
    if(!this.ready() || typeof EventSource === 'undefined') return;
    if(this._es && this._customerId === cid) return;
    this.disconnect();
    this._customerId = cid || null;
    const url = this.base() + '/insights/stream' + (cid ? `?customer_id=${encodeURIComponent(cid)}` : '');
    try{
      const es = this._es = new EventSource(url);
      es.addEventListener('insight', (e)=>{ try{ RXI.put(JSON.parse(e.data)); }catch(_){} });
      es.addEventListener('ready', ()=> console.debug('[insights] stream ready'));
      es.onerror = ()=>{
        // EventSource retries on its own; only intervene when it gives up.
        if(es.readyState === 2){ setTimeout(()=>{ if(RXI._es === es){ RXI._es = null; RXI.connect(RXI._customerId); } }, 4000); }
      };
    }catch(e){ console.debug('[insights] stream unavailable:', e.message); }
  },
  disconnect(){ try{ this._es && this._es.close(); }catch(e){} this._es = null; },

  async resolve(eventId){
    if(!eventId) return null;
    if(this.cache.has(eventId)) return this.cache.get(eventId);
    if(!this.ready()) return null;
    const r = await fetch(`${this.base()}/insights/${encodeURIComponent(eventId)}`);
    if(!r.ok) throw new Error(r.status === 404 ? 'This insight is no longer in the live call buffer.' : `insight lookup failed (${r.status})`);
    const entry = await r.json();
    this.put(entry);
    return entry;
  },
};
// `const` does not attach to window, and ui.js's command palette reads it defensively.
window.RXI = RXI;

const RXI_KIND_LABEL = { live_nudge:'Live nudge', answer:'Grounded answer', synopsis:'Pre-call synopsis', case_logged:'CRM case registered' };

function rxiRuntimeRows(r){
  r = r || {};
  const rows = [
    ['Tool', r.tool || '—'],
    ['Records scanned', r.rows_scanned != null ? Number(r.rows_scanned).toLocaleString('en-IN') : '—'],
    ['AI latency', r.latency_ms != null ? `${r.latency_ms} ms` : '—'],
    ['End to end', r.end_to_end_ms != null ? `${r.end_to_end_ms} ms to Teams` : '—'],
    ['Confidence', r.confidence != null ? `${Math.round(r.confidence*100)}%` : '—'],
    ['Model', r.model || '—'],
  ];
  return `<dl class="rx-kv">${rows.map(x=>`<dt>${esc(x[0])}</dt><dd>${esc(x[1])}</dd>`).join('')}</dl>`;
}

// Render one insight into the existing contextual drawer.
function openInsight(entry, opts = {}){
  if(!entry) return;
  const kindLabel = RXI_KIND_LABEL[entry.kind] || 'Live AI insight';
  const conf = entry.runtime && entry.runtime.confidence != null ? Math.round(entry.runtime.confidence*100) : null;
  const badges = [
    RX.badge(kindLabel, entry.kind==='live_nudge' ? 'warn' : (entry.kind==='case_logged' ? 'pos' : 'accent'), '&#9679;'),
    conf != null ? RX.confidence(conf) : '',
    entry.consent && entry.consent.status ? RX.badge(String(entry.consent.status).replace(/_/g,' '), entry.consent.status==='confirmed_on_later_turn'?'pos':'', '&#128737;') : '',
  ].filter(Boolean);

  const sections = [];
  if(entry.trigger) sections.push({label:'What the customer said', html:`<div class="rx-block why">&ldquo;${esc(entry.trigger)}&rdquo;</div>`});
  if(entry.body) sections.push({label:'The insight', html:`<div class="rx-block why" data-rx-stream="${esc(entry.body)}"></div>`});
  if(entry.say) sections.push({label:'Say to the customer', html:`<div class="rx-block say">&ldquo;${esc(entry.say)}&rdquo;</div>`});
  if(entry.basis) sections.push({label:'Policy basis (internal)', html:`<div class="rx-block basis">${esc(entry.basis)}</div>`});
  if(entry.extra && entry.extra.risks && entry.extra.risks.length) sections.push({label:'Risks & issues', html:`<div class="rx-block dont">${entry.extra.risks.map(esc).join('<br>')}</div>`});
  if(entry.extra && entry.extra.crossSell && entry.extra.crossSell.length) sections.push({label:'Eligible actions', html:`<div class="rx-block say">${entry.extra.crossSell.map(esc).join('<br>')}</div>`});
  if(entry.extra && entry.extra.caseRef) sections.push({label:'Case record', html:`<dl class="rx-kv">
    <dt>Reference</dt><dd>${esc(entry.extra.caseRef)}</dd>
    <dt>Category</dt><dd>${esc(entry.extra.category||'—')}</dd>
    <dt>Priority</dt><dd>${esc(entry.extra.priority||'—')}</dd>
    <dt>Next step</dt><dd>${esc(entry.extra.commitments_by_bank||'—')}${entry.extra.next_follow_up_date?` (by ${esc(entry.extra.next_follow_up_date)})`:''}</dd></dl>`});
  sections.push({label:'AI runtime trace', html: rxiRuntimeRows(entry.runtime)});
  if((entry.sources||[]).length) sections.push({label:'Source refs', html:`<div>${entry.sources.map(s=>RX.badge(s,'','&#8226;')).join(' ')}</div>`});
  if(entry.consent && entry.consent.utterance) sections.push({label:'Consent evidence', html:`<div class="rx-block basis">${esc(entry.consent.utterance)}</div>`});
  sections.push({label:'Event', html:`<dl class="rx-kv">
    <dt>Event id</dt><dd>${esc(entry.eventId)}</dd>
    <dt>Session</dt><dd>${esc(entry.sessionId||'—')}</dd>
    <dt>Captured</dt><dd>${esc(entry.timestamp||'—')}</dd></dl>`});

  const cid = entry.customerId || CURRENT;
  const actions = [
    { label:'Leaf evidence', icon:'&#9636;', onClick:()=>drillInsightEvidence(entry) },
    { label:'Policy basis', icon:'&#9997;', onClick:()=>drillInsightPolicy(entry) },
    { label:'Copy talk-track', icon:'&#9112;', onClick:()=>RX.copy(entry.say || entry.body || '', null) },
    { label:'Open Customer 360', kind:'primary', icon:'&#9673;', onClick:(drw)=>{ drw.close(); if(cid) selectCustomer(cid); } },
  ];
  if(opts.back) actions.unshift({ label:'Back', icon:'&#8592;', onClick:()=>openInsight(entry) });

  RX.drawer.open({
    title: entry.headline || kindLabel,
    subtitle: `${entry.customerName || cid || ''}${entry.turnId ? ` · turn ${entry.turnId}` : ''}`,
    badges, sections, actions,
  });
  // Reveal the narration token-by-token so the drawer reads as generated.
  try{ rxStreamScope(RX.drawer._el); }catch(e){}
}

// Drill to the leaf rows the insight was computed from — reuses the Tool API
// raw-facts endpoint that Core CRM already calls, no new backend surface.
async function drillInsightEvidence(entry){
  const cid = entry.customerId || CURRENT;
  RX.drawer.open({ title:'Leaf evidence', subtitle:cid||'', sections:[{label:'Reading the record', html:RX.skeleton(3)}] });
  try{
    const ev = await api(`/v1/customers/${cid}/raw-facts`);
    const st = ev.stress || {}, f = ev.facility || {};
    const line = (k,v)=>`<div class="rf-line"><span>${esc(k)}</span><b>${esc(v==null||v===''?'—':v)}</b></div>`;
    const threads = (st.open_threads||[]).map(t=>`<div class="rf-line"><span>${esc(t.topic)}</span><b>${esc(t.status||'open')}</b></div>`).join('') || '<div class="rf-line"><span>None on file</span><b>—</b></div>';
    const sections = [
      {label:'Facility & utilisation', html:`<div class="rf-block">${line('Facility', f.type||f.facility_type)}${line('Sanction limit', f.sanction_limit_inr!=null?fmtINR(f.sanction_limit_inr):'')}${line('Avg utilisation', f.avg_utilization_pct!=null?f.avg_utilization_pct+'%':'')}${line('Peak utilisation', f.peak_utilization_pct!=null?f.peak_utilization_pct+'%':'')}</div>`},
      {label:'Conduct & stress signals', html:`<div class="rf-block">${line('EMI bounces', st.emi_bounces)}${line('Cheque returns', st.cheque_returns)}${line('Classification', st.classification||st.sma_status)}${line('Bureau score', ev.bureau_score||ev.cibil)}</div>`},
      {label:'Open threads', html:`<div class="rf-block">${threads}</div>`},
      {label:'Why this matters here', html:`<div class="rx-block why">${esc(entry.body||entry.headline||'')}</div>`},
    ];
    RX.drawer.open({
      title:'Leaf evidence', subtitle:`${entry.customerName||cid} · raw facts on file`,
      badges:[RX.badge('system of record','', '&#9636;'), entry.runtime && entry.runtime.rows_scanned!=null ? RX.badge(`${Number(entry.runtime.rows_scanned).toLocaleString('en-IN')} rows scanned`,'accent') : ''].filter(Boolean),
      sections,
      actions:[{label:'Back to insight', icon:'&#8592;', kind:'primary', onClick:()=>openInsight(entry)}],
    });
  }catch(e){
    RX.drawer.open({ title:'Leaf evidence', subtitle:cid||'',
      sections:[{label:'Unavailable', html:`<div class="rx-error"><div class="ic">&#9888;</div>${esc(e.message)}</div>`}],
      actions:[{label:'Back to insight', icon:'&#8592;', kind:'primary', onClick:()=>openInsight(entry)}] });
  }
}

// Drill to the SOP clauses behind the insight — reuses the existing RAG endpoint.
async function drillInsightPolicy(entry){
  const q = entry.basis || entry.body || entry.headline || '';
  RX.drawer.open({ title:'Policy basis', subtitle:'Retrieving SOP clauses…', sections:[{label:'Searching indexed SOPs', html:RX.skeleton(2)}] });
  try{
    const r = await api('/v1/rag/retrieve', { method:'POST', body: JSON.stringify({ query:q, top_k:3 }) });
    const rows = (r.results||[]);
    const html = rows.length
      ? rows.map(x=>`<div class="rx-block basis" style="margin-bottom:8px"><b>${esc(x.sop_id)} · ${esc(x.sop_title||'')}</b><br>${esc(x.section_title||'')}<br><span style="font-weight:400">${esc(String(x.content||'').replace(/^#+.*$/m,'').slice(0,320))}…</span></div>`).join('')
      : '<div class="rx-empty"><div class="ic">&#9675;</div>No matching Contoso policy found in the indexed SOPs.</div>';
    RX.drawer.open({
      title:'Policy basis', subtitle:`${rows.length} clause(s) · grounded retrieval`,
      badges:[RX.badge(r.grounded?'grounded':'ungrounded', r.grounded?'pos':'warn','&#9997;')],
      sections:[{label:'Query', html:`<div class="rx-block why">${esc(q)}</div>`},{label:'Matched SOP clauses', html}],
      actions:[{label:'Back to insight', icon:'&#8592;', kind:'primary', onClick:()=>openInsight(entry)}],
    });
  }catch(e){
    RX.drawer.open({ title:'Policy basis', subtitle:'',
      sections:[{label:'Unavailable', html:`<div class="rx-error"><div class="ic">&#9888;</div>${esc(e.message)}</div>`}],
      actions:[{label:'Back to insight', icon:'&#8592;', kind:'primary', onClick:()=>openInsight(entry)}] });
  }
}

// Hook for Phase 4 (sound + Notification cue). Defined defensively so the
// deep-link path works even if the polish layer is not loaded.
function onLiveInsight(entry){ if(typeof window.rxNotifyInsight === 'function') window.rxNotifyInsight(entry); }

async function focusInsight(eventId, kind){
  if(!eventId) return;
  try{
    const entry = await RXI.resolve(eventId);
    if(entry) return openInsight(entry);
    RX.drawer.open({ title:'Insight unavailable', subtitle:eventId,
      sections:[{label:'Not in the live buffer', html:`<div class="rx-empty"><div class="ic">&#9675;</div>The video-call app is not reachable from this cockpit, so this ${esc(RXI_KIND_LABEL[kind]||'insight')} could not be loaded.</div>`}] });
  }catch(e){
    RX.drawer.open({ title:'Insight unavailable', subtitle:eventId,
      sections:[{label:'Lookup failed', html:`<div class="rx-error"><div class="ic">&#9888;</div>${esc(e.message)}</div>`}] });
  }
}

// Parse the Teams deep link. Returns nulls for a normal cockpit visit.
function insightRoute(){
  try{
    const q = new URLSearchParams(window.location.search);
    return { customer:q.get('customer')||q.get('customer_id'), focus:q.get('focus'), kind:q.get('kind') };
  }catch(e){ return { customer:null, focus:null, kind:null }; }
}

async function boot() {
  try {
    await fetch(TOOLAPI_URL + "/healthz").then(r => r.json());
    $("statusDot").classList.remove("off"); $("statusText").textContent = "tool API connected";
  } catch {
    $("statusDot").classList.add("off"); $("statusText").textContent = "tool API unreachable";
  }
  await loadQueue();
  const route = insightRoute();
  // The Notification API needs a user gesture before it will prompt, so we ask
  // on the RM's first interaction rather than on load.
  ['pointerdown','keydown'].forEach(ev=>document.addEventListener(ev, rxRequestNotifyPermission, { once:true }));
  // Warm the insight cache for this customer (or all customers) before the RM clicks.
  RXI.connect(route.customer || null);
  if(route.customer){
    await selectCustomer(route.customer);
    if(route.focus) await focusInsight(route.focus, route.kind);
    return;
  }
  await loadBriefing();
}

let DAILY_STORY_STATE = { stage:1, unlocked:1, results:{}, focusCustomerId:null };
let RELATIONSHIP_STORY_STATE = {};

async function loadBriefing(reset=true) {
  setMenu('briefing'); setCrumb('Daily Briefing');
  if(reset) DAILY_STORY_STATE = { stage:1, unlocked:1, results:{}, focusCustomerId:null };
  renderDailyStoryShell();
  await loadDailyStoryStage(DAILY_STORY_STATE.stage || 1, false);
}


const DAILY_STORY_FALLBACK_STAGES = [
  {stage:1, short:'Scan', title:'Scan the portfolio'},
  {stage:2, short:'Explain', title:'Explain the top priorities'},
  {stage:3, short:'Sequence', title:'Sequence the working day'},
  {stage:4, short:'Commit', title:'Commit the operating plan'},
];

function storyAiBadge(r){
  const live = r && r.generated_by === 'llm_grounded';
  const ex=(r&&r.execution)||{};
  return `<span class="story-ai-badge ${live?'live':'fallback'}"><i></i>${live?`Live AI`:'Runtime safety engine · evidence only'}</span>`;
}
function storyConfidence(r){
  const n = Math.round(Number((r&&r.confidence)||0)*100);
  return `<div class="story-confidence"><span>Confidence</span><b>${n}%</b><i><em style="width:${n}%"></em></i></div>`;
}
function renderStoryFacts(facts){
  return `<div class="story-facts">${(facts||[]).map((f,i)=>`<article class="story-fact tone-${esc(f.tone||'neutral')}" style="animation-delay:${i*.045}s"><span>${esc(f.label)}</span><b>${esc(f.value)}</b><em>${esc(f.source||'observed source')}</em>${f.detail?`<p>${esc(f.detail)}</p>`:''}</article>`).join('')}</div>`;
}
function renderStoryFindings(rows){
  return `<div class="story-findings">${(rows||[]).map((x,i)=>`<div><span>${i+1}</span><p>${esc(x)}</p></div>`).join('')}</div>`;
}
function renderStoryEvidence(rows){
  return `<div class="story-evidence"><b>Evidence used in this chapter</b>${(rows||[]).map(x=>`<span>${esc(x)}</span>`).join('')}</div>`;
}
function renderExecutionTrace(r){
  const x=(r&&r.execution)||{};
  if(!x.runtime_generated) return '';
  const runtimeText=x.llm_invoked
    ?'The AI engine generated the narrative at runtime after the bank calculation engine, deterministic policy matcher and live SOP retrieval completed.'
    :'The calculation engine and local SOP decision index executed at runtime. The AI engine was unavailable, so the safety engine ran instead.';
  return `<section class="ai-runtime-trace"><div><span>AI execution</span><b>${esc(x.mode||'Runtime analysis')}</b><p>${runtimeText}</p></div><div class="runtime-metrics"><article><span>AI engine</span><b>${esc(x.model||'Contoso AI')}</b></article><article><span>Calculations</span><b>${esc(x.calculations_executed||0)}</b></article><article><span>Validated SOP rules</span><b>${esc(x.sop_clauses_retrieved||0)}</b></article><article><span>Live RAG chunks</span><b>${esc(x.rag_chunks_retrieved||0)}</b></article><article><span>Cache</span><b>${x.cache_hit?'Reused':'Fresh run'}</b></article></div><small>${esc(x.sop_retrieval_mode||'Local deterministic SOP decision index')} · Prompt ${esc(x.prompt_version||'—')} · Evidence fingerprint ${esc(x.evidence_fingerprint||'—')}</small></section>`;
}
function renderCalculations(rows){
  if(!(rows||[]).length) return '';
  return `<section class="decision-section"><header><div><span>Quantitative engine</span><h4>Calculations that change the decision</h4></div><em>${rows.length} tests</em></header><div class="calculation-grid">${rows.map((x,i)=>`<article class="tone-${esc(x.tone||'neutral')}" style="animation-delay:${i*.035}s"><div><span>${esc(x.label)}</span><b>${esc(x.display)}</b></div><code>${esc(x.formula)}</code><p>${esc(x.interpretation)}</p><small>${(x.sources||[]).map(esc).join(' · ')}</small></article>`).join('')}</div></section>`;
}
function renderPolicyMatches(rows){
  if(!(rows||[]).length) return '';
  return `<section class="decision-section policy-section"><header><div><span>SOP retrieval</span><h4>Policy clauses applied to this customer</h4></div><em>${rows.length} matched</em></header><div class="policy-grid">${rows.map((x,i)=>`<article><div class="policy-ref">${esc(x.sop_ref)}</div><h5>${esc(x.title)}</h5><p class="policy-clause">${esc(x.clause)}</p><div class="policy-application"><b>Why it applies</b><p>${esc(x.why_applicable)}</p></div><div class="policy-decision"><b>Decision effect</b><p>${esc(x.decision_effect)}</p></div><small>${(x.evidence||[]).map(esc).join(' · ')}</small></article>`).join('')}</div></section>`;
}
function renderReasoningBridge(rows, contradictions){
  if(!(rows||[]).length && !(contradictions||[]).length) return '';
  return `<section class="decision-section reasoning-section"><header><div><span>Auditable reasoning</span><h4>Fact → SOP test → bank implication</h4></div></header><div class="reasoning-bridge">${(rows||[]).map(x=>`<article><span>${esc(x.step)}</span><div><b>Observed</b><p>${esc(x.observed_fact)}</p></div><i>→</i><div><b>Policy test</b><p>${esc(x.policy_test)}</p></div><i>→</i><div><b>Implication</b><p>${esc(x.decision_implication)}</p></div></article>`).join('')}</div>${(contradictions||[]).length?`<div class="contradiction-grid">${contradictions.map(x=>`<article><span>Contradiction resolved</span><h5>${esc(x.title)}</h5><div><p>${esc(x.left)}</p><i>vs</i><p>${esc(x.right)}</p></div><b>${esc(x.implication)}</b></article>`).join('')}</div>`:''}</section>`;
}
function renderOutcomes(rows){
  if(!(rows||[]).length) return '';
  return `<div class="outcome-strip">${rows.map(x=>`<article><span>${esc(x.label)}</span><b>${esc(x.value)}</b><p>${esc(x.detail||'')}</p></article>`).join('')}</div>`;
}
function renderSolutionOptions(rows,recommendedId){
  if(!(rows||[]).length) return '';
  return `<section class="decision-section solution-section"><header><div><span>Solution design</span><h4>Intervention lanes tested by AI</h4></div><em>${rows.length} lanes</em></header><div class="solution-grid">${rows.map(x=>`<article class="${x.solution_id===recommendedId?'recommended':''}"><div><span>${esc(x.lane)}</span>${x.solution_id===recommendedId?'<em>Recommended</em>':''}</div><h5>${esc(x.title)}</h5><p>${esc(x.description)}</p>${renderOutcomes(x.quantified_outcomes||[])}<small>${(x.policy_refs||[]).map(esc).join(' · ')}</small><b class="solution-guardrail">${esc(x.guardrail||'')}</b></article>`).join('')}</div></section>`;
}
function renderRetrievedSopChunks(rows){
  if(!(rows||[]).length) return '';
  return `<section class="decision-section live-rag-section"><header><div><span>Live SOP retrieval</span><h4>Live policy retrieval used by the AI engine</h4></div><em>${rows.length} chunks</em></header><div class="rag-chunk-grid">${rows.map(x=>`<article><div><span>${esc(x.sop_id||'SOP')}</span><b>${esc(x.sop_title||'Policy source')}</b></div><h5>${esc(x.section_title||'Relevant section')}</h5><p>${esc(x.content||'')}</p><small>${esc(x.chunk_id||'')} ${x.score!=null?`· score ${Number(x.score).toFixed(3)}`:''}</small></article>`).join('')}</div></section>`;
}
function renderAiSynthesis(r){
  const a=(r&&r.ai_synthesis)||{};
  if(!a.solution_rationale && !(a.policy_reasoning_summary||[]).length && !(a.calculation_callouts||[]).length) return '';
  return `<section class="ai-synthesis-panel"><header><span>Runtime model synthesis</span><b>What the model added after calculations and SOP retrieval</b></header>${a.solution_rationale?`<p>${esc(a.solution_rationale)}</p>`:''}<div>${(a.calculation_callouts||[]).map(x=>`<article><span>∑</span><p>${esc(x)}</p></article>`).join('')}${(a.policy_reasoning_summary||[]).map(x=>`<article><span>§</span><p>${esc(x)}</p></article>`).join('')}</div></section>`;
}
function dailyStageMeta(){
  const result = DAILY_STORY_STATE.results[DAILY_STORY_STATE.stage];
  return (result&&result.stages)||DAILY_STORY_FALLBACK_STAGES;
}
function renderDailyStoryShell(){
  const stages = dailyStageMeta();
  const current = DAILY_STORY_STATE.stage||1;
  $("content").innerHTML = `
    <section class="progressive-hero daily-story-hero">
      <div><div class="eyebrow">RM-1042 · progressive AI operating plan</div><h2>Morning briefing, one decision at a time</h2><p>AI first triages the portfolio, then explains priority, sequences the day and only then proposes actions. Each chapter adds new information instead of repeating the same customer dossier.</p></div>
      <button class="btn story-reset" onclick="loadBriefing(true)">↻ Restart story</button>
    </section>
    <div class="story-rail">${stages.map((st,i)=>`<button class="story-step ${st.stage===current?'active':''} ${st.stage<current?'done':''} ${st.stage>DAILY_STORY_STATE.unlocked?'locked':''}" onclick="gotoDailyStoryStage(${st.stage})"><span>${st.stage<current?'✓':st.stage}</span><b>${esc(st.short||st.title)}</b><em>${esc(st.title)}</em></button>${i<stages.length-1?'<i class="story-link"></i>':''}`).join('')}</div>
    <div id="dailyStoryMount" class="story-mount"><div class="story-loading"><i></i><b>Preparing AI chapter ${current}</b><span>Only evidence required for this decision is being assembled.</span></div></div>`;
}
async function loadDailyStoryStage(stage, force=false){
  if(stage > DAILY_STORY_STATE.unlocked){ toast('Complete the current AI chapter first'); return; }
  DAILY_STORY_STATE.stage = stage;
  renderDailyStoryShell();
  const mount = $("dailyStoryMount");
  if(!force && DAILY_STORY_STATE.results[stage]){
    mount.innerHTML = renderDailyStoryStage(DAILY_STORY_STATE.results[stage]);
    animateCounters(mount); return;
  }
  try{
    const r = await api(`/v1/briefing/progressive/stage?rm_id=${encodeURIComponent(RM_ID||'RM-1042')}`, {method:'POST', body:JSON.stringify({stage, force, focus_customer_id:DAILY_STORY_STATE.focusCustomerId})});
    DAILY_STORY_STATE.results[stage] = r;
    DAILY_STORY_STATE.unlocked = Math.max(DAILY_STORY_STATE.unlocked, Math.min((r.stage_count||4), stage+1));
    DAILY_STORY_STATE.focusCustomerId = r.focus_customer_id || DAILY_STORY_STATE.focusCustomerId;
    renderDailyStoryShell();
    $("dailyStoryMount").innerHTML = renderDailyStoryStage(r);
    animateCounters($("dailyStoryMount"));
  }catch(e){ mount.innerHTML = `<div class="placeholder"><div class="big">Progressive briefing unavailable</div><div>${esc(e.message)}</div></div>`; }
}
function gotoDailyStoryStage(stage){
  if(stage>DAILY_STORY_STATE.unlocked){ toast('Run the previous chapter to unlock this one'); return; }
  loadDailyStoryStage(stage,false);
}
function advanceDailyStory(){
  const max=(dailyStageMeta()||[]).length||4;
  if(DAILY_STORY_STATE.stage<max) loadDailyStoryStage(DAILY_STORY_STATE.stage+1,false);
}
function selectDailyFocus(cid){
  DAILY_STORY_STATE.focusCustomerId=cid;
  DAILY_STORY_STATE.results={1:DAILY_STORY_STATE.results[1]};
  DAILY_STORY_STATE.unlocked=Math.max(2,DAILY_STORY_STATE.unlocked);
  loadDailyStoryStage(2,true);
}
function renderDailyStoryStage(r){
  const stage=Number(r.stage||1), max=Number(r.stage_count||4);
  let visual='';
  if(stage===1){
    visual=`<div class="story-queue">${(r.queue||[]).map((x,i)=>`<button onclick="selectDailyFocus('${esc(x.customer_id)}')"><span class="sq-rank">${x.rank||i+1}</span><div><b>${esc(x.display_name||x.customer)}</b><em>${esc(x.bucket)}</em><p>${esc(x.reason||x.why_now)}</p></div><strong>Open reasoning →</strong></button>`).join('')}</div>`;
  }else if(stage===2){
    visual=`<div class="priority-matrix">${(r.queue||[]).map(x=>`<article class="${x.customer_id===r.focus_customer_id?'focus':''}"><div><span>#${x.rank}</span><b>${esc(x.customer)}</b><em>${esc(x.bucket)}</em></div><p>${esc(x.why_now)}</p><div class="pm-solution"><b>${esc(x.recommended_action||'Decision pack')}</b><span>${esc(x.solution_impact||'')}</span></div><div class="pm-metrics"><span>${x.critical_signals||0} critical</span><span>${x.high_signals||0} high</span><span>${x.document_blockers||0} blockers</span><span>RVS ${x.relationship_value_score||0}</span></div>${(x.policy_refs||[]).length?`<small class="pm-policy">${x.policy_refs.map(esc).join(' · ')}</small>`:''}<button onclick="selectCustomer('${esc(x.customer_id)}')">Open customer</button></article>`).join('')}</div>`;
  }else if(stage===3){
    visual=`<div class="day-sequence">${(r.day_sequence||[]).map((x,i)=>`<article><time>${esc(x.slot)}</time><span>${i+1}</span><div><b>${esc(x.customer)}</b><em>${esc(x.action)}</em><p>${esc(x.why)}</p><small>Prepare: ${esc(x.prep)}</small></div></article>`).join('')}</div>`;
  }else{
    visual=`<div class="commit-grid">${(r.actions||[]).map((x,i)=>`<article><span>${esc(x.slot||String(i+1))}</span><div><b>${esc(x.customer)}</b><em>${esc(x.action)}</em><p>${esc(x.prep)}</p>${x.impact?`<strong class="commit-impact">Expected outcome: ${esc(x.impact)}</strong>`:''}${(x.policy_refs||[]).length?`<small>${x.policy_refs.map(esc).join(' · ')}</small>`:`<small>${esc(x.crm_candidate)}</small>`}</div><button onclick="selectCustomer('${esc(x.customer_id)}')">Open 360 →</button></article>`).join('')}</div>`;
  }
  return `<section class="story-stage-card">
    <header><div><span class="story-chapter">Chapter ${stage} of ${max} · ${esc(r.capability)}</span><h3>${esc(r.title)}</h3><p>${esc(r.question)}</p></div>${storyAiBadge(r)}</header>
    ${renderExecutionTrace(r)}
    <div class="story-answer"><div><span>AI answer</span><h4>${esc(r.headline)}</h4><p>${esc(r.narrative)}</p></div>${storyConfidence(r)}</div>
    ${renderStoryFacts(r.observed_facts)}
    <div class="story-two-col"><section><h4>What this chapter discovered</h4>${renderStoryFindings(r.new_findings)}</section><section class="story-uncertainty"><h4>Still uncertain</h4><p>${esc(r.uncertainty)}</p><span>Human review required</span></section></div>
    ${visual}
    ${renderStoryEvidence(r.evidence)}
    <footer class="story-stage-nav"><button class="btn ghost" onclick="loadDailyStoryStage(${Math.max(1,stage-1)},false)" ${stage===1?'disabled':''}>← Previous chapter</button><button class="btn" onclick="loadDailyStoryStage(${stage},true)">↻ Re-run this AI chapter</button>${stage<max?`<button class="btn primary" onclick="advanceDailyStory()">Next: ${esc((r.stages||[])[stage]?.title||'next chapter')} →</button>`:`<button class="btn primary" onclick="selectCustomer('${esc(r.focus_customer_id||'')}')">Open top customer →</button>`}</footer>
  </section>`;
}

const REL_STORY_FALLBACK_STAGES=[
  {stage:1,short:'Baseline',title:'Establish the relationship baseline'},
  {stage:2,short:'What changed',title:'Detect what changed and why today matters'},
  {stage:3,short:'Posture',title:'Resolve the relationship posture'},
  {stage:4,short:'Conversation',title:'Frame the customer conversation'},
  {stage:5,short:'Commit',title:'Commit the day plan'},
];
function relState(cid){
  if(!RELATIONSHIP_STORY_STATE[cid]) RELATIONSHIP_STORY_STATE[cid]={stage:1,unlocked:1,results:{}};
  return RELATIONSHIP_STORY_STATE[cid];
}
function isRelationshipStoryComplete(cid){ const st=RELATIONSHIP_STORY_STATE[cid]; return !!(st&&st.results&&st.results[5]); }
function relationshipStageMeta(cid){ const st=relState(cid), r=st.results[st.stage]; return (r&&r.stages)||REL_STORY_FALLBACK_STAGES; }
function renderRelationshipStoryShell(cid){
  const st=relState(cid), stages=relationshipStageMeta(cid);
  return `<section class="relationship-story-shell">
    <div class="rel-story-top"><div><div class="eyebrow">RM Assist · how AI helps you</div><h3>From the customer's problem to a grounded next step</h3><p>Each chapter takes one decision: the problem, the AI's read with its data, and what to do. The full working stays one click away.</p></div><button class="btn" onclick="resetRelationshipStory('${cid}')">↻ Restart</button></div>
    <div class="story-rail compact">${stages.map((x,i)=>`<button class="story-step ${x.stage===st.stage?'active':''} ${x.stage<st.stage?'done':''} ${x.stage>st.unlocked?'locked':''}" onclick="gotoRelationshipStoryStage('${cid}',${x.stage})"><span>${x.stage<st.stage?'✓':x.stage}</span><b>${esc(x.short||x.title)}</b><em>${esc(x.title)}</em></button>${i<stages.length-1?'<i class="story-link"></i>':''}`).join('')}</div>
    <div id="relationshipStoryMount" class="story-mount"><div class="story-loading"><i></i><b>Preparing chapter ${st.stage}</b><span>Separating observed customer facts from AI reasoning.</span></div></div>
  </section>`;
}
function resetRelationshipStory(cid){ RELATIONSHIP_STORY_STATE[cid]={stage:1,unlocked:1,results:{}}; loadRelationshipStory(cid,1,false); }
async function loadRelationshipStory(cid, stage=1, force=false){
  const st=relState(cid);
  if(stage>st.unlocked){ toast('Complete the current customer-thesis chapter first'); return; }
  st.stage=stage;
  const host=$("relationshipStory"); if(!host) return;
  host.innerHTML=renderRelationshipStoryShell(cid);
  const mount=$("relationshipStoryMount");
  if(!force&&st.results[stage]){ mount.innerHTML=renderRelationshipStoryStage(cid,st.results[stage]); enableJourneyAfterThesis(cid); animateCounters(mount); return; }
  try{
    const r=await api(`/v1/customers/${cid}/relationship-story/stage`,{method:'POST',body:JSON.stringify({stage,force})});
    st.results[stage]=r; st.unlocked=Math.max(st.unlocked,Math.min(r.stage_count||5,stage+1));
    host.innerHTML=renderRelationshipStoryShell(cid);
    $("relationshipStoryMount").innerHTML=renderRelationshipStoryStage(cid,r);
    enableJourneyAfterThesis(cid); animateCounters($("relationshipStoryMount"));
  }catch(e){ mount.innerHTML=`<div class="placeholder"><div class="big">Customer thesis unavailable</div><div>${esc(e.message)}</div></div>`; }
}
function gotoRelationshipStoryStage(cid,stage){ const st=relState(cid); if(stage>st.unlocked){toast('Run the previous chapter first');return;} loadRelationshipStory(cid,stage,false); }
function advanceRelationshipStory(cid){ const st=relState(cid), max=relationshipStageMeta(cid).length||5; if(st.stage<max) loadRelationshipStory(cid,st.stage+1,false); }
function enableJourneyAfterThesis(cid){ const b=$("journeyNextButton"); if(b) b.disabled=!isRelationshipStoryComplete(cid); }
// All the RM-only "how the AI got here" machinery — hidden behind a dropdown so the
// visible chapter stays: problem → AI read → data → decision.
function rmxInternals(r){
  const f=r.data_freshness||{};
  return `
    ${renderExecutionTrace(r)}
    ${renderCalculations(r.calculations)}
    ${renderPolicyMatches(r.policy_matches)}
    ${renderRetrievedSopChunks(r.retrieved_sop_chunks)}
    ${renderReasoningBridge(r.reasoning_summary,r.contradictions)}
    ${renderAiSynthesis(r)}
    ${(r.new_findings||[]).length?`<div class="rmx-working-sec"><h4>What this chapter established</h4>${renderStoryFindings(r.new_findings)}</div>`:''}
    ${renderStoryEvidence(r.evidence)}
    <div class="story-provenance"><span>Analysis ${esc(r.analysis_id||'')}</span><span>Customer record ${esc(f.customer_updated_at||'—')}</span><span>Last contact ${esc(f.last_interaction_date||'—')}</span><span>Latest txn ${esc(f.latest_transaction_date||'—')}</span><span>Decision as-of ${esc(f.analysis_as_of||'—')}</span></div>`;
}
function renderRelationshipStoryStage(cid,r){
  const stage=Number(r.stage||1),max=Number(r.stage_count||5);
  // Visible, decluttered decision output per chapter (customer-facing / RM action).
  let decision='';
  if(stage===3){
    const d=r.decision||{};
    decision=`
      <div class="rmx-decision">
        <div class="rmx-verdict p-${String(d.posture||'watch').toLowerCase()}"><span>AI-resolved posture</span><b>${esc(d.posture||'Watch')}</b><p>${esc(d.reason||'')}</p>${d.recommended_solution_title?`<strong>→ ${esc(d.recommended_solution_title)}</strong>`:''}</div>
        ${renderOutcomes(r.quantified_outcomes||[])}
      </div>
      ${renderSolutionOptions(r.solution_options||[],d.recommended_solution_id)}
      <details class="rmx-working"><summary><span class="rmx-sum-t">How the AI chose this</span><span class="rmx-sum-tag">RM-only working</span><span class="rmx-chev">⌄</span></summary><div class="rmx-working-body">
        <div class="rmx-working-sec"><h4>Lanes the AI compared</h4><div class="lane-compare">${(d.candidate_lanes||[]).map(x=>`<article class="${x.lane===d.posture?'chosen':''}"><span>${esc(x.lane)}</span><b>${x.active?'Active':'Not dominant'}</b><p>${esc(x.reason)}</p></article>`).join('')}</div>${(d.suppressed_products||[]).length?`<div class="suppressed"><b>Suppressed now</b>${d.suppressed_products.map(x=>`<span>${esc(x)}</span>`).join('')}</div>`:''}</div>
        ${rmxInternals(r)}
      </div></details>`;
  }else if(stage===4){
    const c=r.conversation||{};
    decision=`
      <div class="conversation-frame"><blockquote>“${esc(c.opening_line||r.narrative)}”</blockquote><section><h4>Ask only these</h4>${(c.questions||[]).map((x,i)=>`<div><span>${i+1}</span><p>${esc(x)}</p></div>`).join('')}</section><aside><b>Do not say</b><p>${esc(c.do_not_say||'Do not imply approval.')}</p></aside></div>
      ${renderOutcomes(r.quantified_outcomes||[])}
      <details class="rmx-working"><summary><span class="rmx-sum-t">The AI's working</span><span class="rmx-sum-tag">RM-only</span><span class="rmx-chev">⌄</span></summary><div class="rmx-working-body">${rmxInternals(r)}</div></details>`;
  }else if(stage===5){
    decision=`
      <div class="relationship-commit"><section><h4>${esc((r.recommended_solution||{}).title||'Resolution sequence')}</h4>${(r.actions||[]).map(a=>`<article><span>${esc(a.order)}</span><div><b>${esc(a.action)}</b><p>${esc(a.why)}</p><em>${esc(a.owner)} · ${esc(a.due)}</em></div></article>`).join('')}</section><aside><h4>Needs human approval</h4>${(r.crm_candidates||[]).map(x=>`<div><span>${esc(x.type)}</span><b>${esc(x.title)}</b><em>${x.approval_required?'Authorised review required':'Ready'}</em></div>`).join('')}</aside></div>
      ${renderOutcomes(r.quantified_outcomes||[])}
      <details class="rmx-working"><summary><span class="rmx-sum-t">The AI's working</span><span class="rmx-sum-tag">RM-only</span><span class="rmx-chev">⌄</span></summary><div class="rmx-working-body">${rmxInternals(r)}</div></details>`;
  }else{
    decision=`<details class="rmx-working"><summary><span class="rmx-sum-t">The AI's working — calculations, policy &amp; evidence</span><span class="rmx-sum-tag">RM-only</span><span class="rmx-chev">⌄</span></summary><div class="rmx-working-body">${rmxInternals(r)}</div></details>`;
  }
  return `<section class="story-stage-card relationship-stage rmx-chapter">
    <header class="rmx-head"><div><span class="story-chapter">Chapter ${stage} of ${max} · ${esc(r.capability)}</span><h3>${esc(r.title)}</h3></div>${storyAiBadge(r)}</header>
    <div class="rmx-problem"><span>The problem to solve</span><p>${esc(r.question)}</p></div>
    <div class="rmx-answer"><div class="rmx-answer-main"><span>How AI assists the RM</span><h4>${esc(r.headline)}</h4><p>${esc(r.narrative)}</p></div><div class="rmx-answer-side">${storyConfidence(r)}${r.uncertainty?`<div class="rmx-uncertainty"><span>Still needs your check</span><p>${esc(r.uncertainty)}</p></div>`:''}</div></div>
    ${(r.observed_facts||[]).length?`<div class="rmx-data"><div class="rmx-data-label">The data behind it</div>${renderStoryFacts(r.observed_facts)}</div>`:''}
    ${decision}
    <footer class="story-stage-nav"><button class="btn ghost" onclick="loadRelationshipStory('${cid}',${Math.max(1,stage-1)},false)" ${stage===1?'disabled':''}>← Previous chapter</button><button class="btn" onclick="loadRelationshipStory('${cid}',${stage},true)">↻ Re-run this AI chapter</button>${stage<max?`<button class="btn primary" onclick="advanceRelationshipStory('${cid}')">Next: ${esc((r.stages||[])[stage]?.title||'next chapter')} →</button>`:`<button class="btn primary" onclick="nextStep()">Continue to stakeholder map →</button>`}</footer>
  </section>`;
}

function renderBriefing(b) {
  const cards = (b.briefs || []).map((br) => {
    const lines = (br.narrative_lines || []).map((ln, i) => `
      <div class="brief-line">
        <div class="bl-claim" onclick="this.parentNode.querySelector('.bl-why').classList.toggle('show')">
          <span class="bl-num">${i + 1}</span><span>${esc(ln.claim)}</span>
          <span class="bl-src">${esc(ln.source)} ▾</span>
        </div>
        <div class="bl-why">${(ln.evidence || []).map(e => `<div class="bl-ev">• ${esc(e)}</div>`).join("")}</div>
      </div>`).join("");
    const opps = (br.cross_sell || []).map(o => `<span class="opp-pill">${esc(o.product)}</span>`).join("");
    const blocked = (br.blocked_opportunities || []).map(o => `<span class="opp-pill blocked">Blocked: ${esc(o.product)}</span>`).join("");
    return `
      <div class="brief-card lift">
        <div class="brief-head" onclick="selectCustomer('${br.customer_id}')">
          <div><div class="bh-name">${esc(br.display_name)}</div><div class="bh-sub">${esc(br.headline)}</div></div>
          <div class="bh-open">Open 360 →</div>
        </div>
        <div class="brief-lines">${lines}</div>
        <div class="gameplan-wrap">
          <button class="btn gp-btn" onclick="loadGamePlan('${br.customer_id}', this)">▸ Generate AI game plan for this conversation</button>
          <div class="gameplan" id="gp-${br.customer_id}"></div>
        </div>
        <div class="brief-opps"><span class="opps-label">Opportunity lens</span>${opps || '<span class="muted">No eligible cross-sell now</span>'}${blocked}</div>
      </div>`;
  }).join("");
  $("content").innerHTML = `
    <div class="hero">
      <div><div class="eyebrow">RM-1042 · Daily operating plan</div><h2>Today’s MSME Relationship Briefing</h2><p>${b.customer_count} customer relationships ranked by risk, growth, service recovery and renewal readiness. Click any narrative line to inspect the reasoning trace.</p></div>
      <button class="btn primary" onclick="loadBriefing()">Refresh briefing</button>
    </div>
    <div class="kpi-row">
      <div class="kpi"><span>Portfolio</span><strong data-count="${b.customer_count}">0</strong><em>accounts</em></div>
      <div class="kpi"><span>Mode</span><strong>Explainable</strong><em>reason traces</em></div>
      <div class="kpi"><span>Copilot</span><strong>Human-in-loop</strong><em>approval-gated CRM writes</em></div>
      <div class="kpi"><span>SOP grounding</span><strong>Active</strong><em>cited retrieval</em></div>
    </div>
    <div class="briefing">${cards}</div>`;
  animateCounters($("content"));
}

async function loadQueue() {
  $("queue").innerHTML = skelCards(5);
  try {
    const data = await api("/v1/portfolio/priority-queue");
    QUEUE_CACHE = data.queue || [];
    renderQueue();
  } catch (e) { $("queue").innerHTML = `<div class="loading">Failed to load queue.<br>${esc(e.message)}</div>`; }
}

function renderQueue() {
  const bucketClass = (b) => (b === "Risk Watch" || b === "Customer Intervention") ? "b-risk" : b === "Growth" ? "b-growth" : "b-renewal";
  // Lean build: the queue focuses on the single demoable RM Assist journey (Rakesh Sharma).
  const DEMO_IDS = ["CTB-RTL-002"];
  let q = QUEUE_CACHE.filter(c => DEMO_IDS.includes(c.customer_id))
                     .sort((a, b) => DEMO_IDS.indexOf(a.customer_id) - DEMO_IDS.indexOf(b.customer_id));
  if (QUEUE_FILTER !== "all") q = q.filter(c => bucketKey(c.bucket) === QUEUE_FILTER);
  if (QUEUE_SEARCH) q = q.filter(c => (String(c.display_name||"") + " " + String(c.reason||"")).toLowerCase().includes(QUEUE_SEARCH));
  $("qcount").textContent = q.length + " accounts";
  if (!q.length) { $("queue").innerHTML = `<div class="loading">No accounts match this filter.</div>`; return; }
  $("queue").innerHTML = q.map((c, i) => `
    <div class="qcard ${bucketClass(c.bucket)}${c.customer_id===CURRENT?' active':''}" data-id="${c.customer_id}" style="animation-delay:${i*0.04}s" onclick="selectCustomer('${c.customer_id}')">
      <div class="name">${esc(c.display_name)}</div><div class="bucket">${esc(c.bucket)}</div>
      <div class="reason">${esc(c.reason)}</div>
      <div class="pills">${c.critical_signals ? `<span class="pill crit">${c.critical_signals} critical</span>` : ""}${c.high_signals ? `<span class="pill high">${c.high_signals} high</span>` : ""}${c.blocking_documents ? `<span class="pill">${c.blocking_documents} doc blocker</span>` : ""}<span class="pill">RVS ${c.relationship_value_score}</span></div>
    </div>`).join("");
}

async function selectCustomer(cid) {
  CURRENT = cid;
  document.querySelectorAll(".qcard").forEach(el => el.classList.toggle("active", el.dataset.id === cid));
  setMenu('journeys'); setCrumb('Customer 360');
  $("content").innerHTML = `<div class="hero"><div><div class="eyebrow">Loading dossier</div><div class="skel skel-line" style="width:300px;height:26px;margin-top:8px"></div><div class="skel skel-line" style="width:480px;margin-top:10px"></div></div></div>${skelKpis()}${skelCards(2)}`;
  try {
    const [d360, dossier, playbook, command] = await Promise.all([
      api(`/v1/customers/${cid}/360`),
      api(`/v1/customers/${cid}/relationship-dossier`),
      api(`/v1/customers/${cid}/live-call-playbook`),
      api(`/v1/customers/${cid}/command-center`),
    ]);
    LAST_DOSSIER = dossier;
    renderCustomer(d360, dossier, playbook, command);
    setCrumb(d360.customer ? d360.customer.display_name : 'Customer 360');
  } catch (e) { $("content").innerHTML = `<div class="loading">Failed: ${esc(e.message)}</div>`; }
}

function renderCustomer(d, ds, pb, cmd = null) {
  const c = d.customer, p = d.business_profile, f = d.primary_facility, cs = d.conduct_summary, enh = d.enhancement;
  const eligibleOps = (ds.opportunities || []).filter(o => o.eligible), blockedOps = (ds.opportunities || []).filter(o => !o.eligible);
  window.__C = { d, ds, pb, cmd, cid: c.customer_id, eligibleOps, blockedOps };   // stash for both modes
  $("content").innerHTML = `
    <div class="customer-hero lift">
      <div>
        <div class="eyebrow">${esc(c.customer_id)} · ${esc(c.constitution)} · ${esc(c.home_branch_code)}</div>
        <h2>${esc(c.display_name)}</h2>
        <p>${esc(p.industry_description)} · since ${esc(c.customer_since)} · ${esc(p.operating_locations)} · consent ${esc(c.consent_status)}</p>
        <div class="rx-hero-stats">
          <span><b data-count="${Number(ds.summary_metrics.total_credits_inr)/1e7}" data-prefix="₹" data-suffix=" Cr" data-dec="2">₹0</b> credits FY</span>
          <span><b data-count="${Number(cs.avg_utilization_pct)||0}" data-suffix="%" data-dec="1">0%</b> avg utilisation</span>
          <span><b data-count="${Number((d.bureau&&d.bureau.score)||(ds.summary_metrics&&ds.summary_metrics.bureau_score)||0)}">0</b> bureau score</span>
          <span><b data-count="${Number(ds.summary_metrics.open_tasks)||0}">0</b> open tasks</span>
        </div>
      </div>
      <div class="modeswitch" role="tablist">
        <button class="ms-btn active" id="ms-core" onclick="setMode('core')"><span class="ic">▤</span> Core CRM</button>
        <button class="ms-btn rm" id="ms-rm" onclick="setMode('rm')"><span class="ic">✦</span> RM Assist</button>
      </div>
    </div>
    <div id="modebody"></div>
    <section class="panel" id="memoPanel" style="display:none;margin-top:16px"><h3>Renewal Memo · Draft</h3><div id="memoBody"></div></section>
  `;
  animateCounters($("content"));
  setMode('core');
}

/* ---------- before/after demo mode switch ---------- */
function setMode(mode){
  window.__C.mode = mode;
  const core = $("ms-core"), rm = $("ms-rm");
  if(core) core.classList.toggle("active", mode==='core');
  if(rm) rm.classList.toggle("active", mode==='rm');
  if(mode==='rm') renderRmAssistMode(); else renderCoreMode();
}

/* ===================== CORE CRM (system-of-record, no AI) ===================== */
function renderCoreMode(){
  const { d, ds, cid } = window.__C;
  const c = d.customer, p = d.business_profile, f = d.primary_facility, cs = d.conduct_summary;
  $("modebody").innerHTML = `
    <div class="core-banner"><span class="ic">▤</span> <b>Core banking CRM</b> — system-of-record. This is the customer exactly as the branch sees them today: master data, accounts, transactions, cases and documents. <b>No AI applied.</b></div>
    <div class="kpi-row">
      <div class="kpi"><span>Sanctioned limit</span><strong data-count="${Number(f.sanction_limit_inr)/1e5}" data-prefix="₹" data-suffix=" L" data-dec="2">₹0</strong><em>${esc(f.facility_type||'Working capital')}</em></div>
      <div class="kpi"><span>Credits FY</span><strong data-count="${Number(ds.summary_metrics.total_credits_inr)/1e7}" data-prefix="₹" data-suffix=" Cr" data-dec="2">₹0</strong><em>raw account turnover</em></div>
      <div class="kpi"><span>Open tasks</span><strong><span data-count="${ds.summary_metrics.open_tasks}">0</span></strong><em>${ds.summary_metrics.open_service_tickets} service ticket(s)</em></div>
      <div class="kpi"><span>Avg utilization</span><strong data-count="${cs.avg_utilization_pct}" data-suffix="%">0%</strong><em>peak ${cs.peak_utilization_pct}%</em></div>
    </div>
    <section class="panel" id="rawFactsPanel" style="margin-top:16px"><h3>Raw facts on file <span class="muted">— the data as it sits in the CRM, no insight applied</span></h3><div id="rawFactsBody">${skelPanel("Reading the record\u2026", 4)}</div></section>
    <section class="panel call-records-panel" id="callRecordsPanel" style="margin-top:16px">
      <div class="call-records-head"><div><h3>Teams call transcripts &amp; AI record</h3><p>Post-call transcript, AI answers, nudges and CRM actions. Downloadable for downstream Work IQ demonstrations.</p></div><span class="call-live-pill">POST-CALL RECORD</span></div>
      <div id="callRecordsBody">${skelPanel("Checking for completed call records\u2026", 2)}</div>
    </section>
    <nav class="tabs" id="tabs">
      <button class="tab active" data-tab="relationship" onclick="switchTab('relationship')">Cases &amp; Timeline</button>
      <button class="tab" data-tab="transactions" onclick="switchTab('transactions')">Transactions &amp; GST</button>
      <button class="tab" data-tab="conduct" onclick="switchTab('conduct')">Account Conduct</button>
      <button class="tab" data-tab="documents" onclick="switchTab('documents')">Documents</button>
      <button class="tab" data-tab="personas" onclick="switchTab('personas')">Contacts</button>
    </nav>
    <div id="tabbody" class="tabbody"></div>`;
  loadRawFacts(cid);
  loadCallRecords(cid);
  switchTab('relationship');
  animateCounters($("modebody"));
}

async function loadRawFacts(cid){
  try{
    const ev = await api(`/v1/customers/${cid}/raw-facts`);
    const tb = ev.top_buyer, ts = ev.top_supplier, ss = ev.stock_statement, f = ev.facility, st = ev.stress;
    const threads = (st.open_threads||[]).map(t=>`<li><b>${esc(t.topic)}</b> — <span class="muted">${esc(t.status||'')}</span></li>`).join('') || '<li class="muted">none on file</li>';
    const covs = (st.pending_covenants||[]).map(c=>`<li><b>${esc(c.type)}</b> — ${esc(c.status||'')}, due ${esc(c.due||'')}</li>`).join('') || '<li class="muted">none pending</li>';
    $("rawFactsBody").innerHTML = `
      <div class="rawfacts-grid">
        <div class="rf-block"><div class="rf-label">Vintage & profile</div>
          <div class="rf-line"><span>Constitution</span><b>${esc(ev.constitution||'—')}</b></div>
          <div class="rf-line"><span>Customer since</span><b>${esc(ev.vintage_years!=null?ev.vintage_years+' yrs':'—')}</b></div>
          <div class="rf-line"><span>Consent status</span><b>${esc(ev.consent_status||'—')}</b></div>
          <div class="rf-line"><span>Operating loc.</span><b>${esc(ev.locations||'—')}</b></div>
        </div>
        <div class="rf-block"><div class="rf-label">Facility & utilisation</div>
          <div class="rf-line"><span>Sanction</span><b>${esc(f.sanction_limit_text)}</b></div>
          <div class="rf-line"><span>Outstanding</span><b>${esc(f.outstanding_text)}</b></div>
          <div class="rf-line"><span>Available</span><b>${esc(f.available_text)}</b></div>
          <div class="rf-line"><span>Util avg 30d</span><b>${f.utilisation_avg_30d_pct}%</b></div>
          <div class="rf-line"><span>Util peak 30d</span><b>${f.utilisation_peak_30d_pct}%</b></div>
        </div>
        <div class="rf-block"><div class="rf-label">Turnover (FY)</div>
          <div class="rf-line"><span>Credits</span><b>${esc(ev.turnover.fy_credits_text)}</b></div>
          <div class="rf-line"><span>Debits</span><b>${esc(ev.turnover.fy_debits_text)}</b></div>
        </div>
        <div class="rf-block"><div class="rf-label">Top buyer</div>
          ${tb&&tb.name?`<div class="rf-line"><span>Name</span><b>${esc(tb.name)}</b></div>
          <div class="rf-line"><span>Concentration</span><b>${esc(tb.concentration_band||'—')}</b></div>
          <div class="rf-line"><span>Avg / month</span><b>${esc(tb.avg_monthly_text)}</b></div>
          <div class="rf-line"><span>Payment</span><b>${esc(tb.payment_behaviour||'—')}</b></div>`:'<div class="muted">No buyer on file</div>'}
        </div>
        <div class="rf-block"><div class="rf-label">Top supplier</div>
          ${ts&&ts.name?`<div class="rf-line"><span>Name</span><b>${esc(ts.name)}</b></div>
          <div class="rf-line"><span>Avg / month</span><b>${esc(ts.avg_monthly_text)}</b></div>`:'<div class="muted">No supplier on file</div>'}
        </div>
        <div class="rf-block"><div class="rf-label">Latest stock statement</div>
          <div class="rf-line"><span>Period</span><b>${esc(ss.period||'—')}</b></div>
          <div class="rf-line"><span>Status</span><b>${esc(ss.status||'—')}</b></div>
          <div class="rf-line"><span>Stock value</span><b>${esc(ss.stock_value_text)}</b></div>
          <div class="rf-line"><span>Receivables</span><b>${esc(ss.receivables_text)}</b></div>
          <div class="rf-line"><span>DP cover</span><b>${ss.dp_cover_ratio}x</b></div>
        </div>
        <div class="rf-block wide"><div class="rf-label">Open engagement threads</div><ul class="rf-list">${threads}</ul></div>
        <div class="rf-block wide"><div class="rf-label">Pending covenants</div><ul class="rf-list">${covs}</ul></div>
        <div class="rf-block"><div class="rf-label">Stress flags</div>
          <div class="rf-line"><span>Cheque returns (cycle)</span><b class="${st.cheque_returns_total>0?'down':''}">${st.cheque_returns_total}</b></div>
          <div class="rf-line"><span>Open service tickets</span><b class="${st.open_service_tickets>0?'warn':''}">${st.open_service_tickets}</b></div>
        </div>
      </div>
      <div class="rf-foot">These are the facts the branch can see today. <b>What's missing in Core CRM:</b> any synthesis, prioritisation, projection or grounded outreach — that is precisely what RM Assist adds in the journey.</div>`;
  }catch(e){ $("rawFactsBody").innerHTML = `<div class="muted">Raw facts unavailable: ${esc(e.message)}</div>`; }
}


async function loadCallRecords(cid){
  const body = $("callRecordsBody"); if(!body) return;
  try{
    const data = await api(`/v1/customers/${cid}/call-records`);
    const rows = data.records || [];
    if(!rows.length){
      body.innerHTML = `<div class="call-empty"><span>◌</span><div><b>No completed call transcript yet</b><p>After a Video Assist call ends, the role-tagged transcript and AI event record will appear here automatically.</p></div></div>`;
      return;
    }
    body.innerHTML = `<div class="call-record-list">${rows.map(r=>{
      const when = r.ended_at || r.started_at || '';
      const scope = String(r.capture_scope||'').replaceAll('_',' ');
      return `<article class="call-record-card">
        <div class="call-record-icon">🎙</div>
        <div class="call-record-main">
          <div class="call-record-top"><div><b>${esc(r.headline||'AI-assisted customer call')}</b><span>${esc(r.record_id||'')}</span></div><time>${esc(when ? new Date(when).toLocaleString('en-IN') : 'Completed call')}</time></div>
          <div class="call-record-metrics"><span><b>${Number(r.transcript_turns||0)}</b> transcript turns</span><span><b>${Number(r.ai_event_count||0)}</b> AI/CRM events</span><span><b>${Number(r.crm_case_count||0)}</b> cases</span></div>
          <div class="call-record-scope">Capture: ${esc(scope)} · human review required before reuse</div>
        </div>
        <div class="call-record-actions"><button onclick="downloadCallRecord('${esc(r.record_id)}','txt')">Download TXT</button><button class="secondary" onclick="downloadCallRecord('${esc(r.record_id)}','json')">JSON</button></div>
      </article>`;
    }).join('')}</div>`;
  }catch(e){ body.innerHTML=`<div class="muted">Call records unavailable: ${esc(e.message)}</div>`; }
}

async function downloadCallRecord(recordId, format='txt'){
  try{
    const r = await fetch(TOOLAPI_URL + `/v1/call-records/${encodeURIComponent(recordId)}/download?format=${encodeURIComponent(format)}`, { headers:{"Authorization":"Bearer "+BEARER} });
    if(!r.ok) throw new Error(await r.text().catch(()=>String(r.status)));
    const blob = await r.blob();
    const url = URL.createObjectURL(blob); const a=document.createElement('a');
    a.href=url; a.download=`${recordId}.${format}`; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1500); toast(`Downloaded ${recordId}.${format}`);
  }catch(e){ toast('Transcript download failed: '+e.message); }
}

/* ===================== RM ASSIST (the "after AI" guided journey) ===================== */
const RM_JOURNEY = [
  { key:'thesis',   n:1, level:'Foundational', title:'Progressive Customer Thesis & Daily Briefing',
    blurb:'AI builds the relationship thesis in five chapters: baseline, change detection, posture, conversation and committed action. Nothing downstream is shown before the preceding decision is resolved.',
    adds:'A cumulative, evidence-backed story with no duplicate insight dump.' },
  { key:'personas', n:2, level:'Structuring', title:'Household & Stakeholder Map',
    blurb:'AI structures the people around the banking decision — the customer, co-applicant/spouse, nominee — into who to talk to and how, with disposition and influence.',
    adds:'Turns a contact list into a household influence map with a talking-stance per person.' },
  { key:'strategy', n:3, level:'Decisioning', title:'Relationship Strategy & Next-Best-Action',
    blurb:'The retail-RM brain: AI fuses account conduct, CIBIL, deterministic eligibility and the retail SOPs into ONE prioritised action — the single best move now, the exact talk-track, a guardrail on what NOT to promise, and a do-not-offer list so a stressed account is never mis-sold a higher limit. For a dispute or collections case it leads with service recovery, not upsell.',
    adds:'Eligibility-gated, SOP-grounded next-best-action with spoken talk-tracks — not raw signals to interpret.' },
  { key:'collateral', n:4, level:'Activation', title:'Personalised Offer / Outreach',
    blurb:'AI turns an eligible opportunity (pre-approved loan, card upgrade, wealth, insurance) into a personalised, consent- and eligibility-checked outreach — blocked offers are suppressed with a reason.',
    adds:'Generates compliant, personalised retail outreach the core CRM cannot.' },
  { key:'planner',  n:5, level:'Orchestration', title:'Call Plan - Pre-Call Brief',
    blurb:'Your game plan for the imminent video call: the objective, talking points grounded in this customer data, the likely objections and how to handle them, the one ask, and what NOT to promise.',
    adds:'A focused pre-call brief - talking points, objection handling, the ask and guardrails - not a generic day plan.' },
  { key:'capstone', n:6, level:'Real-time', title:'Live Video Call Copilot Handoff',
    blurb:'The capstone: carry everything above into a live Teams video call. The customer joins from the Video Assist app; RM-only synopsis and real-time, evidence-grounded nudges (CIBIL, card utilisation, EMIs, the open dispute) stream into Teams — never shown to the customer. The Case-Dispute RM can log a chargeback case live.',
    adds:'Real-time in-call video assistance grounded on this customer’s retail evidence + SOPs.' },
  { key:'schedule', n:7, level:'Self-service', title:'Customer self-service scheduling',
    blurb:'From the Contoso mobile banking app the customer taps “Video call your RM”. The RM-side Teams meeting link is generated automatically and a meeting request lands in your Teams; ~60 seconds later a Join button appears in the customer app and drops them into the same secure ACS↔Teams call. The customer never sees a meeting link. A slot-based booking page is also available.',
    adds:'Customer-initiated, one-tap instant video call with an auto-provisioned Teams link — no phone tag, no link sharing, no admin setup.' },
];

/* ---- shared next-best-action (one fetch per customer; de-dupes hero + strategy + planner) ---- */
async function getNBA(cid){
  if(window.__NBA && window.__NBA.cid===cid && window.__NBA.data) return window.__NBA.data;
  const data = await api(`/v1/customers/${cid}/next-best-action`);
  window.__NBA = { cid, data };
  return data;
}
function regenerateStrategy(cid){ window.__NBA = null; loadStrategy(cid); mountOutcomeHero(cid); }

/* ---- Outcome-first hero: the single AI-decided outcome, pinned above the whole journey ---- */
async function mountOutcomeHero(cid){
  const host = document.getElementById('outcomeHero'); if(!host) return;
  const render = (n)=>{
    const plays = n.plays||[];
    const top = plays.find(p=>String(p.eligibility||'').toLowerCase()!=='blocked') || plays[0] || null;
    const dno = n.do_not_offer||[]; const cases = n.open_cases||[];
    const eligibleCount = plays.filter(p=>String(p.eligibility||'').toLowerCase()!=='blocked').length;
    const name = (window.__C && window.__C.d && window.__C.d.customer && window.__C.d.customer.display_name) || cid;
    host.innerHTML = `
      <div class="oc-hero rx-reveal">
        <div class="oc-main">
          <div class="oc-eyebrow">Today's AI-driven outcome &middot; ${esc(name)}</div>
          <h2 class="oc-stance">${esc(n.headline || (n.stance ? n.stance+' \u2014 relationship outcome' : 'Relationship outcome'))}</h2>
          ${n.relationship_read?`<p class="oc-read">${esc(n.relationship_read)}</p>`:''}
          ${top?`<div class="oc-move">
            <div class="oc-move-h"><span class="oc-tag">The one move now</span><b>${esc(top.title||top.product||'Recommended play')}</b>${top.the_number?`<span class="oc-num">${esc(String(top.the_number))}</span>`:''}</div>
            ${top.say?`<blockquote class="oc-say">${esc(top.say)}</blockquote>`:''}
            ${top.guardrail?`<div class="oc-guard"><span>Guardrail</span> ${esc(top.guardrail)}</div>`:''}
          </div>`:'<div class="oc-move"><div class="oc-move-h"><span class="oc-tag">Focus now</span><b>Service recovery first</b></div><blockquote class="oc-say">No eligible product play on a stressed account &mdash; resolve open issues before any offer.</blockquote></div>'}
          <div class="oc-cta">
            <button class="oc-btn primary" onclick="heroGoto(2)">See how the AI got here &rarr;</button>
            <button class="oc-btn" onclick="heroGoto(5)">Go straight to the live call &rarr;</button>
          </div>
        </div>
        <aside class="oc-side">
          <div class="oc-chip"><b>${esc(n.stance||'\u2014')}</b><span>AI stance</span></div>
          <div class="oc-chip pos"><b>${eligibleCount}/${plays.length}</b><span>eligible plays</span></div>
          <div class="oc-chip neg"><b>${dno.length}</b><span>do-not-offer</span></div>
          <div class="oc-chip warn"><b>${cases.length}</b><span>open cases</span></div>
        </aside>
      </div>`;
    if(RX && RX.reveal) RX.reveal(host, '.rx-reveal');
  };
  if(window.__NBA && window.__NBA.cid===cid && window.__NBA.data){ render(window.__NBA.data); return; }
  host.innerHTML = `<div class="oc-hero loading-hero"><div class="rx-gen"><span class="dot"></span> Reading today's AI-driven outcome\u2026</div></div>`;
  try{ render(await getNBA(cid)); }
  catch(e){ host.innerHTML = `<div class="oc-hero loading-hero"><div class="oc-eyebrow">Today's outcome</div><p class="oc-read">Outcome unavailable: ${esc(e.message)}</p></div>`; }
}

function renderRmAssistMode(){
  const { cmd } = window.__C;
  if(window.__C.step == null) window.__C.step = 0;
  const steps = RM_JOURNEY.map((s,i)=>`
    <button class="journey-node ${i===window.__C.step?'active':''} ${i<window.__C.step?'done':''}" onclick="gotoStep(${i})">
      <span class="jn-num">${i<window.__C.step?'✓':s.n}</span>
      <span class="jn-meta"><b>${esc(s.title)}</b><em>${esc(s.level)}</em></span>
    </button>${i<RM_JOURNEY.length-1?'<span class="jn-link"></span>':''}`).join('');
  $("modebody").innerHTML = `
    <div class="rm-banner"><span class="ic">✦</span> <b>RM Assist</b> — the same customer, after AI. Walk the journey left → right if you want the full evidence. The AI has already set today's outcome (shown above) and the single best move to make now.</div>
    <div class="journey-outcome" id="outcomeHero"></div>
    <div class="journey-rail">${steps}</div>
    <div class="journey-stage" id="journeyStage"></div>`;
  renderJourneyStep();
  mountOutcomeHero(window.__C.cid);
}

function gotoStep(i){
  if(window.__C && window.__C.step===0 && i>0 && !isRelationshipStoryComplete(window.__C.cid)){
    toast('Complete the five Customer Thesis chapters before moving ahead'); return;
  }
  window.__C.step = Math.max(0, Math.min(RM_JOURNEY.length-1, i)); renderRmAssistMode();
}
function nextStep(){ if(window.__C.step < RM_JOURNEY.length-1){ window.__C.step++; renderRmAssistMode(); } }
function prevStep(){ if(window.__C.step > 0){ window.__C.step--; renderRmAssistMode(); } }
/* hero CTAs jump straight to the outcome-bearing steps (strategy / live call), bypassing the thesis gate */
function heroGoto(i){ if(!window.__C) return; window.__C.step = Math.max(0, Math.min(RM_JOURNEY.length-1, i)); renderRmAssistMode(); }

function renderJourneyStep(){
  const { d, ds, pb, cmd, cid } = window.__C;
  const s = RM_JOURNEY[window.__C.step];
  const stage = $("journeyStage");
  const head = `
    <div class="stage-head">
      <div><div class="stage-level">Step ${s.n} of ${RM_JOURNEY.length} · ${esc(s.level)}</div><h3 class="stage-title">${esc(s.title)}</h3><p class="stage-blurb">${esc(s.blurb)}</p></div>
      <div class="stage-adds"><span>RM Assist adds</span>${esc(s.adds)}</div>
    </div>`;
  let body = '';
  if(s.key==='thesis')      body = `<div id="relationshipStory" class="relationship-story-host">${renderRelationshipStoryShell(cid)}</div>`;
  else if(s.key==='search')  body = `<div class="panel"><h3>Grounded RAG Search <span class="muted">scoped to ${esc(d.customer.display_name)}</span></h3><div id="searchMount"></div></div>`;
  else if(s.key==='planner') body = '<div class="panel"><h3>Call Plan - Pre-Call Brief</h3><div id="plannerMount"><div class="loading">Building your call plan...</div></div></div>';
  else if(s.key==='personas') body = `<div class="grid"><section class="panel col-6"><h3>Stakeholder Map</h3><p class="muted">Click a stakeholder to generate, live, how the RM should position to that persona.</p><div id="personaTree" class="loading">Mapping stakeholders…</div></section><section class="panel col-6 spotlight"><h3>Persona Narrative <span class="muted" id="personaWho"></span></h3><div id="personaNarrative"><div class="muted">Select a stakeholder.</div></div></section></div>`;
  else if(s.key==='opps')   { const e=(ds.opportunities||[]).filter(o=>o.eligible), b=(ds.opportunities||[]).filter(o=>!o.eligible);
                              body = `<div class="grid"><section class="panel col-6"><h3>Eligible Opportunities</h3>${renderOpportunities(e,b)}</section><section class="panel col-6"><h3>Early Warning Signals</h3>${renderEws(ds.ews, (window.__C&&window.__C.cid)||(d&&d.customer&&d.customer.customer_id))}</section></div>`; }
  else if(s.key==='breach') body = `<section class="panel risk-control-tower"><h3>AI Risk Control Tower <span class="muted">Breach + Income + RM action</span></h3><div id="riskCopilot"><div class="loading">Generating AI control-tower narrative…</div></div></section><div class="grid"><section class="panel col-7"><h3>Breach Radar <span class="muted" id="brBand">computing…</span></h3><div id="brRadar"><div class="loading">Computing breach trajectory…</div></div></section><section class="panel col-5 spotlight"><h3>Income Reconciliation</h3><div id="incRecon"><div class="loading">Triangulating GST · bank · turnover…</div></div></section></div><section class="panel stress-lab"><h3>Scenario Lab <span class="muted">click a preset or drag sliders</span></h3><div id="scenarioLab"><div class="loading">Preparing stress scenarios…</div></div></section>`;
  else if(s.key==='strategy') body = `<section class="panel"><h3>Relationship Strategy &amp; Next-Best-Action <span class="muted">eligibility-gated · SOP-grounded</span></h3><div id="nbaMount"><div class="loading">Computing the next best action…</div></div></section>`;
  else if(s.key==='collateral') body = `<div class="panel"><h3>Personalised Marketing Collateral</h3><div id="collateralMount"><div class="loading">Loading offers…</div></div></div>`;
  else if(s.key==='capstone') body = `
    <div class="panel capstone"><h3>Live Video Call Copilot</h3>
      <p class="ai-p">Everything you have seen — thesis, personas, and the next-best-action strategy — is carried into a live <b>video call</b>. The customer joins your Microsoft Teams meeting from the Video Assist app; the AI co-pilot streams the customer synopsis and real-time, eligibility-gated, SOP-grounded nudges (and complete factual answers) into your Teams chat — never to the customer. Context for <b>${esc(cid)}</b> is loaded automatically.</p>
      <div class="btn-row"><button class="btn primary" onclick="openVideoCall('${cid}')">Start Video Call (Teams)</button><button class="btn" onclick="draftMemo('${cid}')">Draft renewal memo</button><button class="btn" onclick="proposeTask('${cid}')">Propose follow-up task</button></div>
    </div>`;
  else if(s.key==='schedule') body = `
    <div class="panel"><h3>Customer self-service scheduling <span class="muted">customer-initiated · Teams</span></h3>
      <p class="ai-p">Instead of phone tag, the customer opens a booking page, sees <b>${esc(cid)}</b>’s slots for the next two working days, and picks a time. You’re notified instantly in your Teams chat. At the appointment, the customer joins the <b>same secure Teams video call</b> from the booking page — reusing the exact ACS↔Teams join from Step 6.</p>
      <div class="rx-cards" style="margin-top:6px">
        <article class="rx-card sev-pos"><div class="body"><div class="top"><div class="t"><b>1 · Customer taps in the app</b><span class="sum">In Contoso mobile banking, ${esc(cid)} taps “Video call your RM”</span></div></div></div></article>
        <article class="rx-card"><div class="body"><div class="top"><div class="t"><b>2 · RM notified in Teams</b><span class="sum">A meeting request lands in your chat with your join link — auto-created</span></div></div></div></article>
        <article class="rx-card sev-pos"><div class="body"><div class="top"><div class="t"><b>3 · One-tap join</b><span class="sum">~60s later a Join button appears; the customer never sees a link</span></div></div></div></article>
      </div>
      <div class="btn-row" style="margin-top:14px"><button class="btn primary" onclick="openCustomerApp('${cid}')">Open customer mobile app</button><button class="btn" onclick="copyCustomerAppLink('${cid}')">Copy app link to share</button><button class="btn" onclick="openScheduling('${cid}')">Slot-based booking page</button></div>
      <p class="muted" style="margin-top:10px">The RM-side Teams meeting link is generated automatically (a Power Automate flow you own via SCHEDULE_WEBHOOK_URL, or your standing meeting via RM_MEETING_URL). The customer only ever sees a “Join call” button — never the link.</p>
    </div>`;
  const nav = `
    <div class="journey-nav">
      <button class="btn ghost" onclick="prevStep()" ${window.__C.step===0?'disabled':''}>← Previous</button>
      <span class="jn-progress">${RM_JOURNEY.map((_,i)=>`<i class="${i===window.__C.step?'on':''} ${i<window.__C.step?'done':''}"></i>`).join('')}</span>
      ${window.__C.step<RM_JOURNEY.length-1?`<button class="btn primary" id="journeyNextButton" onclick="nextStep()" ${s.key==='thesis'&&!isRelationshipStoryComplete(cid)?'disabled':''}>Next: ${esc(RM_JOURNEY[window.__C.step+1].title)} →</button>`:`<button class="btn" onclick="setMode('core')">↩ Back to Core CRM</button>`}
    </div>`;
  stage.innerHTML = head + `<div class="stage-body">${body}</div>` + nav;
  animateCounters(stage);
  // mount the interactive features
  if(s.key==='personas') loadPersonaTree(cid);
  if(s.key==='breach') loadBreachRadar(cid);
  if(s.key==='collateral') loadCollateral(cid, 'collateralMount');
  if(s.key==='search') mountSearch('searchMount', cid);
  if(s.key==='planner') mountCallPlan('plannerMount', cid);
  if(s.key==='thesis'){ const st=relState(cid); loadRelationshipStory(cid,st.stage||1,false); }
  if(s.key==='strategy') loadStrategy(cid);
}

function switchTab(name){
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.tab===name));
  const { d, ds, pb, cid } = window.__C;
  const body = $("tabbody");
  body.style.opacity = 0; body.style.transform = "translateY(6px)";
  setTimeout(()=>{ body.innerHTML = TABS[name](d, ds, pb, cid); body.style.opacity = 1; body.style.transform = "none";
    animateCounters(body);
    if(name==='playbook') loadAiPlaybook(cid);
    if(name==='personas') loadPersonaTree(cid);
    if(name==='conduct') animateDuoBars(body);
    if(name==='breach') loadBreachRadar(cid);
    if(name==='documents') {/* chat input is in DOM now */}
  }, 130);
}

/* sparkline/bar grow-in for the conduct chart */
function animateDuoBars(scope){
  (scope||document).querySelectorAll(".duo i, .duo b").forEach((el,i)=>{
    const h = el.style.height; el.style.height = "0%";
    setTimeout(()=>{ el.style.height = h; }, 40 + i*16);
  });
}


function renderCommandCockpit(cmd){
  const r = cmd.credit_readiness || {}; const missions = cmd.today_missions || [];
  const score = Number(r.score || 0); const circumference = 2 * Math.PI * 42; const dash = (score/100) * circumference;
  const pendingDocs = (cmd.document_pack?.pending || []).slice(0,4).map(d=>`<span class="evidence-chip ${d.blocking_flag==='Y'?'hot':''}">${esc(d.document_type)} · ${esc(d.status)}</span>`).join('') || '<span class="evidence-chip">No critical document blocker</span>';
  const lanes = cmd.opportunity_workbench?.summary || {};
  const laneHtml = Object.entries(lanes).map(([k,v])=>`<div class="lane-stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join('');
  return `
    <section class="command-deck">
      <div class="thesis-card">
        <div class="eyebrow">Relationship thesis · AI command cockpit</div>
        <h3>${esc(cmd.relationship_thesis)}</h3>
        <div id="dynThesis" class="dyn-thesis">${skelPanel("Generating live relationship thesis\u2026", 5)}</div>
        <div class="evidence-row">${(cmd.evidence_refs||[]).map(x=>`<span class="evidence-chip">${esc(x)}</span>`).join('')}</div>
      </div>
      <div class="readiness-card">
        <svg class="ring rb-${(score>=55?'good':score>=35?'mid':'low')}" viewBox="0 0 100 100"><circle cx="50" cy="50" r="42"/><circle class="progress" cx="50" cy="50" r="42" style="stroke-dasharray:0 ${circumference}" data-dash="${Math.max(dash, score>0?dash:6)} ${circumference}"/></svg>
        <div class="ring-text"><b data-count="${score}">0</b><span>/100 credit readiness</span></div>
        <p>${esc(r.label||'')}</p>
      </div>
      <div class="mission-board">
        <div class="board-head"><b>Today’s mission board</b><span>${missions.length} missions</span></div>
        ${missions.slice(0,5).map((m,i)=>`<div class="mission ${String(m.severity||'').toLowerCase()}" onclick="loadMissionAction('${esc(cmd.customer_id||'')}','${esc((m.title||'').replace(/'/g,''))}','${esc((m.kind||'').replace(/'/g,''))}',${i})"><span>${esc(m.kind)}</span><b>${esc(m.title)}</b><p>${esc(m.why_now)}</p><em>${esc(m.target_outcome)}</em><div class="mission-ai" id="missionAi-${i}"></div><span class="mission-cta">▸ how to action this</span></div>`).join('')}
      </div>
      <div class="mini-workbench"><div class="board-head"><b>Opportunity workbench</b><span>live view</span></div>${laneHtml}<div class="docs-row">${pendingDocs}</div></div>
    </section>
    <section class="sequence-strip">
      ${(cmd.recommended_sequence||[]).map(s=>`<div class="seq-step"><span>${esc(s.step)}</span><b>${esc(s.label)}</b><p>${esc(s.instruction)}</p><details><summary>Why</summary>${esc(s.why)}</details></div>`).join('')}
    </section>`;
}

const TABS = {
  playbook: (d,ds,pb,cid)=>`
    <section class="panel spotlight"><h3>AI RM Playbook <span class="muted" id="pbMode">generating live…</span></h3>
      <div id="aiPlaybook">${skelPanel("Generating today\u2019s conversation playbook\u2026", 4)}</div>
    </section>
    <section class="panel"><h3>Structured Talk Tracks (deterministic)</h3>${renderPlaybook(pb)}</section>`,
  personas: (d,ds,pb,cid)=>`
    <section class="panel"><h3>Stakeholder Map · ${esc(d.customer.display_name)}</h3>
      <p class="muted">Click a stakeholder to generate, live, how the RM should position the relationship to that persona.</p>
      <div id="personaTree" class="loading">Loading stakeholder org…</div>
    </section>
    <section class="panel spotlight"><h3>Persona Narrative <span class="muted" id="personaWho"></span></h3>
      <div id="personaNarrative"><div class="muted">Select a stakeholder above.</div></div>
    </section>`,
  conduct: (d,ds,pb,cid)=>{
    const cs=d.conduct_summary, f=d.primary_facility;
    const months=Object.keys(cs.monthly_credits||{}), credits=Object.values(cs.monthly_credits||{}), debits=Object.values(cs.monthly_debits||{});
    const maxV=Math.max(...credits,...debits,1);
    return `<div class="grid">
      <section class="panel col-8"><h3>Monthly Credits vs Debits · FY2025-26</h3><div class="dual-chart">${months.map((m,i)=>`<div class="duo" title="${m}: CR ${fmtINR(credits[i])} / DR ${fmtINR(debits[i])}"><i style="height:${Math.max(3,(credits[i]/maxV)*100)}%"></i><b style="height:${Math.max(3,((debits[i]||0)/maxV)*100)}%"></b><span>${m.slice(5)}</span></div>`).join('')}</div></section>
      <section class="panel col-4"><h3>Conduct Metrics</h3>${metric('Avg monthly credit',fmtINR(cs.avg_monthly_credit_inr))}${metric('Days > 85%',cs.days_over_85_pct)}${metric('Cash intensity',cs.cash_intensity_pct+'%',cs.cash_intensity_pct>10?'down':'')}${metric('Cheque returns',cs.cheque_return_count,cs.cheque_return_count>1?'down':'')}${metric('Top buyer concentration',cs.top_counterparty_concentration_pct+'%',cs.top_counterparty_concentration_pct>35?'warn':'')}</section>
      <section class="panel col-4"><h3>Customer Snapshot</h3>${metric('PAN / GST',`${d.customer.pan_masked} · ${d.customer.gstin_masked}`)}${metric('Risk / KYC',`${d.customer.risk_category} · ${d.customer.kyc_status}`)}${metric('Facility',`${f.facility_type} · ${fmtINR(f.sanction_limit_inr)} · review ${f.review_due_date}`)}${metric('Promoters',(d.promoters||[]).map(x=>x.name).join(', '))}${metric('RVS',d.customer.relationship_value_score)}</section>
      <section class="panel col-8"><h3>Engagement Threads (multiple flows)</h3>${renderThreads(ds)}</section>
    </div>`;
  },
  opps: (d,ds,pb,cid)=>{
    const elig=(ds.opportunities||[]).filter(o=>o.eligible), blk=(ds.opportunities||[]).filter(o=>!o.eligible);
    return `<div class="grid"><section class="panel col-6"><h3>Cross-sell / Upsell</h3>${renderOpportunities(elig,blk)}</section><section class="panel col-6"><h3>Early Warning Signals</h3>${renderEws(ds.ews, (window.__C&&window.__C.cid)||(d&&d.customer&&d.customer.customer_id))}</section></div>`;
  },
  breach: (d,ds,pb,cid)=>`
    <section class="panel risk-control-tower"><h3>AI Risk Control Tower <span class="muted">Breach + Income + RM action</span></h3><div id="riskCopilot"><div class="loading">Generating AI control-tower narrative…</div></div></section>
    <div class="grid">
      <section class="panel col-7"><h3>Breach Radar <span class="muted" id="brBand">computing live…</span></h3><div id="brRadar"><div class="loading">Computing covenant headroom and breach trajectory…</div></div></section>
      <section class="panel col-5 spotlight"><h3>Income Reconciliation</h3><div id="incRecon"><div class="loading">Triangulating GST · bank · turnover…</div></div></section>
    </div><section class="panel stress-lab"><h3>Scenario Lab <span class="muted">click a preset or drag sliders</span></h3><div id="scenarioLab"><div class="loading">Preparing stress scenarios…</div></div></section>`,
  relationship: (d,ds,pb,cid)=>`<div class="grid"><section class="panel col-6"><h3>CRM Timeline &amp; Use Cases</h3>${renderTimeline(ds.crm_timeline)}</section><section class="panel col-6"><h3>Service Recovery &amp; Tasks</h3>${renderServiceRecovery(ds.crm_timeline)}</section></div>`,
  transactions: (d,ds,pb,cid)=>`<div class="grid"><section class="panel col-7"><h3>Granular Transaction Feed</h3>${renderTransactions(ds.recent_transactions)}</section><section class="panel col-5"><h3>GST, Aging &amp; Counterparties</h3>${renderFinancials(ds)}</section></div>`,
  documents: (d,ds,pb,cid)=>`<div class="grid"><section class="panel col-6"><h3>Documents &amp; Covenants</h3>${renderDocuments(ds)}</section><section class="panel col-6"><h3>Policy Assistant · Grounded</h3><div class="chat-log" id="chatLog"><div class="muted">Ask about Contoso MSME policy. Answers cite indexed SOPs only.</div></div><div class="chat-in"><input id="chatIn" placeholder="e.g. documents needed for renewal?" onkeydown="if(event.key==='Enter')askPolicy()" /><button class="btn" onclick="askPolicy()">Ask</button></div></section></div>`,
};

async function loadAiPlaybook(cid){
  try{
    const r = await api(`/v1/briefing/playbook/${cid}`);
    if($("pbMode")) $("pbMode").textContent = r.mode==='ai' ? 'live · AI' : 'fallback (AI engine unavailable)';
    if($("aiPlaybook")) $("aiPlaybook").innerHTML = renderPlaybookStruct(r.structured);
  }catch(e){ if($("aiPlaybook")) $("aiPlaybook").innerHTML = `<div class="muted">Playbook unavailable: ${esc(e.message)}</div>`; }
}

function renderPlaybookStruct(s){
  if(!s) return '<div class="muted">No playbook.</div>';
  const seq = (s.conversation_sequence||[]).map(step=>`
    <div class="narr-item">
      <div class="ni-main"><span class="ni-order">${esc(step.order)}</span><div><b>${esc(step.thread)}</b><p>${esc(step.what_to_do)}</p></div></div>
      ${step.reasoning?`<details class="why"><summary>Why this, now</summary><p>${esc(step.reasoning)}</p></details>`:''}
    </div>`).join('');
  const push = (s.likely_pushback||[]).map(p=>`
    <div class="narr-item">
      <div class="ni-main"><span class="ni-ic">⚡</span><div><b>${esc(p.pushback)}</b><p>${esc(p.response)}</p></div></div>
      ${p.reasoning?`<details class="why"><summary>Why they react this way</summary><p>${esc(p.reasoning)}</p></details>`:''}
    </div>`).join('');
  return `
    <div class="narr-open">${esc(s.opening_read||'')}</div>
    <div class="narr-label">Conversation sequence</div>${seq}
    ${push?`<div class="narr-label">Likely pushback</div>${push}`:''}
    <div class="narr-donot"><b>Critical — do not:</b> ${esc(s.critical_do_not||'')}
      ${s.do_not_reasoning?`<details class="why"><summary>Why</summary><p>${esc(s.do_not_reasoning)}</p></details>`:''}</div>`;
}

async function loadPersonaTree(cid){
  try{
    const r = await api(`/v1/customers/${cid}/stakeholders`);
    const nodes = r.stakeholders || [];
    const children = (pid)=>nodes.filter(n=>(n.parent_id||"")===(pid||""));
    const neg = (d)=>['anx','stress','frustr','irrit','defens'].some(x=>String(d).toLowerCase().includes(x));
    const renderNode = (n)=>`
      <li>
        <div class="ptnode" onclick="loadPersona('${cid}','${n.stakeholder_id}','${esc(n.name)} · ${esc(n.title)}')">
          <b>${esc(n.name)}</b><span>${esc(n.title)}</span>
          <em class="role">${esc(n.decision_role)} · ${esc(n.influence)} influence</em>
          <em class="disp ${neg(n.disposition)?'neg':'pos'}">${esc(n.disposition)}</em>
        </div>
        ${children(n.stakeholder_id).length?`<ul>${children(n.stakeholder_id).map(renderNode).join('')}</ul>`:''}
      </li>`;
    const roots = children(null);
    $("personaTree").className = "persona-tree";
    $("personaTree").innerHTML = `<ul class="tree-root">${roots.map(renderNode).join('')}</ul>`;
  }catch(e){ if($("personaTree")){$("personaTree").className='';$("personaTree").innerHTML=`<div class="muted">Stakeholders unavailable: ${esc(e.message)}</div>`;} }
}

let PERSONA_PATHS = null;
async function loadPersona(cid, sid, who){
  const ev = window.event;
  $("personaWho").textContent = '· ' + who + ' · simulating conversations live…';
  $("personaNarrative").innerHTML = skelPanel("Simulating 3 grounded conversation paths \u2014 happy, neutral and friction\u2026", 5);
  document.querySelectorAll(".ptnode").forEach(n=>n.classList.remove("sel"));
  if(ev && ev.currentTarget) ev.currentTarget.classList.add("sel");
  try{
    const r = await api(`/v1/customers/${cid}/persona-paths/${sid}`);
    PERSONA_PATHS = r.paths || [];
    const live = r.generated_by==='llm_grounded';
    $("personaWho").textContent = '· ' + who + (live?' · live':' · fallback');
    if(!PERSONA_PATHS.length){ $("personaNarrative").innerHTML = '<div class="muted">No paths generated.</div>'; return; }
    const order = {happy:0, neutral:1, friction:2};
    PERSONA_PATHS.sort((a,b)=>(order[a.path]??9)-(order[b.path]??9));
    const tag = live ? '<span class="ai-badge live">● AI · grounded in customer data</span>' : '<span class="ai-badge muted">rule-based</span>';
    const altMap = {SENIOR_DECISION_MAKER:'Strategic · senior decision-maker', FINANCE_FUNCTION:'Financial detail · finance function', OPERATIONS_FUNCTION:'Operational · operations/collections', SUPPORT_FUNCTION:'Focused · support function'};
    const altLabel = altMap[r.altitude] ? `<span class="altitude-pill">${esc(altMap[r.altitude])}</span>` : '';
    const tabs = PERSONA_PATHS.map((p,i)=>`<button class="path-tab pt-${esc(p.path)} ${i===0?'on':''}" onclick="showPath(${i},this)"><span class="pt-dot"></span>${esc(p.label||p.path)}</button>`).join('');
    $("personaNarrative").innerHTML = `
      <div class="paths-head">${tag}${altLabel}<span class="paths-hint">Same customer data · three ways the conversation can go · pitched to this person's role</span></div>
      <div class="path-tabs">${tabs}</div>
      <div id="pathStage"></div>`;
    showPath(0);
  }catch(e){ $("personaNarrative").innerHTML = `<div class="muted">Conversation simulation unavailable: ${esc(e.message)}</div>`; }
}

function showPath(i, btn){
  const p = (PERSONA_PATHS||[])[i]; if(!p) return;
  document.querySelectorAll(".path-tab").forEach((b,bi)=>b.classList.toggle("on", btn?b===btn:bi===i));
  const stage = $("pathStage"); if(!stage) return;
  const cls = {happy:'p-happy', neutral:'p-neutral', friction:'p-friction'}[p.path]||'p-neutral';
  stage.className = 'path-stage ' + cls;
  stage.innerHTML = `
    <div class="path-summary"><span class="path-badge ${cls}">${esc((p.path||'').toUpperCase())}</span> ${esc(p.summary||'')}</div>
    <div class="convo" id="convoFlow"></div>
    <div class="path-foot">
      ${p.rm_technique?`<div class="pf-tech"><b>RM technique</b> ${esc(p.rm_technique)}</div>`:''}
      ${p.outcome?`<div class="pf-out"><b>Outcome</b> ${esc(p.outcome)}</div>`:''}
    </div>`;
  // animate the dialogue turn-by-turn
  const flow = $("convoFlow"); const turns = p.turns||[];
  turns.forEach((t,ti)=>{
    const isRM = String(t.speaker||'').toLowerCase()==='rm';
    const bubble = document.createElement('div');
    bubble.className = 'turn ' + (isRM?'t-rm':'t-cust');
    bubble.style.animationDelay = (ti*0.5) + 's';
    bubble.innerHTML = `<span class="t-who">${esc(t.speaker||'')}</span><p>${esc(t.text||'')}</p>`;
    flow.appendChild(bubble);
  });
}

/* ===================== TOP-LEVEL RM ASSIST JOURNEY (demo spine) ===================== */
async function loadJourneyFlow(){
  if(!QUEUE_CACHE.length){ try{ const d = await api("/v1/portfolio/priority-queue"); QUEUE_CACHE = d.queue||[]; }catch(e){} }
  const picker = QUEUE_CACHE.map(c=>`<option value="${c.customer_id}">${esc(c.display_name)} · ${esc(c.bucket)}</option>`).join("");
  const ladder = RM_JOURNEY.map((s,i)=>`
    <div class="ladder-step" style="animation-delay:${i*0.05}s">
      <div class="ls-rung">${s.n}</div>
      <div class="ls-body"><div class="ls-level">${esc(s.level)}</div><b>${esc(s.title)}</b><p>${esc(s.blurb)}</p><div class="ls-adds"><span>RM Assist adds</span> ${esc(s.adds)}</div></div>
    </div>${i<RM_JOURNEY.length-1?'<div class="ls-connect"></div>':''}`).join("");
  $("content").innerHTML = `
    <div class="hero">
      <div><div class="eyebrow">The demo spine · simple → complex</div><h2>RM Assist Journey</h2>
      <p>Eight capabilities, layered in increasing complexity — from a plain-language relationship thesis to a live, real-time call copilot. Pick a customer and walk the full progression end to end.</p></div>
      <div class="journey-launch">
        <label class="jl-label">Run the journey for</label>
        <select id="journeyCust" class="select">${picker}</select>
        <button class="btn primary" onclick="startJourneyFor()">Start journey →</button>
      </div>
    </div>
    <div class="ladder">${ladder}</div>`;
  animateCounters($("content"));
}

async function startJourneyFor(){
  const cid = $("journeyCust") ? $("journeyCust").value : (QUEUE_CACHE[0] && QUEUE_CACHE[0].customer_id);
  if(!cid){ toast("No customer available"); return; }
  CURRENT = cid;
  setCrumb('RM Assist Journey');
  $("content").innerHTML = `<div class="hero"><div><div class="eyebrow">Loading journey</div><div class="skel skel-line" style="width:320px;height:26px;margin-top:8px"></div></div></div>${skelCards(2)}`;
  try{
    const [d360, dossier, playbook, command] = await Promise.all([
      api(`/v1/customers/${cid}/360`),
      api(`/v1/customers/${cid}/relationship-dossier`),
      api(`/v1/customers/${cid}/live-call-playbook`),
      api(`/v1/customers/${cid}/command-center`),
    ]);
    LAST_DOSSIER = dossier;
    const c = d360.customer, p = d360.business_profile;
    const eligibleOps = (dossier.opportunities||[]).filter(o=>o.eligible), blockedOps = (dossier.opportunities||[]).filter(o=>!o.eligible);
    window.__C = { d:d360, ds:dossier, pb:playbook, cmd:command, cid, eligibleOps, blockedOps, step:0, journeyTop:true };
    $("content").innerHTML = `
      <div class="customer-hero lift">
        <div><div class="eyebrow">${esc(c.customer_id)} · RM Assist Journey</div><h2>${esc(c.display_name)}</h2>
        <p>${esc(p.industry_description)} · walking all 8 capabilities, simple → complex</p></div>
        <button class="btn ghost" onclick="menuNav('journey')">↩ Choose another customer</button>
      </div>
      <div id="modebody"></div>`;
    renderRmAssistMode();
  }catch(e){ $("content").innerHTML = `<div class="loading">Failed to start journey: ${esc(e.message)}</div>`; }
}
/* ===================== TOP-LEVEL RM ASSIST JOURNEY end ===================== */

/* ===================== RM WORKSPACE: SEARCH / PLANNER / COLLATERAL ===================== */
const RM_ID = "RM-1042";

/* ---- unified RAG search (mounted in the journey, scoped to the customer) ---- */
let SEARCH_SCOPE = "customer";
const SEARCH_SCOPES = {
  customer: { label: "This customer", desc: "Reasons over Kaveri's own data (PII-masked) — conduct, covenants, receivables, eligibility.",
    ph: "e.g. is this account heading for a covenant breach? · what's driving the declining credits?",
    examples: ["Is this account heading for a covenant breach?", "What is driving the declining credits?", "Are they eligible for an enhancement, and why not?"] },
  product: { label: "Product", desc: "Matches the customer's profile to the product catalogue and explains the fit.",
    ph: "e.g. which working-capital product fits them best? · is invoice discounting suitable?",
    examples: ["Which working-capital product fits them best?", "Is invoice discounting suitable here?", "What would POS / digital collections do for them?"] },
  policy: { label: "Policy", desc: "Searches the Contoso SOP / policy index and answers with the cited clause.",
    ph: "e.g. documents required for OD renewal? · cheque-return classification policy? · KYC refresh rule?",
    examples: ["What documents are required for an OD renewal?", "What is the cheque-return classification policy?", "When must KYC be refreshed for a partnership?"] },
  all: { label: "All", desc: "Combines the customer's data, product fit and policy into one grounded answer.",
    ph: "e.g. can we enhance their limit, and what does policy require to do it?",
    examples: ["Can we enhance their limit, and what does policy require to do it?", "What should the RM do about the expired insurance, per policy?"] },
};
function mountSearch(mountId, cid){
  const mount = $(mountId); if(!mount) return;
  SEARCH_SCOPE = "customer";
  mount.innerHTML = `
    <p class="muted">Ask in plain language. RM Assist reasons over the customer's own data (PII-masked) <b>and</b> the SOP index, then answers with citations — pick a lens below.</p>
    <div class="search-scopes">
      ${Object.entries(SEARCH_SCOPES).map(([s,o],i)=>`<button class="qf-btn ${i===0?'on':''}" data-scope="${s}" onclick="setSearchScope('${s}',this)">${o.label}</button>`).join("")}
    </div>
    <div id="scopeDesc" class="scope-desc">${esc(SEARCH_SCOPES.customer.desc)}</div>
    <div class="search-bar"><input id="ragQ" placeholder="${esc(SEARCH_SCOPES.customer.ph)}" onkeydown="if(event.key==='Enter')runSearch('${cid}')"><button class="btn primary" onclick="runSearch('${cid}')">Search</button></div>
    <div id="scopeExamples" class="scope-examples">${SEARCH_SCOPES.customer.examples.map(x=>`<button class="ex-chip" onclick="useExample(this,'${cid}')">${esc(x)}</button>`).join("")}</div>
    <div id="ragResults"><div class="muted" style="margin-top:14px">Pick a lens, tap an example or type your own, and hit Search.</div></div>`;
}
function setSearchScope(s, btn){
  SEARCH_SCOPE = s;
  document.querySelectorAll(".search-scopes .qf-btn").forEach(b=>b.classList.toggle("on", b===btn));
  const o = SEARCH_SCOPES[s]||SEARCH_SCOPES.customer;
  const cid = (window.__C&&window.__C.cid)||CURRENT;
  const d=$("scopeDesc"); if(d) d.textContent = o.desc;
  const q=$("ragQ"); if(q){ q.placeholder = o.ph; q.value=""; }
  const ex=$("scopeExamples"); if(ex) ex.innerHTML = o.examples.map(x=>`<button class="ex-chip" onclick="useExample(this,'${cid}')">${esc(x)}</button>`).join("");
}
function useExample(btn, cid){ const q=$("ragQ"); if(q){ q.value = btn.textContent; } runSearch(cid); }
async function runSearch(cid){
  const q = $("ragQ").value.trim(); if(!q){ return; }
  cid = cid || CURRENT || null;
  $("ragResults").innerHTML = `<div class="ai-answer-box">${skelPanel("Reading this customer's data + policy and reasoning\u2026", 4)}</div>`;
  // 1) the grounded AI answer over customer data (PII-masked) + SOPs — the real RM-assist
  let aiHtml = '';
  if(cid){
    try{
      const a = await api(`/v1/customers/${cid}/assist-search?q=${encodeURIComponent(q)}&scope=${encodeURIComponent(SEARCH_SCOPE||'customer')}`);
      const tag = a.generated_by==='llm_grounded' ? '<span class="ai-badge">AI · grounded in customer data + policy · PII-masked</span>' : '<span class="ai-badge muted">retrieved evidence</span>';
      aiHtml = `<div class="ai-answer-box">${tag}
        <p class="ai-answer">${esc(a.answer||'')}</p>
        ${(a.customer_specifics&&a.customer_specifics.length)?`<div class="ai-sub"><b>For this customer</b><ul>${a.customer_specifics.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}
        ${(a.policy_points&&a.policy_points.length)?`<div class="ai-sub"><b>Policy</b><ul>${a.policy_points.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}
        ${(a.caveats&&a.caveats.length)?`<div class="ai-sub caveat"><b>Check before acting</b><ul>${a.caveats.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}
        ${(a.citations&&a.citations.length)?`<div class="cites">${a.citations.filter(Boolean).map(x=>`<span class="ref">${esc(x)}</span>`).join('')}</div>`:''}
      </div>`;
    }catch(e){ aiHtml = `<div class="ai-answer-box"><div class="muted">AI answer unavailable: ${esc(e.message)}</div></div>`; }
  }
  // 2) the structured retrieval hits underneath (kept for transparency)
  let hitsHtml = '';
  try{
    const r = await api("/v1/search", {method:"POST", body:JSON.stringify({query:q, scope:SEARCH_SCOPE, customer_id: cid, top_k:5})});
    if(r.results && r.results.length){
      const icon = {product:"▣", customer:"◉", policy:"§"};
      hitsHtml = `<div class="search-meta">Underlying sources · ${r.results.length} results</div>` +
        r.results.map(h=>`
          <div class="search-hit src-${esc(h.source)}">
            <div class="sh-head"><span class="sh-src">${icon[h.source]||'•'} ${esc(h.source)}</span><b>${esc(h.title||'')}</b><span class="ref">${esc(h.ref||'')}</span></div>
            ${h.snippet?`<p>${esc(h.snippet)}</p>`:''}
          </div>`).join("");
    }
  }catch(e){ /* hits optional */ }
  $("ragResults").innerHTML = aiHtml + hitsHtml;
}

/* ---- Call Plan & Objection Prep (mounted in the journey, step 5) ---- */
async function mountCallPlan(mountId, cid){
  const mount = $(mountId); if(!mount) return;
  mount.innerHTML = skelPanel("Building your pre-call game plan with the gated strategy\u2026", 5);
  try{
    const nba = await getNBA(cid);
    const plays = (nba.plays||[]).filter(p=>p.eligibility!=='blocked');
    const tp = plays.slice(0,3).map((p,i)=>`
      <div class="cp-block"><div class="er-label">${i+1}. ${esc(p.title||p.product||'Talking point')}${p.the_number?` &middot; <b>${esc(p.the_number)}</b>`:''}</div>
        ${p.rationale?`<p class="th-body">${esc(p.rationale)}</p>`:''}
        ${p.say?`<p class="th-why"><b>Say:</b> &ldquo;${esc(p.say)}&rdquo;</p>`:''}
        ${p.guardrail?`<p class="muted"><b>Don't promise:</b> ${esc(p.guardrail)}</p>`:''}</div>`).join('');
    const cases = (nba.open_cases||[]).map(c=>`<li><b>${esc(c.title||c.type||'Open item')}</b> &mdash; ${esc(c.status||'open')}</li>`).join('');
    const dno = (nba.do_not_offer||[]).map(d=>`<li><b>${esc(d.product||'')}</b> &mdash; ${esc(d.reason||'')}</li>`).join('');
    mount.innerHTML = `
      <div class="panel" style="margin-bottom:14px">
        <div class="er-label">Objective</div>
        <p class="th-headline">${esc(nba.headline||nba.stance||'Relationship action for this call')}</p>
        ${nba.relationship_read?`<p class="th-body"><b>Open with:</b> ${esc(nba.relationship_read)}</p>`:''}
      </div>
      <div class="grid">
        <section class="panel col-7"><h3>Your talking points</h3>${tp||'<div class="muted">No eligible plays right now — focus on service recovery.</div>'}</section>
        <section class="panel col-5">
          <h3>Likely pushback</h3>
          ${cases?`<div class="er-label">They may raise</div><ul class="cp-list">${cases}</ul>`:''}
          ${dno?`<div class="er-label">If they ask for these — decline (and why)</div><ul class="cp-list">${dno}</ul>`:''}
          ${(!cases&&!dno)?'<div class="muted">No open issues flagged.</div>':''}
          <div class="er-label" style="margin-top:12px">Guardrail</div>
          <p class="muted">Never commit a rate, limit or waiver on the call. Log anything unresolved as a CRM case.</p>
        </section>
      </div>
      <div class="btn-row" style="margin-top:14px"><button class="btn primary" onclick="openVideoCall('${cid}')">Start the video call &rarr;</button></div>`;
  }catch(e){ mount.innerHTML = `<div class="muted">Call plan unavailable: ${esc(e.message)}</div>`; }
}

/* ---- legacy DILO / MILO (no longer mounted; retained for reference) ---- */
function mountPlanner(mountId){
  const mount = $(mountId); if(!mount) return;
  mount.innerHTML = `
    <div class="modeswitch" style="margin-bottom:14px"><button class="ms-btn active" id="pl-dilo" onclick="setPlannerTab('dilo')"><span class="ic">◳</span> DILO · Plan</button><button class="ms-btn rm" id="pl-milo" onclick="setPlannerTab('milo')"><span class="ic">▤</span> MILO · Snapshot</button></div>
    <div id="plannerBody"><div class="loading">Loading…</div></div>`;
  setPlannerTab('dilo');
}
function setPlannerTab(t){
  $("pl-dilo")?.classList.toggle("active", t==='dilo'); $("pl-milo")?.classList.toggle("active", t==='milo');
  if(t==='dilo') renderDilo(); else renderMilo();
}
async function renderDilo(){
  $("plannerBody").innerHTML = skelCards(4);
  try{
    const d = await api(`/v1/rm/${RM_ID}/dilo`);
    const blocks = (d.time_blocks||[]).map(b=>`
      <div class="dilo-block b-${b.bucket==='Risk Watch'?'risk':b.bucket==='Growth'?'growth':'renewal'}">
        <div class="dilo-time">${esc(b.slot)}</div>
        <div class="dilo-main"><b>${esc(b.customer)}</b><span class="dilo-act">${esc(b.action)}</span><p>${esc(b.why||'')}</p><em>Prep: ${esc(b.prep)}</em></div>
        <div class="dilo-flags">${b.critical_signals?`<span class="pill crit">${b.critical_signals} critical</span>`:''}${b.blocking_documents?`<span class="pill">${b.blocking_documents} doc</span>`:''}</div>
      </div>`).join("");
    const due = (d.task_load?.due_items||[]).map(x=>`<div class="due-row ${x.overdue?'overdue':''}"><b>${esc(x.task)}</b><span>${esc(x.due_date)}${x.overdue?' · overdue':''}</span></div>`).join("");
    $("plannerBody").innerHTML = `
      <div id="diloAi" class="dilo-ai"><div class="loading">Reasoning over your day…</div></div>
      <div class="kpi-row">
        <div class="kpi"><span>Planned calls</span><strong data-count="${d.time_blocks.length}">0</strong><em>${esc(d.focus_theme)}</em></div>
        <div class="kpi"><span>High-priority tasks</span><strong data-count="${d.task_load.high_priority}">0</strong><em>of ${d.task_load.open_total} open</em></div>
        <div class="kpi"><span>Due ≤48h</span><strong data-count="${d.task_load.due_within_48h}">0</strong><em>SLA-sensitive</em></div>
        <div class="kpi"><span>Mode</span><strong>Suggested</strong><em>RM re-orders freely</em></div>
      </div>
      <div class="grid" style="margin-top:16px">
        <section class="panel col-8"><h3>Time-blocked plan</h3>${blocks||'<div class="muted">No calls planned.</div>'}</section>
        <section class="panel col-4"><h3>Due within 48h</h3>${due||'<div class="muted">Nothing imminent.</div>'}</section>
      </div>`;
    animateCounters($("plannerBody"));
    // AI rationale (why this sequence)
    try{
      const r = await api(`/v1/briefing/dilo-reasoning?rm_id=${RM_ID}`);
      const ai = r.ai||{};
      const tag = ai.generated_by==='llm_grounded' ? '<span class="ai-badge">AI · why this day</span>' : '<span class="ai-badge muted">rule-based</span>';
      $("diloAi").innerHTML = `${tag}
        ${ai.narrative?`<p class="th-headline">${esc(ai.narrative)}</p>`:''}
        ${ai.sequencing_logic?`<p class="th-body"><b>Sequencing:</b> ${esc(ai.sequencing_logic)}</p>`:''}
        ${ai.watchlist_focus?`<p class="th-why"><b>Watch today:</b> ${esc(ai.watchlist_focus)}</p>`:''}`;
    }catch(e){ const a=$("diloAi"); if(a) a.style.display='none'; }
  }catch(e){ $("plannerBody").innerHTML = `<div class="muted">DILO unavailable: ${esc(e.message)}</div>`; }
}
async function renderMilo(){
  $("plannerBody").innerHTML = skelCards(4);
  try{
    const m = await api(`/v1/rm/${RM_ID}/milo`);
    if(!m.available){ $("plannerBody").innerHTML = `<div class="muted">${esc(m.note||'No data')}</div>`; return; }
    const arrow=(t)=> t==='up'?'<span class="up">▲</span>':t==='down'?'<span class="down">▼</span>':'<span class="muted">—</span>';
    const snap = (m.yesterday_snapshot||[]).map(x=>`
      <div class="milo-metric"><span>${esc(x.metric)}</span><b data-count="${x.yesterday}">0</b><em>avg ${x.trailing_avg} ${arrow(x.trend)}</em></div>`).join("");
    $("plannerBody").innerHTML = `
      <div id="miloAi" class="dilo-ai">${skelPanel("Reading your portfolio performance\u2026", 4)}</div>
      <div class="kpi-row">
        <div class="kpi"><span>SLA adherence</span><strong data-count="${m.sla.adherence_pct}" data-suffix="%">0%</strong><em>${m.sla.met}/${m.sla.due} met</em></div>
        <div class="kpi"><span>Calls (period)</span><strong data-count="${m.period_totals.calls_made}">0</strong><em>${m.period_days} working days</em></div>
        <div class="kpi"><span>Tasks closed</span><strong data-count="${m.period_totals.tasks_closed}">0</strong><em>${m.period_totals.documents_collected} docs collected</em></div>
        <div class="kpi"><span>Portfolio credits</span><strong>${m.portfolio_credits.period_change_pct>0?'+':''}${m.portfolio_credits.period_change_pct}%</strong><em>period change ${arrow(m.portfolio_credits.trend)}</em></div>
      </div>
      <section class="panel" style="margin-top:16px"><h3>Yesterday vs trailing average</h3><div class="milo-grid">${snap}</div></section>`;
    animateCounters($("plannerBody"));
    try{
      const r = await api(`/v1/briefing/dilo-reasoning?rm_id=${RM_ID}`);
      const ai = r.ai||{};
      const live = ai.generated_by==='llm_grounded';
      const tag = live ? '<span class="ai-badge live">● AI · portfolio read</span>' : '<span class="ai-badge muted">rule-based</span>';
      $("miloAi").innerHTML = `${tag}${ai.portfolio_read?`<p class="th-headline">${esc(ai.portfolio_read)}</p>`:''}${ai.watchlist_focus?`<p class="th-why"><b>Watch:</b> ${esc(ai.watchlist_focus)}</p>`:''}`;
    }catch(e){ const a=$("miloAi"); if(a) a.style.display='none'; }
  }catch(e){ $("plannerBody").innerHTML = `<div class="muted">MILO unavailable: ${esc(e.message)}</div>`; }
}

/* ---- marketing collateral (customer-level, used in the RM Assist journey) ---- */
async function loadCollateral(cid, mountId){
  const mount = $(mountId); if(!mount) return;
  mount.innerHTML = skelPanel("Loading marketable offers\u2026", 4);
  try{
    const o = await api(`/v1/customers/${cid}/offers`);
    const offers = (o.offers||[]);
    mount.innerHTML = `
      <p class="muted">Pick an eligible offer to generate a personalised, consent- and eligibility-checked email. Blocked offers cannot be marketed until their risk signals clear.</p>
      <div class="offer-grid">${offers.map(of=>`
        <button class="offer-chip ${of.marketable?'ok':'no'}" ${of.marketable?`onclick="genEmail('${cid}','${of.product_id}',this)"`:'disabled'}>
          <b>${esc(of.product)}</b><span>${of.marketable?'Marketable':of.eligible?'Consent-blocked':'Not eligible'}</span>
        </button>`).join("")}</div>
      <div id="emailOut" style="margin-top:14px"></div>`;
  }catch(e){ mount.innerHTML = `<div class="muted">Offers unavailable: ${esc(e.message)}</div>`; }
}
async function genEmail(cid, pid, btn){
  document.querySelectorAll(".offer-chip").forEach(b=>b.classList.remove("sel")); if(btn) btn.classList.add("sel");
  $("emailOut").innerHTML = skelPanel("Drafting a detailed, personalised outreach + RM sell-sheet\u2026", 6);
  try{
    const e = await api(`/v1/customers/${cid}/collateral-pack?product_id=${encodeURIComponent(pid)}`);
    if(e.gated){ $("emailOut").innerHTML = `<div class="narr-donot"><b>Outreach suppressed.</b> ${esc(e.gate_reason||'')}</div>`; return; }
    const tag = e.generated_by==='llm_grounded' ? '<span class="ai-badge">AI · grounded in customer evidence</span>' : '<span class="ai-badge muted">evidence pack</span>';
    const comparison = (e.comparison&&e.comparison.length) ? `
      <div class="cp-block"><div class="er-label">Without vs with — for this customer</div>
        <table class="cp-table"><thead><tr><th></th><th>Today</th><th>With ${esc(e.product||'this')}</th></tr></thead>
        <tbody>${e.comparison.map(c=>`<tr><td>${esc(c.dimension||'')}</td><td>${esc(c.without||'')}</td><td>${esc(c.with||'')}</td></tr>`).join('')}</tbody></table></div>` : '';
    const talk = (e.talking_points&&e.talking_points.length) ? `
      <div class="cp-block"><div class="er-label">RM talking points</div><ul class="cp-list">${e.talking_points.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>` : '';
    const obj = (e.objection_handling&&e.objection_handling.length) ? `
      <div class="cp-block"><div class="er-label">Objection handling</div>${e.objection_handling.map(o=>`<div class="obj"><b>“${esc(o.objection||'')}”</b><p>${esc(o.response||'')}</p></div>`).join('')}</div>` : '';
    $("emailOut").innerHTML = `
      <div class="email-card">
        <div class="email-head"><span>${tag}</span><span class="ref">${esc(e.product||'')}</span></div>
        <div class="email-subj">Subject: ${esc(e.subject||'')}</div>
        <pre class="email-body">${esc(e.body||'')}</pre>
        ${e.eligibility_note?`<div class="email-note">⚠ ${esc(e.eligibility_note)}</div>`:''}
        <div class="btn-row"><button class="btn primary" onclick="navigator.clipboard&&navigator.clipboard.writeText(${JSON.stringify((e.subject||'')+'\\n\\n'+(e.body||''))});toast('Email copied')">Copy email</button></div>
      </div>
      ${comparison}${talk}${obj}`;
  }catch(err){ $("emailOut").innerHTML = `<div class="muted">Generation failed: ${esc(err.message)}</div>`; }
}

let THESIS_DATA = null;
async function loadMissionAction(cid, title, kind, idx){
  const box = document.getElementById(`missionAi-${idx}`); if(!box || !cid) return;
  if(box.dataset.loaded){ box.style.display = box.style.display==='none'?'block':'none'; return; }
  box.style.display='block';
  box.innerHTML = `<div class="loading sm">Generating the play…</div>`;
  try{
    const a = await api(`/v1/customers/${cid}/mission-action?title=${encodeURIComponent(title)}&kind=${encodeURIComponent(kind||'')}`);
    const live = a.generated_by==='llm_grounded';
    const tag = live ? '<span class="ai-badge live sm">● AI</span>' : '<span class="ai-badge muted sm">rule-based</span>';
    const steps = (a.steps||[]).map(s=>`<li>${esc(s)}</li>`).join('');
    const figs = (a.figures_to_cite||[]).map(f=>`<span class="mfig">${esc(f)}</span>`).join('');
    box.innerHTML = `<div class="ma-head">${tag}${a.approach?`<span class="ma-approach">${esc(a.approach)}</span>`:''}</div>
      ${steps?`<ol class="ma-steps">${steps}</ol>`:''}
      ${figs?`<div class="ma-figs"><b>Cite</b> ${figs}</div>`:''}
      ${a.say_this?`<div class="ma-say"><b>Say</b> “${esc(a.say_this)}”</div>`:''}
      ${a.crm_action?`<div class="ma-crm">▸ ${esc(a.crm_action)}</div>`:''}`;
    box.dataset.loaded='1';
  }catch(e){ box.innerHTML = `<div class="muted sm">Couldn't generate: ${esc(e.message)}</div>`; }
}
async function loadDynamicThesis(cid){
  const box = $("dynThesis"); if(!box) return;
  box.innerHTML = skelPanel("Generating live relationship thesis\u2026", 5);
  try{
    const t = await api(`/v1/customers/${cid}/thesis`);
    THESIS_DATA = t;
    const live = t.generated_by==='llm_grounded';
    const tag = live ? '<span class="ai-badge live">● AI · generated live</span>' : '<span class="ai-badge muted">rule-based</span>';
    const fp = t.evidence_footprint||{};
    const fpLine = `<span class="thesis-fp">${live?'synthesised':'derived'} from ${fp.signals||0} signals · ${fp.open_srs||0} open cases · ${fp.blocking_docs||0} doc blockers · ${fp.eligible_opps||0} eligible offers</span>`;
    const postureClass = {Grow:'pg', Protect:'pp', Stabilise:'ps', Watch:'pw'}[t.posture]||'pw';
    // headline + posture show immediately; the rest reveals on click (interactive)
    const facets = [
      {k:'thesis', icon:'◆', label:'The thesis', has:!!t.thesis},
      {k:'why_now', icon:'⏱', label:'Why now', has:!!t.why_now},
      {k:'risk_read', icon:'▲', label:'Risk', has:!!t.risk_read},
      {k:'opportunity_read', icon:'✦', label:'Opportunity', has:!!t.opportunity_read},
      {k:'top_actions', icon:'➔', label:'Top actions', has:(t.top_actions||[]).length>0},
    ].filter(f=>f.has);
    box.innerHTML = `
      <div class="thesis-toprow">${tag} <span class="posture ${postureClass}">${esc(t.posture||'')}</span>
        <button class="thesis-regen" onclick="loadDynamicThesis('${cid}')" title="Regenerate">↻ regenerate</button></div>
      ${fpLine}
      ${t.headline?`<p class="th-headline">${esc(t.headline)}</p>`:''}
      <div class="thesis-facets">${facets.map((f,i)=>`<button class="facet-chip ${i===0?'on':''}" onclick="revealFacet('${f.k}',this)"><span class="fc-ic">${f.icon}</span> ${f.label}</button>`).join('')}</div>
      <div id="facetStage" class="facet-stage"></div>`;
    if(facets.length) revealFacet(facets[0].k);
  }catch(e){ box.innerHTML = `<div class="muted">Live thesis unavailable: ${esc(e.message)}</div>`; }
}

function revealFacet(k, btn){
  const t = THESIS_DATA; if(!t) return;
  document.querySelectorAll(".thesis-facets .facet-chip").forEach(b=>{ if(btn) b.classList.toggle("on", b===btn); });
  const stage = $("facetStage"); if(!stage) return;
  let html = '';
  // Narration bodies carry data-rx-stream so rxStreamScope() reveals them
  // token-by-token instead of popping in fully formed.
  if(k==='thesis') html = `<p class="th-body reveal" data-rx-stream="${esc(t.thesis||'')}"></p>`;
  else if(k==='why_now') html = `<p class="th-why reveal"><b>Why now:</b> <span data-rx-stream="${esc(t.why_now||'')}"></span></p>`;
  else if(k==='risk_read') html = `<div class="th-risk reveal"><b>Most pressing risk</b> <span data-rx-stream="${esc(t.risk_read||'')}"></span></div>`;
  else if(k==='opportunity_read') html = `<div class="th-opp reveal"><b>Where the upside is</b> <span data-rx-stream="${esc(t.opportunity_read||'')}"></span></div>`;
  else if(k==='top_actions'){
    const actions = (t.top_actions||[]).map((a,i)=>`<div class="th-act u-${String(a.urgency||'').toLowerCase()}" style="animation-delay:${i*0.08}s"><span class="th-act-n">${i+1}</span><div><b>${esc(a.action||'')}</b><span>${esc(a.rationale||'')}</span></div>${a.urgency?`<em class="th-act-u">${esc(a.urgency)}</em>`:''}</div>`).join('');
    html = `<div class="th-actions reveal"><div class="er-label">AI-sequenced — do these in order</div>${actions}</div>`;
  }
  stage.innerHTML = html;
  rxStreamScope(stage);
}

/* ===================== BREACH RADAR ===================== */
let BR_STATE = null;  // holds last snapshot for slider baselines

async function loadBreachRadar(cid){
  try{
    const snap = await api(`/v1/customers/${cid}/breach-radar`);
    BR_STATE = snap;
    if($("brBand")) $("brBand").textContent = snap.breach_band;
    if($("brRadar")) { $("brRadar").innerHTML = `<div id="brAi" class="breach-ai">${skelPanel("Reading the trajectory\u2026", 3)}</div>` + renderBreachRadar(snap); animateCounters($("brRadar")); drawSparkline(snap.utilization_sparkline); }
    // AI reading of the trajectory — the "what it means + the play"
    try{
      const bi = await api(`/v1/customers/${cid}/breach-intelligence`);
      const ai = bi.ai||{};
      const live = ai.generated_by==='llm_grounded';
      const tag = live ? '<span class="ai-badge live">● AI · credit-officer read</span>' : '<span class="ai-badge muted">rule-based</span>';
      if($("brAi")) $("brAi").innerHTML = `
        <div class="breach-ai-head">${tag}${ai.verdict?`<span class="breach-verdict">${esc(ai.verdict)}</span>`:''}</div>
        ${ai.what_it_means?`<p class="bai-means">${esc(ai.what_it_means)}</p>`:''}
        ${(ai.drivers&&ai.drivers.length)?`<div class="bai-drivers">${ai.drivers.map(d=>`<span class="bai-driver">${esc(d)}</span>`).join('')}</div>`:''}
        ${ai.intervention?`<div class="bai-play"><b>The play</b> ${esc(ai.intervention)}</div>`:''}
        ${ai.customer_message?`<div class="bai-say"><b>Raise it with the customer</b> “${esc(ai.customer_message)}”</div>`:''}`;
    }catch(e){ const a=$("brAi"); if(a) a.style.display='none'; }
  }catch(e){ if($("brRadar")) $("brRadar").innerHTML = `<div class="muted">Breach Radar unavailable: ${esc(e.message)}</div>`; }
  // Income reconciliation + AI control tower + scenario lab
  if($("incRecon")) loadIncomeReconciliation(cid);
  if($("riskCopilot")) loadBreachIncomeCopilot(cid);
  if($("scenarioLab")) renderScenarioLab(cid, BR_STATE);
}

async function loadIncomeReconciliation(cid){
  const box = $("incRecon"); if(!box) return;
  try{
    const r = await api(`/v1/customers/${cid}/income-reconciliation?narrative=true`);
    box.innerHTML = renderIncomeReconciliation(r);
    animateCounters(box);
  }catch(e){ box.innerHTML = `<div class="muted">Income reconciliation unavailable: ${esc(e.message)}</div>`; }
}

function _inrShort(n){
  n = Number(n)||0; const a = Math.abs(n);
  if(a >= 1e7) return '₹'+(n/1e7).toFixed(2)+' Cr';
  if(a >= 1e5) return '₹'+(n/1e5).toFixed(2)+' L';
  return '₹'+Math.round(n).toLocaleString('en-IN');
}

function renderIncomeReconciliation(r){
  const months = r.months||[]; const agg = r.aggregate||{};
  const findings = r.findings||[]; const n = r.ai_narrative||{};
  // max for bar scaling
  const maxv = Math.max(1, ...months.map(m=>Math.max(m.gst_sales_inr, m.bank_credits_inr)));
  const runrate = r.monthly_turnover_runrate_inr||0;
  const runPct = runrate? Math.min(100, runrate/maxv*100):0;
  const bars = months.map(m=>{
    const gPct = Math.min(100, m.gst_sales_inr/maxv*100);
    const bPct = Math.min(100, m.bank_credits_inr/maxv*100);
    const hot = m.variance_pct>=15? 'hot' : m.variance_pct>=8? 'warm':'';
    const cashDot = m.cash_share_pct>=10 ? `<span class="ir-dot cash" title="cash ${m.cash_share_pct}% of credits"></span>`:'';
    const rpDot = m.related_party_share_pct>0 ? `<span class="ir-dot rp" title="related-party ${m.related_party_share_pct}%"></span>`:'';
    return `<div class="ir-col ${hot}" title="${esc(m.period)} · GST ${_inrShort(m.gst_sales_inr)} · Bank ${_inrShort(m.bank_credits_inr)} · var ${m.variance_pct}%">
      <div class="ir-bars">
        <div class="ir-bar gst" style="height:${gPct}%"></div>
        <div class="ir-bar bank" style="height:${bPct}%"></div>
      </div>
      <div class="ir-var ${hot}">${m.variance_pct}%</div>
      <div class="ir-mo">${esc((m.period||'').slice(2))}${cashDot}${rpDot}</div>
    </div>`;
  }).join('');
  const findingHtml = findings.map(f=>`<div class="ews ${String(f.severity).toLowerCase()}"><div class="t">${esc(f.finding_type)} <span class="sev ${String(f.severity).toLowerCase()}">${esc(f.severity)}</span></div><div class="ev">${esc(f.evidence_metric)}</div><div class="guard">Ask: ${esc(f.recommended_action)}</div></div>`).join('');
  const aiTag = n.generated_by==='llm_grounded' ? '<span class="ai-badge">AI · reconciles GST · bank · turnover</span>' : '<span class="ai-badge muted">rule-based summary</span>';
  const aiHtml = n.summary ? `<div class="sim-ai">${aiTag}<p class="ai-narr">${esc(n.summary)}</p></div>` : '';
  return `
    <div class="ir-intro">Three independent revenue measures, triangulated. Tall bars that diverge = income that doesn't reconcile. <span class="ir-legend"><span class="ir-key gst">GST sales</span><span class="ir-key bank">Bank credits</span></span></div>
    <div class="ir-chart">${bars}${runrate?`<div class="ir-runrate" style="bottom:calc(${runPct}% + 26px)" title="audited turnover run-rate ${_inrShort(runrate)}/mo"></div>`:''}</div>
    <div class="ir-agg">
      <div class="ir-cell"><span>GST sales (yr)</span><b>${_inrShort(agg.gst_sales_total_inr)}</b></div>
      <div class="ir-cell"><span>Bank credits (yr)</span><b>${_inrShort(agg.bank_credits_total_inr)}</b></div>
      <div class="ir-cell ${agg.variance_pct>=15?'breach':agg.variance_pct>=8?'warn':''}"><span>Variance</span><b>${agg.variance_pct}%</b></div>
      ${agg.bank_vs_turnover_pct!=null?`<div class="ir-cell"><span>Bank vs turnover</span><b>${agg.bank_vs_turnover_pct}%</b></div>`:''}
    </div>
    ${aiHtml}
    <div class="section-label" style="margin-top:12px">Reconciliation findings</div>
    ${findingHtml||'<div class="success">Income sources reconcile.</div>'}`;
}

function brScoreClass(s){ return s>=70?'critical':s>=45?'high':s>=25?'medium':'ok'; }

function renderBreachRadar(s){
  const h = s.headroom, dp = s.dp_coverage;
  const dtb = h.days_to_breach;
  const covRows = (s.covenants||[]).map(c=>`
    <div class="cov-row ${c.state}">
      <div class="cov-main"><b>${esc(c.covenant_type)}</b><span>${esc(c.requirement||'')}</span></div>
      <div class="cov-meta">
        <span class="cov-badge ${c.state}">${c.state==='breach'?'Breached':c.state==='at_risk'?'At risk':'OK'}</span>
        <em>${c.days_until_due!=null ? (c.days_until_due<0?`${Math.abs(c.days_until_due)}d overdue`:`due in ${c.days_until_due}d`) : esc(c.status)}</em>
      </div>
    </div>`).join('');
  const utilPct = Math.min(100, h.current_utilization_pct);
  return `
    <div class="br-top">
      <div class="br-score ${brScoreClass(s.breach_score)}">
        <b data-count="${s.breach_score}">0</b><span>breach score</span>
      </div>
      <div class="br-headroom">
        <div class="br-gauge">
          <div class="br-gauge-track">
            <div class="br-warn-mark" style="left:${h.utilization_warn_pct}%"></div>
            <div class="br-gauge-fill ${utilPct>=h.utilization_warn_pct?'hot':''}" style="width:0%" data-w="${utilPct}%"></div>
          </div>
          <div class="br-gauge-labels"><span>Utilisation <b>${h.current_utilization_pct}%</b></span><span>limit ${h.utilization_breach_pct}%</span></div>
        </div>
        <div class="br-stats">
          ${metric('Available limit', fmtINR(h.available_limit_inr), h.available_limit_inr<0?'down':'')}
          ${metric('Slope / day', (h.utilization_slope_per_day>0?'+':'')+h.utilization_slope_per_day+' pp', h.utilization_slope_per_day>0?'warn':'up')}
          ${metric('Days to breach', dtb!=null?`${dtb} days`:'> horizon', dtb!=null&&dtb<=30?'down':'')}
          ${metric('DP cover', dp.cover_ratio+'x (min '+dp.min_cover_ratio+'x)', dp.cover_ratio<dp.min_cover_ratio?'down':'up')}
        </div>
      </div>
    </div>
    <div class="section-label" style="margin-top:14px">Utilisation trend · last ${ (s.utilization_sparkline||[]).length } days</div>
    <svg id="brSpark" class="br-spark" viewBox="0 0 320 60" preserveAspectRatio="none"></svg>
    <div class="section-label" style="margin-top:14px">Covenant register</div>
    <div class="cov-list">${covRows || '<div class="muted">No covenants on file.</div>'}</div>
    <div class="disclaimer">⚠ ${esc(s.guardrail)}</div>`;
}

/* ---------- reusable sparkline ----------
   Generalised from the breach-radar sparkline (which was hardcoded to #brSpark)
   so any element can get a trend line: balances, transaction amounts,
   utilisation. Returns SVG markup; rxSpark() paints it into a target. */
function rxSparklineSvg(vals, opts){
  opts = opts || {};
  const w = opts.w||320, hgt = opts.h||60, pad = 4;
  const v = (vals||[]).map(Number).filter(x=>Number.isFinite(x));
  if(v.length < 2) return '';
  const stroke = opts.stroke || '#1b56d6';
  const fillTop = opts.fillTop || 'rgba(27,86,214,.22)';
  const max = Math.max(...v, opts.forceMax!=null?opts.forceMax:-Infinity);
  const min = Math.min(...v, opts.forceMin!=null?opts.forceMin:Infinity);
  const rng = (max-min)||1;
  const gid = 'rxg' + Math.random().toString(36).slice(2,8);
  const pts = v.map((x,i)=>{ const px=pad+(i/(v.length-1))*(w-2*pad); const py=hgt-pad-((x-min)/rng)*(hgt-2*pad); return [px,py]; });
  const d = pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const area = d + ` L ${w-pad} ${hgt} L ${pad} ${hgt} Z`;
  let guide = '';
  if(opts.warnAt != null){
    const wy = hgt-pad-((opts.warnAt-min)/rng)*(hgt-2*pad);
    if(wy>0 && wy<hgt) guide = `<line x1="0" y1="${wy.toFixed(1)}" x2="${w}" y2="${wy.toFixed(1)}" stroke="#c0322b" stroke-width="1" stroke-dasharray="3 3" opacity=".55"/>`;
  }
  if(opts.zeroLine){
    const zy = hgt-pad-((0-min)/rng)*(hgt-2*pad);
    if(zy>0 && zy<hgt) guide += `<line x1="0" y1="${zy.toFixed(1)}" x2="${w}" y2="${zy.toFixed(1)}" stroke="#8a97a6" stroke-width="1" stroke-dasharray="2 3" opacity=".6"/>`;
  }
  const last = pts[pts.length-1];
  return `<defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${fillTop}"/><stop offset="1" stop-color="rgba(27,86,214,0)"/></linearGradient></defs>`
    + guide
    + `<path d="${area}" fill="url(#${gid})"/>`
    + `<path d="${d}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="stroke-dasharray:1400;stroke-dashoffset:1400;animation:brdash 1s ease forwards"/>`
    + (opts.dot===false?'':`<circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3" fill="${stroke}"/>`);
}
// Paint into any <svg> element (by id or node).
function rxSpark(target, vals, opts){
  const svg = typeof target === 'string' ? $(target) : target;
  if(!svg) return;
  const markup = rxSparklineSvg(vals, opts);
  if(!markup){ svg.innerHTML = ''; return; }
  svg.innerHTML = markup;
}
// Backwards-compatible breach-radar sparkline (same visual as before).
function drawSparkline(vals){
  if(!vals || !vals.length) return;
  rxSpark("brSpark", vals, { warnAt:85, forceMax:100, forceMin:0, dot:false });
}

function renderBreachSim(cid, s){
  const h = s.headroom;
  // sensible slider ceilings from the customer's own scale
  const maxDelay = Math.max(2000000, Math.round((h.drawing_power_inr||5000000)*0.6));
  const maxDraw  = Math.max(1000000, Math.round((h.available_limit_inr>0?h.available_limit_inr:1000000)*2));
  return `
    <div class="sim-intro">Drag to model a stress and re-score the breach trajectory live. Deterministic — no approval is implied.</div>
    <div class="sim-ctrl">
      <label>Buyer payment delayed <b id="simDelayV">₹0</b></label>
      <input type="range" id="simDelay" min="0" max="${maxDelay}" step="100000" value="0" oninput="onSimInput('${cid}')" />
    </div>
    <div class="sim-ctrl">
      <label>Delay duration <b id="simDaysV">30 days</b></label>
      <input type="range" id="simDays" min="0" max="120" step="5" value="30" oninput="onSimInput('${cid}')" />
    </div>
    <div class="sim-ctrl">
      <label>Sales drop <b id="simSalesV">0%</b></label>
      <input type="range" id="simSales" min="0" max="50" step="5" value="0" oninput="onSimInput('${cid}')" />
    </div>
    <div class="sim-ctrl">
      <label>Additional drawdown <b id="simDrawV">₹0</b></label>
      <input type="range" id="simDraw" min="0" max="${maxDraw}" step="100000" value="0" oninput="onSimInput('${cid}')" />
    </div>
    <div class="sim-result" id="simResult"><div class="muted">Move a slider to project the outcome.</div></div>`;
}

let SIM_TIMER = null;
function onSimInput(cid){
  const delay = +$("simDelay").value, days = +$("simDays").value, sales = +$("simSales").value, draw = +$("simDraw").value;
  $("simDelayV").textContent = fmtINR(delay);
  $("simDaysV").textContent = days + " days";
  $("simSalesV").textContent = sales + "%";
  $("simDrawV").textContent = fmtINR(draw);
  // debounce the re-query so dragging is smooth
  if(SIM_TIMER) clearTimeout(SIM_TIMER);
  SIM_TIMER = setTimeout(()=>runBreachSim(cid, {buyer_payment_delay_inr:delay, delay_days:days, sales_drop_pct:sales, additional_drawdown_inr:draw}), 180);
}

async function runBreachSim(cid, scenario){
  try{
    const r = await api(`/v1/customers/${cid}/breach-radar/simulate`, {method:"POST", body:JSON.stringify(scenario)});
    const p = r.projected, b = r.baseline, dl = r.delta;
    const cls = brScoreClass(p.breach_score);
    const arrow = (n)=> n>0?`<span class="down">▲ ${n>0?'+':''}${n}</span>` : n<0?`<span class="up">▼ ${n}</span>` : `<span class="muted">0</span>`;
    const acts = (r.recommended_actions||[]).map(a=>`
      <div class="sim-act ${a.kind.toLowerCase()}">
        <span>${esc(a.kind)}</span><b>${esc(a.action)}</b><em>${esc(a.why)}</em>
        <div class="chips">${(a.refs||[]).map(x=>`<span class="ref">${esc(x)}</span>`).join('')}</div>
      </div>`).join('');
    const n = r.ai_narrative || {};
    const aiBlock = n.summary ? `<div class="sim-ai"><span class="ai-badge ${n.generated_by==='llm_grounded'?'':'muted'}">${n.generated_by==='llm_grounded'?'AI · explains this projection':'rule-based summary'}</span><p class="ai-narr">${esc(n.summary)}</p></div>` : '';
    $("simResult").innerHTML = `
      <div class="sim-grid">
        <div class="sim-cell ${p.crosses_breach_line?'breach':''}"><span>Utilisation</span><b>${p.utilization_pct_after_window}%</b><em>${b.utilization_pct}% → ${arrow(dl.utilization_pct)} pp</em></div>
        <div class="sim-cell ${p.cover_ratio<(BR_STATE?.dp_coverage?.min_cover_ratio||1.1)?'breach':''}"><span>DP cover</span><b>${p.cover_ratio}x</b><em>${b.cover_ratio}x → ${arrow(dl.cover_ratio)}</em></div>
        <div class="sim-cell sc-${cls}"><span>Breach score</span><b>${p.breach_score}</b><em>${b.breach_score} → ${arrow(dl.breach_score)}</em></div>
      </div>
      <div class="sim-band sc-${cls}">${esc(p.breach_band)}${p.crosses_breach_line?' · crosses limit':''}</div>
      ${aiBlock}
      <div class="section-label" style="margin-top:12px">Recommended pre-emptive actions</div>
      ${acts}`;
    animateCounters($("simResult"));
  }catch(e){ if($("simResult")) $("simResult").innerHTML = `<div class="muted">Simulation failed: ${esc(e.message)}</div>`; }
}



/* ===================== DYNAMIC AI BRIEFING STUDIO ===================== */
let BRIEFING_STUDIO_CACHE = {};
let BRIEFING_ACTIVE_CARD = null;

async function loadBriefingStudio(cid){
  const box = $("briefingStudio");
  if(!box) return;
  box.innerHTML = `<section class="brief-studio-shell"><div class="loading">Opening Dynamic AI Briefing Studio…</div></section>`;
  try{
    const r = await api(`/v1/customers/${cid}/briefing-studio?ts=${Date.now()}`);
    BRIEFING_STUDIO_CACHE[cid] = r;
    box.innerHTML = renderBriefingStudio(cid, r);
    animateCounters(box);
    const first = (r.briefing_cards||[])[0];
    if(first) openBriefingDrilldown(cid, first.id, first.question, true);
  }catch(e){
    box.innerHTML = `<section class="brief-studio-shell"><div class="muted">Dynamic AI Briefing Studio unavailable: ${esc(e.message)}</div></section>`;
  }
}

function renderBriefingStudio(cid, r){
  const fp = r.evidence_footprint || {};
  const cards = (r.briefing_cards||[]).map((c,i)=>`
    <button class="brief-card p-${String(c.priority||'').toLowerCase()}" id="brief-card-${esc(c.id)}" onclick="openBriefingDrilldown('${cid}','${esc(c.id)}','${esc((c.question||'').replace(/'/g,"\\'"))}')" style="animation-delay:${i*0.045}s">
      <span class="brief-pri">${esc(c.priority||'AI')} · click to generate</span>
      <b>${esc(c.title||'Briefing card')}</b>
      <em>${esc(c.question||'What should the RM know?')}</em>
      <p>${esc(c.answer||'Click to generate an evidence-backed action drilldown.')}</p>
      <div class="brief-ev">${(c.evidence||[]).slice(0,4).map(x=>`<span>${esc(x)}</span>`).join('')}</div>
      <i>${esc(c.cta||'Generate drilldown')} →</i>
    </button>`).join('');
  const prompts = (r.demo_prompts||[]).slice(0,6).map(q=>`<button class="prompt-pill" onclick="askBriefingPrompt('${cid}','${esc(q.replace(/'/g,"\\'"))}')">${esc(q)}</button>`).join('');
  const seq = (r.meeting_sequence||[]).map(s=>`<div class="meet-step"><span>${esc(s.step)}</span><b>${esc(s.label)}</b><p>${esc(s.instruction)}</p></div>`).join('');
  return `
    <section class="brief-studio-shell">
      <div class="brief-hero">
        <div>
          <div class="eyebrow">Dynamic AI Briefing Studio · generated live</div>
          <h3>${esc(r.headline || 'AI-generated relationship briefing')}</h3>
          <p>${esc(r.guardrail || 'Evidence-first RM guidance. No approval promises.')}</p>
          <div class="brief-footprint">
            <span>posture · ${esc(r.posture||'Watch')}</span>
            <span>${esc(fp.signals||0)} signals</span>
            <span>${esc(fp.open_cases||0)} open cases</span>
            <span>${esc(fp.document_blockers||0)} doc blockers</span>
            <span>${esc(fp.eligible_offers||0)} eligible offers</span>
            <button onclick="loadBriefingStudio('${cid}')">↻ regenerate studio</button>
          </div>
        </div>
        <div class="brief-pulse"><b>${esc((r.generated_by||'ai').replace('_',' '))}</b><span>source</span></div>
      </div>
      <div class="brief-question-bar"><b>Demo prompts</b>${prompts}</div>
      <div class="brief-grid">${cards}</div>
      <div id="briefDrill" class="brief-drill"><div class="loading">Click a briefing card to generate a drilldown.</div></div>
      <div class="meeting-sequence">${seq}</div>
    </section>`;
}

async function openBriefingDrilldown(cid, cardId, question='', silent=false){
  BRIEFING_ACTIVE_CARD = cardId;
  document.querySelectorAll('.brief-card').forEach(b=>b.classList.toggle('on', b.id === `brief-card-${cardId}`));
  const box = $("briefDrill");
  if(!box) return;
  box.innerHTML = `<div class="drill-card">${skelPanel("Generating AI drilldown for " + esc(cardId) + "\u2026", 4)}</div>`;
  try{
    const r = await api(`/v1/customers/${cid}/briefing-drilldown?card_id=${encodeURIComponent(cardId)}&q=${encodeURIComponent(question||'')}&ts=${Date.now()}`);
    box.innerHTML = renderBriefingDrilldown(r);
  }catch(e){
    // backward fallback if an older backend expects topic instead of card_id
    try{
      const r = await api(`/v1/customers/${cid}/briefing-drilldown?topic=${encodeURIComponent(cardId)}&q=${encodeURIComponent(question||'')}&ts=${Date.now()}`);
      box.innerHTML = renderBriefingDrilldown(r);
    }catch(e2){
      box.innerHTML = `<div class="drill-card"><div class="muted">Drilldown failed: ${esc(e2.message || e.message)}</div></div>`;
      if(!silent) toast('Briefing drilldown failed: '+(e2.message||e.message));
    }
  }
}

function askBriefingPrompt(cid, q){
  openBriefingDrilldown(cid, 'why-now', q);
}

function renderBriefingDrilldown(r){
  return `<div class="drill-card">
    <div class="drill-head"><span class="ai-badge ${r.generated_by==='llm_grounded'?'live':'muted'}">${r.generated_by==='llm_grounded'?'● AI drilldown':'rule-based drilldown'}</span><b>${esc(r.title||'RM drilldown')}</b></div>
    <p>${esc(r.answer||'No answer returned.')}</p>
    ${(r.next_questions&&r.next_questions.length)?`<div class="drill-qs"><b>Ask next</b>${r.next_questions.map(x=>`<span>${esc(x)}</span>`).join('')}</div>`:''}
    ${r.say_this?`<div class="drill-say"><b>Customer-safe line:</b> ${esc(r.say_this)}</div>`:''}
    ${r.what_not_to_say?`<div class="drill-no"><b>Do not say:</b> ${esc(r.what_not_to_say)}</div>`:''}
    ${r.crm_action?`<div class="drill-crm">CRM action: ${esc(r.crm_action)}</div>`:''}
    <div class="brief-ev">${(r.evidence||[]).map(x=>`<span>${esc(x)}</span>`).join('')}</div>
  </div>`;
}

async function loadBreachIncomeCopilot(cid){
  const box = $("riskCopilot");
  if(!box) return;
  box.innerHTML = skelPanel("Building AI Risk Control Tower\u2026", 5);
  try{
    const r = await api(`/v1/customers/${cid}/breach-income-copilot?ts=${Date.now()}`);
    window.__RISK_COPILOT = r;
    box.innerHTML = renderRiskCopilot(r);
  }catch(e){
    box.innerHTML = `<div class="muted">AI Risk Control Tower unavailable: ${esc(e.message)}</div>`;
  }
}

function renderRiskCopilot(r){
  const qs = (r.decision_questions||[]).map((q,i)=>`<button class="risk-q" onclick="focusRiskQuestion(this)"><span>${i+1}</span>${esc(q)}</button>`).join('');
  const demo = (r.rm_demo_script||[]).map((s,i)=>`<div class="demo-step"><span>${i+1}</span><p>${esc(s)}</p></div>`).join('');
  const tracks = (r.talk_tracks||[]).map(t=>`<div class="talk-card"><b>${esc(t.title||'Talk track')}</b><p>“${esc(t.say||'')}”</p><em>${esc(t.why||'')}</em></div>`).join('');
  const actions = (r.crm_actions||[]).map(a=>`<button class="btn small" onclick="toast('CRM action staged: ${esc(String(a).replace(/'/g,"\\'"))}')">${esc(a)}</button>`).join('');
  return `<div class="risk-hero"><div><div class="eyebrow">Breach Radar & Income Reconciliation · AI control tower</div><h2>${esc(r.control_tower_headline||'AI risk control tower')}</h2><p>${esc(r.executive_read||'AI connects limit trajectory, income reconciliation, documents, and RM next questions.')}</p><div class="brief-footprint">${(r.evidence_chips||[]).map(x=>`<span>${esc(x)}</span>`).join('')}<span>${esc(r.generated_by||'ai')}</span></div></div><div class="brief-pulse"><b>AI</b><span>demoable</span></div></div>
    <div class="risk-questions">${qs}</div>
    <div class="risk-demo-grid"><div><div class="section-label">How to demo this to the customer</div>${demo}</div><div><div class="section-label">Customer-safe talk tracks</div>${tracks}</div></div>
    <div class="risk-actions">${actions}</div>
    ${r.guardrail?`<div class="disclaimer">⚠ ${esc(r.guardrail)}</div>`:''}`;
}

function focusRiskQuestion(btn){
  document.querySelectorAll('.risk-q').forEach(x=>x.classList.toggle('on', x===btn));
}

function renderScenarioLab(cid, snap){
  const box = $("scenarioLab"); if(!box) return;
  const presets = (window.__RISK_COPILOT && window.__RISK_COPILOT.scenario_presets) || [];
  const phtml = presets.length ? `<div class="preset-row">${presets.map(p=>`<button class="preset-card" onclick='applyScenarioPreset("${cid}", ${JSON.stringify(p.scenario||{}).replace(/'/g,"&#39;")})'><b>${esc(p.label||p.id)}</b><span>${esc((p.projected&&p.projected.breach_band)||'Run scenario')}</span><em>click to project</em></button>`).join('')}</div>` : '';
  box.innerHTML = `${phtml}${snap ? renderBreachSim(cid, snap) : '<div class="muted">Breach radar baseline unavailable.</div>'}`;
}

function applyScenarioPreset(cid, scenario){
  if($("simDelay")) $("simDelay").value = scenario.buyer_payment_delay_inr || 0;
  if($("simDays")) $("simDays").value = scenario.delay_days || 30;
  if($("simSales")) $("simSales").value = scenario.sales_drop_pct || 0;
  if($("simDraw")) $("simDraw").value = scenario.additional_drawdown_inr || 0;
  onSimInput(cid);
}

async function loadGamePlan(cid, btn){
  const box = $("gp-"+cid);
  if(box.dataset.loaded){ box.classList.toggle("show"); btn.textContent = box.classList.contains("show")?"▾ Hide AI game plan":"▸ Generate AI game plan for this conversation"; return; }
  btn.textContent = "Generating live…"; btn.disabled = true;
  box.classList.add("show");
  box.innerHTML = skelPanel("Generating game plan\u2026", 4);
  try{
    const r = await api(`/v1/briefing/playbook/${cid}`);
    box.innerHTML = `<div class="gp-mode">${r.mode==='ai'?'live · AI':'fallback'}</div>` + renderPlaybookStruct(r.structured);
    box.dataset.loaded = "1"; btn.textContent = "▾ Hide AI game plan";
  }catch(e){ box.innerHTML = `<div class="muted">Game plan unavailable: ${esc(e.message)}</div>`; btn.textContent = "▸ Retry AI game plan"; }
  btn.disabled = false;
}

// minimal markdown -> html (headings, bold, bullets, paragraphs)
function mdLite(t){
  if(!t) return '<div class="muted">No content.</div>';
  const lines = String(t).split(/\n/); let html=''; let inUl=false;
  const inline = (s)=>esc(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  for(let ln of lines){
    if(/^\s*[-*]\s+/.test(ln)){ if(!inUl){html+='<ul class="ai-ul">';inUl=true;} html+=`<li>${inline(ln.replace(/^\s*[-*]\s+/,''))}</li>`; continue; }
    if(inUl){html+='</ul>';inUl=false;}
    if(/^#{1,6}\s+/.test(ln)){ html+=`<h4 class="ai-h">${inline(ln.replace(/^#{1,6}\s+/,''))}</h4>`; }
    else if(ln.trim()==='') { html+=''; }
    else { html+=`<p class="ai-p">${inline(ln)}</p>`; }
  }
  if(inUl)html+='</ul>';
  return html;
}

function renderThreads(ds){
  const th = ds.engagement_threads || [];
  if(!th.length) return '<div class="muted">No engagement threads.</div>';
  return `<div class="threads">${th.map(t=>`<div class="thread pr-${String(t.priority).toLowerCase()}"><div class="th-top"><b>${esc(t.topic)}</b><span>${esc(t.status)}</span></div><p>${esc(t.angle)}</p><div class="chips">${(t.products?[t.products]:[]).filter(Boolean).map(x=>`<span class="ref">${esc(x)}</span>`).join('')}<span class="ref">${esc(t.priority)}</span></div></div>`).join('')}</div>`;
}

function metric(k,v,cls=''){ return `<div class="metric"><span class="k">${esc(k)}</span><span class="v ${cls}">${esc(v)}</span></div>`; }
function renderPlaybook(pb){ return `<div class="playbook"><div class="objective"><strong>${esc(pb.primary_objective)}</strong><p>${esc(pb.opening_script)}</p></div><div class="track-grid">${(pb.talk_tracks||[]).map(t=>`<div class="track"><span>${esc(t.label)}</span>${esc(t.text)}</div>`).join('')}</div><details><summary>Do / Don’t say and live nudge triggers</summary><div class="dos"><div><b>Say</b>${(pb.do_say||[]).map(x=>`<p>✓ ${esc(x)}</p>`).join('')}</div><div><b>Do not say</b>${(pb.dont_say||[]).map(x=>`<p>✕ ${esc(x)}</p>`).join('')}</div></div><div class="chips">${(pb.live_nudge_triggers||[]).map(x=>`<span class="ref">${esc(x)}</span>`).join('')}</div></details></div>`; }
function renderOpportunities(eligible, blocked){ const elig = eligible.map(o=>oppCard(o,true)).join('') || '<div class="muted">No eligible product now.</div>'; const block = blocked.slice(0,4).map(o=>oppCard(o,false)).join(''); return `<div class="opps-board"><div><div class="section-label">Eligible to discuss</div>${elig}</div><div><div class="section-label">Blocked / hold</div>${block || '<div class="muted">No blocked products.</div>'}</div></div>`; }
function oppCard(o, ok){ return `<div class="opp-card ${ok?'ok':'no'}"><div class="opp-title">${ok?'✓':'⏸'} ${esc(o.product)}<span>${esc(o.category)}</span></div><p>${esc(o.rationale)}</p><div class="chips">${(o.matched_signals||[]).map(s=>`<span class="ref">${esc(s)}</span>`).join('')}${(o.blocked_by||[]).map(s=>`<span class="ref bad">${esc(s)}</span>`).join('')}</div></div>`; }
function renderEws(ews, cid){
  const list = (ews||[]).length ? ews.map(s=>`<div class="ews ${String(s.severity).toLowerCase()}"><div class="t">${esc(s.signal_type)} <span class="sev ${String(s.severity).toLowerCase()}">${esc(s.severity)}</span></div><div class="ev">${esc(s.evidence_metric)}</div><div class="guard">Guardrail: ${esc(s.false_positive_guardrail)}</div></div>`).join('') : `<div class="success">No material early-warning signals.</div>`;
  const ai = cid ? `<div class="ews-ai-hero" id="ewsAiBox-${esc(cid)}"><div class="loading">Reasoning over these signals as an Indian MSME credit officer (RBI SMA context)…</div></div>` : '';
  // AI read leads; the deterministic signal rows follow as the evidence beneath it
  return `${ai}<div class="ews-rows-label">${(ews||[]).length} signals · deterministic detectors</div>${list}`;
}

async function loadEwsNarrative(cid){
  const box = document.getElementById(`ewsAiBox-${cid}`); if(!box) return;
  try{
    const n = await api(`/v1/customers/${cid}/ews-reasoning`);
    const live = n.generated_by==='llm_grounded';
    const tag = live ? '<span class="ai-badge live">● AI · India MSME credit reasoning</span>' : '<span class="ai-badge muted">rule-based summary</span>';
    const sig = (n.signal_reasoning||[]).map(s=>`
      <div class="ews-reason">
        <div class="er-sig">${esc(s.signal||'')}</div>
        ${s.why_it_matters?`<p>${esc(s.why_it_matters)}</p>`:''}
        <div class="er-split">
          ${s.likely_benign_cause?`<div class="er-benign"><b>Could be benign</b> ${esc(s.likely_benign_cause)}</div>`:''}
          ${s.likely_risk_cause?`<div class="er-risk"><b>Could be risk</b> ${esc(s.likely_risk_cause)}</div>`:''}
        </div>
        ${s.clarification_to_seek?`<div class="er-ask"><b>Ask the customer:</b> ${esc(s.clarification_to_seek)}</div>`:''}
      </div>`).join('');
    box.innerHTML = `<div class="ews-ai-head">${tag}<button class="thesis-regen" onclick="loadEwsNarrative('${cid}')">↻ regenerate</button></div>
      ${n.overall_read?`<p class="ai-narr">${esc(n.overall_read)}</p>`:''}
      ${n.sma_view?`<div class="sma-view"><b>Asset-quality view (RBI SMA):</b> ${esc(n.sma_view)}</div>`:''}
      <details class="ews-detail" open><summary>Per-signal reasoning — benign vs risk, and what to ask</summary>${sig}</details>
      ${n.next_step?`<div class="er-next"><b>This week:</b> ${esc(n.next_step)}</div>`:''}`;
  }catch(e){ box.innerHTML = `<div class="muted">AI credit read unavailable: ${esc(e.message)}</div>`; }
}
function renderTimeline(rows){
  return (rows||[]).slice(0,12).map(e=>{
    const r = e.rich;
    const rich = r ? `
      <details class="case-rich">
        <summary>View full case</summary>
        <div class="cr-body">
          ${r.narrative?`<p class="cr-narr">${esc(r.narrative)}</p>`:''}
          ${(r.discussion_points&&r.discussion_points.length)?`<div class="cr-sec"><span>Discussed</span><ul>${r.discussion_points.map(d=>`<li>${esc(d)}</li>`).join('')}</ul></div>`:''}
          ${r.current_status_detail?`<div class="cr-sec"><span>Current status</span><p>${esc(r.current_status_detail)}</p></div>`:''}
          ${r.customer_position?`<div class="cr-sec"><span>Customer position</span><p>${esc(r.customer_position)}</p></div>`:''}
          ${r.rm_next_step?`<div class="cr-sec"><span>RM next step</span><p>${esc(r.rm_next_step)}</p></div>`:''}
        </div>
      </details>` : '';
    return `<div class="timeline ${esc(e.type)}">
      <div class="tl-date">${esc(String(e.date||'').slice(0,10))}</div>
      <div class="tl-body"><b>${esc(e.title)}</b><span class="tl-status">${esc(e.status)}</span>
        <p>${esc(short(e.detail,170))}</p>
        <div class="chips">${(e.evidence||[]).filter(Boolean).map(x=>`<span class="ref">${esc(x)}</span>`).join('')}</div>
        ${rich}
      </div></div>`;
  }).join('') || '<div class="muted">No CRM events.</div>';
}
function renderDocuments(ds){ const docs=(ds.documents||[]).filter(d=>['Pending','Expired','Overdue'].includes(d.status)); return `<div class="doc-grid">${docs.map(d=>`<div class="doc ${d.blocking_flag==='Y'?'block':''}"><b>${esc(d.document_type)}</b><span>${esc(d.status)}${d.blocking_flag==='Y'?' · blocker':''}</span><em>${esc(d.due_date||'')}</em></div>`).join('') || '<div class="success">No required documents pending.</div>'}</div>`; }
function renderServiceRecovery(tl){ const srv=(tl||[]).filter(e=>e.type==='service').slice(0,3); return `<div class="section-label">Service tickets</div>${srv.map(e=>`<div class="service-row"><b>${esc(e.title)}</b><span>${esc(e.status)}</span><p>${esc(e.detail)}</p></div>`).join('') || '<div class="muted">No recent service ticket.</div>'}`; }
function renderTransactions(txns){ return `<div class="txn-table"><div class="tr head"><span>Date</span><span>CR/DR</span><span>Amount</span><span>Counterparty</span><span>Category</span></div>${(txns||[]).map(t=>`<div class="tr"><span>${esc(t.txn_date)}</span><span class="${t.dr_cr==='CR'?'up':'down'}">${esc(t.dr_cr)}</span><span>${fmtINR(t.amount_inr)}</span><span>${esc(short(t.counterparty_name,28))}</span><span>${esc(t.category_lvl1)}${t.is_return==='Y'?' · return':''}</span></div>`).join('')}</div>`; }
function renderFinancials(ds){ const gst=(ds.gst_monthly||[]).slice(-4); return `<div class="mini-list"><div class="section-label">Latest GST returns</div>${gst.map(g=>`<div>${esc(g.period)} <b>${fmtINR(g.gst_sales_inr)}</b><span>${esc(g.trend_tag)} · variance ${esc(g.variance_vs_bank_credits_pct)}%</span></div>`).join('')}</div><div class="mini-list"><div class="section-label">Debtor aging</div>${metric('0-30', fmtINR(ds.aging.debtors_0_30_inr))}${metric('31-60', fmtINR(ds.aging.debtors_31_60_inr))}${metric('61-90', fmtINR(ds.aging.debtors_61_90_inr))}${metric('90+', fmtINR(ds.aging.debtors_90_plus_inr), Number(ds.aging.debtors_90_plus_inr)>2500000?'down':'')}</div><div class="mini-list"><div class="section-label">Top counterparties</div>${(ds.top_counterparties||[]).slice(0,5).map(cp=>`<div>${esc(cp.counterparty_name)} <b>${fmtINR(cp.total_value_inr)}</b><span>${esc(cp.counterparty_type)} · ${cp.transaction_count} txns</span></div>`).join('')}</div>`; }
function renderFootprint(fp){ return `<div class="footprint">${Object.entries(fp||{}).map(([k,v])=>`<div><strong>${esc(v)}</strong><span>${esc(k.replaceAll('_',' '))}</span></div>`).join('')}</div>`; }
async function openVoice(cid){
  try {
    const sess = await api('/v1/voice/sessions', { method:'POST', body: JSON.stringify({ customer_id: cid }) });
    const host = location.host;
    let voiceOrigin = location.origin;
    if (host.includes('ca-msme-dashboard')) voiceOrigin = location.protocol + '//' + host.replace('ca-msme-dashboard','ca-msme-voice');
    else if (host.includes('dashboard')) voiceOrigin = location.protocol + '//' + host.replace('dashboard','voice');
    const rmUrl = `${voiceOrigin}/rm/session/${sess.session_id}`;
    const customerUrl = `${voiceOrigin}/customer/session/${sess.session_id}`;
    try { await navigator.clipboard.writeText(customerUrl); } catch {}
    window.open(rmUrl, '_blank');
    toast('Two-device call created. Customer URL copied to clipboard.');
  } catch (e) {
    toast('Could not create live-call session: ' + e.message);
  }
}

// Step 7 (capstone): hand off to the Video Assist app, pre-bound to this MSME
// customer. The customer joins the RM's Teams meeting from this app; the synopsis
// and nudges post to the RM's Teams chat, grounded on this customer's evidence.
function openVideoCall(cid){
  const base = (typeof VIDEOASSIST_URL !== 'undefined' ? VIDEOASSIST_URL : '').replace(/\/+$/,'');
  if(!base || base.includes('invalid.local')){
    toast('Video Assist URL not configured (deploy phase9-videoassist).');
    return;
  }
  const custUrl = `${base}/?customer_id=${encodeURIComponent(cid)}`;
  try { navigator.clipboard.writeText(custUrl); } catch {}
  // Surface the exact link reliably (clipboard can fail under window.open focus
  // changes) and warn about in-app browsers, which are the usual cause of the
  // call "timing out" on a phone.
  window.prompt(
    'Share THIS link with '+cid+' for the video call.\n\n'+
    '⚠ The customer must open it in their phone\'s real browser (Chrome/Safari) — '+
    'NOT inside WhatsApp/Instagram/in-app browsers, which block camera & mic and cause the call to time out.\n\n'+
    'Steps: 1) start your Teams "Meet now" and copy the meeting link  2) customer opens the link below, pastes the meeting link, starts video  3) admit them from your Teams lobby.\n\n'+
    'Customer link (copied to clipboard):',
    custUrl
  );
  window.open(custUrl, '_blank');   // RM-side preview
  toast('Customer link ready for '+cid+'. Open it in a real mobile browser, not an in-app one.');
}
// Step 7: customer self-service scheduling page (served by the Video Assist app).
function bookingUrl(cid){ const base=(typeof VIDEOASSIST_URL!=='undefined'?VIDEOASSIST_URL:'').replace(/\/+$/,''); return (!base||base.includes('invalid.local'))?null:`${base}/schedule?customer_id=${encodeURIComponent(cid)}`; }
function openScheduling(cid){ const u=bookingUrl(cid); if(!u){ toast('Video Assist URL not configured (deploy phase9-videoassist).'); return; } window.open(u,'_blank'); toast('Opened the booking page for '+cid+'.'); }
// Step 7 (mobile): the customer's logged-in mobile banking portal with the one-tap
// "Video call your RM" instant-call flow. The RM-side Teams link is auto-generated.
function customerAppUrl(cid){ const base=(typeof VIDEOASSIST_URL!=='undefined'?VIDEOASSIST_URL:'').replace(/\/+$/,''); return (!base||base.includes('invalid.local'))?null:`${base}/bank?customer_id=${encodeURIComponent(cid)}`; }
function openCustomerApp(cid){ const u=customerAppUrl(cid); if(!u){ toast('Video Assist URL not configured (deploy phase9-videoassist).'); return; } window.open(u,'_blank'); toast('Opened the mobile banking app for '+cid+' (tap “Video call your RM”).'); }
function copyCustomerAppLink(cid){ const u=customerAppUrl(cid); if(!u){ toast('Video Assist URL not configured.'); return; } try{ navigator.clipboard.writeText(u); }catch{} window.prompt('Share this customer mobile-app link with '+cid+':', u); }
function copyBookingLink(cid){ const u=bookingUrl(cid); if(!u){ toast('Video Assist URL not configured.'); return; } try{ navigator.clipboard.writeText(u); }catch{} window.prompt('Share this booking link with '+cid+':', u); }
// Progressive-disclosure helpers (UI v6): staggered reveal + accordion toggle.
function staggerReveal(scope, sel){
  const root = (typeof scope==='string') ? document.querySelector(scope) : (scope||document);
  if(!root) return;
  const els = root.querySelectorAll(sel||'.reveal');
  els.forEach((el,i)=> el.style.setProperty('--d',(i*80)+'ms'));
  requestAnimationFrame(()=>requestAnimationFrame(()=> els.forEach(el=>el.classList.add('in'))));
}
function toggleCard(head){ const c = head.closest('.nba-card'); if(c) c.classList.toggle('open'); }

// Flagship RM use-case: Relationship Strategy & Next-Best-Action.
// Progressive disclosure: a Smart Summary Panel (conclusion + key insights +
// confidence + warnings) sits on top; each play is a COMPACT insight card; the
// full coaching (Why / Say / Don't / Policy basis / metadata + copy/export)
// opens in the contextual right-side detail drawer — nothing is dumped at once.
const _playSeverity = (p)=>{
  const e=String(p.eligibility||'').toLowerCase(), t=String(p.type||'').toLowerCase();
  if(e==='blocked') return 'neg';
  if(t==='grow'||e==='eligible') return 'pos';
  return 'warn';
};
const _playConf = (p)=>{ const e=String(p.eligibility||'').toLowerCase(); return e==='eligible'?90:(e==='conditional'?68:(e==='blocked'?35:60)); };

async function loadStrategy(cid){
  const box = document.getElementById('nbaMount'); if(!box) return;
  // structured skeleton + subtle generation indicator (stable layout, no jank)
  box.innerHTML = `<div class="rx-gen"><span class="dot"></span> Generating relationship strategy…</div>
    <div class="rx-sk line w40"></div><div class="rx-sk card"></div>${RX.skeleton ? RX.skeleton(3) : ''}`;
  try{
    const n = await getNBA(cid);
    const live = n.generated_by==='llm_grounded';
    const conf = live ? 86 : 62;
    const plays = n.plays||[];
    const eligibleCount = plays.filter(p=>String(p.eligibility||'').toLowerCase()!=='blocked').length;
    const dno = n.do_not_offer||[]; const cases = n.open_cases||[];
    const aiBadge = live ? RX.badge('AI · grounded','ai') : RX.badge('rule-based','');
    const warnBadge = dno.length ? RX.badge(dno.length+' suppressed','neg','&#9888;') : '';
    // ---- Smart Summary Panel ----
    const summary = `<div class="rx-summary rx-reveal">
      <div class="head"><h2>${esc(n.headline || (n.stance ? n.stance+' — relationship strategy' : 'Relationship strategy'))}</h2>
        ${RX.badge(n.stance||'Strategy','accent')}</div>
      ${n.relationship_read?`<p class="lead">${esc(n.relationship_read)}</p>`:''}
      <div class="meta-row">${aiBadge} ${RX.confidence(conf,'confidence')} ${warnBadge}
        <span style="flex:1"></span>
        <button class="rx-btn ghost sm" data-tip="Re-run the strategy" onclick="regenerateStrategy('${cid}')">&#8635; Regenerate</button>
        ${plays.length?`<button class="rx-btn primary sm" onclick="openStrategyPlay(0)">Dive into top play</button>`:''}
      </div>
      <div class="insights">
        <div class="ins"><div class="k">Stance</div><div class="v">${esc(n.stance||'—')}</div></div>
        <div class="ins"><div class="k">Eligible plays</div><div class="v">${eligibleCount} of ${plays.length}</div></div>
        <div class="ins"><div class="k">Suppressed by policy</div><div class="v">${dno.length}</div></div>
        <div class="ins"><div class="k">Open cases</div><div class="v">${cases.length}</div></div>
      </div></div>`;
    // ---- compact insight cards (click → drawer) ----
    const cards = plays.map((p,i)=>{
      const sev=_playSeverity(p), c=_playConf(p);
      const sum = esc((p.rationale||p.say||p.product||'').replace(/\s+/g,' ').slice(0,96));
      return `<article class="rx-card sev-${sev} rx-reveal" tabindex="0" role="button" aria-label="Open play: ${esc(p.title||p.product||'play')}"
          onclick="openStrategyPlay(${i})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openStrategyPlay(${i});}">
        <div class="body">
          <div class="top"><div class="t"><b>${esc(p.title||p.product||'Recommended play')}</b><span class="sum">${sum}</span></div>
            <span class="rx-badge ${sev==='neg'?'neg':(sev==='pos'?'pos':'warn')}">${esc(String(p.eligibility||'play').toUpperCase())}</span></div>
          <div class="foot">${RX.confidence(c)}
            ${p.the_number?`<span class="metric">${esc(String(p.the_number))}</span>`:''}
            <span class="spacer"></span>
            <span class="open-cue">View coaching &#8594;</span></div>
        </div></article>`;
    }).join('');
    const dnoCard = dno.length ? `<article class="rx-card sev-neg rx-reveal" tabindex="0" role="button" aria-label="View suppressed offers"
        onclick="openSuppressed()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openSuppressed();}">
      <div class="body"><div class="top"><div class="t"><b>Do NOT offer &middot; ${dno.length} suppressed</b>
        <span class="sum">Blocked by deterministic eligibility — tap to see why</span></div>
        ${RX.badge('policy gate','neg','&#9888;')}</div></div></article>` : '';
    box.innerHTML = summary
      + `<div class="rx-cards" aria-label="Recommended plays">${cards || '<div class="rx-empty"><div class="ic">&#9675;</div>No actionable play right now.</div>'}${dnoCard}</div>`;
    if (RX.reveal) RX.reveal(box, '.rx-reveal');
  }catch(e){ box.innerHTML = `<div class="rx-error"><div class="ic">&#9888;</div>Strategy unavailable: ${esc(e.message)}</div>`; }
}

// Open a single play in the contextual detail drawer (full coaching + actions).
function openStrategyPlay(i){
  const N = window.__NBA; if(!N || !N.data || !N.data.plays || !N.data.plays[i]) return;
  const p = N.data.plays[i], cid = N.cid;
  const sev=_playSeverity(p);
  const badges = [
    `<span class="rx-badge ${sev==='neg'?'neg':(sev==='pos'?'pos':'warn')}">${esc(String(p.eligibility||'play').toUpperCase())}</span>`,
    RX.confidence(_playConf(p)),
    p.the_number?`<span class="rx-badge accent">${esc(String(p.the_number))}</span>`:''
  ];
  const sections = [];
  if(p.rationale) sections.push({label:'Why · RM read', html:`<div class="rx-block why">${esc(p.rationale)}</div>`});
  if(p.say) sections.push({label:'Say to the customer', html:`<div class="rx-block say">&ldquo;${esc(p.say)}&rdquo;</div>`});
  if(p.guardrail) sections.push({label:"Guardrail — don't", html:`<div class="rx-block dont">${esc(p.guardrail)}</div>`});
  if(p.sop_basis) sections.push({label:'Policy basis (internal)', html:`<div class="rx-block basis">${esc(p.sop_basis)}</div>`});
  sections.push({label:'Details', html:`<dl class="rx-kv">
    <dt>Product</dt><dd>${esc(p.product||'—')}</dd>
    <dt>Type</dt><dd>${esc(p.type||'—')}</dd>
    <dt>Eligibility</dt><dd>${esc(p.eligibility||'—')}</dd>
    <dt>Key figure</dt><dd>${esc(p.the_number||'—')}</dd></dl>`});
  const copyText = `${p.title||p.product}\nSay: ${p.say||''}\nWhy: ${p.rationale||''}\nGuardrail: ${p.guardrail||''}\nBasis: ${p.sop_basis||''}`;
  RX.drawer.open({
    title: p.title || p.product || 'Recommended play',
    subtitle: `${N.data.stance||''} · ${cid}`,
    badges, sections,
    actions: [
      { label:'Copy talk-track', icon:'&#9112;', onClick:()=>RX.copy(p.say||'', null) },
      { label:'Copy full play', onClick:()=>RX.copy(copyText, null) },
      { label:'Start video call', kind:'primary', icon:'&#9658;', onClick:(drw)=>{ drw.close(); try{ openVideoCall(cid); }catch(e){} } }
    ]
  });
}

// Open the "do not offer" rationale in the drawer (why each was suppressed).
function openSuppressed(){
  const N = window.__NBA; if(!N || !N.data) return;
  const dno = N.data.do_not_offer||[];
  const html = dno.map(x=>`<div class="rx-block dont" style="margin-bottom:8px"><b>${esc(x.product)}</b><br>${esc(x.reason)}</div>`).join('') || '<div class="rx-empty">None.</div>';
  RX.drawer.open({ title:'Suppressed offers', subtitle:`Eligibility-gated · ${N.cid}`,
    badges:[RX.badge('policy gate','neg','&#9888;')],
    sections:[{label:'Why these are not on the table now', html}] });
}
async function loadCrossSell(cid) { /* kept for backward compatibility */ }
async function draftMemo(cid) { $("memoPanel").style.display = "block"; $("memoBody").innerHTML = skelPanel("Drafting evidence-cited memo\u2026", 6); try { const m = await api("/v1/memo/renewal-draft", { method: "POST", body: JSON.stringify({ customer_id: cid }) }); $("memoBody").innerHTML = m.sections.map(s => `<div class="memo-sec"><div class="st">${esc(s.section)}</div><div class="tx">${esc(s.text)}</div>${s.evidence_refs ? `<div class="refs">${s.evidence_refs.map(r => `<span class="ref">${esc(r)}</span>`).join("")}</div>` : ""}</div>`).join("") + `<div class="disclaimer">⚠ ${esc(m.disclaimer)}</div>`; refreshAudit(); toast("Memo drafted — logged to audit trail"); } catch (e) { $("memoBody").innerHTML = `<div class="loading">Failed: ${esc(e.message)}</div>`; } }
async function proposeTask(cid) { try { const cand = await api("/v1/crm/update-candidate", { method: "POST", body: JSON.stringify({ customer_id: cid, type: "task", payload: { title: "RM follow-up: collect pending documents and validate order pipeline", due: "2026-04-10", priority:"High" }, evidence_refs: ["documents", "crm.playbook"] }), }); refreshAudit(); if (confirm(`Proposed: "${cand.payload.title}"\n\nApprove and save to CRM?`)) { await api("/v1/crm/approve-update", { method: "POST", body: JSON.stringify({ candidate_id: cand.candidate_id, approver: "RM-1042" }) }); toast("Task approved & saved to CRM"); await selectCustomer(cid); } else { toast("Left pending — not saved"); } } catch (e) { toast("Write blocked: " + e.message); } }

async function askPolicy() { const inp = $("chatIn"), q = inp.value.trim(); if (!q) return; inp.value = ""; const log = $("chatLog"); log.innerHTML += `<div class="msg u">${esc(q)}</div><div class="msg a" id="pending">…</div>`; log.scrollTop = log.scrollHeight; try { const r = await api("/v1/rag/retrieve", { method: "POST", body: JSON.stringify({ query: q, top_k: 3 }) }); const pend = $("pending"); pend.id = ""; if (r.grounded && r.results.length) { const top = r.results[0]; pend.innerHTML = `Per <strong>${esc(top.sop_title)}</strong> — ${esc(top.section_title)}:<br>${esc((top.content||"").replace(/^#+.*$/m,"").slice(0,260))}…<div class="cites">${r.results.map(x => `<span class="ref">${esc(x.sop_id)} · ${esc(x.section_title)}</span>`).join("")}</div>`; } else { pend.classList.add("notfound"); pend.innerHTML = `No matching Contoso policy found in indexed SOPs.`; } } catch (e) { const pend = $("pending"); pend.id = ""; pend.innerHTML = "Retrieval error: " + esc(e.message); } log.scrollTop = log.scrollHeight; }
async function refreshAudit() { try { const d = await api("/v1/audit/events?limit=60"); const evs = (d.events || []).slice().reverse(); $("events").innerHTML = evs.length ? evs.map(e => `<div class="evt"><div class="et">${esc(e.event_type)}</div><div class="ets">${esc(e.timestamp)}</div><div class="ep">${esc(JSON.stringify(e.payload))}</div></div>`).join("") : `<div class="loading">No events yet.</div>`; } catch { } }

/* ============================================================================
   RAW DATA explorer — menu: "Raw Data"
   A two-pane, in-CRM browser over the exact synthetic customer pack, the
   knowledge-base rules and the Indian-banking SOP corpus that ground every AI
   answer and nudge. Proves the demo is fact-backed: CSV -> sortable table,
   JSON -> highlighted tree, Markdown SOP -> rendered policy. Zero dependencies.
   ============================================================================ */
let RD_CATALOG = null;      // {groups:[{label,files:[...]}], total_files, roots}
let RD_ACTIVE = null;       // active file id
let RD_FILECACHE = {};      // id -> file payload (content cached per session)
let RD_SEARCH = "";         // sidebar file filter
let RD_CSV = null;          // parsed current CSV {cols,data,cap,numCols}
let RD_PROFILE = null;      // cached Customer 360 payload
const RD_PROFILE_ID = '__c360__';

function renderRawData(){
  $("content").innerHTML = `
    <div class="rd-wrap">
      <aside class="rd-sidebar">
        <div class="rd-sb-head">
          <h3>Raw data &amp; policy pack</h3>
          <p id="rdSubhead">The exact records and SOPs that ground every AI answer.</p>
          <div class="rd-search"><span class="ic">⌕</span><input id="rdSearch" placeholder="Search files…" oninput="rdFilter(this.value)" /></div>
        </div>
        <div class="rd-list" id="rdList"><div class="rd-loading">Loading catalog…</div></div>
      </aside>
      <section class="rd-main">
        <div class="rd-view" id="rdView">
          <div class="rd-empty">
            <div class="big">Verifiable source data</div>
            <div>Select any file on the left to render the real customer records, knowledge-base rules or SOP policy — live, inside the CRM.</div>
          </div>
        </div>
      </section>
    </div>`;
  window.scrollTo({top:0,behavior:'smooth'});
  rdLoadCatalog();
}

async function rdLoadCatalog(){
  try{
    RD_CATALOG = await api('/v1/rawdata/catalog');
    const sh = $("rdSubhead");
    if(sh && RD_CATALOG) sh.textContent = `${RD_CATALOG.total_files} source files · customer data, KB rules & RM SOPs.`;
    rdRenderList();
    if(RD_ACTIVE){ if(RD_ACTIVE===RD_PROFILE_ID) rdOpenProfile(); else rdOpen(RD_ACTIVE); return; }
    rdOpenProfile();   // land on the single-click Customer 360 by default
  }catch(e){
    const list=$("rdList");
    if(list) list.innerHTML = `<div class="rd-loading">Could not load catalog.<br><span class="rd-meta-dim">${esc(e.message)}</span></div>`;
  }
}

function rdMatchesSearch(f){
  if(!RD_SEARCH) return true;
  const q = RD_SEARCH.toLowerCase();
  return (f.name+' '+f.description+' '+f.id+' '+f.file).toLowerCase().includes(q);
}
function rdFilter(v){ RD_SEARCH=(v||'').trim(); rdRenderList(); }

function rdRenderList(){
  const list=$("rdList");
  if(!list || !RD_CATALOG) return;
  const html = RD_CATALOG.groups.map(g=>{
    const files = g.files.filter(rdMatchesSearch);
    if(!files.length) return '';
    const items = files.map(f=>{
      const active = f.id===RD_ACTIVE ? ' active' : '';
      const right = f.rows!=null ? `<span class="rd-meta-dim">${Number(f.rows).toLocaleString('en-IN')} rows</span>`
                                 : `<span class="rd-meta-dim">${rdBytes(f.size)}</span>`;
      return `<button class="rd-file${active}" onclick="rdOpen('${esc(f.id)}')">
        <div class="fn">${esc(f.name)}</div>
        <div class="fd">${esc(f.description)}</div>
        <div class="fm">${rdTypeBadge(f.type)}${right}</div>
      </button>`;
    }).join('');
    return `<div class="rd-group"><span>${esc(g.label)}</span><span>${files.length}</span></div>${items}`;
  }).join('');
  const pin = RD_SEARCH ? '' : rdPinCard();
  list.innerHTML = pin + (html || (RD_SEARCH ? `<div class="rd-loading">No files match “${esc(RD_SEARCH)}”.</div>` : ''));
}

function rdPinCard(){
  const active = RD_ACTIVE===RD_PROFILE_ID ? ' active' : '';
  return `<button class="rd-c360${active}" onclick="rdOpenProfile()">
    <div class="c360-star">★</div>
    <div class="c360-tx">
      <div class="c360-t">Rakesh Sharma — Customer 360</div>
      <div class="c360-d">Full history in one click: KYC, accounts, loans, CIBIL, spends &amp; disputes.</div>
    </div>
  </button>`;
}

async function rdOpen(id){
  RD_ACTIVE = id;
  rdRenderList();
  const view=$("rdView");
  if(view) view.innerHTML = '<div class="rd-loading">Loading…</div>';
  try{
    let f = RD_FILECACHE[id];
    if(!f){ f = await api('/v1/rawdata/file?id='+encodeURIComponent(id)); RD_FILECACHE[id]=f; }
    rdRenderFile(f);
  }catch(e){
    if(view) view.innerHTML = `<div class="rd-loading">Could not load this file.<br><span class="rd-meta-dim">${esc(e.message)}</span></div>`;
  }
}

function rdRenderFile(f){
  const view=$("rdView");
  if(!view) return;
  const meta = [ f.rows!=null?`${Number(f.rows).toLocaleString('en-IN')} rows`:null, rdBytes(f.size) ].filter(Boolean).join(' · ');
  let body;
  if(f.type==='csv')        body = rdRenderCSV(f);
  else if(f.type==='json')  body = `<div class="rd-scroll">${rdRenderJSON(f)}</div>`;
  else if(f.type==='md')    body = `<div class="rd-scroll"><div class="rd-md">${rdMdToHtml(f.content)}</div></div>`;
  else                      body = `<div class="rd-scroll"><pre class="rd-pre">${esc(f.content)}</pre></div>`;
  view.innerHTML = `
    <div class="rd-view-head">
      <div class="vh-t">
        <h2>${esc(f.name)} ${rdTypeBadge(f.type)}</h2>
        <div class="path">${esc(f.id)} · ${esc(meta)}</div>
        ${f.description?`<p>${esc(f.description)}</p>`:''}
      </div>
      <button class="rx-btn sm rd-dl" onclick="rdDownload('${esc(f.id)}')"><span class="ic">↓</span> Download</button>
    </div>
    <div class="rd-body" id="rdBody">${body}</div>`;
  if(f.type==='csv') rdRenderCsvBody('');
}

/* ---- CSV → table (client-side parse, filter, cap) ---- */
function rdParseCSV(text){
  const rows=[]; let row=[]; let cur=""; let q=false; const n=text.length; let i=0;
  while(i<n){
    const c=text[i];
    if(q){
      if(c==='"'){ if(text[i+1]==='"'){ cur+='"'; i+=2; continue; } q=false; i++; continue; }
      cur+=c; i++; continue;
    }
    if(c==='"'){ q=true; i++; continue; }
    if(c===','){ row.push(cur); cur=""; i++; continue; }
    if(c==='\r'){ i++; continue; }
    if(c==='\n'){ row.push(cur); rows.push(row); row=[]; cur=""; i++; continue; }
    cur+=c; i++;
  }
  if(cur!=="" || row.length){ row.push(cur); rows.push(row); }
  return rows;
}
function rdNumericCols(cols, data){
  const set=new Set(); const sample=data.slice(0,60); const re=/^-?[\d,]+(\.\d+)?$/;
  cols.forEach((_,ci)=>{
    let n=0, ok=0;
    sample.forEach(r=>{ const v=String(r[ci]||'').trim(); if(v){ n++; if(re.test(v)) ok++; } });
    if(n>=3 && ok/n>=0.8) set.add(ci);
  });
  return set;
}
function rdRenderCSV(f){
  const rows = rdParseCSV(f.content||'');
  const cols = rows.length ? rows[0] : [];
  const data = rows.slice(1);
  RD_CSV = { cols, data, cap:500, numCols: rdNumericCols(cols, data) };
  const head = cols.map(c=>`<th>${esc(c)}</th>`).join('');
  return `
    <div class="rd-csv">
      <div class="rd-toolbar">
        <div class="rd-search rd-mini"><span class="ic">⌕</span><input id="rdCsvFilter" placeholder="Filter rows…" oninput="rdCsvFilter(this.value)" /></div>
        <div class="n" id="rdCsvCount"></div>
      </div>
      <div class="rd-tbl-wrap">
        <table class="rd-tbl">
          <thead><tr><th class="rd-idx">#</th>${head}</tr></thead>
          <tbody id="rdCsvBody"></tbody>
        </table>
      </div>
    </div>`;
}
function rdRenderCsvBody(q){
  if(!RD_CSV) return;
  const s=String(q||'').toLowerCase();
  let data = RD_CSV.data;
  if(s) data = data.filter(r=>r.some(c=>String(c).toLowerCase().includes(s)));
  const total = data.length;
  const shown = data.slice(0, RD_CSV.cap);
  const num = RD_CSV.numCols;
  const rowsHtml = shown.map((r,i)=>{
    const tds = RD_CSV.cols.map((_,ci)=>{
      const v = r[ci]!=null ? r[ci] : '';
      return `<td${num.has(ci)?' class="num"':''} title="${esc(v)}">${esc(v)}</td>`;
    }).join('');
    return `<tr><td class="rd-idx">${i+1}</td>${tds}</tr>`;
  }).join('');
  const bodyEl=$("rdCsvBody");
  if(bodyEl) bodyEl.innerHTML = rowsHtml || `<tr><td class="rd-nomatch" colspan="${RD_CSV.cols.length+1}">No rows match “${esc(q)}”.</td></tr>`;
  const cnt=$("rdCsvCount");
  if(cnt) cnt.textContent = total>RD_CSV.cap
    ? `Showing ${RD_CSV.cap.toLocaleString('en-IN')} of ${total.toLocaleString('en-IN')} rows`
    : `${total.toLocaleString('en-IN')} row${total===1?'':'s'}`;
}
function rdCsvFilter(v){ rdRenderCsvBody(v); }

/* ---- JSON → highlighted ---- */
function rdRenderJSON(f){
  let pretty;
  try{ pretty = JSON.stringify(JSON.parse(f.content), null, 2); }
  catch(e){ return `<pre class="rd-pre">${esc(f.content)}</pre>`; }
  let h = esc(pretty);
  h = h.replace(/&quot;[^&]*?&quot;/g, s=>`<span class="s">${s}</span>`);
  h = h.replace(/<span class="s">(&quot;[^&]*?&quot;)<\/span>(\s*:)/g, '<span class="k">$1</span>$2');
  h = h.replace(/\b(true|false|null)\b/g, '<span class="b">$1</span>');
  h = h.replace(/(:\s*)(-?\d+(?:\.\d+)?)(?=[,\n\r}\]])/g, '$1<span class="nu">$2</span>');
  return `<pre class="rd-json">${h}</pre>`;
}

/* ---- Markdown (SOP) → HTML (safe subset: headings, lists, bold, code, hr) ---- */
function rdInlineMd(s){
  return s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>');
}
function rdMdToHtml(md){
  const lines = String(md||'').replace(/\r\n/g,'\n').split('\n');
  let html=''; let list=null;
  const closeList=()=>{ if(list){ html+=`</${list}>`; list=null; } };
  for(const raw of lines){
    const t = raw.trim();
    if(!t){ closeList(); continue; }
    if(/^#{1,6}\s+/.test(t)){
      closeList();
      const level = t.match(/^#+/)[0].length;
      html += `<h${level}>${rdInlineMd(esc(t.replace(/^#{1,6}\s+/,'')))}</h${level}>`;
      continue;
    }
    if(/^(-{3,}|\*{3,}|_{3,})$/.test(t)){ closeList(); html+='<hr>'; continue; }
    if(/^[-*]\s+/.test(t)){
      if(list!=='ul'){ closeList(); html+='<ul>'; list='ul'; }
      html += `<li>${rdInlineMd(esc(t.replace(/^[-*]\s+/,'')))}</li>`;
      continue;
    }
    if(/^\d+\.\s+/.test(t)){
      if(list!=='ol'){ closeList(); html+='<ol>'; list='ol'; }
      html += `<li>${rdInlineMd(esc(t.replace(/^\d+\.\s+/,'')))}</li>`;
      continue;
    }
    closeList();
    html += `<p>${rdInlineMd(esc(t))}</p>`;
  }
  closeList();
  return html;
}

/* ---- misc helpers ---- */
function rdTypeBadge(t){ const c=(t==='csv'||t==='json'||t==='md')?t:'txt'; return `<span class="rd-badge ${c}">${c}</span>`; }
function rdBytes(n){ n=Number(n)||0; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(2)+' MB'; }
function rdDownload(id){
  const f = RD_FILECACHE[id]; if(!f) return;
  const blob = new Blob([f.content], {type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href=url; a.download=f.file||'data.txt';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 1500);
  toast('Downloaded '+(f.file||''));
}

/* ---- Customer 360 (single-click full profile) ---- */
async function rdOpenProfile(){
  RD_ACTIVE = RD_PROFILE_ID;
  rdRenderList();
  const view=$("rdView");
  if(view) view.innerHTML = '<div class="rd-loading">Assembling Customer 360…</div>';
  try{
    if(!RD_PROFILE) RD_PROFILE = await api('/v1/rawdata/profile');
    rdRenderProfile(RD_PROFILE);
  }catch(e){
    if(view) view.innerHTML = `<div class="rd-loading">Could not load Customer 360.<br><span class="rd-meta-dim">${esc(e.message)}</span></div>`;
  }
}

function rdINR(n){ n=Number(n)||0; return '₹'+Math.round(n).toLocaleString('en-IN'); }
function rdPill(text, tone){ return text ? `<span class="rd-pill ${tone||''}">${esc(String(text))}</span>` : ''; }
function rd360Card(title, sub, inner, span){
  return `<section class="c360-card${span?' span2':''}">
    <header><h3>${esc(title)}</h3>${sub?`<span>${esc(sub)}</span>`:''}</header>
    <div class="c360-cbody">${inner}</div></section>`;
}
function rdDefList(pairs){
  const rows = pairs.filter(p=>p[1]!=='' && p[1]!=null)
    .map(p=>`<div class="c360-dl"><dt>${esc(p[0])}</dt><dd>${p[2]?p[1]:esc(String(p[1]))}</dd></div>`).join('');
  return `<div class="c360-dls">${rows}</div>`;
}
function rdStatusTone(s){
  s=String(s||'').toLowerCase();
  if(/(open|pending|due|overdue|bounce|sma|subprime|unauth)/.test(s)) return 'danger';
  if(/(delay|in progress|in-progress|partial|hold)/.test(s)) return 'warn';
  if(/(closed|received|active|paid|clean|resolved)/.test(s)) return 'ok';
  return 'dim';
}

function rdRenderProfile(p){
  const view=$("rdView"); if(!view||!p) return;
  const c = p.counts||{};
  // KPI chips
  const kpis = (p.kpis||[]).map(k=>`
    <div class="c360-kpi k--${esc(k.tone||'info')}">
      <div class="kl">${esc(k.label)}</div>
      <div class="kv">${esc(k.value)}</div>
      ${k.sub?`<div class="ks">${esc(k.sub)}</div>`:''}
    </div>`).join('');

  // Identity + business
  const idCard = rd360Card('Identity & profile', p.segment||'', rdDefList([
    ['Legal name', p.identity.legal_name],
    ['Constitution', p.identity.constitution],
    ['PAN', p.identity.pan_masked],
    ['Customer since', p.identity.customer_since],
    ['Home branch', p.identity.home_branch_code],
    ['Relationship manager', p.identity.rm_id],
    ['Risk category', p.risk_category],
    ['Occupation', p.business.industry_description],
    ['Location', p.business.registered_address],
  ]) + (p.business.risk_notes?`<p class="c360-note danger"><b>Risk:</b> ${esc(p.business.risk_notes)}</p>`:'')
     + (p.business.growth_notes?`<p class="c360-note"><b>Stance:</b> ${esc(p.business.growth_notes)}</p>`:''));

  // KYC & consent
  const blk = (p.kyc.blocking_documents||[]).map(d=>`<li>${rdPill('BLOCKING','danger')} ${esc(d.document_type)} — ${esc(d.status)}${d.remarks?` · <span class="rd-meta-dim">${esc(d.remarks)}</span>`:''}</li>`).join('');
  const cons = (p.kyc.consents||[]).map(x=>`<li>${esc(x.consent_type||'')} ${rdPill(x.consent_status||'', rdStatusTone(x.consent_status))}<span class="rd-meta-dim"> ${esc(x.purpose||'')}</span></li>`).join('');
  const kycCard = rd360Card('KYC & consent', 'Re-KYC gating', rdDefList([
    ['KYC status', rdPill(p.kyc.status, rdStatusTone(p.kyc.status)), true],
    ['Next re-KYC due', p.kyc.next_kyc_due_date],
    ['Consent status', p.kyc.consent_status],
  ]) + (blk?`<div class="c360-sub">Blocking documents</div><ul class="c360-ul">${blk}</ul>`:'')
     + (cons?`<div class="c360-sub">Consents on file</div><ul class="c360-ul tight">${cons}</ul>`:''));

  // Facilities
  const facRows = (p.facilities||[]).map(f=>`
    <tr>
      <td>${esc(f.facility_type)}<div class="rd-meta-dim">${esc(f.facility_id)}</div></td>
      <td class="num">${rdINR(f.current_outstanding_inr)}</td>
      <td class="num">${rdINR(f.sanction_limit_inr)}</td>
      <td class="num">${f.interest_rate_pct}%</td>
      <td class="num">${f.monthly_finance_charge_inr!=null?rdINR(f.monthly_finance_charge_inr):'—'}</td>
      <td>${rdPill(f.status, rdStatusTone(f.status))}</td>
    </tr>`).join('');
  const facCard = rd360Card('Credit facilities', p.utilization&&p.utilization.utilization_pct?`card utilisation ${p.utilization.utilization_pct}%${p.utilization.over_limit_flag==='Y'?' · OVER-LIMIT':''}`:'', `
    <table class="c360-tbl"><thead><tr><th>Facility</th><th class="num">Outstanding</th><th class="num">Limit</th><th class="num">Rate</th><th class="num">Monthly interest</th><th>Status</th></tr></thead>
    <tbody>${facRows}</tbody></table>`, true);

  // Bureau
  const b=p.bureau||{};
  const bureauCard = rd360Card('Credit bureau (CIBIL)', b.as_of?`as of ${b.as_of}`:'', rdDefList([
    ['Score', `<span class="c360-score ${b.score&&b.score<700?'bad':'good'}">${b.score||'—'}</span>`, true],
    ['Band', b.band],
    ['Enquiries (6m)', b.enquiries_6m],
    ['DPD count', b.dpd_count],
    ['Remarks', b.remarks],
  ]));

  // Spend analytics
  const sp=p.spend||{}; const cats=(sp.by_category||[]).filter(x=>x.debit_inr>0).sort((a,b2)=>b2.debit_inr-a.debit_inr);
  const maxD = cats.length?cats[0].debit_inr:1;
  const bars = cats.map(x=>`
    <div class="c360-bar">
      <div class="bl"><span>${esc(x.category)}</span><span class="rd-meta-dim">${rdINR(x.debit_inr)} · ${x.count} txns · ${x.pct_of_debit}%</span></div>
      <div class="bt"><i style="width:${Math.max(3,Math.round(x.debit_inr/maxD*100))}%"></i></div>
    </div>`).join('');
  const spendCard = rd360Card('Spend & cash-flow analytics', sp.window||'', `
    <div class="c360-stats">
      <div><b>${rdINR(sp.total_debit_inr)}</b><span>total debits</span></div>
      <div><b>${rdINR(sp.total_credit_inr)}</b><span>total credits (inflows)</span></div>
      <div><b>${Number(sp.txn_count||0).toLocaleString('en-IN')}</b><span>transactions</span></div>
    </div>
    ${(sp.recent||[]).length>2?`<div class="c360-sub">Transaction size trend <span class="rd-meta-dim">— latest ${(sp.recent||[]).length}, oldest to newest</span></div>
    <svg id="rxSparkTxn" class="br-spark rx-spark" viewBox="0 0 320 60" preserveAspectRatio="none"></svg>`:''}
    <div class="c360-sub">Debit spend by category</div>${bars}`, true);

  // Recent transactions
  const rtx = (sp.recent||[]).map(t=>`
    <tr>
      <td>${esc(t.txn_date)}</td>
      <td class="${t.dr_cr==='CR'?'cr':'dr'}">${t.dr_cr}</td>
      <td class="num ${t.dr_cr==='CR'?'cr':'dr'}">${rdINR(t.amount_inr)}</td>
      <td>${esc(t.category_lvl1)}</td>
      <td>${esc(t.description)}${t.anomaly_tag?` ${rdPill(t.anomaly_tag,'danger')}`:''}</td>
      <td class="num">${rdINR(t.balance_after_txn_inr)}</td>
    </tr>`).join('');
  const txCard = rd360Card('Recent transactions', 'latest 15', `
    ${(sp.recent||[]).length>2?`<div class="c360-sub">Balance trend <span class="rd-meta-dim">— running balance across these transactions</span></div>
    <svg id="rxSparkBal" class="br-spark rx-spark" viewBox="0 0 320 60" preserveAspectRatio="none"></svg>`:''}
    <table class="c360-tbl"><thead><tr><th>Date</th><th>Dr/Cr</th><th class="num">Amount</th><th>Category</th><th>Narration</th><th class="num">Balance</th></tr></thead>
    <tbody>${rtx}</tbody></table>`, true);

  // Repayments
  const rp=p.repayments||{}; const rpRows=(rp.rows||[]).map(r=>`
    <tr><td>${esc(r.due_date)}</td><td class="num">${rdINR(r.amount_due_inr)}</td><td class="num">${rdINR(r.amount_paid_inr)}</td>
    <td class="num">${esc(r.days_past_due)}</td><td>${rdPill(r.payment_status, rdStatusTone(r.payment_status))}</td></tr>`).join('');
  const rpCard = rd360Card('Loan repayments', `${rp.bounced||0} bounced · ${rp.delayed||0} delayed`, `
    <table class="c360-tbl"><thead><tr><th>Due</th><th class="num">Due ₹</th><th class="num">Paid ₹</th><th class="num">DPD</th><th>Status</th></tr></thead>
    <tbody>${rpRows}</tbody></table>`);

  // Disputes / SRs
  const dsp = (p.disputes||[]).map(d=>`
    <div class="c360-row">
      <div class="rt"><b>${esc(d.category)}</b> ${rdPill(d.status, rdStatusTone(d.status))} ${rdPill(d.priority,'dim')}</div>
      <div class="rd-meta-dim">${esc(d.ticket_id)} · raised ${esc(d.created_date)}${d.sla_due_date?` · SLA ${esc(d.sla_due_date)}`:''}</div>
      <div class="rr">${esc(d.description)}</div>
    </div>`).join('');
  const dspCard = rd360Card('Disputes & service requests', `${(p.disputes||[]).filter(d=>rdStatusTone(d.status)==='danger').length} open`, dsp||'<div class="rd-meta-dim">None on file.</div>');

  // Documents
  const dcs = (p.documents||[]).map(d=>`<li>${d.blocking_flag==='Y'?rdPill('BLOCKING','danger'):''} ${esc(d.document_type)} ${rdPill(d.status, rdStatusTone(d.status))}</li>`).join('');
  const dcCard = rd360Card('Documents', `${c.documents||0} on file`, `<ul class="c360-ul">${dcs}</ul>`);

  // Guarantors / family
  const grs = (p.guarantors||[]).map(g=>`<li><b>${esc(g.name)}</b> ${rdPill(g.role,'dim')} <span class="rd-meta-dim">${esc(g.bureau_score_band||'')}${g.net_worth_band_inr?` · ${esc(g.net_worth_band_inr)}`:''}</span></li>`).join('');
  const grCard = rd360Card('Promoters / guarantors', 'co-obligant liability', grs?`<ul class="c360-ul">${grs}</ul>`:'<div class="rd-meta-dim">None on file.</div>');

  // Opportunities
  const ops = (p.opportunities||[]).map(o=>`<li><b>${esc(o.opportunity_type)}</b> ${rdPill(o.stage,'dim')} ${rdPill(o.status,rdStatusTone(o.status))}${o.blockers?`<div class="rd-meta-dim">Blockers: ${esc(o.blockers)}</div>`:''}</li>`).join('');
  const opCard = rd360Card('Opportunities', '', ops?`<ul class="c360-ul">${ops}</ul>`:'<div class="rd-meta-dim">Service-recovery only.</div>');

  // Interactions timeline
  const its = (p.interactions||[]).map(it=>`
    <div class="c360-tl">
      <div class="tld">${esc(it.interaction_date)} · ${esc(it.channel)} ${rdPill(it.sentiment, rdStatusTone(it.sentiment))}</div>
      <div class="tlt"><b>${esc(it.subject)}</b></div>
      <div class="tls">${esc(it.summary)}</div>
    </div>`).join('');
  const itCard = rd360Card('RM interactions', `${c.interactions||0} logged`, its||'<div class="rd-meta-dim">None logged.</div>', true);

  const ribbon = [
    [c.transactions,'transactions'],[c.accounts,'accounts'],[c.facilities,'facilities'],
    [c.service_requests,'service requests'],[c.documents,'documents'],[c.cheque_returns,'bounces'],
    [c.opportunities,'opportunities'],[c.interactions,'interactions'],
  ].map(x=>`<span><b data-count="${Number(x[0]||0)}">0</b> ${x[1]}</span>`).join('');

  view.innerHTML = `
    <div class="rd-view-head c360-head">
      <div class="vh-t">
        <h2><span class="c360-hstar">★</span> ${esc(p.name)} <span class="c360-cid">${esc(p.customer_id)}</span></h2>
        <div class="path">Customer 360 · assembled live from ${Number(c.transactions||0).toLocaleString('en-IN')} transactions + ${(c.accounts||0)+(c.facilities||0)} accounts/facilities + KYC, bureau &amp; CRM</div>
        <p>${esc(p.segment)} — a single, verifiable view of everything the bank knows about this customer.</p>
      </div>
      <button class="rx-btn sm rd-dl" onclick="rdProfileDownload()"><span class="ic">↓</span> Download JSON</button>
    </div>
    <div class="rd-body c360-bd">
      <div class="c360-ribbon">${ribbon}</div>
      <div class="c360-kpis">${kpis}</div>
      <div class="rd-360">
        ${idCard}${kycCard}${facCard}${bureauCard}${spendCard}${txCard}${rpCard}${dspCard}${dcCard}${grCard}${opCard}${itCard}
      </div>
    </div>`;
  // Trend lines + counters over the data already fetched for this view.
  const recent = (sp.recent||[]).slice().reverse();     // API returns newest-first
  if(recent.length > 2){
    rxSpark('rxSparkBal', recent.map(t=>Number(t.balance_after_txn_inr)||0), { zeroLine:true, stroke:'#1b56d6' });
    rxSpark('rxSparkTxn', recent.map(t=>Number(t.amount_inr)||0), { stroke:'#287c78', fillTop:'rgba(40,124,120,.22)' });
  }
  animateCounters(view);
  window.scrollTo({top:0,behavior:'smooth'});
}

function rdProfileDownload(){
  if(!RD_PROFILE) return;
  const blob = new Blob([JSON.stringify(RD_PROFILE,null,2)], {type:'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href=url; a.download=`customer-360-${RD_PROFILE.customer_id||'record'}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 1500);
  toast('Downloaded Customer 360');
}

boot();
