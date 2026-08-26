import OpenAI from 'openai';
import { DefaultAzureCredential } from '@azure/identity';
import {
  toolApiReady, getRawFacts, get360, getCrmTimeline, getNextBestAction,
  getTransactionInsights, queryTransactions, ragRetrieve,
  getCardLimitAssessment, initiateCardLimitReview,
  getLiveCallPlaybook,
} from './toolapi.js';

// Entra-based config (no API keys).
const endpoint = process.env.AZURE_AI_ENDPOINT;
const chatModel = process.env.VOICE_AI_CHAT_DEPLOYMENT || process.env.AZURE_AI_CHAT_DEPLOYMENT || 'gpt-4.1-mini';
const fastChatModel = process.env.VOICE_AI_FAST_DEPLOYMENT || chatModel;
const scope = process.env.AZURE_AI_SCOPE || 'https://ai.azure.com/.default';

/* ---------------------------------------------------------------------
   Reasoning vs non-reasoning request shape.

   Reasoning deployments (gpt-5.x / o-series) reject `temperature` and
   `max_tokens`; they take `max_completion_tokens` and bill their hidden
   reasoning tokens out of that same budget. A budget sized for a plain
   chat model (8 / 70 / 96 tokens) is consumed entirely by reasoning and
   the response comes back EMPTY — hence the separate budgets below.

   Non-reasoning deployments keep today's exact request shape, so the
   default gpt-4.1-mini behaviour is unchanged.
   --------------------------------------------------------------------- */
const REASONING_DEPLOYMENTS = new Set(
  String(process.env.AI_REASONING_DEPLOYMENTS || '')
    .split(',').map(s => s.trim()).filter(Boolean),
);
const REASONING_NAME_HINT = /(^|[^a-z0-9])(gpt-5|o1|o3|o4)/i;
const REASONING_EFFORT = process.env.VOICE_AI_REASONING_EFFORT || 'low';
// Reasoning tokens are invisible but billed against max_completion_tokens, so every
// budget needs headroom on top of the visible answer. 6x with a 512 floor keeps the
// small classifier calls viable without inflating the long generative ones.
const REASONING_BUDGET_FLOOR = Number(process.env.VOICE_AI_REASONING_MIN_TOKENS || 512);
const REASONING_BUDGET_MULTIPLIER = Number(process.env.VOICE_AI_REASONING_TOKEN_MULTIPLIER || 6);

export function isReasoningModel(model) {
  const m = String(model || '');
  return REASONING_DEPLOYMENTS.has(m) || REASONING_NAME_HINT.test(m);
}

/**
 * Build the model-specific half of a chat.completions request body.
 * @param {string} model    deployment name
 * @param {object} opts     { maxTokens, temperature, json }
 */
export function buildParams(model, { maxTokens, temperature, json } = {}) {
  const params = { model };
  if (isReasoningModel(model)) {
    params.reasoning_effort = REASONING_EFFORT;
    if (maxTokens != null) {
      params.max_completion_tokens = Math.max(
        REASONING_BUDGET_FLOOR,
        Math.ceil(maxTokens * REASONING_BUDGET_MULTIPLIER),
      );
    }
    // `temperature` is deliberately omitted: reasoning models reject it.
  } else {
    if (temperature != null) params.temperature = temperature;
    if (maxTokens != null) params.max_tokens = maxTokens;
  }
  if (json) params.response_format = { type: 'json_object' };
  return params;
}

let credential = null;
let cachedClient = null;
let cachedClientUntil = 0;
if (endpoint) {
  try { credential = new DefaultAzureCredential(); }
  catch (e) { console.error('Identity init failed:', e.message); }
}
export const aiReady = () => !!(endpoint && credential);
export const groundingReady = () => toolApiReady();

// Reuse the OpenAI client and its underlying HTTP connection during a call. The
// Entra token is refreshed well before normal expiry, avoiding per-turn identity
// and TLS setup without embedding a long-lived API key.
async function getClient() {
  const now = Date.now();
  if (cachedClient && now < cachedClientUntil) return cachedClient;
  const token = await credential.getToken(scope);
  cachedClient = new OpenAI({ baseURL: endpoint, apiKey: token.token, timeout: 15000, maxRetries: 1 });
  const expires = Number(token.expiresOnTimestamp || (now + 60 * 60 * 1000));
  cachedClientUntil = Math.max(now + 60_000, Math.min(expires - 5 * 60 * 1000, now + 45 * 60 * 1000));
  return cachedClient;
}

let nudgeWarmupPromise = null;
export async function warmNudgeModel() {
  if (!aiReady() || process.env.VOICE_AI_WARMUP === '0') return { warmed: false, reason: 'disabled_or_unavailable' };
  if (nudgeWarmupPromise) return nudgeWarmupPromise;
  const started = Date.now();
  nudgeWarmupPromise = (async () => {
    try {
      const client = await getClient();
      await client.chat.completions.create({
        ...buildParams(fastChatModel, { temperature: 0, maxTokens: 8, json: true }),
        messages: [{ role: 'system', content: 'Return only JSON.' }, { role: 'user', content: '{"ready":true}' }],
      }, { timeout: isReasoningModel(fastChatModel) ? 8000 : 2500, maxRetries: 0 });
      return { warmed: true, latency_ms: Date.now() - started, model: fastChatModel };
    } catch (error) {
      console.warn('[nudge-warmup]', error.message);
      return { warmed: false, latency_ms: Date.now() - started, reason: error.message };
    }
  })();
  return nudgeWarmupPromise;
}

/* =====================================================================
   Per-customer pre-warmed evidence cache.
   Heavy Tool API reads happen at call start, not after every utterance.
   ===================================================================== */
const cache = new Map();
const primeInflight = new Map();

const valueOr = (settled, fallback = null) => settled.status === 'fulfilled' ? settled.value : fallback;

function isOpenStatus(status) {
  return !/closed|resolved|completed|saved|won|lost|cancelled|withdrawn/i.test(String(status || ''));
}

function openCrmItems(entry, limit = 10) {
  return (entry.timeline?.events || [])
    .filter((e) => ['service', 'interaction', 'task'].includes(e.type) && isOpenStatus(e.status))
    .slice(0, limit)
    .map((e) => ({
      type: e.type,
      title: e.title,
      status: e.status,
      detail: String(e.detail || '').slice(0, 180),
      reference: Array.isArray(e.evidence) ? e.evidence[0] : null,
    }));
}

function buildRecoverySnapshot(entry) {
  const f = entry.facts || {};
  const facility = f.facility || {};
  const dispute = f.dispute || null;
  const stress = f.stress || {};
  const limit = Number(facility.sanction_limit_inr || 0);
  const outstanding = Number(facility.outstanding_inr || 0);
  const disputed = Number(dispute?.amount_inr || 0);
  const undisputed = Math.max(0, outstanding - disputed);
  const currentUtil = limit ? outstanding / limit * 100 : Number(facility.utilisation_avg_30d_pct || 0);
  const undisputedUtil = limit ? undisputed / limit * 100 : 0;
  const apr = Number(facility.interest_rate_pct || f.card?.apr_pct || 0);
  const disputedInterestMonthly = apr && disputed ? disputed * apr / 1200 : 0;
  const loans = f.loans || [];
  const monthlyLoanEmi = loans.reduce((sum, row) => sum + Number(row.emi_inr || 0), 0);
  const openItems = openCrmItems(entry, 12);
  const existingRefs = openItems
    .filter((row) => /dispute|chargeback|late.?fee|interest|collection|emi|retention|complaint/i.test(`${row.title} ${row.detail}`))
    .map((row) => ({ title: row.title, status: row.status, reference: row.reference }))
    .slice(0, 8);
  return {
    card: {
      limit_inr: limit,
      outstanding_inr: outstanding,
      current_utilisation_pct: Number(currentUtil.toFixed(1)),
      apr_pct: apr,
      undisputed_balance_inr: undisputed,
      undisputed_utilisation_pct: Number(undisputedUtil.toFixed(1)),
    },
    dispute: dispute ? {
      merchant: dispute.merchant,
      amount_inr: disputed,
      date: dispute.date,
      status: dispute.status,
      ticket_id: dispute.ticket_id,
      illustrative_interest_exposure_per_month_inr: Number(disputedInterestMonthly.toFixed(2)),
    } : null,
    repayment: {
      delayed_emis: Number(stress.delayed_emis || 0),
      recent_returns: Number(stress.cheque_returns_recent || 0),
      monthly_loan_emi_inr: monthlyLoanEmi,
      cibil_score: Number(f.credit_score?.score || 0),
    },
    existing_issue_refs: existingRefs,
    standard_options: (entry.nba?.plays || [])
      .filter((x) => String(x.eligibility || '').toLowerCase() !== 'blocked')
      .slice(0, 5)
      .map((x) => ({ product: x.product, eligibility: x.eligibility, guardrail: x.guardrail, basis: friendlySop(x.sop_basis) })),
    case_governance: {
      new_case_is_last_resort: true,
      duplicate_case_for_existing_issue_is_prohibited: true,
      required_sequence: [
        'Clarify the customer concern and identify the existing case or source record.',
        'Explain the current status and the standard policy-backed remedies.',
        'Escalate or register a new case only if the issue remains unresolved after the standard route.',
        'The RM must ask the customer for explicit permission to register the new case.',
        'The system may write the case only after a later customer turn clearly confirms permission.',
      ],
    },
  };
}

function buildFastNudgeEvidence(entry) {
  const f = entry.facts || {};
  const r = buildRecoverySnapshot(entry);
  const nba = entry.nba || {};
  return {
    customer: { id: f.customer_id || entry.c360?.customer?.customer_id, name: entry.name, segment: f.segment },
    strategy: {
      stance: nba.stance || null,
      eligible_options: r.standard_options.slice(0, 3),
      do_not_offer: (nba.do_not_offer || []).slice(0, 3).map((x) => ({ product: x.product, reason: x.reason })),
    },
    recovery: r,
    guardrails: [
      'Use exact supplied numbers and existing references.',
      'Never promise refund, blanket waiver, approval, settlement or release.',
      'Never open a duplicate case.',
      'A new case requires SOP exhaustion, an RM permission question and explicit consent on a later customer turn.',
    ],
  };
}

function buildNudgeEvidence(entry) {
  const f = entry.facts || {};
  const nba = entry.nba || {};
  const facility = f.facility || {};
  const stress = f.stress || {};
  const dispute = f.dispute || null;
  const openItems = openCrmItems(entry, 6);
  return {
    customer: { id: f.customer_id || entry.c360?.customer?.customer_id, name: entry.name, segment: f.segment, vintage_years: f.vintage_years },
    relationship_numbers: {
      card_limit_inr: facility.sanction_limit_inr,
      card_outstanding_inr: facility.outstanding_inr,
      card_utilisation_avg_30d_pct: facility.utilisation_avg_30d_pct,
      annual_income_inr: f.turnover?.annual_income_inr,
      savings_average_balance_inr: f.savings?.avg_balance_inr,
      cibil_score: f.credit_score?.score,
    },
    active_issues: {
      dispute: dispute ? { amount_inr: dispute.amount_inr, merchant: dispute.merchant, status: dispute.status, ticket_id: dispute.ticket_id } : null,
      repayment_stress: { recent_returns: stress.cheque_returns_recent, delayed_emis: stress.delayed_emis, open_service_tickets: stress.open_service_tickets },
      open_crm_items: openItems,
    },
    strategy: {
      stance: nba.stance || null,
      eligible_plays: (nba.plays || []).filter((x) => x.eligibility !== 'blocked').slice(0, 5).map((x) => ({ product: x.product, eligibility: x.eligibility, number: x.the_number, say: x.say, guardrail: x.guardrail, basis: friendlySop(x.sop_basis) })),
      do_not_offer: (nba.do_not_offer || []).slice(0, 5),
    },
    recovery_decision: buildRecoverySnapshot(entry),
    live_call_playbook: entry.liveCallPlaybook ? {
      primary_objective: entry.liveCallPlaybook.primary_objective,
      talk_tracks: (entry.liveCallPlaybook.talk_tracks || []).slice(0, 4),
      do_say: (entry.liveCallPlaybook.do_say || []).slice(0, 4),
      dont_say: (entry.liveCallPlaybook.dont_say || []).slice(0, 4),
    } : null,
    guardrails: [
      'AI recommends; a named human records every consequential decision.',
      'Never promise approval, payment release, settlement or a limit change.',
      'Use only supplied evidence and suppress blocked products.',
      'Do not create a duplicate case for an issue that is already open.',
      'Do not create a new case directly from a customer statement. Complete the SOP route, ask for explicit permission, and wait for a later clear customer confirmation.',
    ],
  };
}

export async function primeCustomer(cid) {
  if (!cid) throw new Error('customerId required');
  if (cache.has(cid)) return cache.get(cid);
  if (primeInflight.has(cid)) return primeInflight.get(cid);
  const work = (async () => {
    const [factsR, c360R, nbaR, txnR, timelineR, cardLimitR, playbookR] = await Promise.allSettled([
      getRawFacts(cid), get360(cid), getNextBestAction(cid), getTransactionInsights(cid), getCrmTimeline(cid),
      getCardLimitAssessment(cid), getLiveCallPlaybook(cid),
    ]);
    const facts = valueOr(factsR, {});
    const c360 = valueOr(c360R, {});
    const name = c360?.customer?.display_name || c360?.display_name || facts.company || cid;
    const entry = {
      facts, c360, name,
      nba: valueOr(nbaR, null),
      transactionInsights: valueOr(txnR, null),
      timeline: valueOr(timelineR, null),
      cardLimitAssessment: valueOr(cardLimitR, null),
      liveCallPlaybook: valueOr(playbookR, null),
      primedAt: Date.now(),
    };
    entry.nudgeEvidence = buildNudgeEvidence(entry);
    entry.nudgeEvidenceJson = JSON.stringify(entry.nudgeEvidence);
    // The latency-critical classifier receives a smaller, stable prefix than the
    // full coaching pack. This reduces prompt processing while preserving every
    // number and control needed for recovery/retention decisions.
    entry.fastNudgeEvidence = buildFastNudgeEvidence(entry);
    entry.fastNudgeEvidenceJson = JSON.stringify(entry.fastNudgeEvidence);
    cache.set(cid, entry);
    return entry;
  })();
  primeInflight.set(cid, work);
  try { return await work; }
  finally { primeInflight.delete(cid); }
}

export function getCustomerName(cid) { return cache.get(cid)?.name || cid; }

function money(n) {
  const x = Number(n || 0);
  if (Math.abs(x) >= 1e7) return `₹${(x / 1e7).toFixed(2)} Cr`;
  if (Math.abs(x) >= 1e5) return `₹${(x / 1e5).toFixed(2)} L`;
  return `₹${x.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}
function inr0(n) { return `₹${Math.round(Number(n || 0)).toLocaleString('en-IN')}`; }

/* ---------- compact, number-rich RETAIL evidence formatter ---------- */
function factsToText(f) {
  if (!f) return '';
  const L = [];
  L.push(`Customer ${f.company || f.customer_id} — ${f.industry || 'retail customer'}; ${f.segment || ''}; ${f.locations || ''}; with the bank ${f.vintage_years ?? '?'}y; consent ${f.consent_status || 'n/a'}.`);
  const cs = f.credit_score || {};
  if (cs.score) L.push(`CIBIL score ${cs.score} (${cs.band || ''}, as of ${cs.as_of || 'n/a'}); 6-month enquiries ${cs.enquiries_6m}, DPD ${cs.dpd_flag}${cs.dpd_count ? ` (${cs.dpd_count})` : ''}.`);
  const card = f.facility || {};
  L.push(`Credit card: limit ${card.sanction_limit_text}, outstanding ${card.outstanding_text}, available ${card.available_text}, utilisation avg30d ${card.utilisation_avg_30d_pct}% (peak ${card.utilisation_peak_30d_pct}%), APR ${card.interest_rate_pct}%.`);
  const sv = f.savings || {};
  if (sv.avg_balance_text) L.push(`Savings (${sv.product || ''}): average balance ${sv.avg_balance_text}.`);
  const t = f.turnover || {};
  if (t.annual_income_text) L.push(`Annual income ${t.annual_income_text} (prev year ${t.annual_income_prev_text}); FY inflows ${t.fy_credits_text}, outflows ${t.fy_debits_text}.`);
  (f.loans || []).forEach(ln => L.push(`${ln.type}: outstanding ${ln.outstanding_text} of ${ln.sanction_text}, EMI ${ln.emi_text}/month at ${ln.rate_pct}%, status ${ln.status}.`));
  const ins = f.insurance || {};
  if (ins.policy_type) L.push(`Insurance held: ${ins.policy_type} with ${ins.insurer}, sum assured ${ins.sum_insured_text}, annual premium ${ins.annual_premium_text}, status ${ins.status}.`);
  else L.push('Insurance: none held (a protection gap).');
  const k = f.kyc || {};
  if (k.status) L.push(`KYC ${k.status}${k.rekyc_pending ? ' (video re-KYC pending)' : ''}, next due ${k.due_date || 'n/a'}.`);
  const st = f.stress || {};
  L.push(`Conduct: EMI/auto-debit bounces recent ${st.cheque_returns_recent}/${st.cheque_returns_total}; delayed EMIs ${st.delayed_emis || 0}; open service items ${st.open_service_tickets}.`);
  const d = f.dispute;
  if (d && d.amount_text) L.push(`OPEN DISPUTE: ${d.amount_text} at ${d.merchant} on ${d.date} — ${d.status}${d.ticket_id ? ` (${d.ticket_id})` : ''}.`);
  return L.join('\n');
}

const SOP_FRIENDLY = {
  '01': 'KYC / Re-KYC policy', '02': 'Card Dispute & Chargeback policy',
  '03': 'Loan Eligibility & FOIR policy', '04': 'Unauthorised Transaction & Fraud policy',
  '05': 'Collections & Restructuring policy', '06': 'Insurance & Protection policy',
  '07': 'Fair Practices & Grievance Redressal policy', '08': 'Consent & DPDP policy',
  '09': 'Escalation & Human Handoff policy',
  '16': 'Live Call Recovery & Case Consent policy',
};
function friendlySop(id) {
  const m = String(id || '').match(/(\d{2})/);
  if (m && SOP_FRIENDLY[m[1]]) return SOP_FRIENDLY[m[1]];
  return String(id || '').replace(/^\d+[_-]?/, '').replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase()).trim() || 'bank policy';
}
const scrubSop = (s) => String(s || '').replace(/\bSOP[\s_-]*\d+[\w_-]*/gi, '').replace(/\b\d{2}_[a-z_]+/gi, '').replace(/\s{2,}/g, ' ').trim();

function txLine(t) {
  const side = String(t.dr_cr || '').toUpperCase() === 'CR' ? 'CR' : 'DR';
  const cp = t.counterparty_name || t.description || t.category_lvl1 || '—';
  const flags = [t.is_return === 'Y' ? 'BOUNCED' : '', String(t.anomaly_tag || '').toLowerCase() === 'unauthorized' ? 'DISPUTED' : ''].filter(Boolean);
  return `• ${t.txn_date || '-'} ${side} ${money(t.amount_inr)} — ${cp}${flags.length ? ` (${flags.join(', ')})` : ''}`;
}
function txList(title, rows) {
  if (!rows?.length) return `${title}: no matching transactions were found.`;
  return `${title}:\n${rows.map(txLine).join('\n')}`;
}
function openCasesToText(tl, limit = 6) {
  const ev = (tl?.events || []);
  const isOpen = (e) => !/closed|resolved|completed|saved|won|lost/i.test(String(e.status || ''));
  const open = ev.filter(e => ['service', 'interaction', 'task'].includes(e.type) && isOpen(e));
  const items = (open.length ? open : ev.filter(e => ['service', 'interaction', 'task'].includes(e.type))).slice(0, limit);
  if (!items.length) return 'No open cases, complaints or service requests are on record.';
  return items.map(e => `• [${e.type}] ${e.title || ''} — ${e.status || 'n/a'}${e.detail ? ` — ${String(e.detail).slice(0, 160)}` : ''}`).join('\n');
}

function existingReferenceForWorkflow(entry, workflow) {
  if (!workflow) return '';
  const r = buildRecoverySnapshot(entry);
  const category = String(workflow.draft?.category || '').toLowerCase();
  const text = `${workflow.issue_key || ''} ${workflow.draft?.subject || ''} ${workflow.draft?.summary || ''} ${category}`.toLowerCase();
  const existing = r.existing_issue_refs || [];

  // A formal grievance about poor handling is distinct from the operational
  // dispute/fee-review record. It may proceed through SOP + explicit consent,
  // unless a grievance/complaint record already exists.
  if (/complaint|escalation/.test(category)) {
    const grievance = existing.find((x) => /complaint|grievance|retention|escalation/i.test(`${x.title || ''}`));
    return grievance?.reference || grievance?.title || '';
  }

  if (r.dispute?.ticket_id && /dispute|chargeback|refund|transaction|merchant/.test(text)) return r.dispute.ticket_id;
  const preferred = existing.find((x) => {
    const row = `${x.title || ''}`.toLowerCase();
    if (/interest|fee|waiver/.test(text) && /interest|fee|waiver/.test(row)) return true;
    if (/emi|restructur|hardship|collection/.test(text) && /emi|restructur|hardship|collection/.test(row)) return true;
    if (/complaint|grievance|retention/.test(text) && /complaint|grievance|retention/.test(row)) return true;
    return false;
  });
  return preferred?.reference || preferred?.title || '';
}

function disputeStatusAnswer(entry) {
  const r = buildRecoverySnapshot(entry);
  const d = r.dispute;
  if (!d) return 'No open disputed transaction is present in the current source records.';
  const feeLine = d.illustrative_interest_exposure_per_month_inr
    ? ` At the recorded ${r.card.apr_pct.toFixed(1)}% APR, the disputed leg represents an illustrative ${money(d.illustrative_interest_exposure_per_month_inr)} per month of interest exposure while unresolved; an authorised card-service owner must decide whether any such interest or fee is reversible.`
    : '';
  return `The ${money(d.amount_inr)} GlobalMart Online transaction dated ${d.date || 'the recorded date'} is already under an open dispute${d.ticket_id ? `, reference ${d.ticket_id}` : ''}. The current data does not contain a final chargeback/refund decision or a confirmed refund date, so the RM must not invent one. The next step is to obtain the current chargeback or provisional-credit status and confirm whether any interest or fees were applied to the disputed amount.${feeLine} Do not open a duplicate case for the same transaction.`;
}

function interestReliefAnswer(entry) {
  const r = buildRecoverySnapshot(entry);
  const d = r.dispute;
  const options = r.standard_options.map((x) => x.product).filter(Boolean).slice(0, 3);
  const existing = r.existing_issue_refs.map((x) => x.reference || x.title).filter(Boolean).slice(0, 4);
  const split = d
    ? `The recorded card outstanding is ${money(r.card.outstanding_inr)} against a ${money(r.card.limit_inr)} limit at ${r.card.apr_pct.toFixed(1)}% APR. ${money(d.amount_inr)} is already disputed${d.ticket_id ? ` under ${d.ticket_id}` : ''}; isolating that amount leaves an indicative verified balance of ${money(r.card.undisputed_balance_inr)}, or ${r.card.undisputed_utilisation_pct.toFixed(1)}% of the limit.`
    : `The recorded card outstanding is ${money(r.card.outstanding_inr)} against a ${money(r.card.limit_inr)} limit at ${r.card.apr_pct.toFixed(1)}% APR.`;
  return `A blanket removal of all interest cannot be confirmed on the call. ${split} The policy-safe sequence is: first review interest and fees attributable to any disputed or incorrectly applied amount; second explain the verified undisputed balance and contractual charges; third assess hardship or restructuring options such as ${options.join(', ') || 'EMI conversion or restructuring'}, subject to eligibility and human approval. The record also shows ${r.repayment.delayed_emis} delayed EMI(s) and ${r.repayment.recent_returns} recent return(s), so affordability must be discussed without coercion. ${existing.length ? `Relevant open records already exist (${existing.join(', ')}), so no duplicate case should be created.` : 'If the issue remains unresolved after these steps, the RM may ask the customer for permission to register a formal case.'}`;
}

/* =====================================================================
   Synopsis — posted to Teams at call connect. This also warms the model,
   evidence cache and strategy so the first customer question is faster.
   ===================================================================== */
export async function generateSynopsis(cid) {
  if (!aiReady()) return null;
  let primed;
  try { primed = await primeCustomer(cid); }
  catch (e) { console.warn('[synopsis] grounding unavailable:', e.message); return null; }
  const { facts, name, nba } = primed;
  const strat = nba
    ? `\nStrategy stance: ${nba.stance}. Eligible plays: ${(nba.plays || []).filter(p => p.eligibility !== 'blocked').map(p => `${p.product}${p.the_number ? ' (' + p.the_number + ')' : ''}`).join('; ') || 'none'}. Do NOT offer: ${(nba.do_not_offer || []).map(d => `${d.product} (${d.reason})`).join('; ') || 'none'}.`
    : '';
  const client = await getClient();
  const sys = `You are a relationship-manager co-pilot for Contoso Bank's RETAIL segment. Produce a crisp pre-call synopsis for ${name}. Use ONLY supplied evidence. Return STRICT JSON {"headline":"...","summary":"...","risks":["..."],"crossSell":["..."]}. summary under 45 words; max 3 risks and 3 eligible actions. Lead with open disputes or repayment stress. Never recommend a blocked product.`;
  const r = await client.chat.completions.create({
    ...buildParams(chatModel, { temperature: 0.2, maxTokens: 300, json: true }),
    messages: [{ role: 'system', content: sys }, { role: 'user', content: `RETAIL evidence:\n${factsToText(facts)}${strat}` }],
  });
  return JSON.parse(r.choices[0].message.content);
}

function normaliseQuestionPlan(raw = {}) {
  const allowedTools = new Set(['transactions', 'cases', 'card', 'loans', 'credit_score', 'dispute', 'interest_relief', 'kyc', 'savings', 'insurance', 'income', 'repayments', 'overview', 'product_eligibility', 'card_limit', 'cashflow', 'policy', 'verify_caller', 'fees', 'restructuring', 'consolidation', 'prepayment', 'sma', 'retention', 'guarantor', 'none']);
  const allowedOps = new Set(['recent', 'largest', 'smallest', 'aggregate', 'summary', 'explain', 'status', 'list', 'search', 'category_breakdown', 'merchant_breakdown', 'eligibility', 'request', 'compare', 'interest', 'impact', 'waiver', 'hardship']);
  const tool = allowedTools.has(raw.tool) ? raw.tool : 'none';
  const operation = allowedOps.has(raw.operation) ? raw.operation : 'summary';
  const direction = ['all', 'debit', 'credit'].includes(raw.direction) ? raw.direction : 'all';
  return {
    is_question: !!raw.is_question,
    confidence: Math.max(0, Math.min(1, Number(raw.confidence || 0))),
    tool, operation, direction,
    limit: Math.max(1, Math.min(10, Number(raw.limit || 5))),
    period_days: raw.period_days ? Math.max(1, Math.min(366, Number(raw.period_days))) : null,
    merchant: raw.merchant ? String(raw.merchant).slice(0, 80) : null,
    category: raw.category ? String(raw.category).slice(0, 80) : null,
    requested_limit_inr: raw.requested_limit_inr ? Math.max(0, Number(raw.requested_limit_inr)) : null,
    customer_authorises_action: !!raw.customer_authorises_action,
    reason: String(raw.reason || '').slice(0, 200),
  };
}

function txCached(entry, q) {
  const i = entry.transactionInsights;
  if (!i) return null;
  if (q.operation === 'recent' && !q.period_days && !q.merchant && !q.category) return i.recent;
  if (q.operation === 'largest' && !q.period_days && !q.merchant && !q.category) {
    if (q.direction === 'debit') return i.largest_debits;
    if (q.direction === 'credit') return i.largest_credits;
    return i.largest_all;
  }
  if (q.operation === 'aggregate' && q.period_days === 30 && !q.merchant && !q.category) return i.last_30_days;
  return null;
}

export async function executeQuestionPlan(cid, entry, rawPlan) {
  const q = normaliseQuestionPlan(rawPlan);
  const f = entry.facts || {};
  const nm = entry.name || cid;
  const sourceRefs = [];
  if (!q.is_question || q.tool === 'none') return { text: null, tool: 'none', sourceRefs, rowsScanned: 0, plan: q };

  if (q.tool === 'transactions') {
    const result = txCached(entry, q) || await queryTransactions(cid, q);
    sourceRefs.push('Tool API transaction ledger');
    const rows = (result?.transactions || []).slice(0, q.limit);
    let title;
    if (q.operation === 'largest') title = `Top ${rows.length} highest ${q.direction === 'debit' ? 'debit' : q.direction === 'credit' ? 'credit' : ''} transactions for ${nm}`.replace(/\s+/g, ' ');
    else if (q.operation === 'smallest') title = `Lowest ${rows.length} matching transactions for ${nm}`;
    else if (q.operation === 'merchant_breakdown') {
      const groups = (result?.top_counterparties || []).slice(0, q.limit);
      return { text: groups.length ? `Top counterparties for ${nm}:\n${groups.map(x => `• ${x.name}: ${money(x.amount_inr)} across ${x.count} transaction(s)`).join('\n')}` : 'No matching counterparties were found.', tool: 'transactions.merchant_breakdown', sourceRefs, rowsScanned: result?.rows_scanned || 0, plan: q };
    }
    else if (q.operation === 'category_breakdown') {
      const groups = (result?.top_categories || []).slice(0, q.limit);
      return { text: groups.length ? `Top transaction categories for ${nm}:\n${groups.map(x => `• ${x.name}: ${money(x.amount_inr)} across ${x.count} transaction(s)`).join('\n')}` : 'No matching categories were found.', tool: 'transactions.category_breakdown', sourceRefs, rowsScanned: result?.rows_scanned || 0, plan: q };
    }
    else if (q.operation === 'aggregate' || q.operation === 'summary') {
      const m = result?.metrics || {};
      const period = q.period_days ? ` over the last ${q.period_days} days in the available ledger` : '';
      return {
        text: `${nm} has ${result?.rows_matched || 0} matching transactions${period}: total ${money(m.total_amount_inr)}, average ${money(m.average_amount_inr)}, debits ${money(m.debit_total_inr)} across ${m.debit_count || 0} entries, and credits ${money(m.credit_total_inr)} across ${m.credit_count || 0} entries.`,
        tool: `transactions.${q.operation}`, sourceRefs, rowsScanned: result?.rows_scanned || 0, plan: q,
      };
    } else title = `${q.limit} most recent ${q.direction === 'debit' ? 'debit' : q.direction === 'credit' ? 'credit' : ''} transactions for ${nm}`.replace(/\s+/g, ' ');
    return { text: txList(title, rows), tool: `transactions.${q.operation}`, sourceRefs, rowsScanned: result?.rows_scanned || 0, plan: q };
  }

  if (q.tool === 'cases') {
    sourceRefs.push('CRM timeline');
    return { text: `Open cases / service items for ${nm}:\n${openCasesToText(entry.timeline, q.limit)}`, tool: 'cases.list', sourceRefs, rowsScanned: entry.timeline?.events?.length || 0, plan: q };
  }
  if (q.tool === 'card') {
    const c = f.facility || {};
    const ref = f.reference || {};
    const stmt = ref.card_statement || {};
    const fin = ref.finance_charge || {};
    const fees = ref.fees || {};
    const limit = Number(c.sanction_limit_inr || 0), out = Number(c.outstanding_inr || 0);
    const util = limit ? out / limit * 100 : Number(c.utilisation_peak_30d_pct || 0);
    const excess = Math.max(0, out - limit);
    const apr = Number(fin.apr_pct || c.interest_rate_pct || 0);
    const d = f.dispute || {};

    // Q4 — "when is my next card payment due?"
    if (q.operation === 'status' && stmt.payment_due_date) {
      const pl = (f.loans || []).find(x => /personal/i.test(x.type || '')) || {};
      const emiClause = pl.next_due_date ? ` Separately, your next ${pl.type} EMI of ${pl.emi_text || money(pl.emi_inr)} is due on ${pl.next_due_date}.` : '';
      return {
        text: `Your latest credit-card statement is dated ${stmt.statement_date}, with a total amount due of ${inr0(stmt.total_due_inr)} and a minimum due of ${inr0(stmt.minimum_due_inr)} payable by ${stmt.payment_due_date}. Because the balance is currently over the limit, the full over-limit amount of ${inr0(stmt.over_limit_inr)} is included in that minimum. Paying at least the minimum by ${stmt.payment_due_date} avoids the late-payment fee; clearing the full balance avoids further interest.${emiClause}`,
        tool: 'card.payment_due', sourceRefs: ['Credit-card statement', 'Card fees & charges policy'], rowsScanned: 1, plan: q,
      };
    }
    // Q5 — "how much interest am I paying a month?"
    if (q.operation === 'interest' && fin.monthly_interest_inr != null) {
      return {
        text: `At ${apr.toFixed(1)}% per annum — about ${Number(fin.monthly_rate_pct).toFixed(2)}% a month — the finance charge on the current ${money(fin.on_balance_inr)} balance is roughly ${inr0(fin.monthly_interest_inr)} per month, or about ${inr0(fin.annual_interest_inr)} a year, for as long as it keeps revolving. Paying only the minimum keeps almost the whole balance accruing interest, so paying more than the minimum — or moving this balance to a lower-rate loan — is what actually reduces it.`,
        tool: 'card.finance_charge', sourceRefs: ['Credit-card facility', 'Finance-charge computation'], rowsScanned: 1, plan: q,
      };
    }
    sourceRefs.push('Credit facility', '30-day utilisation');
    // Q6/Q7 — "why is it 42%, how am I over the limit, will it get blocked?"
    if (q.operation === 'explain') {
      const parts = [];
      parts.push(`The card is a revolving, unsecured credit line, so ${apr.toFixed(1)}% p.a. is the standard revolving rate for that product — it is priced high because it is short-term borrowing with no security, not a penalty aimed at you.`);
      if (excess > 0) parts.push(`Your balance is ${money(out)} against a ${money(limit)} limit, which is ${inr0(excess)} over the limit; that can cause new purchases to be declined and adds an over-limit fee of about ${inr0((fees.card_over_limit || {}).total_inr)}, and if the minimum is missed a late fee of about ${inr0((fees.card_late_payment || {}).total_inr)}.`);
      parts.push(`The card is not simply "blocked", but staying over-limit and revolving at ${apr.toFixed(1)}% is the expensive part — so the better move is to bring it back within the limit or shift it to a lower-rate loan, which we can look at together.`);
      if (limit && d.amount_inr) parts.push(`If the open disputed ${money(d.amount_inr)} is set aside, the verified balance is about ${money(out - Number(d.amount_inr))}.`);
      return { text: parts.join(' '), tool: 'card.explain', sourceRefs, rowsScanned: 1, plan: q };
    }
    // default — utilisation summary
    const adj = limit && d.amount_inr ? Math.max(0, out - Number(d.amount_inr)) / limit * 100 : null;
    let text = `Your credit-card limit is ${money(limit)}, outstanding is ${money(out)}, available amount is ${money(c.available_inr)}, and current calculated utilisation is ${util.toFixed(1)}%.`;
    if (adj != null) text += ` If the open disputed amount of ${money(d.amount_inr)} is isolated for analysis, the remaining balance is ${money(out - Number(d.amount_inr))}, or approximately ${adj.toFixed(1)}% of the limit.`;
    return { text, tool: `card.${q.operation}`, sourceRefs, rowsScanned: 1, plan: q };
  }
  if (q.tool === 'dispute') {
    const d = f.dispute;
    sourceRefs.push('Transaction ledger', 'CRM dispute case', 'Card Dispute & Chargeback policy', 'Fair Practices & Grievance policy');
    const text = d ? disputeStatusAnswer(entry) : `No open disputed transaction is present in ${nm}'s current records.`;
    return { text, tool: 'dispute.status', sourceRefs, rowsScanned: d ? 1 : 0, plan: q };
  }
  if (q.tool === 'interest_relief') {
    sourceRefs.push('Credit-card facility', 'Open dispute ledger', 'Repayment history', 'Collections & Restructuring policy', 'Fair Practices & Grievance policy');
    return { text: interestReliefAnswer(entry), tool: 'interest_relief.explain', sourceRefs, rowsScanned: 1 + Number(f.stress?.open_service_tickets || 0), plan: q };
  }
  if (q.tool === 'loans') {
    const loans = f.loans || [];
    sourceRefs.push('Loan facilities', 'Repayment schedule');
    if (!loans.length) return { text: `No active home or personal loan is present in ${nm}'s current records.`, tool: 'loans.summary', sourceRefs, rowsScanned: 0, plan: q };
    const card = f.facility || {};
    const cardOut = Number(card.outstanding_inr || 0);
    const totalOut = loans.reduce((a, x) => a + Number(x.outstanding_inr || 0), 0);
    const totalEmi = loans.reduce((a, x) => a + Number(x.emi_inr || 0), 0);
    // "When is my next EMI due?" — answer the date and amount, not the delayed-EMI count.
    if (q.operation === 'status') {
      const dueParts = loans.map(x => x.next_due_date
        ? `your next ${x.type} EMI of ${x.emi_text} is due on ${x.next_due_date}`
        : `your ${x.type} EMI is ${x.emi_text} per ${String(x.frequency || 'month').toLowerCase().includes('month') ? 'month' : 'period'} (the exact next date is not in the current record)`);
      const stmt = f.reference?.card_statement || {};
      const cardClause = stmt.payment_due_date ? ` Your credit-card minimum of ${inr0(stmt.minimum_due_inr)} is also due on ${stmt.payment_due_date}.` : '';
      return { text: `${dueParts.join('; ')}.${cardClause}`, tool: 'loans.next_due', sourceRefs, rowsScanned: loans.length, plan: q };
    }
    const detail = loans.map(x => `${x.type}: ${x.outstanding_text} outstanding of ${x.sanction_text}, ${x.emi_text} monthly EMI at ${x.rate_pct}%${x.next_due_date ? ` (next due ${x.next_due_date})` : ''} — ${x.status}`).join('; ');
    const combined = cardOut ? ` Adding the credit-card outstanding of ${money(cardOut)}, total outstanding across the card and loans is ${money(totalOut + cardOut)}.` : '';
    return { text: `${detail}. Total loan outstanding is ${money(totalOut)} and combined recorded EMI is ${money(totalEmi)} per month.${combined}`, tool: 'loans.summary', sourceRefs, rowsScanned: loans.length, plan: q };
  }
  if (q.tool === 'credit_score') {
    const c = f.credit_score || {};
    const st = f.stress || {};
    const ref = f.reference || {};
    const sma = ref.sma || {};
    sourceRefs.push('Credit bureau');
    if (q.operation === 'impact') {
      const bounces = Number(st.cheque_returns_total || st.cheque_returns_recent || 0);
      return {
        text: `Honest answer: your CIBIL is ${c.score || 'on record'}${c.band ? ` — ${c.band}` : ''}. The ${bounces || 'recent'} bounced EMI(s) are already reported to the bureau and are part of why the score sits where it does — so the missed payments affect it whether or not we restructure. A formal restructuring can be reported as "restructured", which lenders do see, but it is generally viewed far better than continued missed payments or a slide to default${sma.days_to_npa ? `, and you still have about ${sma.days_to_npa} days before any default classification` : ''}. The most powerful thing for your score is returning to on-time payments, which a right-sized EMI is designed to help you do. I will not pretend it has zero impact, but managed restructuring is the lesser harm.`,
        tool: 'credit_score.impact', sourceRefs, rowsScanned: 1, plan: q,
      };
    }
    return { text: c.score ? `The recorded CIBIL score is ${c.score} (${c.band || 'band not stated'}) as of ${c.as_of || 'the latest bureau pull'}, with ${c.enquiries_6m || 0} enquiries in six months and DPD status ${c.dpd_flag || 'not stated'}${c.dpd_count ? ` across ${c.dpd_count} instances` : ''}.` : 'No current bureau score is present in the customer record.', tool: 'credit_score.summary', sourceRefs, rowsScanned: c.score ? 1 : 0, plan: q };
  }
  if (q.tool === 'kyc') {
    const k = f.kyc || {};
    sourceRefs.push('KYC record', 'Document status', 'Video re-KYC (V-CIP) procedure');
    const dueClause = k.due_date ? ` (next recorded due date ${k.due_date})` : '';
    const statusLine = `KYC status is ${k.status || 'not stated'}${k.rekyc_pending ? ', with a video re-KYC pending' : ''}${dueClause}.`;
    const docs = (k.pending_documents || []).filter(Boolean);
    const docLine = docs.length ? ` To complete it you need: ${docs.join(', ')}.` : '';
    // "Can you do my KYC / re-KYC on this call, right now?" is an ACTION request, not a
    // status query — answer what actually happens on the call instead of restating status.
    if (q.operation === 'request' || q.customer_authorises_action) {
      return {
        text: `Yes. ${statusLine} We can complete the video re-KYC (V-CIP) on this same call: I verify your identity live on video against your record, capture the required proof of identity and address, and submit it for the bank's maker-checker approval. Once approved that clears the pending re-KYC and unblocks any credit, limit or restructuring review. For your safety I will only confirm identity here — I will never ask for your full card number, PIN, OTP or password.`,
        tool: 'kyc.rekyc_on_call', sourceRefs, rowsScanned: 1, plan: q,
      };
    }
    return {
      text: `${statusLine}${docLine}${k.due_date ? ` The deadline is ${k.due_date}.` : ''} The simplest route is a video re-KYC (V-CIP) which we can do live on this call — I verify your identity on video and capture the proof of identity and address, then it goes for maker-checker approval.${k.blocking ? ' Until it is completed it blocks any new credit, limit increase or restructuring.' : ''} I can also log a follow-up task now with a secure upload link and a reminder before ${k.due_date || 'the due date'}.`,
      tool: 'kyc.status', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'savings') {
    const s = f.savings || {};
    sourceRefs.push('Savings account');
    return { text: `The recorded average savings-account balance is ${s.avg_balance_text || money(s.avg_balance_inr)}${s.product ? ` in ${s.product}` : ''}.`, tool: 'savings.summary', sourceRefs, rowsScanned: 1, plan: q };
  }
  if (q.tool === 'insurance') {
    const i = f.insurance || {};
    sourceRefs.push('Insurance holding');
    return { text: i.policy_type ? `Insurance on record: ${i.policy_type} with ${i.insurer || 'the insurer'}, sum assured ${i.sum_insured_text || '-'}, annual premium ${i.annual_premium_text || '-'}, status ${i.status || '-'}.` : 'No insurance policy is recorded for this customer.', tool: 'insurance.summary', sourceRefs, rowsScanned: i.policy_type ? 1 : 0, plan: q };
  }

  if (q.tool === 'cashflow') {
    const t = f.turnover || {};
    const inflows = Number(t.fy_credits_inr || 0), outflows = Number(t.fy_debits_inr || 0);
    const gap = outflows - inflows;
    const ratio = inflows ? outflows / inflows * 100 : 0;
    const monthlyGap = gap / 12;
    const tx = await queryTransactions(cid, { operation:'aggregate', direction:'debit', period_days:q.period_days || 365, limit:q.limit || 5 });
    const cats = (tx?.top_categories || []).slice(0, 5);
    sourceRefs.push('Account inflow/outflow ledger', 'Transaction category aggregation');
    const direction = gap > 0 ? 'exceed' : 'are below';
    const caveat = 'Bank-account debits are not the same as discretionary spending: they may include investments, transfers, taxes and loan repayments.';
    const categories = cats.length ? ` Largest debit categories in the available ledger are ${cats.map(x=>`${x.name} ${money(x.amount_inr)}`).join(', ')}.` : '';
    return {
      text:`Recorded annual account outflows of ${money(outflows)} ${direction} inflows of ${money(inflows)} by ${money(Math.abs(gap))}. That is an outflow-to-inflow ratio of ${ratio.toFixed(1)}% and an average monthly ${gap > 0 ? 'deficit' : 'surplus'} of ${money(Math.abs(monthlyGap))}. ${caveat}${categories}`,
      tool:'cashflow.compare', sourceRefs, rowsScanned:tx?.rows_scanned || 0, plan:q,
      nudgeOverride:{type:gap > 0 ? 'stress' : 'growth', nudge:`Do not label the customer as overspending from gross debits alone. The account shows ${money(inflows)} inflows versus ${money(outflows)} outflows (${ratio.toFixed(1)}%), but the debit mix must be separated into consumption, investments, transfers, tax and debt service before advice is given.`, say:`Your account shows outflows ${gap > 0 ? 'slightly above' : 'below'} inflows, but that does not automatically mean you are spending too much. Let us separate actual household spending from investments, transfers and repayments before drawing a conclusion.`, basis:'Customer cash-flow comparison and transaction-category evidence'}
    };
  }

  if (q.tool === 'income') {
    const t = f.turnover || {};
    sourceRefs.push('Income profile', 'Account inflows/outflows');
    return { text: `Recorded annual income is ${t.annual_income_text || '-'} versus ${t.annual_income_prev_text || '-'} previously. FY account inflows are ${t.fy_credits_text || '-'} and outflows are ${t.fy_debits_text || '-'}.`, tool: 'income.summary', sourceRefs, rowsScanned: 1, plan: q };
  }
  if (q.tool === 'repayments') {
    const st = f.stress || {};
    const c = f.facility || {};
    const out = Number(c.outstanding_inr || 0), limit = Number(c.sanction_limit_inr || 0);
    const apr = Number(c.interest_rate_pct || 0);
    sourceRefs.push('Repayment history', 'Auto-debit returns', 'Credit-card facility', 'Collections & Restructuring policy');
    const stressLine = `The record shows ${st.delayed_emis || 0} delayed EMI(s) and ${st.cheque_returns_recent || 0} recent auto-debit or cheque return(s) out of ${st.cheque_returns_total || 0} recorded returns.`;
    const posLine = out ? ` The card outstanding is ${money(out)}${limit ? ` against a ${money(limit)} limit` : ''}${apr ? ` at ${apr.toFixed(1)}% APR` : ''}.` : '';
    const optLine = ` Restructuring or EMI conversion can be assessed through an affordability review — it is subject to eligibility and the bank's approval and cannot be confirmed on this call, but I can start that review for you.`;
    return { text: `${stressLine}${posLine}${optLine}`, tool: 'repayments.summary', sourceRefs, rowsScanned: Number(st.cheque_returns_total || 0), plan: q };
  }
  if (q.tool === 'card_limit') {
    const a = entry.cardLimitAssessment || await getCardLimitAssessment(cid);
    entry.cardLimitAssessment = a;
    sourceRefs.push('Card-limit decision engine', 'Credit bureau', 'Income trend', 'Repayment history', 'PR-002');
    const band = a.recommended_review_band || {};
    if (q.operation === 'request' && q.customer_authorises_action) {
      const result = await initiateCardLimitReview(cid, q.requested_limit_inr || band.upper_inr || null);
      if (result.status === 'ALREADY_OPEN') {
        return { text: `A credit-card limit review is already open under request ${result.request_id}. Your current limit remains ${money(a.current_limit_inr)} and no approval or limit change has occurred. The card team must complete the human assessment.`, tool:'card_limit.request_existing', sourceRefs, rowsScanned:(a.tests||[]).length, plan:q, actionTaken:false, actionResult:result, nudgeOverride:{type:'service', nudge:`Do not create a duplicate request. Review ${result.request_id} is already open; current limit ${money(a.current_limit_inr)} remains unchanged pending human underwriting.`, say:'Your review request is already in progress. I will track the existing request rather than create a duplicate.', basis:'PR-002 approval-gated credit review and duplicate-control'} };
      }
      if (result.status === 'BLOCKED') {
        return { text: `I cannot initiate a limit review yet. The current pre-screen is blocked by: ${(result.assessment?.blockers || []).join('; ') || result.reason}. The existing limit remains ${money(a.current_limit_inr)}.`, tool:'card_limit.request_blocked', sourceRefs, rowsScanned:(a.tests||[]).length, plan:q, actionTaken:false, nudgeOverride:{type:'compliance', nudge:'Do not promise or initiate the limit increase while policy blockers remain.', say:'I can explain the blockers and what would need to change before a review can be considered.', basis:'PR-002 credit-limit eligibility and human underwriting'} };
      }
      const target = result.target_limit_inr || band.upper_inr;
      return { text: `I have initiated a credit-card limit review from ${money(a.current_limit_inr)} to a review ceiling of ${money(target)}. Request ${result.request_id || 'created'} is now in the CRM. Your current limit has not changed; human card/credit underwriting must assess and communicate the final decision.`, tool:'card_limit.request', sourceRefs, rowsScanned:(a.tests||[]).length, plan:q, actionTaken:true, actionResult:result, nudgeOverride:{type:'service', nudge:`Customer consent is captured and review ${result.request_id || ''} has been initiated. Current limit ${money(a.current_limit_inr)}; policy-tested review band ${money(band.lower_inr)}–${money(band.upper_inr)}. No approval or limit change has occurred.`, say:'I have started the formal review. Your existing limit stays unchanged until our card team completes the assessment and confirms a decision.', basis:'PR-002 pre-screen + approval-gated credit review'} };
    }
    const failed = (a.tests || []).filter(x => !x.passed);
    const text = a.eligible_for_review
      ? `You are eligible to initiate a card-limit review, not an automatic increase. Current limit ${money(a.current_limit_inr)}, outstanding ${money(a.current_outstanding_inr)}, utilisation ${Number(a.current_utilisation_pct).toFixed(1)}%, CIBIL ${a.cibil_score}, and income growth ${Number(a.income_growth_pct).toFixed(1)}%. The policy-tested review band is ${money(band.lower_inr)} to ${money(band.upper_inr)}. Final approval and amount require human underwriting.`
      : `A card-limit review should not be initiated yet. Current limit ${money(a.current_limit_inr)}, utilisation ${Number(a.current_utilisation_pct).toFixed(1)}%, CIBIL ${a.cibil_score}. Blockers: ${failed.map(x=>`${x.test} (${x.actual}; required ${x.required})`).join('; ')}.`;
    return { text, tool:'card_limit.eligibility', sourceRefs, rowsScanned:(a.tests||[]).length, plan:q, actionTaken:false, nudgeOverride:{type:a.eligible_for_review?'growth':'compliance', nudge:a.eligible_for_review?`The customer passes the limit-review pre-screen. Do not position this as approval; explain the ${money(band.lower_inr)}–${money(band.upper_inr)} review band and capture explicit consent before initiating.`:`Do not offer a limit increase. Resolve the failed policy tests first: ${failed.map(x=>x.test).join(', ')}.`, say:a.eligible_for_review?'Your profile supports starting a formal review, but the card team must still assess and approve the final limit.':'The bank cannot start a limit-increase review until the identified blockers are resolved.', basis:'PR-002 limit-increase eligibility and human underwriting'} };
  }

  if (q.tool === 'product_eligibility') {
    const nba = entry.nba || {};
    const plays = (nba.plays || []).filter(p => p.eligibility !== 'blocked');
    const blocked = nba.do_not_offer || [];
    sourceRefs.push('Eligibility engine', 'Next-best-action strategy');
    const text = `Current relationship stance: ${nba.stance || 'not available'}. Eligible or conditional plays: ${plays.map(p => `${p.product}${p.the_number ? ` (${p.the_number})` : ''}`).join('; ') || 'none'}. Suppressed offers: ${blocked.map(b => `${b.product} — ${b.reason}`).join('; ') || 'none'}.`;
    return { text, tool: 'product_eligibility.summary', sourceRefs, rowsScanned: plays.length + blocked.length, plan: q };
  }
  if (q.tool === 'policy') {
    const hits = await ragRetrieve(rawPlan.policy_query || rawPlan.reason || 'relevant retail banking policy', 3).catch(() => null);
    const rows = hits?.results || hits?.chunks || [];
    sourceRefs.push('Azure AI Search policy index');
    const text = rows.length ? `Relevant policy guidance:\n${rows.slice(0, 3).map(x => `• ${x.title || x.source || 'Policy'}: ${x.snippet || x.content || ''}`).join('\n')}` : 'No matching policy passage was retrieved.';
    return { text, tool: 'policy.retrieve', sourceRefs, rowsScanned: rows.length, plan: q };
  }
  if (q.tool === 'verify_caller') {
    const k = f.kyc || {};
    sourceRefs.push('Customer-protection & anti-fraud policy', 'Caller-verification procedure');
    return {
      text: `That is exactly the right question to ask, and please always stay cautious. Here is how you can be sure this is genuinely Contoso Bank: I will never ask you for your full card number, CVV, PIN, OTP or password — anyone who does is not from the bank. You can also hang up and call the number printed on the back of your card or on your statement and ask for me by name. I can quote details we already hold — for example that your KYC is due${k.due_date ? ` by ${k.due_date}` : ''} — but I will not ask you to reveal secret credentials to "prove" yourself. Anything we agree today I will also send you in writing through the app. If you are comfortable, we can verify your identity properly with a quick video re-KYC.`,
      tool: 'verify_caller.explain', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'fees') {
    const ref = f.reference || {};
    const fees = ref.fees || {};
    const late = fees.card_late_payment || {}, ol = fees.card_over_limit || {}, bnc = fees.emi_bounce || {}, pen = fees.penal_interest || {};
    const apr = Number((ref.finance_charge || {}).apr_pct || f.facility?.interest_rate_pct || 0);
    sourceRefs.push('Card fees & charges schedule', 'Loan penal-charge policy');
    if (q.operation === 'waiver') {
      return {
        text: `I understand — a client paying you late is exactly the kind of thing we should look at, and I am not treating this as deliberate. What I can do is log a fee-review / waiver request citing the reason (a delayed client receipt) for the two bounced EMIs, referenced to your existing service record, so an authorised officer assesses it. What I cannot do is confirm a waiver on this call — that needs approval. For context, each bounce charge is about ${inr0(bnc.total_inr)} and penal interest is running at roughly ${inr0(pen.running_per_month_inr)} a month across ${pen.current_overdue_emis || 2} overdue EMIs; regularising those two EMIs stops the penal interest growing while the waiver request is assessed.`,
        tool: 'fees.waiver_request', sourceRefs, rowsScanned: 1, plan: q,
      };
    }
    return {
      text: `Here are the exact charges if a payment is missed, all indicative and inclusive of GST where it applies. On the personal loan: a bounced EMI or failed auto-debit is about ${inr0(bnc.total_inr)} per instance, and penal interest of ${Number(pen.pct_per_month || 0).toFixed(0)}% per month applies on each overdue EMI (currently about ${inr0(pen.running_per_month_inr)} a month across ${pen.current_overdue_emis || 0} overdue EMIs). On the credit card: missing the minimum due adds a late-payment fee of about ${inr0(late.total_inr)}, and staying over the limit adds an over-limit fee of about ${inr0(ol.total_inr)}; interest also keeps accruing at ${apr.toFixed(1)}%. The exact figure is always confirmed on your statement.`,
      tool: 'fees.schedule', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'restructuring') {
    const ref = f.reference || {};
    const sma = ref.sma || {};
    const cons = ref.consolidation || {};
    const loan = (f.loans || []).find(l => /personal/i.test(l.type || '')) || (f.loans || [])[0] || {};
    sourceRefs.push('Loan facility', 'Collections & Restructuring policy', 'Affordability review');
    const base = `Because your income has dropped, we can look at reducing the monthly burden rather than only chasing the arrears. On your ${loan.type || 'personal loan'} of ${loan.outstanding_text || money(loan.outstanding_inr)} at ${loan.rate_pct || ''}%, an affordability review can consider: a step-down / lower-EMI restructure that reduces the ${loan.emi_text || money(loan.emi_inr)} instalment by extending the tenure; a short moratorium of a month or two on principal for breathing space; or converting the high-cost ${Number((ref.finance_charge || {}).apr_pct || 0).toFixed(0)}% card balance into a lower-rate EMI of about ${inr0(cons.consolidated_emi_inr)} a month.`;
    if (q.operation === 'hardship') {
      return {
        text: `${base} If things genuinely worsen, the pathway is a planned one, not a surprise: we agree a hardship or restructuring arrangement in writing, and the loan would only move toward default at 90 days past due — you have about ${sma.days_to_npa || 90} days before that from today, so there is time to arrange it properly. There is always a human handling your case; I can connect you to the restructuring / collections-support desk so it is managed with a plan, not pressure. I cannot commit terms on the call, but I can start the review today.`,
        tool: 'restructuring.hardship', sourceRefs, rowsScanned: 1, plan: q,
      };
    }
    return {
      text: `${base} The account is ${sma.class || 'in early stress'} at ${sma.days_past_due || 0} days past due, so the first steps are to clear or part-clear the two bounced EMIs and complete your re-KYC; then I raise the restructuring request with your current income position. I cannot commit the terms on this call, but I can start that review today, and a managed restructure is not recorded as a settlement.`,
      tool: 'restructuring.options', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'consolidation') {
    const ref = f.reference || {};
    const cons = ref.consolidation || {};
    const apr = Number((ref.finance_charge || {}).apr_pct || f.facility?.interest_rate_pct || 0);
    const disputed = Number(f.dispute?.amount_inr || 0);
    sourceRefs.push('Credit-card facility', 'Personal-loan pricing', 'Balance-transfer / consolidation policy');
    return {
      text: `Yes — moving the high-cost card balance into a lower-rate loan is a sensible question, so let me show the honest maths. On the verified card balance of ${money(cons.verified_card_balance_inr)}${disputed ? ` (I exclude the disputed ${money(disputed)} until it is resolved)` : ''}, an indicative consolidation at about ${Number(cons.indicative_rate_pct).toFixed(1)}% over ${cons.tenure_months} months would be roughly ${inr0(cons.consolidated_emi_inr)} a month. Compare that with today: at ${apr.toFixed(1)}% the card costs about ${inr0(cons.card_interest_only_monthly_inr)} a month in interest alone, and that barely reduces the principal. So the ${inr0(cons.consolidated_emi_inr)} consolidation EMI actually clears the debt in ${cons.tenure_months} months with total interest of about ${inr0(cons.total_interest_inr)}, instead of paying ${inr0(cons.card_interest_only_monthly_inr)} every month indefinitely on the card. This is indicative only — subject to eligibility, re-KYC, dispute closure and human credit approval — but I can start that assessment now.`,
      tool: 'consolidation.model', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'prepayment') {
    const ref = f.reference || {};
    const pp = ref.prepayment || {};
    const fc = pp.foreclosure_charge || {};
    sourceRefs.push('Personal-loan agreement', 'Prepayment & foreclosure policy');
    return {
      text: `Good instinct — prepaying is the cheapest way out, and I would encourage it. On your fixed-rate personal loan, after the first ${pp.lock_in_emis || 12} EMIs you can part-prepay up to ${Number(pp.part_prepay_free_pct_per_year || 0).toFixed(0)}% of the balance each year — about ${inr0(pp.part_prepay_free_limit_inr)} — with no charge. A full foreclosure attracts about ${Number(pp.foreclosure_charge_pct || 0).toFixed(0)}% of the outstanding plus GST, roughly ${inr0(fc.total_inr)} on today's balance. One condition: any arrears must be cleared first${pp.arrears_to_clear_first_inr ? ` (currently about ${inr0(pp.arrears_to_clear_first_inr)})` : ''}. So a lump sum used to part-prepay is charge-free and cuts your interest immediately; a full foreclosure is allowed too, just with that one-time charge. The part-prepayment route is the one I would recommend.`,
      tool: 'prepayment.terms', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'sma') {
    const ref = f.reference || {};
    const sma = ref.sma || {};
    sourceRefs.push('Asset-classification (SMA/NPA) policy', 'Loan facility');
    return {
      text: `No — being flagged is not the same as being a defaulter, and I want to be very clear about that. Your loan is marked ${sma.class || 'in an early-stress category'}${sma.meaning ? ` (${sma.meaning})` : ''}, an early-warning stage because two EMIs are overdue${sma.days_past_due ? ` (${sma.days_past_due} days)` : ''}. It is a signal for us to step in and help early, not a label that makes you a defaulter. A default — an NPA — is only recorded at 90 days past due, and you have about ${sma.days_to_npa || 90} days before that point; with a plan in place we avoid it entirely. In fact this flag is exactly why I am offering to restructure now — to bring the account back to a standard, healthy status rather than let it slip.`,
      tool: 'sma.explain', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'retention') {
    const ref = f.reference || {};
    const ret = ref.retention || {};
    const fc = (ref.prepayment || {}).foreclosure_charge || {};
    const score = f.credit_score?.score;
    sourceRefs.push('Relationship value', 'Rate-review policy', 'Foreclosure schedule');
    return {
      text: `That is a fair question and I would not blame you for comparing. Two honest points. First, switching now is not friction-free: you would need to foreclose this personal loan (a one-time charge of about ${inr0(fc.total_inr)}), and the other bank will still price for the two recent bounces and your current CIBIL${score ? ` of ${score}` : ''} — so the headline rate they advertise may not be the rate you actually get today. Second, staying lets us fix this from the inside: I can log a rate-review on your ${Number(ret.current_pl_rate_pct || 0).toFixed(1)}% personal loan toward our indicative floor of about ${Number(ret.indicative_review_floor_pct || 0).toFixed(1)}% (a review, not a guarantee) and consolidate the costly card balance at a lower rate — with no foreclosure charge. You have been with us about ${Number(ret.relationship_years || 0).toFixed(1)} years; I would rather earn your stay with a concrete review than lose you to a headline number. Shall I log that rate review?`,
      tool: 'retention.value', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  if (q.tool === 'guarantor') {
    const ref = f.reference || {};
    const sma = ref.sma || {};
    sourceRefs.push('Recovery & fair-practices policy', 'Facility structure');
    return {
      text: `I hear you, and let me answer this carefully because it matters. Your personal loan and credit card are unsecured facilities in your own name — there is no guarantor and no family member recorded on them, so there is nothing for the bank to pursue against your family on these accounts. Recovery is only ever from the borrower on the facility, through a proper, regulated and respectful process — never by pressuring relatives. Far more importantly, we are nowhere near that: the account is early-stage ${sma.class || 'stress'} with about ${sma.days_to_npa || 90} days before it would even be classified as a default, and we have real options — restructuring, a lower EMI, consolidation — to keep it well away from there. Let us put a plan in place today so this worry is off the table; I can also arrange a proper counselling and restructuring conversation for you.`,
      tool: 'guarantor.explain', sourceRefs, rowsScanned: 1, plan: q,
    };
  }
  sourceRefs.push('Customer 360', 'Account conduct', 'CRM');
  return { text: factsToText(f), tool: 'overview.summary', sourceRefs, rowsScanned: 1, plan: q };
}

/* =====================================================================
   FAST SEMANTIC NUDGE PATH.
   This is the latency-critical path: customer turn -> compact evidence -> LLM
   decision -> Teams. It deliberately performs no Tool API action and no CRM
   write. Do not use keyword rules; the model must understand the customer turn.
   ===================================================================== */
const FAST_NUDGE_TIMEOUT_MS = Math.max(1500, Number(process.env.FAST_NUDGE_TIMEOUT_MS || 3400));
const NUDGE_MIN_CONFIDENCE = Math.max(0, Math.min(1, Number(process.env.NUDGE_MIN_CONFIDENCE || 0.66)));

function dataBackedScenarioNudge(entry, scenario, fallback = {}) {
  const r = buildRecoverySnapshot(entry);
  const d = r.dispute;
  const ref = d?.ticket_id || r.existing_issue_refs.find((x) => x.reference)?.reference || '';
  const disputedLine = d ? `${money(d.amount_inr)} ${d.merchant || 'card'} dispute${ref ? ` (${ref})` : ''}` : 'the recorded service issue';
  const cardLine = r.card.limit_inr
    ? `${money(r.card.outstanding_inr)} outstanding against a ${money(r.card.limit_inr)} limit at ${r.card.apr_pct.toFixed(1)}% APR`
    : 'the recorded card position';
  const balanceLine = d && r.card.limit_inr
    ? `isolating the disputed amount leaves approximately ${money(r.card.undisputed_balance_inr)} (${r.card.undisputed_utilisation_pct.toFixed(1)}% utilisation)`
    : '';

  if (scenario === 'attrition') {
    const ref = entry.facts?.reference || {};
    const ret = ref.retention || {};
    const foreclosure = ((ref.prepayment || {}).foreclosure_charge || {}).total_inr;
    return {
      type: 'retention',
      nudge: `Do not respond with a generic "we value you". Acknowledge the competing offer, then hold it against facts: switching means foreclosing the personal loan (about ${money(foreclosure)}) and the other lender still prices the ${r.repayment.cibil_score || 'current'} CIBIL and the two recent bounces, so the advertised rate may not be the one actually offered. Counter with a concrete internal move — log a rate-review on the ${Number(ret.current_pl_rate_pct || 0).toFixed(1)}% loan toward the ~${Number(ret.indicative_review_floor_pct || 0).toFixed(1)}% floor (a review, not a promise) and consolidate the ${r.card.apr_pct.toFixed(1)}% card. Keep any open case; do not duplicate it.`,
      say: `I understand another bank has approached you, and I would not blame you for looking. Before you move — switching would mean foreclosing this loan and the new lender will still price in the recent misses, so the headline rate may not be what you actually get. If you stay, I can log a rate review on your loan and consolidate the costly card balance. Let me put that concrete step in place for you.`,
      basis: `Retention value, ${r.card.apr_pct.toFixed(1)}% card APR and rate-review policy; continue any existing case`,
    };
  }
  if (scenario === 'interest_relief') {
    return {
      type: 'stress',
      nudge: `Do not accept or reject a blanket interest waiver. Separate (1) charges attributable to the open ${disputedLine}, (2) the verified undisputed balance${balanceLine ? ` — ${money(r.card.undisputed_balance_inr)} at ${r.card.undisputed_utilisation_pct.toFixed(1)}% utilisation` : ''}, and (3) hardship options. Review dispute-linked fees first; then assess EMI conversion or restructuring under policy.`,
      say: `I cannot remove all interest on this call. I can review any interest or fees linked to the disputed amount, explain the remaining verified balance, and assess restructuring options that may reduce the burden.`,
      basis: `Card dispute, fair-practices and collections/restructuring controls; ${cardLine}`,
    };
  }
  if (scenario === 'dispute_distress') {
    return {
      type: 'service',
      nudge: `Continue the existing ${disputedLine}; do not create another case. Explain that no final refund decision or refund date is present in the current record, obtain the chargeback/provisional-credit status, and check whether disputed-leg interest or fees require authorised review.`,
      say: `This transaction is already under review${ref ? ` under ${ref}` : ''}. I do not want to give you an invented refund date; I will confirm the current chargeback status and whether any related charges need review.`,
      basis: 'Existing dispute record, chargeback policy and duplicate-case control',
    };
  }
  if (scenario === 'hardship') {
    const ref = entry.facts?.reference || {};
    const sma = ref.sma || {};
    const cons = ref.consolidation || {};
    const loanType = (entry.facts?.loans || [])[0]?.type || 'loan';
    return {
      type: 'stress',
      nudge: `Shift from collections language to an affordability review. The ${loanType} is ${sma.class || 'in early stress'} (${r.repayment.delayed_emis} EMI[s] overdue, ${sma.days_past_due || 0} DPD) alongside ${cardLine}; a genuine income drop is the trigger, not unwillingness. Offer concrete relief — a step-down EMI, a short moratorium, or consolidating the ${r.card.apr_pct.toFixed(1)}% card to about ${inr0(cons.consolidated_emi_inr)}/month — subject to affordability and approval, and note there are about ${sma.days_to_npa || 90} days before any NPA classification. Do not promise approval or imply default is imminent.`,
      say: `Let us first understand what changed in your income, then I can look at a lower, right-sized EMI or a short breathing space, and at moving the expensive card balance to a cheaper EMI. Nothing is decided on this call, but this is early enough that we have real options, and I will start the review today.`,
      basis: `Non-coercive collections, ${sma.class || 'early-stress'} affordability and restructuring policy`,
    };
  }
  if (scenario === 'compliance') {
    const kyc = entry.facts?.kyc || {};
    const kycBlock = kyc.rekyc_pending
      ? `a video re-KYC is pending (due ${kyc.due_date || 'the recorded date'}), which blocks any new credit, limit increase or consolidation until it is completed`
      : 'identity verification and written confirmation govern anything actionable on this call';
    return {
      type: 'compliance',
      nudge: `Do not give a yes/no approval or confirm any limit, rate, waiver or new facility on the call. Explain the verification steps and that ${kycBlock}. Offer to complete the video re-KYC (V-CIP) now; never ask for the full card number, PIN, OTP or password, and treat an OTP as authentication only, not as consent to any action.`,
      say: `I am not able to approve anything on this call, but I can walk you through exactly how we verify both your identity and that this is a genuine call from the bank. I will never ask for your full card number, PIN, OTP or password. If it helps, we can complete your video re-KYC right now, and I will send anything we discuss to you in writing.`,
      basis: `KYC / Re-KYC and customer-protection controls${kyc.rekyc_pending ? '; video re-KYC pending' : ''}`,
    };
  }
  if (scenario === 'growth') {
    const options = r.standard_options.map((x) => x.product).filter(Boolean).slice(0, 2);
    const stressed = (r.repayment.delayed_emis || 0) > 0 || (r.repayment.recent_returns || 0) > 0 || !!d;
    return {
      type: 'growth',
      nudge: stressed
        ? `Do not lead with new credit. The account shows ${cardLine}${d ? ` and an open ${disputedLine}` : ''}; service recovery and re-KYC come first, then reassess. ${options.length ? `Only ${options.join(' or ')} may be positioned, and only as subject to appraisal — never approved on the call.` : 'No new credit is eligible while the account is in stress or dispute.'}`
        : `Position only eligible plays and never promise approval, rate or limit on the call. ${options.length ? `Eligible or conditional: ${options.join(', ')}.` : 'No eligible upsell right now.'} Keep it suitability-led and capture consent before initiating any review.`,
      say: stressed
        ? `Before we look at anything new, I would like to settle the open items and complete your re-KYC so we start from a clean base — then we can properly explore the right options for you.`
        : (options.length
            ? `Based on your profile, ${options.join(' and ')} could suit you. I cannot confirm the terms on the call, but I can start a proper review and keep you updated in writing.`
            : `I would rather not add anything that increases pressure right now; let us keep your position stable and revisit growth once things settle.`),
      basis: 'Product suitability and service-recovery-first controls',
    };
  }
  if (scenario === 'scam') {
    const kyc = entry.facts?.kyc || {};
    return {
      type: 'compliance',
      nudge: `Treat the customer's suspicion as correct behaviour, not an obstacle. Prove the bank's authenticity instead of demanding trust: state plainly that the bank will never ask for the full card number, CVV, PIN, OTP or password; invite a call-back on the number on the card or statement; and verify the customer through video re-KYC${kyc.due_date ? ` (due ${kyc.due_date})` : ''}, not by extracting secrets. Never share or request sensitive credentials to "prove" identity.`,
      say: `You are absolutely right to check, and please always be this careful. I will never ask you for your full card number, CVV, PIN, OTP or password. You can also hang up and call the number on the back of your card and ask for me by name. If you are comfortable, we can verify your identity properly with a quick video re-KYC.`,
      basis: 'Customer-protection / anti-fraud and caller-verification controls',
    };
  }
  return {
    type: fallback.type || 'service',
    nudge: scrubSop(fallback.nudge),
    say: scrubSop(fallback.say),
    basis: fallback.basis ? friendlySop(fallback.basis) : '',
  };
}

export async function evaluateNudgeFast(cid, latest, context = '') {
  const started = Date.now();
  if (!aiReady()) return null;
  const entry = cache.get(cid) || await primeCustomer(cid);
  const client = await getClient();
  const sys = `You are the latency-critical semantic classifier and coaching layer for a Contoso Bank relationship manager in a Teams video call. The server has already verified that the speaker is the CUSTOMER. Do not use keyword rules; infer meaning from the whole turn and supplied evidence.

Return STRICT JSON:
{"nudge_required":true|false,"confidence":0.0,"scenario":"attrition|interest_relief|dispute_distress|hardship|compliance|growth|scam|other|none","type":"growth|stress|compliance|service|cross-sell|retention|none","nudge":"one concise instruction or null","say":"one safe sentence or null","basis":"short evidence basis or null"}

Use attrition when the customer threatens to leave or is tempted by another bank's offer. Use interest_relief for requests to remove, waive or stop interest/fees. Use dispute_distress when an existing disputed transaction is causing anger, urgency or confusion. Use hardship when inability to pay, income drop or affordability distress is the core issue. Use scam when the customer questions whether the call or caller is genuine, fears a scam or fraud, or hesitates to share details for safety. Use compliance when the customer demands an on-the-spot approval or a straight yes/no, pushes to bypass verification, or seeks any commitment (rate, limit, waiver, new facility) that cannot be made on a call. Use growth only when the customer proactively asks for a new product, a higher limit or a top-up. A neutral factual status question without distress normally needs no nudge because the detailed answer path handles it.

Critical controls: use existing case references and exact supplied numbers; never produce a generic reassurance when a concrete recovery plan is possible; never promise a refund, waiver, approval, settlement or release; never recommend a duplicate case. A new CRM case is never created from this fast path. Case registration is permitted only after the standard SOP route is exhausted, the RM asks for permission, and a later customer turn clearly confirms permission. Keep output compact.`;
  try {
    const r = await client.chat.completions.create({
      ...buildParams(fastChatModel, { temperature: 0.02, maxTokens: 96, json: true }),
      messages: [
        { role: 'system', content: sys },
        { role: 'user', content: `STABLE CUSTOMER NUDGE EVIDENCE:\n${entry.fastNudgeEvidenceJson || JSON.stringify(entry.fastNudgeEvidence || buildFastNudgeEvidence(entry))}\n\nRecent customer context (pronouns only): ${String(context || '').slice(0, 300)}\n\nLATEST CUSTOMER TURN:\n${String(latest || '').slice(0, 650)}` },
      ],
    }, { timeout: FAST_NUDGE_TIMEOUT_MS, maxRetries: 0 });
    const out = JSON.parse(r.choices[0].message.content || '{}');
    const confidence = Math.max(0, Math.min(1, Number(out.confidence || 0)));
    if (!out.nudge_required || confidence < NUDGE_MIN_CONFIDENCE) {
      console.log(`[fast-nudge] no nudge scenario=${out.scenario || 'none'} confidence=${confidence.toFixed(2)} latency=${Date.now() - started}ms`);
      return null;
    }
    const scenario = ['attrition','interest_relief','dispute_distress','hardship','compliance','growth','scam','other'].includes(out.scenario) ? out.scenario : 'other';
    const rendered = dataBackedScenarioNudge(entry, scenario, out);
    if (!String(rendered.nudge || '').trim()) return null;
    const latency = Date.now() - started;
    const cachedTokens = Number(r.usage?.prompt_tokens_details?.cached_tokens || 0);
    return {
      ...rendered,
      scenario,
      confidence,
      runtime: { mode: 'live_foundry_fast_nudge_v3', model: fastChatModel, latency_ms: latency, cached_prompt_tokens: cachedTokens },
    };
  } catch (e) {
    console.error('[fast-nudge]', e.message);
    return null;
  }
}

export async function evaluateCaseConsent(latest, pendingCase, context = '') {
  if (!aiReady() || !pendingCase?.draft) return { status: 'ambiguous', confidence: 0, reason: 'AI or pending case unavailable' };
  const client = await getClient();
  const sys = `You are a consent classifier for a bank CRM case-registration workflow. The RM has already explained the standard remedy and asked the customer for permission to register the specific formal case described below. Semantically classify ONLY the latest customer turn. Do not use keyword rules. Return STRICT JSON {"status":"affirmative|negative|ambiguous|new_issue","confidence":0.0,"reason":"short explanation"}. Affirmative requires a clear, voluntary yes to registering this case; general frustration, silence, a new question or agreement with facts is not consent.`;
  try {
    const r = await client.chat.completions.create({
      ...buildParams(chatModel, { temperature: 0, maxTokens: 70, json: true }),
      messages: [
        { role: 'system', content: sys },
        { role: 'user', content: `CASE THE RM ASKED PERMISSION TO REGISTER:\n${JSON.stringify({ subject: pendingCase.draft.subject, category: pendingCase.draft.category, summary: pendingCase.draft.summary })}\n\nRecent context: ${String(context || '').slice(0, 240)}\n\nLATEST CUSTOMER TURN:\n${String(latest || '').slice(0, 500)}` },
      ],
    }, { timeout: Math.min(FAST_NUDGE_TIMEOUT_MS, isReasoningModel(chatModel) ? 7000 : 3500), maxRetries: 0 });
    const out = JSON.parse(r.choices[0].message.content || '{}');
    return {
      status: ['affirmative','negative','ambiguous','new_issue'].includes(out.status) ? out.status : 'ambiguous',
      confidence: Math.max(0, Math.min(1, Number(out.confidence || 0))),
      reason: String(out.reason || '').slice(0, 180),
    };
  } catch (e) {
    console.error('[case-consent]', e.message);
    return { status: 'ambiguous', confidence: 0, reason: e.message };
  }
}

/* =====================================================================
   DETAILED ANSWER / ACTION / CASE PATH. It runs in parallel with the fast
   nudge path. `includeNudge:false` makes the fast path the sole live-coaching
   owner, preventing duplicate or late strategic nudges.
   ===================================================================== */
export async function respond(cid, latest, context = '', options = {}) {
  const started = Date.now();
  const includeNudge = options.includeNudge !== false;
  if (!aiReady()) return { answer: null, nudge: null, caseWorkflow: null, runtime: { mode: 'ai_unavailable' } };
  const entry = cache.get(cid) || await primeCustomer(cid);
  const facts = entry.facts;
  const nm = entry.name || cid;
  const nba = entry.nba;
  const strategy = nba ? JSON.stringify({
    stance: nba.stance,
    plays: (nba.plays || []).map(p => ({ product: p.product, eligibility: p.eligibility, the_number: p.the_number, say: p.say, guardrail: p.guardrail, basis: friendlySop(p.sop_basis) })),
    do_not_offer: nba.do_not_offer || [],
  }) : '(strategy unavailable)';

  const client = await getClient();
  const nudgeContract = includeNudge ? `\n "nudge":{"text":"...","say":"...","basis":"...","type":"growth|stress|compliance|service|cross-sell|retention"} or null,` : '';
  const nudgeInstruction = includeNudge ? 'Nudge only when the RM needs a strategic move.' : 'Do not generate a nudge in this detailed path; the dedicated fast semantic nudge workflow owns live coaching.';
  const caseState = JSON.stringify(options.caseState || { pending_consent: false, issue_history: [] });
  const sys = `You are the detailed AI answer/action planner for a Contoso Bank RETAIL RM on a Teams video call with ${nm}. Read the customer's LATEST transcribed utterance and recent context. Do not use keyword rules. Semantically decide whether the latest line is a factual question and/or an unresolved issue. Return STRICT JSON:
{
 "question":{"is_question":true|false,"confidence":0.0,"tool":"transactions|cases|card|card_limit|cashflow|loans|credit_score|dispute|interest_relief|kyc|savings|insurance|income|repayments|overview|product_eligibility|policy|verify_caller|fees|restructuring|consolidation|prepayment|sma|retention|guarantor|none","operation":"recent|largest|smallest|aggregate|summary|explain|status|list|search|category_breakdown|merchant_breakdown|eligibility|request|compare|interest|impact|waiver|hardship","direction":"all|debit|credit","limit":1-10,"period_days":number|null,"merchant":string|null,"category":string|null,"policy_query":string|null,"requested_limit_inr":number|null,"customer_authorises_action":true|false,"reason":"why this tool/operation"},
 "direct_answer":"Only for an answer fully supported by the compact EVIDENCE when no deterministic tool is needed; otherwise null",${nudgeContract}
 "case_workflow":{"action":"none|track_existing|continue_sop|seek_consent","issue_key":"short stable key","reason":"why","sop_exhausted":true|false,"existing_reference":"existing case/task reference or empty","draft":{"subject":"<=70 chars","summary":"2-3 sentences","category":"Card dispute|Chargeback|Fraud|Service request|Complaint|Collections|Escalation","priority":"High|Medium|Low","sentiment":"Positive|Neutral|Concerned|Negative","commitments_by_bank":"...","next_follow_up_date":"YYYY-MM-DD or ''"} or null} or null
}

QUESTION PLANNING EXAMPLES (semantic examples, not keyword rules):
- "my last five transactions" -> transactions/recent/all/5.
- "top highest transactions" -> transactions/largest/all/5.
- "largest payments I made" -> transactions/largest/debit.
- "highest money received" -> transactions/largest/credit.
- "total debits in the last 30 days" -> transactions/aggregate/debit/period_days=30.
- "why is utilisation over 100" -> card/explain.
- "am I spending too much or too little" -> cashflow/compare; explain that gross debits are not identical to discretionary spend.
- "what loans and EMI do I have" -> loans/summary.
- "when is my next EMI due / next loan payment date" -> loans/status. State the next EMI date and amount; never answer with the delayed-EMI count.
- "how much money do I have / my account balance / savings balance" -> savings/summary. Answer only the savings balance, never the full profile.
- "total outstanding on my loan and card / how much do I owe in total" -> loans/summary. It also returns the card outstanding and the combined total.
- "my CIBIL / credit score" -> credit_score/summary.
- "is my KYC done / re-KYC status / video KYC" -> kyc/status.
- "can you do my KYC / re-KYC on this call / complete my video KYC now / do the KYC right now" -> kyc/request. This means START the video re-KYC (V-CIP) capture on the call, never a final KYC approval; set customer_authorises_action=true when the customer clearly asks to do it now.
- "do I have any insurance / am I covered" -> insurance/summary.
- "my income this year / how much do I earn" -> income/summary.
- "how many EMIs have I missed / any bounces" -> repayments/summary.
- "what happened to my fraud complaint" -> dispute/status or cases/list depending on meaning.
- "why did I not get the GlobalMart refund" -> dispute/status. This is continuity on the existing dispute, never a new case.
- "remove all interest / I will not pay interest" -> interest_relief/explain. Split disputed-leg charges, verified balance and hardship options; never promise a blanket waiver.
- "can you increase my credit limit / what do you think" -> card_limit/eligibility; customer_authorises_action=false.
- "go ahead and increase my limit / please apply" -> card_limit/request; customer_authorises_action=true. This means initiate a review request, never change or approve the limit.
- "how do I know this call is really from the bank / are you a scammer / is this genuine" -> verify_caller/explain. Prove authenticity and never ask for full card number, PIN, OTP, CVV or password.
- "when is my next credit-card payment due / card bill due date / minimum due" -> card/status. Give the statement due date and minimum due.
- "how much interest am I paying on the card each month / monthly finance charge" -> card/interest.
- "why is my card rate 42% / is that too high / my card is over the limit, will it get blocked" -> card/explain.
- "what exactly do I need for KYC / what documents and by when" -> kyc/status. List pending documents and the deadline; offer to log a follow-up task.
- "what are the charges if I miss an EMI or card payment / late and penalty fees" -> fees/schedule.
- "two EMIs bounced because a client paid me late, can the penalty be waived" -> fees/waiver. Log a waiver request; never confirm a waiver on the call.
- "my income dropped, can you reduce my EMI or give me breathing room / restructure" -> restructuring/options.
- "if I truly cannot pay, what happens and what are my options" -> restructuring/hardship. Outline the hardship/collections pathway with a human in the loop.
- "can I move my card debt into a cheaper loan / consolidate, and what would it cost or save" -> consolidation/model. Show current vs consolidated EMI.
- "can I part-prepay or foreclose the personal loan, any penalty" -> prepayment/terms.
- "will restructuring or the missed EMIs damage my CIBIL / credit score" -> credit_score/impact.
- "my account is flagged as stress / SMA — does that make me a defaulter" -> sma/explain.
- "another bank is offering a lower rate, why should I stay" -> retention/value. Log a rate review; do not promise a rate.
- "if I default, will the bank come after my family or guarantors" -> guarantor/explain. Answer with care; there is no guarantor on these unsecured facilities.

Rules: small talk or an answer to an RM question => question.tool=none and case_workflow.action=none. Always choose the most SPECIFIC tool for a single fact; use "overview" ONLY when the customer explicitly asks for a full account summary or "tell me everything about my account" — never as a catch-all for a single question. ${nudgeInstruction} The question plan may select any supported tool and parameters; do not squeeze all transaction questions into "recent". A plain status question is not a new case. If an open CRM case/task already covers the issue, use track_existing with its reference and never create a duplicate. For a blanket waiver, dissatisfaction or attrition threat, first use continue_sop: explain the data, existing status and standard remedies. seek_consent is allowed only when no existing case covers the issue, the standard SOP path has already been explained and exhausted across the conversation, and a formal case is still appropriate. Even seek_consent does NOT create a case: it only tells the server to ask the RM to obtain explicit permission. The case may be written only after a later customer turn clearly confirms permission. When nudges are enabled, cite exact supplied numbers. Never recommend blocked products, never promise approval, refund, waiver or settlement, and never invent figures. A request to increase a card limit must use card_limit eligibility/request. If the customer explicitly says to proceed, set customer_authorises_action=true; the deterministic tool creates only an approval-gated review task, never a limit change. For action tools, do not also produce a generic growth nudge.`;

  let out;
  try {
    const r = await client.chat.completions.create({
      ...buildParams(chatModel, { temperature: 0.1, maxTokens: includeNudge ? 360 : 300, json: true }),
      messages: [
        { role: 'system', content: sys },
        { role: 'user', content: `COMPACT CUSTOMER EVIDENCE:\n${factsToText(facts)}\n\nGATED STRATEGY:\n${strategy}\n\nCASE GOVERNANCE STATE:\n${caseState}\n\nRecent customer context (for pronouns only): "${context}"\n\nLATEST TRANSCRIBED CUSTOMER LINE:\n"${latest}"` },
      ],
    });
    out = JSON.parse(r.choices[0].message.content);
  } catch (e) {
    console.error('[respond]', e.message);
    return { answer: null, nudge: null, caseWorkflow: null, runtime: { mode: 'ai_error', error: e.message, latency_ms: Date.now() - started } };
  }

  const plan = normaliseQuestionPlan(out.question || {});
  let executed = null;
  try { executed = await executeQuestionPlan(cid, entry, { ...(out.question || {}), ...plan }); }
  catch (e) { console.error('[tool execution]', e.message); }
  const direct = out.direct_answer && String(out.direct_answer).trim() !== 'null' ? String(out.direct_answer).trim() : null;
  const answerText = executed?.text || direct;
  const latency = Date.now() - started;
  const answer = answerText ? {
    text: answerText,
    runtime: {
      mode: 'live_foundry_transcript_plan', model: chatModel,
      tool: executed?.tool || 'evidence.direct_answer',
      confidence: plan.confidence,
      rows_scanned: executed?.rowsScanned || 0,
      source_refs: executed?.sourceRefs || ['Grounded customer evidence'],
      latency_ms: latency,
      plan_reason: plan.reason,
    },
  } : null;

  let nudge = null;
  const no = includeNudge ? executed?.nudgeOverride : null;
  if (no) {
    nudge = { nudge:no.nudge, say:no.say, basis:no.basis, type:no.type || 'service', runtime:{ mode:'deterministic_action_workflow', model:chatModel, latency_ms:latency, tool:executed?.tool } };
  } else if (includeNudge && out.nudge?.text && String(out.nudge.text).trim() !== 'null') {
    nudge = {
      nudge: scrubSop(out.nudge.text), say: scrubSop(out.nudge.say),
      basis: out.nudge.basis ? friendlySop(out.nudge.basis) : '',
      type: out.nudge.type || 'info',
      runtime: { mode: 'live_foundry_transcript_plan', model: chatModel, latency_ms: latency },
    };
  }
  let caseWorkflow = null;
  const cw = out.case_workflow || null;
  if (cw && ['none','track_existing','continue_sop','seek_consent'].includes(cw.action)) {
    const draft = cw.draft?.subject && String(cw.draft.subject).trim()
      ? { ...cw.draft, kind: cw.draft.kind || cw.draft.category || 'unresolved' }
      : null;
    caseWorkflow = {
      action: cw.action,
      issue_key: String(cw.issue_key || draft?.subject || 'unresolved_issue').slice(0, 90),
      reason: String(cw.reason || '').slice(0, 280),
      sop_exhausted: !!cw.sop_exhausted,
      existing_reference: String(cw.existing_reference || '').slice(0, 100),
      draft,
    };
    // Independent duplicate-case safety: even if the model forgets to carry the
    // existing reference, known dispute/fee/restructure records force continuity.
    const existingReference = caseWorkflow.existing_reference || existingReferenceForWorkflow(entry, caseWorkflow);
    if (existingReference) {
      caseWorkflow.action = 'track_existing';
      caseWorkflow.existing_reference = String(existingReference).slice(0, 100);
      caseWorkflow.sop_exhausted = false;
      caseWorkflow.draft = null;
      caseWorkflow.reason = `Continue existing record ${caseWorkflow.existing_reference}; duplicate case registration is prohibited.`;
    }
  }

  console.log(`[router] tool=${answer?.runtime?.tool || 'none'} qconf=${plan.confidence.toFixed(2)} nudge=${nudge ? nudge.type : 'no'} case_action=${caseWorkflow?.action || 'none'} latency=${latency}ms`);
  return { answer, nudge, caseWorkflow, runtime: answer?.runtime || nudge?.runtime || { mode: 'live_foundry_transcript_plan', latency_ms: latency } };
}

/* =====================================================================
   Post-call summary from the captured transcript. The record itself is stored
   separately and includes every utterance and AI event.
   ===================================================================== */
export async function generateCaseFromTranscript(cid, transcript) {
  const nm = getCustomerName(cid);
  const lines = (transcript || []).slice(-80).map(t => typeof t === 'string' ? `CUSTOMER: ${t}` : `${String(t.role || 'customer').toUpperCase()}: ${t.text || ''}`);
  const convo = lines.join('\n');
  if (!aiReady() || !convo.trim()) {
    return { subject: `Video call with ${nm}`, summary: 'Video call held; transcript saved for RM review.', sentiment: 'Neutral', category: 'Relationship review', commitments_by_customer: 'To be confirmed', commitments_by_bank: 'RM follow-up', next_follow_up_date: '' };
  }
  const client = await getClient();
  const sys = `You are logging a CRM interaction from a RETAIL bank video call with ${nm}. Use ONLY the transcript. Return STRICT JSON {"subject":"<=70 chars","summary":"2-4 concrete sentences","category":"Relationship review|Card dispute|Collections|Loan / limit|Service request|Compliance|Retention|Cross-sell","sentiment":"Positive|Neutral|Concerned|Negative","commitments_by_customer":"what was agreed, or None","commitments_by_bank":"what the bank will do, or RM follow-up","next_follow_up_date":"YYYY-MM-DD or ''"}. Never invent a commitment and never use approved/sanctioned.`;
  const r = await client.chat.completions.create({
    ...buildParams(chatModel, { temperature: 0.1, maxTokens: 420, json: true }),
    messages: [{ role: 'system', content: sys }, { role: 'user', content: `Captured call transcript:\n${convo}` }],
  });
  try { return JSON.parse(r.choices[0].message.content); }
  catch { return { subject: `Video call with ${nm}`, summary: convo.slice(0, 500), sentiment: 'Neutral', category: 'Relationship review', commitments_by_customer: 'To be confirmed', commitments_by_bank: 'RM follow-up', next_follow_up_date: '' }; }
}

export async function diagnose() {
  const out = { ok: false, endpoint, deployment: chatModel, fastDeployment: fastChatModel, reasoning: isReasoningModel(chatModel), toolApiReady: toolApiReady(), planner: 'semantic transcript -> structured tool plan' };
  if (!endpoint) return { ...out, reason: 'AZURE_AI_ENDPOINT not set' };
  try {
    const client = await getClient();
    const r = await client.chat.completions.create({ ...buildParams(chatModel, { maxTokens: 5 }), messages: [{ role: 'user', content: 'Reply with: ok' }] });
    out.sample = r.choices[0].message.content; out.ok = true;
  } catch (e) { return { ...out, error: e.status ? `${e.status} ${e.message}` : e.message }; }
  return out;
}
