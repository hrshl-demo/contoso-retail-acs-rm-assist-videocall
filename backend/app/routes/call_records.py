"""Downloadable post-call transcript and AI-event artifacts.

The POC persists records in the Tool API runtime store. The API shape is the
production seam for Azure Blob Storage / Dataverse / Work IQ ingestion later.
"""
from __future__ import annotations

import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.deps import get_store, require_bearer
from app.store import DataStore

router = APIRouter(prefix="/v1", tags=["call-records"])


class CallRecordBody(BaseModel):
    record_id: str | None = None
    session_id: str
    customer_id: str
    customer_name: str | None = None
    mode: str = "video_assist_teams"
    started_at: str | None = None
    ended_at: str | None = None
    capture_scope: str = "customer_audio_plus_ai_events"
    transcript: list[dict] = Field(default_factory=list)
    ai_events: list[dict] = Field(default_factory=list)
    crm_cases: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class TranscriptMergeBody(BaseModel):
    transcript: list[dict] = Field(default_factory=list)
    source: str = "teams_post_meeting_transcript"
    metadata: dict = Field(default_factory=dict)


def _summary(rec: dict) -> dict:
    return {
        "record_id": rec.get("record_id"),
        "session_id": rec.get("session_id"),
        "customer_id": rec.get("customer_id"),
        "customer_name": rec.get("customer_name"),
        "mode": rec.get("mode"),
        "started_at": rec.get("started_at"),
        "ended_at": rec.get("ended_at"),
        "capture_scope": rec.get("capture_scope"),
        "transcript_turns": len(rec.get("transcript") or []),
        "ai_event_count": len(rec.get("ai_events") or []),
        "crm_case_count": len(rec.get("crm_cases") or []),
        "headline": (rec.get("summary") or {}).get("subject") or (rec.get("summary") or {}).get("headline") or "Call transcript",
        "status": rec.get("status", "final"),
        "conversation_type": (rec.get("metadata") or {}).get("conversation_type") or rec.get("mode"),
        "participant_role": (rec.get("metadata") or {}).get("participant_role") or "customer",
        "participant_name": (rec.get("metadata") or {}).get("participant_name") or rec.get("customer_name"),
        "evidence_origin": (rec.get("metadata") or {}).get("evidence_origin") or "live_video_assist",
        "replacement_rule": (rec.get("metadata") or {}).get("replacement_rule") or "",
        "download": {
            "txt": f"/v1/call-records/{rec.get('record_id')}/download?format=txt",
            "json": f"/v1/call-records/{rec.get('record_id')}/download?format=json",
        },
    }


@router.post("/call-records", dependencies=[Depends(require_bearer)])
def save_call_record(body: CallRecordBody, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=body.customer_id):
        raise HTTPException(404, "Customer not found")
    record = body.model_dump()
    record.setdefault("created_at", datetime.utcnow().isoformat() + "Z")
    saved = store.save_call_record(record)

    # Materialise a factual CRM interaction so the transcript is visible on the
    # normal customer timeline, while the full content remains in call_records.
    summary = saved.get("summary") or {}
    existing = [r for r in store.where("interactions", customer_id=body.customer_id)
                if r.get("call_record_id") == saved.get("record_id")]
    if not existing:
        store.tables.setdefault("interactions", []).append({
            "interaction_id": f"INT-CALL-{len(store.tables.get('interactions', []))+1:05d}",
            "customer_id": body.customer_id,
            "rm_id": "RM-1042",
            "interaction_date": (body.ended_at or body.started_at or datetime.utcnow().isoformat())[:10],
            "channel": "Teams video call",
            "subject": summary.get("subject") or "AI-assisted call transcript",
            "summary": summary.get("summary") or summary.get("call_summary") or "Call transcript captured and available for download.",
            "commitments_by_customer": summary.get("commitments_by_customer") or "See call record",
            "commitments_by_bank": summary.get("commitments_by_bank") or "See call record",
            "next_follow_up_date": summary.get("next_follow_up_date") or "",
            "sentiment": summary.get("sentiment") or "Neutral",
            "linked_task_id": "",
            "created_by": "Video Assist AI",
            "call_record_id": saved.get("record_id"),
            "transcript_available": "Y",
        })
    return {"record": _summary(saved)}


@router.get("/customers/{customer_id}/call-records", dependencies=[Depends(require_bearer)])
def list_customer_call_records(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    return {"customer_id": customer_id, "records": [_summary(r) for r in store.call_records_for_customer(customer_id)]}


@router.get("/call-records/{record_id}", dependencies=[Depends(require_bearer)])
def get_call_record(record_id: str, store: DataStore = Depends(get_store)):
    rec = store.get_call_record(record_id)
    if not rec:
        raise HTTPException(404, "Call record not found")
    return rec


def _txt(rec: dict) -> str:
    out = [
        "CONTOSO BANK · AI-ASSISTED CALL TRANSCRIPT",
        f"Record: {rec.get('record_id')}",
        f"Session: {rec.get('session_id')}",
        f"Customer: {rec.get('customer_name') or rec.get('customer_id')}",
        f"Started: {rec.get('started_at') or '-'}",
        f"Ended: {rec.get('ended_at') or '-'}",
        f"Capture scope: {rec.get('capture_scope')}",
        "",
        "TRANSCRIPT",
    ]
    for turn in rec.get("transcript") or []:
        ts = turn.get("timestamp") or turn.get("ts") or ""
        role = str(turn.get("role") or "speaker").upper()
        out.append(f"[{ts}] {role}: {turn.get('text','')}")
    out += ["", "AI EVENTS"]
    for ev in rec.get("ai_events") or []:
        out.append(f"[{ev.get('timestamp','')}] {str(ev.get('type','AI')).upper()}: {ev.get('text') or ev.get('summary') or ''}")
    out += ["", "CRM CASES"]
    for case in rec.get("crm_cases") or []:
        out.append(f"{case.get('caseRef') or case.get('case_ref') or '-'}: {case.get('subject','')} — {case.get('summary','')}")
    summary = rec.get("summary") or {}
    out += ["", "POST-CALL SUMMARY", summary.get("summary") or summary.get("call_summary") or json.dumps(summary, ensure_ascii=False)]
    out += ["", "NOTE", "This record may combine participant speech captured in Video Assist with RM companion lines captured from CRM. A Microsoft Graph/Teams transcript can still be merged later through the existing endpoint."]
    return "\n".join(out) + "\n"


@router.post("/call-records/{record_id}/merge-transcript", dependencies=[Depends(require_bearer)])
def merge_call_transcript(record_id: str, body: TranscriptMergeBody, store: DataStore = Depends(get_store)):
    """Merge a later two-speaker Teams/Graph transcript into the POC call record.

    The live demo can save customer-mic speech immediately. An approved post-meeting
    workflow can later call this seam with speaker-attributed Teams transcript turns.
    """
    rec = store.get_call_record(record_id)
    if not rec:
        raise HTTPException(404, "Call record not found")
    if not body.transcript:
        raise HTTPException(400, "transcript is required")
    merged = dict(rec)
    merged["transcript"] = body.transcript
    merged["capture_scope"] = "full_two_speaker_teams_transcript_plus_ai_and_crm_events"
    meta = dict(merged.get("metadata") or {})
    meta.update(body.metadata or {})
    meta.update({"full_teams_transcript_status": "merged", "full_teams_transcript_source": body.source, "merged_at": datetime.utcnow().isoformat() + "Z"})
    merged["metadata"] = meta
    saved = store.save_call_record(merged)
    return {"record": _summary(saved)}


@router.get("/call-records/{record_id}/download", dependencies=[Depends(require_bearer)])
def download_call_record(record_id: str, format: str = Query("txt", pattern="^(txt|json)$"), store: DataStore = Depends(get_store)):
    rec = store.get_call_record(record_id)
    if not rec:
        raise HTTPException(404, "Call record not found")
    safe = "".join(c for c in record_id if c.isalnum() or c in "-_")
    if format == "json":
        content = json.dumps(rec, indent=2, ensure_ascii=False, default=str)
        media = "application/json"
        filename = f"{safe}.json"
    else:
        content = _txt(rec)
        media = "text/plain; charset=utf-8"
        filename = f"{safe}.txt"
    return Response(content=content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
