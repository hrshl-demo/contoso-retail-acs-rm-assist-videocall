"""Post-call wrap-up synthesis for the two-device POC."""
from __future__ import annotations
from datetime import datetime, timedelta
from app.store import DataStore
from app.services.command_center import credit_readiness


def build_wrapup(store: DataStore, session: dict) -> dict:
    cid = session.get("customer_id")
    transcripts = session.get("transcripts", [])
    customer_lines = [t.get("text", "") for t in transcripts if t.get("role") == "customer"]
    rm_lines = [t.get("text", "") for t in transcripts if t.get("role") == "rm"]
    facts = session.get("captured_facts", [])
    nudges = session.get("fired_nudges", [])
    docs = session.get("document_requests", [])
    uploads = session.get("uploaded_documents", [])
    readiness = credit_readiness(store, cid) if cid else {}

    intents = []
    for n in nudges:
        i = n.get("intent")
        if i and i not in intents:
            intents.append(i)

    asks = []
    joined = " ".join(customer_lines).lower()
    if any(x in joined for x in ["working capital", "higher limit", "enhance", "more funds"]):
        asks.append("Customer asked for working-capital / limit support.")
    if any(x in joined for x in ["cheque", "payment delay", "receivable"]):
        asks.append("Customer discussed buyer payment delay / receivable pressure.")
    if any(x in joined for x in ["charges", "fee", "dispute"]):
        asks.append("Customer raised service or charges concern.")
    if any(x in joined for x in ["another bank", "move", "switch"]):
        asks.append("Customer indicated attrition or competitor-bank risk.")
    if not asks and customer_lines:
        asks.append("Customer discussed account relationship and required RM follow-up.")

    commitments_bank = []
    if docs:
        commitments_bank.append("RM requested documents and must verify receipt/status in CRM.")
    if intents:
        commitments_bank.append("RM must convert accepted nudges into CRM notes/tasks/opportunities.")
    commitments_bank.append("RM must not communicate credit approval until credit appraisal is complete.")

    follow_up_date = (datetime.utcnow() + timedelta(days=2)).date().isoformat()
    crm_note = (
        f"AI-assisted live-call summary for {session.get('customer_name','customer')}. "
        f"Main topics: {', '.join(intents) if intents else 'relationship discussion'}. "
        f"Customer asks: {' '.join(asks)} "
        f"Captured facts: {', '.join(f.get('fact_type')+': '+f.get('value','') for f in facts[:6]) or 'none captured'}. "
        f"Documents requested: {sum(len(d.get('items', [])) for d in docs)} item(s). "
        f"Credit readiness: {readiness.get('score','-')} - {readiness.get('label','-')}. "
        "Any facility action remains subject to documentation, policy checks and credit approval."
    )

    return {
        "session_id": session.get("session_id"),
        "customer_id": cid,
        "customer_name": session.get("customer_name"),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "call_summary": crm_note,
        "customer_asks": asks,
        "rm_commitments": commitments_bank,
        "customer_commitments": [f"Confirm/submit {f.get('value')}" for f in facts if f.get("fact_type") == "document_mentioned"][:5],
        "captured_facts": facts,
        "risks_detected": [n.get("nudge_text") for n in nudges if n.get("priority") in {"High", "Critical"}][:5],
        "nudges_triggered": [{"intent": n.get("intent"), "priority": n.get("priority"), "next_question": n.get("recommended_next_utterance")} for n in nudges],
        "documents_requested": docs,
        "documents_uploaded": uploads,
        "crm_note_draft": crm_note,
        "follow_up_tasks": [
            {"title": "Verify live-call documents and update CRM", "due_date": follow_up_date, "priority": "High" if docs else "Medium"},
            {"title": "Prepare credit/service handoff from call wrap-up", "due_date": follow_up_date, "priority": "Medium"},
        ],
        "evidence_refs": ["voice.transcripts", "voice.nudges", "voice.captured_facts", "document_requests", "credit_readiness"],
    }
