#!/usr/bin/env python3
"""
Narrative enrichment pass for the Contoso Bank synthetic dataset.

Reads data/contosobank/contosobank_dataset.json, fills blank prose fields from
structured evidence, and writes the JSON back in place. Azure OpenAI is optional:
use --offline for deterministic templated prose.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "contosobank_dataset.json"
ENRICHED_AT = "2026-04-01T10:00:00+05:30"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enrich_contosobank")


def _aoai_client():
    ep = os.environ["FOUNDRY_AOAI_ENDPOINT"].rstrip("/")
    if not ep.endswith("/openai/v1"):
        ep = ep + "/openai/v1"
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import OpenAI
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")
    client = OpenAI(base_url=ep, api_key=token_provider)
    deployment = os.environ.get("FOUNDRY_CHAT_DEPLOYMENT", "gpt-5-4")
    return client, deployment


def _chat(client, deployment, messages, max_tokens, want_json):
    kwargs = {
        "model": deployment,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if want_json:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        retry = dict(kwargs)
        retry.pop("response_format", None)
        retry.pop("max_completion_tokens", None)
        return client.chat.completions.create(**retry)


SYSTEM = (
    "You are a relationship-management assistant at Contoso Bank, a fictional Indian retail bank. "
    "Write concise, professional RM-facing prose grounded ONLY in the supplied evidence. Never invent "
    "figures, names, dates, identifiers or outcomes. Never imply assured returns. Keep RBI/SEBI-style "
    "suitability, consent and credit-judgement boundaries clear. Return ONLY valid JSON."
)

NARRATIVE_STRING_KEYS = {
    "advisor_brief", "voice_bio", "bio", "blurb", "note", "thread_summary", "body_text",
    "agenda_text", "discussion_summary", "customer_verbatim", "agent_notes", "resolution_note",
    "reason", "suitability_note", "loss_reason_text", "decline_reason_text", "description",
    "headline", "summary_text", "extracted_text", "narrative",
}
NARRATIVE_LIST_KEYS = {"talking_points", "decisions", "action_items"}


def inr(value):
    if value is None:
        return "₹0"
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 10000000:
        return f"{sign}₹{value / 10000000:.2f} crore"
    if value >= 100000:
        return f"{sign}₹{value / 100000:.2f} lakh"
    return f"{sign}₹{value:,.0f}"


def display_name(node):
    p = node.get("profile", {})
    return p.get("full_name") or p.get("entity_name") or p.get("cust_id", "customer")


def product_map(bundle):
    return {p["product_id"]: p for p in bundle.get("reference", {}).get("products_catalog", [])}


def compact_facts(facts):
    parts = []
    for key, value in (facts or {}).items():
        label = key.replace("_", " ")
        if key.endswith("_inr"):
            parts.append(f"{label}: {inr(value)}")
        elif key.endswith("_pct"):
            parts.append(f"{label}: {value}%")
        else:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


def ai_json(client, deployment, instruction, evidence, max_tokens=650):
    user = (
        f"{instruction}\n\nReturn JSON only.\n\n"
        f"EVIDENCE:\n```json\n{json.dumps(evidence, indent=2, ensure_ascii=False, default=str)}\n```"
    )
    resp = _chat(client, deployment, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                 max_tokens=max_tokens, want_json=True)
    return json.loads((resp.choices[0].message.content or "{}").strip())


def ai_text(client, deployment, instruction, evidence, fallback, max_tokens=260):
    if client is None:
        return fallback
    try:
        data = ai_json(client, deployment, instruction + '\nShape: {"text":"<prose>"}', evidence, max_tokens=max_tokens)
        text = str(data.get("text", "")).strip()
        return text or fallback
    except Exception as exc:
        log.warning("AI text fallback (%s)", exc)
        return fallback


def customer_evidence(cid, node):
    p = node.get("profile", {})
    facilities = node.get("facilities", [])
    accounts = node.get("accounts", [])
    tickets = node.get("operations", {}).get("service_tickets", [])
    opps = node.get("crm", {}).get("opportunities", [])
    return {
        "cust_id": cid,
        "name": display_name(node),
        "segment": p.get("segment"),
        "sub_segment": p.get("sub_segment"),
        "rm_id": p.get("rm_id"),
        "city": p.get("city"),
        "declared_income_inr": p.get("declared_annual_income_inr"),
        "turnover_inr": p.get("annual_turnover_inr"),
        "kyc_status": (node.get("kyc") or {}).get("kyc_status"),
        "risk_profile": (node.get("risk_profile") or {}).get("investment_risk_appetite"),
        "account_count": len(accounts),
        "deposit_balance_inr": sum(float(a.get("current_balance_inr", 0)) for a in accounts if a.get("account_type") == "DEPOSIT"),
        "investment_value_inr": sum(float(h.get("market_value_inr", 0)) for h in node.get("investment_holding", [])),
        "sanctioned_facilities_inr": sum(float(f.get("sanctioned_limit_inr", 0)) for f in facilities),
        "facility_types": [f.get("facility_type") for f in facilities],
        "open_or_breached_tickets": [
            {"ticket_id": t.get("ticket_id"), "category": t.get("category"), "sla_breach": t.get("sla_breach_flag"),
             "reopened": t.get("reopened_count"), "status": t.get("status")}
            for t in tickets
        ],
        "opportunities": [
            {"opp_id": o.get("opp_id"), "product_id": o.get("product_id"), "stage": o.get("stage"),
             "status": o.get("win_loss_status"), "value_inr": o.get("expected_value_inr"),
             "suitability": o.get("suitability_checked_flag")}
            for o in opps
        ],
        "arc": [{"event_code": a.get("event_code"), "date": a.get("date"), "facts": a.get("facts")}
                for a in node.get("six_month_arc", [])],
    }


def fallback_customer_profile(cid, node):
    p = node.get("profile", {})
    name = display_name(node)
    seg = p.get("segment", "")
    kyc = (node.get("kyc") or {}).get("kyc_status")
    if seg == "RETAIL" and cid == "CTB-RTL-001":
        inv = sum(h["market_value_inr"] for h in node.get("investment_holding", []))
        brief = (
            f"{name} is a Priority retail customer in Pune with salary banking, three FDs, "
            f"a home loan outstanding of {inr(14200000)}, two credit cards and mutual-fund holdings of {inr(inv)}. "
            f"His six-month pattern includes a Diwali card-spend spike, a reopened NACH complaint, "
            f"an idle {inr(1800000)} bonus, an education-loan enquiry, KYC due status and a hastily executed ELSS order."
        )
        talking = [
            "Close or evidence the KYC update before any new advisory recommendation.",
            "Acknowledge the November NACH complaint before discussing investments or card limits.",
            f"Frame the education-loan and idle-bonus discussion around suitability; no assured-return language.",
        ]
    elif seg == "RETAIL":
        brief = (
            f"{name} is a Priority retail customer and the promoter of Meenakshi Textiles Private Limited. "
            f"Her personal relationship includes savings, FD and mutual-fund balances, while her business renewal creates "
            f"a consented cross-segment context for Priya and Arjun."
        )
        talking = [
            "Keep personal wealth advice separate from business credit judgement.",
            "Check liquidity needs before any fresh allocation.",
            "Use the active consent marker before cross-segment visibility is surfaced.",
        ]
    elif seg == "MSME":
        sanctioned = sum(f["sanctioned_limit_inr"] for f in node.get("facilities", []))
        brief = (
            f"{name} is a Tiruppur garment manufacturer with sanctioned facilities of {inr(sanctioned)}, "
            f"including cash credit, term loan, LC and BG lines. The arc shows festive utilisation rising, "
            f"late stock statements, DSCR covenant failures, renewal pressure and a short SMA-0 episode that cured in March."
        )
        talking = [
            "Lead with renewal, overdue valuation and DSCR breach evidence before discussing enhancement.",
            "Separate seasonal Diwali utilisation from the January no-unwind anomaly.",
            "Surface payment-gateway and export-LC opportunities only with stress caveats.",
        ]
    else:
        sanctioned = sum(f["sanctioned_limit_inr"] for f in node.get("facilities", []))
        brief = (
            f"{name} is a mid-corporate chemicals group with aggregate Contoso Bank limits of {inr(sanctioned)} "
            f"and estimated wallet share of {p.get('wallet_share_pct')}%. The group added an acquired entity in November, "
            f"built quarter-end float, generated a dealer-payment supply-chain-finance signal, and has an unresolved CMS fee dispute."
        )
        talking = [
            "Do not pitch supply-chain finance until the CMS fee dispute is acknowledged.",
            "Use the acquired entity debt schedule as the refinancing evidence.",
            "Treat the CFO private-banking marker as an information-barrier flag, not a sales shortcut.",
        ]
    return {
        "bio": f"{name} is a fictional Contoso Bank {p.get('sub_segment', seg)} relationship in {p.get('city', '')}, mapped to RM {p.get('rm_id')} with KYC status {kyc}.",
        "advisor_brief": brief,
        "voice_bio": brief.split(".")[0].strip() + ".",
        "talking_points": talking[:3],
    }


def enrich_profile(bundle, cid, node, client, deployment):
    fallback = fallback_customer_profile(cid, node)
    if client is not None:
        try:
            data = ai_json(
                client,
                deployment,
                'Produce {"bio":"<1 sentence>","advisor_brief":"<3-4 sentences>","voice_bio":"<1 sentence>","talking_points":["<point>","<point>","<point>"]} for this relationship.',
                customer_evidence(cid, node),
                max_tokens=850,
            )
            for key in ("bio", "advisor_brief", "voice_bio"):
                if str(data.get(key, "")).strip():
                    fallback[key] = str(data[key]).strip()
            points = [str(x).strip() for x in data.get("talking_points", []) if str(x).strip()]
            if points:
                fallback["talking_points"] = points[:3]
        except Exception as exc:
            log.warning("AI profile fallback for %s (%s)", cid, exc)
    node["profile"]["bio"] = fallback["bio"]
    node["profile"]["advisor_brief"] = fallback["advisor_brief"]
    node["profile"]["voice_bio"] = fallback["voice_bio"]
    node["profile"]["talking_points"] = fallback["talking_points"]


def enrich_rms(bundle, client, deployment):
    for rm in bundle.get("rms", {}).values():
        pstats = rm.get("portfolio_stats", {})
        branch = next((b for b in bundle["reference"]["branches"] if b["branch_id"] == rm["branch_id"]), {})
        fallback = {
            "bio": f"{rm['rm_name']} is a {rm['role']} for Contoso Bank in {branch.get('city', '')}, with languages {', '.join(rm.get('languages_spoken', []))}.",
            "advisor_brief": (
                f"{rm['rm_name']} covers the {rm['segment']} segment from {branch.get('branch_name', rm['branch_id'])}. "
                f"Portfolio indicators are {', '.join(f'{k}={v}' for k, v in pstats.items())}; daily work centres on "
                f"{', '.join(rm.get('daily_workflow', [])[:3])}."
            ),
            "voice_bio": f"{rm['rm_name']} is the Contoso Bank {rm['role']} for {rm['segment']} relationships.",
            "talking_points": [
                f"Prioritise {rm.get('kpis', ['relationship health'])[0]}.",
                "Use service, suitability and credit-control flags before any pitch.",
                "Capture the next action in CRM immediately after the interaction.",
            ],
        }
        if client is not None:
            try:
                data = ai_json(client, deployment,
                               'Produce {"bio":"<1 sentence>","advisor_brief":"<2 sentences>","voice_bio":"<1 sentence>","talking_points":["<point>","<point>","<point>"]} for this RM persona.',
                               {"rm": rm, "branch": branch}, max_tokens=650)
                for key in ("bio", "advisor_brief", "voice_bio"):
                    if str(data.get(key, "")).strip():
                        fallback[key] = str(data[key]).strip()
                points = [str(x).strip() for x in data.get("talking_points", []) if str(x).strip()]
                if points:
                    fallback["talking_points"] = points[:3]
            except Exception as exc:
                log.warning("AI RM fallback for %s (%s)", rm["rm_id"], exc)
        rm.update(fallback)


def enrich_products(bundle, client, deployment):
    for product in bundle.get("reference", {}).get("products_catalog", []):
        fallback = (
            f"{product['product_name']} is a Contoso Bank {product['product_family'].lower()} product for "
            f"{product['segment_applicability']} relationships, with tickets from {inr(product['min_ticket_inr'])} "
            f"to {inr(product['max_ticket_inr'])}. It must be offered only after the applicable product rules, "
            f"suitability checks and service blockers are reviewed."
        )
        product["blurb"] = ai_text(
            client,
            deployment,
            'Write one compliant product blurb grounded only in the evidence.',
            product,
            fallback,
            max_tokens=180,
        )


def enrich_interactions(node, client, deployment):
    name = display_name(node)
    for it in node.get("crm", {}).get("interactions", []):
        fallback = (
            f"{it['interaction_date']} {it['channel']} with {name}: purpose {it['purpose_code']} ended as "
            f"{it['outcome_code']} with sentiment {it['sentiment_score']}. "
            f"Linked opportunity {it.get('linked_opportunity_id') or 'none'} and ticket {it.get('linked_ticket_id') or 'none'} were checked."
        )
        it["note"] = ai_text(client, deployment, "Write a concise RM interaction note.", {"customer": name, "interaction": it},
                             fallback, max_tokens=220)


def enrich_emails(node, client, deployment):
    name = display_name(node)
    for thread in node.get("crm", {}).get("email_threads", []):
        thread["thread_summary"] = ai_text(
            client,
            deployment,
            "Write a one-sentence email thread summary.",
            {"customer": name, "thread": {k: v for k, v in thread.items() if k != "messages"}},
            f"{thread['subject']} thread for {name}, started {thread['thread_start_date']}, with {thread['message_count']} messages and status {thread['resolution_status']}.",
            max_tokens=160,
        )
        for msg in thread.get("messages", []):
            fallback = (
                f"Regarding '{thread['subject']}', {msg['sender_role']} records: {msg['deterministic_intent']}. "
                f"This message stays within the known relationship facts for {name} and does not add new figures."
            )
            msg["body_text"] = ai_text(client, deployment, "Write a short grounded email body for this message.",
                                       {"customer": name, "thread_subject": thread["subject"], "message": msg},
                                       fallback, max_tokens=180)


def enrich_meetings(node, client, deployment):
    name = display_name(node)
    for mtg in node.get("crm", {}).get("meeting_summaries", []):
        evidence = {"customer": name, "meeting": mtg}
        mtg["agenda_text"] = ai_text(client, deployment, "Write a meeting agenda from evidence.", evidence,
                                     f"Review {name} relationship items linked to {', '.join(mtg.get('linked_items', []))}.",
                                     max_tokens=150)
        mtg["discussion_summary"] = ai_text(
            client,
            deployment,
            "Write a concise meeting discussion summary.",
            evidence,
            f"Attendees {', '.join(mtg.get('attendees', []))} reviewed {', '.join(mtg.get('linked_items', []))} and agreed that the RM should document follow-ups before any product or credit action.",
            max_tokens=260,
        )
        mtg["decisions"] = [
            f"Use only verified Contoso Bank records for {name}.",
            "Do not proceed with advisory or credit action until mandatory checks are complete.",
        ]
        mtg["action_items"] = [
            {"owner": "RM", "action": "Update CRM note and next action", "due_date": mtg.get("follow_up_date") or mtg["meeting_date"]},
            {"owner": "Customer", "action": "Provide pending documents where applicable", "due_date": mtg.get("follow_up_date") or mtg["meeting_date"]},
        ]


def enrich_tickets(node, client, deployment):
    name = display_name(node)
    for ticket in node.get("operations", {}).get("service_tickets", []):
        n = ticket.get("complaint_narrative", {})
        base = {"customer": name, "ticket": {k: v for k, v in ticket.items() if k != "complaint_narrative"}}
        reopened = ticket.get("reopened_count", 0)
        fallback_verbatim = (
            f"I need Contoso Bank to resolve {ticket['sub_category']} on ticket {ticket['ticket_id']}; "
            f"the delay is affecting my confidence in the relationship."
        )
        n["customer_verbatim"] = ai_text(client, deployment, "Write a customer complaint verbatim grounded in the ticket.",
                                         base, fallback_verbatim, max_tokens=200)
        n["agent_notes"] = ai_text(
            client,
            deployment,
            "Write internal agent notes for this service ticket.",
            base,
            f"Ticket {ticket['ticket_id']} for {name} is categorised as {ticket['category']}/{ticket['sub_category']}, priority {ticket['priority']}, SLA breach {ticket['sla_breach_flag']} and reopened {reopened} time(s).",
            max_tokens=220,
        )
        n["resolution_note"] = ai_text(
            client,
            deployment,
            "Write a resolution note or current status note.",
            base,
            f"Status is {ticket['status']}; the RM must acknowledge the issue before any new pitch and record closure evidence when operations confirms resolution.",
            max_tokens=180,
        )
        n["root_cause_code"] = ticket["sub_category"]
        n["emotion_label"] = "FRUSTRATED" if ticket.get("sla_breach_flag") or reopened else "NEUTRAL"
        n["escalation_language_flag"] = bool(ticket.get("sla_breach_flag") or reopened)


def enrich_opportunities(bundle, node, client, deployment):
    products = product_map(bundle)
    name = display_name(node)
    opps = {o["opp_id"]: o for o in node.get("crm", {}).get("opportunities", [])}
    for opp in opps.values():
        product = products.get(opp["product_id"], {"product_name": opp["product_id"]})
        evidence = {"customer": name, "opportunity": opp, "product": product}
        fallback = (
            f"{product['product_name']} was surfaced for {name} from {opp['source']} on {opp['created_date']} "
            f"with stage {opp['stage']} and expected value {inr(opp['expected_value_inr'])}. "
            f"Proceed only after service blockers, suitability and documented customer need are checked."
        )
        opp["reason"] = ai_text(client, deployment, "Write an opportunity reason grounded in the evidence.",
                                evidence, fallback, max_tokens=220)
        if opp.get("suitability_checked_flag"):
            opp["suitability_note"] = f"Suitability gate is marked checked for {product['product_name']}; RM must still evidence current facts before recommendation."
        else:
            opp["suitability_note"] = f"Suitability evidence is missing for {product['product_name']}; do not treat this as a valid recommendation until the RM documents need, risk fit and consent."
        if opp.get("loss_reason_text") == "":
            opp["loss_reason_text"] = (
                f"Marked lost because {opp.get('loss_reason_code')} applies; do not re-pitch {product['product_name']} without acknowledging this prior response."
            )
    for offer in node.get("crm", {}).get("offer_responses", []):
        if offer.get("decline_reason_text") == "":
            opp = opps.get(offer["opp_id"], {})
            product = products.get(opp.get("product_id"), {"product_name": opp.get("product_id", "the product")})
            offer["decline_reason_text"] = ai_text(
                client,
                deployment,
                "Write a grounded decline reason for this offer response.",
                {"customer": name, "offer": offer, "opportunity": opp, "product": product},
                f"{name} declined or deferred {product['product_name']} on {offer.get('response_date')}; record the prior refusal before any future outreach.",
                max_tokens=180,
            )


def enrich_credit_and_ops(node, client, deployment):
    name = display_name(node)
    for col in node.get("collateral", []):
        overdue = col.get("next_valuation_due") and col["next_valuation_due"] < "2026-03-31"
        fallback = (
            f"{col['collateral_type']} for {name} is assessed at {inr(col['assessed_value_inr'])}, "
            f"valued on {col['valuation_date']} with next valuation due {col['next_valuation_due']}. "
            f"{'The valuation is overdue and should be refreshed before renewal.' if overdue else 'The valuation date is within the recorded review cycle.'}"
        )
        col["description"] = ai_text(client, deployment, "Write a collateral description grounded in the record.",
                                     {"customer": name, "collateral": col}, fallback, max_tokens=190)
    for cov in node.get("covenants", []):
        fallback = (
            f"{cov['metric_code']} covenant on {cov['facility_id']} requires {cov['threshold_operator']} {cov['threshold_value']}; "
            f"latest observed value is {cov.get('observed_value')} on {cov['last_tested_date']} with result {cov['last_test_result']}."
        )
        cov["description"] = ai_text(client, deployment, "Write a covenant description grounded in threshold and observed value.",
                                     {"customer": name, "covenant": cov}, fallback, max_tokens=170)
    for event in node.get("operations", {}).get("trade_finance_events", []):
        if event.get("note") == "":
            fallback = (
                f"{event['instrument_type']} {event['instrument_ref']} for {name} was recorded on {event['event_date']} "
                f"for {inr(event['amount_inr'])} with status {event['status']}."
            )
            event["note"] = ai_text(client, None if client is None else deployment,
                                    "Write a one-sentence trade finance event note.",
                                    {"customer": name, "event": event}, fallback, max_tokens=120)
    for doc in node.get("operations", {}).get("documents", []):
        fallback = (
            f"{doc['doc_title']} ({doc['doc_type']}) for {name}, dated {doc['doc_date']}, is synthetic evidence "
            f"used to ground RM Assist; page count {doc['page_count']} and sensitivity {doc['sensitivity_class']}."
        )
        doc["extracted_text"] = ai_text(client, deployment, "Write a short synthetic document extract grounded in metadata.",
                                        {"customer": name, "document": doc}, fallback, max_tokens=150)
    for sig in node.get("external_signals", []):
        fallback_headline = f"{sig['signal_type']} signal for {name} on {sig['signal_date']}"
        sig["headline"] = ai_text(client, deployment, "Write a fictional external signal headline.",
                                  {"customer": name, "signal": sig}, fallback_headline, max_tokens=80)
        fallback_summary = (
            f"Synthetic {sig['source_name']} signal with severity {sig['severity_score']} and confidence {sig['confidence_score']}; "
            f"use only as corroborating evidence with internal Contoso Bank records."
        )
        sig["summary_text"] = ai_text(client, deployment, "Write a fictional external signal summary grounded in the signal fields.",
                                      {"customer": name, "signal": sig}, fallback_summary, max_tokens=160)


def enrich_arcs(node, client, deployment):
    name = display_name(node)
    for arc in node.get("six_month_arc", []):
        fallback = (
            f"In {arc['month']}, {name} triggered {arc['event_code']}. Grounded facts: "
            f"{compact_facts(arc.get('facts'))}."
        )
        arc["narrative"] = ai_text(client, deployment, "Write one grounded six-month arc sentence.",
                                   {"customer": name, "arc": arc}, fallback, max_tokens=170)


def enrich_customers(bundle, client, deployment):
    for cid, node in bundle.get("customers", {}).items():
        enrich_profile(bundle, cid, node, client, deployment)
        enrich_interactions(node, client, deployment)
        enrich_emails(node, client, deployment)
        enrich_meetings(node, client, deployment)
        enrich_tickets(node, client, deployment)
        enrich_opportunities(bundle, node, client, deployment)
        enrich_credit_and_ops(node, client, deployment)
        enrich_arcs(node, client, deployment)
        log.info("Enriched narratives for %s", cid)


def blank_narrative_paths(obj, path=""):
    blanks = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else key
            if key in NARRATIVE_STRING_KEYS and isinstance(value, str) and not value.strip():
                blanks.append(child)
            elif key in NARRATIVE_LIST_KEYS and isinstance(value, list) and not value:
                blanks.append(child)
            else:
                blanks.extend(blank_narrative_paths(value, child))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            blanks.extend(blank_narrative_paths(value, f"{path}[{idx}]"))
    return blanks


def enrich_bundle(bundle, client=None, deployment=None):
    enrich_products(bundle, client, deployment)
    enrich_rms(bundle, client, deployment)
    enrich_customers(bundle, client, deployment)
    bundle["meta"]["narrative_enriched"] = True
    bundle["meta"]["enriched_at"] = ENRICHED_AT
    bundle["meta"]["enrichment_model"] = deployment or "offline-template"


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich Contoso Bank dataset narratives.")
    parser.add_argument("--offline", action="store_true", help="Force deterministic templated text.")
    parser.add_argument("--path", default=str(JSON_PATH), help="Dataset JSON path.")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        log.error("%s not found. Run generate_contosobank.py first.", path)
        return 1
    bundle = json.loads(path.read_text(encoding="utf-8"))

    client = deployment = None
    if not args.offline:
        try:
            client, deployment = _aoai_client()
            log.info("Azure OpenAI ready (deployment=%s).", deployment)
        except Exception as exc:
            log.warning("Azure OpenAI unavailable (%s); using offline templates.", exc)
            client = deployment = None

    enrich_bundle(bundle, client, deployment)
    blanks = blank_narrative_paths(bundle)
    if blanks:
        for item in blanks[:20]:
            log.error("Blank narrative field remains: %s", item)
        log.error("%d blank narrative fields remain.", len(blanks))
        return 1

    path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("Wrote enriched dataset -> %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
