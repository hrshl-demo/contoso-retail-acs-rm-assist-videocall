// toolapi.js — server-side client for the Contoso MSME Tool API.
//
// Integration boundary (Step 6 -> Step 7): instead of grounding on a local
// retail JSON, Video Assist grounds the in-call synopsis and nudges on the REAL
// MSME customer evidence + SOPs that the Contoso FastAPI Tool API already serves.
// The shared bearer is held SERVER-SIDE only (never sent to the customer browser),
// exactly like the Teams webhook — so this widens no browser credential exposure.
//
// Every call is defensive: short timeout + typed errors, so a Tool API blip
// degrades gracefully (minimal synopsis / skipped nudge) and never breaks the
// live video call.

const BASE = (process.env.TOOLAPI_URL || '').replace(/\/+$/, '');
const BEARER = process.env.TOOLAPI_BEARER || '';
const TIMEOUT_MS = Number(process.env.TOOLAPI_TIMEOUT_MS || 6000);

export const toolApiReady = () => !!(BASE && BEARER);

async function tapi(pathname, { method = 'GET', body } = {}) {
  if (!toolApiReady()) throw new Error('Tool API not configured (TOOLAPI_URL/TOOLAPI_BEARER)');
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}${pathname}`, {
      method,
      headers: {
        Authorization: `Bearer ${BEARER}`,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(`Tool API ${method} ${pathname} -> ${res.status} ${detail.slice(0, 200)}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/* ---- typed endpoint helpers (one per intent the router can dispatch) ---- */

// Rich, citable MSME evidence pack: facility limits/utilisation, FY credits/debits,
// top buyer/supplier, stock & DP cover, cheque returns, covenants, GST, bureau.
export const getRawFacts = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/raw-facts`);

// Consolidated customer 360 (name, segment, headline metrics).
export const get360 = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/360`);

// Recent account/facility transactions (the MSME analogue of "last N transactions").
export const getRecentTransactions = (cid, limit = 5) =>
  tapi(`/v1/customers/${encodeURIComponent(cid)}/transactions/recent?limit=${encodeURIComponent(limit)}`);

// Deterministic limit-enhancement eligibility: eligible/blockers/band/caveats/evidence.
// This is what a "can you increase my limit" moment must reason over — NOT transactions.
export const getEnhancement = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/enhancement`);

// Eligibility-checked cross-sell opportunities (with blockers).
export const getCrossSell = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/cross-sell`);

// Flagship: Relationship Strategy & Next-Best-Action — eligibility-gated, SOP-grounded
// plays + talk-tracks + do-not-offer list. Powers both the CRM and the in-call nudges.
export const getNextBestAction = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/next-best-action`);

// Deterministic early-warning signals (narrative=false: let our nudge LLM phrase it).
export const getEws = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/ews?narrative=false`);

// CRM feed: interactions, service requests/cases, tasks, opportunities, write-backs.
// This is the right source for "open issues / cases / complaints" questions.
export const getCrmTimeline = (cid) => tapi(`/v1/customers/${encodeURIComponent(cid)}/crm-timeline`);

// Compact pre-call/live-call operating playbook. This is read once during
// session priming and contributes safe talk-tracks, open-ticket continuity and
// do-not-promise guidance to the low-latency nudge evidence pack.
export const getLiveCallPlaybook = (cid) =>
  tapi(`/v1/customers/${encodeURIComponent(cid)}/live-call-playbook`);

// Human-in-the-loop CRM write-back: propose a candidate, then approve it. The Tool
// API materialises an approved candidate into its in-memory CRM tables, so the
// dashboard shows it on the next refresh (no container restart).
export const crmPropose = (body) => tapi('/v1/crm/update-candidate', { method: 'POST', body });
export const crmApprove = (body) => tapi('/v1/crm/approve-update', { method: 'POST', body });

// Grounded SOP/policy retrieval over the Contoso AI Search index (Phase 5).
export const ragRetrieve = (query, topK = 3) =>
  tapi('/v1/rag/retrieve', { method: 'POST', body: { query, top_k: topK } });

// Pre-warmed transaction views plus generic AI-planned transaction execution.
export const getTransactionInsights = (cid) =>
  tapi(`/v1/customers/${encodeURIComponent(cid)}/transactions/insights`);
export const queryTransactions = (cid, plan) =>
  tapi(`/v1/customers/${encodeURIComponent(cid)}/transactions/query`, { method: 'POST', body: plan });


// Retail card-limit review: deterministic pre-screen and approval-gated request.
export const getCardLimitAssessment = (cid) =>
  tapi(`/v1/customers/${encodeURIComponent(cid)}/card-limit-assessment`);
export const initiateCardLimitReview = (cid, requestedLimitInr = null) =>
  tapi(`/v1/customers/${encodeURIComponent(cid)}/card-limit-review`, { method: 'POST', body: { requested_limit_inr: requestedLimitInr, actor: 'RM-2207', customer_consent: true } });

// Persist the role-tagged transcript + AI/CRM events centrally so the normal CRM
// can list and download it after the call.
export const saveCallRecord = (record) => tapi('/v1/call-records', { method: 'POST', body: record });
