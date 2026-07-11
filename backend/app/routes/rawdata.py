"""
backend/app/routes/rawdata.py

Raw Data explorer endpoints. Powers the CRM "Raw Data" tab, which lets the RM
show a customer, live in the demo, the ACTUAL synthetic data pack and the
Indian-banking SOP corpus that ground every AI answer and nudge. This makes the
POC verifiable end-to-end: "this is the real data / the real policy the model
reads", not a scripted illusion.

Two endpoints (bearer-gated, read-only):
  - GET /v1/rawdata/catalog        grouped manifest of every shippable data file
  - GET /v1/rawdata/file?id=<id>   the raw content of one file (path-traversal safe)

Files are served from three baked-in roots (see config + Dockerfile):
  csv -> settings.data_dir   (/app/data/csv)            the synthetic customer pack
  kb  -> settings.kb_dir     (/app/data/knowledge_base) product rules + playbooks
  sop -> settings.sop_dir    (/app/data/sop)            RM Standard Operating Procedures
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.deps import require_bearer, get_store
from app.store import DataStore

router = APIRouter(prefix="/v1/rawdata", tags=["rawdata"], dependencies=[Depends(require_bearer)])

# Extensions we are willing to surface (everything in the pack is one of these).
_ALLOWED_EXT = {".csv", ".json", ".md", ".txt"}

# Root key -> settings attribute. Resolved lazily so tests/dev can override paths.
_ROOT_KEYS = ("csv", "kb", "sop")


def _roots() -> dict[str, Path]:
    s = get_settings()
    return {
        "csv": Path(s.data_dir),
        "kb": Path(s.kb_dir),
        "sop": Path(s.sop_dir),
    }


# ---- presentation helpers --------------------------------------------------

# Acronyms that must stay upper-cased when we prettify a file name.
_ACRONYMS = {
    "kyc", "sma", "emi", "cibil", "gst", "itr", "crm", "ai", "ews", "msme",
    "foir", "rm", "hni", "vcip", "apr", "pan", "aoai", "sop", "fy", "id",
    "rbi", "dpd", "ews", "poc", "ovd",
}

# Curated, demo-facing descriptions keyed by file BASENAME. Anything not listed
# falls back to a per-group generic line, so the catalog is always populated.
_DESCRIPTIONS: dict[str, str] = {
    # 01 master
    "customer_master.csv": "Master customer record — identity, contact, segment, KYC status and risk band.",
    "msme_business_profile.csv": "Business profile — constitution, vintage, activity and self-employment details.",
    "promoters_guarantors.csv": "Promoters and guarantors on the relationship (family / co-obligant liability).",
    "stakeholders.csv": "Related parties and signatories linked to the customer.",
    "portfolio_assignments.csv": "RM-to-customer portfolio mapping.",
    # 02 accounts
    "accounts.csv": "Deposit, card and loan account register with balances and limits.",
    "current_account_transactions_fy2025_26.csv": "Full FY2025–26 transaction ledger used for cash-flow and income-trend analysis.",
    "daily_balances.csv": "Daily end-of-day balances — savings behaviour and buffer analysis.",
    "counterparty_master.csv": "Known counterparties / merchants seen in the transaction ledger.",
    # 03 credit
    "loan_facilities.csv": "Sanctioned facilities — card and personal-loan limits, rates, EMIs and outstanding.",
    "daily_limit_utilization.csv": "Daily card-limit utilisation — the over-limit / high-utilisation evidence.",
    "repayment_history.csv": "EMI repayment track — bounced/delayed EMIs and SMA classification signals.",
    "collateral_security.csv": "Collateral and security held against facilities.",
    "facility_covenants.csv": "Covenants and conditions attached to sanctioned facilities.",
    "stock_statements.csv": "Stock / drawing-power statements (working-capital facilities).",
    "insurance_status.csv": "Insurance / credit-protection coverage status — the protection-gap evidence.",
    # 04 financials
    "gst_returns_monthly.csv": "Monthly GST turnover returns (income corroboration).",
    "financial_statements_summary.csv": "Summarised financial statements — income and obligation trend.",
    "debtor_creditor_aging.csv": "Debtor / creditor ageing (receivables stress).",
    "bureau_summary.csv": "Credit-bureau summary — CIBIL score and enquiry / DPD history.",
    # 05 operations
    "document_status.csv": "KYC / loan document checklist and validity — re-KYC due tracking.",
    "service_requests.csv": "Service requests and disputes — includes the open GlobalMart chargeback.",
    "cheque_returns.csv": "Cheque / mandate return history.",
    "consent_registry.csv": "Customer consents captured for contact and processing.",
    # 06 crm
    "rm_interactions.csv": "Logged RM interactions and call notes.",
    "engagement_threads.csv": "Multi-touch engagement threads with the customer.",
    "crm_tasks.csv": "Open and closed CRM tasks and follow-ups.",
    "opportunities.csv": "Identified cross-sell / service opportunities and stages.",
    "audit_log.csv": "Seed audit-trail entries (glass-box lineage).",
    # 08 rm
    "rm_daily_activity.csv": "RM daily activity feed (DILO / MILO planner input).",
    # knowledge base
    "product_rules.csv": "Product eligibility rules engine — gates for card-limit / new credit.",
    "document_checklist_rules.csv": "Document-checklist rules per product.",
    "product_catalog.csv": "Product catalogue with indicative pricing bands.",
    "marketing_templates.csv": "Approved marketing / collateral templates.",
    "policy_documents_manifest.csv": "Index of policy documents fed to the RAG policy index.",
    "solution_playbooks.csv": "RM solution playbooks — restructuring, hardship and retention plays.",
    "crm_cases_enriched.csv": "AI-enriched CRM case narratives overlaid on the seed cases.",
    "ai_generation_manifest.json": "Provenance manifest for the one-time AI content generation.",
}

# csv sub-directory (top folder) -> friendly group label + ordering weight.
_CSV_GROUPS: dict[str, tuple[str, int]] = {
    "01_master_data": ("Customer master data", 10),
    "02_accounts": ("Accounts & transactions", 20),
    "03_credit": ("Credit & facilities", 30),
    "04_financials": ("Financials & bureau", 40),
    "05_operations": ("Operations & service", 50),
    "06_crm": ("CRM & engagement", 60),
    "07_voice": ("Voice / live call", 70),
    "08_ai_expected_outputs": ("AI expected outputs", 80),
    "08_rm": ("RM activity", 85),
}
_KB_GROUP = ("Knowledge base & product rules", 90)
_SOP_GROUP = ("SOPs — Standard Operating Procedures", 100)


def _prettify(stem: str) -> str:
    words = stem.replace("-", "_").split("_")
    out = []
    for w in words:
        if not w:
            continue
        if w.lower() in _ACRONYMS:
            out.append(w.upper())
        elif w.isdigit():
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out) if out else stem


def _ext_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {".csv": "csv", ".json": "json", ".md": "md", ".txt": "text"}.get(ext, "text")


def _count_rows(path: Path) -> int | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            n = sum(1 for _ in f)
        return max(0, n - 1)  # minus header
    except Exception:
        return None


def _sop_meta(path: Path) -> tuple[str, str]:
    """Return (title, description) for an SOP markdown file: the first H1 as the
    title and the first line under a '## Purpose' heading as the description."""
    title = _prettify(path.stem)
    purpose = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return title, "Bank Standard Operating Procedure (RM policy guidance)."
    got_title = False
    in_purpose = False
    for ln in lines:
        s = ln.strip()
        if not got_title and s.startswith("# "):
            title = s[2:].strip()
            got_title = True
            continue
        if s.lower().startswith("## purpose"):
            in_purpose = True
            continue
        if in_purpose:
            if s.startswith("#"):
                break
            if s:
                purpose = s.lstrip("- ").strip()
                break
    if not purpose:
        purpose = "Bank Standard Operating Procedure (RM policy guidance)."
    return title, purpose


def _describe(root_key: str, path: Path) -> str:
    base = path.name
    if base in _DESCRIPTIONS:
        return _DESCRIPTIONS[base]
    if root_key == "sop":
        return _sop_meta(path)[1]
    if root_key == "kb":
        return "Knowledge-base reference used by the rules and RAG layer."
    return "Synthetic data table used by the RM Assist tool API."


def _group_for(root_key: str, root: Path, path: Path) -> tuple[str, int]:
    if root_key == "kb":
        return _KB_GROUP
    if root_key == "sop":
        return _SOP_GROUP
    # csv: use the top-level sub-directory
    try:
        rel = path.relative_to(root)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
    except Exception:
        top = ""
    return _CSV_GROUPS.get(top, ("Customer data", 88))


def _display_name(root_key: str, path: Path) -> str:
    if root_key == "sop":
        return _sop_meta(path)[0]
    return _prettify(path.stem)


def _iter_files(root: Path):
    if not root.exists():
        return
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() in _ALLOWED_EXT:
                yield p


def _entry(root_key: str, root: Path, path: Path) -> dict:
    rel = path.relative_to(root).as_posix()
    typ = _ext_type(path)
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    entry = {
        "id": f"{root_key}/{rel}",
        "name": _display_name(root_key, path),
        "file": path.name,
        "type": typ,
        "size": size,
        "description": _describe(root_key, path),
    }
    if typ == "csv":
        entry["rows"] = _count_rows(path)
    return entry


@router.get("/catalog")
def catalog() -> dict:
    """Grouped manifest of every data / SOP file baked into the image."""
    roots = _roots()
    groups: dict[str, dict] = {}
    total = 0
    for root_key in _ROOT_KEYS:
        root = roots[root_key]
        for path in _iter_files(root):
            label, weight = _group_for(root_key, root, path)
            g = groups.setdefault(label, {"label": label, "weight": weight, "files": []})
            g["files"].append(_entry(root_key, root, path))
            total += 1
    ordered = sorted(groups.values(), key=lambda g: (g["weight"], g["label"]))
    for g in ordered:
        g["files"].sort(key=lambda e: e["name"].lower())
        g.pop("weight", None)
    return {
        "groups": ordered,
        "total_files": total,
        "roots": {
            "csv": "Synthetic customer data pack",
            "kb": "Knowledge base & product rules",
            "sop": "RM Standard Operating Procedures",
        },
    }


def _resolve_safe(file_id: str) -> tuple[str, Path, Path]:
    """Map an id like 'csv/03_credit/loan_facilities.csv' to a concrete path,
    guarding against traversal outside the allowed roots."""
    if not file_id or "\x00" in file_id:
        raise HTTPException(400, "Missing or invalid id.")
    file_id = file_id.replace("\\", "/").lstrip("/")
    parts = file_id.split("/", 1)
    if len(parts) != 2 or parts[0] not in _ROOT_KEYS:
        raise HTTPException(400, "id must be '<csv|kb|sop>/<relative-path>'.")
    root_key, rel = parts[0], parts[1]
    root = _roots()[root_key].resolve()
    target = (root / rel).resolve()
    # Path-traversal guard: the resolved target must live under its root.
    if root != target and root not in target.parents:
        raise HTTPException(400, "Path outside of the permitted data root.")
    if target.suffix.lower() not in _ALLOWED_EXT:
        raise HTTPException(400, "Unsupported file type.")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {file_id}")
    return root_key, root, target


@router.get("/file")
def file(id: str = Query(..., description="Catalog id, e.g. csv/03_credit/loan_facilities.csv")) -> dict:
    """Return the raw content of one cataloged file."""
    root_key, root, target = _resolve_safe(id)
    typ = _ext_type(target)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(500, f"Could not read file: {exc}")
    out = {
        "id": id,
        "name": _display_name(root_key, target),
        "file": target.name,
        "type": typ,
        "size": len(content.encode("utf-8", errors="replace")),
        "description": _describe(root_key, target),
        "content": content,
    }
    if typ == "csv":
        out["rows"] = _count_rows(target)
    return out


# ---- Customer 360 -----------------------------------------------------------
# A single-click, fully-joined view of THE demo customer (Rakesh Sharma,
# CTB-RTL-002): identity, KYC, accounts, facilities, bureau, spend analytics,
# disputes, documents and the CRM trail — assembled live from the same seed
# tables the AI grounds on, so the RM can prove the data on screen.

from datetime import datetime, timezone  # noqa: E402


def _f(v, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default


def _i(v, default: int = 0) -> int:
    try:
        return int(round(_f(v, default)))
    except Exception:
        return default


def _lakh(n: float) -> str:
    return f"\u20b9{n / 100000:.2f} L"


def _pct(n: float) -> str:
    return f"{n:.1f}%"


def _pick(row: dict, *keys) -> dict:
    return {k: row.get(k, "") for k in keys}


@router.get("/profile")
def profile(id: str = Query(default="", description="Customer id; defaults to the demo customer."),
            store: DataStore = Depends(get_store)) -> dict:
    """Assemble the full Customer-360 record for one customer (default: the only
    customer in this single-customer demo pack)."""
    customers = store.all("customer_master")
    if not customers:
        raise HTTPException(404, "No customers loaded.")
    cid = id.strip() or customers[0].get("customer_id", "")
    cust = store.one("customer_master", customer_id=cid)
    if not cust:
        raise HTTPException(404, f"Customer not found: {cid}")
    biz = store.one("business_profile", customer_id=cid) or {}
    bureau = store.one("bureau", customer_id=cid) or {}
    fin = store.one("financials", customer_id=cid) or {}
    accounts = store.where("accounts", customer_id=cid)
    facilities = store.where("facilities", customer_id=cid)
    repayments = store.where("repayments", customer_id=cid)
    docs = store.where("documents", customer_id=cid)
    srs = store.where("service_requests", customer_id=cid)
    bounces = store.where("cheque_returns", customer_id=cid)
    consent = store.where("consent", customer_id=cid)
    opps = store.where("opportunities", customer_id=cid)
    interactions = store.where("interactions", customer_id=cid)
    guarantors = store.where("promoters", customer_id=cid)
    txns = store.where("transactions", customer_id=cid)
    util = store.where("utilization", customer_id=cid)

    # ---- facilities (compute card finance charge + carry status) ----
    card = next((f for f in facilities if str(f.get("facility_type", "")).upper() in ("CC", "CREDIT CARD")), None)
    fac_out = []
    total_outstanding = 0.0
    for f in facilities:
        out = _f(f.get("current_outstanding_inr"))
        total_outstanding += out
        apr = _f(f.get("interest_rate_pct"))
        row = {
            "facility_id": f.get("facility_id", ""),
            "facility_type": f.get("facility_type", ""),
            "sanction_limit_inr": _f(f.get("sanction_limit_inr")),
            "current_outstanding_inr": out,
            "interest_rate_pct": apr,
            "security_type": f.get("security_type", ""),
            "status": f.get("facility_status", ""),
            "monthly_finance_charge_inr": round(out * apr / 100 / 12) if apr else None,
        }
        fac_out.append(row)

    # ---- card utilisation (latest daily snapshot) ----
    util_latest = {}
    if util:
        util_sorted = sorted(util, key=lambda r: r.get("date", ""))
        u = util_sorted[-1]
        util_latest = {
            "date": u.get("date", ""),
            "sanction_limit_inr": _f(u.get("sanction_limit_inr")),
            "outstanding_inr": _f(u.get("outstanding_inr")),
            "utilization_pct": _f(u.get("utilization_pct")),
            "over_limit_flag": u.get("over_limit_flag", ""),
            "available_limit_inr": _f(u.get("available_limit_inr")),
        }

    # ---- spend analytics from the transaction ledger ----
    total_debit = total_credit = 0.0
    by_cat: dict[str, dict] = {}
    for t in txns:
        amt = _f(t.get("amount_inr"))
        cat = t.get("category_lvl1") or "Uncategorised"
        is_dr = str(t.get("dr_cr", "")).upper() == "DR"
        if is_dr:
            total_debit += amt
        else:
            total_credit += amt
        c = by_cat.setdefault(cat, {"category": cat, "debit_inr": 0.0, "credit_inr": 0.0, "count": 0})
        c["count"] += 1
        c["debit_inr" if is_dr else "credit_inr"] += amt
    cats = sorted(by_cat.values(), key=lambda c: (c["debit_inr"] + c["credit_inr"]), reverse=True)
    for c in cats:
        gross = total_debit if c["debit_inr"] >= c["credit_inr"] else total_credit
        c["pct_of_debit"] = round(c["debit_inr"] / total_debit * 100, 1) if total_debit else 0.0
    dates = sorted(t.get("txn_date", "") for t in txns if t.get("txn_date"))
    window = f"{dates[0]} \u2192 {dates[-1]}" if dates else ""
    recent = sorted(txns, key=lambda r: (r.get("txn_timestamp", ""), r.get("txn_id", "")), reverse=True)[:15]
    recent_out = [{
        "txn_date": t.get("txn_date", ""),
        "dr_cr": t.get("dr_cr", ""),
        "amount_inr": _f(t.get("amount_inr")),
        "category_lvl1": t.get("category_lvl1", ""),
        "description": t.get("description", ""),
        "counterparty_name": t.get("counterparty_name", ""),
        "balance_after_txn_inr": _f(t.get("balance_after_txn_inr")),
        "anomaly_tag": t.get("anomaly_tag", "") if t.get("anomaly_tag", "") not in ("None", "") else "",
    } for t in recent]

    # ---- repayments summary ----
    bounced = [r for r in repayments if "bounce" in str(r.get("payment_status", "")).lower()
               or "return" in str(r.get("payment_status", "")).lower() or _i(r.get("days_past_due")) > 0
               and "bounce" in str(r.get("remarks", "")).lower()]
    delayed = [r for r in repayments if _i(r.get("days_past_due")) > 0]

    # ---- headline KPIs ----
    cibil = _i(bureau.get("score"))
    kyc_status = cust.get("kyc_status", "")
    open_disputes = [s for s in srs if str(s.get("status", "")).lower() in ("open", "in progress", "in-progress")]
    inc_cur = _f(biz.get("annual_turnover_current_year_inr") or fin.get("turnover_inr"))
    inc_prev = _f(biz.get("annual_turnover_prev_year_inr") or fin.get("turnover_prev_inr"))
    inc_delta = round((inc_cur - inc_prev) / inc_prev * 100, 1) if inc_prev else 0.0
    card_util = util_latest.get("utilization_pct") or (
        round(_f(card.get("current_outstanding_inr")) / _f(card.get("sanction_limit_inr")) * 100, 1)
        if card and _f(card.get("sanction_limit_inr")) else 0.0)

    kpis = [
        {"label": "CIBIL score", "value": str(cibil) if cibil else "\u2014",
         "sub": bureau.get("bureau_score_band", ""), "tone": "danger" if cibil and cibil < 700 else "ok"},
        {"label": "Total outstanding", "value": _lakh(total_outstanding),
         "sub": f"across {len(facilities)} facilities", "tone": "warn"},
        {"label": "Card utilisation", "value": _pct(card_util) if card_util else "\u2014",
         "sub": "over-limit" if card_util and card_util > 100 else "of sanctioned limit",
         "tone": "danger" if card_util and card_util >= 90 else "info"},
        {"label": "KYC status", "value": kyc_status or "\u2014",
         "sub": f"due {cust.get('next_kyc_due_date', '')}" if cust.get("next_kyc_due_date") else "",
         "tone": "warn" if str(kyc_status).lower() in ("due", "pending", "overdue") else "ok"},
        {"label": "Open disputes / SRs", "value": str(len(open_disputes)),
         "sub": "service recovery first" if open_disputes else "none open",
         "tone": "danger" if open_disputes else "ok"},
        {"label": "Income trend (YoY)", "value": f"{inc_delta:+.1f}%",
         "sub": f"{_lakh(inc_prev)} \u2192 {_lakh(inc_cur)}", "tone": "danger" if inc_delta < 0 else "ok"},
    ]

    return {
        "customer_id": cid,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "name": cust.get("display_name") or cust.get("legal_name", ""),
        "segment": cust.get("segment", ""),
        "risk_category": cust.get("risk_category", ""),
        "kpis": kpis,
        "identity": {
            "legal_name": cust.get("legal_name", ""),
            "constitution": cust.get("constitution", ""),
            "pan_masked": cust.get("pan_masked", ""),
            "customer_since": cust.get("customer_since", ""),
            "home_branch_code": cust.get("home_branch_code", ""),
            "rm_id": cust.get("rm_id", ""),
            "priority_sector_flag": cust.get("priority_sector_flag", ""),
            "relationship_value_score": cust.get("relationship_value_score", ""),
        },
        "business": {
            "industry_description": biz.get("industry_description", ""),
            "business_vintage_years": biz.get("business_vintage_years", ""),
            "registered_address": biz.get("registered_address", ""),
            "employee_count": biz.get("employee_count", ""),
            "annual_turnover_prev_year_inr": _f(biz.get("annual_turnover_prev_year_inr")),
            "annual_turnover_current_year_inr": _f(biz.get("annual_turnover_current_year_inr")),
            "risk_notes": biz.get("risk_notes", ""),
            "growth_notes": biz.get("growth_notes", ""),
            "business_model_notes": biz.get("business_model_notes", ""),
        },
        "kyc": {
            "status": kyc_status,
            "next_kyc_due_date": cust.get("next_kyc_due_date", ""),
            "consent_status": cust.get("consent_status", ""),
            "blocking_documents": [_pick(d, "document_type", "status", "blocking_flag", "remarks")
                                   for d in docs if str(d.get("blocking_flag", "")).upper() == "Y"],
            "consents": [_pick(c, "consent_type", "consent_status", "purpose", "channel", "consent_date")
                         for c in consent],
        },
        "accounts": [{
            "account_id": a.get("account_id", ""),
            "account_type": a.get("account_type", ""),
            "status": a.get("status", ""),
            "sanction_limit_inr": _f(a.get("sanction_limit_inr")),
            "avg_monthly_balance_inr": _f(a.get("avg_monthly_balance_inr")),
            "interest_rate_pct": _f(a.get("interest_rate_pct")),
            "open_date": a.get("open_date", ""),
        } for a in accounts],
        "facilities": fac_out,
        "utilization": util_latest,
        "bureau": {
            "score": cibil,
            "band": bureau.get("bureau_score_band", ""),
            "as_of": bureau.get("as_of", ""),
            "enquiries_6m": _i(bureau.get("enquiries_6m")),
            "dpd_flag": bureau.get("dpd_flag", ""),
            "dpd_count": _i(bureau.get("dpd_count")),
            "remarks": bureau.get("remarks", ""),
        },
        "spend": {
            "window": window,
            "txn_count": len(txns),
            "total_debit_inr": round(total_debit),
            "total_credit_inr": round(total_credit),
            "by_category": [{
                "category": c["category"],
                "debit_inr": round(c["debit_inr"]),
                "credit_inr": round(c["credit_inr"]),
                "count": c["count"],
                "pct_of_debit": c["pct_of_debit"],
            } for c in cats],
            "recent": recent_out,
        },
        "repayments": {
            "total": len(repayments),
            "bounced": len(bounces),
            "delayed": len(delayed),
            "rows": [_pick(r, "due_date", "amount_due_inr", "amount_paid_inr", "days_past_due",
                           "payment_status", "remarks") for r in
                     sorted(repayments, key=lambda r: r.get("due_date", ""), reverse=True)[:14]],
        },
        "disputes": [{
            "ticket_id": s.get("ticket_id", ""),
            "category": s.get("category", ""),
            "status": s.get("status", ""),
            "priority": s.get("priority", ""),
            "created_date": s.get("created_date", ""),
            "sla_due_date": s.get("sla_due_date", ""),
            "sentiment": s.get("customer_sentiment", ""),
            "description": s.get("description", ""),
        } for s in srs],
        "cheque_returns": [_pick(b, "return_date", "amount_inr", "return_reason", "severity",
                                 "counterparty_name", "remarks") for b in bounces],
        "documents": [_pick(d, "document_type", "status", "required_flag", "blocking_flag",
                            "expiry_date", "remarks") for d in docs],
        "opportunities": [_pick(o, "opportunity_type", "stage", "status", "recommended_band_inr", "blockers")
                          for o in opps],
        "interactions": [{
            "interaction_date": it.get("interaction_date", ""),
            "channel": it.get("channel", ""),
            "subject": it.get("subject", ""),
            "summary": it.get("summary", ""),
            "sentiment": it.get("sentiment", ""),
            "next_follow_up_date": it.get("next_follow_up_date", ""),
        } for it in sorted(interactions, key=lambda r: r.get("interaction_date", ""), reverse=True)],
        "guarantors": [_pick(g, "role", "name", "shareholding_pct", "bureau_score_band",
                             "net_worth_band_inr", "kyc_status") for g in guarantors],
        "financials": {
            "fy": fin.get("fy", ""),
            "turnover_inr": _f(fin.get("turnover_inr")),
            "turnover_prev_inr": _f(fin.get("turnover_prev_inr")),
            "remarks": fin.get("remarks", ""),
        },
        "counts": {
            "transactions": len(txns), "accounts": len(accounts), "facilities": len(facilities),
            "service_requests": len(srs), "documents": len(docs), "opportunities": len(opps),
            "interactions": len(interactions), "cheque_returns": len(bounces),
        },
    }
