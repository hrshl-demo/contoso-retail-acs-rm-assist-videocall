"""
backend/app/services/crosssell.py  — RETAIL edition.

Cross-sell / upsell engine. Derives a RETAIL customer's SIGNAL SET from the
deterministic analytics + profile + CIBIL, then matches it against the retail
product catalog. Every suggestion is eligibility-checked (blocking signals veto
it) and carries an explicit reason + the SOP it maps to.

Shared by the start-of-day briefing AND the live-call nudge engine.
"""
from __future__ import annotations
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine


def derive_signals(store: DataStore, customer_id: str) -> set[str]:
    """Turn a retail customer's data into a set of named signals the catalog matches on."""
    sig: set[str] = set()
    c = AccountConduct(store, customer_id).summary()
    ews = EWSEngine(store, customer_id).signals()
    cust = store.one("customer_master", customer_id=customer_id) or {}
    facs = store.where("facilities", customer_id=customer_id)
    bureau = store.one("bureau", customer_id=customer_id) or {}
    ins = store.where("insurance", customer_id=customer_id)

    # CIBIL score
    score = bureau.get("score") or bureau.get("commercial_score")
    try:
        score = int(float(score)) if score not in (None, "") else None
    except (TypeError, ValueError):
        score = None

    # conduct-derived (utilisation = CREDIT CARD utilisation)
    if c["credits_trend_label"] == "rising":
        sig.add("rising_credits")
    if c["avg_utilization_pct"] > 80:
        sig.add("high_utilization")
    if c["avg_utilization_pct"] < 50:
        sig.add("low_utilization")
    if c["cheque_return_count"] <= 1 and c["credits_trend_label"] != "declining":
        sig.add("clean_conduct")
    if c["cheque_return_count"] >= 2:                 # retail: >=2 EMI/auto-debit bounces
        sig.add("cheque_returns_3plus")
    if c["cash_intensity_pct"] > 10:
        sig.add("high_cash_intensity")

    # CIBIL-derived
    if score is not None and score >= 750:
        sig.add("high_credit_score")

    # EWS-derived
    if any(s["severity"] == "Critical" for s in ews):
        sig.add("critical_ews")
    if any(s["signal_type"] in ("Declining credits", "Cheque return", "Delayed interest") for s in ews):
        sig.add("receivable_delays")

    # KYC
    if str(cust.get("kyc_status", "")).lower() != "valid":
        sig.add("kyc_due")

    # products held
    if any(f.get("facility_type") == "Home Loan" for f in facs):
        sig.add("home_loan_held")
    if not ins:
        sig.add("insurance_gap")

    # investable surplus: rising income + low card utilisation + clean conduct
    if c["credits_trend_label"] == "rising" and c["avg_utilization_pct"] < 50 and c["cheque_return_count"] <= 1:
        sig.add("investable_surplus")

    return sig


def opportunities(store: DataStore, customer_id: str) -> list[dict]:
    """Eligibility-checked cross-sell/upsell suggestions, ranked, with reasons."""
    signals = derive_signals(store, customer_id)
    prof = store.one("business_profile", customer_id=customer_id) or {}
    vintage = int(prof.get("business_vintage_years") or 0)
    catalog = store.all("product_catalog")
    out = []

    for p in catalog:
        fit = set((p.get("fit_signals") or "").split(";")) - {""}
        blocking = set((p.get("blocking_signals") or "").split(";")) - {""}
        matched = fit & signals
        blocked_by = blocking & signals
        if not matched:
            continue
        min_vintage = int(p.get("min_vintage_years") or 0)
        eligible = (not blocked_by) and (vintage >= min_vintage)

        reasons = sorted(matched)
        blockers = sorted(blocked_by)
        if vintage < min_vintage:
            blockers.append(f"vintage {vintage}y < required {min_vintage}y")

        out.append({
            "product_id": p["product_id"], "product": p["name"], "category": p["category"],
            "eligible": eligible, "match_score": len(matched),
            "matched_signals": reasons, "blocked_by": blockers,
            "rationale": p["rationale_template"], "sop_ref": p.get("sop_ref"),
            "stance": ("Raise as opportunity" if eligible else "Hold — address blockers first"),
        })

    out.sort(key=lambda o: (not o["eligible"], -o["match_score"]))
    return out
