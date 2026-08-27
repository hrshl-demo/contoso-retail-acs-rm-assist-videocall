// Posts to a Teams Workflow (Power Automate) webhook.
// The "Post message in a chat or channel" action renders HTML, so we send light
// HTML (not markdown asterisks) and map the flow's Message to triggerBody()?['text'].

// Base URL of the RM cockpit, injected by tools/deploy-videoassist-on-vm.sh as
// https://<rmassist-host> — the SAME origin this app is served from, since Caddy serves the
// cockpit at / and this app at /video. Empty in local dev, in which case every card degrades
// to exactly its current content with no deep link.
const CRM_BASE_URL = String(process.env.CRM_BASE_URL || '').replace(/\/+$/, '');

/**
 * Deep link to the cockpit, focused on one insight.
 * The CRM boot router reads ?customer / ?focus / ?kind and opens the drawer.
 */
export function cockpitLink(customerId, eventId, kind) {
  if (!CRM_BASE_URL || CRM_BASE_URL.includes('invalid.local') || !eventId) return null;
  const q = new URLSearchParams();
  if (customerId) q.set('customer', String(customerId));
  q.set('focus', String(eventId));
  if (kind) q.set('kind', String(kind));
  return `${CRM_BASE_URL}/?${q.toString()}`;
}

// The RM-facing call to action appended to a card. Kept as a plain <a href> inside
// `text` so the EXISTING Power Automate flow renders it with zero changes.
function cockpitCta(customerId, eventId, kind, label) {
  const href = cockpitLink(customerId, eventId, kind);
  if (!href) return null;
  return `<b>🔎 <a href="${esc(href)}">${esc(label || 'Open in RM Cockpit')}</a></b> — full evidence, runtime trace and drill-down.`;
}

/**
 * Optional Adaptive Card mirror of the same content, sent as an ADDITIONAL field in
 * the POST body. The current flow reads only triggerBody()?['text'] and ignores this,
 * so existing deployments are unaffected; a flow can opt in later without any change
 * on this side. See docs/POWER_AUTOMATE.md.
 */
export function buildAdaptiveCard({ title, facts = [], body = [], linkUrl, linkLabel }) {
  const blocks = [{ type: 'TextBlock', text: String(title || 'Contoso RM Assist'), weight: 'Bolder', size: 'Medium', wrap: true }];
  body.filter(Boolean).forEach((t) => blocks.push({ type: 'TextBlock', text: String(t), wrap: true, spacing: 'Small' }));
  const facts2 = facts.filter((f) => f && f.value != null && f.value !== '');
  if (facts2.length) blocks.push({ type: 'FactSet', facts: facts2.map((f) => ({ title: String(f.title), value: String(f.value) })) });
  const card = {
    type: 'AdaptiveCard',
    $schema: 'http://adaptivecards.io/schemas/adaptive-card.json',
    version: '1.4',
    body: blocks,
  };
  if (linkUrl) card.actions = [{ type: 'Action.OpenUrl', title: String(linkLabel || 'Open in RM Cockpit'), url: String(linkUrl) }];
  return card;
}

export async function postText(webhookUrl, text, options = {}) {
  if (!webhookUrl) { console.warn('No Teams webhook configured — skipping post.'); return false; }
  const timeoutMs = Math.max(1500, Number(options.timeoutMs || 10000));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { 'Content-Type': 'application/json' };
    // Metadata travels as headers so existing Power Automate request schemas that
    // accept only {text} remain backwards compatible.
    if (options.eventId) headers['X-Contoso-Event-Id'] = String(options.eventId);
    if (options.kind) headers['X-Contoso-Message-Kind'] = String(options.kind);
    // {text} stays the contract. `card` is purely additive: flows that don't ask for
    // it never see it, so no existing Power Automate flow needs to change.
    const payload = { text };
    if (options.card) payload.card = options.card;
    if (options.deepLink) payload.deepLink = String(options.deepLink);
    const res = await fetch(webhookUrl, {
      method: 'POST', headers, signal: controller.signal,
      body: JSON.stringify(payload)
    });
    if (!res.ok) { console.error('Teams post failed', res.status, await res.text().catch(() => '')); return false; }
    return true;
  } catch (e) {
    console.error(e.name === 'AbortError' ? `Teams post timed out after ${timeoutMs} ms` : `Teams post error: ${e.message}`);
    return false;
  } finally { clearTimeout(timer); }
}

// Meeting-request card posted to the RM's Teams the moment the customer taps
// "Video call your RM" in the mobile banking portal. Carries the RM's own join link.
export function callRequestText(b, whenText) {
  const out = [];
  out.push(`<b>\uD83D\uDCF9 Video call requested \u00B7 ${esc(b.name || b.customerId || 'Customer')}</b>`);
  out.push('The customer tapped <b>Video call your RM</b> in the Contoso mobile app.');
  const lead = Math.round(Number(b.leadSeconds || 60));
  out.push(`Going live in about <b>${lead}s</b>${whenText ? ` (\u2248 ${esc(whenText)})` : ''}. Booking ${esc(b.id)}${b.customerId ? ` \u00B7 ${esc(b.customerId)}` : ''}.`);
  if (b.rmJoinLink) out.push(`<b>\u25B6 Join as RM:</b> <a href="${esc(b.rmJoinLink)}">${esc(b.rmJoinLink)}</a>`);
  if (b.calendared) out.push(b.calendarWebLink
    ? `<b>\uD83D\uDCC5 Added to your calendar:</b> <a href="${esc(b.calendarWebLink)}">open event</a>`
    : '<b>\uD83D\uDCC5 Added to your Teams calendar.</b>');
  if (b.synthetic) out.push('<i>Demo meeting link \u2014 set GRAPH_* + RM_USER_ID, SCHEDULE_WEBHOOK_URL, or RM_MEETING_URL for a real Teams meeting.</i>');
  out.push('<i>The customer sees only a \u201CJoin call\u201D button \u2014 the meeting link is never shown to them.</i>');
  return out.join('<br>');
}

export function synopsisText(s, customerName, ctx = {}) {
  const out = [];
  out.push(`<b>🟦 Customer synopsis · ${customerName}</b>`);
  if (s.headline) out.push(`<i>${s.headline}</i>`);
  if (s.summary) out.push(s.summary);
  if (s.risks && s.risks.length) out.push('<b>⚠ Risks &amp; issues</b><br>' + s.risks.map((r) => '• ' + r).join('<br>'));
  if (s.crossSell && s.crossSell.length) out.push('<b>➤ Cross-sell</b><br>' + s.crossSell.map((c) => '• ' + c).join('<br>'));
  const cta = cockpitCta(ctx.customerId, ctx.eventId, ctx.kind || 'synopsis', 'Open the full 360 in RM Cockpit');
  if (cta) out.push(cta);
  return out.join('<br><br>');
}

// Small HTML escape so the customer's quoted line can't break the card markup.
function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]); }
// The line the customer just said — gives the RM the conversational anchor for the card.
function onLine(t) { return t ? `<i>🎙 Customer said: &ldquo;${esc(String(t).slice(0, 160))}&rdquo;</i>` : null; }

export function answerText(a, heard, ctx = {}) {
  const out = [];
  const o = onLine(heard); if (o) out.push(o);
  const text = typeof a === 'string' ? a : (a?.text || '');
  out.push(`<b>📄 Answer</b>`, esc(text).replace(/\n/g, '<br>'));
  const r = typeof a === 'object' ? a?.runtime : null;
  if (r) {
    const bits = [
      'Live AI',
      r.tool ? `tool ${esc(r.tool)}` : null,
      Number.isFinite(Number(r.rows_scanned)) ? `${Number(r.rows_scanned)} records scanned` : null,
      Number.isFinite(Number(r.latency_ms)) ? `${Number(r.latency_ms)} ms` : null,
      Number.isFinite(Number(r.confidence)) ? `${Math.round(Number(r.confidence) * 100)}% intent confidence` : null,
    ].filter(Boolean);
    if (bits.length) out.push(`<i>AI runtime: ${bits.join(' &middot; ')}</i>`);
  }
  const cta = cockpitCta(ctx.customerId, ctx.eventId, ctx.kind || 'answer', 'Open this answer in RM Cockpit');
  if (cta) out.push(cta);
  return out.join('<br>');
}

export function nudgeText(n, heard, ctx = {}) {
  const tag = (n.type || 'info').toUpperCase();
  const out = [];
  const o = onLine(heard); if (o) out.push(o);
  out.push(`<b>💡 Nudge · ${esc(tag)}</b>`, esc(n.nudge));
  if (n.say) out.push(`<b>🗣 Say to customer:</b> &ldquo;${esc(n.say)}&rdquo;`);
  if (n.basis) out.push(`<i>Policy basis (internal): ${esc(n.basis)}</i>`);
  const r = n.runtime;
  if (r && Number.isFinite(Number(r.latency_ms))) {
    const total = Number.isFinite(Number(r.end_to_end_ms)) ? ` &middot; ${Number(r.end_to_end_ms)} ms to Teams request` : '';
    out.push(`<i>Live AI &middot; ${Number(r.latency_ms)} ms semantic reasoning${total}</i>`);
  }
  const cta = cockpitCta(ctx.customerId, ctx.eventId, ctx.kind || 'live_nudge', 'Open this nudge in RM Cockpit');
  if (cta) out.push(cta);
  return out.join('<br>');
}

export function caseConsentNudgeText(pending, heard) {
  const out = [];
  const o = onLine(heard); if (o) out.push(o);
  const draft = pending?.draft || {};
  out.push('<b>🛡️ Formal case registration needs customer consent</b>');
  out.push('The policy-backed resolution route has been explained, but <b>no new CRM case has been created</b>.');
  if (draft.subject) out.push(`<b>Proposed case:</b> ${esc(draft.subject)}`);
  if (draft.category) out.push(`<i>Category:</i> ${esc(draft.category)}`);
  out.push('<b>🗣 Ask the customer:</b> &ldquo;I have explained the available resolution path. Would you like me to register a formal case so the bank can track this issue and follow up with you?&rdquo;');
  out.push('<i>Control: only a clear customer yes on a later turn permits registration. Silence, anger, a new question or an ambiguous response is not consent.</i>');
  return out.join('<br>');
}

export function caseConsentClarifyText(pending, heard) {
  const out = [];
  const o = onLine(heard); if (o) out.push(o);
  out.push('<b>🛡️ Consent is not yet clear — do not register a case</b>');
  if (pending?.draft?.subject) out.push(`<b>Pending proposal:</b> ${esc(pending.draft.subject)}`);
  out.push('<b>🗣 Clarify:</b> &ldquo;Just to confirm, do I have your permission to register a formal case for this issue? Please say yes or no.&rdquo;');
  out.push('<i>The system will continue the conversation without creating a case until explicit consent is captured.</i>');
  return out.join('<br>');
}

export function transcriptReadyText(record, summary = {}) {
  const out = [];
  out.push(`<b>📝 Call transcript saved to CRM</b>`);
  out.push(`<b>${esc(record?.customer_name || record?.customer_id || 'Customer')}</b> &middot; ${esc(record?.record_id || 'call record')}`);
  const turns = Number(record?.transcript_turns ?? record?.transcript?.length ?? 0);
  const ai = Number(record?.ai_event_count ?? record?.ai_events?.length ?? 0);
  out.push(`${turns} customer transcript turn(s) &middot; ${ai} AI/CRM event(s)`);
  const headline = summary?.subject || summary?.headline;
  if (headline) out.push(`<i>${esc(headline)}</i>`);
  if (summary?.summary || summary?.call_summary) out.push(esc(summary.summary || summary.call_summary));
  out.push(`<i>Open the customer's Core CRM record → Call transcripts to download TXT or JSON. Current POC capture: customer speech plus AI answers, nudges and CRM actions.</i>`);
  return out.join('<br>');
}

// Confirmation card after explicit customer consent has been captured and the RM-controlled write succeeds.
export function caseLoggedText(k, heard, ctx = {}) {
  const out = [];
  const o = onLine(heard); if (o) out.push(o);
  const head = k.live ? '📌 Case registered after customer consent &middot;' : '📌 Case logged after customer consent &middot;';
  out.push(`<b>${head} ${esc(k.caseRef)}</b>`);
  if (k.subject) out.push(`<b>${esc(k.subject)}</b>`);
  if (k.summary) out.push(esc(k.summary));
  const meta = [k.category && `<i>Category:</i> ${esc(k.category)}`, k.priority && `<i>Priority:</i> ${esc(k.priority)}`, k.sentiment && `<i>Sentiment:</i> ${esc(k.sentiment)}`].filter(Boolean).join(' &middot; ');
  if (meta) out.push(meta);
  if (k.commitments_by_bank) out.push(`<i>Next step:</i> ${esc(k.commitments_by_bank)}${k.next_follow_up_date ? ` (by ${esc(k.next_follow_up_date)})` : ''}`);
  if (k.customerConsentConfirmed) out.push(`<i>Customer consent:</i> Confirmed on a later call turn${k.consentTurnId ? ` &middot; turn ${esc(k.consentTurnId)}` : ''}.`);
  out.push(`<i>Visible in the CRM on refresh${k.candidateId ? ` &middot; ref ${esc(k.candidateId)}` : ''}.</i>`);
  const cta = cockpitCta(ctx.customerId, ctx.eventId, ctx.kind || 'case_logged', 'Open this case in RM Cockpit');
  if (cta) out.push(cta);
  return out.join('<br>');
}

/**
 * Adaptive Card mirror of a recorded insight. Sent as the optional `card` field
 * alongside the unchanged `text`; the current flow ignores it entirely.
 */
export function insightCard(entry, href) {
  if (!entry) return null;
  const r = entry.runtime || {};
  const facts = [
    entry.customerName || entry.customerId ? { title: 'Customer', value: entry.customerName || entry.customerId } : null,
    r.tool ? { title: 'Tool', value: r.tool } : null,
    r.rows_scanned != null ? { title: 'Records scanned', value: String(r.rows_scanned) } : null,
    r.latency_ms != null ? { title: 'AI latency', value: `${r.latency_ms} ms` } : null,
    r.confidence != null ? { title: 'Confidence', value: `${Math.round(r.confidence * 100)}%` } : null,
    entry.consent?.status ? { title: 'Consent', value: String(entry.consent.status) } : null,
  ].filter(Boolean);
  return buildAdaptiveCard({
    title: entry.headline || 'Contoso RM Assist',
    body: [entry.body, entry.say ? `Say: “${entry.say}”` : null, entry.basis ? `Policy basis (internal): ${entry.basis}` : null],
    facts,
    linkUrl: href || cockpitLink(entry.customerId, entry.eventId, entry.kind),
    linkLabel: 'Open in RM Cockpit',
  });
}
