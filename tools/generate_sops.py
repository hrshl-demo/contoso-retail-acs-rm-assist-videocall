#!/usr/bin/env python3
"""
tools/generate_sops.py

ONE-TIME generator for the Contoso Bank RM-Assist SOP corpus
(docs/sop/contosobank_*.md).

Runs on the Azure VM after keyless Entra sign-in using Azure OpenAI, matching
the repository's reference implementation style:

    OpenAI(base_url = <FOUNDRY_AOAI_ENDPOINT>/openai/v1,
           api_key  = get_bearer_token_provider(DefaultAzureCredential(),
                                                "https://ai.azure.com/.default"))

Azure OpenAI is optional. With no FOUNDRY_AOAI_ENDPOINT (or --offline), the
script emits a detailed deterministic corpus from the structured TOPICS model.

Generated markdown is written under a namespace prefix so the curated
docs/sop/01_*.md ... docs/sop/20_*.md files are preserved:

    docs/sop/contosobank_<slug>.md

Usage:
    python tools\\generate_sops.py --offline
    python tools\\generate_sops.py
    python tools\\generate_sops.py --only kyc
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_sops")

REPO_ROOT = Path(__file__).resolve().parents[1]
SOP_DIR = REPO_ROOT / "docs" / "sop"
DATASET = REPO_ROOT / "data" / "contosobank" / "contosobank_dataset.json"

FIRM = "Contoso Bank"
FILE_PREFIX = "contosobank_"

# ---------------------------------------------------------------------------
# Structured topic model. Each topic can render a detailed deterministic SOP
# and can prompt the model for richer prose. The first six mirror the RM-Assist
# use cases: RTL-1, RTL-2, MSM-1, MSM-2, CRP-1 and CRP-2.
# ---------------------------------------------------------------------------
TOPICS = [
    {
        "slug": "01_retail_client_pulse_premeeting_capture",
        "title": "Retail Client Pulse, Pre-Meeting Brief and Post-Interaction Capture",
        "scope": "Generating a cited one-page relationship brief before a Priority retail client interaction, then drafting the post-interaction note, commitments and follow-ups for RM approval.",
        "regulatory": [
            "RBI Master Direction - Know Your Customer (KYC) Direction, 2016, as amended",
            "Digital Personal Data Protection Act, 2023 (DPDP) - purpose limitation, consent and data-principal rights",
            "RBI customer service and Fair Practices expectations for transparent, non-misleading communication",
            "RBI Integrated Ombudsman Scheme, 2021 - complaint visibility before commercial outreach",
            "Internal Contoso Bank entitlement, CRM audit-trail and call-recording policies",
        ],
        "preconditions": [
            "Requesting RM has an active portfolio mapping for the customer or household on the interaction date.",
            "Customer identity is resolved to one primary CUST_ID and permitted family-linked IDs only where consent and entitlement exist.",
            "KYC, risk profile, complaint, account, loan, card, investment and interaction data sources have refreshed successfully.",
            "Live call capture is used only after clear consent to recording/transcription; otherwise the RM may dictate a summary after the meeting.",
            "RM Assist is internal-only: it drafts, cites and structures information; it never contacts the customer directly.",
        ],
        "procedure": [
            "Trigger the brief from a calendar event, CRM task, inbound call routing or explicit RM request such as 'brief me on Rajesh Iyer'.",
            "Resolve customer identity and run an entitlement check against rm_portfolio_map; refuse and log any lookup outside the RM's book.",
            "Set the delta window from the last recorded customer interaction and retrieve only material changes since that date.",
            "Retrieve structured positions, service tickets, complaints, delinquency flags, KYC status, risk profile, investment holdings, open commitments and relevant policy snippets through separately-permissioned channels.",
            "Run deterministic blocking checks before any model synthesis: open or recently breached complaints, KYC due/overdue, expired risk profile, delinquency, suspected fraud and pending commitments.",
            "Generate a concise brief with relationship snapshot, what changed, open issues, ranked talking points, suggested opening line, compliance flags and source references.",
            "Require every number, date, product and assertion in the brief to map back to a retrieved source; drop unsupported statements instead of inferring.",
            "After the meeting, draft a CRM note, commitments, owners, dates, sentiment and next actions; present them to the RM for correction.",
            "Write to CRM only after explicit RM approval; record edit distance and dismissal reasons for monitored feedback, not self-modifying production behaviour.",
        ],
        "guardrails": [
            "Never allow free-form customer search or cross-book browsing; entitlement failure is a hard stop.",
            "Do not place a product pitch above an unresolved service failure, reopened complaint or Banking Ombudsman-sensitive matter.",
            "Do not generate investment, insurance or credit talking points when KYC, risk profile or mandatory suitability evidence is stale.",
            "Mask PAN, Aadhaar, account numbers and contact details in the brief unless the RM has a documented need-to-know.",
            "Do not write notes, promises or suitability rationale into CRM without RM approval.",
            "Do not fabricate sources, figures, dates or relationship history; state 'not found' where evidence is absent.",
        ],
        "escalation": [
            "Entitlement mismatch or attempted unauthorised access -> Compliance and Information Security review.",
            "Open, breached or reopened complaint -> service owner and Principal Nodal Officer path before commercial follow-up.",
            "Suitability conflict or missing evidence -> Branch Manager / Investment Product Compliance approval before proceeding.",
            "Ungrounded or contradictory source data -> manual RM note; raise a data-quality ticket to operations.",
        ],
    },
    {
        "slug": "02_retail_next_best_conversation_suitability",
        "title": "Retail Next Best Conversation and Suitability-Grounded Portfolio Prioritisation",
        "scope": "Creating a daily ranked worklist for retail RMs that identifies the households to contact today, with evidence, suitability gates and prior-decline suppression applied before ranking.",
        "regulatory": [
            "RBI Fair Practices Code principles on transparency, fairness and non-coercive selling",
            "RBI Master Direction - KYC and periodic updation requirements",
            "DPDP Act 2023 - purpose-bound processing and consented use of personal data",
            "SEBI/AMFI mutual fund distribution disclosures where bank staff distribute MF products",
            "IRDAI corporate-agent conduct and suitability expectations for insurance solicitation",
            "Internal Contoso Bank customer suitability, vulnerable-customer and do-not-disturb policies",
        ],
        "preconditions": [
            "Customer has an active relationship and permitted marketing/contact preference for the proposed channel.",
            "KYC is not overdue and the relevant risk profile or needs analysis is current for wealth, insurance and investment conversations.",
            "Any prior product decline, complaint, vulnerability flag, recent bereavement, fraud case or collections status is available to the ranking process.",
            "Campaign, model-generated and RM-sourced opportunities are deduplicated at household level before scoring.",
            "RM remains the decision-maker and may dismiss a recommendation with a recorded reason.",
        ],
        "procedure": [
            "Ingest candidate opportunities from balances, maturities, card revolve behaviour, loan enquiries, salary credits, SIP gaps, insurance renewal dates, complaint status and RM notes.",
            "Apply hard pre-filters for contact consent, KYC validity, risk profile, prior decline cooling-off period, collections blocks and product eligibility.",
            "Score remaining opportunities using customer need, recency, relationship value, customer-stated objective, service context, probability of acceptance and suitability fit.",
            "Rank a manageable daily worklist, typically the top 10-15 households, with one primary reason and two supporting evidence points per customer.",
            "Annotate every recommended conversation as service recovery, advice, retention, credit need, deposit need, protection need or documentation follow-up.",
            "For investment or insurance leads, generate a suitability rationale placeholder that the RM must complete and approve before recording an opportunity.",
            "Capture RM dismissals such as 'already declined', 'complaint open', 'family event' or 'wrong product' and feed them to governed model monitoring.",
            "Review conversion, complaint-backfire rate, suitability exceptions and customer opt-outs weekly with the Branch Sales Manager and Compliance.",
        ],
        "guardrails": [
            "No recommendation may bypass a deterministic eligibility or suitability gate because of sales target pressure.",
            "Do not re-pitch a product declined in the last 180 days unless the reason for decline has demonstrably changed and is shown to the RM.",
            "Do not use sensitive personal data beyond the consented banking purpose; keep health, religion, caste and similar attributes out of ranking.",
            "Do not recommend ULIPs, mutual funds, insurance or structured products as guaranteed-return substitutes.",
            "Do not propose fresh credit to an account in active collections, fraud dispute, unresolved complaint or hardship review.",
        ],
        "escalation": [
            "Suitability override requested -> Investment Product Compliance or authorised supervisor approval.",
            "High complaint-risk lead surfaced -> Branch Manager review before outreach.",
            "Repeated opt-outs or DND breach risk -> CRM contact-governance queue.",
            "Model ranking drift or biased concentration -> Analytics Model Risk and Compliance review.",
        ],
    },
    {
        "slug": "03_msme_working_capital_renewal_copilot",
        "title": "MSME Working Capital Renewal Copilot",
        "scope": "Assembling an MSME working-capital renewal pack with drawing-power reconstruction, document status, covenant checks, peer benchmarking, deviations and a drafted credit note for human review.",
        "regulatory": [
            "RBI Master Direction on lending to Micro, Small and Medium Enterprises (MSME), including Udyam classification treatment",
            "RBI Prudential Norms on Income Recognition, Asset Classification and Provisioning (IRAC)",
            "RBI Fair Practices Code for lenders and transparent sanction terms",
            "RBI guidelines on credit information reporting and collateral/security documentation",
            "Internal Contoso Bank credit delegation, stock-audit, collateral-valuation and renewal policies",
        ],
        "preconditions": [
            "Borrower KYC, beneficial ownership, Udyam registration, GST profile and constitution documents are current or exception-tagged.",
            "Existing facility, sanction letter, stock statement, debtor/creditor ageing, insurance, collateral and repayment records are retrievable.",
            "Last audited financials, provisional financials and bank statements are available or marked as pending with owner and date.",
            "RM Assist has access only to evidence required for the renewal and cannot approve limits or pricing.",
            "All calculations are deterministic and displayed as working papers for credit officer validation.",
        ],
        "procedure": [
            "Create the renewal checklist 90 days before expiry and flag files not started by the policy threshold.",
            "Collect financial statements, GST returns, bank statements, stock statements, insurance, collateral valuations, Udyam evidence, charge records and existing sanction conditions.",
            "Reconstruct drawing power from eligible stock and receivables after applying margins, ageing exclusions, unpaid creditors and stock-statement date checks.",
            "Compare actual utilisation, overdrawn days, inward/outward cheque returns, LC devolvements, BG invocation, GST delays and account conduct against the prior sanction assumptions.",
            "Run covenant and security-condition tests, including DSCR, current ratio, debtor days, stock submission discipline, collateral coverage and insurance currency.",
            "Benchmark working-capital cycle, utilisation and margin against the borrower's sector and peer band where permitted data exists.",
            "Draft the renewal note with requested limits, recommended limits, deviations, mitigants, early warning observations and pending documents.",
            "Route the draft to RM, Credit Analyst and approving authority; approvals, deviations and changes remain outside the model.",
        ],
        "guardrails": [
            "Do not auto-renew a limit or communicate sanction terms without credit approval.",
            "Do not treat late stock statements, overdue valuations or unresolved covenant breaches as clerical issues; surface them as credit deviations.",
            "Do not use promoter retail wealth data unless a permitted banking purpose and entitlement exists.",
            "Do not suppress stress indicators because the account is strategically important or revenue-generating.",
            "Do not change Udyam/MSME classification without documentary evidence.",
        ],
        "escalation": [
            "Renewal due within 30 days with material documents pending -> Cluster Head and Regional Credit Manager.",
            "Drawing-power shortfall, excess drawings or stale valuation -> Credit Risk and Operations hold on enhancement.",
            "Covenant breach, SMA event or LC/BG stress -> Credit Monitoring Cell.",
            "Suspected diversion of funds or related-party anomaly -> Compliance and Fraud Risk review.",
        ],
    },
    {
        "slug": "04_msme_portfolio_early_warning_triage",
        "title": "MSME Portfolio Early Warning and Triage",
        "scope": "Running a weekly portfolio-wide stress scan for MSME RMs that correlates utilisation, submission discipline, transaction behaviour, covenant tests, relationship notes and external signals into a triaged intervention list.",
        "regulatory": [
            "RBI IRAC norms including STANDARD, SMA-0, SMA-1, SMA-2 and NPA asset classification",
            "RBI Framework for Revitalising Distressed Assets in the Economy and corrective action principles",
            "RBI reporting expectations for Special Mention Accounts where applicable",
            "RBI Fair Practices Code and respectful borrower communication standards",
            "Internal Contoso Bank Early Warning Signal (EWS), watchlist and credit monitoring policy",
        ],
        "preconditions": [
            "Facility master, limit-utilisation, repayments, stock statements, cheque returns, covenant results and trade finance events are refreshed.",
            "Seasonality calendars and borrower-sector context are available to avoid mistaking known festive spikes for stress.",
            "RM and credit teams have agreed triage categories: monitor, intervene, credit review, recovery or exit.",
            "Customer communication templates have been reviewed for Fair Practices and non-coercive language.",
            "No adverse classification change is made solely by model output; credit policy classification remains system-of-record driven.",
        ],
        "procedure": [
            "Run a weekly scan across the MSME book and compute signals such as sustained utilisation above 90%, excess over drawing power, delayed stock statements, debtor-day elongation, cheque returns, GST delays and LC devolvement.",
            "Separate seasonal working-capital bulges from anomalous persistence by comparing the borrower with its own history and peer-sector patterns.",
            "Correlate structured signals with RM notes, complaint narratives, email commitments, external sector alerts and buyer/payment stress indicators.",
            "Classify each account into evidence-backed triage buckets and show why it was selected, not just the score.",
            "Generate a recommended intervention: documentation chase, unit visit, stock audit, repayment discussion, temporary ad-hoc review, restructuring assessment or watchlist note.",
            "Assign an owner and due date for each intervention; unresolved items reappear until closed or escalated.",
            "Review top risks with Cluster Head and Credit Monitoring; record RM feedback on false positives and missed stress.",
        ],
        "guardrails": [
            "Do not label a borrower stressed without disclosing the exact evidence and the classification basis.",
            "Do not recommend recovery action before exploring corrective or restructuring options appropriate to the account stage.",
            "Do not downgrade, freeze or recall limits solely from an AI-generated narrative.",
            "Do not ignore positive curing events; show both deterioration and remediation evidence.",
            "Do not disclose watchlist or EWS labels to unauthorised customer contacts.",
        ],
        "escalation": [
            "SMA-1 or worse, repeated excess drawings or cheque-return cluster -> Regional Credit Manager and Credit Monitoring Cell.",
            "Suspected stock inflation, fund diversion or false invoices -> Fraud Risk and audit-led stock verification.",
            "Viable stress requiring relief -> restructuring desk under approved policy.",
            "Model misses later-confirmed stress -> Model Risk review and signal recalibration.",
        ],
    },
    {
        "slug": "05_corporate_group_relationship_wallet_whitespace",
        "title": "Corporate Group Relationship Intelligence and Wallet Whitespace",
        "scope": "Producing an on-demand group-level brief for corporate RMs that consolidates exposure, utilisation, float, fees, covenants, group structure and transaction-derived wallet whitespace across entities.",
        "regulatory": [
            "RBI Large Exposures Framework and group exposure aggregation expectations",
            "RBI Master Direction - KYC, including beneficial ownership and legal entity customer due diligence",
            "RBI guidelines on customer service, transparent charges and dispute handling",
            "DPDP Act 2023 and internal information-barrier rules for linked personal and corporate relationships",
            "Internal Contoso Bank credit delegation, pricing, treasury suitability and conflict-management policies",
        ],
        "preconditions": [
            "Legal entity group structure, authorised signatories, beneficial owners and effective dates are current.",
            "Credit facilities, utilisation, collateral, covenants, fee income, CASA float, transaction banking and trade records are mapped at entity and group level.",
            "Any personal/private-banking relationship of corporate officers is separated by entitlement and used only where a permitted purpose exists.",
            "Fee disputes, uncommunicated charge changes and open service tickets are visible before any wallet conversation.",
            "Wallet estimates are labelled as observed evidence, inference or RM input; they are not presented as facts unless sourced.",
        ],
        "procedure": [
            "Resolve the corporate group as-of the requested date, including acquisitions, subsidiaries, borrower/non-borrower entities and links effective mid-period.",
            "Aggregate sanctioned and outstanding exposure, utilisation, collateral, covenants, pricing, fees, float and profitability by entity and group.",
            "Identify open disputes, uncommunicated fee changes, pending documentation and covenant concerns that should frame the RM's meeting.",
            "Analyse transaction flows for evidence of competitor wallet: dealer credits, forex flows, salary payments, CMS collections, LC/BG activity and supply-chain patterns.",
            "Classify whitespace as directly observed, strongly inferred or exploratory; attach evidence and confidence level.",
            "Draft meeting talking points that separate service recovery, credit review, transaction banking, treasury, salary mandate and supply-chain finance themes.",
            "Record RM edits, customer feedback and actual wallet outcomes for governed monitoring.",
        ],
        "guardrails": [
            "Do not use a CFO's personal banking information to sell corporate products unless entitlement and purpose are documented.",
            "Do not state competitor balances or wallet share as fact when only transaction evidence is available.",
            "Do not ignore fee disputes or complaint escalation when proposing new products.",
            "Do not recommend treasury or derivative products without treasury suitability and authorised-dealer controls.",
            "Do not aggregate unrelated entities merely because names look similar; use verified group links and effective dates.",
        ],
        "escalation": [
            "Group exposure nearing internal or RBI large-exposure thresholds -> Credit Risk and Group Exposure Committee.",
            "Fee dispute or uncommunicated charge change -> Client Service Head and Principal Nodal Officer route if complaint persists.",
            "Information-barrier ambiguity -> Compliance before using linked relationship data.",
            "High-value wallet proposal -> product specialist and pricing approval workflow.",
        ],
    },
    {
        "slug": "06_corporate_annual_review_covenant_compliance",
        "title": "Corporate Annual Review and Covenant Compliance Copilot",
        "scope": "Continuously computing covenant status and assembling annual review packs for corporate groups with full provenance, while leaving credit judgement and approval with authorised humans.",
        "regulatory": [
            "RBI Prudential Norms on IRAC and credit monitoring",
            "RBI Large Exposures Framework and related borrower-group exposure controls",
            "RBI Master Circulars / Directions on loans and advances and end-use monitoring",
            "Companies Act and board-authorisation evidence relevant to corporate borrowing",
            "Internal Contoso Bank annual review, covenant testing, credit rating and delegation policies",
        ],
        "preconditions": [
            "Latest audited/provisional financials, stock statements, valuation reports, sanction letters, board resolutions and covenant definitions are digitised or exception-tagged.",
            "Facility and covenant masters capture formula, frequency, threshold, cure period, test date and evidence source.",
            "Entity-level and group-level exposures reconcile with core credit systems.",
            "Exceptions, waivers and amendments are captured from approval workflows, not inferred from emails alone.",
            "Credit analyst and approver review is mandatory before any annual review pack is submitted.",
        ],
        "procedure": [
            "Maintain a covenant calendar across all facilities and entities, showing upcoming tests, overdue evidence and cure periods.",
            "Compute each covenant deterministically from sourced financial, utilisation or operational data and show the calculation line-by-line.",
            "Detect trajectory risk where a covenant is within a policy buffer of breach, even if the current test has passed.",
            "Assemble the annual review pack: group structure, exposure, conduct, financial performance, covenant status, collateral, pricing, ESG/sector notes, external ratings and pending conditions.",
            "Highlight deviations: overdue annual review, covenant breach or near-breach, valuation expiry, end-use concern, unregularised excess, litigation or adverse external signal.",
            "Draft the review narrative with source references and separate facts, calculations, RM commentary and credit recommendation sections.",
            "Route to RM, Credit Analyst, Product Specialists and approving authority; track edits and final approval decisions.",
        ],
        "guardrails": [
            "Do not amend, waive or cure a covenant without recorded approving-authority action.",
            "Do not convert a near-breach into a pass/fail conclusion without the stated policy threshold and buffer.",
            "Do not submit an annual review pack with missing mandatory documents unless the exception owner, due date and approving authority are shown.",
            "Do not hide group entities or off-balance-sheet exposures from consolidated review.",
            "Do not let generated narrative override deterministic covenant calculations.",
        ],
        "escalation": [
            "Actual covenant breach -> Credit Monitoring Cell, Relationship Head and approving authority under cure/waiver policy.",
            "Annual review overdue or repeated missing evidence -> Business Head and Credit Administration.",
            "Exposure concentration or large-exposure concern -> Group Exposure Committee.",
            "Material adverse news, rating downgrade or suspected end-use issue -> Credit Risk and Compliance.",
        ],
    },
    {
        "slug": "07_kyc_rekyc_ckyc_vcip",
        "title": "KYC, Re-KYC, CKYC and V-CIP for Banking Relationships",
        "scope": "Opening, maintaining and periodically updating KYC for retail, MSME and corporate customers, including beneficial ownership, CKYC, video KYC and re-KYC blocks on new business.",
        "regulatory": [
            "RBI Master Direction - Know Your Customer (KYC) Direction, 2016, as amended",
            "Prevention of Money Laundering Act, 2002 and PMLA Rules",
            "Central KYC Registry (CKYCR) operating requirements",
            "RBI Video-based Customer Identification Process (V-CIP) requirements",
            "DPDP Act 2023 for personal-data notice, consent and retention discipline",
        ],
        "preconditions": [
            "Customer category is identified: individual, proprietor, partnership, company, LLP, trust, HUF, NRI/NRE/NRO or authorised signatory.",
            "Officially Valid Documents, PAN/Form 60, current address, photographs and contact details are available as applicable.",
            "For non-individuals, constitution documents, board/partner resolutions, authorised signatories and beneficial owners are captured.",
            "Risk category is assigned and periodic updation due date is calculated based on risk.",
            "Any V-CIP session uses approved infrastructure, geotagging, liveness and audit recording.",
        ],
        "procedure": [
            "Collect and verify OVD, PAN/Form 60, address, contact, occupation/business, income/turnover and source-of-funds information.",
            "Screen customer, beneficial owners and authorised signatories against sanctions, PEP and adverse-media lists per AML policy.",
            "Check CKYC and existing customer records; reuse valid KYC where permitted and update gaps instead of duplicating records.",
            "For V-CIP, obtain consent, verify live presence, capture required images, confirm location within permitted jurisdiction and record the session.",
            "Assign risk category and next periodic updation date; trigger enhanced due diligence for high-risk, PEP, complex ownership or unusual activity.",
            "Before any new loan, limit increase, investment, insurance or account activation, run a KYC status gate.",
            "For re-KYC due/overdue cases, offer the lowest-friction permitted channel such as self-declaration, V-CIP or branch visit based on change type and risk.",
            "Store KYC evidence in approved repositories with masking, retention and access logging.",
        ],
        "guardrails": [
            "Do not open or activate a relationship where mandatory KYC or beneficial-owner evidence is missing.",
            "Do not read out or display full Aadhaar, PAN or account identifiers unless legally required and access-controlled.",
            "Do not rely on expired documents, stale address proof or unverifiable beneficial ownership.",
            "Do not use KYC documents for sales or analytics purposes beyond the notified purpose.",
            "Do not accept third-party introductions as a substitute for Contoso Bank's own customer due diligence.",
        ],
        "escalation": [
            "PEP, sanctions, adverse media or complex ownership -> AML Compliance and Principal Officer.",
            "KYC mismatch, suspected impersonation or V-CIP failure -> Operations hold and Fraud Risk.",
            "High-risk customer onboarding -> enhanced due diligence approval.",
            "Data breach or unauthorised KYC access -> DPO, Information Security and Compliance.",
        ],
    },
    {
        "slug": "08_dpdp_consent_data_handling",
        "title": "DPDP Consent, Recording and Data Handling for RM Assist",
        "scope": "Managing consent, call recording, transcription, personal-data use, retention, masking and data-principal rights for RM Assist workflows across retail, MSME and corporate banking.",
        "regulatory": [
            "Digital Personal Data Protection Act, 2023",
            "RBI Cyber Security Framework / information security expectations for banks",
            "RBI KYC and customer confidentiality requirements",
            "Information Technology Act and applicable CERT-In reporting expectations",
            "Internal Contoso Bank privacy, data-retention, role-based access and incident-response policies",
        ],
        "preconditions": [
            "A clear notice explains purpose, data categories, retention, recording/transcription use and customer rights.",
            "Consent status and channel preference are available before live capture or data use beyond core servicing.",
            "Role-based access and data minimisation rules are configured for RM, credit, operations, compliance and analytics users.",
            "Transcription, summarisation and storage services are approved for bank data and subject to logging.",
            "Data-principal request and breach escalation queues are operational.",
        ],
        "procedure": [
            "Before recording or live transcription, inform the customer of the purpose and obtain consent in the approved script or digital flow.",
            "Process personal data only for notified banking purposes such as servicing, suitability, credit assessment, complaint handling and regulatory compliance.",
            "Minimise prompts and outputs: include only fields required for the SOP workflow and mask PAN, Aadhaar, account numbers and mobile numbers where possible.",
            "Separate customer data, policy data and model-monitoring data; restrict cross-use unless a lawful purpose and approval exist.",
            "Store recordings, transcripts, prompts, outputs and approvals in approved repositories with retention labels and access logs.",
            "Provide data-principal rights handling for access, correction, grievance and withdrawal where applicable, subject to banking retention obligations.",
            "Monitor for privacy incidents, unusual access, excessive downloads and prompt/output leakage.",
        ],
        "guardrails": [
            "Do not record, transcribe or summarise a customer call without consent where consent is required.",
            "Do not paste customer data into unapproved public tools or third-party systems.",
            "Do not use personal data collected for KYC, complaint or hardship servicing to target unrelated sales campaigns without a permitted purpose.",
            "Do not expose one customer's or group entity's data in another customer's brief.",
            "Do not retain raw transcripts longer than the approved retention schedule.",
        ],
        "escalation": [
            "Consent dispute or withdrawal conflict -> Privacy Office / DPO.",
            "Suspected personal-data breach -> DPO, Information Security, Legal and regulatory notification workflow.",
            "Unauthorised access or cross-book browsing -> Compliance and disciplinary process.",
            "Customer privacy grievance unresolved -> Principal Nodal Officer / grievance channel.",
        ],
    },
    {
        "slug": "09_complaint_grievance_integrated_ombudsman",
        "title": "Complaint, Grievance and RBI Integrated Ombudsman Handling",
        "scope": "Logging, resolving, escalating and learning from customer complaints across banking products, ensuring RM Assist surfaces open grievances before any sales or credit conversation.",
        "regulatory": [
            "RBI Integrated Ombudsman Scheme, 2021",
            "RBI customer service directions and Charter of Customer Rights",
            "RBI Fair Practices Code for lenders",
            "RBI circulars on limiting liability in unauthorised electronic banking transactions where applicable",
            "Internal Contoso Bank complaint TAT, Principal Nodal Officer and root-cause analysis policy",
        ],
        "preconditions": [
            "Complaint channel, date, customer identity, product, issue type, monetary impact and desired resolution are captured.",
            "A unique ticket number, owner, TAT and escalation path are assigned.",
            "RM Assist can retrieve open, breached, reopened and recently closed complaints for the customer or permitted group.",
            "Frontline staff have scripts for acknowledgement, empathy, interim updates and Ombudsman rights.",
            "Charge reversals, compensation and waivers follow delegation of authority.",
        ],
        "procedure": [
            "Acknowledge every complaint promptly with ticket number, owner, expected TAT and required documents.",
            "Classify the complaint: service failure, fee/charge dispute, unauthorised transaction, mis-selling, collections conduct, credit bureau issue, KYC or data privacy.",
            "Place open, breached or reopened complaints at the top of any RM brief and mark commercial conversations as conditional until service recovery is addressed.",
            "Investigate using source records, call recordings, system logs, product terms and customer communication history.",
            "Provide interim updates when TAT risk exists; do not close without documented resolution or approved closure reason.",
            "If unresolved within policy/RBI timelines, inform the customer of escalation to Contoso Bank's Principal Nodal Officer and the RBI Integrated Ombudsman route.",
            "Perform root-cause tagging and feed systemic issues into product, operations, RM training or model guardrail updates.",
        ],
        "guardrails": [
            "Do not discourage, mislead or penalise a customer for approaching the RBI Ombudsman.",
            "Do not promise a waiver, compensation or reversal beyond delegated authority.",
            "Do not close a complaint merely because an RM made a call; evidence of resolution is required.",
            "Do not pitch new products while an unresolved complaint materially affects trust or suitability.",
            "Do not alter complaint categories to improve TAT metrics.",
        ],
        "escalation": [
            "Breached TAT or reopened complaint -> Principal Nodal Officer path.",
            "Mis-selling, coercive collections or data privacy allegation -> Compliance and relevant business head.",
            "Unauthorised electronic transaction -> Fraud Operations and liability framework queue.",
            "Repeat complaint pattern -> Operational Risk and root-cause corrective action.",
        ],
    },
    {
        "slug": "10_credit_underwriting_foir_affordability",
        "title": "Credit Underwriting, FOIR and Affordability Assessment",
        "scope": "Assessing retail and MSME borrower eligibility, repayment capacity, bureau conduct, FOIR/DSCR and documentation before recommending or approving credit.",
        "regulatory": [
            "RBI Fair Practices Code for lenders",
            "RBI guidelines on credit information reporting and borrower transparency",
            "RBI IRAC norms for existing repayment conduct and delinquency consideration",
            "RBI KYC / AML requirements for borrower and beneficial-owner identification",
            "Internal Contoso Bank credit policy, delegation, FOIR, LTV, DSCR and bureau-score norms",
        ],
        "preconditions": [
            "KYC/re-KYC is current for borrower, co-borrower, guarantor and beneficial owners as applicable.",
            "Income, turnover, bank statements, bureau report, existing obligations, collateral and loan purpose are verified.",
            "Product policy defines minimum age, employment/business vintage, bureau thresholds, FOIR/DSCR, LTV and negative-profile criteria.",
            "Any current delinquency, restructuring, complaint, fraud dispute or hardship flag is visible before recommendation.",
            "RM Assist may compute and explain eligibility but cannot approve or communicate final sanction.",
        ],
        "procedure": [
            "Capture loan purpose, requested amount, tenor, repayment source, collateral and customer-stated constraints.",
            "Verify income or cash flow using salary credits, ITR/Form 16, GST returns, audited financials, bank statements and employer/business evidence.",
            "Calculate existing monthly obligations from bureau, internal loans, credit cards, EMI bounces, guarantees and disclosed debts.",
            "Compute FOIR for individuals and DSCR/debt-service capacity for business borrowers using policy-defined inclusions and exclusions.",
            "Check bureau history, enquiry velocity, cheque/NACH bounces, SMA/DPD status, restructuring and write-off/settlement markers.",
            "Validate collateral, LTV/margin, insurance and legal/title checks where secured credit is involved.",
            "Generate an eligibility note with approved policy rules, deviations, mitigants, missing documents and reasons for decline or lower amount.",
            "Route deviations and exceptions to credit authority; communicate only approved, fair and transparent terms to the customer.",
        ],
        "guardrails": [
            "Do not offer fresh or enhanced credit to a customer in unresolved collections, active hardship or material dispute without credit approval.",
            "Do not exclude known obligations to manufacture FOIR eligibility.",
            "Do not treat bureau score alone as approval; repayment capacity and purpose must be assessed.",
            "Do not promise sanction, rate or waiver before approval.",
            "Do not discriminate on prohibited or sensitive personal attributes.",
        ],
        "escalation": [
            "FOIR/DSCR deviation -> Credit approver per delegation.",
            "Suspected income manipulation, mule account or undisclosed borrowing -> Fraud Risk and Credit Risk.",
            "Customer hardship or delinquency discovered -> collections/restructuring desk before new credit.",
            "Fair-lending or discrimination complaint -> Compliance and Grievance team.",
        ],
    },
    {
        "slug": "11_collections_restructuring_irac_sma_npa",
        "title": "Collections, Restructuring and IRAC Asset Classification",
        "scope": "Handling missed payments and borrower hardship fairly while preserving RBI IRAC classification discipline across STANDARD, SMA and NPA stages.",
        "regulatory": [
            "RBI Prudential Norms on Income Recognition, Asset Classification and Provisioning (IRAC)",
            "RBI Fair Practices Code and recovery-agent conduct requirements",
            "RBI Resolution Framework / restructuring guidelines as applicable to eligible borrowers",
            "RBI Integrated Ombudsman Scheme, 2021 for collections and servicing complaints",
            "Internal Contoso Bank collections, hardship, restructuring, repossession and write-off policies",
        ],
        "preconditions": [
            "Loan account, repayment schedule, DPD, bounce reason, contact consent, vulnerability and complaint status are known.",
            "Asset classification is system-derived and reconciled: STANDARD, SMA-0, SMA-1, SMA-2 or NPA.",
            "Borrower's hardship facts, repayment capacity and relief eligibility are documented before restructuring recommendation.",
            "Recovery-agent allocation, contact hours, scripts and conduct monitoring are approved.",
            "Any settlement, waiver, restructuring or legal notice follows delegated authority.",
        ],
        "procedure": [
            "At first missed payment, verify whether the issue is operational, dispute-related, fraud-related, income shock or willingness-to-pay.",
            "Classify by DPD and RBI IRAC stage: SMA-0 for early overdue, SMA-1 for 31-60 DPD, SMA-2 for 61-90 DPD and NPA beyond norms subject to product rules.",
            "For operational errors or unresolved disputes, route to service/fraud teams and avoid coercive collection language.",
            "For genuine hardship, assess temporary relief such as EMI date shift, step-up/step-down, moratorium/deferral, tenure extension or restructuring as policy permits.",
            "Explain that forbearance generally defers or reschedules obligations and does not automatically forgive principal or interest.",
            "Document customer consent, revised terms, bureau impact, classification impact and approval authority for any restructuring.",
            "Escalate non-cooperation, fraud, chronic delinquency or collateral risk to legal/recovery only after policy stages and notices.",
            "Monitor restructured accounts for performance, relapse and correct IRAC tagging.",
        ],
        "guardrails": [
            "Do not threaten arrest, police action, public shaming or asset seizure outside lawful due process.",
            "Do not contact family, employer, neighbours or unrelated third parties for repayment pressure without lawful basis and consent.",
            "Do not offer a new loan or limit increase as a cure for delinquency unless approved as a formal resolution strategy.",
            "Do not backdate payments, evergreen loans or mask DPD/SMA/NPA status.",
            "Do not promise interest/principal forgiveness without delegated approval.",
        ],
        "escalation": [
            "SMA-1 and above with viable borrower -> restructuring/hardship desk and Credit Monitoring.",
            "SMA-2/NPA risk, fraud or collateral impairment -> Credit Risk, Legal and Recovery Head.",
            "Collections conduct complaint -> Grievance team, Compliance and recovery-agent oversight.",
            "Any classification override request -> Finance, Credit Risk and authorised approving authority.",
        ],
    },
    {
        "slug": "12_mis_selling_suitability_third_party_products",
        "title": "Mis-Selling Prevention and Suitability for Third-Party Products",
        "scope": "Ensuring mutual funds, insurance, PMS referrals, deposits and other products are recommended only with documented customer need, risk fit, disclosures and consent.",
        "regulatory": [
            "RBI Fair Practices Code and customer-rights principles for bank distribution",
            "IRDAI corporate-agent / insurance solicitation conduct requirements",
            "SEBI and AMFI mutual fund distribution disclosure and riskometer expectations",
            "DPDP Act 2023 for consented processing of personal data in suitability assessment",
            "Internal Contoso Bank suitability, vulnerable-customer, conflict-of-interest and incentive-governance policies",
        ],
        "preconditions": [
            "Customer KYC is current and risk/needs profile is valid for the product category.",
            "Customer objective, horizon, liquidity need, risk appetite, income, dependants and existing exposure are documented.",
            "Product features, risks, charges, lock-in, surrender/exit loads, tax assumptions and conflicts are available in approved product notes.",
            "Prior declines, complaints, vulnerability flags and do-not-solicit preferences are checked.",
            "RM has required certification or routes to a qualified specialist where required.",
        ],
        "procedure": [
            "Start with the customer need or objective, not the product or incentive campaign.",
            "Match products to risk appetite, horizon, liquidity need, affordability and concentration; record why unsuitable alternatives were not selected.",
            "Disclose charges, risks, lock-in, surrender value, exit load, tax caveats, market risk, insurer/fund role and Contoso Bank's distributor role.",
            "For insurance, separate protection need from investment discussion and avoid return-led framing where protection is primary.",
            "For mutual funds, show riskometer/category fit and avoid past-performance-only recommendations.",
            "Capture customer questions, RM explanation, consent and suitability rationale before application submission.",
            "Suppress or escalate any sale where evidence shows complaint, vulnerability, cognitive concern, language barrier or product complexity mismatch.",
            "Review post-sale complaints, free-look cancellations, early surrenders and concentration to detect mis-selling patterns.",
        ],
        "guardrails": [
            "Do not guarantee returns, imply fixed maturity value for market-linked products or compare unlike products misleadingly.",
            "Do not bundle insurance or investments as mandatory for loan sanction unless legally/product-policy required and clearly disclosed.",
            "Do not sell high-lock-in or high-risk products to meet month-end targets without documented suitability.",
            "Do not hide commissions, charges, surrender penalties or product issuer identity.",
            "Do not continue a pitch after the customer declines or asks not to be contacted.",
        ],
        "escalation": [
            "Suitability mismatch or vulnerable customer -> supervisor and product compliance approval before proceeding.",
            "Mis-selling allegation -> complaint ticket, Compliance and root-cause review.",
            "Repeated cancellations or early surrenders by RM/product -> Sales Governance review.",
            "Incentive conflict concern -> Business Head and Compliance.",
        ],
    },
    {
        "slug": "13_msme_stock_statement_drawing_power_collateral",
        "title": "MSME Stock Statement, Drawing Power and Collateral Monitoring",
        "scope": "Controlling monthly stock statements, drawing power, insurance, collateral valuation and security perfection for MSME working-capital facilities.",
        "regulatory": [
            "RBI IRAC norms and credit monitoring expectations for working-capital accounts",
            "RBI MSME lending and fair-lending principles",
            "RBI guidelines on collateral/security documentation and charge registration where applicable",
            "SARFAESI and Companies Act charge-related requirements where applicable",
            "Internal Contoso Bank stock audit, drawing power, margin, collateral valuation and insurance policies",
        ],
        "preconditions": [
            "Facility sanction letter defines eligible stock/debtors, margins, stock-statement frequency, insurance and valuation requirements.",
            "Borrower submits stock, receivable, creditor and insurance data in approved format by due date.",
            "Collateral master, valuation date, charge status, insurance policy and linkage to facilities are current.",
            "System can identify excess over drawing power and ageing exclusions deterministically.",
            "Exceptions have owner, approval authority and target closure date.",
        ],
        "procedure": [
            "Track monthly stock-statement due dates and issue reminders before the due date, not after expiry.",
            "Validate stock and receivable values against GST, account turnover, debtor ageing, inspections and prior months for anomalies.",
            "Exclude ineligible or aged receivables and apply sanction margins to compute drawing power.",
            "Compare drawing power with outstanding utilisation; flag excess, frequent near-excess and stale-statement reliance.",
            "Track collateral valuation expiry, insurance coverage, charge registration and pari-passu sharing conditions.",
            "Trigger stock audit or unit visit for repeated late statements, unexplained stock jumps, debtor concentration or suspected fund diversion.",
            "Feed drawing-power shortfall, stale valuation and insurance gaps into renewal, EWS and credit-monitoring workflows.",
        ],
        "guardrails": [
            "Do not allow drawings against stale or unsupported stock statements beyond policy tolerance.",
            "Do not accept inflated, unaudited or inconsistent stock values without verification.",
            "Do not ignore overdue collateral valuations or lapsed insurance because account conduct is otherwise good.",
            "Do not manually override drawing power without recorded credit approval.",
            "Do not use collateral value as a substitute for repayment capacity.",
        ],
        "escalation": [
            "Excess over drawing power -> RM, Operations and Credit Monitoring for regularisation.",
            "Repeated late or suspect stock statements -> stock audit and Regional Credit Manager.",
            "Overdue valuation, lapsed insurance or charge defect -> Credit Administration hold.",
            "Suspected diversion/fraud -> Fraud Risk, Internal Audit and Compliance.",
        ],
    },
    {
        "slug": "14_corporate_covenant_monitoring_information_barriers",
        "title": "Corporate Covenant Monitoring and Information-Barrier Controls",
        "scope": "Monitoring covenant performance across corporate groups while maintaining boundaries between corporate, retail/private banking and product-specialist information.",
        "regulatory": [
            "RBI IRAC and credit monitoring norms",
            "RBI Large Exposures Framework for borrower groups",
            "RBI KYC beneficial ownership and group-relationship expectations",
            "DPDP Act 2023 and bank confidentiality obligations",
            "Internal Contoso Bank information-barrier, Chinese-wall, covenant-waiver and credit-approval policies",
        ],
        "preconditions": [
            "Covenant definitions, thresholds, test dates, cure periods, waivers and amendments are system-of-record fields.",
            "Group entity links and exposure aggregation are verified with effective dates.",
            "Retail/private-banking data relating to promoters, CFOs or directors is separated and not visible to corporate users unless purpose and entitlement permit.",
            "Treasury, transaction banking and credit teams have role-based views appropriate to their function.",
            "Covenant outputs are marked as facts, calculations, commentary or recommendations.",
        ],
        "procedure": [
            "Maintain entity-level and group-level covenant inventory with formula, source data and approval history.",
            "Compute covenant results on schedule and after material events such as acquisition, rating action, debt drawdown or financial statement update.",
            "Flag near-breaches using policy buffers and trend analysis; distinguish temporary quarter-end effects from sustained deterioration.",
            "Check whether proposed wallet-whitespace or treasury conversations could rely on restricted personal/private-banking data; block or redact where required.",
            "Prepare covenant exception notes with facts, calculation, cause, customer explanation, proposed cure and approval path.",
            "Record waivers, amendments and cures only from authorised approval workflows; update monitoring calendar after approval.",
            "Audit access to group briefs and covenant packs for cross-team data leakage.",
        ],
        "guardrails": [
            "Do not infer consent to use an officer's personal banking information because the officer represents a corporate customer.",
            "Do not share covenant stress, watchlist status or non-public corporate information with retail/private banking teams.",
            "Do not rely on email narrative to amend a covenant unless the approved waiver/amendment is recorded.",
            "Do not mask a breach by changing formulas, periods or source data after the test date.",
            "Do not let product specialists access credit-sensitive details beyond need-to-know.",
        ],
        "escalation": [
            "Information-barrier ambiguity -> Compliance before sharing or using data.",
            "Covenant breach or waiver request -> Credit Monitoring and approving authority.",
            "Group exposure or related-party concentration concern -> Group Exposure Committee.",
            "Suspected data leakage -> Information Security, DPO and Compliance.",
        ],
    },
]


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _first_text(record: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _grounding() -> dict:
    """Compact, real facts from the generated customer data to ground the SOPs."""
    if not DATASET.exists():
        return {"firm": FIRM}
    try:
        d = json.loads(DATASET.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read grounding dataset %s (%s)", DATASET, exc)
        return {"firm": FIRM}

    try:
        data = _as_dict(d)
        facts: dict[str, Any] = {"firm": FIRM}

        products_source = (
            _as_dict(data.get("products")).get("catalog")
            or data.get("product_catalog")
            or data.get("products")
            or []
        )
        products = []
        for item in _as_list(products_source):
            name = _first_text(item, ("name", "product_name", "product", "title"))
            if name:
                products.append(name)
        if products:
            facts["products"] = products[:8]

        segments_source = data.get("segments") or data.get("customer_segments") or []
        segments = []
        for item in _as_list(segments_source):
            if isinstance(item, str):
                segments.append(item)
            else:
                segment = _first_text(item, ("segment", "name", "segment_name", "sub_segment"))
                if segment:
                    segments.append(segment)
        if not segments:
            for table_name in ("customer_master", "customers", "clients"):
                for item in _as_list(data.get(table_name)):
                    segment = _first_text(item, ("segment", "sub_segment", "customer_segment"))
                    if segment:
                        segments.append(segment)
        if segments:
            facts["segments"] = sorted(set(segments))[:8]

        sample_customer = None
        for table_name in ("customer_master", "customers", "clients"):
            for item in _as_list(data.get(table_name)):
                if isinstance(item, dict):
                    sample_customer = {
                        "name": _first_text(item, ("full_name", "name", "customer_name", "entity_name")),
                        "segment": _first_text(item, ("segment", "sub_segment", "customer_segment")),
                        "type": _first_text(item, ("cust_type", "customer_type", "constitution")),
                    }
                    sample_customer = {k: v for k, v in sample_customer.items() if v}
                    if sample_customer:
                        break
            if sample_customer:
                break
        if sample_customer:
            facts["sample_customer"] = sample_customer

        rm_source = data.get("relationship_managers") or data.get("rms") or data.get("rm_master")
        rms = []
        for item in _as_list(rm_source):
            name = _first_text(item, ("full_name", "name", "rm_name"))
            role = _first_text(item, ("role", "designation", "segment"))
            if name or role:
                rms.append({"name": name, "role": role})
        if rms:
            facts["sample_relationship_managers"] = rms[:3]

        return facts
    except Exception as exc:
        log.warning("Grounding dataset shape not recognised (%s)", exc)
        return {"firm": FIRM}


# ---------------------------------------------------------------------------
# Deterministic renderer (fallback + baseline corpus)
# ---------------------------------------------------------------------------
def render_fallback(topic: dict) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "- Not specified."

    def steps(items: list[str]) -> str:
        return "\n".join(f"{idx}. {item}" for idx, item in enumerate(items, 1)) if items else "1. Not specified."

    return f"""# {topic['title']}

*{FIRM} — Standard Operating Procedure. Synthetic demo content; illustrative and not legal advice.*

## 1. Purpose & Scope

{topic['scope']} This SOP applies to {FIRM} relationship managers, branch staff, operations, credit, product, compliance and RM Assist workflows that prepare evidence, recommendations or drafts for human review.

## 2. Regulatory Basis

Key references (indicative):
{bullets(topic['regulatory'])}

## 3. Eligibility & Preconditions

{bullets(topic['preconditions'])}

## 4. Procedure

{steps(topic['procedure'])}

## 5. Guardrails & Prohibited Practices

{bullets(topic['guardrails'])}

## 6. Escalation & Approval Matrix

{bullets(topic['escalation'])}
"""


# ---------------------------------------------------------------------------
# Azure OpenAI renderer (keyless Entra, mirrors backend/app/services/llm.py)
# ---------------------------------------------------------------------------
def _aoai_client():
    ep = os.environ.get("FOUNDRY_AOAI_ENDPOINT", "").strip()
    if not ep:
        raise RuntimeError("FOUNDRY_AOAI_ENDPOINT not set")
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import OpenAI
    ep = ep.rstrip("/")
    if not ep.endswith("/openai/v1"):
        ep = ep + "/openai/v1"
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://ai.azure.com/.default")
    client = OpenAI(base_url=ep, api_key=token_provider)
    deployment = os.environ.get("FOUNDRY_CHAT_DEPLOYMENT", "gpt-5-4")
    return client, deployment


SYSTEM = (
    f"You are a compliance writer for {FIRM}, a fictional Indian bank serving retail, "
    "MSME and corporate customers. Write precise, practical Standard Operating "
    "Procedures for relationship managers, operations, credit and compliance staff. "
    "Use ONLY Indian banking context: RBI, RBI IRAC asset classification (STANDARD, "
    "SMA-0, SMA-1, SMA-2, NPA), DPDP Act 2023, Fair Practices Code, KYC/AML, "
    "grievance redressal and the RBI Integrated Ombudsman Scheme. Ground the procedure "
    "in the provided KEY POINTS and DATA; do not invent specific figures beyond them. "
    "Never imply guaranteed returns, guaranteed credit approval or coercive collections. "
    "Output MARKDOWN only, with a single '# ' H1 title and exactly these H2 sections "
    "in order: '## 1. Purpose & Scope', '## 2. Regulatory Basis', "
    "'## 3. Eligibility & Preconditions', '## 4. Procedure', "
    "'## 5. Guardrails & Prohibited Practices', "
    "'## 6. Escalation & Approval Matrix'. No code fences, no backticks."
)


def _chat_completion_create(client, deployment: str, messages: list[dict[str, str]]):
    kwargs: dict[str, Any] = {
        "model": deployment,
        "messages": messages,
        "max_completion_tokens": 1600,
    }
    try:
        return client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("max_completion_tokens", None)
        return client.chat.completions.create(**kwargs)
    except Exception as exc:
        name = exc.__class__.__name__.lower()
        text = str(exc).lower()
        unsupported_arg = (
            "badrequest" in name
            and (
                "max_completion_tokens" in text
                or "unsupported" in text
                or "unrecognized" in text
                or "unknown parameter" in text
            )
        )
        if unsupported_arg:
            kwargs.pop("max_completion_tokens", None)
            return client.chat.completions.create(**kwargs)
        raise


def render_ai(client, deployment: str, topic: dict, grounding: dict) -> str:
    user = (
        f"Write the SOP titled: {topic['title']}\n\n"
        f"KEY POINTS to expand into detailed, numbered prose:\n{json.dumps(topic, indent=2)}\n\n"
        f"GROUNDING DATA (real products/segments/customers you may cite if relevant):\n"
        f"{json.dumps(grounding, indent=2)}\n\n"
        "Expand each section into clear, actionable guidance an RM, operations officer, "
        "credit officer or compliance reviewer can follow. Keep it realistic, "
        "India-specific and suitable for Contoso Bank's RM Assist corpus."
    )
    resp = _chat_completion_create(
        client,
        deployment,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    )
    md = (resp.choices[0].message.content or "").strip()
    if not md.startswith("# "):
        md = f"# {topic['title']}\n\n" + md
    return md.rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the Contoso Bank RM-Assist SOP corpus.")
    ap.add_argument("--offline", action="store_true", help="Force the deterministic corpus (no Azure OpenAI).")
    ap.add_argument("--out", default=str(SOP_DIR), help="Output directory (default docs/sop).")
    ap.add_argument("--only", default="", help="Only (re)generate topics whose slug contains this substring.")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    grounding = _grounding()

    client = deployment = None
    use_ai = not args.offline
    if use_ai:
        try:
            client, deployment = _aoai_client()
            log.info("Azure OpenAI ready (deployment=%s). Generating AI-authored SOPs.", deployment)
        except Exception as exc:
            log.warning("Azure OpenAI unavailable (%s) - falling back to the deterministic corpus.", exc)
            use_ai = False

    only = args.only.lower().strip()
    if not only:
        for old in out_dir.glob(f"{FILE_PREFIX}*.md"):
            old.unlink()
        log.info("Cleared previous generated SOP markdown matching %s*.md in %s", FILE_PREFIX, out_dir)

    written = 0
    for topic in TOPICS:
        if only and only not in topic["slug"].lower():
            continue
        md = None
        if use_ai:
            try:
                md = render_ai(client, deployment, topic, grounding)
                log.info("AI-authored: %s (%d chars)", topic["slug"], len(md))
            except Exception as exc:
                log.warning("AI generation failed for %s (%s) - using fallback.", topic["slug"], exc)
        if md is None:
            md = render_fallback(topic)
            log.info("Deterministic: %s", topic["slug"])
        (out_dir / f"{FILE_PREFIX}{topic['slug']}.md").write_text(md, encoding="utf-8")
        written += 1

    log.info("Wrote %d SOP file(s) to %s", written, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
