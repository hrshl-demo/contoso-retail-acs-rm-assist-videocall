# Retail Client Pulse, Pre-Meeting Brief and Post-Interaction Capture

*Contoso Bank — Standard Operating Procedure. Synthetic demo content; illustrative and not legal advice.*

## 1. Purpose & Scope

Generating a cited one-page relationship brief before a Priority retail client interaction, then drafting the post-interaction note, commitments and follow-ups for RM approval. This SOP applies to Contoso Bank relationship managers, branch staff, operations, credit, product, compliance and RM Assist workflows that prepare evidence, recommendations or drafts for human review.

## 2. Regulatory Basis

Key references (indicative):
- RBI Master Direction - Know Your Customer (KYC) Direction, 2016, as amended
- Digital Personal Data Protection Act, 2023 (DPDP) - purpose limitation, consent and data-principal rights
- RBI customer service and Fair Practices expectations for transparent, non-misleading communication
- RBI Integrated Ombudsman Scheme, 2021 - complaint visibility before commercial outreach
- Internal Contoso Bank entitlement, CRM audit-trail and call-recording policies

## 3. Eligibility & Preconditions

- Requesting RM has an active portfolio mapping for the customer or household on the interaction date.
- Customer identity is resolved to one primary CUST_ID and permitted family-linked IDs only where consent and entitlement exist.
- KYC, risk profile, complaint, account, loan, card, investment and interaction data sources have refreshed successfully.
- Live call capture is used only after clear consent to recording/transcription; otherwise the RM may dictate a summary after the meeting.
- RM Assist is internal-only: it drafts, cites and structures information; it never contacts the customer directly.

## 4. Procedure

1. Trigger the brief from a calendar event, CRM task, inbound call routing or explicit RM request such as 'brief me on Rajesh Iyer'.
2. Resolve customer identity and run an entitlement check against rm_portfolio_map; refuse and log any lookup outside the RM's book.
3. Set the delta window from the last recorded customer interaction and retrieve only material changes since that date.
4. Retrieve structured positions, service tickets, complaints, delinquency flags, KYC status, risk profile, investment holdings, open commitments and relevant policy snippets through separately-permissioned channels.
5. Run deterministic blocking checks before any model synthesis: open or recently breached complaints, KYC due/overdue, expired risk profile, delinquency, suspected fraud and pending commitments.
6. Generate a concise brief with relationship snapshot, what changed, open issues, ranked talking points, suggested opening line, compliance flags and source references.
7. Require every number, date, product and assertion in the brief to map back to a retrieved source; drop unsupported statements instead of inferring.
8. After the meeting, draft a CRM note, commitments, owners, dates, sentiment and next actions; present them to the RM for correction.
9. Write to CRM only after explicit RM approval; record edit distance and dismissal reasons for monitored feedback, not self-modifying production behaviour.

## 5. Guardrails & Prohibited Practices

- Never allow free-form customer search or cross-book browsing; entitlement failure is a hard stop.
- Do not place a product pitch above an unresolved service failure, reopened complaint or Banking Ombudsman-sensitive matter.
- Do not generate investment, insurance or credit talking points when KYC, risk profile or mandatory suitability evidence is stale.
- Mask PAN, Aadhaar, account numbers and contact details in the brief unless the RM has a documented need-to-know.
- Do not write notes, promises or suitability rationale into CRM without RM approval.
- Do not fabricate sources, figures, dates or relationship history; state 'not found' where evidence is absent.

## 6. Escalation & Approval Matrix

- Entitlement mismatch or attempted unauthorised access -> Compliance and Information Security review.
- Open, breached or reopened complaint -> service owner and Principal Nodal Officer path before commercial follow-up.
- Suitability conflict or missing evidence -> Branch Manager / Investment Product Compliance approval before proceeding.
- Ungrounded or contradictory source data -> manual RM note; raise a data-quality ticket to operations.
