import { CallClient, LocalVideoStream, VideoStreamRenderer } from '@azure/communication-calling';
import { AzureCommunicationTokenCredential } from '@azure/communication-common';
import './styles.css';
import * as SpeechSDK from 'microsoft-cognitiveservices-speech-sdk';

const $ = (id) => document.getElementById(id);
const log = (m) => { const el = $('log'); el.textContent += m + '\n'; el.scrollTop = el.scrollHeight; console.log(m); };

let callClient, callAgent, deviceManager, call;
let localVideoStream, localRenderer;
let micOn = true, camOn = true;
const remoteRenderers = new Map();
let recognizer = null, recognizing = false, sessionStarted = false, tokenTimer = null;
let previewTimer = null, latestPreviewText = '', lastPreviewSent = '';
let voiceSessionId = null, finalizePromise = null;
let nextTurnId = 0;
const pendingTranscripts = [];
let sessionStartPromise = null;
const QUERY = new URLSearchParams(location.search);
const CUSTOMER_ID = QUERY.get('customer_id') || '';
const PARTICIPANT_ROLE = (QUERY.get('participant_role') || 'customer').toLowerCase();
const PARTICIPANT_NAME = QUERY.get('participant_name') || (PARTICIPANT_ROLE === 'branch_manager' ? 'Branch Manager' : 'Customer');
const CONVERSATION_TYPE = QUERY.get('conversation_type') || (PARTICIPANT_ROLE === 'branch_manager' ? 'branch_manager_escalation' : 'customer_emergency_call');

(function personaliseParticipantUi(){
  window.addEventListener('DOMContentLoaded', () => {
    const internal = PARTICIPANT_ROLE === 'branch_manager';
    const title = document.querySelector('.setup-copy h1');
    const desc = document.querySelector('.setup-copy p');
    const consent = document.querySelector('.consent');
    const localCaption = document.querySelector('.tile.self figcaption');
    const remoteCaption = document.querySelector('.tile:not(.self) figcaption');
    const remotePh = document.querySelector('#remoteVideo .ph');
    if (title) title.textContent = internal ? `Internal escalation with ${PARTICIPANT_NAME}` : 'Speak with your Relationship Manager';
    if (desc) desc.textContent = internal ? 'Join the RM’s Teams meeting to review the case together. The speaker-attributed transcript becomes CRM evidence.' : 'Paste the meeting link your RM shared, then start your secure video session.';
    if (consent) consent.textContent = internal ? 'This synthetic internal session is transcribed and added to the case evidence pack.' : 'Your session is recorded and AI-assisted to help your Relationship Manager serve you better.';
    if (localCaption) localCaption.textContent = PARTICIPANT_NAME;
    if (remoteCaption) remoteCaption.textContent = 'Relationship Manager';
    // The remote tile now renders a skeleton; keep the label in sync with it.
    const remoteWaitText = document.querySelector('#remoteVideo .rx-wait-text');
    if (remoteWaitText) remoteWaitText.textContent = REMOTE_WAIT_LABEL;
    else if (remotePh) remotePh.textContent = 'Waiting for your RM…';
    document.title = internal ? `Video Assist — ${PARTICIPANT_NAME}` : 'Video Assist — Relationship Manager';
  });
})();

function setStatus(s, label) { $('statusPill').dataset.state = s; $('statusText').textContent = label; }
function setPhase(p) { document.body.dataset.phase = p; }
function setLoading(on) { $('startBtn').dataset.loading = on ? 'true' : 'false'; $('startBtn').disabled = on; }

/* =====================================================================
   Phase 4 polish — active-speaker ring driven by REAL audio, and a
   skeleton placeholder while the RM has not joined. Nothing here fakes
   activity: the remote ring comes from ACS's own isSpeakingChanged and
   the local ring from a Web Audio AnalyserNode measuring the mic.
   ===================================================================== */
const REMOTE_WAIT_LABEL = 'Waiting for your RM';
function remoteWaitHtml(label) {
  return '<div class="rx-wait" role="status" aria-live="polite">'
    + '<div class="rx-wait-av rx-sk" aria-hidden="true"></div>'
    + '<div class="rx-wait-lines" aria-hidden="true">'
    + '<div class="rx-wait-line rx-sk"></div><div class="rx-wait-line short rx-sk"></div></div>'
    + '<p class="rx-wait-label ph"><span class="rx-wait-text">' + (label || REMOTE_WAIT_LABEL)
    + '</span><span class="rx-wait-dots" aria-hidden="true"></span></p></div>';
}
function resetRemoteTile(label) {
  const f = $('remoteVideo');
  if (!f) return;
  f.innerHTML = remoteWaitHtml(label);
  f.classList.remove('has-video', 'rx-speaking');
}

// --- local mic level -> ring ------------------------------------------------
let micAudioCtx = null, micAnalyser = null, micStream = null, micRaf = 0;
async function startLocalSpeakingMeter() {
  if (micAnalyser) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || !navigator.mediaDevices?.getUserMedia) return;
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micAudioCtx = new AC();
    if (micAudioCtx.state === 'suspended') await micAudioCtx.resume().catch(() => {});
    const src = micAudioCtx.createMediaStreamSource(micStream);
    micAnalyser = micAudioCtx.createAnalyser();
    micAnalyser.fftSize = 512;
    micAnalyser.smoothingTimeConstant = 0.75;
    src.connect(micAnalyser);
    const buf = new Uint8Array(micAnalyser.fftSize);
    const frame = $('localVideo');
    let speaking = false, quietFrames = 0;
    const tick = () => {
      micRaf = requestAnimationFrame(tick);
      if (!micAnalyser || !frame) return;
      micAnalyser.getByteTimeDomainData(buf);
      // RMS of the time-domain waveform, normalised around the 128 midpoint.
      let sum = 0;
      for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / buf.length);
      const level = Math.min(1, rms * 7);
      frame.style.setProperty('--rx-level', level.toFixed(2));
      // Hysteresis: quick to light up, slow to drop, so it doesn't strobe
      // between syllables.
      if (micOn && rms > 0.045) { quietFrames = 0; if (!speaking) { speaking = true; frame.classList.add('rx-speaking'); } }
      else if (speaking && ++quietFrames > 22) { speaking = false; frame.classList.remove('rx-speaking'); }
    };
    tick();
  } catch (e) { log('Speaking indicator unavailable: ' + (e?.message || e)); }
}
function stopLocalSpeakingMeter() {
  if (micRaf) { cancelAnimationFrame(micRaf); micRaf = 0; }
  micAnalyser = null;
  try { micStream?.getTracks().forEach((t) => t.stop()); } catch (_) {}
  micStream = null;
  try { micAudioCtx?.close(); } catch (_) {}
  micAudioCtx = null;
  const f = $('localVideo');
  if (f) { f.classList.remove('rx-speaking'); f.style.removeProperty('--rx-level'); }
}

// In-app browsers (WhatsApp, Instagram, Facebook, etc.) block camera/mic/WebRTC, which
// makes the ACS call "time out". Detect and warn the customer to use a real browser.
const IN_APP_BROWSER = /(WhatsApp|Instagram|FBAN|FBAV|FB_IAB|Line|Snapchat|Twitter|MicroMessenger)/i.test(navigator.userAgent || '');
if (IN_APP_BROWSER) {
  window.addEventListener('DOMContentLoaded', () => {
    try {
      const b = document.createElement('div');
      b.style.cssText = 'position:fixed;inset:0 0 auto 0;z-index:9999;background:#b00020;color:#fff;padding:14px 16px;font:600 14px/1.4 system-ui,sans-serif;text-align:center';
      b.textContent = "Please open this link in your phone's browser (Chrome or Safari). In-app browsers (WhatsApp/Instagram) block the camera and microphone, so the video call cannot connect here.";
      document.body.appendChild(b);
    } catch (_) {}
  });
  try { log("In-app browser detected — open in Chrome/Safari for camera & mic to work."); } catch (_) {}
}

async function fetchToken() {
  const r = await fetch('/token');
  if (!r.ok) { const b = await r.json().catch(() => ({})); throw new Error('Token service (' + r.status + ') ' + (b.error || '')); }
  return (await r.json()).token;
}

$('startBtn').onclick = startSession;
async function startSession() {
  const link = $('meetingLink').value.trim();
  if (!link) { flashInput('Enter the meeting link from your RM to continue.'); return; }
  try {
    setLoading(true); setStatus('connecting', 'Connecting…');
    if (!callAgent) {
      log('Requesting access…');
      const token = await fetchToken();
      callClient = new CallClient();
      callAgent = await callClient.createCallAgent(new AzureCommunicationTokenCredential(token), { displayName: PARTICIPANT_NAME });
      deviceManager = await callClient.getDeviceManager();
      await deviceManager.askDevicePermission({ audio: true, video: true });
    }
    const videoOptions = {};
    const cams = await deviceManager.getCameras();
    if (cams && cams[0]) { localVideoStream = new LocalVideoStream(cams[0]); videoOptions.localVideoStreams = [localVideoStream]; await showLocal(localVideoStream); camOn = true; }
    else { camOn = false; log('No camera detected — audio only.'); }
    log('Joining the session…');
    call = callAgent.join({ meetingLink: link }, { videoOptions });
    micOn = true; setPhase('in-call'); reflectControls(); wireCall();
    void startLocalSpeakingMeter();
  } catch (e) { log('Could not start: ' + (e?.message || e)); setStatus('error', "Couldn't connect"); setLoading(false); }
}

function wireCall() {
  call.on('stateChanged', () => {
    const s = call.state; log('Call: ' + s);
    if (s === 'Connecting') setStatus('connecting', 'Connecting…');
    else if (s === 'Ringing') setStatus('connecting', 'Ringing…');
    else if (s === 'Connected') { setStatus('connected', 'Connected'); setLoading(false); onConnected(); }
    else if (s === 'Disconnected') { const r = call.callEndReason; if (r && r.code) log('Ended (code ' + r.code + '/' + r.subCode + ').'); setStatus('idle', 'Session ended'); finalizeCall(); endCleanup(); }
  });
  call.on('isMutedChanged', () => { micOn = !call.isMuted; reflectControls(); if (!micOn) log('Customer microphone muted — transcript triggers paused.'); });
  call.on('remoteParticipantsUpdated', (e) => e.added.forEach(subscribe));
  call.remoteParticipants.forEach(subscribe);
}

// Step 6 -> Step 7 handoff: the CRM opens this app with ?customer_id=CTB-MSME-001|002.
// We forward it to the backend so the synopsis/nudges ground on that MSME customer.

// Self-service scheduling (Step 7) hands off here with ?link=<Teams meeting link>.
// Instant "Call your RM" hands off with ?booking=<id> and we fetch the link opaquely
// (the customer never sees a meeting link). Either way the customer just clicks Start.
(function () {
  const lnk = QUERY.get('link');
  if (lnk) { const el = $('meetingLink'); if (el) el.value = lnk; }
  const bookingId = QUERY.get('booking') || QUERY.get('call');
  if (bookingId) prepareOpaqueJoin(bookingId);
})();

// Instant-call handoff: the customer tapped "Join call" in the mobile banking app.
// We fetch the RM's Teams meeting link SERVER-SIDE by booking id and drop it into the
// (now hidden) input, so the customer joins without ever seeing a meeting link.
async function prepareOpaqueJoin(bookingId, attempt) {
  attempt = attempt || 0;
  const input = $('meetingLink');
  const btn = $('startBtn');
  const btnLabel = btn ? btn.querySelector('.btn-label') : null;
  const title = document.querySelector('.setup-copy h1');
  const desc = document.querySelector('.setup-copy p');
  if (title) title.textContent = 'Connect with your Relationship Manager';
  if (desc) desc.textContent = 'Your secure video call is ready. Tap Join to speak with your RM now.';
  if (input) input.style.display = 'none';
  if (btnLabel) btnLabel.textContent = 'Join now';
  try {
    const r = await fetch('/call/' + encodeURIComponent(bookingId) + '/join');
    if (r.ok) { const j = await r.json(); if (j && j.link && input) { input.value = j.link; log('Secure call ready — tap Join now.'); return; } }
    if (r.status === 425 && attempt < 30) { setTimeout(function () { prepareOpaqueJoin(bookingId, attempt + 1); }, 1500); return; }
    log('Your call is being set up — please wait a moment, then tap Join now.');
  } catch (e) {
    if (attempt < 30) { setTimeout(function () { prepareOpaqueJoin(bookingId, attempt + 1); }, 1500); return; }
    log('Could not reach the call service — please retry.');
  }
}

async function onConnected() {
  if (sessionStarted) return; sessionStarted = true;
  // Start Azure Speech immediately. Session creation, customer-data priming and
  // synopsis generation happen in parallel; early final transcripts are buffered
  // until the server returns the authoritative session id.
  void startRecognition();
  sessionStartPromise = (async () => {
    try {
      const r = await fetch('/session/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...(CUSTOMER_ID ? { customerId: CUSTOMER_ID } : {}), meetingLink: $('meetingLink').value.trim(), participantRole: PARTICIPANT_ROLE, participantName: PARTICIPANT_NAME, conversationType: CONVERSATION_TYPE }),
      });
      const data = await r.json();
      voiceSessionId = data.sessionId || null;
      finalizePromise = null;
      if (!data.aiReady) log('Note: AI co-pilot is not configured on the server.');
      await flushPendingTranscripts();
      return data;
    } catch (e) { log('session/start error: ' + (e?.message || e)); return null; }
  })();
}

/* Interim recognition is used only to pre-compute the semantic nudge. It is
   never stored as transcript evidence and never posts to Teams before Azure
   Speech emits the authoritative final utterance. */
function scheduleTranscriptPreview(text) {
  if (PARTICIPANT_ROLE !== 'customer' || !micOn || !voiceSessionId) return;
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (clean.length < 24 || clean.split(/\s+/).length < 5) return;
  latestPreviewText = clean;
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    const candidate = latestPreviewText;
    if (!candidate || (candidate === lastPreviewSent) || Math.abs(candidate.length - lastPreviewSent.length) < 7) return;
    lastPreviewSent = candidate;
    void fetch('/transcript/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
      body: JSON.stringify({ text: candidate, turnId: nextTurnId + 1, role: PARTICIPANT_ROLE, sessionId: voiceSessionId }),
    }).catch(() => {});
  }, 280);
}

/* customer-side transcription via Azure Speech (robust; reuses the call mic, no Web Speech) */
async function startRecognition() {
  try {
    const r = await fetch('/speech/token');
    if (!r.ok) { log('Speech token unavailable — transcription off. (Use ?debug=1 to simulate.)'); return; }
    const { token, region } = await r.json();

    const speechConfig = SpeechSDK.SpeechConfig.fromAuthorizationToken(token, region);
    speechConfig.speechRecognitionLanguage = 'en-IN';
    // End an utterance promptly after natural conversational pauses. This reduces
    // the time before the final transcript reaches the AI while preserving enough
    // silence for Indian-English sentence boundaries. Guard for older SDK builds.
    try {
      const pid = SpeechSDK.PropertyId?.Speech_SegmentationSilenceTimeoutMs;
      if (pid != null) speechConfig.setProperty(pid, '400');
    } catch (_) {}
    const audioConfig = SpeechSDK.AudioConfig.fromDefaultMicrophoneInput();
    recognizer = new SpeechSDK.SpeechRecognizer(speechConfig, audioConfig);

    recognizer.recognizing = (_s, e) => {
      const t = (e.result?.text || '').trim();
      if (t) scheduleTranscriptPreview(t);
    };
    recognizer.recognized = (_s, e) => {
      if (e.result.reason === SpeechSDK.ResultReason.RecognizedSpeech) {
        const t = (e.result.text || '').trim();
        if (t && micOn) {
          if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
          latestPreviewText = t;
          log('Heard: ' + t); sendTranscript(t);
        }
        else if (t) log('Ignored speech while customer microphone is muted.');
      }
    };
    recognizer.canceled = (_s, e) => log('Speech canceled: ' + e.errorDetails);

    recognizer.startContinuousRecognitionAsync(
      () => { recognizing = true; log('Listening (Azure Speech).'); },
      (err) => log('Speech start failed: ' + err)
    );

    // refresh the auth token every 8 minutes (tokens last ~10)
    tokenTimer = setInterval(async () => {
      try { const rr = await fetch('/speech/token'); const d = await rr.json(); if (recognizer && d.token) recognizer.authorizationToken = d.token; } catch (_) {}
    }, 8 * 60 * 1000);
  } catch (e) { log('Transcription error: ' + (e?.message || e)); }
}
function stopRecognition() {
  recognizing = false;
  if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
  latestPreviewText = ''; lastPreviewSent = '';
  if (tokenTimer) { clearInterval(tokenTimer); tokenTimer = null; }
  if (recognizer) { try { recognizer.stopContinuousRecognitionAsync(() => { try { recognizer.close(); } catch (_) {} recognizer = null; }); } catch (_) { recognizer = null; } }
}

async function postTranscript(item) {
  try {
    const r = await fetch('/transcript', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
      body: JSON.stringify({ text: item.text, turnId: item.turnId, finalizedAt: item.finalizedAt, role: PARTICIPANT_ROLE, source: `azure_speech_${PARTICIPANT_ROLE}_mic`, sessionId: voiceSessionId }),
    });
    if (!r.ok) log(`transcript turn ${item.turnId}: server returned ${r.status}`);
  } catch (e) { log('transcript: ' + (e?.message || e)); }
}

async function flushPendingTranscripts() {
  if (!voiceSessionId || !pendingTranscripts.length) return;
  const items = pendingTranscripts.splice(0);
  // Preserve customer turn order after a very fast first utterance.
  for (const item of items) await postTranscript(item);
}

async function sendTranscript(text) {
  // This app captures the joining participant's mic. Customer speech can trigger
  // live nudges; branch-manager speech is stored as internal call evidence.
  const item = { text, turnId: ++nextTurnId, finalizedAt: new Date().toISOString() };
  if (!voiceSessionId) {
    pendingTranscripts.push(item);
    log(`Buffered transcript turn ${item.turnId} while the call context is prepared.`);
    return;
  }
  await postTranscript(item);
}

/* Formal case registration is owned by the consent-gated server workflow.
   The customer UI deliberately exposes no button that can bypass the RM permission
   question or the later explicit-consent turn. */

/* hidden debug simulate (only with ?debug=1) */
if (QUERY.get('debug') === '1') {
  const p = $('debugPanel'); if (p) p.hidden = false;
  const send = () => { const t = $('simInput').value.trim(); if (!t) return; log('Sim: ' + t); sendTranscript(t); $('simInput').value = ''; };
  $('simBtn').onclick = send;
  $('simInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
}


async function finalizeCall() {
  if (!sessionStarted && !voiceSessionId) return null;
  if (finalizePromise) return finalizePromise;
  finalizePromise = (async () => {
    try {
      const r = await fetch('/session/finalize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: voiceSessionId }),
      });
      const d = await r.json().catch(() => ({}));
      if (d.ok) log('Call transcript saved to CRM: ' + (d.record?.record_id || 'ready'));
      else log('Transcript save: ' + (d.error || 'failed'));
      return d;
    } catch (e) { log('Transcript save error: ' + (e?.message || e)); return null; }
  })();
  return finalizePromise;
}

// Best-effort finalisation if the customer closes the tab immediately after a call.
window.addEventListener('pagehide', () => {
  if (!sessionStarted && !voiceSessionId) return;
  try {
    const blob = new Blob([JSON.stringify({ sessionId: voiceSessionId })], { type: 'application/json' });
    navigator.sendBeacon('/session/finalize', blob);
  } catch (_) {}
});

/* controls */
$('micBtn').onclick = async () => { if (!call) return; try { if (call.isMuted) { await call.unmute(); micOn = true; } else { await call.mute(); micOn = false; } reflectControls(); } catch (e) { log('Mic: ' + (e?.message || e)); } };
$('camBtn').onclick = async () => { if (!call || !localVideoStream) return; try { if (camOn) { await call.stopVideo(localVideoStream); camOn = false; hideLocal(); } else { await call.startVideo(localVideoStream); camOn = true; await showLocal(localVideoStream); } reflectControls(); } catch (e) { log('Camera: ' + (e?.message || e)); } };
$('leaveBtn').onclick = async () => {
  try { await Promise.allSettled([call?.hangUp(), finalizeCall()]); }
  catch (e) { log(e?.message || e); }
  endCleanup();
};
function reflectControls() {
  const mic = $('micBtn'), cam = $('camBtn');
  mic.dataset.on = micOn ? 'true' : 'false'; mic.querySelector('.label').textContent = micOn ? 'Mute' : 'Unmute'; mic.setAttribute('aria-pressed', String(!micOn));
  cam.dataset.on = camOn ? 'true' : 'false'; cam.querySelector('.label').textContent = camOn ? 'Stop video' : 'Start video'; cam.setAttribute('aria-pressed', String(!camOn));
}

/* video */
async function showLocal(stream) { if (localRenderer) { localRenderer.dispose(); localRenderer = null; } localRenderer = new VideoStreamRenderer(stream); const v = await localRenderer.createView({ scalingMode: 'Crop' }); const f = $('localVideo'); f.innerHTML = ''; f.appendChild(v.target); f.classList.add('has-video'); }
function hideLocal() { if (localRenderer) { localRenderer.dispose(); localRenderer = null; } const f = $('localVideo'); f.innerHTML = '<span class="ph">Camera off</span>'; f.classList.remove('has-video'); }
function subscribe(p) {
  p.on('videoStreamsUpdated', (e) => { e.added.forEach(renderRemote); e.removed.forEach(unrender); });
  p.videoStreams.forEach(renderRemote);
  // Real speaking state from ACS — not a timer, not a guess.
  try {
    const reflect = () => {
      const f = $('remoteVideo');
      if (f) f.classList.toggle('rx-speaking', !!p.isSpeaking && !p.isMuted);
    };
    p.on('isSpeakingChanged', reflect);
    p.on('isMutedChanged', reflect);
    reflect();
  } catch (e) { log('Remote speaking indicator unavailable: ' + (e?.message || e)); }
}
async function renderRemote(stream) {
  if (!stream.isAvailable) { stream.on('isAvailableChanged', () => { if (stream.isAvailable) renderRemote(stream); }); return; }
  if (remoteRenderers.has(stream)) return;
  const r = new VideoStreamRenderer(stream); const v = await r.createView({ scalingMode: 'Fit' });
  remoteRenderers.set(stream, r); const f = $('remoteVideo'); f.innerHTML = ''; f.appendChild(v.target); f.classList.add('has-video'); log('RM video connected.');
}
function unrender(stream) { const r = remoteRenderers.get(stream); if (r) { r.dispose(); remoteRenderers.delete(stream); } if (remoteRenderers.size === 0) resetRemoteTile(); }

function endCleanup() {
  stopRecognition(); stopLocalSpeakingMeter(); sessionStarted = false;
  remoteRenderers.forEach((r) => r.dispose()); remoteRenderers.clear();
  if (localRenderer) { localRenderer.dispose(); localRenderer = null; }
  $('localVideo').innerHTML = '<span class="ph">Camera preview</span>'; $('localVideo').classList.remove('has-video');
  resetRemoteTile();
  setPhase('idle'); setLoading(false); call = null;
}
function flashInput(msg) { const i = $('meetingLink'); i.classList.add('shake'); log(msg); setTimeout(() => i.classList.remove('shake'), 450); }
