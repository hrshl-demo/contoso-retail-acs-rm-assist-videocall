/* ============================================================================
   insight-store.js — the payload behind the "Open in RM Cockpit" deep link.

   Every AI card that goes to Teams is also recorded here, keyed by the SAME
   eventId that server.js already mints for the Teams post
   (e.g. "VCALL-AB12CD34:turn-7:nudge"). The Teams card carries that eventId in
   its deep link, so a click resolves to the exact insight — headline, full
   text, runtime trace, source refs, consent state — with no ambiguity.

   Deliberately in-memory: a bounded ring buffer, no database, no new Azure
   resource, sub-millisecond reads. Losing it on a container restart is
   acceptable — the durable copy is the call record written to the Tool API at
   /session/finalize.

   Also exposes an SSE hub so a cockpit tab that is already open caches the
   payload BEFORE the RM clicks the Teams link, making the drill-down feel
   instant rather than a cold fetch.
   ============================================================================ */

const MAX_INSIGHTS = Math.max(50, Number(process.env.INSIGHT_STORE_MAX || 400));

/** eventId -> insight. A Map preserves insertion order, which is our ring. */
const insights = new Map();
/** Open SSE responses. */
const subscribers = new Set();

function trim() {
  while (insights.size > MAX_INSIGHTS) {
    const oldest = insights.keys().next();
    if (oldest.done) return;
    insights.delete(oldest.value);
  }
}

/**
 * Record (or update) one insight.
 * @param {object} i
 * @param {string} i.eventId      same id used for the Teams post
 * @param {string} i.kind         live_nudge | answer | synopsis | case_logged | ...
 * @param {string} i.customerId
 * @param {string} [i.sessionId]
 * @param {number|string} [i.turnId]
 * @param {string} i.headline     one-line summary shown in the drawer header
 * @param {string} [i.body]       the full insight text
 * @param {string} [i.say]        suggested talk-track
 * @param {string} [i.basis]      internal policy basis
 * @param {object} [i.runtime]    { tool, rows_scanned, latency_ms, confidence, model, ... }
 * @param {string[]} [i.sources]  source refs (tool names, case refs, SOP ids)
 * @param {object} [i.consent]    { required, status, utterance, turnId }
 * @param {string} [i.trigger]    the customer line that produced this insight
 * @param {object} [i.extra]      kind-specific payload (e.g. case fields)
 */
export function recordInsight(i = {}) {
  const eventId = String(i.eventId || '').trim();
  if (!eventId) return null;
  const runtime = i.runtime || {};
  const entry = {
    eventId,
    kind: String(i.kind || 'insight'),
    customerId: String(i.customerId || ''),
    customerName: String(i.customerName || ''),
    sessionId: String(i.sessionId || ''),
    turnId: i.turnId != null ? String(i.turnId) : '',
    headline: String(i.headline || ''),
    body: String(i.body || ''),
    say: i.say ? String(i.say) : '',
    basis: i.basis ? String(i.basis) : '',
    trigger: i.trigger ? String(i.trigger).slice(0, 400) : '',
    runtime: {
      tool: runtime.tool || null,
      rows_scanned: Number.isFinite(Number(runtime.rows_scanned)) ? Number(runtime.rows_scanned) : null,
      latency_ms: Number.isFinite(Number(runtime.latency_ms)) ? Number(runtime.latency_ms) : null,
      end_to_end_ms: Number.isFinite(Number(runtime.end_to_end_ms)) ? Number(runtime.end_to_end_ms) : null,
      confidence: Number.isFinite(Number(runtime.confidence)) ? Number(runtime.confidence) : null,
      model: runtime.model || null,
      mode: runtime.mode || null,
    },
    sources: Array.isArray(i.sources) ? i.sources.map(String).filter(Boolean).slice(0, 12) : [],
    consent: i.consent || null,
    extra: i.extra || null,
    timestamp: new Date().toISOString(),
  };
  // Re-recording an id keeps its original ring position rather than jumping the
  // queue, so a late runtime update cannot evict a newer insight.
  insights.set(eventId, entry);
  trim();
  broadcast(entry);
  return entry;
}

export function getInsight(eventId) {
  return insights.get(String(eventId || '')) || null;
}

export function listInsights({ customerId, limit } = {}) {
  const cap = Math.max(1, Math.min(200, Number(limit) || 50));
  const out = [];
  // Iterate newest-first without copying the whole map.
  const all = Array.from(insights.values());
  for (let n = all.length - 1; n >= 0 && out.length < cap; n--) {
    const e = all[n];
    if (customerId && e.customerId !== customerId) continue;
    out.push(e);
  }
  return out;
}

export function insightStoreStats() {
  return { size: insights.size, capacity: MAX_INSIGHTS, subscribers: subscribers.size };
}

/* ------------------------------ SSE hub ---------------------------------- */

function writeEvent(res, event, data) {
  try {
    res.write(`event: ${event}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
    return true;
  } catch (e) {
    return false;
  }
}

function broadcast(entry) {
  for (const sub of Array.from(subscribers)) {
    if (sub.customerId && sub.customerId !== entry.customerId) continue;
    if (!writeEvent(sub.res, 'insight', entry)) subscribers.delete(sub);
  }
}

/**
 * Attach an SSE subscriber. The CORS headers are already applied by the
 * middleware in server.js, so this only adds the stream-specific ones.
 * Replays the most recent insights immediately so a tab opened mid-call is
 * warm, then streams. A heartbeat keeps intermediaries from idling it out.
 */
export function attachInsightStream(req, res, { customerId, replay = 20, heartbeatMs = 15000 } = {}) {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders?.();

  const sub = { res, customerId: customerId || '' };
  subscribers.add(sub);

  // Tell the client how long to wait before reconnecting, then replay.
  res.write('retry: 3000\n\n');
  listInsights({ customerId, limit: replay }).reverse().forEach((e) => writeEvent(res, 'insight', e));
  writeEvent(res, 'ready', { ok: true, ...insightStoreStats() });

  const beat = setInterval(() => {
    // A comment frame is a valid no-op keepalive in the SSE wire format.
    try { res.write(': keepalive\n\n'); } catch (e) { cleanup(); }
  }, Math.max(5000, heartbeatMs));

  function cleanup() {
    clearInterval(beat);
    subscribers.delete(sub);
  }
  req.on('close', cleanup);
  req.on('error', cleanup);
  return cleanup;
}

/** Test/reset hook — not wired to any route. */
export function _resetInsightStore() {
  insights.clear();
  subscribers.clear();
}
