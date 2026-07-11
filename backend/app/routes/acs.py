"""
backend/app/routes/acs.py

Lean ACS phone-call mode for the RM Assist POC.

Purpose:
  ACS owns the human phone call. RM Assist only consumes real-time transcript
  events and pushes RM-only nudges to the dashboard.

Core endpoints:
  POST /v1/acs/calls/start                 Create a session and optionally dial RM + customer
  POST /v1/acs/events/{session_id}         ACS Call Automation callback sink (no bearer)
  WS   /v1/acs/transcription/{session_id}  ACS real-time transcription WebSocket sink
  GET  /v1/acs/sessions/{session_id}       Dashboard state
  GET  /v1/acs/sessions/{session_id}/events Poll dashboard events
  POST /v1/acs/sessions/{session_id}/transcript Inject transcript for demo/testing
  POST /v1/acs/sessions/{session_id}/wrap-up Post-call summary
  POST /v1/acs/sessions/{session_id}/dial-customer Manually/retry customer leg
  POST /v1/acs/sessions/{session_id}/start-transcription Manually/retry transcription
  POST /v1/acs/sessions/{session_id}/end   End ACS call/session

This is intentionally in-memory for the POC. Persist sessions/events in Redis,
Cosmos DB, or Event Hubs for a pilot.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel

from app.config import get_settings
from app.deps import get_store, require_bearer
from app.store import DataStore
from app.services.fact_extractor import extract_facts
from app.services.nudge_engine import NudgeEngine
from app.services.call_wrapup import build_wrapup

logger = logging.getLogger("contoso.msme.acs")

router = APIRouter(prefix="/v1/acs", tags=["acs-phone-call"])
ws_router = APIRouter()

_LOCK = Lock()
_SESSIONS: dict[str, dict] = {}
_EVENTS: dict[str, list[dict]] = {}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_session_id() -> str:
    return "ACS-" + secrets.token_hex(4).upper()


def _get_session(session_id: str) -> dict:
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "ACS session not found")
    return sess


def _event(session_id: str, event_type: str, payload: dict, target: str = "rm") -> dict:
    with _LOCK:
        events = _EVENTS.setdefault(session_id, [])
        ev = {
            "seq": len(events),
            "session_id": session_id,
            "type": event_type,
            "target": target,
            "timestamp": _utc(),
            "payload": payload,
        }
        events.append(ev)
        return ev


def _public_session(session_id: str) -> dict:
    sess = _get_session(session_id)
    hidden = {"transcripts", "captured_facts", "fired_nudges", "fired_intents", "raw_acs_events"}
    return {k: v for k, v in sess.items() if k not in hidden}


def _digits(s: str | None) -> str:
    return re.sub(r"\D+", "", s or "")


def _normalize_phone(s: str | None) -> str:
    if not s:
        return ""
    s = s.strip()
    if not s:
        return ""
    if s.startswith("+"):
        return s
    d = _digits(s)
    return "+" + d if d else ""


def _phone_matches(configured: str | None, observed: str | None) -> bool:
    """Compare E.164-ish numbers from ACS callbacks and dashboard input."""
    cd = _digits(configured)
    od = _digits(observed)
    if not cd or not od:
        return False
    return cd == od or cd.endswith(od) or od.endswith(cd)


def _participant_phones(data: dict) -> list[str]:
    phones: list[str] = []
    for p in data.get("participants") or []:
        ident = p.get("identifier") or {}
        raw = ident.get("rawId") or ident.get("raw_id") or ""
        phone = None
        if isinstance(ident.get("phoneNumber"), dict):
            phone = ident.get("phoneNumber", {}).get("value")
        if isinstance(ident.get("phone_number"), dict):
            phone = ident.get("phone_number", {}).get("value")
        # ACS PSTN participants usually appear as rawId "4:+<E164>". Do not
        # treat ACS communicationUser raw IDs like "8:acs:..." as phone numbers.
        if not phone and raw and str(raw).startswith("4:+"):
            phone = str(raw).split("4:", 1)[1]
        if not phone:
            continue
        norm = _normalize_phone(phone)
        if norm and _digits(norm):
            phones.append(norm)
    # Preserve order, remove duplicates.
    out: list[str] = []
    for phone in phones:
        if phone not in out:
            out.append(phone)
    return out


def _participant_flags(sess: dict, data: dict) -> dict:
    phones = _participant_phones(data)
    rm_seen = any(_phone_matches(sess.get("rm_phone"), p) for p in phones)
    customer_seen = any(_phone_matches(sess.get("customer_phone"), p) for p in phones)
    return {"phones": phones, "rm_seen": rm_seen, "customer_seen": customer_seen}


def _external_base_url(request: Request) -> str:
    settings = get_settings()
    configured = (settings.acs_public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    # Container Apps terminates TLS before the app. The external URL is HTTPS.
    base = str(request.base_url).rstrip("/")
    return re.sub(r"^http://", "https://", base)


def _ws_base_from_http(http_base: str) -> str:
    return re.sub(r"^https://", "wss://", re.sub(r"^http://", "ws://", http_base.rstrip("/")))



def _normalize_cognitive_endpoint(endpoint: str | None) -> str:
    """Return an ACS Call Intelligence compatible Azure AI Services endpoint.

    ACS Call Automation real-time transcription expects the Cognitive Services
    custom subdomain endpoint at call setup. Foundry/OpenAI endpoints such as
    *.services.ai.azure.com/openai/v1 are valid for model calls, but ACS may
    not treat them as call intelligence configuration.
    """
    ep = (endpoint or "").strip()
    if not ep:
        return ""
    ep = re.sub(r"/openai/v1/?$", "/", ep.rstrip("/"))
    ep = re.sub(r"/api/projects/[^/]+/?$", "/", ep.rstrip("/"))
    ep = ep.rstrip("/") + "/"
    m = re.match(r"^https://([^.]+)\.services\.ai\.azure\.com/?$", ep)
    if m:
        ep = f"https://{m.group(1)}.cognitiveservices.azure.com/"
    return ep


def _acs_client():
    settings = get_settings()
    if not settings.acs_endpoint and not settings.acs_connection_string:
        raise HTTPException(500, "ACS endpoint not configured. Re-run Phase 2/4 or set ACS_ENDPOINT.")
    try:
        from azure.communication.callautomation import CallAutomationClient
        from azure.identity import DefaultAzureCredential
    except Exception as exc:  # pragma: no cover - container dependency issue
        raise HTTPException(500, f"ACS Call Automation SDK not installed: {exc}") from exc
    if settings.acs_connection_string:
        return CallAutomationClient.from_connection_string(settings.acs_connection_string)
    client_id = os.getenv("AZURE_CLIENT_ID") or None
    credential = DefaultAzureCredential(managed_identity_client_id=client_id) if client_id else DefaultAzureCredential()
    return CallAutomationClient(settings.acs_endpoint, credential)


def _start_acs_call(sess: dict, request: Request) -> dict:
    settings = get_settings()
    rm_phone = sess.get("rm_phone")
    customer_phone = sess.get("customer_phone")
    caller_number = sess.get("caller_number")
    if not rm_phone or not customer_phone or not caller_number:
        raise HTTPException(422, "rm_phone, customer_phone and caller_number are required for ACS PSTN dialing.")

    from azure.communication.callautomation import (
        PhoneNumberIdentifier,
        StreamingTransportType,
        TranscriptionOptions,
    )

    client = _acs_client()
    http_base = _external_base_url(request)
    ws_base = _ws_base_from_http(http_base)
    callback_url = f"{http_base}/v1/acs/events/{sess['session_id']}"
    transcription_url = f"{ws_base}/v1/acs/transcription/{sess['session_id']}"
    locale = settings.acs_transcription_locale or "en-US"
    cognitive_endpoint = _normalize_cognitive_endpoint(settings.acs_cognitive_services_endpoint or settings.foundry_endpoint)

    # Start ACS real-time transcription at call setup.
    # In ACS, later start_transcription() can fail with 8522 if the call
    # was not created with Cognitive Services + transcription configuration
    # already attached. Starting at setup is the safest POC path.
    transcription = TranscriptionOptions(
        transport_url=transcription_url,
        transport_type=StreamingTransportType.WEBSOCKET,
        locale=locale,
        start_transcription=True,
        enable_intermediate_results=bool(settings.acs_enable_intermediate_transcripts),
    )

    logger.info("Creating ACS call session=%s rm=%s customer=%s callback=%s transcription=%s",
                sess["session_id"], rm_phone, customer_phone, callback_url, transcription_url)
    kwargs = {
        "target_participant": PhoneNumberIdentifier(rm_phone),
        "callback_url": callback_url,
        "source_caller_id_number": PhoneNumberIdentifier(caller_number),
        "operation_context": f"rm-leg:{sess['session_id']}",
        "transcription": transcription,
    }
    if cognitive_endpoint:
        kwargs["cognitive_services_endpoint"] = cognitive_endpoint
    else:
        logger.warning("ACS cognitive services endpoint is empty; real-time transcription will not start.")

    result = client.create_call(**kwargs)
    call_connection_id = getattr(result, "call_connection_id", None)
    server_call_id = getattr(result, "server_call_id", None)
    with _LOCK:
        sess["status"] = "dialing_rm"
        sess["call_started"] = True
        sess["call_connection_id"] = call_connection_id
        sess["server_call_id"] = server_call_id
        sess["callback_url"] = callback_url
        sess["transcription_url"] = transcription_url
        sess["transcription_locale"] = locale
        sess["cognitive_services_endpoint"] = cognitive_endpoint
        sess["transcription_status"] = "starting"
        sess["transcription_start_requested"] = True
        sess["transcription_configured_at_setup"] = bool(cognitive_endpoint)
    _event(sess["session_id"], "acs.call.created", {
        "call_connection_id": call_connection_id,
        "server_call_id": server_call_id,
        "callback_url": callback_url,
        "transcription_url": transcription_url,
        "cognitive_services_endpoint": cognitive_endpoint,
        "transcription_configured_at_setup": bool(cognitive_endpoint),
        "transcription_start_at_setup": True,
        "status": sess["status"],
    })
    return _public_session(sess["session_id"])


def _add_customer_participant(sess: dict, *, force: bool = False, reason: str = "auto") -> dict:
    """Add/retry the customer PSTN leg on the active RM call connection."""
    if not sess.get("call_connection_id"):
        payload = {"error": "No call_connection_id yet; RM leg not established.", "reason": reason}
        _event(sess["session_id"], "acs.customer.add_skipped", payload)
        return payload
    if sess.get("customer_connected") and not force:
        payload = {"status": "already_connected", "customer_phone": sess.get("customer_phone"), "reason": reason}
        _event(sess["session_id"], "acs.customer.add_skipped", payload)
        return payload
    if sess.get("customer_add_requested") and not force:
        payload = {"status": "already_requested", "customer_phone": sess.get("customer_phone"), "reason": reason}
        _event(sess["session_id"], "acs.customer.add_skipped", payload)
        return payload
    from azure.communication.callautomation import PhoneNumberIdentifier
    client = _acs_client()
    call_connection = client.get_call_connection(sess["call_connection_id"])
    call_connection.add_participant(
        PhoneNumberIdentifier(sess["customer_phone"]),
        source_caller_id_number=PhoneNumberIdentifier(sess["caller_number"]),
        operation_context=f"add-customer:{sess['session_id']}:{reason}",
    )
    sess["customer_add_requested"] = True
    sess["customer_leg_status"] = "dialing"
    sess["status"] = "dialing_customer"
    sess.pop("last_customer_add_error", None)
    payload = {
        "customer_phone": sess.get("customer_phone"),
        "status": sess["status"],
        "reason": reason,
        "force": force,
    }
    _event(sess["session_id"], "acs.customer.add_requested", payload)
    return payload


def _start_transcription(sess: dict, *, force: bool = False, reason: str = "auto") -> dict:
    """Start/retry ACS real-time transcription for the active call."""
    if not sess.get("call_connection_id"):
        payload = {"error": "No call_connection_id yet; call not ready.", "reason": reason}
        _event(sess["session_id"], "acs.transcription.start_skipped", payload)
        return payload
    if sess.get("transcription_status") in {"active", "connected", "starting"} and not force:
        payload = {"status": sess.get("transcription_status"), "reason": reason}
        _event(sess["session_id"], "acs.transcription.start_skipped", payload)
        return payload
    client = _acs_client()
    call_connection = client.get_call_connection(sess["call_connection_id"])
    locale = sess.get("transcription_locale") or get_settings().acs_transcription_locale or "en-US"
    callback_url = sess.get("callback_url") or None
    call_connection.start_transcription(
        locale=locale,
        operation_context=f"start-transcription:{sess['session_id']}:{reason}",
        operation_callback_url=callback_url,
    )
    sess["transcription_status"] = "starting"
    sess["transcription_start_requested"] = True
    sess.pop("last_transcription_error", None)
    payload = {"status": "starting", "locale": locale, "reason": reason, "callback_url": callback_url}
    _event(sess["session_id"], "acs.transcription.start_requested", payload)
    return payload


def _extract_event_type(ev: dict) -> str:
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    return str(ev.get("type") or ev.get("eventType") or ev.get("kind") or data.get("eventType") or data.get("kind") or "")


def _extract_data(ev: dict) -> dict:
    data = ev.get("data")
    return data if isinstance(data, dict) else ev


def _speaker_role(sess: dict, participant_raw_id: str | None, text: str = "", raw: dict | None = None) -> str:
    """Classify the speaker. Order of confidence:
      1) participantRawID phone matches the customer's or RM's number -> that role.
      2) ACS speaker diarization index, mapped to a role once we've seen the pairing.
      3) Otherwise UNKNOWN -> caller must NOT fire nudges (we only nudge on lines we
         are confident came from the customer). Defaulting unknown->customer caused
         RM speech to trigger nudges, which is unacceptable."""
    raw_digits = _digits(participant_raw_id)
    if raw_digits:
        cust = _digits(sess.get("customer_phone"))
        rm = _digits(sess.get("rm_phone"))
        if cust and cust in raw_digits:
            return "customer"
        if rm and rm in raw_digits:
            return "rm"

    # Speaker diarization: ACS may send a stable speaker id/index even without a phone.
    spk = None
    if raw:
        spk = raw.get("participantRawID") or raw.get("speaker") or raw.get("speakerId") \
              or raw.get("SpeakerId") or (raw.get("transcriptionData") or {}).get("speaker")
    if spk is not None:
        spk = str(spk)
        seen = sess.setdefault("_speaker_map", {})
        if spk in seen:
            return seen[spk]
        # First speaker we ever hear after the call connects is, by our call flow, the
        # RM (the RM answers first and greets). The next distinct speaker is the
        # customer. This pairing is recorded so it stays stable for the session.
        if not seen:
            seen[spk] = "rm"
            return "rm"
        if "customer" not in seen.values():
            seen[spk] = "customer"
            return "customer"
        # more than two speakers seen -> unknown
        return "unknown"

    # No phone, no diarization -> we cannot attribute. Do NOT assume customer.
    return "unknown"


def _process_transcript(session_id: str, role: str, text: str, store: DataStore, *, source: str = "acs_transcription", confidence: float | None = None, raw: dict | None = None, final: bool = True) -> list[dict]:
    sess = _get_session(session_id)
    text = (text or "").strip()
    if not text:
        return []
    sess.setdefault("counts", {}).setdefault("transcript", 0)
    sess["counts"]["transcript"] += 1
    tr = {
        "role": role,
        "text": text,
        "source": source,
        "confidence": confidence,
        "final": final,
        "transcript_no": sess["counts"]["transcript"],
        "timestamp": _utc(),
    }
    if raw:
        tr["participant_raw_id"] = raw.get("participantRawID") or raw.get("participantRawId")
        tr["result_status"] = raw.get("resultStatus")
    sess.setdefault("transcripts", []).append(tr)
    events = [_event(session_id, "transcript.final" if final else "transcript.partial", tr, "rm")]
    store.add_event("acs.transcript", {"session_id": session_id, "role": role, "text": text[:180], "customer_id": sess.get("customer_id")})

    if final:
        facts = extract_facts(text, role)
        if facts:
            for f in facts:
                f["source"] = source
                f["role"] = role
            sess.setdefault("captured_facts", []).extend(facts)
            sess["counts"]["facts_captured"] = sess["counts"].get("facts_captured", 0) + len(facts)
            events.append(_event(session_id, "facts.captured", {"facts": facts, "transcript": tr}, "rm"))

    if role == "customer" and final:
        engine = NudgeEngine(store, sess["customer_id"])
        nudges = engine.detect(text)
        for n in nudges:
            intent_key = n.get("intent") or n.get("nudge_type") or n.get("nudge_text", "")[:80]
            if intent_key in sess.setdefault("fired_intents", []):
                continue
            sess["fired_intents"].append(intent_key)
            n["session_id"] = session_id
            n["customer_id"] = sess["customer_id"]
            n["nudge_id"] = "ACS-N-" + secrets.token_hex(4)
            n["source"] = source
            n["next_best_question"] = n.get("recommended_next_utterance")
            n.setdefault("what_not_to_say", "Do not promise approval, sanction, pricing, or exception handling on this call.")
            sess["counts"]["nudges"] = sess["counts"].get("nudges", 0) + 1
            sess.setdefault("fired_nudges", []).append(n)
            events.append(_event(session_id, "nudge.fired", n, "rm"))
            store.add_event("acs.nudge_fired", {"session_id": session_id, "intent": n.get("intent"), "priority": n.get("priority"), "customer_id": sess["customer_id"]})
    return events


class StartAcsCallBody(BaseModel):
    customer_id: str
    rm_phone: str | None = None
    customer_phone: str | None = None
    caller_number: str | None = None
    rm_id: str = "RM-1042"
    dial: bool = True


class InjectTranscriptBody(BaseModel):
    role: str = "customer"
    text: str
    source: str = "acs_dashboard_manual"


class EndAcsBody(BaseModel):
    hangup_acs_call: bool = True


@router.get("/config", dependencies=[Depends(require_bearer)])
def acs_config():
    s = get_settings()
    return {
        "acs_endpoint_configured": bool(s.acs_endpoint or s.acs_connection_string),
        "default_caller_number": s.acs_caller_number,
        "default_rm_phone": s.acs_default_rm_phone,
        "default_customer_phone": s.acs_default_customer_phone,
        "locale": s.acs_transcription_locale,
        "intermediate_transcripts": s.acs_enable_intermediate_transcripts,
    }


@router.post("/calls/start", dependencies=[Depends(require_bearer)])
def start_acs_call(body: StartAcsCallBody, request: Request, store: DataStore = Depends(get_store)):
    cust = store.one("customer_master", customer_id=body.customer_id)
    if not cust:
        raise HTTPException(404, "Customer not found")
    settings = get_settings()
    session_id = _new_session_id()
    rm_phone = _normalize_phone(body.rm_phone or settings.acs_default_rm_phone)
    customer_phone = _normalize_phone(body.customer_phone or settings.acs_default_customer_phone)
    caller_number = _normalize_phone(body.caller_number or settings.acs_caller_number)
    sess = {
        "session_id": session_id,
        "mode": "acs_phone_call",
        "customer_id": body.customer_id,
        "customer_name": cust.get("display_name") or cust.get("legal_name") or body.customer_id,
        "rm_id": body.rm_id,
        "rm_phone": rm_phone,
        "customer_phone": customer_phone,
        "caller_number": caller_number,
        "status": "created",
        "call_started": False,
        "created_at": _utc(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        "counts": {"transcript": 0, "nudges": 0, "facts_captured": 0, "acs_events": 0},
        "transcripts": [],
        "captured_facts": [],
        "fired_nudges": [],
        "fired_intents": [],
        "offers": [],
        "raw_acs_events": [],
        "rm_leg_status": "waiting",
        "customer_leg_status": "waiting",
        "transcription_status": "waiting",
        "participant_phones": [],
    }
    with _LOCK:
        _SESSIONS[session_id] = sess
        _EVENTS[session_id] = []
    _event(session_id, "acs.session.created", {
        "customer_id": body.customer_id,
        "customer_name": sess["customer_name"],
        "dial_requested": body.dial,
    })
    store.add_event("acs.session_created", {"session_id": session_id, "customer_id": body.customer_id})

    if body.dial:
        try:
            return {"session": _start_acs_call(sess, request), "dashboard_url": f"/acs/session/{session_id}"}
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("ACS create call failed")
            sess["status"] = "dial_failed"
            sess["last_error"] = str(exc)
            _event(session_id, "acs.call.failed", {"error": str(exc), "status": sess["status"]})
            return {"session": _public_session(session_id), "dashboard_url": f"/acs/session/{session_id}", "warning": str(exc)}
    sess["status"] = "dashboard_only"
    return {"session": _public_session(session_id), "dashboard_url": f"/acs/session/{session_id}"}


@router.get("/sessions/{session_id}", dependencies=[Depends(require_bearer)])
def get_acs_session(session_id: str):
    return _public_session(session_id)


@router.get("/sessions/{session_id}/events", dependencies=[Depends(require_bearer)])
def poll_acs_events(session_id: str, after: int = Query(-1)):
    _get_session(session_id)
    events = [ev for ev in _EVENTS.get(session_id, []) if ev["seq"] > after and ev.get("target") in ("rm", "both")]
    return {"session": _public_session(session_id), "events": events}


@router.post("/sessions/{session_id}/transcript", dependencies=[Depends(require_bearer)])
def inject_transcript(session_id: str, body: InjectTranscriptBody, store: DataStore = Depends(get_store)):
    role = body.role if body.role in {"rm", "customer"} else "customer"
    events = _process_transcript(session_id, role, body.text, store, source=body.source, final=True)
    return {"session": _public_session(session_id), "events": events}


@router.post("/sessions/{session_id}/wrap-up", dependencies=[Depends(require_bearer)])
def acs_wrapup(session_id: str, store: DataStore = Depends(get_store)):
    sess = _get_session(session_id)
    wrap = build_wrapup(store, sess)
    sess["post_call_summary"] = wrap
    ev = _event(session_id, "wrapup.generated", wrap, "rm")
    store.add_event("acs.wrapup_generated", {"session_id": session_id, "customer_id": sess.get("customer_id")})
    return {"event": ev, "wrapup": wrap, "session": _public_session(session_id)}


@router.post("/sessions/{session_id}/dial-customer", dependencies=[Depends(require_bearer)])
def dial_customer_now(session_id: str, store: DataStore = Depends(get_store)):
    sess = _get_session(session_id)
    try:
        payload = _add_customer_participant(sess, force=True, reason="manual-dashboard")
        store.add_event("acs.customer_dial_manual", {"session_id": session_id, "customer_id": sess.get("customer_id"), "payload": payload})
        return {"session": _public_session(session_id), "payload": payload}
    except Exception as exc:
        logger.exception("Manual add customer failed")
        sess["customer_leg_status"] = "failed"
        sess["last_customer_add_error"] = str(exc)
        sess["last_error"] = str(exc)
        _event(session_id, "acs.customer.add_failed", {"error": str(exc), "reason": "manual-dashboard"})
        return {"session": _public_session(session_id), "error": str(exc)}


@router.post("/sessions/{session_id}/start-transcription", dependencies=[Depends(require_bearer)])
def start_transcription_now(session_id: str, store: DataStore = Depends(get_store)):
    sess = _get_session(session_id)
    try:
        payload = _start_transcription(sess, force=True, reason="manual-dashboard")
        store.add_event("acs.transcription_manual", {"session_id": session_id, "customer_id": sess.get("customer_id"), "payload": payload})
        return {"session": _public_session(session_id), "payload": payload}
    except Exception as exc:
        logger.exception("Manual start transcription failed")
        sess["transcription_status"] = "failed"
        sess["last_transcription_error"] = str(exc)
        sess["last_error"] = str(exc)
        _event(session_id, "acs.transcription.start_failed", {"error": str(exc), "reason": "manual-dashboard"})
        return {"session": _public_session(session_id), "error": str(exc)}


@router.post("/sessions/{session_id}/end", dependencies=[Depends(require_bearer)])
def end_acs_session(session_id: str, body: EndAcsBody, store: DataStore = Depends(get_store)):
    sess = _get_session(session_id)
    err = None
    if body.hangup_acs_call and sess.get("call_connection_id"):
        try:
            client = _acs_client()
            client.get_call_connection(sess["call_connection_id"]).hang_up(is_for_everyone=True)
        except Exception as exc:
            err = str(exc)
            logger.warning("ACS hangup failed: %s", exc)
    sess["status"] = "ended"
    sess["ended_at"] = _utc()
    payload = {"status": sess["status"], "hangup_error": err}
    _event(session_id, "acs.session.ended", payload)
    store.add_event("acs.session_ended", {"session_id": session_id, "customer_id": sess.get("customer_id")})
    return {"session": _public_session(session_id), "hangup_error": err}


@router.post("/events/{session_id}")
async def acs_events(session_id: str, request: Request, store: DataStore = Depends(get_store)):
    """ACS Call Automation callback endpoint.

    This endpoint is intentionally unauthenticated because ACS cannot send the POC
    bearer token. The unguessable session id is treated as the callback secret for
    this POC. Production should add callback validation / private ingress.
    """
    sess = _get_session(session_id)
    body: Any = await request.json()
    # Event Grid validation support, if ACS sends CloudEvents validation through this route.
    if isinstance(body, list):
        validation = next((e for e in body if str(e.get("eventType") or e.get("type", "")).endswith("SubscriptionValidationEvent")), None)
        if validation:
            code = (validation.get("data") or {}).get("validationCode")
            return {"validationResponse": code}
        events = body
    elif isinstance(body, dict):
        if str(body.get("eventType") or body.get("type", "")).endswith("SubscriptionValidationEvent"):
            return {"validationResponse": (body.get("data") or {}).get("validationCode")}
        events = [body]
    else:
        events = []

    handled = []
    for ev in events:
        etype = _extract_event_type(ev)
        data = _extract_data(ev)
        sess.setdefault("raw_acs_events", []).append(ev)
        sess.setdefault("counts", {})["acs_events"] = sess["counts"].get("acs_events", 0) + 1
        if data.get("callConnectionId") and not sess.get("call_connection_id"):
            sess["call_connection_id"] = data.get("callConnectionId")
        _event(session_id, "acs.event", {"event_type": etype, "data": data}, "rm")
        handled.append(etype)

        if "CallConnected" in etype:
            sess["rm_leg_status"] = "connected"
            sess["rm_connected"] = True
            sess["status"] = "rm_connected"
            _event(session_id, "acs.rm.connected", {"status": sess["status"], "event_type": etype})
            try:
                _add_customer_participant(sess, reason="call-connected")
            except Exception as exc:
                logger.exception("Could not add customer participant after CallConnected")
                sess["customer_leg_status"] = "failed"
                sess["last_customer_add_error"] = str(exc)
                sess["last_error"] = str(exc)
                _event(session_id, "acs.customer.add_failed", {"error": str(exc), "reason": "call-connected"})
            if not sess.get("transcription_start_requested"):
                try:
                    _start_transcription(sess, reason="call-connected")
                except Exception as exc:
                    logger.exception("Could not start transcription after CallConnected")
                    sess["transcription_status"] = "failed"
                    sess["last_transcription_error"] = str(exc)
                    sess["last_error"] = str(exc)
                    _event(session_id, "acs.transcription.start_failed", {"error": str(exc), "reason": "call-connected"})
        elif "ParticipantsUpdated" in etype:
            flags = _participant_flags(sess, data)
            sess["participant_phones"] = flags["phones"]
            _event(session_id, "acs.participants.detected", flags)
            if flags["rm_seen"]:
                if not sess.get("rm_connected"):
                    _event(session_id, "acs.rm.detected", {"rm_phone": sess.get("rm_phone"), "participants": flags["phones"]})
                sess["rm_connected"] = True
                sess["rm_leg_status"] = "connected"
                if not sess.get("customer_connected"):
                    sess["status"] = "rm_connected" if not sess.get("customer_add_requested") else "dialing_customer"
                # Some PSTN flows emit ParticipantsUpdated but not CallConnected. Treat
                # RM-number presence as the safe trigger to dial the customer.
                if not sess.get("customer_add_requested") and not sess.get("customer_connected"):
                    try:
                        _add_customer_participant(sess, reason="rm-participants-updated")
                    except Exception as exc:
                        logger.exception("Could not add customer after RM ParticipantsUpdated")
                        sess["customer_leg_status"] = "failed"
                        sess["last_customer_add_error"] = str(exc)
                        sess["last_error"] = str(exc)
                        _event(session_id, "acs.customer.add_failed", {"error": str(exc), "reason": "rm-participants-updated"})
                # For ACS native transcription, we start at call setup. Do not
                # spam startTranscription on every participant update; keep the
                # manual button available if setup-start fails.
                if sess.get("transcription_status") in (None, "", "waiting") and not sess.get("transcription_start_requested"):
                    try:
                        _start_transcription(sess, reason="rm-participants-updated")
                    except Exception as exc:
                        logger.exception("Could not start transcription after RM ParticipantsUpdated")
                        sess["transcription_status"] = "failed"
                        sess["last_transcription_error"] = str(exc)
                        sess["last_error"] = str(exc)
                        _event(session_id, "acs.transcription.start_failed", {"error": str(exc), "reason": "rm-participants-updated"})
            if flags["customer_seen"]:
                sess["customer_connected"] = True
                sess["customer_leg_status"] = "connected"
                sess["status"] = "live"
                _event(session_id, "acs.customer.connected", {"customer_phone": sess.get("customer_phone"), "participants": flags["phones"]})
            elif flags["rm_seen"]:
                _event(session_id, "acs.status", {"status": sess.get("status"), "message": "RM connected. Customer leg is not connected yet; add request has been sent or is available via Dial customer now.", "participants": flags["phones"]})
        elif any(k in etype for k in ("AddParticipantSucceeded", "ParticipantAdded")):
            sess["customer_add_succeeded"] = True
            sess["customer_leg_status"] = "add_succeeded_waiting_for_participant_update"
            if sess.get("rm_connected"):
                sess["status"] = "dialing_customer"
            _event(session_id, "acs.customer.add_succeeded", {"event_type": etype, "message": "ACS accepted customer add. Waiting for participant update with customer phone.", "data": data})
        elif "AddParticipantFailed" in etype:
            sess["customer_leg_status"] = "failed"
            sess["last_customer_add_error"] = json.dumps(data)[:2000]
            sess["last_error"] = sess["last_customer_add_error"]
            _event(session_id, "acs.customer.add_failed", {"event_type": etype, "data": data})
        elif any(k in etype for k in ("TranscriptionStarted", "TranscriptionUpdated")):
            sess["transcription_status"] = "active"
            _event(session_id, "acs.transcription.status", {"status": "active", "event_type": etype})
        elif "TranscriptionFailed" in etype:
            sess["transcription_status"] = "failed"
            sess["last_transcription_error"] = json.dumps(data)[:2000]
            sess["last_error"] = sess["last_transcription_error"]
            _event(session_id, "acs.transcription.status", {"status": "failed", "event_type": etype, "data": data})
        elif any(k in etype for k in ("CallDisconnected", "CallEnded")):
            sess["status"] = "ended"
            if not sess.get("customer_connected"):
                sess["customer_leg_status"] = sess.get("customer_leg_status") or "not_connected"
            _event(session_id, "acs.status", {"status": sess.get("status"), "event_type": etype, "data": data})
        elif "CreateCallFailed" in etype:
            sess["status"] = "ended" if sess.get("status") == "ended" else "dial_failed"
            sess["last_error"] = json.dumps(data)[:2000]
            _event(session_id, "acs.status", {"status": sess.get("status"), "event_type": etype, "data": data})
    store.add_event("acs.callback", {"session_id": session_id, "events": handled, "customer_id": sess.get("customer_id")})
    return {"ok": True, "handled": handled, "session": _public_session(session_id)}


@ws_router.websocket("/v1/acs/transcription/{session_id}")
async def acs_transcription_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    store: DataStore = websocket.app.state.store
    try:
        sess = _get_session(session_id)
    except HTTPException:
        await websocket.close(code=4404)
        return
    _event(session_id, "acs.transcription.ws_connected", {"status": "connected"}, "rm")
    sess["transcription_status"] = "connected"
    try:
        while True:
            raw_msg = await websocket.receive_text()
            try:
                msg = json.loads(raw_msg)
            except Exception:
                _event(session_id, "acs.transcription.raw", {"raw": raw_msg[:500]}, "rm")
                continue
            kind = msg.get("kind") or msg.get("Kind") or ""
            if kind == "TranscriptionMetadata" or "transcriptionMetadata" in msg:
                meta = msg.get("transcriptionMetadata") or {}
                sess["transcription_status"] = "active"
                sess["transcription_metadata"] = meta
                _event(session_id, "acs.transcription.metadata", meta, "rm")
                continue
            if kind == "TranscriptionData" or "transcriptionData" in msg:
                data = msg.get("transcriptionData") or {}
                text = (data.get("text") or "").strip()
                result_status = str(data.get("resultStatus") or data.get("ResultStatus") or "Final")
                final = result_status.lower() == "final"
                if not final and not get_settings().acs_enable_intermediate_transcripts:
                    continue
                role = _speaker_role(sess, data.get("participantRawID") or data.get("participantRawId"), text, raw=data)
                _process_transcript(
                    session_id,
                    role,
                    text,
                    store,
                    source="acs_transcription",
                    confidence=data.get("confidence"),
                    raw=data,
                    final=final,
                )
                continue
            _event(session_id, "acs.transcription.unhandled", {"message": msg}, "rm")
    except Exception as exc:
        # WebSocket disconnects are expected when ACS stops transcription/call ends.
        logger.info("ACS transcription websocket closed session=%s reason=%s", session_id, exc)
        try:
            sess["transcription_status"] = "closed"
            _event(session_id, "acs.transcription.ws_closed", {"reason": str(exc)}, "rm")
        except Exception:
            pass
