"""Live-call fact extraction for the POC session room."""
from __future__ import annotations
import re
from datetime import datetime

_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crore|cr|lakh|lac|lakhs|lacs|million|mn)?", re.I)
_DAYS = re.compile(r"\b(\d{1,3})\s*(?:days|day)\b", re.I)
_DATE_WORD = re.compile(r"\b(today|tomorrow|next week|next month|by friday|by monday|before june|before month end|this week)\b", re.I)
_BUYER = re.compile(r"(?:buyer|client|OEM|order from|PO from|supplier)\s+(?:is\s+|called\s+|named\s+)?([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})")
_COMPETITOR = re.compile(r"\b(another bank|other bank|competitor|switch(?:ing)? bank|move(?:d)? to .* bank)\b", re.I)
_DOC = re.compile(r"\b(insurance copy|insurance|stock statement|gst return|gstr|gst|debtor aging|po copy|purchase order|financials|financial statement|itr|bank statement|kyc)\b", re.I)
_LIMIT = re.compile(r"\b(?:increase|raise|enhance|extend|higher|up to|to)\b[^.]{0,30}?(?:limit|cc|od|overdraft|credit)\b", re.I)
_ORDER = re.compile(r"\b(new order|recurring order|large order|export order|bulk order|repeat order|purchase order|\bpo\b|work order)\b", re.I)
_GST = re.compile(r"\b(?:gst|turnover|sales|revenue)\b[^.]{0,25}?(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crore|cr|lakh|lac|lakhs|lacs)\b", re.I)
_EMPLOYEES = re.compile(r"\b(\d{1,4})\s*(?:employees|staff|workers|people on payroll)\b", re.I)
_TENURE = re.compile(r"\b(\d{1,2})\s*(?:years|yrs)\b[^.]{0,20}(?:business|operation|relationship|banking)?", re.I)
_DELAY = re.compile(r"\b(payment delay|delayed payment|buyer.{0,15}(?:late|delay)|receivable.{0,10}(?:stuck|pending|delay)|not paid|outstanding)\b", re.I)


def _amount_to_text(match) -> str:
    num, unit = match.group(1), (match.group(2) or "").lower()
    if not unit and float(num) < 1000:
        return ""
    unit = {"cr": "crore", "lac": "lakh", "lacs": "lakh", "lakhs": "lakh", "mn": "million"}.get(unit, unit)
    return f"₹{num} {unit}".strip()


def extract_facts(text: str, speaker_role: str = "customer") -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    facts: list[dict] = []
    now = datetime.utcnow().isoformat() + "Z"

    for m in _AMOUNT.finditer(text):
        val = _amount_to_text(m)
        if val:
            facts.append({"fact_type": "amount_mentioned", "value": val, "source_snippet": text, "confidence": 0.72, "requires_rm_confirmation": True, "captured_at": now})
            break

    if _LIMIT.search(text):
        facts.append({"fact_type": "limit_change_request", "value": "Customer asked about a limit increase / enhancement", "source_snippet": text, "confidence": 0.8, "requires_rm_confirmation": True, "captured_at": now})
    if _ORDER.search(text):
        facts.append({"fact_type": "order_or_po_mentioned", "value": "New / recurring order or PO referenced", "source_snippet": text, "confidence": 0.78, "requires_rm_confirmation": True, "captured_at": now})
    m = _GST.search(text)
    if m:
        unit = {"cr": "crore", "lac": "lakh", "lacs": "lakh", "lakhs": "lakh"}.get((m.group(2) or "").lower(), m.group(2))
        facts.append({"fact_type": "turnover_or_gst_figure", "value": f"₹{m.group(1)} {unit}".strip(), "source_snippet": text, "confidence": 0.7, "requires_rm_confirmation": True, "captured_at": now})
    m = _EMPLOYEES.search(text)
    if m:
        facts.append({"fact_type": "employee_count", "value": f"{m.group(1)} employees", "source_snippet": text, "confidence": 0.75, "requires_rm_confirmation": True, "captured_at": now})
    m = _DAYS.search(text)
    if m:
        facts.append({"fact_type": "payment_or_delivery_cycle_days", "value": f"{m.group(1)} days", "source_snippet": text, "confidence": 0.82, "requires_rm_confirmation": True, "captured_at": now})
    if _DELAY.search(text):
        facts.append({"fact_type": "receivable_delay_signal", "value": "Buyer payment / receivable delay mentioned", "source_snippet": text, "confidence": 0.7, "requires_rm_confirmation": True, "captured_at": now})
    m = _DATE_WORD.search(text)
    if m:
        facts.append({"fact_type": "timeline_mentioned", "value": m.group(1), "source_snippet": text, "confidence": 0.7, "requires_rm_confirmation": True, "captured_at": now})
    m = _BUYER.search(text)
    if m:
        facts.append({"fact_type": "counterparty_mentioned", "value": m.group(1).strip(), "source_snippet": text, "confidence": 0.62, "requires_rm_confirmation": True, "captured_at": now})
    if _COMPETITOR.search(text):
        facts.append({"fact_type": "attrition_or_competitor_signal", "value": "competitor bank / switching risk", "source_snippet": text, "confidence": 0.86, "requires_rm_confirmation": False, "captured_at": now})
    for m in _DOC.finditer(text):
        facts.append({"fact_type": "document_mentioned", "value": m.group(1), "source_snippet": text, "confidence": 0.78, "requires_rm_confirmation": True, "captured_at": now})

    # de-duplicate within utterance
    seen, out = set(), []
    for f in facts:
        key = (f["fact_type"], f["value"].lower())
        if key not in seen:
            seen.add(key); out.append(f)
    return out[:5]
