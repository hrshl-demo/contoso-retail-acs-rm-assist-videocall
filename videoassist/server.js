import express from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { CommunicationIdentityClient } from '@azure/communication-identity';
import { DefaultAzureCredential } from '@azure/identity';
import {
  aiReady, groundingReady, primeCustomer, getCustomerName, warmNudgeModel,
  generateSynopsis, evaluateNudgeFast, evaluateCaseConsent, respond, diagnose, generateCaseFromTranscript,
} from './nudge-engine.js';
import { getRawFacts, crmPropose, crmApprove, saveCallRecord } from './toolapi.js';
import { postText, synopsisText, answerText, nudgeText, caseConsentNudgeText, caseConsentClarifyText, caseLoggedText, transcriptReadyText, callRequestText, cockpitLink, insightCard } from './teams.js';
import { recordInsight, getInsight, listInsights, attachInsightStream, insightStoreStats } from './insight-store.js';
import { graphConfigured, createRmCalendarMeeting } from './graph.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json({ limit: '256kb' }));
// The CRM is a separate Container App. These POC automation endpoints contain no
// webhook secret and are safe to invoke from the configured CRM origin.
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', process.env.CRM_ALLOWED_ORIGIN || '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});
const port = process.env.PORT || 3000;
// Bind address. On the VM this is 127.0.0.1 so Caddy is the ONLY public ingress and the
// Node process is unreachable from the internet regardless of NSG rules. Unset (all
// interfaces) preserves the previous container behaviour, where the platform did the
// isolation.
const host = process.env.HOST || undefined;

const connectionString = process.env.ACS_CONNECTION_STRING;
let identityClient;
if (connectionString) identityClient = new CommunicationIdentityClient(connectionString);
else console.warn('WARNING: ACS_CONNECTION_STRING not set â€” /token will error.');

// In-memory session state. customerId comes from the CRM handoff (Step 6 -> 7):
// the VideoAssist URL carries ?customer_id=CTB-MSME-001|002 and the client posts
// it to /session/start. The Teams webhook stays SERVER-SIDE (never the customer UI).
const TEAMS_WEBHOOK_URL = process.env.TEAMS_WEBHOOK_URL || null;
// A dedicated minimal Power Automate workflow may be configured for the latency-
// critical nudge. Teams remains mandatory; when unset we fall back to the main
// Teams workflow without changing current deployments.
const TEAMS_NUDGE_WEBHOOK_URL = process.env.TEAMS_NUDGE_WEBHOOK_URL || TEAMS_WEBHOOK_URL;
const CRM_BASE_URL = String(process.env.CRM_BASE_URL || '').replace(/\/+$/, '');
const NUDGE_FRESHNESS_MS = Math.max(2500, Number(process.env.NUDGE_FRESHNESS_MS || 5500));
const NUDGE_TEAMS_TIMEOUT_MS = Math.max(2000, Number(process.env.NUDGE_TEAMS_TIMEOUT_MS || 5000));
const FAST_PATH_HEADSTART_MS = Math.max(0, Number(process.env.FAST_PATH_HEADSTART_MS || 300));
const CASE_SOP_MIN_TURNS = Math.max(1, Number(process.env.CASE_SOP_MIN_TURNS || 2));
const DEFAULT_CUSTOMER_ID = process.env.DEFAULT_CUSTOMER_ID || 'CTB-RTL-002';
let session = newSession(DEFAULT_CUSTOMER_ID);
function newSession(cid, meta = {}) { return {
  sessionId: 'VCALL-' + Math.random().toString(36).slice(2,10).toUpperCase(), customerId: cid, meetingLink: null, startedAt: new Date().toISOString(),
  participantRole: String(meta.participantRole || 'customer').toLowerCase(), participantName: String(meta.participantName || ''),
  conversationType: String(meta.conversationType || 'customer_emergency_call'), transcript: [], aiEvents: [], lastNudgeAt: 0,
  lastAnswerText: '', lastNudgeText: '', loggedCaseKeys: new Set(), postedNudgeTurns: new Set(), caseLog: [],
  latestCustomerTurnId: 0, primingPromise: null, finalized: false, callRecord: null,
  issueWorkflows: new Map(), pendingCaseConsent: null, nudgePreviews: new Map()
}; }

app.get('/token', async (req, res) => {
  try {
    if (!identityClient) throw new Error('ACS_CONNECTION_STRING not configured');
    const user = await identityClient.createUser();
    const t = await identityClient.getToken(user, ['voip']);
    res.json({ userId: user.communicationUserId, token: t.token, expiresOn: t.expiresOn });
  } catch (e) { console.error('Token error:', e.message); res.status(500).json({ error: e.message }); }
});

const SPEECH_REGION = process.env.AZURE_SPEECH_REGION || 'centralindia';
const speechCredential = new DefaultAzureCredential();
const SPEECH_RESOURCE_ID = process.env.AZURE_SPEECH_RESOURCE_ID || '';

// Issues a short-lived Azure Speech token using the managed identity (Entra, no keys).
app.get('/speech/token', async (req, res) => {
  try {
    const aad = await speechCredential.getToken('https://cognitiveservices.azure.com/.default');
    // Authorization token format for Speech with AAD: "aad#<resourceId>#<aadToken>"
    const authToken = `aad#${SPEECH_RESOURCE_ID}#${aad.token}`;
    res.json({ token: authToken, region: SPEECH_REGION });
  } catch (e) {
    console.error('speech/token error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

app.get('/healthz', (req, res) => res.json({
  ok: true, aiReady: aiReady(), grounding: groundingReady(), teamsConfigured: !!TEAMS_WEBHOOK_URL, teamsNudgeConfigured: !!TEAMS_NUDGE_WEBHOOK_URL,
  cockpitDeepLinks: !!CRM_BASE_URL, insights: insightStoreStats(),
}));

/* ============================================================================
   Insight drill-down — the destination of the "Open in RM Cockpit" link on a
   Teams card. Served from the in-memory ring buffer (sub-50 ms, no DB).
   ============================================================================ */
app.get('/insights/stream', (req, res) => {
  const cid = String(req.query.customer_id || req.query.customerId || '').trim();
  attachInsightStream(req, res, { customerId: cid });
});
app.get('/insights/:eventId', (req, res) => {
  const entry = getInsight(req.params.eventId);
  if (!entry) return res.status(404).json({ error: 'unknown insight', eventId: req.params.eventId });
  res.json(entry);
});
app.get('/insights', (req, res) => {
  const cid = String(req.query.customer_id || req.query.customerId || '').trim();
  res.json({ ok: true, customerId: cid || null, insights: listInsights({ customerId: cid, limit: req.query.limit }) });
});
// Per-customer evidence pack (proxied from the Tool API) â€” handy for debugging.
app.get('/portfolio', async (req, res) => {
  const cid = (req.query.customer_id || session.customerId || DEFAULT_CUSTOMER_ID).toString();
  try { res.json(await getRawFacts(cid)); }
  catch (e) { res.status(502).json({ error: e.message }); }
});
app.get('/diag', async (req, res) => { try { res.json(await diagnose()); } catch (e) { res.status(500).json({ ok: false, error: e.message }); } });

// Pre-warm a selected customer before the participant joins. Demo Studio calls
// this during its explicit preparation step. This POC endpoint trusts the supplied
// customer id; production must authenticate the RM/session that is requesting it.
app.post('/session/prime', async (req, res) => {
  const cid = String(req.body?.customerId || req.body?.customer_id || DEFAULT_CUSTOMER_ID).trim();
  const started = Date.now();
  try {
    const entry = await primeCustomer(cid);
    void warmNudgeModel();
    res.json({ ok: true, customerId: cid, customerName: entry?.name || getCustomerName(cid), primedInMs: Date.now() - started, aiReady: aiReady(), grounding: groundingReady() });
  } catch (e) { console.error('[session/prime]', e.message); res.status(502).json({ error: e.message }); }
});

// Call start â†’ bind the MSME customer, generate synopsis, post to Teams (server-side webhook only).
app.post('/session/start', async (req, res) => {
  const cid = (req.body?.customerId || req.body?.customer_id || DEFAULT_CUSTOMER_ID).toString();
  session = newSession(cid, {
    participantRole: req.body?.participantRole || req.body?.participant_role || 'customer',
    participantName: req.body?.participantName || req.body?.participant_name || '',
    conversationType: req.body?.conversationType || req.body?.conversation_type || 'customer_emergency_call',
  });
  session.meetingLink = String(req.body?.meetingLink || '').trim() || null;
  try {
    const startedSession = session;
    // Return the session immediately. Customer-data priming, synopsis generation
    // and Teams delivery are background work and cannot delay microphone transcription.
    res.json({ ok: true, sessionId: startedSession.sessionId, customerId: cid, participantRole: startedSession.participantRole, participantName: startedSession.participantName, conversationType: startedSession.conversationType, aiReady: aiReady(), grounding: groundingReady(), teamsConfigured: !!TEAMS_WEBHOOK_URL, teamsNudgeConfigured: !!TEAMS_NUDGE_WEBHOOK_URL });
    startedSession.primingPromise = primeCustomer(cid)
      .catch((e) => { console.warn('prime failed:', e.message); return null; });
    void warmNudgeModel();
    if (aiReady() && TEAMS_WEBHOOK_URL) {
      startedSession.primingPromise.then(() => {
        if (session.sessionId !== startedSession.sessionId) return;
        generateSynopsis(cid)
        .then((synopsis) => {
          if (!synopsis) return false;
          const eventId = `${startedSession.sessionId}:synopsis`;
          const entry = recordInsight({
            eventId, kind: 'synopsis', customerId: cid, customerName: getCustomerName(cid),
            sessionId: startedSession.sessionId,
            headline: synopsis.headline || `Pre-call synopsis · ${getCustomerName(cid)}`,
            body: synopsis.summary || '',
            sources: ['customer_360', 'next_best_action'],
            extra: { risks: synopsis.risks || [], crossSell: synopsis.crossSell || [] },
          });
          const ctx = { customerId: cid, eventId, kind: 'synopsis' };
          return postText(TEAMS_WEBHOOK_URL, synopsisText(synopsis, getCustomerName(cid), ctx), {
            kind: 'synopsis', eventId,
            card: insightCard(entry), deepLink: cockpitLink(cid, eventId, 'synopsis'),
          });
        })
        .catch((e) => console.warn('[synopsis async]', e.message));
      });
    }
  } catch (e) { console.error('session/start:', e.message); if (!res.headersSent) res.status(500).json({ error: e.message }); }
});


app.get('/session/current', (req, res) => {
  const cid = String(req.query.customer_id || req.query.customerId || '').trim();
  if (cid && cid !== String(session.customerId)) return res.status(404).json({ error: 'no active session for customer' });
  res.json({
    ok: true, sessionId: session.sessionId, customerId: session.customerId,
    participantRole: session.participantRole, participantName: session.participantName,
    conversationType: session.conversationType, startedAt: session.startedAt,
    finalized: session.finalized, transcriptTurns: session.transcript.length,
    pendingCaseConsent: session.pendingCaseConsent ? { issueKey: session.pendingCaseConsent.issueKey, subject: session.pendingCaseConsent.draft?.subject } : null,
  });
});

const normaliseText = (x) => String((x && (x.text || x.nudge)) || x || '').replace(/\s+/g, ' ').trim().toLowerCase();

function caseStateSnapshot(callSession) {
  return {
    pending_consent: !!callSession.pendingCaseConsent,
    pending_issue: callSession.pendingCaseConsent ? {
      issue_key: callSession.pendingCaseConsent.issueKey,
      subject: callSession.pendingCaseConsent.draft?.subject,
      asked_at_turn: callSession.pendingCaseConsent.askedAtTurn,
    } : null,
    required_sop_turns_before_consent: CASE_SOP_MIN_TURNS,
    issue_history: [...callSession.issueWorkflows.values()].slice(-8).map((x) => ({
      issue_key: x.issueKey,
      sop_turns: x.sopTurns,
      last_action: x.lastAction,
      existing_reference: x.existingReference || '',
    })),
  };
}

// Compact consent state carried on an insight so the cockpit drawer can show
// exactly what the customer had (and had not) agreed to at that moment.
function caseConsentSnapshot(callSession) {
  const p = callSession?.pendingCaseConsent;
  return {
    status: p ? 'pending_customer_permission' : 'no_case_pending',
    pending_subject: p?.draft?.subject || '',
    asked_at_turn: p?.askedAtTurn ?? null,
    cases_registered: (callSession?.caseLog || []).length,
  };
}

async function processPendingCaseConsent(callSession, cid, latest, context, turnId, heardAt) {
  const pending = callSession.pendingCaseConsent;
  if (!pending) return { handled: false };
  const result = await evaluateCaseConsent(latest, pending, context);
  callSession.aiEvents.push({
    type: 'case_consent_classification', turnId, status: result.status, confidence: result.confidence,
    issueKey: pending.issueKey, trigger: latest, timestamp: new Date().toISOString(),
  });
  if (result.status === 'affirmative' && result.confidence >= 0.72) {
    const card = await writeCaseToCrm(cid, pending.draft, {
      live: true,
      heard: latest,
      customerConsent: { confirmed: true, utterance: latest, turnId, askedAtTurn: pending.askedAtTurn },
    });
    callSession.caseLog.push(card);
    callSession.aiEvents.push({ type: 'crm_case', turnId, text: card.summary, caseRef: card.caseRef, consentConfirmed: true, timestamp: new Date().toISOString() });
    callSession.loggedCaseKeys.add(normaliseText(pending.draft.subject).slice(0, 80));
    callSession.pendingCaseConsent = null;
    const state = callSession.issueWorkflows.get(pending.issueKey);
    if (state) { state.lastAction = 'case_registered_after_consent'; state.consentStatus = 'affirmative'; }
    return { handled: true, outcome: 'case_registered', caseRef: card.caseRef, elapsedMs: Date.now() - heardAt };
  }
  if (result.status === 'negative' && result.confidence >= 0.65) {
    callSession.pendingCaseConsent = null;
    const state = callSession.issueWorkflows.get(pending.issueKey);
    if (state) { state.lastAction = 'customer_declined_case'; state.consentStatus = 'negative'; }
    return { handled: true, outcome: 'customer_declined', elapsedMs: Date.now() - heardAt };
  }
  if (result.status === 'new_issue') {
    callSession.pendingCaseConsent = null;
    const state = callSession.issueWorkflows.get(pending.issueKey);
    if (state) state.lastAction = 'consent_interrupted_by_new_issue';
    return { handled: false, outcome: 'new_issue' };
  }
  if (TEAMS_NUDGE_WEBHOOK_URL) {
    await postText(TEAMS_NUDGE_WEBHOOK_URL, caseConsentClarifyText(pending, latest), {
      timeoutMs: NUDGE_TEAMS_TIMEOUT_MS,
      eventId: `${callSession.sessionId}:turn-${turnId}:case-consent-clarify`,
      kind: 'case_consent_clarify',
    });
  }
  return { handled: true, outcome: 'consent_ambiguous', elapsedMs: Date.now() - heardAt };
}

async function processCaseWorkflow(callSession, cid, workflow, latest, turnId) {
  if (!workflow || workflow.action === 'none') return { action: 'none' };
  const issueKey = normaliseText(workflow.issue_key || workflow.draft?.subject || 'unresolved_issue').slice(0, 90);
  const state = callSession.issueWorkflows.get(issueKey) || {
    issueKey, sopTurns: 0, lastAction: 'new', existingReference: '', consentStatus: 'not_requested',
  };
  state.lastAction = workflow.action;
  state.lastReason = workflow.reason || '';
  if (workflow.existing_reference) state.existingReference = workflow.existing_reference;

  if (workflow.action === 'track_existing') {
    state.sopTurns += 1;
    callSession.issueWorkflows.set(issueKey, state);
    callSession.aiEvents.push({ type: 'case_continuity', turnId, issueKey, existingReference: state.existingReference, trigger: latest, timestamp: new Date().toISOString() });
    return { action: 'track_existing', existingReference: state.existingReference };
  }

  if (workflow.action === 'continue_sop') {
    state.sopTurns += 1;
    callSession.issueWorkflows.set(issueKey, state);
    callSession.aiEvents.push({ type: 'case_sop_progress', turnId, issueKey, sopTurns: state.sopTurns, trigger: latest, timestamp: new Date().toISOString() });
    return { action: 'continue_sop', sopTurns: state.sopTurns };
  }

  // seek_consent is deliberately gated twice: the planner must mark the SOP as
  // exhausted, and this call session must already contain multiple earlier
  // SOP/continuity turns for the same issue. This prevents a first angry sentence
  // or a simple information request from becoming a CRM case.
  if (workflow.action === 'seek_consent') {
    if (!workflow.sop_exhausted || state.sopTurns < CASE_SOP_MIN_TURNS || !workflow.draft || state.existingReference) {
      state.sopTurns += 1;
      state.lastAction = 'seek_consent_suppressed_until_sop_complete';
      callSession.issueWorkflows.set(issueKey, state);
      callSession.aiEvents.push({ type: 'case_consent_suppressed', turnId, issueKey, reason: state.existingReference ? 'existing_case' : `sop_not_exhausted_${state.sopTurns}_of_${CASE_SOP_MIN_TURNS}`, trigger: latest, timestamp: new Date().toISOString() });
      return { action: 'continue_sop', suppressed: true };
    }
    if (callSession.pendingCaseConsent) return { action: 'consent_already_pending' };
    callSession.pendingCaseConsent = {
      issueKey,
      draft: workflow.draft,
      reason: workflow.reason,
      askedAtTurn: turnId,
      createdAt: new Date().toISOString(),
    };
    state.lastAction = 'consent_requested';
    state.consentStatus = 'pending';
    callSession.issueWorkflows.set(issueKey, state);
    callSession.aiEvents.push({ type: 'case_consent_nudge', turnId, issueKey, draft: workflow.draft, trigger: latest, timestamp: new Date().toISOString() });
    if (TEAMS_NUDGE_WEBHOOK_URL) {
      await postText(TEAMS_NUDGE_WEBHOOK_URL, caseConsentNudgeText(callSession.pendingCaseConsent, latest), {
        timeoutMs: NUDGE_TEAMS_TIMEOUT_MS,
        eventId: `${callSession.sessionId}:turn-${turnId}:case-consent-request`,
        kind: 'case_consent_request',
      });
    }
    return { action: 'seek_consent', pending: true };
  }
  return { action: 'none' };
}

function previewSimilarity(previewText, finalText) {
  const normalise = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9â‚¹]+/g, ' ').trim();
  const a = normalise(previewText), b = normalise(finalText);
  if (!a || !b) return 0;
  const coverage = Math.min(1, a.length / Math.max(1, b.length));
  const prefix = b.startsWith(a) || a.startsWith(b) ? coverage : 0;
  const as = new Set(a.split(/\s+/).filter(Boolean)), bs = new Set(b.split(/\s+/).filter(Boolean));
  let overlap = 0; as.forEach((token) => { if (bs.has(token)) overlap += 1; });
  const tokenScore = overlap / Math.max(1, Math.max(as.size, bs.size));
  return Math.max(prefix, tokenScore);
}

// Interim Azure Speech hypotheses are used only to pre-compute semantic coaching.
// They never enter the transcript, create a case, or post to Teams. The final
// recognised utterance must still arrive before any nudge is delivered.
app.post('/transcript/preview', async (req, res) => {
  const text = String(req.body?.text || '').trim();
  const requestedSessionId = String(req.body?.sessionId || '').trim();
  if (requestedSessionId && requestedSessionId !== session.sessionId) return res.status(409).json({ error: 'stale voice session' });
  const role = String(req.body?.role || session.participantRole || 'customer').toLowerCase();
  if (role !== 'customer' || !text || !aiReady() || session.pendingCaseConsent) return res.json({ ok: true, accepted: false });
  const turnId = Math.max(1, Number(req.body?.turnId || session.transcript.length + 1));
  const context = session.transcript.slice(-3).map((turn) => turn.text).join(' ');
  const prior = session.nudgePreviews.get(turnId);
  if (prior && previewSimilarity(prior.text, text) > 0.96 && Math.abs(prior.text.length - text.length) < 8) {
    return res.status(202).json({ ok: true, accepted: true, reused: true, turnId });
  }
  const preview = {
    text,
    startedAt: Date.now(),
    promise: evaluateNudgeFast(session.customerId || DEFAULT_CUSTOMER_ID, text, context)
      .then((result) => ({ result, completedAt: Date.now() }))
      .catch((error) => ({ result: null, completedAt: Date.now(), error: error.message })),
  };
  session.nudgePreviews.set(turnId, preview);
  while (session.nudgePreviews.size > 4) session.nudgePreviews.delete(session.nudgePreviews.keys().next().value);
  res.status(202).json({ ok: true, accepted: true, turnId });
});

// Live transcript chunk -> customer-only fast semantic nudge -> mandatory Teams,
// while the detailed answer/tool/case path runs concurrently in the background.
app.post('/transcript', async (req, res) => {
  const receivedAt = Date.now();
  const text = (req.body?.text || '').trim();
  const requestedSessionId = String(req.body?.sessionId || '').trim();
  if (requestedSessionId && requestedSessionId !== session.sessionId) return res.status(409).json({ error: 'stale voice session' });
  if (!text) return res.json({ ok: true });
  const role = (req.body?.role || session.participantRole || 'customer').toString().toLowerCase();
  const allowedRoles = new Set(['customer', 'rm', 'branch_manager', 'operations']);
  if (!allowedRoles.has(role)) return res.status(422).json({ error: 'unsupported transcript role' });
  const cid = session.customerId || DEFAULT_CUSTOMER_ID;
  const requestedTurnId = Number(req.body?.turnId || req.body?.turn_id || 0);
  const turnId = Number.isFinite(requestedTurnId) && requestedTurnId > 0 ? requestedTurnId : session.transcript.length + 1;
  const turn = { sequence: session.transcript.length + 1, turnId, role, text, timestamp: new Date().toISOString(), received_at_ms: receivedAt, source: req.body?.source || `${role}_speech_or_companion` };
  session.transcript.push(turn);
  const latest = text;
  const context = session.transcript.slice(-4, -1).map(t => t.text).join(' ');
  const now = Date.now();
  console.log(`[transcript:${cid}:${role}:turn-${turnId}] "${latest}"`);

  // RM companion and internal participant turns are evidence capture only. The
  // browser-provided role is a POC seam; production must derive it from an
  // authenticated participant/session mapping.
  if (role !== 'customer') return res.json({ ok: true, sessionId: session.sessionId, captured: true, transcriptTurns: session.transcript.length, turnId });
  if (!aiReady()) { console.log('[transcript] skipped: aiReady=false'); return res.json({ ok: true, turnId }); }

  // A CRM case can only be registered from a later customer turn that clearly
  // confirms the RM's explicit permission question. The same turn that raises
  // an issue can never create a case.
  if (session.pendingCaseConsent) {
    try {
      const consent = await processPendingCaseConsent(session, cid, latest, context, turnId, receivedAt);
      if (consent.handled) return res.json({ ok: true, sessionId: session.sessionId, turnId, caseConsent: consent });
    } catch (e) {
      console.error('[case-consent workflow]', e.message);
      return res.status(502).json({ error: 'Case consent could not be verified; no case was created.', detail: e.message });
    }
  }
  // Short de-bounce only; the old 1.2s gate dropped legitimate consecutive questions.
  if (now - session.lastNudgeAt <= 250) { console.log('[transcript] skipped: duplicate burst'); return res.json({ ok: true, turnId }); }
  session.lastNudgeAt = now;
  session.latestCustomerTurnId = Math.max(session.latestCustomerTurnId, turnId);
  const turnSession = session;
  const turnSessionId = session.sessionId;
  const norm = normaliseText;

  // A stable interim speech hypothesis may already have started the semantic
  // classifier. Reuse it only when it closely matches the final utterance and it
  // produced a positive nudge; otherwise run the authoritative final text.
  const preview = turnSession.nudgePreviews.get(turnId);
  const similarity = preview ? previewSimilarity(preview.text, latest) : 0;
  const previewEligible = !!preview && similarity >= 0.72 && preview.text.length >= Math.max(24, latest.length * 0.58);
  const finalNudgeEvaluation = previewEligible
    ? preview.promise.then(async (outcome) => {
        if (outcome?.result) {
          outcome.result.runtime = { ...(outcome.result.runtime || {}), speculative_preview_reused: true, preview_similarity: Number(similarity.toFixed(2)), preview_lead_ms: Math.max(0, receivedAt - preview.startedAt) };
          return outcome.result;
        }
        return evaluateNudgeFast(cid, latest, context);
      })
    : evaluateNudgeFast(cid, latest, context);
  turnSession.nudgePreviews.delete(turnId);

  const fastNudgePromise = finalNudgeEvaluation
    .then(async (nudge) => {
      const age = Date.now() - receivedAt;
      const stillCurrent = session.sessionId === turnSessionId && turnSession.latestCustomerTurnId === turnId;
      if (!nudge) return { posted: false, reason: 'no_nudge', latency_ms: age };
      if (!stillCurrent) { console.log(`[fast-nudge] turn-${turnId} suppressed: superseded`); return { posted: false, reason: 'superseded', latency_ms: age }; }
      if (age > NUDGE_FRESHNESS_MS) { console.log(`[fast-nudge] turn-${turnId} suppressed: stale ${age}ms`); return { posted: false, reason: 'stale', latency_ms: age }; }
      if (turnSession.postedNudgeTurns.has(turnId) || norm(nudge) === norm(turnSession.lastNudgeText)) return { posted: false, reason: 'duplicate', latency_ms: age };
      turnSession.postedNudgeTurns.add(turnId);
      turnSession.lastNudgeText = nudge.nudge;
      nudge.runtime = { ...(nudge.runtime || {}), end_to_end_ms: age };
      turnSession.aiEvents.push({ type: 'nudge', turnId, text: nudge.nudge, say: nudge.say, basis: nudge.basis, confidence: nudge.confidence, runtime: nudge.runtime, trigger: latest, timestamp: new Date().toISOString() });
      const eventId = `${turnSessionId}:turn-${turnId}:nudge`;
      // Recorded BEFORE the Teams POST so the cockpit's SSE cache is already warm
      // by the time the RM can physically click the link in the card.
      const entry = recordInsight({
        eventId, kind: 'live_nudge', customerId: cid, customerName: getCustomerName(cid),
        sessionId: turnSessionId, turnId,
        headline: `${String(nudge.type || 'info').toUpperCase()} nudge · ${getCustomerName(cid)}`,
        body: nudge.nudge, say: nudge.say, basis: nudge.basis,
        runtime: { ...(nudge.runtime || {}), confidence: nudge.confidence },
        sources: [nudge.scenario ? `scenario:${nudge.scenario}` : null, 'fast_nudge_evidence'].filter(Boolean),
        consent: caseConsentSnapshot(turnSession),
        trigger: latest,
      });
      const ok = await postText(TEAMS_NUDGE_WEBHOOK_URL, nudgeText(nudge, latest, { customerId: cid, eventId, kind: 'live_nudge' }), {
        timeoutMs: NUDGE_TEAMS_TIMEOUT_MS, eventId, kind: 'live_nudge',
        card: insightCard(entry), deepLink: cockpitLink(cid, eventId, 'live_nudge'),
      });
      console.log(`[teams] fast nudge turn-${turnId} posted=${ok} model=${nudge.runtime?.latency_ms || '?'}ms total=${Date.now() - receivedAt}ms`);
      return { posted: ok, reason: ok ? 'posted' : 'teams_failed', latency_ms: Date.now() - receivedAt };
    })
    .catch((e) => { console.error('[fast-nudge ERROR]', e.message); return { posted: false, reason: 'error', latency_ms: Date.now() - receivedAt }; });

  // Give the latency-critical classifier a short head start so two model calls
  // do not compete for the same deployment connection at the exact same instant.
  const detailedPromise = new Promise((resolve) => setTimeout(resolve, FAST_PATH_HEADSTART_MS))
    .then(() => respond(cid, latest, context, { includeNudge: false, caseState: caseStateSnapshot(turnSession) }))
    .then(async ({ answer, caseWorkflow, runtime }) => {
      if (session.sessionId !== turnSessionId) return;
      console.log(`[respond-detail] turn-${turnId} answer=${answer ? 'YES' : 'no'} case_action=${caseWorkflow?.action || 'none'}`);
      const posts = [];
      if (answer && TEAMS_WEBHOOK_URL && norm(answer) !== norm(turnSession.lastAnswerText)) {
        turnSession.lastAnswerText = answer.text;
        turnSession.aiEvents.push({ type: 'answer', turnId, text: answer.text, runtime: answer.runtime, trigger: latest, timestamp: new Date().toISOString() });
        const answerEventId = `${turnSessionId}:turn-${turnId}:answer`;
        const answerEntry = recordInsight({
          eventId: answerEventId, kind: 'answer', customerId: cid, customerName: getCustomerName(cid),
          sessionId: turnSessionId, turnId,
          headline: `Answer · ${answer.runtime?.tool ? `${answer.runtime.tool} lookup` : 'grounded response'}`,
          body: answer.text, runtime: answer.runtime,
          sources: [answer.runtime?.tool ? `tool:${answer.runtime.tool}` : null, answer.runtime?.operation ? `op:${answer.runtime.operation}` : null].filter(Boolean),
          consent: caseConsentSnapshot(turnSession),
          trigger: latest,
        });
        posts.push(postText(TEAMS_WEBHOOK_URL, answerText(answer, latest, { customerId: cid, eventId: answerEventId, kind: 'answer' }), {
          kind: 'answer', eventId: answerEventId,
          card: insightCard(answerEntry), deepLink: cockpitLink(cid, answerEventId, 'answer'),
        }).then(ok => console.log('[teams] answer posted:', ok)));
      } else if (answer) console.log('[teams] answer suppressed (duplicate)');
      // Answer cards and future non-live auxiliary cards post concurrently.
      if (posts.length) await Promise.allSettled(posts);
      const caseResult = await processCaseWorkflow(turnSession, cid, caseWorkflow, latest, turnId);
      return { runtime, caseResult };
    })
    .catch((e) => { console.error('[respond-detail ERROR]', e.message); });

  // Both workflows are owned by this request, but they execute concurrently.
  // The live Teams nudge posts as soon as the fast promise resolves; waiting for
  // the detailed promise here only keeps answer/case work lifecycle-safe.
  const [fast, detailedRuntime] = await Promise.all([fastNudgePromise, detailedPromise]);
  res.json({ ok: true, sessionId: turnSessionId, turnId, fastNudge: fast, detailedRuntime });
});

async function finalizeCurrentSession({ postToTeams = true } = {}) {
  if (session.finalized && session.callRecord) return session.callRecord;
  session.finalized = true;
  session.endedAt = new Date().toISOString();
  const cid = session.customerId || DEFAULT_CUSTOMER_ID;
  const summary = await generateCaseFromTranscript(cid, session.transcript).catch(e => ({ subject: `Video call with ${getCustomerName(cid)}`, summary: `Transcript captured; summary generation failed: ${e.message}`, sentiment: 'Neutral', category: 'Relationship review', commitments_by_customer: 'See transcript', commitments_by_bank: 'RM review', next_follow_up_date: '' }));
  const record = {
    record_id: `CALLREC-${session.sessionId}`,
    session_id: session.sessionId,
    customer_id: cid,
    customer_name: getCustomerName(cid),
    mode: session.conversationType === 'branch_manager_escalation' ? 'video_assist_internal_teams' : 'video_assist_teams',
    started_at: session.startedAt,
    ended_at: session.endedAt,
    capture_scope: session.transcript.some(t => t.role === 'rm') ? 'speaker_attributed_participant_plus_rm_companion_and_ai_events' : `${session.participantRole}_audio_plus_ai_and_crm_events`,
    transcript: session.transcript,
    ai_events: session.aiEvents,
    crm_cases: session.caseLog,
    summary,
    metadata: {
      transcript_source: 'Azure Speech participant microphone + CRM RM companion',
      meeting_link_present: !!session.meetingLink,
      participant_role: session.participantRole,
      participant_name: session.participantName || getCustomerName(cid),
      conversation_type: session.conversationType,
      full_teams_transcript_status: session.transcript.some(t => t.role === 'rm') ? 'speaker_attributed_poc_complete' : 'merge_seam_ready',
      work_iq_ready: true,
    },
  };
  if (groundingReady()) {
    const saved = await saveCallRecord(record);
    session.callRecord = saved.record || record;
  } else session.callRecord = record;
  if (postToTeams && TEAMS_WEBHOOK_URL) await postText(TEAMS_WEBHOOK_URL, transcriptReadyText(session.callRecord, summary)).catch(() => {});
  return session.callRecord;
}

app.post('/session/finalize', async (req, res) => {
  const requestedSessionId = String(req.body?.sessionId || '').trim();
  if (requestedSessionId && requestedSessionId !== session.sessionId) return res.status(409).json({ error: 'stale voice session' });
  try { res.json({ ok: true, record: await finalizeCurrentSession() }); }
  catch (e) { console.error('[finalize]', e.message); res.status(500).json({ error: e.message }); }
});

/* ============================================================================
   Consent-gated CRM case registration. Detecting an unresolved issue is not a
   write action. The SOP route must be exhausted, the RM must ask permission, and
   a later customer turn must clearly confirm permission before this function is
   allowed to materialise a CRM interaction.
   ============================================================================ */
async function writeCaseToCrm(cid, draft, { live = false, heard = '', customerConsent = null } = {}) {
  if (!customerConsent?.confirmed || !String(customerConsent.utterance || '').trim()) {
    throw new Error('Explicit customer consent from a later transcript turn is required before CRM case registration.');
  }
  const clean = (s) => String(s || '').replace(/\bsanctioned\b/gi, 'sanction').replace(/\bapproved\b/gi, 'agreed');
  const caseRef = 'CASE-' + Math.random().toString(36).slice(2, 7).toUpperCase();
  const payload = {
    subject: `[${caseRef}] ${clean(draft.subject)}`.slice(0, 150),
    summary: clean(draft.summary), channel: 'Video call',
    sentiment: draft.sentiment || 'Neutral', category: draft.category || 'Service request',
    priority: draft.priority || 'Medium',
    commitments_by_customer: clean(draft.commitments_by_customer || 'Awaiting bank follow-up'),
    commitments_by_bank: clean(draft.commitments_by_bank || 'RM follow-up'),
    next_follow_up_date: draft.next_follow_up_date || '', rm_id: 'RM-1042',
    consent_status: 'Confirmed on video call',
    consent_evidence: clean(customerConsent.utterance).slice(0, 240),
    consent_turn_id: customerConsent.turnId || '',
  };
  const cand = await crmPropose({ customer_id: cid, type: 'interaction', payload, evidence_refs: [live ? 'video_call_customer_consent' : 'video_call_wrapup_customer_consent'] });
  await crmApprove({ candidate_id: cand.candidate_id, approver: 'RM-1042 Â· Video Assist' });
  const card = { caseRef, candidateId: cand.candidate_id, subject: clean(draft.subject), summary: payload.summary,
    category: payload.category, sentiment: payload.sentiment, priority: payload.priority,
    commitments_by_bank: payload.commitments_by_bank, next_follow_up_date: payload.next_follow_up_date,
    customerConsentConfirmed: true, consentTurnId: customerConsent.turnId || '', live };
  if (TEAMS_WEBHOOK_URL) {
    const caseEventId = `${session.sessionId}:case-${caseRef}`;
    const caseEntry = recordInsight({
      eventId: caseEventId, kind: 'case_logged', customerId: cid, customerName: getCustomerName(cid),
      sessionId: session.sessionId, turnId: customerConsent.turnId || '',
      headline: `Case ${caseRef} · ${card.subject || payload.category}`,
      body: payload.summary,
      sources: [`crm_candidate:${cand.candidate_id}`, 'video_call_customer_consent'],
      consent: { status: 'confirmed_on_later_turn', utterance: String(customerConsent.utterance || '').slice(0, 240), turnId: customerConsent.turnId || '' },
      trigger: heard,
      extra: { caseRef, candidateId: cand.candidate_id, category: payload.category, priority: payload.priority, sentiment: payload.sentiment, commitments_by_bank: payload.commitments_by_bank, next_follow_up_date: payload.next_follow_up_date },
    });
    await postText(TEAMS_WEBHOOK_URL, caseLoggedText(card, heard, { customerId: cid, eventId: caseEventId, kind: 'case_logged' }), {
      kind: 'case_logged', eventId: caseEventId,
      card: insightCard(caseEntry), deepLink: cockpitLink(cid, caseEventId, 'case_logged'),
    }).catch(() => {});
  }
  console.log(`[case] logged ${caseRef} (${cand.candidate_id}) for ${cid}${live ? ' [live]' : ''}`);
  return card;
}

app.post('/case/log', async (req, res) => {
  if (process.env.ALLOW_MANUAL_CASE_LOG !== '1') {
    return res.status(405).json({ error: 'Manual case logging is disabled. Use the consent-gated live conversation workflow.' });
  }
  const cid = (req.body?.customerId || session.customerId || DEFAULT_CUSTOMER_ID).toString();
  if (!groundingReady()) return res.status(400).json({ error: 'Tool API not configured; cannot write to CRM.' });
  if (req.body?.customerConsent !== true || !String(req.body?.consentUtterance || '').trim()) {
    return res.status(422).json({ error: 'Explicit customer consent and the confirming customer utterance are required before a case can be logged.' });
  }
  try {
    const draft = await generateCaseFromTranscript(cid, session.transcript);
    const card = await writeCaseToCrm(cid, draft, { live: false, customerConsent: { confirmed: true, utterance: req.body.consentUtterance, turnId: req.body.turnId || '' } });
    session.caseLog.push(card);
    res.json({ ok: true, ...card });
  } catch (e) { console.error('[case] log failed:', e.message); res.status(500).json({ error: e.message }); }
});

/* ============================================================================
   STEP 7 â€” Customer self-service scheduling (additive; the live video call in
   /token, /session/start, /transcript is untouched).

   Feasibility: ACS does not expose calendars. The "real" version reads the RM's
   availability and creates a Teams meeting via a Power Automate flow the RM owns
   (Office 365 Outlook "Find meeting times" + "Create event / Teams meeting") â€”
   no Graph app registration or admin consent, mirroring the nudge webhook. Set
   SCHEDULE_WEBHOOK_URL to that flow's URL to make it real; otherwise we serve
   synthetic business-hours availability and record the booking, notifying the RM
   in Teams. Either way the customer joins the resulting Teams link via the SAME
   ACS->Teams join used by the live call.
   ============================================================================ */
const SCHEDULE_WEBHOOK_URL = process.env.SCHEDULE_WEBHOOK_URL || null;          // RM-owned Power Automate flow (creates meeting)
const SCHEDULE_AVAIL_URL = process.env.SCHEDULE_AVAILABILITY_WEBHOOK_URL || null; // optional real availability
const RM_DISPLAY_NAME = process.env.RM_DISPLAY_NAME || 'your Relationship Manager';
// Fully-automated RM-side meeting link. Set to the RM's standing Teams meeting join
// URL (or wire SCHEDULE_WEBHOOK_URL to a Power Automate flow that creates a fresh
// meeting). Either way the customer NEVER sees the link — they only get a Join button.
const RM_MEETING_URL = process.env.RM_MEETING_URL || process.env.RM_STANDING_MEETING_URL || null;
// Demo cadence: how long after the customer taps "Video call your RM" the call goes live.
const CALL_LEAD_SECONDS = Math.max(5, Math.min(600, Number(process.env.CALL_LEAD_SECONDS || 60)));
const bookings = new Map();   // id -> booking (in-memory POC store)

// Customer-facing RM name (strip the internal "(Branch RM, RM-2207)" suffix).
function rmCustomerName() { return String(RM_DISPLAY_NAME).replace(/\s*\(.*?\)\s*/g, '').trim() || 'your Relationship Manager'; }
// Indian-format rupees for the customer app.
function inr(n) { try { return '\u20B9' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 }); } catch (e) { return '\u20B9' + n; } }

// Baked customer-facing profile for the mobile banking portal. Real figures for
// Rakesh Sharma (CTB-RTL-002); the live in-call grounding still comes from the Tool
// API by customer_id. Kept static so the portal always renders, even offline.
const CUSTOMER_PROFILES = {
  'CTB-RTL-002': {
    name: 'Rakesh Sharma', tier: 'Contoso Priority Banking', memberSince: '2022', branch: 'Contoso Bank \u00B7 Pune (Camp)',
    accounts: [
      { kind: 'savings', name: 'Savings Account', mask: '\u2022\u2022 1123', primaryLabel: inr(23824), primarySub: 'Available balance' },
      { kind: 'card', name: 'Contoso Classic Credit Card', mask: '\u2022\u2022 0801', primaryLabel: inr(306247), primarySub: 'Outstanding of ' + inr(300000) + ' limit', alert: 'Over limit \u00B7 42% p.a.' },
      { kind: 'loan', name: 'Personal Loan', mask: '\u2022\u2022 PL01', primaryLabel: inr(410000), primarySub: 'Outstanding of ' + inr(600000) + ' \u00B7 EMI due' },
    ],
    quickActions: ['Pay card', 'Transfer', 'Statements', 'Rewards'],
    creditScore: 642, creditBand: 'Needs attention',
  },
  'CTB-RTL-003': {
    name: 'Meera Iyer', tier: 'Contoso Priority Banking', memberSince: '2019', branch: 'Contoso Bank \u00B7 Pune (Baner)',
    accounts: [
      { kind: 'savings', name: 'Savings Account', mask: '\u2022\u2022 3007', primaryLabel: inr(381876), primarySub: 'Available balance' },
      { kind: 'card', name: 'Contoso Classic Credit Card', mask: '\u2022\u2022 3021', primaryLabel: inr(52500), primarySub: 'Outstanding of ' + inr(300000) + ' limit' },
      { kind: 'loan', name: 'Personal Loan', mask: '\u2022\u2022 PL01', primaryLabel: inr(410000), primarySub: 'Outstanding of ' + inr(600000) },
    ],
    quickActions: ['Pay card', 'Transfer', 'Statements', 'Rewards'],
    creditScore: 771, creditBand: 'Good',
  },
  'CTB-RTL-004': {
    name: 'Imran Qureshi', tier: 'Contoso Priority Banking', memberSince: '2020', branch: 'Contoso Bank \u00B7 Pune (Hadapsar)',
    accounts: [
      { kind: 'savings', name: 'Savings Account', mask: '\u2022\u2022 4014', primaryLabel: inr(172407), primarySub: 'Available balance' },
      { kind: 'card', name: 'Contoso Classic Credit Card', mask: '\u2022\u2022 4038', primaryLabel: inr(132000), primarySub: 'Outstanding of ' + inr(300000) + ' limit' },
      { kind: 'loan', name: 'Personal Loan', mask: '\u2022\u2022 PL01', primaryLabel: inr(410000), primarySub: 'Outstanding of ' + inr(600000) },
    ],
    quickActions: ['Pay card', 'Transfer', 'Statements', 'Rewards'],
    creditScore: 694, creditBand: 'Fair',
  },
  'CTB-RTL-005': {
    name: 'Anita Deshmukh', tier: 'Contoso Priority Banking', memberSince: '2015', branch: 'Contoso Bank \u00B7 Pune (Aundh)',
    accounts: [
      { kind: 'savings', name: 'Savings Account', mask: '\u2022\u2022 5002', primaryLabel: inr(431011), primarySub: 'Available balance' },
      { kind: 'card', name: 'Contoso Classic Credit Card', mask: '\u2022\u2022 5049', primaryLabel: inr(52500), primarySub: 'Outstanding of ' + inr(300000) + ' limit' },
      { kind: 'loan', name: 'Personal Loan', mask: '\u2022\u2022 PL01', primaryLabel: inr(410000), primarySub: 'Outstanding of ' + inr(600000) },
    ],
    quickActions: ['Pay card', 'Transfer', 'Statements', 'Rewards'],
    creditScore: 771, creditBand: 'Good',
  },
};
function customerProfile(cid) {
  // NEVER fall back to another customer's financials. This map used to default
  // to Rakesh for any unknown id, which was invisible while he was the only
  // customer in the pack — but the moment the cockpit could hand a different
  // customer to this portal, it rendered THEIR name on top of HIS balances,
  // card outstanding and credit score. On a banking surface that is the worst
  // possible failure, so an unknown id now gets a name-only profile with no
  // figures at all (bank.js already renders that state cleanly).
  const base = CUSTOMER_PROFILES[cid];
  const primed = getCustomerName(cid);
  if (!base) {
    const name = (primed && primed !== cid) ? primed : 'Contoso customer';
    console.warn(`[portal] no baked profile for ${cid} — serving a figure-free profile.`);
    return {
      customerId: cid, name, greetingName: String(name).split(/\s+/)[0] || name,
      tier: 'Contoso Bank', memberSince: '',
      rm: { name: rmCustomerName(), title: 'Your Relationship Manager', branch: 'Contoso Bank' },
      accounts: [], quickActions: ['Transfer', 'Statements'],
      creditScore: null, creditBand: '', profileAvailable: false,
    };
  }
  // getCustomerName returns the id itself before the customer is primed — prefer the
  // baked display name so the portal always shows a real name.
  const name = (primed && primed !== cid) ? primed : base.name;
  return {
    customerId: cid, name, greetingName: String(name).split(/\s+/)[0] || name,
    tier: base.tier, memberSince: base.memberSince,
    rm: { name: rmCustomerName(), title: 'Your Relationship Manager', branch: base.branch },
    accounts: base.accounts, quickActions: base.quickActions,
    creditScore: base.creditScore, creditBand: base.creditBand, profileAvailable: true,
  };
}

// Customer-safe profile for the mobile banking portal (no internal risk flags).
app.get('/me', (req, res) => {
  const cid = String(req.query.customer_id || req.query.customerId || DEFAULT_CUSTOMER_ID).trim();
  res.json(customerProfile(cid));
});

function pad(n) { return String(n).padStart(2, '0'); }
function synthAvailability(days) {
  // The next `days` WORKING days that still have future 30-min slots (09:30â€“16:30).
  // We ALWAYS advance the cursor each iteration and bound the scan, so this can
  // never loop (a weekend or an after-hours clock just rolls to the next day).
  const out = []; const now = new Date();
  const cur = new Date(now); cur.setHours(0, 0, 0, 0);
  let scanned = 0;
  while (out.length < days && scanned < 14) {
    const dow = cur.getDay();
    if (dow !== 0 && dow !== 6) {               // weekday
      const slots = [];
      for (let h = 9; h <= 16; h++) {
        for (const m of [0, 30]) {
          if (h === 9 && m === 0) continue;
          const start = new Date(cur); start.setHours(h, m, 0, 0);
          if (start <= now) continue;           // future only
          const busy = ((h * 2 + (m ? 1 : 0) + cur.getDate()) % 5) === 0;  // deterministic "busy"
          slots.push({ startIso: start.toISOString(), time: `${pad(h)}:${pad(m)}`, available: !busy });
        }
      }
      if (slots.length) {
        const dayIso = `${cur.getFullYear()}-${pad(cur.getMonth() + 1)}-${pad(cur.getDate())}`;
        const label = cur.toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'short' });
        out.push({ dayIso, label, slots });
      }
    }
    cur.setDate(cur.getDate() + 1);             // ALWAYS advance â€” no infinite loop
    scanned++;
  }
  return out;
}

// RM availability for the next 1â€“2 days (real via webhook, else synthetic).
app.get('/availability', async (req, res) => {
  const days = Math.max(1, Math.min(3, parseInt(req.query.days, 10) || 2));
  if (SCHEDULE_AVAIL_URL) {
    try { const r = await fetch(`${SCHEDULE_AVAIL_URL}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days }) });
      if (r.ok) return res.json({ rm: RM_DISPLAY_NAME, source: 'rm-calendar', days: await r.json() }); } catch (e) { console.warn('avail webhook:', e.message); }
  }
  res.json({ rm: RM_DISPLAY_NAME, source: 'synthetic', days: synthAvailability(days) });
});

// Create a booking. Notifies the RM in Teams; optionally triggers the RM's
// Power Automate flow which creates the Teams meeting and returns a join link.
app.post('/bookings', async (req, res) => {
  const b = req.body || {};
  const slotIso = String(b.slotIso || '').trim();
  if (!slotIso) return res.status(400).json({ error: 'slotIso required' });
  const id = 'BK-' + Math.random().toString(36).slice(2, 8).toUpperCase();
  const booking = {
    id, customerId: b.customerId || null, name: (b.name || '').trim() || (b.customerId || 'Customer'),
    contact: (b.contact || '').trim(), slotIso, note: (b.note || '').trim(),
    status: 'requested', meetingLink: null, createdAt: new Date().toISOString(),
  };
  bookings.set(id, booking);
  const when = new Date(slotIso).toLocaleString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

  // (1) tell the RM in Teams (reuses the existing webhook)
  if (TEAMS_WEBHOOK_URL) {
    const msg = `<b>ðŸ“… New video-call booking Â· ${booking.name}</b><br>Requested for <b>${when}</b>${booking.contact ? ' Â· ' + booking.contact : ''}${booking.customerId ? '<br>Customer: ' + booking.customerId : ''}<br>Booking ${id}. Start your Teams â€œMeet nowâ€ at that time and share the join link.`;
    await postText(TEAMS_WEBHOOK_URL, msg).catch(() => {});
  }
  // (2) optionally have the RM's Power Automate flow create the real Teams meeting
  if (SCHEDULE_WEBHOOK_URL) {
    try {
      const r = await fetch(SCHEDULE_WEBHOOK_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(booking) });
      if (r.ok) { const j = await r.json().catch(() => ({})); const link = j.joinLink || j.meetingLink || j.joinUrl;
        if (link) { booking.meetingLink = link; booking.status = 'scheduled'; } }
    } catch (e) { console.warn('schedule webhook:', e.message); }
  }
  res.json(booking);
});

app.get('/bookings/:id', (req, res) => {
  const b = bookings.get(req.params.id); if (!b) return res.status(404).json({ error: 'not found' });
  res.json(b);
});

// RM (or the Power Automate flow) attaches the Teams join link to a booking.
app.post('/bookings/:id/link', async (req, res) => {
  const b = bookings.get(req.params.id); if (!b) return res.status(404).json({ error: 'not found' });
  const link = String((req.body || {}).meetingLink || '').trim(); if (!link) return res.status(400).json({ error: 'meetingLink required' });
  b.meetingLink = link; b.status = 'ready';
  res.json(b);
});

/* ============================================================================
   Instant "Call your RM" flow (customer mobile banking portal). The customer taps
   one button; the RM-side Teams meeting link is provisioned AUTOMATICALLY and held
   server-side. After CALL_LEAD_SECONDS the customer sees a "Join call" button that
   drops them into the RM's Teams meeting via the same ACS->Teams join. The customer
   is never shown a meeting link. A meeting request is posted to the RM's Teams the
   moment the call is scheduled.
   ============================================================================ */

// Provision the RM-side Teams meeting link. Preference order keeps this fully
// automated with no human in the loop:
//   1. SCHEDULE_WEBHOOK_URL  -> RM-owned Power Automate flow creates a fresh meeting
//                               + calendar event (no Graph app registration / consent).
//   2. Microsoft Graph       -> app-only Calendars.ReadWrite creates a REAL calendar
//                               event on the RM's calendar with a Teams meeting attached.
//   3. RM_MEETING_URL        -> the RM's standing Teams meeting join link.
//   4. synthetic demo link   -> so the flow always completes end-to-end offline.
async function provisionMeetingLink(booking) {
  if (SCHEDULE_WEBHOOK_URL) {
    try {
      const r = await fetch(SCHEDULE_WEBHOOK_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(booking) });
      if (r.ok) { const j = await r.json().catch(() => ({})); const link = j.joinLink || j.meetingLink || j.joinUrl;
        if (link) return { link, source: 'power-automate', calendared: true, webLink: j.webLink || j.eventLink || null, eventId: j.eventId || null }; }
    } catch (e) { console.warn('[call] provision via schedule webhook:', e.message); }
  }
  if (graphConfigured()) {
    try {
      const m = await createRmCalendarMeeting({
        subject: `Video banking call \u00B7 ${booking.name || booking.customerId}`,
        startIso: booking.scheduledAt, customerName: booking.name,
        note: booking.note || null,
      });
      return { link: m.joinUrl, source: 'graph', calendared: true, eventId: m.eventId, webLink: m.webLink };
    } catch (e) { console.warn('[call] provision via graph:', e.message); }
  }
  if (RM_MEETING_URL) return { link: RM_MEETING_URL, source: 'rm-standing-meeting' };
  return { link: `https://teams.microsoft.com/l/meetup-join/DEMO-${booking.id}`, source: 'synthetic', synthetic: true };
}

// The customer taps "Video call your RM". Schedule it CALL_LEAD_SECONDS out, provision
// the RM meeting link server-side, and notify the RM in Teams. No link is returned.
app.post('/call/request', async (req, res) => {
  const b = req.body || {};
  const cid = String(b.customerId || b.customer_id || DEFAULT_CUSTOMER_ID).trim();
  const lead = CALL_LEAD_SECONDS;
  const scheduledAt = new Date(Date.now() + lead * 1000).toISOString();
  const id = 'CALL-' + Math.random().toString(36).slice(2, 8).toUpperCase();
  const name = (b.name || '').trim() || getCustomerName(cid) || customerProfile(cid).name || 'Customer';
  const booking = {
    id, mode: 'instant', customerId: cid, name, contact: (b.contact || '').trim(), note: (b.note || '').trim(),
    slotIso: scheduledAt, scheduledAt, leadSeconds: lead, status: 'scheduling',
    meetingLink: null, rmJoinLink: null, synthetic: false, createdAt: new Date().toISOString(),
  };
  bookings.set(id, booking);
  try {
    const prov = await provisionMeetingLink(booking);
    booking.meetingLink = prov.link; booking.rmJoinLink = prov.link; booking.linkSource = prov.source; booking.synthetic = !!prov.synthetic;
    booking.calendared = !!prov.calendared; booking.calendarEventId = prov.eventId || null; booking.calendarWebLink = prov.webLink || null;
  } catch (e) { console.warn('[call] provision failed:', e.message); }
  // Notify the RM in Teams with a meeting request + the RM's own join link.
  if (TEAMS_WEBHOOK_URL) {
    const whenText = new Date(scheduledAt).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    postText(TEAMS_WEBHOOK_URL, callRequestText(booking, whenText), { kind: 'call-request', eventId: `${id}:request` }).catch(() => {});
  }
  // Pre-warm the AI + customer context so the RM's synopsis is ready when they join.
  void primeCustomer(cid).then(() => warmNudgeModel()).catch(() => {});
  res.json({ id, customerId: cid, scheduledAt, leadSeconds: lead, status: booking.status, rm: rmCustomerName() });
});

// Customer-safe call status. Never exposes the meeting link.
app.get('/call/:id', (req, res) => {
  const b = bookings.get(req.params.id); if (!b) return res.status(404).json({ error: 'not found' });
  const at = new Date(b.scheduledAt || b.slotIso).getTime();
  const remainingMs = Math.max(0, at - Date.now());
  const joinReady = remainingMs === 0 && !!b.meetingLink;
  if (joinReady && b.status !== 'ready') b.status = 'ready';
  res.json({
    id: b.id, customerId: b.customerId, scheduledAt: b.scheduledAt || b.slotIso,
    leadSeconds: b.leadSeconds || CALL_LEAD_SECONDS, remainingMs, joinReady,
    status: joinReady ? 'ready' : 'scheduling', rm: rmCustomerName(),
  });
});

// Opaque join: the video SPA fetches the Teams link by booking id ONLY when the call
// is live, so the customer joins without the link ever appearing in the UI.
app.get('/call/:id/join', (req, res) => {
  const b = bookings.get(req.params.id); if (!b) return res.status(404).json({ error: 'not found' });
  const at = new Date(b.scheduledAt || b.slotIso).getTime();
  if (Date.now() < at) return res.status(425).json({ error: 'too early', remainingMs: at - Date.now() });
  if (!b.meetingLink) return res.status(409).json({ error: 'meeting not ready' });
  res.json({ link: b.meetingLink, customerId: b.customerId, synthetic: !!b.synthetic });
});

// The customer mobile banking portal (logged-in journey; starts the instant call).
app.get('/bank', (req, res) => sendHtml(res, path.join(publicDir, 'bank.html')));

// Static front-end with asset-safe SPA fallback. The scheduling page is plain
// static under /public so the vite-built call app (dist) is left untouched.
const distDir = path.join(__dirname, 'dist');
const publicDir = path.join(__dirname, 'public');

// ---- Public path prefix injection -------------------------------------------------
// Behind Caddy this app lives at https://<host>/video/*, and `handle_path` STRIPS the
// prefix before proxying — so Express sees "/" and has no idea it is mounted anywhere.
// The BROWSER still needs the prefix on every URL it builds, and there are two distinct
// kinds of URL, which is why one mechanism alone is not enough:
//
//   1. Bundled asset URLs in dist/index.html  -> handled at BUILD time by Vite's `base`.
//   2. fetch() string literals in app code    -> Vite does NOT touch these. They are
//      handled HERE, at serve time, by injecting window.__VA_BASE__ into every HTML
//      response. client/main.js, public/bank.js and public/schedule.js all read it
//      through a single api() helper rather than hard-coding the prefix.
//
// Also substitutes the literal token __VA_BASE__ in HTML, which is how bank.html and
// schedule.html reference their own CSS/JS. Those two are served by Express and are NOT
// processed by Vite, so `base` never sees them.
//
// PUBLIC_BASE_PATH defaults to EMPTY, preserving the exact current behaviour for the
// phase9 Container App (served at the root). The VM deploy sets it to "/video".
const PUBLIC_BASE = String(process.env.PUBLIC_BASE_PATH || '').replace(/\/+$/, '');

function sendHtml(res, file) {
  let html;
  try {
    html = fs.readFileSync(file, 'utf8');
  } catch (e) {
    return res.status(404).send('Not found');
  }
  html = html.split('__VA_BASE__').join(PUBLIC_BASE);
  // Injected as the first child of <head> so it is defined before ANY other script runs,
  // including the module bundle and the classic scripts in bank.html / schedule.html.
  const tag = `<script>window.__VA_BASE__=${JSON.stringify(PUBLIC_BASE)};</script>`;
  if (/<head(\s[^>]*)?>/i.test(html)) html = html.replace(/<head(\s[^>]*)?>/i, (m) => m + tag);
  else html = tag + html;
  res.set('Content-Type', 'text/html; charset=utf-8');
  // The prefix is baked into the response, so a cached copy from a different mount point
  // would be actively wrong rather than merely stale.
  res.set('Cache-Control', 'no-store');
  return res.send(html);
}

app.use(express.static(publicDir, { index: false }));
app.get('/schedule', (req, res) => sendHtml(res, path.join(publicDir, 'schedule.html')));
// An explicitly requested /index.html must be routed BEFORE the static mount, or
// express.static serves it raw with a literal __VA_BASE__ left in the markup.
app.get('/index.html', (req, res) => sendHtml(res, path.join(distDir, 'index.html')));
// `index: false` on BOTH static mounts is load-bearing, not tidiness. With the default,
// express.static answers "/" with dist/index.html DIRECTLY and the request never reaches
// the SPA fallback below — which would silently bypass sendHtml and serve the page with
// window.__VA_BASE__ un-injected.
app.use(express.static(distDir, { index: false }));
app.get('*', (req, res) => {
  if (path.extname(req.path)) return res.status(404).send('Not found');
  sendHtml(res, path.join(distDir, 'index.html'));
});

const _listenArgs = host ? [port, host] : [port];
app.listen(..._listenArgs, () => console.log(`Video Assist listening on ${host || '0.0.0.0'}:${port} — AI ${aiReady() ? 'ready' : 'OFF'}, grounding ${groundingReady() ? 'Tool API' : 'OFF'}`));
