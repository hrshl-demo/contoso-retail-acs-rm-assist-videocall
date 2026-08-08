#!/usr/bin/env python3
"""
Deterministic synthetic dataset generator for Contoso Bank.

Produces one persona-anchored JSON bundle:
    data/contosobank/contosobank_dataset.json

The generator owns every identifier, date, amount, ratio and classification.
Narrative fields are intentionally left blank for enrich_contosobank.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


CODENAME = "CONTOSOBANK-SYN v1.0"
SCHEMA_VERSION = "contosobank-persona-slice-1.0"
DEFAULT_SEED = 20260331
DEMO_TODAY = "2026-03-31"
GENERATED_AT = "2026-04-01T09:00:00+05:30"
WINDOW_START = date(2025, 10, 1)
WINDOW_END = date(2026, 3, 31)
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "contosobank_dataset.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_contosobank")


def r2(value):
    return round(float(value) + 1e-9, 2)


def iso(value):
    return value.isoformat() if isinstance(value, date) else str(value)


def d(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def stable_int(text, modulo):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % modulo


def bizdays(start=WINDOW_START, end=WINDOW_END):
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def amount_band(rng, low, high):
    return r2(rng.uniform(low, high))


def txn_timestamp(txn_date, seq, rail):
    base = {
        "RTGS": 10,
        "NEFT": 11,
        "UPI": 13,
        "CARD_POS": 18,
        "CARD_ECOM": 20,
        "NACH": 9,
        "INTERNAL": 15,
        "CHEQUE": 12,
        "FOREX_WIRE": 14,
        "IMPS": 16,
    }.get(rail, 12)
    minute = (seq * 7 + stable_int(rail + str(txn_date), 50)) % 60
    return f"{iso(txn_date)}T{base:02d}:{minute:02d}:00+05:30"


def make_txns(account, flows):
    """Compute running balances and update account.current_balance_inr."""
    rows = []
    bal = float(account.get("opening_balance_inr", 0))
    semantics = account.get("balance_semantics", "DEPOSIT_BALANCE")
    for seq, flow in enumerate(sorted(flows, key=lambda f: (f["txn_date"], f.get("sort", 0))), 1):
        amt = r2(flow["amount_inr"])
        if semantics == "CARD_OUTSTANDING":
            bal = bal + amt if flow["direction"] == "DR" else bal - amt
        else:
            bal = bal - amt if flow["direction"] == "DR" else bal + amt
        bal = r2(bal)
        txn_id = f"TXN-{account['account_id'][-6:]}-{seq:05d}"
        rows.append({
            "txn_id": txn_id,
            "account_id": account["account_id"],
            "cust_id": account["cust_id"],
            "txn_date": iso(flow["txn_date"]),
            "txn_timestamp": txn_timestamp(flow["txn_date"], seq, flow["rail"]),
            "value_date": iso(flow.get("value_date", flow["txn_date"])),
            "rail": flow["rail"],
            "direction": flow["direction"],
            "amount_inr": amt,
            "running_balance_inr": bal,
            "counterparty_name": flow.get("counterparty_name", ""),
            "counterparty_account_masked": flow.get("counterparty_account_masked", ""),
            "counterparty_ifsc": flow.get("counterparty_ifsc", ""),
            "merchant_category_code": flow.get("merchant_category_code", ""),
            "narration": flow.get("narration", ""),
            "channel": flow.get("channel", "MOBILE"),
            "is_reversal": False,
            "reversal_ref_txn_id": None,
            "festival_flag": flow.get("festival_flag", ""),
            "quarter_end_flag": flow["txn_date"] in (date(2025, 12, 31), date(2026, 3, 31)),
        })
    account["current_balance_inr"] = r2(bal)
    if "card" in account:
        account["card"]["current_outstanding_inr"] = r2(bal)
        account["card"]["min_due_inr"] = r2(max(500, bal * 0.05)) if bal > 0 else 0.0
    if "deposit" in account:
        account["deposit"]["mab_inr"] = r2((account.get("opening_balance_inr", 0) + bal) / 2)
    return rows


def branch_lookup(reference, branch_id):
    return next(b for b in reference["branches"] if b["branch_id"] == branch_id)


def product_lookup(reference, product_id):
    return next(p for p in reference["products_catalog"] if p["product_id"] == product_id)


def account(account_id, cust_id, product_id, branch, account_type, subtype, opening, **extra):
    node = {
        "account_id": account_id,
        "cust_id": cust_id,
        "product_id": product_id,
        "product_name": extra.pop("product_name"),
        "account_type": account_type,
        "account_subtype": subtype,
        "branch_id": branch["branch_id"],
        "ifsc_code": branch["ifsc_code"],
        "open_date": extra.pop("open_date", "2020-04-01"),
        "close_date": None,
        "currency": extra.pop("currency", "INR"),
        "account_status": extra.pop("account_status", "ACTIVE"),
        "opening_balance_inr": r2(opening),
        "current_balance_inr": r2(opening),
        "balance_semantics": extra.pop("balance_semantics", "DEPOSIT_BALANCE"),
    }
    node.update(extra)
    return node


def make_interaction(iid, cust_id, rm_id, when, channel, purpose, outcome, minutes, sentiment, **extra):
    node = {
        "interaction_id": iid,
        "cust_id": cust_id,
        "rm_id": rm_id,
        "interaction_date": when,
        "channel": channel,
        "duration_minutes": minutes,
        "direction": extra.pop("direction", "OUTBOUND"),
        "purpose_code": purpose,
        "outcome_code": outcome,
        "sentiment_score": sentiment,
        "next_action_code": extra.pop("next_action_code", ""),
        "next_action_due_date": extra.pop("next_action_due_date", None),
        "linked_opportunity_id": extra.pop("linked_opportunity_id", None),
        "linked_ticket_id": extra.pop("linked_ticket_id", None),
        "note": "",
        "note_quality_flag": extra.pop("note_quality_flag", "OK"),
    }
    node.update(extra)
    return node


def make_email_thread(thread_id, cust_id, rm_id, subject, start_date, participants, messages):
    return {
        "thread_id": thread_id,
        "cust_id": cust_id,
        "rm_id": rm_id,
        "subject": subject,
        "thread_start_date": start_date,
        "message_count": len(messages),
        "participants": participants,
        "resolution_status": "OPEN" if any(m.get("status") == "OPEN" for m in messages) else "RESOLVED",
        "thread_summary": "",
        "messages": [
            {
                "message_id": f"{thread_id}-M{idx:02d}",
                "thread_id": thread_id,
                "sender_role": msg["sender_role"],
                "sent_timestamp": msg["sent_timestamp"],
                "body_text": "",
                "has_attachment": msg.get("has_attachment", False),
                "attachment_doc_id": msg.get("attachment_doc_id"),
                "deterministic_intent": msg["intent"],
            }
            for idx, msg in enumerate(messages, 1)
        ],
    }


def make_meeting(summary_id, interaction_id, date_text, attendees, linked_items):
    return {
        "summary_id": summary_id,
        "interaction_id": interaction_id,
        "meeting_date": date_text,
        "agenda_text": "",
        "discussion_summary": "",
        "decisions": [],
        "action_items": [],
        "attendees": attendees,
        "linked_items": linked_items,
        "follow_up_date": None,
    }


def make_service_ticket(ticket_id, cust_id, account_id, raised_date, category, sub_category, priority, resolved_at,
                        reopened_count=0, status="RESOLVED", linked_txn_id=None):
    raised = datetime.strptime(raised_date + "T10:00:00", "%Y-%m-%dT%H:%M:%S")
    resolved = datetime.strptime(resolved_at + "T16:30:00", "%Y-%m-%dT%H:%M:%S") if resolved_at else None
    hours = int(((resolved or raised) - raised).total_seconds() // 3600)
    return {
        "ticket_id": ticket_id,
        "cust_id": cust_id,
        "account_id": account_id,
        "raised_date": raised_date,
        "channel": "MOBILE_APP",
        "category": category,
        "sub_category": sub_category,
        "priority": priority,
        "sla_hours": 72 if priority == "HIGH" else 120,
        "first_response_at": f"{raised_date}T12:10:00+05:30",
        "resolved_at": f"{resolved_at}T16:30:00+05:30" if resolved_at else None,
        "actual_resolution_hours": hours,
        "sla_breach_flag": hours > (72 if priority == "HIGH" else 120),
        "reopened_count": reopened_count,
        "status": status,
        "assigned_team": "Retail Operations" if category != "CMS" else "Transaction Banking Operations",
        "linked_txn_id": linked_txn_id,
        "complaint_narrative": {
            "customer_verbatim": "",
            "agent_notes": "",
            "resolution_note": "",
            "root_cause_code": "",
            "emotion_label": "",
            "escalation_language_flag": False,
        },
    }


def make_opportunity(opp_id, cust_id, rm_id, product_id, source, created, stage, value, probability, close_date,
                     status="OPEN", suitability=True, loss_reason_code=None):
    return {
        "opp_id": opp_id,
        "cust_id": cust_id,
        "rm_id": rm_id,
        "product_id": product_id,
        "source": source,
        "created_date": created,
        "stage": stage,
        "expected_value_inr": r2(value),
        "probability_pct": probability,
        "expected_close_date": close_date,
        "actual_close_date": None if status == "OPEN" else close_date,
        "win_loss_status": status,
        "loss_reason_code": loss_reason_code,
        "reason": "",
        "suitability_checked_flag": suitability,
        "suitability_note": "",
        "loss_reason_text": "" if status == "LOST" else None,
    }


def make_offer(resp_id, opp_id, cust_id, offer_date, channel, response, response_date=None):
    return {
        "response_id": resp_id,
        "opp_id": opp_id,
        "cust_id": cust_id,
        "offer_date": offer_date,
        "offer_channel": channel,
        "response": response,
        "response_date": response_date,
        "decline_reason_text": "" if response == "DECLINED" else None,
    }


def build_reference():
    branches = [
        {"branch_id": "BR0101", "branch_name": "Kalyani Nagar Priority Branch", "ifsc_code": "CTBK0000101",
         "micr_code": "411999101", "city": "Pune", "state": "Maharashtra", "region": "West",
         "branch_tier": "METRO", "cluster_id": "CL-W01", "opened_date": "2008-06-16"},
        {"branch_id": "BR0234", "branch_name": "Tiruppur Industrial Cluster Branch", "ifsc_code": "CTBK0000234",
         "micr_code": "641999234", "city": "Tiruppur", "state": "Tamil Nadu", "region": "South",
         "branch_tier": "URBAN", "cluster_id": "CL-S07", "opened_date": "2004-03-22"},
        {"branch_id": "BR0308", "branch_name": "BKC Corporate Banking Branch", "ifsc_code": "CTBK0000308",
         "micr_code": "400999308", "city": "Mumbai", "state": "Maharashtra", "region": "West",
         "branch_tier": "METRO", "cluster_id": "CL-W09", "opened_date": "1999-11-08"},
    ]
    products = [
        ("PRD00001", "DEPOSIT", "Contoso Salary Plus Savings", "RETAIL", False, None, 0, 25000000, "FIXED"),
        ("PRD00002", "DEPOSIT", "Contoso Priority Savings", "RETAIL", False, None, 0, 100000000, "FIXED"),
        ("PRD00003", "DEPOSIT", "Contoso Fixed Deposit", "ALL", False, "LOW", 10000, 500000000, "FIXED"),
        ("PRD00004", "LOAN", "Contoso Home Loan", "RETAIL", False, None, 1500000, 50000000, "REPO_LINKED"),
        ("PRD00005", "CARD", "Contoso Signature Credit Card", "RETAIL", False, None, 40000, 2500000, "FEE_ONLY"),
        ("PRD00006", "INVESTMENT", "Contoso Mutual Fund Platform", "RETAIL", True, "MODERATE", 500, 400000000, "FEE_ONLY"),
        ("PRD00007", "INVESTMENT", "Contoso ELSS Tax Saver", "RETAIL", True, "HIGH", 500, 1500000, "FEE_ONLY"),
        ("PRD00008", "LOAN", "Contoso Education Loan", "RETAIL", False, None, 50000, 15000000, "MCLR_LINKED"),
        ("PRD00009", "DEPOSIT", "Contoso Business Current Account", "MSME", False, None, 0, 500000000, "FIXED"),
        ("PRD00010", "LOAN", "Contoso Cash Credit", "MSME", False, None, 1000000, 350000000, "MCLR_LINKED"),
        ("PRD00011", "LOAN", "Contoso Business Term Loan", "MSME", False, None, 1000000, 500000000, "MCLR_LINKED"),
        ("PRD00012", "TRADE", "Contoso Import Letter of Credit", "MSME", False, None, 500000, 300000000, "FEE_ONLY"),
        ("PRD00013", "TRADE", "Contoso Bank Guarantee", "MSME", False, None, 500000, 200000000, "FEE_ONLY"),
        ("PRD00014", "CMS", "Contoso Dealer CMS", "CORPORATE", False, None, 0, 5000000000, "FEE_ONLY"),
        ("PRD00015", "LOAN", "Contoso Working Capital Consortium", "CORPORATE", False, None, 50000000, 25000000000, "TBILL_LINKED"),
        ("PRD00016", "TRADE", "Contoso Corporate LC Line", "CORPORATE", False, None, 5000000, 5000000000, "FEE_ONLY"),
        ("PRD00017", "FOREX", "Contoso Forward Cover Line", "CORPORATE", False, None, 1000000, 4000000000, "FEE_ONLY"),
        ("PRD00018", "DERIVATIVE", "Contoso Interest Rate Swap Line", "CORPORATE", False, None, 1000000, 4000000000, "FEE_ONLY"),
        ("PRD00019", "CMS", "Contoso Payment Gateway Collections", "MSME", False, None, 0, 100000000, "FEE_ONLY"),
        ("PRD00020", "LOAN", "Contoso Supply Chain Finance", "CORPORATE", False, None, 5000000, 3000000000, "MCLR_LINKED"),
    ]
    catalog = [{
        "product_id": pid,
        "product_family": fam,
        "product_name": name,
        "segment_applicability": seg,
        "is_third_party": third,
        "risk_grade": risk,
        "min_ticket_inr": r2(mn),
        "max_ticket_inr": r2(mx),
        "pricing_basis": pricing,
        "launch_date": "2018-04-01",
        "sunset_date": None,
        "blurb": "",
    } for pid, fam, name, seg, third, risk, mn, mx, pricing in products]
    rules = [
        {"product_id": "PRD00006", "fit_signals": "valid_risk_profile;goal_based_investing",
         "blocking_signals": "open_complaint;expired_suitability;prior_decline_unaddressed"},
        {"product_id": "PRD00007", "fit_signals": "tax_saving_need;valid_risk_profile",
         "blocking_signals": "risk_profile_conservative;no_suitability_note"},
        {"product_id": "PRD00008", "fit_signals": "child_education_goal;declared_income_supports_foir",
         "blocking_signals": "kyc_overdue;foir_above_55"},
        {"product_id": "PRD00010", "fit_signals": "working_capital_cycle;stock_statement_current",
         "blocking_signals": "stale_collateral;renewal_overdue;sma_active"},
        {"product_id": "PRD00019", "fit_signals": "upi_collections;ecommerce_push",
         "blocking_signals": "unresolved_current_account_complaint"},
        {"product_id": "PRD00020", "fit_signals": "dealer_payment_pattern;repeat_dealers",
         "blocking_signals": "fee_dispute_unresolved;information_barrier_unresolved"},
    ]
    calendar = [
        {"date": "2025-10-18", "fy_quarter": "FY2026-Q3", "festival_flag": "DHANTERAS", "festival_intensity": 1.0,
         "is_working_day": False, "is_quarter_end": False, "is_fy_end": False},
        {"date": "2025-10-20", "fy_quarter": "FY2026-Q3", "festival_flag": "DIWALI", "festival_intensity": 1.0,
         "is_working_day": False, "is_quarter_end": False, "is_fy_end": False},
        {"date": "2025-12-31", "fy_quarter": "FY2026-Q3", "festival_flag": "", "festival_intensity": 0.0,
         "is_working_day": True, "is_quarter_end": True, "is_fy_end": False},
        {"date": "2026-03-31", "fy_quarter": "FY2026-Q4", "festival_flag": "FY_END", "festival_intensity": 0.8,
         "is_working_day": True, "is_quarter_end": True, "is_fy_end": True},
    ]
    industries = [
        {"industry_code": "IND-TXT", "nic_code": "14301", "industry_name": "Knitted apparel manufacturing",
         "sector": "Textiles and apparel", "sub_sector": "Garments", "typical_wc_cycle_days": 62},
        {"industry_code": "IND-CHM", "nic_code": "20299", "industry_name": "Speciality chemicals",
         "sector": "Chemicals", "sub_sector": "Intermediates", "typical_wc_cycle_days": 88},
    ]
    schemes = [
        {"scheme_code": "SCH-CGTMSE-S", "scheme_name": "Fictional MSME Credit Guarantee - Small",
         "regulator_body": "Synthetic policy corpus", "eligibility_json": {"segment": "MSME", "max_turnover_cr": 50},
         "guarantee_cover_pct": 75, "max_exposure_inr": 50000000, "is_active": True},
        {"scheme_code": "SCH-PSL-TXT", "scheme_name": "Fictional Priority Sector Textile Cluster Tag",
         "regulator_body": "Synthetic policy corpus", "eligibility_json": {"sector": "Textiles"},
         "guarantee_cover_pct": 0, "max_exposure_inr": 0, "is_active": True},
    ]
    return {
        "bank_name": "Contoso Bank",
        "products_catalog": catalog,
        "product_rules": rules,
        "branches": branches,
        "calendar_key_dates": calendar,
        "industries": industries,
        "schemes": schemes,
        "synthetic_data_notice": "All names, identifiers, accounts, PAN, GSTIN, CIN, IFSC and amounts are fictional synthetic data.",
    }


def build_rms():
    return {
        "RM-2207": {
            "rm_id": "RM-2207",
            "rm_name": "Priya Deshmukh",
            "role": "Priority Relationship Manager",
            "segment": "RETAIL",
            "employee_grade": "M4",
            "branch_id": "BR0101",
            "cluster_id": "CL-W01",
            "reporting_manager_id": "RM-1102",
            "date_joined": "2018-07-09",
            "languages_spoken": ["Marathi", "Hindi", "English"],
            "certification_flags": {"NISM_VA": True, "IRDAI_CA": True},
            "portfolio_stats": {"mapped_households": 268, "customer_ids": 382, "relationship_value_inr": 21400000000},
            "kpis": ["AQB growth", "Third-party fee income", "Cross-sell ratio", "Priority acquisitions", "NPS"],
            "daily_workflow": ["08:45 branch huddle", "09:30 outbound calls", "11:30 client meetings",
                               "14:00 service escalations", "17:30 CRM updates"],
            "bio": "",
            "advisor_brief": "",
            "voice_bio": "",
            "talking_points": [],
            "activity_summary": {"calls": 1190, "visits": 268, "video": 214, "branch_walk_ins": 170},
        },
        "RM-3412": {
            "rm_id": "RM-3412",
            "rm_name": "Arjun Nair",
            "role": "Business Banking Relationship Manager",
            "segment": "MSME",
            "employee_grade": "M5",
            "branch_id": "BR0234",
            "cluster_id": "CL-S07",
            "reporting_manager_id": "RM-3301",
            "date_joined": "2017-05-15",
            "languages_spoken": ["Tamil", "Malayalam", "English", "Hindi"],
            "certification_flags": {"MSME_CREDIT": True, "TRADE_FINANCE": True},
            "portfolio_stats": {"entities": 118, "borrowing_relationships": 74, "sanctioned_limits_inr": 41200000000},
            "kpis": ["Net advances growth", "Current-account float", "Trade fee income", "Renewal timeliness", "SMA migration"],
            "daily_workflow": ["Unit visits", "Stock statement follow-up", "Renewal file preparation", "Credit committee coordination"],
            "bio": "",
            "advisor_brief": "",
            "voice_bio": "",
            "talking_points": [],
            "activity_summary": {"calls": 296, "visits": 318, "video": 128},
        },
        "RM-5104": {
            "rm_id": "RM-5104",
            "rm_name": "Sanjay Malhotra",
            "role": "Senior Corporate Relationship Manager",
            "segment": "CORPORATE",
            "employee_grade": "D2",
            "branch_id": "BR0308",
            "cluster_id": "CL-W09",
            "reporting_manager_id": "RM-5001",
            "date_joined": "2012-09-03",
            "languages_spoken": ["Hindi", "English", "Punjabi"],
            "certification_flags": {"WHOLESALE_CREDIT": True, "TREASURY_PRODUCTS": True},
            "portfolio_stats": {"groups": 22, "legal_entities": 91, "aggregate_sanctioned_inr": 485000000000},
            "kpis": ["Return on RWA", "Wallet share", "NFB fee income", "CASA float", "Annual review timeliness"],
            "daily_workflow": ["CFO meetings", "Pricing approvals", "Credit committee packs", "Product-specialist coordination"],
            "bio": "",
            "advisor_brief": "",
            "voice_bio": "",
            "talking_points": [],
            "activity_summary": {"client_meetings": 142, "internal_calls": 344},
        },
    }


def rajesh_investments():
    values = [
        ("MF-RI-001", "Contoso Bluechip Fund - Direct Growth", "MF_EQUITY", 2600000, 20000, True, "HIGH"),
        ("MF-RI-002", "Contoso Flexi-Cap Fund - Direct Growth", "MF_EQUITY", 2200000, 15000, True, "HIGH"),
        ("MF-RI-003", "Contoso Corporate Bond Fund", "MF_DEBT", 1600000, 10000, True, "LOW"),
        ("MF-RI-004", "Contoso Balanced Advantage Fund", "MF_HYBRID", 1450000, 15000, True, "MODERATE"),
        ("MF-RI-005", "Contoso ELSS Tax Saver", "MF_EQUITY", 1500000, 15000, True, "HIGH"),
        ("MF-RI-006", "Contoso Liquid Fund", "MF_DEBT", 1100000, 10000, True, "LOW"),
        ("MF-RI-007", "Contoso Nifty 50 Index Fund", "MF_EQUITY", 1000000, 0, False, "HIGH"),
        ("MF-RI-008", "Contoso Short Duration Fund", "MF_DEBT", 850000, 0, False, "LOW"),
        ("MF-RI-009", "Contoso Large & Midcap Fund", "MF_EQUITY", 1200000, 0, False, "VERY_HIGH"),
        ("MF-RI-010", "Contoso Gold Savings Fund", "SGB", 650000, 0, False, "MODERATE"),
        ("MF-RI-011", "Contoso Overnight Fund", "MF_DEBT", 450000, 0, False, "LOW"),
        ("MF-RI-012", "Contoso Pharma Opportunities Fund", "MF_EQUITY", 600000, 0, False, "HIGH"),
        ("MF-RI-013", "Contoso Banking ETF", "EQUITY", 650000, 0, False, "HIGH"),
        ("MF-RI-014", "Contoso International FoF", "MF_EQUITY", 550000, 0, False, "VERY_HIGH"),
        ("MF-RI-015", "Contoso Hybrid Equity Fund", "MF_HYBRID", 750000, 0, False, "MODERATE"),
        ("MF-RI-016", "Contoso Children's Education Fund", "MF_HYBRID", 700000, 0, False, "MODERATE"),
        ("MF-RI-017", "Contoso Retirement Advantage Fund", "MF_HYBRID", 450000, 0, False, "MODERATE"),
        ("MF-RI-018", "Contoso Arbitrage Fund", "MF_DEBT", 200000, 0, False, "LOW"),
        ("MF-RI-019", "Contoso Savings Fund", "MF_DEBT", 100000, 0, False, "LOW"),
    ]
    holdings = []
    for idx, (hid, scheme, inst, value, sip, active, risk) in enumerate(values, 1):
        nav = r2(20 + idx * 2.7)
        units = r2(value / nav)
        holdings.append({
            "holding_id": hid,
            "account_id": "1010000000001009",
            "cust_id": "CTB-RTL-001",
            "instrument_type": inst,
            "scheme_name": scheme,
            "folio_number": f"CTB-RI-{idx:05d}",
            "units": units,
            "nav_inr": nav,
            "market_value_inr": r2(value),
            "cost_value_inr": r2(value * (0.82 + (idx % 5) * 0.025)),
            "purchase_date": "2024-03-15" if idx <= 6 else "2022-09-12",
            "sip_flag": active,
            "sip_amount_inr": sip,
            "sip_status": "ACTIVE" if active else "INACTIVE",
            "risk_grade": risk,
            "suitability_checked_flag": not (scheme == "Contoso ELSS Tax Saver"),
        })
    return holdings


def build_rajesh(reference, rng):
    cid = "CTB-RTL-001"
    rm_id = "RM-2207"
    branch = branch_lookup(reference, "BR0101")
    accounts = [
        account("1010000000001001", cid, "PRD00001", branch, "DEPOSIT", "SAVINGS", 420000,
                product_name="Contoso Salary Plus Savings", open_date="2011-04-18",
                deposit={"deposit_subtype": "SAVINGS", "interest_rate_pct": 3.0, "aqb_inr": 480000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": True,
                         "nominee_registered": True}),
        account("1010000000001002", cid, "PRD00002", branch, "DEPOSIT", "SAVINGS", 185000,
                product_name="Contoso Priority Savings", open_date="2014-10-03",
                deposit={"deposit_subtype": "SAVINGS", "interest_rate_pct": 3.0, "aqb_inr": 220000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False,
                         "nominee_registered": True}),
        account("1010000000001003", cid, "PRD00003", branch, "DEPOSIT", "FD", 2200000,
                product_name="Contoso Fixed Deposit", open_date="2024-07-12",
                deposit={"deposit_subtype": "FD", "interest_rate_pct": 7.15, "aqb_inr": 2200000,
                         "maturity_date": "2026-07-12", "auto_renew_flag": True, "sweep_flag": False,
                         "nominee_registered": True}),
        account("1010000000001004", cid, "PRD00003", branch, "DEPOSIT", "FD", 1800000,
                product_name="Contoso Fixed Deposit", open_date="2025-01-22",
                deposit={"deposit_subtype": "FD", "interest_rate_pct": 7.05, "aqb_inr": 1800000,
                         "maturity_date": "2027-01-22", "auto_renew_flag": True, "sweep_flag": False,
                         "nominee_registered": True}),
        account("1010000000001005", cid, "PRD00003", branch, "DEPOSIT", "FD", 2200000,
                product_name="Contoso Fixed Deposit", open_date="2026-01-30",
                deposit={"deposit_subtype": "FD", "interest_rate_pct": 6.9, "aqb_inr": 2200000,
                         "maturity_date": "2027-01-30", "auto_renew_flag": False, "sweep_flag": False,
                         "nominee_registered": True}),
        account("1010000000001006", cid, "PRD00004", branch, "LOAN", "HOME", 14200000,
                product_name="Contoso Home Loan", open_date="2019-06-05", balance_semantics="LOAN_OUTSTANDING",
                loan={"loan_subtype": "HOME", "sanctioned_amount_inr": 21000000, "disbursed_amount_inr": 21000000,
                      "outstanding_inr": 14200000, "interest_rate_pct": 8.65, "rate_type": "REPO_LINKED",
                      "tenor_months": 240, "emi_inr": 154200, "first_emi_date": "2019-07-05",
                      "maturity_date": "2039-06-05", "dpd_days": 0, "asset_classification": "STANDARD",
                      "restructure_flag": False, "insurance_linked_flag": True}),
        account("1010000000001007", cid, "PRD00005", branch, "CARD", "CREDIT_CARD", 30000,
                product_name="Contoso Signature Credit Card", open_date="2016-08-19",
                balance_semantics="CARD_OUTSTANDING",
                card={"card_variant": "SIGNATURE", "credit_limit_inr": 1200000, "current_outstanding_inr": 30000,
                      "min_due_inr": 1500, "statement_date": "2026-03-12", "due_date": "2026-04-02",
                      "revolve_flag": True, "revolve_cycle_month": "2025-11", "reward_points_balance": 48210,
                      "last_limit_review_date": "2025-05-16"}),
        account("1010000000001008", cid, "PRD00005", branch, "CARD", "CREDIT_CARD", 12000,
                product_name="Contoso Signature Credit Card", open_date="2021-02-11",
                balance_semantics="CARD_OUTSTANDING",
                card={"card_variant": "PLATINUM", "credit_limit_inr": 600000, "current_outstanding_inr": 12000,
                      "min_due_inr": 600, "statement_date": "2026-03-18", "due_date": "2026-04-08",
                      "revolve_flag": False, "reward_points_balance": 14110,
                      "last_limit_review_date": "2025-08-21"}),
        account("1010000000001009", cid, "PRD00006", branch, "INVESTMENT", "MF_UMBRELLA", 18600000,
                product_name="Contoso Mutual Fund Platform", open_date="2015-09-01",
                investment_summary={"market_value_inr": 18600000, "schemes_count": 19, "active_sips": 6,
                                    "monthly_sip_inr": 85000}),
    ]
    salary = next(a for a in accounts if a["account_id"].endswith("1001"))
    joint = next(a for a in accounts if a["account_id"].endswith("1002"))
    card1 = next(a for a in accounts if a["account_id"].endswith("1007"))
    card2 = next(a for a in accounts if a["account_id"].endswith("1008"))

    salary_flows = []
    salary_dates = ["2025-10-31", "2025-11-28", "2025-12-31", "2026-01-30", "2026-02-27", "2026-03-31"]
    for idx, day in enumerate(salary_dates, 1):
        salary_flows.append({"txn_date": d(day), "rail": "NEFT", "direction": "CR", "amount_inr": 566667,
                             "counterparty_name": "Fictional IT Services Employer", "narration": "Monthly salary credit",
                             "channel": "BULK_UPLOAD", "sort": idx})
    salary_flows += [
        {"txn_date": d("2025-12-19"), "rail": "NEFT", "direction": "CR", "amount_inr": 1800000,
         "counterparty_name": "Fictional IT Services Employer", "narration": "Annual performance bonus",
         "channel": "BULK_UPLOAD", "sort": 10},
        {"txn_date": d("2026-01-30"), "rail": "INTERNAL", "direction": "DR", "amount_inr": 2200000,
         "counterparty_name": "Contoso Bank Fixed Deposit", "narration": "FD booking from idle bonus after 41 days",
         "channel": "NET", "sort": 20},
        {"txn_date": d("2026-03-27"), "rail": "INTERNAL", "direction": "DR", "amount_inr": 150000,
         "counterparty_name": "Contoso ELSS Tax Saver", "narration": "FY-end ELSS investment order",
         "channel": "NET", "festival_flag": "FY_END", "sort": 21},
    ]
    for day in ["2025-10-06", "2025-11-14", "2025-12-05", "2026-01-05", "2026-02-05", "2026-03-05"]:
        salary_flows.append({"txn_date": d(day), "rail": "NACH", "direction": "DR", "amount_inr": 154200,
                             "counterparty_name": "Contoso Home Loan", "narration": "Home loan EMI debit",
                             "channel": "NACH", "sort": 30})
    salary_flows.append({"txn_date": d("2025-11-05"), "rail": "NACH", "direction": "DR", "amount_inr": 590,
                         "counterparty_name": "Contoso Bank", "narration": "NACH return charge - home loan EMI",
                         "channel": "NACH", "sort": 31})
    for day in ["2025-10-10", "2025-11-10", "2025-12-10", "2026-01-10", "2026-02-10", "2026-03-10"]:
        salary_flows.append({"txn_date": d(day), "rail": "NACH", "direction": "DR", "amount_inr": 85000,
                             "counterparty_name": "Contoso Mutual Fund SIP", "narration": "Monthly SIP basket",
                             "channel": "NACH", "sort": 40})
    for day, amt in [("2025-11-20", 84000), ("2025-12-20", 380000), ("2026-01-20", 125000),
                     ("2026-02-20", 112000), ("2026-03-20", 143000)]:
        salary_flows.append({"txn_date": d(day), "rail": "INTERNAL", "direction": "DR", "amount_inr": amt,
                             "counterparty_name": "Contoso Credit Card", "narration": "Credit card payment",
                             "channel": "MOBILE", "sort": 50})
    for month in range(10, 13):
        for n in range(7):
            day = date(2025, month, min(24, 3 + n * 3))
            salary_flows.append({"txn_date": day, "rail": "UPI", "direction": "DR",
                                 "amount_inr": amount_band(rng, 650, 4200),
                                 "counterparty_name": f"Pune Merchant {n+1}",
                                 "counterparty_ifsc": "CTBK0000101", "merchant_category_code": "5411",
                                 "narration": "UPI household spend", "sort": 80 + n})
    for month in range(1, 4):
        for n in range(7):
            day = date(2026, month, min(25, 2 + n * 3))
            salary_flows.append({"txn_date": day, "rail": "UPI", "direction": "DR",
                                 "amount_inr": amount_band(rng, 750, 5200),
                                 "counterparty_name": f"Pune Merchant {n+8}",
                                 "merchant_category_code": "5812", "narration": "UPI household spend",
                                 "sort": 80 + n})

    joint_flows = []
    for day in ["2025-10-03", "2025-11-03", "2025-12-03", "2026-01-03", "2026-02-03", "2026-03-03"]:
        joint_flows.append({"txn_date": d(day), "rail": "INTERNAL", "direction": "CR", "amount_inr": 60000,
                            "counterparty_name": "Rajesh Iyer Salary Account", "narration": "Household transfer",
                            "channel": "MOBILE", "sort": 1})
    for day in ["2025-10-08", "2025-11-08", "2025-12-08", "2026-01-08", "2026-02-08", "2026-03-08"]:
        joint_flows.append({"txn_date": d(day), "rail": "NACH", "direction": "DR", "amount_inr": 42000,
                            "counterparty_name": "Apartment Association", "narration": "Maintenance and utilities",
                            "channel": "NACH", "sort": 2})

    card1_flows = [
        {"txn_date": d("2025-10-14"), "rail": "CARD_ECOM", "direction": "DR", "amount_inr": 115000,
         "counterparty_name": "Fictional Electronics Mall", "merchant_category_code": "5732",
         "narration": "Festive electronics purchase", "festival_flag": "DIWALI", "channel": "POS", "sort": 1},
        {"txn_date": d("2025-10-17"), "rail": "CARD_POS", "direction": "DR", "amount_inr": 90000,
         "counterparty_name": "Fictional Jewellery House", "merchant_category_code": "5944",
         "narration": "Dhanteras jewellery purchase", "festival_flag": "DHANTERAS", "channel": "POS", "sort": 2},
        {"txn_date": d("2025-10-19"), "rail": "CARD_ECOM", "direction": "DR", "amount_inr": 85000,
         "counterparty_name": "Fictional Travel Portal", "merchant_category_code": "4722",
         "narration": "Family holiday booking", "festival_flag": "DIWALI", "channel": "NET", "sort": 3},
        {"txn_date": d("2025-10-21"), "rail": "CARD_POS", "direction": "DR", "amount_inr": 70000,
         "counterparty_name": "Fictional Home Store", "merchant_category_code": "5712",
         "narration": "Festive home furnishings", "festival_flag": "DIWALI", "channel": "POS", "sort": 4},
        {"txn_date": d("2025-10-24"), "rail": "CARD_ECOM", "direction": "DR", "amount_inr": 60000,
         "counterparty_name": "Fictional App Store", "merchant_category_code": "5817",
         "narration": "Festive online purchases", "festival_flag": "DIWALI", "channel": "NET", "sort": 5},
    ]
    for day, amt in [("2025-11-20", 84000), ("2025-12-20", 380000), ("2026-01-20", 125000),
                     ("2026-02-20", 112000), ("2026-03-20", 143000)]:
        card1_flows.append({"txn_date": d(day), "rail": "INTERNAL", "direction": "CR", "amount_inr": amt,
                            "counterparty_name": "Rajesh Iyer Salary Account", "narration": "Card payment received",
                            "channel": "MOBILE", "sort": 70})
    for month in [11, 12]:
        for n in range(4):
            card1_flows.append({"txn_date": date(2025, month, 4 + n * 5), "rail": "CARD_POS", "direction": "DR",
                                "amount_inr": amount_band(rng, 9000, 26000), "counterparty_name": f"Card Merchant {month}-{n}",
                                "merchant_category_code": "5812", "narration": "Card purchase", "channel": "POS", "sort": 20 + n})
    for month in [1, 2, 3]:
        for n in range(5):
            card1_flows.append({"txn_date": date(2026, month, 3 + n * 5), "rail": "CARD_ECOM", "direction": "DR",
                                "amount_inr": amount_band(rng, 7000, 23000), "counterparty_name": f"Online Merchant {month}-{n}",
                                "merchant_category_code": "5817", "narration": "Card e-commerce purchase",
                                "channel": "NET", "sort": 20 + n})
    card2_flows = []
    for month in [10, 11, 12]:
        for n in range(3):
            card2_flows.append({"txn_date": date(2025, month, 6 + n * 7), "rail": "CARD_POS", "direction": "DR",
                                "amount_inr": amount_band(rng, 3000, 14000), "counterparty_name": f"Family Merchant {month}-{n}",
                                "merchant_category_code": "5411", "narration": "Secondary card purchase",
                                "channel": "POS", "sort": n})
    for month in [1, 2, 3]:
        for n in range(3):
            card2_flows.append({"txn_date": date(2026, month, 5 + n * 6), "rail": "CARD_ECOM", "direction": "DR",
                                "amount_inr": amount_band(rng, 2500, 12000), "counterparty_name": f"Family Online {month}-{n}",
                                "merchant_category_code": "5942", "narration": "Secondary card e-commerce purchase",
                                "channel": "NET", "sort": n})
    for day, amt in [("2025-11-19", 39000), ("2025-12-19", 36000), ("2026-01-19", 31000),
                     ("2026-02-19", 28000), ("2026-03-19", 26000)]:
        card2_flows.append({"txn_date": d(day), "rail": "INTERNAL", "direction": "CR", "amount_inr": amt,
                            "counterparty_name": "Rajesh Iyer Salary Account", "narration": "Secondary card payment",
                            "channel": "MOBILE", "sort": 90})

    transactions = []
    transactions.extend(make_txns(salary, salary_flows))
    transactions.extend(make_txns(joint, joint_flows))
    transactions.extend(make_txns(card1, card1_flows))
    transactions.extend(make_txns(card2, card2_flows))

    repayment_schedule = []
    for idx, due in enumerate(["2025-10-05", "2025-11-05", "2025-12-05", "2026-01-05", "2026-02-05", "2026-03-05"], 1):
        paid = "2025-11-14" if due == "2025-11-05" else (d(due) + timedelta(days=1)).isoformat()
        repayment_schedule.append({
            "schedule_id": f"RPS-RI-{idx:02d}",
            "account_id": "1010000000001006",
            "instalment_no": 76 + idx,
            "due_date": due,
            "principal_due_inr": 52000,
            "interest_due_inr": 102200,
            "total_due_inr": 154200,
            "paid_date": paid,
            "paid_amount_inr": 154200,
            "payment_status": "PAID_LATE" if due == "2025-11-05" else "PAID",
            "dpd_days": 9 if due == "2025-11-05" else 0,
        })

    interactions = [
        make_interaction("INT-RTL-001", cid, rm_id, "2025-10-22", "CALL", "FESTIVE_CARD_SPEND", "ADVICE_GIVEN", 18, 0.35),
        make_interaction("INT-RTL-002", cid, rm_id, "2025-11-05", "CALL", "NACH_FAILURE", "ESCALATED", 24, -0.55,
                         linked_ticket_id="TCK-RTL-001"),
        make_interaction("INT-RTL-003", cid, rm_id, "2025-11-14", "CALL", "COMPLAINT_CLOSURE", "RESOLVED", 16, -0.15,
                         linked_ticket_id="TCK-RTL-001"),
        make_interaction("INT-RTL-004", cid, rm_id, "2025-12-20", "EMAIL", "BONUS_CREDIT", "FOLLOW_UP_SET", 0, 0.10,
                         linked_opportunity_id="OPP-RTL-001"),
        make_interaction("INT-RTL-005", cid, rm_id, "2026-01-08", "VIDEO", "EDUCATION_LOAN_ENQUIRY", "QUALIFIED", 32, 0.25,
                         linked_opportunity_id="OPP-RTL-002"),
        make_interaction("INT-RTL-006", cid, rm_id, "2026-01-30", "BRANCH", "FD_BOOKING", "WON", 28, 0.42,
                         linked_opportunity_id="OPP-RTL-001"),
        make_interaction("INT-RTL-007", cid, rm_id, "2026-02-14", "WHATSAPP_BUSINESS", "KYC_DUE", "DOCUMENTS_PENDING", 5, -0.05,
                         linked_ticket_id="TCK-RTL-002"),
        make_interaction("INT-RTL-008", cid, rm_id, "2026-02-21", "CALL", "KYC_REMINDER", "FOLLOW_UP_SET", 11, 0.0,
                         linked_ticket_id="TCK-RTL-002", note_quality_flag="THIN"),
        make_interaction("INT-RTL-009", cid, rm_id, "2026-03-18", "EMAIL", "TAX_SAVER", "PITCHED", 0, 0.08,
                         linked_opportunity_id="OPP-RTL-005"),
        make_interaction("INT-RTL-010", cid, rm_id, "2026-03-27", "CALL", "ELSS_EXECUTION", "WON", 14, 0.12,
                         linked_opportunity_id="OPP-RTL-005"),
        make_interaction("INT-RTL-011", cid, rm_id, "2026-03-29", "CALL", "SUITABILITY_GAP", "CONTROL_FLAGGED", 12, -0.1),
        make_interaction("INT-RTL-012", cid, rm_id, "2025-12-05", "CALL", "CARD_LIMIT_REVIEW", "DECLINED", 9, 0.05,
                         linked_opportunity_id="OPP-RTL-003"),
        make_interaction("INT-RTL-013", cid, rm_id, "2026-02-04", "VIDEO", "ANNUAL_REVIEW", "FOLLOW_UP_SET", 42, 0.28),
        make_interaction("INT-RTL-014", cid, rm_id, "2026-03-10", "CALL", "INSURANCE_REVIEW", "DEFERRED", 15, 0.02,
                         linked_opportunity_id="OPP-RTL-006"),
    ]
    meetings = [
        make_meeting("MS-RI-001", "INT-RTL-005", "2026-01-08",
                     ["Rajesh Iyer", "Priya Deshmukh"], ["PRD00008", "1010000000001006"]),
        make_meeting("MS-RI-002", "INT-RTL-013", "2026-02-04",
                     ["Rajesh Iyer", "Priya Deshmukh"], ["MF-RI-001", "MF-RI-005"]),
    ]
    emails = [
        make_email_thread("EM-RI-001", cid, rm_id, "Home loan NACH failure escalation", "2025-11-05",
                          ["Rajesh Iyer", "Priya Deshmukh", "Retail Operations"],
                          [{"sender_role": "CUSTOMER", "sent_timestamp": "2025-11-05T11:04:00+05:30", "intent": "complaint raised"},
                           {"sender_role": "RM", "sent_timestamp": "2025-11-05T11:31:00+05:30", "intent": "acknowledgement"},
                           {"sender_role": "OPERATIONS", "sent_timestamp": "2025-11-12T15:20:00+05:30", "intent": "resolution"},
                           {"sender_role": "CUSTOMER", "sent_timestamp": "2025-11-13T09:10:00+05:30", "intent": "reopening"},
                           {"sender_role": "RM", "sent_timestamp": "2025-11-14T17:10:00+05:30", "intent": "closure"}]),
        make_email_thread("EM-RI-002", cid, rm_id, "Education loan options for elder child", "2026-01-08",
                          ["Rajesh Iyer", "Priya Deshmukh"],
                          [{"sender_role": "CUSTOMER", "sent_timestamp": "2026-01-08T18:20:00+05:30", "intent": "education loan enquiry"},
                           {"sender_role": "RM", "sent_timestamp": "2026-01-09T10:30:00+05:30", "intent": "documents and indicative terms"}]),
        make_email_thread("EM-RI-003", cid, rm_id, "KYC periodic update", "2026-02-14",
                          ["Priya Deshmukh", "Rajesh Iyer"],
                          [{"sender_role": "RM", "sent_timestamp": "2026-02-14T09:15:00+05:30", "intent": "KYC due reminder"},
                           {"sender_role": "CUSTOMER", "sent_timestamp": "2026-02-15T20:43:00+05:30", "intent": "requests mobile route"}]),
        make_email_thread("EM-RI-004", cid, rm_id, "Tax-saving investment discussion", "2026-03-18",
                          ["Priya Deshmukh", "Rajesh Iyer"],
                          [{"sender_role": "RM", "sent_timestamp": "2026-03-18T11:00:00+05:30", "intent": "ELSS option"},
                           {"sender_role": "CUSTOMER", "sent_timestamp": "2026-03-26T22:12:00+05:30", "intent": "urgent execution"}]),
    ]
    tickets = [
        make_service_ticket("TCK-RTL-001", cid, "1010000000001006", "2025-11-05", "LOAN_SERVICING",
                            "NACH_EMI_FAILURE", "HIGH", "2025-11-14", reopened_count=1),
        make_service_ticket("TCK-RTL-002", cid, None, "2026-02-14", "KYC", "PERIODIC_UPDATE_DUE", "MEDIUM",
                            "2026-03-03", reopened_count=0),
    ]
    opportunities = [
        make_opportunity("OPP-RTL-001", cid, rm_id, "PRD00003", "MODEL_GENERATED", "2025-12-20",
                         "WON", 2200000, 85, "2026-01-30", status="WON"),
        make_opportunity("OPP-RTL-002", cid, rm_id, "PRD00008", "INBOUND", "2026-01-08",
                         "QUALIFIED", 4500000, 45, "2026-04-15", status="OPEN"),
        make_opportunity("OPP-RTL-003", cid, rm_id, "PRD00005", "CAMPAIGN", "2025-12-05",
                         "LOST", 0, 20, "2025-12-05", status="LOST", loss_reason_code="CUSTOMER_DECLINED_LIMIT_REVIEW"),
        make_opportunity("OPP-RTL-004", cid, rm_id, "PRD00006", "MODEL_GENERATED", "2026-01-31",
                         "DEFERRED", 900000, 35, "2026-04-30", status="OPEN"),
        make_opportunity("OPP-RTL-005", cid, rm_id, "PRD00007", "CAMPAIGN", "2026-03-18",
                         "WON", 150000, 90, "2026-03-27", status="WON", suitability=False),
        make_opportunity("OPP-RTL-006", cid, rm_id, "PRD00006", "RM_SOURCED", "2026-03-10",
                         "LOST", 500000, 25, "2026-03-10", status="LOST", loss_reason_code="WANTS_NO_NEW_RISK"),
        make_opportunity("OPP-RTL-007", cid, rm_id, "PRD00002", "REFERRAL", "2026-02-04",
                         "OPEN", 0, 30, "2026-04-10", status="OPEN"),
    ]
    offers = [
        make_offer("OFR-RI-001", "OPP-RTL-001", cid, "2025-12-21", "EMAIL", "ACCEPTED", "2026-01-30"),
        make_offer("OFR-RI-002", "OPP-RTL-002", cid, "2026-01-09", "EMAIL", "DEFERRED", "2026-01-12"),
        make_offer("OFR-RI-003", "OPP-RTL-003", cid, "2025-12-05", "CALL", "DECLINED", "2025-12-05"),
        make_offer("OFR-RI-004", "OPP-RTL-005", cid, "2026-03-18", "EMAIL", "ACCEPTED", "2026-03-27"),
        make_offer("OFR-RI-005", "OPP-RTL-006", cid, "2026-03-10", "CALL", "DECLINED", "2026-03-10"),
    ]
    return {
        "profile": {
            "cust_id": cid,
            "cust_type": "INDIVIDUAL",
            "segment": "RETAIL",
            "sub_segment": "PRIORITY",
            "full_name": "Rajesh Iyer",
            "age": 48,
            "city": "Pune",
            "state": "Maharashtra",
            "date_of_birth": "1977-05-18",
            "pan": "CTBPI2207A",
            "gstin": None,
            "cin": None,
            "annual_turnover_inr": None,
            "relationship_start_date": "2011-04-18",
            "home_branch_id": "BR0101",
            "rm_id": rm_id,
            "nre_nro_flag": "RESIDENT",
            "is_active": True,
            "declared_annual_income_inr": 6800000,
            "occupation": "Senior Vice President, fictional IT services firm",
            "household": {"spouse": "Anita Iyer", "children_ages": [17, 13]},
            "preferred_channel": "WhatsApp and email",
            "bio": "",
            "advisor_brief": "",
            "voice_bio": "",
            "talking_points": [],
        },
        "kyc": {"kyc_id": "KYC-RTL-001", "cust_id": cid, "kyc_risk_category": "LOW",
                "last_kyc_date": "2016-02-14", "next_kyc_due_date": "2026-02-14",
                "kyc_status": "DUE", "ckyc_identifier": "91000000022071",
                "preferred_update_channel": "MOBILE_BANKING", "pep_flag": False,
                "sanctions_screen_status": "CLEAR"},
        "risk_profile": {"profile_id": "RISK-RTL-001", "cust_id": cid, "investment_risk_appetite": "MODERATE",
                         "risk_profiling_date": "2024-03-22", "profile_valid_until": "2027-03-22",
                         "declared_annual_income_inr": 6800000, "investment_horizon_years": 10,
                         "stated_objectives": "Children's higher education in 2027 and 2031; retirement at 58",
                         "suitability_band": "BALANCED"},
        "contacts": [{"contact_id": "CON-RI-SELF", "cust_id": cid, "contact_type": "SELF", "name": "Rajesh Iyer",
                      "designation": "Customer", "mobile_masked": "+91-98xxxxx481", "email_masked": "rajesh.iyer@example.invalid",
                      "is_primary": True, "preferred_language": "English", "preferred_contact_window": "Saturday morning"}],
        "accounts": accounts,
        "transactions": transactions,
        "repayment_schedule": repayment_schedule,
        "investment_holding": rajesh_investments(),
        "loans": [a["loan"] | {"account_id": a["account_id"]} for a in accounts if "loan" in a],
        "facilities": [],
        "collateral": [],
        "covenants": [],
        "financials": {"bureau": {"score": 787, "report_date": "2026-02-01", "active_tradelines": 5,
                                  "foir_pct": 41.8, "recent_enquiries": 1}},
        "operations": {"service_tickets": tickets, "documents": [
            {"doc_id": "DOC-RI-001", "cust_id": cid, "doc_type": "KYC_DOC", "doc_title": "Periodic KYC mobile update pack",
             "doc_date": "2026-02-14", "page_count": 3, "storage_uri": "synthetic://contosobank/ri/kyc-pack",
             "extracted_text": "", "sensitivity_class": "CONFIDENTIAL"}
        ], "consents": [{"consent_id": "CNS-RI-001", "purpose": "RM advisory contact", "status": "ACTIVE",
                         "captured_date": "2025-04-01"}]},
        "crm": {"interactions": interactions, "meeting_summaries": meetings, "email_threads": emails,
                "opportunities": opportunities, "offer_responses": offers},
        "external_signals": [],
        "defect_ledger": [
            {"defect_id": "DEF-RI-001", "defect_class": "KYC_DUE", "cust_id": cid, "related_entity_id": "KYC-RTL-001",
             "injected_date": "2026-02-14", "expected_detecting_use_case": "RTL-1", "expected_detection_window_days": 7,
             "difficulty_band": "LOW"},
            {"defect_id": "DEF-RI-002", "defect_class": "SUITABILITY_EVIDENCE_MISSING", "cust_id": cid,
             "related_entity_id": "OPP-RTL-005", "injected_date": "2026-03-27",
             "expected_detecting_use_case": "RTL-2", "expected_detection_window_days": 2,
             "difficulty_band": "MEDIUM"},
        ],
        "six_month_arc": [
            {"arc_id": "ARC-RI-OCT", "date": "2025-10-20", "month": "October", "event_code": "FESTIVE_CARD_SPIKE",
             "facts": {"card_spend_inr": 420000, "revolve_one_cycle": True}, "narrative": ""},
            {"arc_id": "ARC-RI-NOV", "date": "2025-11-05", "month": "November", "event_code": "FAILED_NACH_COMPLAINT",
             "facts": {"emi_inr": 154200, "resolution_days": 9, "reopened_count": 1}, "narrative": ""},
            {"arc_id": "ARC-RI-DEC", "date": "2025-12-19", "month": "December", "event_code": "IDLE_BONUS",
             "facts": {"bonus_inr": 1800000, "idle_days": 41}, "narrative": ""},
            {"arc_id": "ARC-RI-JAN", "date": "2026-01-08", "month": "January", "event_code": "EDUCATION_LOAN_ENQUIRY",
             "facts": {"indicative_need_inr": 4500000, "child_age": 17}, "narrative": ""},
            {"arc_id": "ARC-RI-FEB", "date": "2026-02-14", "month": "February", "event_code": "KYC_DUE",
             "facts": {"kyc_status": "DUE"}, "narrative": ""},
            {"arc_id": "ARC-RI-MAR", "date": "2026-03-27", "month": "March", "event_code": "HASTY_TAX_SAVER",
             "facts": {"elss_inr": 150000, "suitability_checked_flag": False}, "narrative": ""},
        ],
        "sample_markers": {"transaction_population_estimate": 3180, "transactions_sampled": len(transactions),
                           "sample_strategy": "Key events plus representative monthly salary, UPI, card, SIP and EMI flows"},
    }


def build_lakshmi(reference, rng):
    cid = "CTB-RTL-002"
    rm_id = "RM-2207"
    branch = branch_lookup(reference, "BR0101")
    accounts = [
        account("1010000000002001", cid, "PRD00002", branch, "DEPOSIT", "SAVINGS", 2200000,
                product_name="Contoso Priority Savings", open_date="2018-03-05",
                deposit={"deposit_subtype": "SAVINGS", "interest_rate_pct": 3.0, "aqb_inr": 2200000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": True,
                         "nominee_registered": True}),
        account("1010000000002002", cid, "PRD00003", branch, "DEPOSIT", "FD", 4000000,
                product_name="Contoso Fixed Deposit", open_date="2024-09-30",
                deposit={"deposit_subtype": "FD", "interest_rate_pct": 7.05, "aqb_inr": 4000000,
                         "maturity_date": "2026-09-30", "auto_renew_flag": True, "sweep_flag": False,
                         "nominee_registered": True}),
        account("1010000000002003", cid, "PRD00006", branch, "INVESTMENT", "MF_UMBRELLA", 9500000,
                product_name="Contoso Mutual Fund Platform", open_date="2019-02-14",
                investment_summary={"market_value_inr": 9500000, "schemes_count": 8, "active_sips": 2,
                                    "monthly_sip_inr": 50000}),
    ]
    sav = accounts[0]
    flows = []
    for month in [10, 11, 12]:
        flows.append({"txn_date": date(2025, month, 5), "rail": "NEFT", "direction": "CR", "amount_inr": 320000,
                      "counterparty_name": "Meenakshi Textiles Private Limited", "narration": "Promoter remuneration",
                      "channel": "NET", "sort": 1})
    for month in [1, 2, 3]:
        flows.append({"txn_date": date(2026, month, 5), "rail": "NEFT", "direction": "CR", "amount_inr": 320000,
                      "counterparty_name": "Meenakshi Textiles Private Limited", "narration": "Promoter remuneration",
                      "channel": "NET", "sort": 1})
    for day in ["2025-12-18", "2026-02-10", "2026-03-22"]:
        flows.append({"txn_date": d(day), "rail": "INTERNAL", "direction": "DR", "amount_inr": 250000,
                      "counterparty_name": "Contoso Mutual Fund Platform", "narration": "Wealth allocation transfer",
                      "channel": "MOBILE", "sort": 10})
    transactions = make_txns(sav, flows)
    interactions = [
        make_interaction("INT-LS-001", cid, rm_id, "2025-12-18", "VIDEO", "WEALTH_REVIEW", "FOLLOW_UP_SET", 36, 0.32,
                         linked_opportunity_id="OPP-LS-001"),
        make_interaction("INT-LS-002", cid, rm_id, "2026-02-10", "CALL", "PROMOTER_LIQUIDITY", "ADVICE_GIVEN", 18, 0.22),
        make_interaction("INT-LS-003", cid, rm_id, "2026-03-22", "VISIT", "PERSONAL_TAX_PLANNING", "DEFERRED", 40, 0.12,
                         linked_opportunity_id="OPP-LS-002"),
    ]
    opportunities = [
        make_opportunity("OPP-LS-001", cid, rm_id, "PRD00006", "RM_SOURCED", "2025-12-18",
                         "QUALIFIED", 1000000, 50, "2026-04-15", status="OPEN"),
        make_opportunity("OPP-LS-002", cid, rm_id, "PRD00007", "CAMPAIGN", "2026-03-22",
                         "LOST", 150000, 30, "2026-03-22", status="LOST", loss_reason_code="BUSINESS_RENEWAL_PRIORITY"),
    ]
    return {
        "profile": {
            "cust_id": cid, "cust_type": "INDIVIDUAL", "segment": "RETAIL", "sub_segment": "PRIORITY",
            "full_name": "Lakshmi Subramanian", "age": 51, "city": "Coimbatore", "state": "Tamil Nadu",
            "date_of_birth": "1974-08-11", "pan": "CTBPS3412L", "relationship_start_date": "2018-03-05",
            "home_branch_id": "BR0101", "rm_id": rm_id, "nre_nro_flag": "RESIDENT", "is_active": True,
            "declared_annual_income_inr": 8400000, "occupation": "Promoter and Managing Director, Meenakshi Textiles Private Limited",
            "linked_business_customer_id": "CTB-MSME-001", "bio": "", "advisor_brief": "", "voice_bio": "",
            "talking_points": [],
        },
        "kyc": {"kyc_id": "KYC-RTL-002", "cust_id": cid, "kyc_risk_category": "MEDIUM",
                "last_kyc_date": "2024-08-20", "next_kyc_due_date": "2032-08-20", "kyc_status": "CURRENT",
                "ckyc_identifier": "91000000034122", "preferred_update_channel": "NET_BANKING",
                "pep_flag": False, "sanctions_screen_status": "CLEAR"},
        "risk_profile": {"profile_id": "RISK-RTL-002", "cust_id": cid, "investment_risk_appetite": "MODERATE",
                         "risk_profiling_date": "2025-02-06", "profile_valid_until": "2028-02-06",
                         "declared_annual_income_inr": 8400000, "investment_horizon_years": 7,
                         "stated_objectives": "Retirement income, business-contingency liquidity and wealth transfer",
                         "suitability_band": "BALANCED"},
        "contacts": [{"contact_id": "CON-LS-SELF", "cust_id": cid, "contact_type": "SELF", "name": "Lakshmi Subramanian",
                      "designation": "Customer", "mobile_masked": "+91-98xxxxx412", "email_masked": "lakshmi.subramanian@example.invalid",
                      "is_primary": True, "preferred_language": "Tamil/English", "preferred_contact_window": "Weekday evening"}],
        "accounts": accounts,
        "transactions": transactions,
        "repayment_schedule": [],
        "investment_holding": [
            {"holding_id": "MF-LS-001", "account_id": "1010000000002003", "cust_id": cid,
             "instrument_type": "MF_HYBRID", "scheme_name": "Contoso Balanced Advantage Fund",
             "folio_number": "CTB-LS-00001", "units": 126000, "nav_inr": 42.06, "market_value_inr": 5300000,
             "cost_value_inr": 4200000, "purchase_date": "2021-06-10", "sip_flag": True,
             "sip_amount_inr": 30000, "sip_status": "ACTIVE", "risk_grade": "MODERATE", "suitability_checked_flag": True},
            {"holding_id": "MF-LS-002", "account_id": "1010000000002003", "cust_id": cid,
             "instrument_type": "MF_DEBT", "scheme_name": "Contoso Short Duration Fund",
             "folio_number": "CTB-LS-00002", "units": 205000, "nav_inr": 20.49, "market_value_inr": 4200000,
             "cost_value_inr": 3900000, "purchase_date": "2020-04-18", "sip_flag": True,
             "sip_amount_inr": 20000, "sip_status": "ACTIVE", "risk_grade": "LOW", "suitability_checked_flag": True},
        ],
        "loans": [], "facilities": [], "collateral": [], "covenants": [],
        "financials": {"wealth_summary": {"relationship_value_inr": 15700000, "liquidity_buffer_inr": 6200000}},
        "operations": {"service_tickets": [], "documents": [], "consents": [
            {"consent_id": "CNS-LS-001", "purpose": "Cross-segment visibility between retail RM and business RM",
             "status": "ACTIVE", "captured_date": "2025-12-18"}
        ]},
        "crm": {"interactions": interactions, "meeting_summaries": [
            make_meeting("MS-LS-001", "INT-LS-001", "2025-12-18", ["Lakshmi Subramanian", "Priya Deshmukh"],
                         ["CTB-MSME-001", "PRD00006"])
        ], "email_threads": [], "opportunities": opportunities,
                "offer_responses": [make_offer("OFR-LS-001", "OPP-LS-002", cid, "2026-03-22", "VISIT", "DECLINED", "2026-03-22")]},
        "external_signals": [],
        "defect_ledger": [],
        "six_month_arc": [
            {"arc_id": "ARC-LS-DEC", "date": "2025-12-18", "month": "December", "event_code": "WEALTH_REVIEW_AFTER_BUSINESS_SURGE",
             "facts": {"business_customer_id": "CTB-MSME-001", "personal_mf_value_inr": 9500000}, "narrative": ""},
            {"arc_id": "ARC-LS-FEB", "date": "2026-02-10", "month": "February", "event_code": "BUSINESS_RENEWAL_LINKED_LIQUIDITY",
             "facts": {"cash_credit_renewal_due": "2026-02-28"}, "narrative": ""},
        ],
        "sample_markers": {"transaction_population_estimate": 180, "transactions_sampled": len(transactions),
                           "sample_strategy": "Personal wealth flows needed for interlock visibility"},
    }


def build_meenakshi(reference, rng):
    cid = "CTB-MSME-001"
    rm_id = "RM-3412"
    branch = branch_lookup(reference, "BR0234")
    accounts = [
        account("2340000000001001", cid, "PRD00009", branch, "DEPOSIT", "CURRENT", 3400000,
                product_name="Contoso Business Current Account", open_date="2016-05-16",
                deposit={"deposit_subtype": "CURRENT", "interest_rate_pct": 0.0, "aqb_inr": 3400000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False,
                         "nominee_registered": False}),
        account("2340000000001002", cid, "PRD00009", branch, "DEPOSIT", "COLLECTION", 900000,
                product_name="Contoso Business Current Account", open_date="2022-04-01",
                deposit={"deposit_subtype": "CURRENT", "interest_rate_pct": 0.0, "aqb_inr": 900000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False,
                         "nominee_registered": False}),
        account("2340000000001003", cid, "PRD00009", branch, "DEPOSIT", "GST_TAX", 600000,
                product_name="Contoso Business Current Account", open_date="2021-07-01",
                deposit={"deposit_subtype": "CURRENT", "interest_rate_pct": 0.0, "aqb_inr": 600000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False,
                         "nominee_registered": False}),
    ]
    current, collection, taxacct = accounts
    coll_flows = []
    month_factor = {10: 1.35, 11: 1.20, 12: 1.00, 1: 1.10, 2: 0.95, 3: 1.05}
    for bd in bizdays():
        if rng.random() < 0.66:
            for n in range(1 + (1 if rng.random() < 0.30 else 0)):
                amt = amount_band(rng, 28000, 185000) * month_factor[bd.month]
                coll_flows.append({"txn_date": bd, "rail": "UPI" if amt < 200000 else "NEFT", "direction": "CR",
                                   "amount_inr": r2(amt), "counterparty_name": f"Domestic Wholesaler {stable_int(str(bd)+str(n), 41)+1}",
                                   "merchant_category_code": "5131", "narration": "Domestic garment collection",
                                   "channel": "API", "festival_flag": "DIWALI" if bd.month == 10 and bd.day >= 10 else "",
                                   "sort": n})
    cur_flows = []
    for month in [10, 11, 12]:
        cur_flows.append({"txn_date": date(2025, month, 1), "rail": "INTERNAL", "direction": "CR",
                          "amount_inr": 7200000 * month_factor[month],
                          "counterparty_name": "Collections sweep", "narration": "Collection account sweep",
                          "channel": "API", "sort": 1})
    for month in [1, 2, 3]:
        cur_flows.append({"txn_date": date(2026, month, 1), "rail": "INTERNAL", "direction": "CR",
                          "amount_inr": 7200000 * month_factor[month],
                          "counterparty_name": "Collections sweep", "narration": "Collection account sweep",
                          "channel": "API", "sort": 1})
    for month in [10, 11, 12]:
        cur_flows.append({"txn_date": date(2025, month, 7), "rail": "NACH", "direction": "DR",
                          "amount_inr": 1800000, "counterparty_name": "Employee salary file",
                          "narration": "Wages NACH bulk debit", "channel": "BULK_UPLOAD", "sort": 10})
        cur_flows.append({"txn_date": date(2025, month, 20), "rail": "NEFT", "direction": "DR",
                          "amount_inr": 940000 + (month - 10) * 60000, "counterparty_name": "GST PMT",
                          "narration": "GST outflow", "channel": "NET", "sort": 11})
    for month in [1, 2, 3]:
        cur_flows.append({"txn_date": date(2026, month, 7), "rail": "NACH", "direction": "DR",
                          "amount_inr": 1860000, "counterparty_name": "Employee salary file",
                          "narration": "Wages NACH bulk debit", "channel": "BULK_UPLOAD", "sort": 10})
        cur_flows.append({"txn_date": date(2026, month, 20), "rail": "NEFT", "direction": "DR",
                          "amount_inr": 980000 + month * 35000, "counterparty_name": "GST PMT",
                          "narration": "GST outflow", "channel": "NET", "sort": 11})
    for idx, day in enumerate(["2025-10-09", "2025-10-16", "2025-11-12", "2025-11-21", "2025-12-18",
                               "2026-01-12", "2026-01-28", "2026-02-15", "2026-03-12"], 1):
        cur_flows.append({"txn_date": d(day), "rail": "RTGS", "direction": "DR", "amount_inr": 1850000 + idx * 130000,
                          "counterparty_name": f"Yarn Supplier {idx}", "counterparty_ifsc": "CTBK0000234",
                          "narration": "Yarn supplier RTGS", "channel": "NET", "sort": 20 + idx})
    for day, amt in [("2025-10-24", 2600000), ("2025-10-29", 1800000)]:
        cur_flows.append({"txn_date": d(day), "rail": "RTGS", "direction": "DR", "amount_inr": amt,
                          "counterparty_name": "Competitor-bank supplier account", "counterparty_ifsc": "FICT0000444",
                          "narration": "Supplier payment routed outside Contoso Bank", "channel": "NET",
                          "festival_flag": "DIWALI", "sort": 80})
    tax_flows = []
    for month in [10, 11, 12]:
        tax_flows.append({"txn_date": date(2025, month, 18), "rail": "INTERNAL", "direction": "CR",
                          "amount_inr": 1200000, "counterparty_name": "Meenakshi Current Account",
                          "narration": "Tax provisioning sweep", "channel": "NET", "sort": 1})
        tax_flows.append({"txn_date": date(2025, month, 20), "rail": "NEFT", "direction": "DR",
                          "amount_inr": 1120000, "counterparty_name": "GST PMT",
                          "narration": "GST payment", "channel": "NET", "sort": 2})
    for month in [1, 2, 3]:
        tax_flows.append({"txn_date": date(2026, month, 18), "rail": "INTERNAL", "direction": "CR",
                          "amount_inr": 1250000, "counterparty_name": "Meenakshi Current Account",
                          "narration": "Tax provisioning sweep", "channel": "NET", "sort": 1})
        tax_flows.append({"txn_date": date(2026, month, 20), "rail": "NEFT", "direction": "DR",
                          "amount_inr": 1160000, "counterparty_name": "GST PMT",
                          "narration": "GST payment", "channel": "NET", "sort": 2})
    transactions = []
    transactions.extend(make_txns(collection, coll_flows))
    transactions.extend(make_txns(current, cur_flows))
    transactions.extend(make_txns(taxacct, tax_flows))
    facilities = [
        {"facility_id": "CF-MSME-001", "cust_id": cid, "group_id": None, "facility_type": "CC",
         "product_id": "PRD00010", "sanctioned_limit_inr": 65000000, "drawing_power_inr": 59500000,
         "outstanding_inr": 50500000, "fund_based_flag": True, "sanction_date": "2025-02-28",
         "expiry_date": "2026-02-28", "next_review_date": "2026-02-28", "pricing_spread_bps": 275,
         "benchmark_rate_code": "MCLR_1Y", "internal_rating": 6, "external_rating": None,
         "cgtmse_covered_flag": False, "scheme_code": "SCH-PSL-TXT", "utilisation_pct": 77.69,
         "asset_classification": "STANDARD", "dpd_days": 0},
        {"facility_id": "CF-MSME-002", "cust_id": cid, "group_id": None, "facility_type": "TERM_LOAN",
         "product_id": "PRD00011", "sanctioned_limit_inr": 28000000, "drawing_power_inr": 28000000,
         "outstanding_inr": 28000000, "fund_based_flag": True, "sanction_date": "2023-06-20",
         "expiry_date": "2028-06-20", "next_review_date": "2026-06-20", "pricing_spread_bps": 310,
         "benchmark_rate_code": "MCLR_1Y", "internal_rating": 6, "external_rating": None,
         "cgtmse_covered_flag": False, "scheme_code": None, "utilisation_pct": 100.0,
         "asset_classification": "STANDARD", "dpd_days": 0},
        {"facility_id": "CF-MSME-003", "cust_id": cid, "group_id": None, "facility_type": "LC",
         "product_id": "PRD00012", "sanctioned_limit_inr": 30000000, "drawing_power_inr": 30000000,
         "outstanding_inr": 12600000, "fund_based_flag": False, "sanction_date": "2025-02-28",
         "expiry_date": "2026-02-28", "next_review_date": "2026-02-28", "pricing_spread_bps": 120,
         "benchmark_rate_code": "FEE_ONLY", "internal_rating": 6, "external_rating": None,
         "cgtmse_covered_flag": False, "scheme_code": None, "utilisation_pct": 42.0,
         "asset_classification": "STANDARD", "dpd_days": 0},
        {"facility_id": "CF-MSME-004", "cust_id": cid, "group_id": None, "facility_type": "BG",
         "product_id": "PRD00013", "sanctioned_limit_inr": 5000000, "drawing_power_inr": 5000000,
         "outstanding_inr": 2200000, "fund_based_flag": False, "sanction_date": "2025-02-28",
         "expiry_date": "2026-02-28", "next_review_date": "2026-02-28", "pricing_spread_bps": 100,
         "benchmark_rate_code": "FEE_ONLY", "internal_rating": 6, "external_rating": None,
         "cgtmse_covered_flag": False, "scheme_code": None, "utilisation_pct": 44.0,
         "asset_classification": "STANDARD", "dpd_days": 0},
    ]
    utilisation = []
    days_excess = 0
    for idx, bd in enumerate(bizdays(), 1):
        if bd.month == 10:
            pct = 71 + min(22, idx * 0.75) + rng.uniform(-0.8, 0.8)
        elif bd.month == 11:
            pct = 88 + rng.uniform(-2, 5)
        elif bd.month == 12:
            pct = 89 + rng.uniform(-1, 4)
        elif bd.month == 1:
            pct = 91 + rng.uniform(-1, 5)
        elif bd.month == 2:
            pct = 90 + rng.uniform(-1, 6)
        else:
            pct = 83 + rng.uniform(-5, 4)
        pct = min(97.0, max(68.0, pct))
        outstanding = r2(65000000 * pct / 100)
        dp = 59500000 if bd.month not in (1, 2) else 58800000
        excess = r2(max(0, outstanding - dp))
        days_excess = days_excess + 1 if excess else 0
        utilisation.append({"util_id": f"UTIL-MT-{idx:04d}", "facility_id": "CF-MSME-001",
                            "business_date": bd.isoformat(), "sanctioned_limit_inr": 65000000,
                            "drawing_power_inr": dp, "outstanding_inr": outstanding,
                            "utilisation_pct": r2(pct), "excess_over_dp_inr": excess,
                            "days_in_excess": days_excess})
    stock_delays = [3, 16, 9, 21, 6, 12]
    debtor_days = [58, 72, 68, 79, 76, 70]
    stock_statements = []
    for idx, mon in enumerate(["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"], 1):
        month_end = d(mon + "-28") if mon.endswith("02") else d(mon + "-30")
        if mon.endswith(("10", "12", "01", "03")):
            month_end = d(mon + "-31")
        delay = stock_delays[idx - 1]
        raw = 11200000 + idx * 400000
        wip = 7200000 + idx * 250000
        finished = 9800000 + idx * 380000
        debtors = 18500000 + idx * 650000
        dp = r2((raw + wip + finished) * 0.75 + debtors * 0.60)
        stock_statements.append({"stmt_id": f"STK-MT-{idx:02d}", "cust_id": cid, "facility_id": "CF-MSME-001",
                                 "statement_month": mon, "raw_material_inr": raw, "wip_inr": wip,
                                 "finished_goods_inr": finished, "sundry_debtors_inr": debtors,
                                 "debtors_over_90d_inr": 1200000 + idx * 150000,
                                 "sundry_creditors_inr": 9000000 + idx * 300000,
                                 "computed_drawing_power_inr": dp, "submission_date": (month_end + timedelta(days=delay)).isoformat(),
                                 "days_delayed": delay, "is_estimated": False, "debtor_days": debtor_days[idx - 1]})
    collateral = [
        {"collateral_id": "COL-MT-001", "cust_id": cid, "collateral_type": "INDUSTRIAL_LAND",
         "assessed_value_inr": 92000000, "valuation_date": "2023-08-17", "next_valuation_due": "2025-08-17",
         "valuer_name": "Fictional Valuers LLP", "charge_type": "EXCLUSIVE", "roc_charge_id": "ROC-MT-6501",
         "cersai_registration_id": "CERSAI-MT-0001", "insurance_policy_expiry": "2026-06-30", "description": ""},
        {"collateral_id": "COL-MT-002", "cust_id": cid, "collateral_type": "PLANT_MACHINERY",
         "assessed_value_inr": 41000000, "valuation_date": "2024-06-30", "next_valuation_due": "2026-06-30",
         "valuer_name": "Fictional Machinery Assessors", "charge_type": "FIRST_PARI_PASSU", "roc_charge_id": "ROC-MT-6502",
         "cersai_registration_id": "CERSAI-MT-0002", "insurance_policy_expiry": "2026-04-15", "description": ""},
        {"collateral_id": "COL-MT-003", "cust_id": cid, "collateral_type": "STOCK",
         "assessed_value_inr": 28500000, "valuation_date": "2026-03-31", "next_valuation_due": "2026-04-30",
         "valuer_name": "Stock statement", "charge_type": "HYPOTHECATION", "roc_charge_id": "ROC-MT-6503",
         "cersai_registration_id": None, "insurance_policy_expiry": "2026-05-31", "description": ""},
        {"collateral_id": "COL-MT-004", "cust_id": cid, "collateral_type": "PERSONAL_GUARANTEE",
         "assessed_value_inr": 0, "valuation_date": "2025-02-28", "next_valuation_due": "2026-02-28",
         "valuer_name": "Lakshmi Subramanian", "charge_type": "GUARANTEE", "roc_charge_id": None,
         "cersai_registration_id": None, "insurance_policy_expiry": None, "description": ""},
    ]
    collateral_links = [
        {"link_id": "CLINK-MT-001", "collateral_id": "COL-MT-001", "facility_id": "CF-MSME-001",
         "allocated_value_inr": 65000000, "security_coverage_ratio": 1.0},
        {"link_id": "CLINK-MT-002", "collateral_id": "COL-MT-002", "facility_id": "CF-MSME-002",
         "allocated_value_inr": 28000000, "security_coverage_ratio": 1.0},
        {"link_id": "CLINK-MT-003", "collateral_id": "COL-MT-003", "facility_id": "CF-MSME-001",
         "allocated_value_inr": 28500000, "security_coverage_ratio": 0.44},
        {"link_id": "CLINK-MT-004", "collateral_id": "COL-MT-004", "facility_id": "CF-MSME-001",
         "allocated_value_inr": 0, "security_coverage_ratio": 0.0},
    ]
    covenants = []
    cov_specs = [
        ("COV-MT-001", "CF-MSME-001", "FINANCIAL", "DSCR", ">=", 1.25, "2025-12-31", "FAIL", 1.18, 1),
        ("COV-MT-002", "CF-MSME-001", "FINANCIAL", "DSCR", ">=", 1.25, "2026-03-31", "FAIL", 1.17, 2),
        ("COV-MT-003", "CF-MSME-001", "FINANCIAL", "CURRENT_RATIO", ">=", 1.20, "2026-03-31", "PASS", 1.23, 0),
        ("COV-MT-004", "CF-MSME-001", "INFORMATION", "STOCK_STATEMENT_DAYS", "<=", 10, "2026-03-31", "FAIL", 12, 3),
        ("COV-MT-005", "CF-MSME-002", "FINANCIAL", "INTEREST_COVER", ">=", 2.00, "2026-03-31", "PASS", 2.10, 0),
        ("COV-MT-006", "CF-MSME-003", "AFFIRMATIVE", "IMPORT_BILL_DOCS", "<=", 7, "2026-03-31", "PASS", 5, 0),
        ("COV-MT-007", "CF-MSME-004", "AFFIRMATIVE", "BG_MARGIN_TOPUP", ">=", 15, "2026-03-31", "PASS", 18, 0),
        ("COV-MT-008", "CF-MSME-001", "NEGATIVE", "NO_UNAPPROVED_OUTSIDE_BANKING", "=", 0, "2025-10-31", "FAIL", 2, 1),
        ("COV-MT-009", "CF-MSME-001", "FINANCIAL", "DEBTOR_DAYS", "<=", 75, "2026-01-31", "FAIL", 79, 1),
        ("COV-MT-010", "CF-MSME-001", "INFORMATION", "GST_RETURN_TIMELINESS", "<=", 3, "2026-02-20", "PASS", 1, 0),
        ("COV-MT-011", "CF-MSME-002", "AFFIRMATIVE", "MACHINERY_INSURANCE_CURRENT", "=", 1, "2026-03-31", "PASS", 1, 0),
    ]
    for cidc, fid, typ, metric, op, threshold, tested, result, observed, breaches in cov_specs:
        covenants.append({"covenant_id": cidc, "facility_id": fid, "covenant_type": typ,
                          "metric_code": metric, "description": "", "test_frequency": "QUARTERLY",
                          "threshold_operator": op, "threshold_value": threshold, "observed_value": observed,
                          "last_tested_date": tested, "last_test_result": result, "breach_count_12m": breaches,
                          "waiver_status": "REQUESTED" if result == "FAIL" and metric == "DSCR" else "NONE"})
    trade_events = []
    for idx, day in enumerate(["2025-10-08", "2025-10-21", "2025-11-11", "2025-12-03", "2025-12-19",
                               "2026-01-10", "2026-01-24", "2026-02-08", "2026-03-09"], 1):
        trade_events.append({"event_id": f"TF-MT-LC-{idx:02d}", "facility_id": "CF-MSME-003", "cust_id": cid,
                             "instrument_type": "LC_ISSUED", "instrument_ref": f"LC-MT-2026-{idx:03d}",
                             "amount_inr": 1800000 + idx * 120000, "currency": "USD", "event_date": day,
                             "expiry_date": (d(day) + timedelta(days=90)).isoformat(), "beneficiary_name": f"Yarn Exporter {idx}",
                             "country_code": "SG", "status": "OPEN", "note": ""})
    for idx, day in enumerate(["2026-01-18", "2026-02-12", "2026-03-19"], 1):
        trade_events.append({"event_id": f"TF-MT-AMD-{idx:02d}", "facility_id": "CF-MSME-003", "cust_id": cid,
                             "instrument_type": "LC_AMENDED", "instrument_ref": f"LC-MT-2026-00{idx}",
                             "amount_inr": 0, "currency": "USD", "event_date": day,
                             "expiry_date": (d(day) + timedelta(days=45)).isoformat(), "beneficiary_name": f"Yarn Exporter {idx}",
                             "country_code": "SG", "status": "AMENDED", "note": ""})
    for idx, day in enumerate(["2026-02-25", "2026-03-17"], 1):
        trade_events.append({"event_id": f"TF-MT-BILL-{idx:02d}", "facility_id": "CF-MSME-003", "cust_id": cid,
                             "instrument_type": "BILL_OVERDUE", "instrument_ref": f"BILL-MT-{idx:03d}",
                             "amount_inr": 1250000 + idx * 200000, "currency": "USD", "event_date": day,
                             "expiry_date": (d(day) + timedelta(days=15)).isoformat(), "beneficiary_name": f"Yarn Exporter {idx}",
                             "country_code": "SG", "status": "OVERDUE", "note": ""})
    interactions = []
    key_events = [("2025-10-06", "VISIT", "FESTIVE_ORDER_SURGE", "FOLLOW_UP_SET", 70, 0.22),
                  ("2025-10-28", "CALL", "FLOAT_LEAKAGE", "ADVICE_GIVEN", 18, -0.05),
                  ("2025-11-16", "CALL", "CHEQUE_RETURNS", "ESCALATED", 26, -0.35),
                  ("2025-11-24", "VISIT", "LATE_STOCK_STATEMENT", "DOCUMENTS_PENDING", 62, -0.12),
                  ("2025-12-31", "VIDEO", "DSCR_BREACH", "CREDIT_REVIEW", 48, -0.28),
                  ("2026-01-13", "VISIT", "EXPORT_LC_DISCUSSION", "QUALIFIED", 74, 0.32),
                  ("2026-01-29", "CALL", "HIGH_UTILISATION", "FOLLOW_UP_SET", 20, -0.22),
                  ("2026-02-19", "VISIT", "RENEWAL_FILE_START", "DOCUMENTS_PENDING", 83, -0.05),
                  ("2026-02-28", "CALL", "RENEWAL_DUE", "ESCALATED", 31, -0.25),
                  ("2026-03-07", "CALL", "SMA0_CURE", "RESOLVED", 18, 0.02),
                  ("2026-03-31", "VIDEO", "SECOND_COVENANT_FAIL", "CREDIT_REVIEW", 52, -0.30)]
    for idx, item in enumerate(key_events, 1):
        interactions.append(make_interaction(f"INT-MT-{idx:03d}", cid, rm_id, item[0], item[1], item[2], item[3],
                                             item[4], item[5]))
    for idx in range(12, 23):
        day = (WINDOW_START + timedelta(days=idx * 7)).isoformat()
        interactions.append(make_interaction(f"INT-MT-{idx:03d}", cid, rm_id, day, "CALL", "ROUTINE_MONITORING",
                                             "FOLLOW_UP_SET", 12 + idx % 15, 0.04))
    meetings = [
        make_meeting("MS-MT-001", "INT-MT-001", "2025-10-06", ["Arjun Nair", "Lakshmi Subramanian", "Karthik Subramanian"],
                     ["CF-MSME-001", "STK-MT-01"]),
        make_meeting("MS-MT-002", "INT-MT-006", "2026-01-13", ["Arjun Nair", "Karthik Subramanian"], ["PRD00012", "PRD00019"]),
        make_meeting("MS-MT-003", "INT-MT-008", "2026-02-19", ["Arjun Nair", "Lakshmi Subramanian"], ["CF-MSME-001"]),
    ]
    emails = [
        make_email_thread("EM-MT-001", cid, rm_id, "October stock statement submission", "2025-11-01",
                          ["Arjun Nair", "Meenakshi Finance Team"],
                          [{"sender_role": "RM", "sent_timestamp": "2025-11-01T09:10:00+05:30", "intent": "stock statement chase"},
                           {"sender_role": "CUSTOMER", "sent_timestamp": "2025-11-16T18:30:00+05:30", "intent": "late submission"}]),
        make_email_thread("EM-MT-002", cid, rm_id, "Cash credit renewal documents", "2026-02-19",
                          ["Arjun Nair", "Lakshmi Subramanian", "Credit Analyst"],
                          [{"sender_role": "RM", "sent_timestamp": "2026-02-19T10:00:00+05:30", "intent": "renewal checklist"},
                           {"sender_role": "CUSTOMER", "sent_timestamp": "2026-02-22T13:20:00+05:30", "intent": "partial documents"},
                           {"sender_role": "CREDIT", "sent_timestamp": "2026-02-25T17:45:00+05:30", "intent": "valuation overdue"}]),
        make_email_thread("EM-MT-003", cid, rm_id, "Export LC and payment gateway", "2026-01-13",
                          ["Karthik Subramanian", "Arjun Nair", "Trade Finance Specialist"],
                          [{"sender_role": "CUSTOMER", "sent_timestamp": "2026-01-13T19:00:00+05:30", "intent": "export LC query"},
                           {"sender_role": "PRODUCT_SPECIALIST", "sent_timestamp": "2026-01-14T11:00:00+05:30", "intent": "indicative process"}]),
    ]
    opportunities = [
        make_opportunity("OPP-MT-001", cid, rm_id, "PRD00019", "RM_SOURCED", "2026-01-13",
                         "QUALIFIED", 1800000, 55, "2026-04-30", status="OPEN"),
        make_opportunity("OPP-MT-002", cid, rm_id, "PRD00012", "INBOUND", "2026-01-13",
                         "QUALIFIED", 30000000, 50, "2026-04-15", status="OPEN"),
        make_opportunity("OPP-MT-003", cid, rm_id, "PRD00010", "MODEL_GENERATED", "2026-02-19",
                         "CREDIT_REVIEW", 65000000, 70, "2026-03-31", status="OPEN"),
        make_opportunity("OPP-MT-004", cid, rm_id, "PRD00011", "MODEL_GENERATED", "2026-03-05",
                         "LOST", 12000000, 20, "2026-03-05", status="LOST", loss_reason_code="STRESS_SIGNALS_FIRST"),
    ]
    tickets = [
        make_service_ticket("TCK-MT-001", cid, "2340000000001001", "2025-11-16", "CHEQUE",
                            "THREE_RETURNS_FROM_WHOLESALER", "MEDIUM", "2025-11-21", reopened_count=0),
        make_service_ticket("TCK-MT-002", cid, None, "2026-02-25", "CREDIT_DOCS",
                            "VALUATION_OVERDUE", "HIGH", "2026-03-12", reopened_count=1),
    ]
    external_signals = [
        {"signal_id": "EXT-MT-001", "cust_id": cid, "signal_date": "2025-10-12", "signal_type": "NEWS",
         "source_name": "Synthetic Textile Desk", "headline": "", "summary_text": "", "severity_score": 0.35,
         "confidence_score": 0.82},
        {"signal_id": "EXT-MT-002", "cust_id": cid, "signal_date": "2026-01-22", "signal_type": "GST_FILING_DELAY",
         "source_name": "Synthetic GST Monitor", "headline": "", "summary_text": "", "severity_score": 0.55,
         "confidence_score": 0.76},
        {"signal_id": "EXT-MT-003", "cust_id": cid, "signal_date": "2026-02-18", "signal_type": "BUYER_STRESS",
         "source_name": "Synthetic Buyer Ledger Watch", "headline": "", "summary_text": "", "severity_score": 0.62,
         "confidence_score": 0.70},
    ]
    docs = []
    for idx, stmt in enumerate(stock_statements, 1):
        docs.append({"doc_id": f"DOC-MT-STK-{idx:02d}", "cust_id": cid, "doc_type": "STOCK_STATEMENT",
                     "doc_title": f"Stock statement {stmt['statement_month']}", "doc_date": stmt["submission_date"],
                     "page_count": 4, "storage_uri": f"synthetic://contosobank/mt/stock/{idx}",
                     "extracted_text": "", "sensitivity_class": "CONFIDENTIAL"})
    for idx, mon in enumerate(["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"], 1):
        docs.append({"doc_id": f"DOC-MT-GST-{idx:02d}", "cust_id": cid, "doc_type": "GST_RETURN",
                     "doc_title": f"GSTR-3B summary {mon}", "doc_date": f"{mon}-20", "page_count": 3,
                     "storage_uri": f"synthetic://contosobank/mt/gst/{idx}", "extracted_text": "",
                     "sensitivity_class": "CONFIDENTIAL"})
    for idx in range(13, 32):
        dtype = "FINANCIAL_STATEMENT" if idx % 5 == 0 else ("VALUATION_REPORT" if idx == 17 else "CREDIT_NOTE")
        docs.append({"doc_id": f"DOC-MT-{idx:02d}", "cust_id": cid, "doc_type": dtype,
                     "doc_title": f"Meenakshi credit document {idx}", "doc_date": (WINDOW_START + timedelta(days=idx * 5)).isoformat(),
                     "page_count": 2 + idx % 8, "storage_uri": f"synthetic://contosobank/mt/doc/{idx}",
                     "extracted_text": "", "sensitivity_class": "CONFIDENTIAL"})
    return {
        "profile": {
            "cust_id": cid, "cust_type": "PVT_LTD", "segment": "MSME", "sub_segment": "SMALL",
            "entity_name": "Meenakshi Textiles Private Limited", "full_name": "Meenakshi Textiles Private Limited",
            "date_of_incorporation": "2009-09-21", "city": "Tiruppur", "state": "Tamil Nadu",
            "pan": "CTBCM6501Z", "gstin": "33CTBCM6501Z1Z5", "cin": "U17299TZ2009PTC650001",
            "udyam_number": "UDYAM-TN-32-6500001", "udyam_classification": "SMALL", "annual_turnover_inr": 470000000,
            "employees": 142, "industry_code": "IND-TXT", "relationship_start_date": "2016-05-16",
            "home_branch_id": "BR0234", "rm_id": rm_id, "promoter_name": "Lakshmi Subramanian",
            "promoter_retail_cust_id": "CTB-RTL-002", "is_active": True, "bio": "", "advisor_brief": "",
            "voice_bio": "", "talking_points": [],
        },
        "kyc": {"kyc_id": "KYC-MSME-001", "cust_id": cid, "kyc_risk_category": "MEDIUM",
                "last_kyc_date": "2025-02-28", "next_kyc_due_date": "2033-02-28",
                "kyc_status": "CURRENT", "ckyc_identifier": None, "preferred_update_channel": "BRANCH",
                "pep_flag": False, "sanctions_screen_status": "CLEAR"},
        "risk_profile": None,
        "contacts": [
            {"contact_id": "CON-MT-LS", "cust_id": cid, "contact_type": "PROMOTER", "name": "Lakshmi Subramanian",
             "designation": "Managing Director", "mobile_masked": "+91-98xxxxx412", "email_masked": "lakshmi.subramanian@example.invalid",
             "is_primary": True, "preferred_language": "Tamil/English", "preferred_contact_window": "Afternoon"},
            {"contact_id": "CON-MT-KS", "cust_id": cid, "contact_type": "DIRECTOR", "name": "Karthik Subramanian",
             "designation": "Director - Exports and Digital", "mobile_masked": "+91-88xxxxx217", "email_masked": "karthik.subramanian@example.invalid",
             "is_primary": False, "preferred_language": "English/Tamil", "preferred_contact_window": "Morning"},
        ],
        "accounts": accounts, "transactions": transactions, "repayment_schedule": [],
        "investment_holding": [], "loans": [], "facilities": facilities, "limit_utilisation_daily": utilisation,
        "collateral": collateral, "collateral_facility_links": collateral_links, "covenants": covenants,
        "financials": {
            "bureau": {"commercial_score": 721, "report_date": "2026-02-01", "recent_enquiries": 4},
            "gst_summaries": [{"period": m, "taxable_sales_inr": 36000000 + i * 1800000,
                               "gst_paid_inr": 940000 + i * 45000, "filing_status": "DELAYED" if m == "2026-01" else "ON_TIME"}
                              for i, m in enumerate(["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"])],
            "statements": [{"period": "FY2025", "revenue_inr": 470000000, "ebitda_margin_pct": 11.8,
                            "dscr": 1.31, "tol_tnw": 2.62},
                           {"period": "H2-FY2026", "revenue_inr": 257000000, "ebitda_margin_pct": 9.9,
                            "dscr": 1.18, "tol_tnw": 2.88}],
            "stock_statements": stock_statements,
        },
        "operations": {"service_tickets": tickets, "documents": docs, "trade_finance_events": trade_events,
                       "delinquency_events": [
                           {"event_id": "DLQ-MT-001", "account_id": "CF-MSME-001", "event_date": "2026-03-01",
                            "dpd_bucket": "SMA_0", "previous_classification": "STANDARD", "new_classification": "SMA_0",
                            "trigger_reason_code": "INTEREST_OVERDUE", "cured_date": "2026-03-07"},
                           {"event_id": "DLQ-MT-002", "account_id": "CF-MSME-001", "event_date": "2026-03-07",
                            "dpd_bucket": "CURRENT", "previous_classification": "SMA_0", "new_classification": "STANDARD",
                            "trigger_reason_code": "CURED", "cured_date": "2026-03-07"},
                           {"event_id": "DLQ-MT-003", "account_id": "2340000000001001", "event_date": "2025-11-16",
                            "dpd_bucket": "NA", "previous_classification": "STANDARD", "new_classification": "STANDARD",
                            "trigger_reason_code": "CHEQUE_RETURN_CLUSTER", "cured_date": None}],
                       "float_leakage_events": [{"event_id": "FL-MT-001", "event_date": "2025-10-24",
                                                 "amount_inr": 2600000, "observed_bank": "Competitor Bank"},
                                                {"event_id": "FL-MT-002", "event_date": "2025-10-29",
                                                 "amount_inr": 1800000, "observed_bank": "Competitor Bank"}]},
        "crm": {"interactions": interactions, "meeting_summaries": meetings, "email_threads": emails,
                "opportunities": opportunities, "offer_responses": [
                    make_offer("OFR-MT-001", "OPP-MT-001", cid, "2026-01-14", "EMAIL", "DEFERRED", "2026-01-18"),
                    make_offer("OFR-MT-002", "OPP-MT-004", cid, "2026-03-05", "CALL", "DECLINED", "2026-03-05")]},
        "external_signals": external_signals,
        "defect_ledger": [
            {"defect_id": "DEF-MT-001", "defect_class": "STRESS_TRAJECTORY", "cust_id": cid,
             "related_entity_id": "CF-MSME-001", "injected_date": "2026-01-31",
             "expected_detecting_use_case": "MSM-1", "expected_detection_window_days": 14, "difficulty_band": "HIGH"},
            {"defect_id": "DEF-MT-002", "defect_class": "OVERDUE_VALUATION", "cust_id": cid,
             "related_entity_id": "COL-MT-001", "injected_date": "2026-02-25",
             "expected_detecting_use_case": "MSM-2", "expected_detection_window_days": 7, "difficulty_band": "MEDIUM"},
            {"defect_id": "DEF-MT-003", "defect_class": "COVENANT_BREACH", "cust_id": cid,
             "related_entity_id": "COV-MT-001", "injected_date": "2025-12-31",
             "expected_detecting_use_case": "MSM-1", "expected_detection_window_days": 5, "difficulty_band": "MEDIUM"},
        ],
        "six_month_arc": [
            {"arc_id": "ARC-MT-OCT", "date": "2025-10-20", "month": "October", "event_code": "FESTIVE_ORDER_SURGE",
             "facts": {"cc_utilisation_start_pct": 71, "cc_utilisation_peak_pct": 93, "float_leakage_inr": 4400000}, "narrative": ""},
            {"arc_id": "ARC-MT-NOV", "date": "2025-11-16", "month": "November", "event_code": "DEBTOR_BUILD_CHEQUE_RETURNS",
             "facts": {"cheque_returns": 3, "stock_statement_delay_days": 16}, "narrative": ""},
            {"arc_id": "ARC-MT-DEC", "date": "2025-12-31", "month": "December", "event_code": "DSCR_BREACH",
             "facts": {"dscr": 1.18, "threshold": 1.25}, "narrative": ""},
            {"arc_id": "ARC-MT-JAN", "date": "2026-01-31", "month": "January", "event_code": "NO_SEASONAL_UNWIND",
             "facts": {"utilisation_above_90_pct_days": 19, "debtor_days": 79}, "narrative": ""},
            {"arc_id": "ARC-MT-FEB", "date": "2026-02-28", "month": "February", "event_code": "RENEWAL_PRESSURE",
             "facts": {"renewal_due_date": "2026-02-28", "file_started": "2026-02-19"}, "narrative": ""},
            {"arc_id": "ARC-MT-MAR", "date": "2026-03-31", "month": "March", "event_code": "SECOND_COVENANT_FAIL_SMA0_CURED",
             "facts": {"sma0_days": 6, "dscr": 1.17}, "narrative": ""},
        ],
        "sample_markers": {"transaction_population_estimate": 8940, "transactions_sampled": len(transactions),
                           "utilisation_population_estimate": 600, "utilisation_sampled": len(utilisation),
                           "sample_strategy": "All key stress/renewal events plus sampled current-account and UPI activity"},
    }


def build_sundaram(reference, rng):
    cid = "CTB-CORP-001"
    rm_id = "RM-5104"
    branch = branch_lookup(reference, "BR0308")
    accounts = [
        account("3080000000001001", cid, "PRD00014", branch, "DEPOSIT", "CURRENT", 580000000,
                product_name="Contoso Dealer CMS", open_date="2014-04-01",
                deposit={"deposit_subtype": "CURRENT", "interest_rate_pct": 0.0, "aqb_inr": 820000000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False, "nominee_registered": False}),
        account("3080000000001002", cid, "PRD00014", branch, "DEPOSIT", "CMS_POOL", 210000000,
                product_name="Contoso Dealer CMS", open_date="2018-06-15",
                deposit={"deposit_subtype": "CURRENT", "interest_rate_pct": 0.0, "aqb_inr": 275000000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False, "nominee_registered": False}),
        account("3080000000001003", cid, "PRD00017", branch, "DEPOSIT", "FOREX_EEFC", 65000000,
                product_name="Contoso Forward Cover Line", open_date="2020-02-20", currency="USD",
                deposit={"deposit_subtype": "CURRENT", "interest_rate_pct": 0.0, "aqb_inr": 65000000,
                         "maturity_date": None, "auto_renew_flag": False, "sweep_flag": False, "nominee_registered": False}),
    ]
    op, cms, forex = accounts
    op_flows = []
    for day, amt in [("2025-12-26", 240000000), ("2025-12-29", 260000000), ("2025-12-30", 330000000),
                     ("2025-12-31", 350000000), ("2026-01-06", 1180000000),
                     ("2026-03-25", 340000000), ("2026-03-26", 360000000), ("2026-03-27", 380000000),
                     ("2026-03-30", 390000000), ("2026-03-31", 390000000)]:
        direction = "DR" if day == "2026-01-06" else "CR"
        op_flows.append({"txn_date": d(day), "rail": "RTGS", "direction": direction, "amount_inr": amt,
                         "counterparty_name": "Quarter-end treasury concentration", "narration": "Quarter-end float build/unwind",
                         "channel": "API", "sort": 5})
    for month in [10, 11, 12]:
        op_flows.append({"txn_date": date(2025, month, 15), "rail": "RTGS", "direction": "DR",
                         "amount_inr": 460000000 if month == 12 else 220000000,
                         "counterparty_name": "Advance Tax PMT", "narration": "Corporate advance tax outflow",
                         "channel": "NET", "sort": 20})
        op_flows.append({"txn_date": date(2025, month, 28), "rail": "RTGS", "direction": "DR",
                         "amount_inr": 185000000, "counterparty_name": "Feedstock Supplier Consortium",
                         "narration": "Feedstock payment", "channel": "NET", "sort": 21})
    for month in [1, 2, 3]:
        op_flows.append({"txn_date": date(2026, month, 15), "rail": "RTGS", "direction": "DR",
                         "amount_inr": 520000000 if month == 3 else 240000000,
                         "counterparty_name": "Advance Tax PMT", "narration": "Corporate advance tax outflow",
                         "channel": "NET", "sort": 20})
        op_flows.append({"txn_date": date(2026, month, 27), "rail": "RTGS", "direction": "DR",
                         "amount_inr": 195000000, "counterparty_name": "Feedstock Supplier Consortium",
                         "narration": "Feedstock payment", "channel": "NET", "sort": 21})
    cms_flows = []
    dealer_count = 0
    for bd in [x for x in bizdays(date(2026, 1, 1), date(2026, 1, 31)) if x.weekday() < 5]:
        for n in range(2):
            dealer_count += 1
            cms_flows.append({"txn_date": bd, "rail": "NEFT", "direction": "CR",
                              "amount_inr": amount_band(rng, 1600000, 4200000),
                              "counterparty_name": f"Recurring Dealer {1 + dealer_count % 41}",
                              "narration": "Dealer collection via CMS", "channel": "API", "sort": n})
    for day in ["2025-10-21", "2025-11-18", "2025-12-19", "2026-02-14", "2026-03-21"]:
        forex_amt = amount_band(rng, 22000000, 56000000)
        forex_key = stable_int(day, 10000)
        cms_flows.append({"txn_date": d(day), "rail": "FOREX_WIRE", "direction": "CR",
                          "amount_inr": forex_amt, "counterparty_name": f"Export Buyer {forex_key}",
                          "narration": "Export realisation", "channel": "API", "sort": 10})
    forex_flows = [
        {"txn_date": d("2026-03-23"), "rail": "FOREX_WIRE", "direction": "CR", "amount_inr": 38000000,
         "counterparty_name": "USD receivable conversion", "narration": "Export realisation", "channel": "API", "sort": 1},
        {"txn_date": d("2026-03-24"), "rail": "FOREX_WIRE", "direction": "DR", "amount_inr": 21000000,
         "counterparty_name": "Feedstock import settlement", "narration": "Import payment", "channel": "API", "sort": 2},
    ]
    transactions = []
    transactions.extend(make_txns(op, op_flows))
    transactions.extend(make_txns(cms, cms_flows))
    transactions.extend(make_txns(forex, forex_flows))
    group_entities = [
        {"cust_id": "CTB-CORP-001", "entity_name": "Sundaram Speciality Chemicals Limited", "cust_type": "PUBLIC_LTD",
         "role": "PARENT", "incorporation_date": "1987-04-12", "existing_debt_inr": 2850000000},
        {"cust_id": "CTB-CORP-002", "entity_name": "Sundaram Dahej Manufacturing Private Limited", "cust_type": "PVT_LTD",
         "role": "SUBSIDIARY", "incorporation_date": "2006-03-09", "existing_debt_inr": 1350000000},
        {"cust_id": "CTB-CORP-003", "entity_name": "Sundaram Taloja Intermediates Private Limited", "cust_type": "PVT_LTD",
         "role": "SUBSIDIARY", "incorporation_date": "2011-07-21", "existing_debt_inr": 850000000},
        {"cust_id": "CTB-CORP-004", "entity_name": "Sundaram Logistics Services Private Limited", "cust_type": "PVT_LTD",
         "role": "SUBSIDIARY", "incorporation_date": "2017-08-11", "existing_debt_inr": 260000000},
        {"cust_id": "CTB-CORP-005", "entity_name": "Sundaram Specialty Intermediates Private Limited", "cust_type": "PVT_LTD",
         "role": "ACQUIRED_SUBSIDIARY", "incorporation_date": "2015-02-19", "existing_debt_inr": 950000000,
         "effective_from": "2025-11-14"},
    ]
    group_links = [
        {"link_id": "CGL-SS-001", "group_id": "GRP-SS-001", "cust_id": ent["cust_id"], "relationship_type": ent["role"],
         "shareholding_pct": 100 if ent["cust_id"] != "CTB-CORP-001" else 0, "effective_from": ent.get("effective_from", "2025-10-01"),
         "effective_to": None}
        for ent in group_entities
    ] + [{"link_id": "CGL-SS-006", "group_id": "GRP-SS-001", "cust_id": "CTB-CORP-005",
          "relationship_type": "ACQUIRED_WITH_COMPETITOR_DEBT", "shareholding_pct": 100,
          "effective_from": "2025-11-14", "effective_to": None}]
    facilities = [
        ("CF-CORP-001", "WC_CONSORTIUM", "PRD00015", 2400000000, 1464000000, True, 165, "T_BILL_91D"),
        ("CF-CORP-002", "TERM_LOAN_DAHEJ", "PRD00015", 1200000000, 1120000000, True, 210, "MCLR_1Y"),
        ("CF-CORP-003", "TERM_LOAN_TALOJA", "PRD00015", 650000000, 570000000, True, 205, "MCLR_1Y"),
        ("CF-CORP-004", "LC", "PRD00016", 1500000000, 890000000, False, 95, "FEE_ONLY"),
        ("CF-CORP-005", "BG", "PRD00013", 650000000, 310000000, False, 85, "FEE_ONLY"),
        ("CF-CORP-006", "FORWARD_IRS", "PRD00018", 400000000, 95000000, False, 70, "FEE_ONLY"),
    ]
    facility_rows = []
    for fid, typ, pid, limit, outstanding, fund, spread, bench in facilities:
        facility_rows.append({"facility_id": fid, "cust_id": cid, "group_id": "GRP-SS-001", "facility_type": typ,
                              "product_id": pid, "sanctioned_limit_inr": limit, "drawing_power_inr": limit,
                              "outstanding_inr": outstanding, "fund_based_flag": fund, "sanction_date": "2025-04-01",
                              "expiry_date": "2026-06-30", "next_review_date": "2026-03-31" if fid in ("CF-CORP-001", "CF-CORP-003") else "2026-06-30",
                              "pricing_spread_bps": spread, "benchmark_rate_code": bench, "internal_rating": 4,
                              "external_rating": "A-/Stable", "cgtmse_covered_flag": False, "scheme_code": None,
                              "utilisation_pct": r2(outstanding / limit * 100), "asset_classification": "STANDARD",
                              "dpd_days": 0})
    util_rows = []
    for idx, bd in enumerate(bizdays(), 1):
        for fid, typ, _pid, limit, _out, _fund, _spread, _bench in facilities[:4]:
            base = 0.78 if fid == "CF-CORP-001" else (0.62 if typ.startswith("TERM") else 0.55)
            if bd in (date(2025, 12, 31), date(2026, 3, 31)):
                base = 0.61 if fid == "CF-CORP-001" else base - 0.05
            pct = max(0.20, min(0.92, base + rng.uniform(-0.035, 0.035)))
            util_rows.append({"util_id": f"UTIL-SS-{idx:04d}-{fid[-1]}", "facility_id": fid,
                              "business_date": bd.isoformat(), "sanctioned_limit_inr": limit,
                              "drawing_power_inr": limit, "outstanding_inr": r2(limit * pct),
                              "utilisation_pct": r2(pct * 100), "excess_over_dp_inr": 0.0, "days_in_excess": 0})
    covenants = []
    cov_specs = [
        ("COV-SS-001", "CF-CORP-001", "FINANCIAL", "CONSOLIDATED_TOL_TNW", "<=", 3.00, "2026-02-28", "PASS", 2.94),
        ("COV-SS-002", "CF-CORP-001", "FINANCIAL", "INTEREST_COVER", ">=", 2.50, "2026-02-28", "PASS", 2.58),
        ("COV-SS-003", "CF-CORP-002", "FINANCIAL", "DSCR", ">=", 1.30, "2026-03-31", "PASS", 1.36),
        ("COV-SS-004", "CF-CORP-001", "INFORMATION", "ANNUAL_REVIEW_SUBMISSION", "<=", 0, "2026-03-31", "FAIL", 7),
        ("COV-SS-005", "CF-CORP-004", "AFFIRMATIVE", "LC_MARGIN", ">=", 10, "2026-03-31", "PASS", 12),
        ("COV-SS-006", "CF-CORP-006", "NEGATIVE", "UNHEDGED_FX_EXPOSURE_PCT", "<=", 35, "2026-03-31", "PASS", 31),
        ("COV-SS-007", "CF-CORP-003", "FINANCIAL", "NET_DEBT_EBITDA", "<=", 3.25, "2026-03-31", "PASS", 3.12),
        ("COV-SS-008", "CF-CORP-005", "AFFIRMATIVE", "BG_CLAIM_NOTICE", "=", 0, "2026-03-31", "PASS", 0),
        ("COV-SS-009", "CF-CORP-001", "FINANCIAL", "MIN_CURRENT_RATIO", ">=", 1.10, "2026-03-31", "PASS", 1.14),
        ("COV-SS-010", "CF-CORP-001", "NEGATIVE", "NO_UNAPPROVED_ACQUISITION_DEBT", "=", 0, "2025-11-14", "WATCH", 950000000),
    ]
    for cidc, fid, typ, metric, op, threshold, tested, result, observed in cov_specs:
        covenants.append({"covenant_id": cidc, "facility_id": fid, "covenant_type": typ,
                          "metric_code": metric, "description": "", "test_frequency": "QUARTERLY",
                          "threshold_operator": op, "threshold_value": threshold, "observed_value": observed,
                          "last_tested_date": tested, "last_test_result": result, "breach_count_12m": 1 if result == "FAIL" else 0,
                          "waiver_status": "NONE"})
    collateral = []
    for idx in range(1, 13):
        collateral.append({"collateral_id": f"COL-SS-{idx:03d}", "cust_id": cid,
                           "collateral_type": "COMMERCIAL_PROPERTY" if idx <= 4 else ("PLANT_MACHINERY" if idx <= 8 else "CORPORATE_GUARANTEE"),
                           "assessed_value_inr": 250000000 + idx * 45000000,
                           "valuation_date": "2024-12-31" if idx <= 8 else "2025-04-01",
                           "next_valuation_due": "2026-12-31" if idx <= 8 else "2026-04-01",
                           "valuer_name": f"Fictional Corporate Valuer {idx}",
                           "charge_type": "FIRST_PARI_PASSU" if idx <= 10 else "SECOND",
                           "roc_charge_id": f"ROC-SS-{idx:06d}", "cersai_registration_id": f"CERSAI-SS-{idx:04d}",
                           "insurance_policy_expiry": "2026-09-30", "description": ""})
    trade_events = []
    for idx in range(1, 25):
        day = (WINDOW_START + timedelta(days=idx * 6)).isoformat()
        trade_events.append({"event_id": f"TF-SS-{idx:03d}", "facility_id": "CF-CORP-004", "cust_id": cid,
                             "instrument_type": "LC_ISSUED" if idx % 4 else "EXPORT_REALISATION",
                             "instrument_ref": f"SS-LC-2026-{idx:04d}", "amount_inr": 22000000 + idx * 900000,
                             "currency": "USD", "event_date": day,
                             "expiry_date": (d(day) + timedelta(days=120)).isoformat(),
                             "beneficiary_name": f"Feedstock Supplier {idx % 7 + 1}", "country_code": "SG",
                             "status": "OPEN" if idx % 4 else "REALISED", "note": ""})
    interactions = []
    events = [("2025-10-14", "CALL", "ROUTINE_LC_PIPELINE", "FOLLOW_UP_SET", 22, 0.12),
              ("2025-11-14", "VISIT", "ACQUISITION_CLOSURE", "QUALIFIED", 65, 0.18),
              ("2025-12-30", "CALL", "QUARTER_END_FLOAT", "ADVICE_GIVEN", 24, 0.15),
              ("2026-01-16", "VIDEO", "DEALER_PAYMENT_PATTERN", "QUALIFIED", 54, 0.28),
              ("2026-02-09", "CALL", "CMS_FEE_DISPUTE", "ESCALATED", 31, -0.42),
              ("2026-02-28", "VIDEO", "COVENANT_NEAR_BREACH", "CREDIT_REVIEW", 47, -0.12),
              ("2026-03-18", "VISIT", "ANNUAL_REVIEW_DUE", "DOCUMENTS_PENDING", 78, -0.08),
              ("2026-03-27", "CALL", "IRS_ENQUIRY", "QUALIFIED", 26, 0.22),
              ("2026-03-31", "CALL", "FY_END_FLOAT", "FOLLOW_UP_SET", 18, 0.10)]
    for idx, item in enumerate(events, 1):
        interactions.append(make_interaction(f"INT-SS-{idx:03d}", cid, rm_id, item[0], item[1], item[2], item[3],
                                             item[4], item[5]))
    for idx in range(10, 59):
        day = (WINDOW_START + timedelta(days=idx * 3)).isoformat()
        interactions.append(make_interaction(f"INT-SS-{idx:03d}", cid, rm_id, day, "EMAIL", "INTERNAL_COORDINATION",
                                             "FOLLOW_UP_SET", 0, 0.02, direction="INTERNAL"))
    meetings = [
        make_meeting("MS-SS-001", "INT-SS-002", "2025-11-14", ["Sanjay Malhotra", "Anand Krishnan", "Nikhil Bose"],
                     ["GRP-SS-001", "CTB-CORP-005"]),
        make_meeting("MS-SS-002", "INT-SS-004", "2026-01-16", ["Sanjay Malhotra", "Anand Krishnan", "CMS Specialist"],
                     ["PRD00020", "PRD00014"]),
        make_meeting("MS-SS-003", "INT-SS-006", "2026-02-28", ["Sanjay Malhotra", "Anand Krishnan", "Credit Risk"],
                     ["COV-SS-001", "CF-CORP-001"]),
        make_meeting("MS-SS-004", "INT-SS-007", "2026-03-18", ["Sanjay Malhotra", "Nikhil Bose"],
                     ["CF-CORP-001", "CF-CORP-003"]),
        make_meeting("MS-SS-005", "INT-SS-008", "2026-03-27", ["Sanjay Malhotra", "Nikhil Bose", "Treasury Specialist"],
                     ["PRD00018"]),
    ]
    emails = [
        make_email_thread("EM-SS-001", cid, rm_id, "Acquired entity debt refinance", "2025-11-14",
                          ["Sanjay Malhotra", "Anand Krishnan", "Credit", "Legal"],
                          [{"sender_role": "CUSTOMER_CFO", "sent_timestamp": "2025-11-14T18:45:00+05:30", "intent": "acquisition closed"},
                           {"sender_role": "RM", "sent_timestamp": "2025-11-15T09:20:00+05:30", "intent": "refinance opportunity"},
                           {"sender_role": "CREDIT", "sent_timestamp": "2025-11-17T11:30:00+05:30", "intent": "debt schedule request"}]),
        make_email_thread("EM-SS-002", cid, rm_id, "CMS fee dispute - acquired entity", "2026-02-09",
                          ["Anand Krishnan", "Sanjay Malhotra", "Transaction Banking Ops"],
                          [{"sender_role": "CUSTOMER_CFO", "sent_timestamp": "2026-02-09T08:50:00+05:30", "intent": "fee dispute"},
                           {"sender_role": "RM", "sent_timestamp": "2026-02-09T09:35:00+05:30", "intent": "escalation"},
                           {"sender_role": "OPERATIONS", "sent_timestamp": "2026-02-14T16:20:00+05:30", "intent": "partial reversal"},
                           {"sender_role": "CUSTOMER_CFO", "sent_timestamp": "2026-02-16T10:05:00+05:30", "intent": "second escalation"}]),
        make_email_thread("EM-SS-003", cid, rm_id, "Annual review pack", "2026-03-18",
                          ["Sanjay Malhotra", "Nikhil Bose", "Credit"],
                          [{"sender_role": "RM", "sent_timestamp": "2026-03-18T10:00:00+05:30", "intent": "review checklist"},
                           {"sender_role": "CUSTOMER_TREASURER", "sent_timestamp": "2026-03-19T18:00:00+05:30", "intent": "documents pending"}]),
    ]
    opportunities = [
        make_opportunity("OPP-SS-001", cid, rm_id, "PRD00020", "MODEL_GENERATED", "2026-01-16",
                         "QUALIFIED", 750000000, 60, "2026-05-31", status="OPEN"),
        make_opportunity("OPP-SS-002", cid, rm_id, "PRD00015", "MODEL_GENERATED", "2025-11-15",
                         "QUALIFIED", 950000000, 55, "2026-06-30", status="OPEN"),
        make_opportunity("OPP-SS-003", cid, rm_id, "PRD00018", "INBOUND", "2026-03-27",
                         "QUALIFIED", 400000000, 45, "2026-04-30", status="OPEN"),
        make_opportunity("OPP-SS-004", cid, rm_id, "PRD00014", "RM_SOURCED", "2026-02-09",
                         "LOST", 0, 15, "2026-02-16", status="LOST", loss_reason_code="FEE_DISPUTE_UNRESOLVED"),
    ]
    tickets = [
        make_service_ticket("TCK-SS-001", cid, "3080000000001002", "2026-02-09", "CMS",
                            "FEE_DISPUTE_ACQUIRED_ENTITY", "HIGH", "2026-02-25", reopened_count=2),
        make_service_ticket("TCK-SS-002", cid, None, "2026-03-18", "CREDIT_DOCS", "ANNUAL_REVIEW_PENDING",
                            "MEDIUM", None, reopened_count=0, status="OPEN"),
    ]
    external_signals = [
        {"signal_id": "EXT-SS-001", "cust_id": cid, "signal_date": "2025-11-14", "signal_type": "REGULATORY_FILING",
         "source_name": "Synthetic MCA Filing Watch", "headline": "", "summary_text": "", "severity_score": 0.30,
         "confidence_score": 0.90},
        {"signal_id": "EXT-SS-002", "cust_id": cid, "signal_date": "2025-12-10", "signal_type": "RATING_ACTION",
         "source_name": "Fictional Ratings Desk", "headline": "", "summary_text": "", "severity_score": 0.20,
         "confidence_score": 0.86},
        {"signal_id": "EXT-SS-003", "cust_id": cid, "signal_date": "2026-01-08", "signal_type": "NEWS",
         "source_name": "Synthetic Chemicals Monitor", "headline": "", "summary_text": "", "severity_score": 0.35,
         "confidence_score": 0.78},
        {"signal_id": "EXT-SS-004", "cust_id": cid, "signal_date": "2026-02-20", "signal_type": "SECTOR_MARGIN_PRESSURE",
         "source_name": "Synthetic Sector Desk", "headline": "", "summary_text": "", "severity_score": 0.48,
         "confidence_score": 0.74},
        {"signal_id": "EXT-SS-005", "cust_id": cid, "signal_date": "2026-03-21", "signal_type": "TREASURY_MARKET",
         "source_name": "Synthetic Treasury Desk", "headline": "", "summary_text": "", "severity_score": 0.33,
         "confidence_score": 0.80},
    ]
    docs = []
    for idx in range(1, 25):
        docs.append({"doc_id": f"DOC-SS-{idx:03d}", "cust_id": cid,
                     "doc_type": "FINANCIAL_STATEMENT" if idx <= 5 else ("BOARD_RESOLUTION" if idx % 6 == 0 else "CREDIT_NOTE"),
                     "doc_title": f"Sundaram group document {idx}", "doc_date": (WINDOW_START + timedelta(days=idx * 6)).isoformat(),
                     "page_count": 5 + idx % 12, "storage_uri": f"synthetic://contosobank/ss/doc/{idx}",
                     "extracted_text": "", "sensitivity_class": "RESTRICTED"})
    return {
        "profile": {
            "cust_id": cid, "cust_type": "PUBLIC_LTD", "segment": "CORPORATE", "sub_segment": "MID_CORP",
            "entity_name": "Sundaram Speciality Chemicals Group", "full_name": "Sundaram Speciality Chemicals Group",
            "date_of_incorporation": "1987-04-12", "city": "Mumbai", "state": "Maharashtra",
            "pan": "CTBCS5104M", "gstin": "27CTBCS5104M1Z7", "cin": "L20299MH1987PLC510401",
            "annual_turnover_inr": 28500000000, "industry_code": "IND-CHM", "relationship_start_date": "2014-04-01",
            "home_branch_id": "BR0308", "rm_id": rm_id, "group_id": "GRP-SS-001", "is_active": True,
            "wallet_share_pct": 34, "internal_rating": 4, "external_rating": "A-/Stable",
            "bio": "", "advisor_brief": "", "voice_bio": "", "talking_points": [],
        },
        "entity_group": {"group_id": "GRP-SS-001", "group_name": "Sundaram Speciality Chemicals Group",
                         "group_type": "BUSINESS_GROUP", "parent_cust_id": "CTB-CORP-001",
                         "group_exposure_limit_inr": 6800000000, "group_rating": "4"},
        "group_entities": group_entities, "customer_group_links": group_links,
        "kyc": {"kyc_id": "KYC-CORP-001", "cust_id": cid, "kyc_risk_category": "MEDIUM",
                "last_kyc_date": "2025-04-01", "next_kyc_due_date": "2033-04-01",
                "kyc_status": "CURRENT", "ckyc_identifier": None, "preferred_update_channel": "BRANCH",
                "pep_flag": False, "sanctions_screen_status": "CLEAR"},
        "risk_profile": None,
        "contacts": [
            {"contact_id": "CON-SS-AK", "cust_id": cid, "contact_type": "CFO", "name": "Anand Krishnan",
             "designation": "Group CFO", "age": 52, "mobile_masked": "+91-99xxxxx204", "email_masked": "anand.krishnan@example.invalid",
             "is_primary": True, "preferred_language": "English", "preferred_contact_window": "Business hours",
             "private_banking_relationship": True, "salary_account_with_bank": True, "consent_status": "ACTIVE"},
            {"contact_id": "CON-SS-NB", "cust_id": cid, "contact_type": "TREASURER", "name": "Nikhil Bose",
             "designation": "Group Treasurer", "age": 39, "mobile_masked": "+91-98xxxxx118", "email_masked": "nikhil.bose@example.invalid",
             "is_primary": False, "preferred_language": "English/Hindi", "preferred_contact_window": "Business hours"},
        ],
        "accounts": accounts, "transactions": transactions, "repayment_schedule": [],
        "investment_holding": [], "loans": [], "facilities": facility_rows, "limit_utilisation_daily": util_rows,
        "collateral": collateral, "collateral_facility_links": [
            {"link_id": f"CLINK-SS-{idx:03d}", "collateral_id": col["collateral_id"],
             "facility_id": "CF-CORP-001" if idx <= 6 else "CF-CORP-002",
             "allocated_value_inr": r2(col["assessed_value_inr"] * 0.7), "security_coverage_ratio": 0.7}
            for idx, col in enumerate(collateral, 1)
        ], "covenants": covenants,
        "financials": {"statements": [{"period": "FY2025", "revenue_inr": 28500000000, "ebitda_margin_pct": 15.4,
                                       "tol_tnw": 2.72, "interest_cover": 3.05},
                                      {"period": "H2-FY2026", "revenue_inr": 14800000000, "ebitda_margin_pct": 14.1,
                                       "tol_tnw": 2.94, "interest_cover": 2.58}],
                       "wallet": {"estimated_wallet_share_pct": 34, "competitor_banks": 2,
                                  "competitor_dealer_finance_flag": True, "forex_flow_with_competitor_pct": 62},
                       "dealer_payment_pattern": {"january_inbound_count": 340, "recurring_dealers": 41,
                                                  "mean_ticket_inr": 2800000, "lag_days_range": [45, 60]}},
        "operations": {"service_tickets": tickets, "documents": docs, "trade_finance_events": trade_events,
                       "delinquency_events": []},
        "crm": {"interactions": interactions, "meeting_summaries": meetings, "email_threads": emails,
                "opportunities": opportunities, "offer_responses": [
                    make_offer("OFR-SS-001", "OPP-SS-001", cid, "2026-01-17", "EMAIL", "DEFERRED", "2026-01-21"),
                    make_offer("OFR-SS-002", "OPP-SS-004", cid, "2026-02-12", "EMAIL", "DECLINED", "2026-02-16")]},
        "external_signals": external_signals,
        "defect_ledger": [
            {"defect_id": "DEF-SS-001", "defect_class": "UNCOMMUNICATED_FEE_CHANGE", "cust_id": cid,
             "related_entity_id": "TCK-SS-001", "injected_date": "2026-02-09",
             "expected_detecting_use_case": "CRP-1", "expected_detection_window_days": 3, "difficulty_band": "MEDIUM"},
            {"defect_id": "DEF-SS-002", "defect_class": "COVENANT_NEAR_BREACH", "cust_id": cid,
             "related_entity_id": "COV-SS-001", "injected_date": "2026-02-28",
             "expected_detecting_use_case": "CRP-2", "expected_detection_window_days": 7, "difficulty_band": "HIGH"},
            {"defect_id": "DEF-SS-003", "defect_class": "OVERDUE_ANNUAL_REVIEW", "cust_id": cid,
             "related_entity_id": "CF-CORP-001", "injected_date": "2026-03-31",
             "expected_detecting_use_case": "CRP-2", "expected_detection_window_days": 5, "difficulty_band": "LOW"},
        ],
        "six_month_arc": [
            {"arc_id": "ARC-SS-OCT", "date": "2025-10-14", "month": "October", "event_code": "ROUTINE_LC_RUN_RATE",
             "facts": {"lc_events_in_sample": 24}, "narrative": ""},
            {"arc_id": "ARC-SS-NOV", "date": "2025-11-14", "month": "November", "event_code": "ACQUISITION_CLOSES",
             "facts": {"acquired_debt_inr": 950000000, "effective_from": "2025-11-14"}, "narrative": ""},
            {"arc_id": "ARC-SS-DEC", "date": "2025-12-31", "month": "December", "event_code": "QUARTER_END_FLOAT_BUILD",
             "facts": {"float_build_inr": 1180000000, "unwind_date": "2026-01-06", "dressed_utilisation_pct": 61}, "narrative": ""},
            {"arc_id": "ARC-SS-JAN", "date": "2026-01-16", "month": "January", "event_code": "DEALER_PAYMENT_PATTERN",
             "facts": {"dealer_inflows": 340, "recurring_dealers": 41, "mean_ticket_inr": 2800000}, "narrative": ""},
            {"arc_id": "ARC-SS-FEB", "date": "2026-02-28", "month": "February", "event_code": "CMS_FEE_DISPUTE_AND_COVENANT_TIGHTENING",
             "facts": {"tol_tnw": 2.94, "threshold": 3.0, "fee_ticket": "TCK-SS-001"}, "narrative": ""},
            {"arc_id": "ARC-SS-MAR", "date": "2026-03-31", "month": "March", "event_code": "FY_END_FLOAT_AND_IRS_ENQUIRY",
             "facts": {"fy_end_float_peak_inr": 1860000000, "irs_notional_inr": 400000000}, "narrative": ""},
        ],
        "sample_markers": {"transaction_population_estimate": 84200, "transactions_sampled": len(transactions),
                           "utilisation_population_estimate": 4650, "utilisation_sampled": len(util_rows),
                           "sample_strategy": "Group-level key events, dealer-payment sample, float spikes and annual-review evidence"},
    }


def build_dataset(seed=DEFAULT_SEED):
    rng = random.Random(seed)
    reference = build_reference()
    customers = {
        "CTB-RTL-001": build_rajesh(reference, rng),
        "CTB-RTL-002": build_lakshmi(reference, rng),
        "CTB-MSME-001": build_meenakshi(reference, rng),
        "CTB-CORP-001": build_sundaram(reference, rng),
    }
    cross_links = [
        {"link_id": "XLINK-001", "link_type": "MSME_PROMOTER_IS_RETAIL_PRIORITY_CUSTOMER",
         "from_cust_id": "CTB-MSME-001", "to_cust_id": "CTB-RTL-002", "from_rm_id": "RM-3412",
         "to_rm_id": "RM-2207", "contact_id": "CON-MT-LS", "effective_from": "2025-10-01",
         "visibility_rule": "Business-side limit conversations and personal-side wealth conversations are visible only to entitled RMs with active consent.",
         "evidence": ["CTB-MSME-001.profile.promoter_retail_cust_id", "CTB-RTL-002.profile.linked_business_customer_id"]},
        {"link_id": "XLINK-002", "link_type": "CORPORATE_CFO_PRIVATE_BANKING_AND_SALARY_MANDATE",
         "from_cust_id": "CTB-CORP-001", "to_contact_id": "CON-SS-AK", "from_rm_id": "RM-5104",
         "effective_from": "2025-10-01",
         "visibility_rule": "Consented CFO personal relationship is flagged for information-barrier handling; the corporate RM sees only mandate-safe relationship markers.",
         "evidence": ["CTB-CORP-001.contacts.CON-SS-AK.private_banking_relationship"]},
    ]
    return {
        "meta": {
            "bank_name": "Contoso Bank",
            "codename": CODENAME,
            "version": "1.0",
            "generated_at": GENERATED_AT,
            "schema_version": SCHEMA_VERSION,
            "demo_today": DEMO_TODAY,
            "window_start": WINDOW_START.isoformat(),
            "window_end": WINDOW_END.isoformat(),
            "seed": seed,
            "deterministic_core": True,
            "narrative_enriched": False,
            "synthetic_data": True,
        },
        "reference": reference,
        "rms": build_rms(),
        "customers": customers,
        "cross_links": cross_links,
    }


def iter_values(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from iter_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_values(value)
    else:
        yield obj


def assert_no_nan(bundle):
    for value in iter_values(bundle):
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise AssertionError("NaN/Infinity present in dataset")


def validate_dataset(bundle):
    assert bundle["meta"]["bank_name"] == "Contoso Bank"
    assert bundle["meta"]["codename"] == CODENAME
    customers = bundle["customers"]
    rms = bundle["rms"]
    product_ids = {p["product_id"] for p in bundle["reference"]["products_catalog"]}
    assert {"RM-2207", "RM-3412", "RM-5104"} <= set(rms)
    assert {"CTB-RTL-001", "CTB-MSME-001", "CTB-CORP-001"} <= set(customers)
    assert customers["CTB-RTL-001"]["profile"]["full_name"] == "Rajesh Iyer"
    assert customers["CTB-MSME-001"]["profile"]["entity_name"] == "Meenakshi Textiles Private Limited"
    assert customers["CTB-CORP-001"]["profile"]["entity_name"] == "Sundaram Speciality Chemicals Group"
    assert customers["CTB-MSME-001"]["profile"]["promoter_retail_cust_id"] == "CTB-RTL-002"
    assert customers["CTB-RTL-002"]["profile"]["rm_id"] == "RM-2207"
    for cid, node in customers.items():
        assert node["profile"]["rm_id"] in rms, f"{cid}: missing RM"
        acct_ids = {a["account_id"] for a in node.get("accounts", [])}
        facility_ids = {f["facility_id"] for f in node.get("facilities", [])}
        collateral_ids = {c["collateral_id"] for c in node.get("collateral", [])}
        account_map = {a["account_id"]: a for a in node.get("accounts", [])}
        txns_by_account = {}
        for txn in node.get("transactions", []):
            assert txn["cust_id"] == cid, f"{cid}: transaction cust mismatch"
            assert txn["account_id"] in acct_ids, f"{cid}: orphan transaction"
            txn_date = d(txn["txn_date"])
            assert WINDOW_START <= txn_date <= WINDOW_END, f"{cid}: txn outside window"
            txns_by_account.setdefault(txn["account_id"], []).append(txn)
        for aid, rows in txns_by_account.items():
            acct = account_map[aid]
            bal = float(acct.get("opening_balance_inr", 0))
            semantics = acct.get("balance_semantics", "DEPOSIT_BALANCE")
            for txn in sorted(rows, key=lambda r: (r["txn_date"], r["txn_id"])):
                amt = float(txn["amount_inr"])
                if semantics == "CARD_OUTSTANDING":
                    bal = bal + amt if txn["direction"] == "DR" else bal - amt
                else:
                    bal = bal - amt if txn["direction"] == "DR" else bal + amt
                bal = r2(bal)
                assert r2(txn["running_balance_inr"]) == bal, f"{cid}: running balance mismatch {aid}"
            assert r2(acct["current_balance_inr"]) == bal, f"{cid}: account tie-out mismatch {aid}"
        for opp in node.get("crm", {}).get("opportunities", []):
            assert opp["product_id"] in product_ids, f"{cid}: opportunity product missing"
        opp_ids = {o["opp_id"] for o in node.get("crm", {}).get("opportunities", [])}
        ticket_ids = {t["ticket_id"] for t in node.get("operations", {}).get("service_tickets", [])}
        for offer in node.get("crm", {}).get("offer_responses", []):
            assert offer["opp_id"] in opp_ids, f"{cid}: orphan offer"
        for interaction in node.get("crm", {}).get("interactions", []):
            if interaction.get("linked_opportunity_id"):
                assert interaction["linked_opportunity_id"] in opp_ids, f"{cid}: interaction orphan opp"
            if interaction.get("linked_ticket_id"):
                assert interaction["linked_ticket_id"] in ticket_ids, f"{cid}: interaction orphan ticket"
        for cov in node.get("covenants", []):
            assert cov["facility_id"] in facility_ids, f"{cid}: orphan covenant"
        for link in node.get("collateral_facility_links", []):
            assert link["facility_id"] in facility_ids, f"{cid}: orphan collateral facility"
            assert link["collateral_id"] in collateral_ids, f"{cid}: orphan collateral"
        for event in node.get("six_month_arc", []):
            event_date = d(event["date"])
            assert WINDOW_START <= event_date <= WINDOW_END, f"{cid}: arc outside window"
    for link in bundle["cross_links"]:
        assert link["from_cust_id"] in customers, f"orphan cross-link source {link['link_id']}"
        if "to_cust_id" in link:
            assert link["to_cust_id"] in customers, f"orphan cross-link target {link['link_id']}"
        if "from_rm_id" in link:
            assert link["from_rm_id"] in rms, f"orphan cross-link RM {link['link_id']}"
        if "to_rm_id" in link:
            assert link["to_rm_id"] in rms, f"orphan cross-link RM {link['link_id']}"
    assert_no_nan(bundle)
    return True


def summary_counts(bundle):
    customers = bundle["customers"]
    accounts = sum(len(c.get("accounts", [])) for c in customers.values())
    txns = sum(len(c.get("transactions", [])) for c in customers.values())
    facilities = sum(len(c.get("facilities", [])) for c in customers.values())
    covenants = sum(len(c.get("covenants", [])) for c in customers.values())
    interactions = sum(len(c.get("crm", {}).get("interactions", [])) for c in customers.values())
    return {
        "rms": len(bundle["rms"]),
        "customers": len(customers),
        "accounts": accounts,
        "transactions": txns,
        "facilities": facilities,
        "covenants": covenants,
        "interactions": interactions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic Contoso Bank synthetic dataset.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON path.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Deterministic seed.")
    parser.add_argument("--validate", action="store_true",
                        help="Validate an existing --out file if present; otherwise validate the generated bundle.")
    args = parser.parse_args()

    out = Path(args.out)
    if args.validate and out.exists():
        bundle = json.loads(out.read_text(encoding="utf-8"))
        validate_dataset(bundle)
        log.info("Validation passed for %s", out)
        return 0

    bundle = build_dataset(args.seed)
    validate_dataset(bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = summary_counts(bundle)
    log.info("Wrote %s", out)
    log.info("Summary: %s", ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
