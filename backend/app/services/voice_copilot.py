"""
backend/app/services/voice_copilot.py

Voice Live integration for the MSME RM Assist copilot.

Unlike the loans POC (where the AI was an autonomous agent that SPOKE to the
customer), here the AI is a SILENT COPILOT: it listens to a live RM<->customer
call, transcribes both sides, and the backend runs each finalized transcript
segment through the NudgeEngine to surface RM-facing guidance. The AI never
produces audio to the customer.

We reuse the proven proxy machinery from the loans POC: ticket auth, Entra bearer
minting for the Voice Live WSS, and the bidirectional pump. The differences:
  - session.update configures transcription only (no agent voice/turn responses)
  - we intercept transcription-completed events, run the nudge engine, and push
    nudges onto a per-session queue the CRM polls/streams
"""
from __future__ import annotations
import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.store import DataStore
from app.services.nudge_engine import NudgeEngine

logger = logging.getLogger("contoso.msme.voice")

_cred = None


def _get_cred():
    global _cred
    if _cred is None:
        from azure.identity.aio import DefaultAzureCredential
        _cred = DefaultAzureCredential()
    return _cred


async def mint_voicelive_bearer() -> str:
    tok = await _get_cred().get_token("https://cognitiveservices.azure.com/.default")
    return tok.token


# ---------- tickets (single-use, 60s) bound to a customer ----------
_TICKETS: dict[str, tuple[float, str]] = {}
_TTL = 60.0


def issue_ticket(customer_id: str) -> str:
    _gc()
    t = secrets.token_urlsafe(24)
    _TICKETS[t] = (time.monotonic() + _TTL, customer_id)
    return t


def consume_ticket(t: str) -> tuple[bool, str | None]:
    _gc()
    e = _TICKETS.pop(t, None)
    if not e:
        return False, None
    exp, cid = e
    return (exp > time.monotonic()), cid


def _gc():
    now = time.monotonic()
    for k in [k for k, (exp, _) in _TICKETS.items() if exp <= now]:
        _TICKETS.pop(k, None)


# ---------- per-session nudge queues (CRM polls these) ----------
# session_id -> deque of nudge dicts. Bounded so a long call can't grow unbounded.
NUDGE_QUEUES: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
SESSION_TRANSCRIPT: dict[str, list[dict]] = defaultdict(list)


def _voice_session_update() -> dict:
    """Configure Voice Live as a SILENT transcriber. We pin en-IN (the proven
    no-flapping STT fix) and disable agent responses — modalities text only,
    no auto turn responses, so the model never speaks to the customer."""
    return {
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "instructions": ("You are a silent transcription service. Do not respond. "
                             "Only transcribe the audio accurately."),
            "input_audio_format": "pcm16",
            "input_audio_sampling_rate": 24000,
            "input_audio_transcription": {"model": "azure-speech", "language": "en-IN"},
            "turn_detection": {
                "type": "azure_semantic_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "languages": ["en"],
                # CRITICAL: do not auto-create agent responses — copilot is silent.
                "create_response": False,
                "interrupt_response": False,
            },
            "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        },
    }


async def copilot_session(browser_ws: WebSocket, store: DataStore, customer_id: str) -> None:
    """Run the silent-copilot proxy for one live call.
    Caller has accepted the browser WS and validated the ticket."""
    settings = get_settings()
    session_id = "VS-" + secrets.token_hex(6)
    NUDGE_QUEUES[session_id].clear()
    SESSION_TRANSCRIPT[session_id].clear()

    try:
        bearer = await mint_voicelive_bearer()
    except Exception as e:
        logger.exception("bearer mint failed")
        await browser_ws.send_json({"type": "proxy.error", "error": "auth_mint_failed", "detail": str(e)})
        await browser_ws.close(); return

    engine = NudgeEngine(store, customer_id)
    cust = store.one("customer_master", customer_id=customer_id) or {}
    store.add_event("voice.session_started", {"session_id": session_id, "customer_id": customer_id})

    await browser_ws.send_json({
        "type": "proxy.session", "session_id": session_id,
        "customer_id": customer_id, "customer_name": cust.get("display_name"),
        "model": settings.foundry_voicelive_model, "mode": "silent_copilot",
    })

    if not settings.foundry_voicelive_ws_endpoint:
        await browser_ws.send_json({"type": "proxy.error", "error": "voicelive_not_configured",
                                    "detail": "FOUNDRY_VOICELIVE_WS_ENDPOINT unset"})
        await browser_ws.close(); return

    base = settings.foundry_voicelive_ws_endpoint
    upstream_url = (f"{base}&model={settings.foundry_voicelive_model}" if "?" in base
                    else f"{base}?api-version=2025-10-01&model={settings.foundry_voicelive_model}")

    try:
        async with websockets.connect(
            upstream_url, extra_headers=[("Authorization", f"Bearer {bearer}")],
            max_size=10 * 1024 * 1024, ping_interval=20, ping_timeout=10, close_timeout=5,
        ) as upstream:
            await upstream.send(json.dumps(_voice_session_update()))
            logger.info("Silent-copilot session %s up (customer %s)", session_id, customer_id)

            async def browser_to_upstream():
                try:
                    while True:
                        msg = await browser_ws.receive_text()
                        await upstream.send(msg)
                except WebSocketDisconnect:
                    logger.info("browser disconnected %s", session_id)
                except Exception:
                    logger.exception("b2u error")

            async def upstream_to_browser():
                try:
                    async for raw in upstream:
                        s = raw if isinstance(raw, str) else raw.decode("utf-8")
                        # forward raw event to browser (for transcript display)
                        try:
                            await browser_ws.send_text(s)
                        except Exception:
                            break
                        # intercept finalized transcription -> nudge engine
                        try:
                            ev = json.loads(s)
                        except Exception:
                            continue
                        if ev.get("type") == "conversation.item.input_audio_transcription.completed":
                            text = (ev.get("transcript") or "").strip()
                            if text:
                                await _handle_transcript(browser_ws, store, engine, session_id, customer_id, text)
                except websockets.ConnectionClosed:
                    logger.info("upstream closed %s", session_id)
                except Exception:
                    logger.exception("u2b error")

            await asyncio.gather(browser_to_upstream(), upstream_to_browser(), return_exceptions=True)
    except Exception:
        logger.exception("copilot session crashed %s", session_id)
        try:
            await browser_ws.send_json({"type": "proxy.error", "error": "session_crashed"})
        except Exception:
            pass
    finally:
        store.add_event("voice.session_ended", {"session_id": session_id})
        try:
            await browser_ws.close()
        except Exception:
            pass


async def _handle_transcript(browser_ws, store, engine, session_id, customer_id, text):
    """Run a finalized transcript segment through the nudge engine and emit nudges."""
    SESSION_TRANSCRIPT[session_id].append({"text": text, "ts": time.time()})
    nudges = engine.detect(text)
    for n in nudges:
        n["session_id"] = session_id
        n["customer_id"] = customer_id
        n["nudge_id"] = "LN-" + secrets.token_hex(4)
        NUDGE_QUEUES[session_id].append(n)
        store.add_event("voice.nudge_fired", {"session_id": session_id, "intent": n["intent"],
                                              "priority": n["priority"]})
        try:
            await browser_ws.send_json({"type": "copilot.nudge", "nudge": n})
        except Exception:
            pass


def drain_nudges(session_id: str) -> list[dict]:
    """CRM polling fallback: return and clear queued nudges for a session."""
    q = NUDGE_QUEUES.get(session_id)
    if not q:
        return []
    out = list(q); q.clear()
    return out
