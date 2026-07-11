// Posts to a Teams Workflow (Power Automate) webhook.
// The "Post message in a chat or channel" action renders HTML, so we send light
// HTML (not markdown asterisks) and map the flow's Message to triggerBody()?['text'].

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
    const res = await fetch(webhookUrl, {
      method: 'POST', headers, signal: controller.signal,
      body: JSON.stringify({ text })
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

export function synopsisText(s, customerName) {
  const out = [];
  out.push(`<b>🟦 Customer synopsis · ${customerName}</b>`);
  if (s.headline) out.push(`<i>${s.headline}</i>`);
  if (s.summary) out.push(s.summary);
  if (s.risks && s.risks.length) out.push('<b>⚠ Risks &amp; issues</b><br>' + s.risks.map((r) => '• ' + r).join('<br>'));
  if (s.crossSell && s.crossSell.length) out.push('<b>➤ Cross-sell</b><br>' + s.crossSell.map((c) => '• ' + c).join('<br>'));
  return out.join('<br><br>');
}

// Small HTML escape so the customer's quoted line can't break the card markup.
function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]); }
// The line the customer just said — gives the RM the conversational anchor for the card.
function onLine(t) { return t ? `<i>🎙 Customer said: &ldquo;${esc(String(t).slice(0, 160))}&rdquo;</i>` : null; }

export function answerText(a, heard) {
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
  return out.join('<br>');
}

export function nudgeText(n, heard) {
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
export function caseLoggedText(k, heard) {
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
  return out.join('<br>');
}
