"""Retail credit-card limit review assessment and approval-gated initiation."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import mean

from app.store import DataStore


def _num(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default


def assess(store: DataStore, customer_id: str) -> dict:
    cust = store.one("customer_master", customer_id=customer_id) or {}
    facilities = [x for x in store.where("facilities", customer_id=customer_id) if x.get("facility_type") == "CC"]
    card = facilities[0] if facilities else {}
    bureau = store.one("bureau", customer_id=customer_id) or {}
    financials = sorted(store.where("financials", customer_id=customer_id), key=lambda x: x.get("period_end_date") or x.get("financial_year") or "")
    current_fin = financials[-1] if financials else {}
    current_income = _num(current_fin.get("turnover_inr") or current_fin.get("annual_income_inr"))
    previous_income = _num(current_fin.get("turnover_prev_inr") or current_fin.get("annual_income_prev_inr"))
    if not current_income:
        profile = store.one("business_profile", customer_id=customer_id) or {}
        current_income = _num(profile.get("annual_turnover_current_year_inr"))
        previous_income = _num(profile.get("annual_turnover_prev_year_inr"))
    income_growth = ((current_income - previous_income) / previous_income * 100) if previous_income else 0.0
    utils = [_num(x.get("utilization_pct")) for x in store.where("utilization", customer_id=customer_id)]
    current_out = _num(card.get("current_outstanding_inr"))
    limit = _num(card.get("sanction_limit_inr"))
    current_util = current_out / limit * 100 if limit else (utils[-1] if utils else 0)
    avg_util = mean(utils[-30:]) if utils else current_util
    peak_util = max(utils[-30:]) if utils else current_util
    repayments = store.where("repayments", customer_id=customer_id)
    adverse = [r for r in repayments if str(r.get("payment_status", "")).lower() in {"delayed","failed","bounced","overdue"}]
    disputes = [s for s in store.where("service_requests", customer_id=customer_id) if str(s.get("status")).lower() == "open" and any(k in str(s.get("category","")).lower() for k in ("fraud","dispute","chargeback"))]
    score = int(_num(bureau.get("score")))
    kyc_ok = str(cust.get("kyc_status","")).lower() in {"valid","current"}

    tests = [
        {"test":"CIBIL threshold","actual":str(score),"required":"≥750","passed":score >= 750,"source":"bureau_summary"},
        {"test":"Income trend","actual":f"{income_growth:+.1f}%","required":"positive / stable","passed":income_growth >= 5,"source":"financial_statements_summary"},
        {"test":"Repayment conduct","actual":f"{len(adverse)} adverse events","required":"0","passed":not adverse,"source":"repayment_history"},
        {"test":"KYC readiness","actual":cust.get("kyc_status") or "unknown","required":"Valid","passed":kyc_ok,"source":"customer_master"},
        {"test":"Open card dispute","actual":f"{len(disputes)} open","required":"0","passed":not disputes,"source":"service_requests"},
        {"test":"Current utilisation","actual":f"{current_util:.1f}%","required":"≤70% for proactive review","passed":current_util <= 70,"source":"loan_facilities"},
    ]
    blockers = [x["test"] for x in tests if not x["passed"]]
    eligible = not blockers and bool(card)
    lower = int(round(limit * 1.30 / 50000) * 50000) if limit else 0
    upper_by_multiple = limit * 1.50
    upper_by_income = current_income * 0.25 if current_income else upper_by_multiple
    upper = int(round(min(upper_by_multiple, upper_by_income) / 50000) * 50000) if limit else 0
    if upper < lower: lower = upper
    return {
        "customer_id":customer_id,"customer_name":cust.get("display_name"),"eligible_for_review":eligible,
        "decision":"Eligible to initiate a card-limit review" if eligible else "Do not initiate until blockers are resolved",
        "current_limit_inr":limit,"current_outstanding_inr":current_out,"available_inr":max(0, limit-current_out),
        "current_utilisation_pct":round(current_util,1),"avg_30d_utilisation_pct":round(avg_util,1),"peak_30d_utilisation_pct":round(peak_util,1),
        "cibil_score":score,"income_current_inr":current_income,"income_previous_inr":previous_income,"income_growth_pct":round(income_growth,1),
        "recommended_review_band":{"lower_inr":lower,"upper_inr":upper} if eligible else None,
        "tests":tests,"blockers":blockers,
        "policy_basis":[
            "PR-002: CIBIL ≥750, rising income and clean repayment are required for a limit-increase review.",
            "The copilot may initiate a review request but cannot change the limit or approve credit.",
            "Final amount must come from the bank's credit/card decision system after human review.",
        ],
        "human_approval_required":True,
        "customer_consent_required":True,
        "disclaimer":"Pre-screen only. This is not approval and does not alter the credit limit.",
    }


def initiate_review(store: DataStore, customer_id: str, requested_limit_inr: float | None = None, actor: str = "RM-2207") -> dict:
    a = assess(store, customer_id)
    if not a["eligible_for_review"]:
        return {"created":False,"status":"BLOCKED","assessment":a,"reason":"; ".join(a["blockers"]) or "Not eligible"}
    existing = [t for t in store.where("tasks", customer_id=customer_id) if "card limit review" in str(t.get("title","")).lower() and str(t.get("status","")).lower() == "open"]
    if existing:
        return {"created":False,"status":"ALREADY_OPEN","request_id":existing[0].get("task_id"),"assessment":a}
    band = a["recommended_review_band"] or {}
    target = float(requested_limit_inr or band.get("upper_inr") or a["current_limit_inr"])
    if target < a["current_limit_inr"] or target > float(band.get("upper_inr") or target):
        target = float(band.get("upper_inr") or a["current_limit_inr"])
    due = (date.today() + timedelta(days=2)).isoformat()
    cand = store.propose_write({
        "customer_id":customer_id,"type":"task","evidence_refs":["card_limit_assessment","customer_video_call_consent","PR-002"],
        "payload":{"rm_id":actor,"title":f"Card limit review: ₹{int(a['current_limit_inr']):,} → up to ₹{int(target):,}","due_date":due,"status":"Open","priority":"Medium","approval_state":"Credit/Card underwriting required"}
    })
    saved = store.approve_write(cand["candidate_id"], f"{actor} · customer request captured via Video Assist")
    note = store.propose_write({
        "customer_id":customer_id,"type":"interaction","evidence_refs":["customer_video_call_consent","card_limit_assessment"],
        "payload":{"rm_id":actor,"channel":"Video call","subject":"Customer requested credit-card limit review","summary":f"Customer requested a limit increase. Pre-screen passed; current limit ₹{int(a['current_limit_inr']):,}, current utilisation {a['current_utilisation_pct']}%, CIBIL {a['cibil_score']}, income growth {a['income_growth_pct']:+.1f}%. Review initiated for up to ₹{int(target):,}; no approval or limit change has occurred.","commitments_by_customer":"Consent to credit/card assessment and provide any requested income evidence","commitments_by_bank":"Complete human credit/card review; communicate decision without promise","next_follow_up_date":due,"sentiment":"Positive"}
    })
    store.approve_write(note["candidate_id"], f"{actor} · customer request captured via Video Assist")
    request_id = (saved or {}).get("materialized_id") or (saved or {}).get("task_id") or cand["candidate_id"]
    store.add_event("card_limit.review_initiated", {"customer_id":customer_id,"target_limit_inr":target,"actor":actor,"request_id":request_id})
    return {"created":True,"status":"REVIEW_INITIATED","request_id":request_id,"target_limit_inr":target,"due_date":due,"assessment":a,"message":"Card-limit review initiated. The current limit is unchanged pending human underwriting."}
