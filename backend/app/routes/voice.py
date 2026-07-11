"""
backend/app/routes/voice.py

Live-call copilot endpoints.
  POST /v1/voice/sessions          -> create a two-device RM/customer session
  GET  /v1/voice/sessions/{sid}    -> read shared session state
  POST /v1/voice/sessions/{sid}/join -> participant joins as rm/customer
  POST /v1/voice/sessions/{sid}/transcript -> role-tagged transcript + RM-only nudges
  POST /v1/voice/sessions/{sid}/signal -> WebRTC signaling envelope
  GET  /v1/voice/sessions/{sid}/signals -> WebRTC signal polling
  GET  /v1/voice/sessions/{sid}/events -> room event polling
  POST /v1/voice/ticket            -> single-use ticket bound to a customer (bearer)
  WS   /v1/voice/stream?ticket=..  -> browser audio <-> Voice Live silent copilot
  GET  /v1/voice/nudges/{sid}      -> poll fallback for queued nudges (bearer)
  POST /v1/voice/simulate          -> inject transcript text to fire nudges WITHOUT a mic
                                      (lets you test the full nudge path from a script)
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, WebSocket, HTTPException, Query
from pydantic import BaseModel
import secrets
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

from app.deps import require_bearer, get_store
from app.store import DataStore
from app.services.voice_copilot import (
    issue_ticket, consume_ticket, copilot_session, drain_nudges,
)
from app.services.nudge_engine import NudgeEngine
from app.services.fact_extractor import extract_facts
from app.services.call_wrapup import build_wrapup

router = APIRouter(prefix="/v1/voice", tags=["voice"])
ws_router = APIRouter()   # WS cannot use the bearer Header dependency; ticket-auth instead



# ---------------------------------------------------------------------------
# Two-device POC session room
# ---------------------------------------------------------------------------
# This is intentionally in-memory for the POC. The shape is the production swap
# seam: move these dictionaries to Redis / Azure Web PubSub / SignalR / Cosmos DB
# later without changing the browser contract.
_SESSION_LOCK = Lock()
_SESSIONS: dict[str, dict] = {}
_SESSION_EVENTS: dict[str, list[dict]] = {}
_SESSION_SIGNALS: dict[str, list[dict]] = {}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _new_session_id() -> str:
    return 'CALL-' + secrets.token_hex(4).upper()


def _opposite(role: str) -> str:
    return 'customer' if role == 'rm' else 'rm'


def _assert_role(role: str) -> str:
    if role not in {'rm', 'customer'}:
        raise HTTPException(422, 'role must be rm or customer')
    return role


def _get_session(session_id: str) -> dict:
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, 'Session not found')
    return sess


def _event(session_id: str, event_type: str, payload: dict, target: str = 'both') -> dict:
    with _SESSION_LOCK:
        events = _SESSION_EVENTS.setdefault(session_id, [])
        ev = {
            'seq': len(events),
            'session_id': session_id,
            'type': event_type,
            'target': target,
            'timestamp': _utc(),
            'payload': payload,
        }
        events.append(ev)
        return ev


def _session_public(session_id: str) -> dict:
    sess = _get_session(session_id)
    # Do not leak RM-only state (nudges, transcripts, captured facts, post-call
    # summaries) through the generic session object. Role-targeted events carry
    # that data only to the RM screen.
    hidden = {'internal', 'transcripts', 'captured_facts', 'fired_nudges', 'fired_intents',
              'document_requests', 'uploaded_documents', 'post_call_summary'}
    return {k: v for k, v in sess.items() if k not in hidden}


class CreateSessionBody(BaseModel):
    customer_id: str
    rm_id: str = 'RM-1042'


class JoinSessionBody(BaseModel):
    role: str
    display_name: str | None = None


class MuteBody(BaseModel):
    role: str
    muted: bool


class AiListenerBody(BaseModel):
    active: bool


class TranscriptBody(BaseModel):
    role: str
    text: str
    source: str = 'voice_live'


class SignalBody(BaseModel):
    role: str
    kind: str
    payload: dict


class DocumentRequestBody(BaseModel):
    role: str = 'rm'
    items: list[str]
    note: str | None = None


class DocumentUploadBody(BaseModel):
    role: str = 'customer'
    document_type: str
    filename: str | None = None
    note: str | None = None


class EndSessionBody(BaseModel):
    role: str = 'rm'


@router.post('/sessions', dependencies=[Depends(require_bearer)])
def create_voice_session(body: CreateSessionBody, store: DataStore = Depends(get_store)):
    cust = store.one('customer_master', customer_id=body.customer_id)
    if not cust:
        raise HTTPException(404, 'Customer not found')
    session_id = _new_session_id()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace('+00:00', 'Z')
    sess = {
        'session_id': session_id,
        'customer_id': body.customer_id,
        'customer_name': cust.get('display_name') or cust.get('legal_name') or body.customer_id,
        'rm_id': body.rm_id,
        'status': 'created',
        'created_at': _utc(),
        'expires_at': expires_at,
        'ai_listener_active': True,
        'participants': {
            'rm': {'joined': False, 'muted': False, 'display_name': 'Relationship Manager'},
            'customer': {'joined': False, 'muted': False, 'display_name': cust.get('display_name') or 'Customer'},
        },
        'counts': {'transcript': 0, 'nudges': 0, 'documents_requested': 0, 'facts_captured': 0},
        'transcripts': [],
        'captured_facts': [],
        'fired_nudges': [],
        'fired_intents': [],
        'offers': [],
        'document_requests': [],
        'uploaded_documents': [],
    }
    with _SESSION_LOCK:
        _SESSIONS[session_id] = sess
        _SESSION_EVENTS[session_id] = []
        _SESSION_SIGNALS[session_id] = []
    _event(session_id, 'session.created', {'customer_id': body.customer_id, 'customer_name': sess['customer_name']}, 'both')
    store.add_event('voice.session_room_created', {'session_id': session_id, 'customer_id': body.customer_id})
    return _session_public(session_id)


@router.get('/sessions/{session_id}', dependencies=[Depends(require_bearer)])
def get_voice_session(session_id: str):
    return _session_public(session_id)


@router.post('/sessions/{session_id}/join', dependencies=[Depends(require_bearer)])
def join_voice_session(session_id: str, body: JoinSessionBody, store: DataStore = Depends(get_store)):
    role = _assert_role(body.role)
    sess = _get_session(session_id)
    with _SESSION_LOCK:
        sess['participants'][role]['joined'] = True
        sess['participants'][role]['last_seen_at'] = _utc()
        if body.display_name:
            sess['participants'][role]['display_name'] = body.display_name
        if sess['status'] in {'created', 'ended'}:
            sess['status'] = 'live'
    _event(session_id, 'participant.joined', {'role': role, 'display_name': sess['participants'][role]['display_name']}, 'both')
    store.add_event('voice.participant_joined', {'session_id': session_id, 'role': role, 'customer_id': sess['customer_id']})
    return _session_public(session_id)


@router.post('/sessions/{session_id}/mute', dependencies=[Depends(require_bearer)])
def set_mute(session_id: str, body: MuteBody, store: DataStore = Depends(get_store)):
    role = _assert_role(body.role)
    sess = _get_session(session_id)
    with _SESSION_LOCK:
        sess['participants'][role]['muted'] = bool(body.muted)
        sess['participants'][role]['last_seen_at'] = _utc()
    _event(session_id, 'participant.mute_changed', {'role': role, 'muted': body.muted}, 'both')
    store.add_event('voice.mute_changed', {'session_id': session_id, 'role': role, 'muted': body.muted})
    return _session_public(session_id)


@router.post('/sessions/{session_id}/ai-listener', dependencies=[Depends(require_bearer)])
def set_ai_listener(session_id: str, body: AiListenerBody, store: DataStore = Depends(get_store)):
    sess = _get_session(session_id)
    with _SESSION_LOCK:
        sess['ai_listener_active'] = bool(body.active)
    _event(session_id, 'ai_listener.changed', {'active': sess['ai_listener_active']}, 'both')
    store.add_event('voice.ai_listener_changed', {'session_id': session_id, 'active': sess['ai_listener_active']})
    return _session_public(session_id)


@router.post('/sessions/{session_id}/transcript', dependencies=[Depends(require_bearer)])
def push_session_transcript(session_id: str, body: TranscriptBody, store: DataStore = Depends(get_store)):
    role = _assert_role(body.role)
    text = (body.text or '').strip()
    if not text:
        return {'session_id': session_id, 'events': []}
    sess = _get_session(session_id)
    sess['counts']['transcript'] += 1
    tr = {
        'role': role,
        'text': text,
        'source': body.source,
        'transcript_no': sess['counts']['transcript'],
        'timestamp': _utc(),
    }
    sess.setdefault('transcripts', []).append(tr)
    events = [_event(session_id, 'transcript.final', tr, 'both')]
    store.add_event('voice.transcript_final', {'session_id': session_id, 'role': role, 'text': text[:160], 'customer_id': sess['customer_id']})

    # Live fact capture is shown only to the RM. Facts are candidates; RM validates
    # before CRM writeback. This keeps the POC transparent and human-in-loop.
    facts = extract_facts(text, role) if sess.get('ai_listener_active', True) else []
    if facts:
        for f in facts:
            f['speaker_role'] = role
            f['fact_id'] = 'FACT-' + secrets.token_hex(3)
        sess.setdefault('captured_facts', []).extend(facts)
        sess['counts']['facts_captured'] = len(sess.get('captured_facts', []))
        events.append(_event(session_id, 'facts.captured', {'facts': facts, 'source_transcript_no': tr['transcript_no']}, 'rm'))
        store.add_event('voice.facts_captured', {'session_id': session_id, 'count': len(facts), 'customer_id': sess['customer_id']})

    # RM-only nudges are fired from customer utterances. RM utterances still appear
    # in the transcript and post-call audit, but do not spam the RM with advice.
    if role == 'customer' and sess.get('ai_listener_active', True):
        engine = NudgeEngine(store, sess['customer_id'])
        nudges = engine.detect(text)
        for n in nudges:
            # Session-level de-dupe by intent keeps the call from spamming the RM.
            intent_key = n.get('intent') or n.get('nudge_type') or n.get('nudge_text','')[:60]
            if intent_key in sess.setdefault('fired_intents', []):
                continue
            sess['fired_intents'].append(intent_key)
            n['session_id'] = session_id
            n['customer_id'] = sess['customer_id']
            n['nudge_id'] = 'SN-' + secrets.token_hex(4)
            n['next_best_question'] = n.get('recommended_next_utterance')
            n.setdefault('what_not_to_say', 'Do not promise approval, pricing, sanction, or exception handling on this call.')
            sess['counts']['nudges'] += 1
            sess.setdefault('fired_nudges', []).append(n)
            events.append(_event(session_id, 'nudge.fired', n, 'rm'))
            store.add_event('voice.session_nudge_fired', {'session_id': session_id, 'intent': n.get('intent'), 'priority': n.get('priority'), 'customer_id': sess['customer_id']})
    return {'session_id': session_id, 'events': events, 'session': _session_public(session_id)}


@router.get('/sessions/{session_id}/events', dependencies=[Depends(require_bearer)])
def poll_session_events(session_id: str, role: str = Query('rm'), after: int = Query(-1)):
    role = _assert_role(role)
    _get_session(session_id)
    events = _SESSION_EVENTS.get(session_id, [])
    allowed = []
    for ev in events:
        if ev['seq'] <= after:
            continue
        if ev.get('target') in ('both', role):
            allowed.append(ev)
    return {'session': _session_public(session_id), 'events': allowed}


@router.post('/sessions/{session_id}/signal', dependencies=[Depends(require_bearer)])
def post_webrtc_signal(session_id: str, body: SignalBody):
    role = _assert_role(body.role)
    _get_session(session_id)
    if body.kind not in {'offer', 'answer', 'candidate', 'renegotiate'}:
        raise HTTPException(422, 'Unsupported signal kind')
    with _SESSION_LOCK:
        signals = _SESSION_SIGNALS.setdefault(session_id, [])
        sig = {
            'seq': len(signals),
            'session_id': session_id,
            'from': role,
            'to': _opposite(role),
            'kind': body.kind,
            'payload': body.payload,
            'timestamp': _utc(),
        }
        signals.append(sig)
    return sig


@router.get('/sessions/{session_id}/signals', dependencies=[Depends(require_bearer)])
def poll_webrtc_signals(session_id: str, role: str = Query(...), after: int = Query(-1)):
    role = _assert_role(role)
    _get_session(session_id)
    signals = [s for s in _SESSION_SIGNALS.get(session_id, []) if s['to'] == role and s['seq'] > after]
    return {'signals': signals}


@router.post('/sessions/{session_id}/document-request', dependencies=[Depends(require_bearer)])
def request_documents(session_id: str, body: DocumentRequestBody, store: DataStore = Depends(get_store)):
    role = _assert_role(body.role)
    if role != 'rm':
        raise HTTPException(403, 'Only RM can request documents')
    sess = _get_session(session_id)
    items = [i.strip() for i in body.items if i.strip()]
    if not items:
        raise HTTPException(422, 'At least one document item is required')
    sess['counts']['documents_requested'] += len(items)
    payload = {'items': items, 'note': body.note or '', 'requested_by': role, 'customer_id': sess['customer_id'], 'requested_at': _utc()}
    sess.setdefault('document_requests', []).append(payload)
    ev = _event(session_id, 'documents.requested', payload, 'customer')
    store.propose_write({
        'customer_id': sess['customer_id'],
        'type': 'task',
        'payload': {
            'source': 'live_call_document_request',
            'subject': 'Collect live-call requested MSME documents',
            'summary': 'RM requested: ' + ', '.join(items),
            'priority': 'High',
            'owner': sess.get('rm_id', 'RM-1042'),
            'due_date': (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat(),
        },
        'evidence_refs': ['voice.session', 'documents.requested'],
    })
    store.add_event('voice.documents_requested', {'session_id': session_id, 'items': items, 'customer_id': sess['customer_id']})
    return {'event': ev, 'session': _session_public(session_id)}


@router.post('/sessions/{session_id}/document-upload', dependencies=[Depends(require_bearer)])
def upload_document_mock(session_id: str, body: DocumentUploadBody, store: DataStore = Depends(get_store)):
    role = _assert_role(body.role)
    if role != 'customer':
        raise HTTPException(403, 'Only customer can use the mock upload flow')
    sess = _get_session(session_id)
    doc = {
        'document_type': body.document_type.strip(),
        'filename': body.filename or (body.document_type.strip().replace(' ', '_') + '.pdf'),
        'note': body.note or '',
        'uploaded_by': role,
        'uploaded_at': _utc(),
        'status': 'received_pending_verification',
    }
    sess.setdefault('uploaded_documents', []).append(doc)
    ev = _event(session_id, 'documents.uploaded', doc, 'rm')
    store.propose_write({
        'customer_id': sess['customer_id'],
        'type': 'task',
        'payload': {
            'source': 'customer_mock_upload',
            'subject': 'Verify customer-uploaded document',
            'summary': f"Customer uploaded {doc['document_type']} ({doc['filename']}); verify before marking complete.",
            'priority': 'High',
            'owner': sess.get('rm_id', 'RM-1042'),
            'due_date': (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(),
        },
        'evidence_refs': ['voice.session', 'documents.uploaded'],
    })
    store.add_event('voice.document_uploaded', {'session_id': session_id, 'document_type': doc['document_type'], 'customer_id': sess['customer_id']})
    return {'event': ev, 'session': _session_public(session_id)}


@router.post('/sessions/{session_id}/wrap-up', dependencies=[Depends(require_bearer)])
def call_wrapup(session_id: str, store: DataStore = Depends(get_store)):
    sess = _get_session(session_id)
    wrap = build_wrapup(store, sess)
    sess['post_call_summary'] = wrap
    ev = _event(session_id, 'wrapup.generated', wrap, 'rm')
    cand = store.propose_write({
        'customer_id': sess['customer_id'],
        'type': 'note',
        'payload': {
            'source': 'post_call_wrapup',
            'subject': 'AI-assisted live-call wrap-up',
            'summary': wrap.get('crm_note_draft', ''),
            'channel': 'Live call',
            'sentiment': 'Neutral',
            'next_follow_up_date': (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat(),
        },
        'evidence_refs': wrap.get('evidence_refs', []),
    })
    store.add_event('voice.wrapup_generated', {'session_id': session_id, 'customer_id': sess['customer_id'], 'candidate_id': cand.get('candidate_id')})
    return {'event': ev, 'wrapup': wrap, 'crm_candidate': cand, 'session': _session_public(session_id)}


@router.post('/sessions/{session_id}/end', dependencies=[Depends(require_bearer)])
def end_voice_session(session_id: str, body: EndSessionBody, store: DataStore = Depends(get_store)):
    role = _assert_role(body.role)
    sess = _get_session(session_id)
    with _SESSION_LOCK:
        sess['status'] = 'ended'
        sess['ended_at'] = _utc()
    _event(session_id, 'session.ended', {'ended_by': role}, 'both')
    store.add_event('voice.session_room_ended', {'session_id': session_id, 'ended_by': role, 'customer_id': sess['customer_id']})
    return _session_public(session_id)


class TicketBody(BaseModel):
    customer_id: str


@router.post("/ticket", dependencies=[Depends(require_bearer)])
def voice_ticket(body: TicketBody, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=body.customer_id):
        raise HTTPException(404, "Customer not found")
    return {"ticket": issue_ticket(body.customer_id), "customer_id": body.customer_id}


@router.get("/nudges/{session_id}", dependencies=[Depends(require_bearer)])
def poll_nudges(session_id: str):
    return {"session_id": session_id, "nudges": drain_nudges(session_id)}


class SimulateBody(BaseModel):
    customer_id: str
    transcript: list[str]      # ordered customer/RM utterances to replay


@router.post("/simulate", dependencies=[Depends(require_bearer)])
def simulate(body: SimulateBody, store: DataStore = Depends(get_store)):
    """Fire the nudge engine over a scripted transcript — no mic/Voice Live needed.
    Returns the nudges each line would have produced live. Useful for testing and
    as a fallback demo path if audio is unavailable."""
    if not store.one("customer_master", customer_id=body.customer_id):
        raise HTTPException(404, "Customer not found")
    engine = NudgeEngine(store, body.customer_id)
    out = []
    for line in body.transcript:
        nudges = engine.detect(line)
        for n in nudges:
            n.setdefault("nudge_id", "SIM-" + secrets.token_hex(4))
        out.append({"utterance": line, "nudges": nudges})
        for n in nudges:
            store.add_event("voice.nudge_simulated", {"intent": n["intent"], "priority": n["priority"], "customer_id": body.customer_id})
    return {"customer_id": body.customer_id, "results": out}


@ws_router.websocket("/v1/voice/stream")
async def voice_stream(websocket: WebSocket):
    ticket = websocket.query_params.get("ticket")
    ok, customer_id = consume_ticket(ticket or "")
    if not ok or not customer_id:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    store: DataStore = websocket.app.state.store
    await copilot_session(websocket, store, customer_id)
