"""
backend/app/services/nudge_engine.py

Fast, deterministic live-call nudge engine for the Contoso Bank MSME RM Assist
POC. Each nudge fuses live transcript intent with customer conduct, EWS, CRM,
product eligibility and SOP references. It is intentionally rule-first so the
POC demo remains reliable even if the LLM path or Voice Live latency varies.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from app.store import DataStore
from app.services.analytics import AccountConduct, EWSEngine
from app.services.crosssell import opportunities
from app.services.relationship import recent_transactions


@dataclass
class Intent:
    name: str
    patterns: list[str]
    nudge_type: str
    priority: str
    builder: str
    crm_action: str | None = None


INTENTS: list[Intent] = [
    Intent("immediate_approval",
           [r"approve.*(now|today)", r"sanction.*(now|today)", r"can you approve", r"just approve",
            r"approve the (higher )?limit", r"give me the (higher )?limit now", r"confirm.*approval"],
           "Guardrail", "High", "build_overcommit_warning", "Add note"),
    Intent("oem_order_context",
           [r"oem", r"purchase order", r"\bpo\b", r"latest .*order", r"new .*order", r"large .*order",
            r"order.*pipeline", r"contract", r"buyer.*order"],
           "Conversation path", "High", "build_order_nudge", "Create opportunity"),
    Intent("enhancement_request",
           [r"more (working )?capital", r"increase\b.{0,40}\blimit", r"higher limit", r"larger (order|loan|limit)",
            r"large .*limit", r"bigger limit", r"more funds", r"enhance", r"temporary limit", r"ad hoc limit",
            r"credit limit", r"raise\b.{0,40}\blimit", r"extend\b.{0,40}\blimit", r"limit.{0,20}increase"],
           "Credit path", "High", "build_enhancement_nudge", "Create opportunity"),
    Intent("growth_claim",
           [r"turnover.*grow", r"business.*grow", r"sales.*(up|increased|grown)", r"expanded", r"capacity",
            r"new (client|buyer)", r"production.*increase"],
           "Data point", "Medium", "build_growth_datapoint", "Add note"),
    Intent("cheque_or_delay",
           [r"cheque.*(bounce|return)", r"bounce", r"payment.*delay", r"buyer.*delay", r"receivable", r"didn'?t pay",
            r"late payment", r"collections.*stuck", r"outstanding from buyer"],
           "Risk question", "High", "build_delay_question", "Add note"),
    Intent("charges_complaint",
           [r"\bcharge", r"\bfee", r"\bpenal", r"deduct", r"debited.*(why|wrong|extra)", r"\bdispute"],
           "Service recovery", "High", "build_charges_nudge", "Update opportunity"),
    Intent("document_dispute",
           [r"already (sent|submitted|gave)", r"i submitted", r"sent.*(insurance|statement|document|stock)",
            r"uploaded.*document"],
           "Document", "High", "build_doc_dispute_nudge", "Create task"),
    Intent("attrition_risk",
           [r"another bank", r"move.*account", r"close.*account", r"switch.*bank", r"competitor", r"shifting"],
           "Retention", "High", "build_attrition_nudge", "Create task"),
    Intent("renewal_topic",
           [r"renew", r"renewal", r"review.*limit", r"facility.*due", r"limit.*review"],
           "Renewal", "Medium", "build_renewal_nudge", "Add note"),
    Intent("trade_finance",
           [r"letter of credit", r"\blc\b", r"bank guarantee", r"\bbg\b", r"supplier payment", r"import", r"export", r"forex"],
           "Cross-sell", "Medium", "build_trade_nudge", "Create opportunity"),
    Intent("digital_collections",
           [r"cash collection", r"cash deposit", r"\bupi\b", r"\bqr\b", r"\bpos\b", r"retail collection", r"digital collection"],
           "Cross-sell", "Medium", "build_collections_nudge", "Create opportunity"),
    Intent("payroll",
           [r"salary", r"payroll", r"employees", r"staff account"],
           "Cross-sell", "Low", "build_payroll_nudge", "Create opportunity"),
    # ---- service / account / document topics (data-driven, phrasing-tolerant) ----
    Intent("service_status",
           [r"open (case|ticket|complaint|issue|request)", r"my (case|ticket|complaint|issue|request)",
            r"last .* (case|ticket|complaint)", r"pending (request|complaint|issue)", r"status of my",
            r"any (open|pending)", r"raised .* (request|complaint|ticket)", r"service request", r"\bsr\b",
            r"tell me .* (case|ticket|complaint|issue)", r"what.* (open|pending)"],
           "Service status", "High", "build_service_status_nudge", "Add note"),
    Intent("account_overview",
           [r"about my account", r"my account", r"account (detail|summary|overview|status|standing)",
            r"how is my (account|relationship)", r"my (relationship|profile)", r"overview of my",
            r"tell me about (my|the) account", r"where do i stand", r"my (limit|facility) detail"],
           "Account overview", "Medium", "build_account_overview_nudge", "Add note"),
    Intent("document_status_query",
           [r"document.* (status|pending|submitted|need)", r"what .* document", r"pending document",
            r"which document", r"do i (need|have) to (submit|give|send)", r"papers", r"paperwork",
            r"stock statement", r"debtor aging", r"gst .* (pending|return|submit)", r"insurance .* (expire|pending)"],
           "Document", "High", "build_document_status_nudge", "Create task"),
    Intent("kyc_status",
           [r"\bkyc\b", r"know your customer", r"re.?kyc", r"update .* (kyc|details|address)", r"kyc .* due"],
           "Compliance", "Medium", "build_kyc_nudge", "Create task"),
    Intent("transactions_query",
           [r"transaction", r"last \d+ (transaction|payment|credit|debit)", r"recent (transaction|payment|activity)",
            r"my (payment|credit|debit)s?\b", r"statement of account", r"account activity", r"recent activity",
            r"money (came|received|paid)", r"last .* (received|paid|credited|debited)"],
           "Account activity", "Medium", "build_transactions_nudge", "Add note"),
    Intent("closed_case_reason",
           [r"why .* (reject|declined|closed|not (approved|done))", r"reason .* (reject|closed|declined|case|ticket|request)",
            r"detail.{0,30}(case|ticket|request|limit|update|dispute|charge)", r"(case|ticket|request|dispute|charge).{0,30}detail",
            r"what happened (to|with) .* (case|ticket|limit|request)",
            r"previous .* (limit|case|request)", r"(tell me|know) .* (that|the|this|my) (case|ticket|request|dispute)",
            r"(this|that) .* (case|ticket|dispute).{0,30}(closed|detail|status|reject)",
            r"(closed|that) .* (case|ticket|dispute|charge)"],
           "Case detail", "High", "build_case_detail_nudge", "Add note"),
]


class NudgeEngine:
    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.conduct = AccountConduct(store, customer_id).summary()
        self.ews = EWSEngine(store, customer_id).signals()
        self.cust = store.one("customer_master", customer_id=customer_id) or {}
        self.prof = store.one("business_profile", customer_id=customer_id) or {}
        self.facility = store.one("facilities", customer_id=customer_id) or {}
        self._opps = None

    @property
    def opps(self):
        if self._opps is None:
            self._opps = opportunities(self.s, self.cid)
        return self._opps

    # ---- conversation cues that make a given offer RELEVANT *right now* ----
    # Each offer is only surfaced when the customer says something that signals the
    # underlying need, AND the product is eligible. The "why now" links the spoken
    # cue to the offer — that's the reasoning, not a static product list.
    OFFER_CUES = {
        "PRD-CC-ENH":   [r"more (working )?capital", r"cash.?flow", r"tight", r"funds short", r"need money",
                         r"working capital", r"grow", r"expand", r"scaling", r"more orders"],
        "PRD-INVOICE":  [r"receivable", r"buyer.{0,15}(pay|delay|late)", r"payment.{0,10}(delay|late|stuck|pending)",
                         r"not paid", r"outstanding", r"debtor", r"collect.{0,10}(money|payment)", r"credit period"],
        "PRD-TRADE-LC": [r"supplier", r"import", r"raw material", r"purchase order", r"\bpo\b", r"letter of credit",
                         r"\blc\b", r"vendor payment", r"advance to supplier"],
        "PRD-FOREX":    [r"export", r"forex", r"foreign", r"dollar", r"currency", r"overseas buyer", r"usd"],
        "PRD-POS":      [r"cash sales", r"cash deposit", r"counter sales", r"upi", r"\bqr\b", r"card machine",
                         r"collection", r"retail", r"footfall", r"shop"],
        "PRD-PAYROLL":  [r"salary", r"payroll", r"employees", r"staff", r"workers", r"team"],
        "PRD-INSURE":   [r"insurance", r"insured", r"policy", r"cover", r"fire", r"theft", r"damage", r"protect"],
    }

    def _match_offers(self, text: str) -> list[dict]:
        """Surface an offer ONLY when the customer's words signal the need and the
        product is eligible. Returns at most one offer per line, with a 'why now'
        reasoning that connects the spoken cue to the product."""
        elig = {o.get("product_id"): o for o in self.opps if o.get("eligible")}
        if not elig:
            return []
        hits = []
        for pid, pats in self.OFFER_CUES.items():
            if pid not in elig:
                continue
            for p in pats:
                m = re.search(p, text)
                if m:
                    hits.append((pid, m.group(0)))
                    break
        if not hits:
            return []
        # one offer per line — pick the highest-priority eligible match
        priority = {"PRD-CC-ENH": 0, "PRD-INVOICE": 1, "PRD-TRADE-LC": 2, "PRD-FOREX": 3,
                    "PRD-POS": 4, "PRD-PAYROLL": 5, "PRD-INSURE": 6}
        hits.sort(key=lambda h: priority.get(h[0], 9))
        pid, cue = hits[0]
        return [self._build_offer_nudge(elig[pid], cue)]

    def _build_offer_nudge(self, opp: dict, cue: str) -> dict:
        pid = opp.get("product_id", "")
        product = opp.get("product", "Offer")
        why_now = self._offer_why_now(pid, cue)
        return {
            "intent": "offer_" + pid,
            "nudge_type": "Offer",
            "product_id": pid,
            "product": product,
            "priority": "Medium",
            "nudge_text": why_now,
            "reasoning": why_now,
            "recommended_next_utterance": self._offer_opening(pid, product),
            "evidence_refs": (opp.get("evidence_refs") or []) + ["CrossSellEngine", "product_catalog"],
            "what_not_to_say": "Position as an option to explore — do not promise approval, pricing or sanction on the call.",
            "crm_action_type": "Create opportunity",
            "requires_rm_approval": True,
            "trigger_snippet": cue,
            "customer_id": self.cid,
            "commercial_opportunity": opp,
        }

    def _offer_why_now(self, pid: str, cue: str) -> str:
        """The reasoning: tie the spoken cue to the product, grounded in this
        customer's data where relevant."""
        c = self.conduct
        why = {
            "PRD-CC-ENH": (f"Customer signalled a working-capital need (\u201C{cue}\u201D). With credits "
                           f"{c['credits_trend_label']} {c['credits_trend_pct']}% and utilisation {c['avg_utilization_pct']}%, "
                           f"a limit review is a genuine fit — raise it as an option, subject to appraisal."),
            "PRD-INVOICE": (f"Customer mentioned receivables / buyer payment delay (\u201C{cue}\u201D). Invoice discounting "
                            f"releases cash against confirmed receivables — directly addresses the cash-flow gap they just described."),
            "PRD-TRADE-LC": (f"Customer referenced suppliers / purchase orders (\u201C{cue}\u201D). A letter of credit or trade line "
                             f"can improve supplier terms and free up the cash-credit limit for actual working capital."),
            "PRD-FOREX": (f"Customer mentioned export / foreign-currency exposure (\u201C{cue}\u201D). A forex / hedging facility "
                          f"protects margins from currency swings — relevant to the exposure they just raised."),
            "PRD-POS": (f"Customer referenced cash / counter collections (\u201C{cue}\u201D). QR/POS digital collections reduce cash "
                        f"handling and improve reconciliation — and routing sales through the account also strengthens their banking record."),
            "PRD-PAYROLL": (f"Customer mentioned staff / payroll (\u201C{cue}\u201D). A salary-account package simplifies payroll and "
                            f"deepens the relationship across their employees."),
            "PRD-INSURE": (f"Customer touched on insurance / protection (\u201C{cue}\u201D). Their cover is a gap that is also a covenant "
                           f"requirement — closing it protects both the customer and the bank's security."),
        }
        return why.get(pid, f"Customer's mention of \u201C{cue}\u201D signals a fit for {pid}; raise it as an option to explore.")

    def _offer_opening(self, pid: str, product: str) -> str:
        lines = {
            "PRD-CC-ENH": "Given how your business is growing, a working-capital limit review could give you more headroom — shall I walk you through what that needs?",
            "PRD-INVOICE": "If buyer payments are stretching your cash, invoice discounting could release funds against your confirmed receivables — would that help?",
            "PRD-TRADE-LC": "For your supplier payments, a letter of credit or trade line could improve your terms — would you like me to explain how it works?",
            "PRD-FOREX": "With your buyer exposure, a forex or hedging facility could protect your margins from currency swings — is that something you'd consider?",
            "PRD-POS": "To cut down on cash handling and improve reconciliation, our QR/POS digital collections could help — would you like to see how?",
            "PRD-PAYROLL": "With your team size, a salary-account package could simplify payroll and add employee benefits — shall I share the details?",
            "PRD-INSURE": "Your stock and collateral insurance is a protection gap we should close — I can help you renew it; would you like me to start that?",
        }
        return lines.get(pid, f"You may be eligible for {product} — would you like me to explain how it could help your business?")
        lines = {
            "PRD-CC-ENH": "Given how your business is growing, a working-capital limit review could give you more headroom — shall I walk you through what that needs?",
            "PRD-INVOICE": "If buyer payments are stretching your cash, invoice discounting could release funds against your confirmed receivables — would that help?",
            "PRD-TRADE-LC": "For your supplier payments, a letter of credit or trade line could improve your terms — would you like me to explain how it works?",
            "PRD-FOREX": "With your buyer exposure, a forex or hedging facility could protect your margins from currency swings — is that something you'd consider?",
            "PRD-POS": "To cut down on cash handling and improve reconciliation, our QR/POS digital collections could help — would you like to see how?",
            "PRD-PAYROLL": "With your team size, a salary-account package could simplify payroll and add employee benefits — shall I share the details?",
            "PRD-INSURE": "Your stock and collateral insurance is a protection gap we should close — I can help you renew it; would you like me to start that?",
        }
        return lines.get(pid, f"You may be eligible for {product} — would you like me to explain how it could help your business?")

    def detect(self, transcript_snippet: str) -> list[dict]:
        """Return all high-value nudges triggered by the snippet, max 3.

        Guards against firing on stray words: a transcript line must look like an
        actual customer request/statement, not a one- or two-word fragment that
        merely happens to contain a keyword (e.g. the RM or customer saying the
        word "credit" in passing should NOT trigger a credit nudge)."""
        text = (transcript_snippet or "").lower().strip()
        if not text:
            return []
        words = [w for w in re.split(r"\W+", text) if w]
        # A real intent needs enough words to BE a request. A bare keyword or a
        # 2-3 word fragment ("credit", "my limit", "the charges") is not enough on
        # its own — require either a reasonable sentence length, or an explicit
        # request/question marker alongside the keyword.
        has_request_marker = bool(re.search(
            r"\b(can|could|will|would|want|need|please|how|what|why|when|where|which|"
            r"increase|raise|tell me|give me|show me|help|check|explain|status|update|"
            r"understand|asking|discuss)\b", text))
        if len(words) < 4 and not (len(words) >= 3 and has_request_marker):
            return []
        nudges: list[dict] = []
        seen_builders: set[str] = set()
        for intent in INTENTS:
            if intent.builder in seen_builders:
                continue
            if any(re.search(p, text) for p in intent.patterns):
                builder = getattr(self, intent.builder)
                nudge = builder(transcript_snippet)
                if nudge:
                    nudge.update({
                        "intent": intent.name,
                        "nudge_type": intent.nudge_type,
                        "priority": intent.priority,
                        "crm_action_type": intent.crm_action,
                        "requires_rm_approval": True,
                        "trigger_snippet": transcript_snippet,
                        "customer_id": self.cid,
                    })
                    nudges.append(nudge)
                    seen_builders.add(intent.builder)
                    if len(nudges) >= 3:
                        break
        # ---- Conversation-triggered cross-sell OFFERS ----
        # An offer surfaces only when the customer's words signal the underlying need
        # AND the product is eligible. Each carries a "why now" reasoning tying the
        # spoken cue to the offer — so it reads as judgement, not a product list.
        for offer in self._match_offers(text):
            offer["requires_rm_approval"] = True
            offer["trigger_snippet"] = transcript_snippet
            nudges.append(offer)
        # ---- Data-driven fallback (Option B) ----
        # If no intent matched but the customer said something substantive, surface
        # the single most material fact about THIS customer so the RM always gets a
        # relevant nudge — not just on the few keyworded topics. Priority order:
        # open service request > expired/blocking required doc > critical EWS >
        # pending required docs > renewal/review due > top cross-sell opportunity.
        if not nudges and self._is_substantive(text):
            fb = self.build_data_fallback_nudge(transcript_snippet)
            if fb:
                fb.update({
                    "intent": fb.pop("_intent", "context_surface"),
                    "nudge_type": fb.pop("_nudge_type", "Context"),
                    "priority": fb.pop("_priority", "Medium"),
                    "crm_action_type": fb.pop("_crm_action", "Add note"),
                    "requires_rm_approval": True,
                    "trigger_snippet": transcript_snippet,
                    "customer_id": self.cid,
                })
                nudges.append(fb)
        return nudges

    @staticmethod
    def _is_substantive(text: str) -> bool:
        """A customer line worth surfacing context for: a real question or statement,
        not a greeting / acknowledgement / filler."""
        t = (text or "").strip().lower().rstrip("?.!, ")
        if len(t) < 6:
            return False
        # exact filler phrases
        fillers = {"hello", "hi", "hey", "ok", "okay", "yes", "no", "yeah", "yep", "nope",
                   "hmm", "thanks", "thank you", "bye", "goodbye", "hello hello", "hi there",
                   "hello there", "hey there", "good morning", "good afternoon", "good evening",
                   "i am good", "i'm good", "fine", "i am fine", "i'm fine", "how are you",
                   "sure", "alright", "right", "got it", "understood", "noted",
                   "yes please", "no thanks", "thanks a lot", "thank you so much", "ok thanks",
                   "okay sure", "yes sure", "no problem", "of course", "absolutely", "perfect",
                   "great", "cool", "fine thanks", "all good", "nothing else", "that's all",
                   "thats all", "that is all", "carry on", "go ahead", "please go ahead", "yes go ahead"}
        if t in fillers:
            return False
        # greeting prefixes ("hello there sir", "hi how are you", ...) with no real content
        greet_prefixes = ("hello", "hi ", "hi,", "hey", "good morning", "good afternoon",
                          "good evening", "how are you")
        if t.startswith(greet_prefixes) or t in ("hi", "hey"):
            # remove greeting words, how-are-you, and honorifics; if little remains, filler
            stripped = t
            stripped = re.sub(r"\b(hello|hi|hey|good morning|good afternoon|good evening)\b", "", stripped)
            stripped = re.sub(r"\bhow are you\b", "", stripped)
            stripped = re.sub(r"\b(sir|madam|ma'?am|maam|sirs|there)\b", "", stripped)
            stripped = re.sub(r"\bi am (good|fine|well|ok)\b|\bi'?m (good|fine|well|ok)\b", "", stripped)
            stripped = re.sub(r"[^\w\s]", " ", stripped)
            words = [w for w in stripped.split() if w]
            if len(words) < 2:
                return False
        words = [w for w in re.split(r"\W+", t) if w]
        return len(words) >= 2

    def _eligible(self, product_id: str | None = None) -> list[dict]:
        out = [o for o in self.opps if o.get("eligible")]
        if product_id:
            out = [o for o in out if o.get("product_id") == product_id]
        return out

    def _blocked(self, product_id: str | None = None) -> list[dict]:
        out = [o for o in self.opps if not o.get("eligible")]
        if product_id:
            out = [o for o in out if o.get("product_id") == product_id]
        return out

    def _base(self, text: str, refs: list[str], next_utterance: str, *, reasoning: str | None = None, **extra) -> dict:
        return {
            "nudge_text": text,
            "reasoning": reasoning or text,
            "evidence_refs": refs,
            "recommended_next_utterance": next_utterance,
            "customer_snapshot": {
                "display_name": self.cust.get("display_name"),
                "credits_trend": f"{self.conduct['credits_trend_label']} {self.conduct['credits_trend_pct']}%",
                "avg_utilization_pct": self.conduct["avg_utilization_pct"],
                "peak_utilization_pct": self.conduct["peak_utilization_pct"],
                "cheque_returns": self.conduct["cheque_return_count"],
            },
            **extra,
        }

    # ---------- builders ----------
    def build_order_nudge(self, snippet: str) -> dict:
        recent = recent_transactions(self.s, self.cid, 20)
        oem_txns = [t for t in recent if "oem" in (t.get("counterparty_name") or "").lower() or "oem" in (t.get("description") or "").lower()]
        last = oem_txns[0] if oem_txns else (recent[0] if recent else {})
        enh_ok = bool(self._eligible("PRD-CC-ENH"))
        top_opp = self._eligible()[0] if self._eligible() else None
        if enh_ok:
            text = (f"OEM/order topic detected. Data supports a growth-led review: credits {self.conduct['credits_trend_label']} "
                    f"{self.conduct['credits_trend_pct']}%, avg utilization {self.conduct['avg_utilization_pct']}%, "
                    f"last notable receipt {last.get('counterparty_name','-')} on {last.get('txn_date','-')}. Ask for PO value, delivery schedule and debtor terms before discussing limit size.")
        else:
            blocker = "; ".join(self._blocked("PRD-CC-ENH")[0].get("blocked_by", [])) if self._blocked("PRD-CC-ENH") else "policy/data blockers"
            text = (f"Order topic detected, but do not position enhancement yet. Blocker: {blocker}. Use the order as fact-finding and collect proof before any credit discussion.")
        return self._base(
            text,
            ["transactions:recent_oem_receipts", "business_profile:growth_notes", "SOP 03_limit_enhancement_eligibility"],
            "Congratulations on the order. To initiate the review, may I confirm the PO amount, buyer name, delivery schedule and expected payment terms?",
            commercial_opportunity=top_opp,
            crm_payload={"type": "opportunity", "opportunity_type": "Order-led working capital review", "stage": "Needs PO validation"},
        )

    def build_enhancement_nudge(self, snippet: str) -> dict:
        eligible = self._eligible("PRD-CC-ENH")
        critical = [s for s in self.ews if s["severity"] == "Critical"]
        if eligible:
            txt = (f"Enhancement review is supportable, not approved: credits {self.conduct['credits_trend_label']} "
                   f"{self.conduct['credits_trend_pct']}%, utilization avg {self.conduct['avg_utilization_pct']}% "
                   f"and peak {self.conduct['peak_utilization_pct']}%. Ask for latest GST, stock statement, debtor aging and PO copy. "
                   f"Mention buyer concentration ~{self.conduct['top_counterparty_concentration_pct']}% as a credit caveat.")
            nxt = "I can initiate an enhancement review after validating the PO, GST, stock statement and debtor aging; credit will make the final assessment."
            refs = ["analytics:credits_trend", "analytics:utilization", "SOP 03_limit_enhancement_eligibility"]
            crm_payload = {"type": "opportunity", "opportunity_type": "Working Capital Limit Enhancement", "stage": "RM discussion"}
        else:
            blk = critical[0]["signal_type"] if critical else ", ".join(self._blocked("PRD-CC-ENH")[0].get("blocked_by", ["conduct concerns"])) if self._blocked("PRD-CC-ENH") else "conduct concerns"
            txt = (f"Do NOT signal enhancement. Blocker: {blk}. If utilization is high while credits are "
                   f"{self.conduct['credits_trend_label']}, treat it as a risk conversation before growth.")
            nxt = "Let us first clear the document/conduct blockers. After that I can place the case for review."
            refs = ["ews_engine", "CrossSellEngine:blocked_by", "SOP 03_limit_enhancement_eligibility"]
            crm_payload = {"type": "task", "title": "Clear enhancement blockers before credit review", "priority": "High"}
        return self._base(txt, refs, nxt, crm_payload=crm_payload)

    def build_overcommit_warning(self, snippet: str) -> dict:
        return self._base(
            "Do not commit approval on the call. Enhancement/renewal requires credit appraisal, customer documents and RM/credit approval. Use non-committal language and create a CRM note.",
            ["SOP 09_escalation_and_human_handoff", "compliance:no_credit_decision"],
            "I cannot confirm approval on this call, but I will initiate the review and revert after credit assessment.",
            crm_payload={"type": "note", "subject": "Credit approval guardrail used"},
        )

    def build_growth_datapoint(self, snippet: str) -> dict:
        gst = self.s.where("gst", customer_id=self.cid)
        trend = gst[-1]["trend_tag"] if gst else "n/a"
        return self._base(
            f"Validate the growth claim: bank credits are {self.conduct['credits_trend_label']} {self.conduct['credits_trend_pct']}%, GST trend is '{trend}', top buyer concentration ~{self.conduct['top_counterparty_concentration_pct']}%. Acknowledge growth but ask for order proof.",
            ["gst_returns_monthly", "analytics:concentration", "transactions"],
            "That growth is encouraging. Can you share the order/customer breakup and expected payment cycle so I capture the right basis for the review?",
            crm_payload={"type": "note", "subject": "Growth claim validation"},
        )

    def build_delay_question(self, snippet: str) -> dict:
        crs = self.s.where("cheque_returns", customer_id=self.cid)
        opp = self._eligible("PRD-INVOICE")[:1]
        text = (f"Receivable/cheque-delay topic detected. On record: {len(crs)} cheque return(s), credits trend "
                f"{self.conduct['credits_trend_label']} {self.conduct['credits_trend_pct']}%. Ask which buyer delayed, amount pending and expected collection date. Do not allege wrongdoing.")
        if opp:
            text += " If buyer quality is acceptable, invoice discounting may be a relevant follow-up after risk clarification."
        return self._base(
            text,
            ["cheque_returns", "debtor_aging", "SOP 04_early_warning_signals", "PRD-INVOICE"],
            "Which buyer payment is delayed, what amount is outstanding and by when do you expect collection?",
            commercial_opportunity=opp[0] if opp else None,
            crm_payload={"type": "note", "subject": "Receivable delay discussed"},
        )

    def build_charges_nudge(self, snippet: str) -> dict:
        open_sr = [s for s in self.s.where("service_requests", customer_id=self.cid) if s["status"] == "Open"]
        ref = open_sr[0]["ticket_id"] if open_sr else "no open ticket"
        return self._base(
            f"Service recovery first. Charges concern detected; open ticket: {ref}. Acknowledge, confirm the charge/date and avoid cross-sell until ownership is clear.",
            ["service_requests", "SOP 09_escalation_and_human_handoff"],
            "I understand the concern. Let me capture the exact charge/date and have it reviewed before we discuss anything else.",
            crm_payload={"type": "task", "title": "Review customer charges complaint", "priority": "High"},
        )

    def build_doc_dispute_nudge(self, snippet: str) -> dict:
        docs = self.s.where("documents", customer_id=self.cid)
        problematic = [d for d in docs if d.get("status") in ("Pending", "Expired", "Overdue")]
        status = ", ".join(f"{d['document_type']}={d['status']}" for d in problematic[:3]) or "all documents appear current"
        return self._base(
            f"Customer says document was submitted. CRM document status: {status}. Do not mark as received on voice statement alone; ask for channel/date and create verification task.",
            ["document_status", "SOP 05_document_checklist_by_constitution"],
            "Can you confirm the date and channel where you submitted it? I will create a verification task and update the CRM after validation.",
            crm_payload={"type": "task", "title": "Verify disputed document submission", "priority": "High"},
        )

    def build_attrition_nudge(self, snippet: str) -> dict:
        rvs = self.cust.get("relationship_value_score", "?")
        return self._base(
            f"Attrition risk detected. Relationship value score {rvs}. Acknowledge the issue, ask what the competing bank offered, and create a same-day retention task. Do not over-promise rate/limit concessions.",
            ["relationship_value_score", "service_requests", "SOP 09_escalation_and_human_handoff"],
            "I do not want you to move without us understanding the issue. What exactly has the other bank offered and what timeline are you considering?",
            crm_payload={"type": "task", "title": "Same-day MSME retention follow-up", "priority": "High"},
        )

    def build_renewal_nudge(self, snippet: str) -> dict:
        pending = [d["document_type"] for d in self.s.where("documents", customer_id=self.cid) if d["status"] in ("Pending", "Expired", "Overdue") and d["required_flag"] == "Y"]
        return self._base(
            f"Renewal topic detected. Review due {self.facility.get('review_due_date','-')}. Confirm pending documents: {', '.join(sorted(set(pending))) or 'none'}. Summarize conduct before discussing terms.",
            ["loan_facilities", "document_status", "SOP 02_working_capital_renewal"],
            "Your review is due on this date. Let us confirm the pending document list and then I will prepare the renewal note.",
            crm_payload={"type": "task", "title": "Prepare renewal pack", "priority": "Medium"},
        )

    def build_trade_nudge(self, snippet: str) -> dict:
        opp = self._eligible("PRD-TRADE-LC")[:1] or self._eligible("PRD-FOREX")[:1]
        if opp:
            txt = f"Trade/supplier topic detected. Eligible cross-sell: {opp[0]['product']}. Rationale: {opp[0]['rationale']} Ask for supplier terms and invoice/PO cycle."
            next_u = "Would an LC/BG or structured supplier payment line help with this supplier cycle? I can map the documents needed."
        else:
            blocked = self._blocked("PRD-TRADE-LC")[:1]
            txt = f"Trade topic detected, but check blockers before pitching. {blocked[0]['blocked_by'] if blocked else 'No eligible trade product matched from current data.'}"
            next_u = "Let me first validate eligibility and current risk flags before I suggest a trade facility."
        return self._base(txt, ["CrossSellEngine", "product_catalog", "SOP 06_collateral_and_insurance"], next_u,
                          commercial_opportunity=opp[0] if opp else None,
                          crm_payload={"type": "opportunity", "opportunity_type": "Trade finance discussion", "stage": "Call nudge"})

    def build_collections_nudge(self, snippet: str) -> dict:
        opp = self._eligible("PRD-POS")[:1]
        text = "Digital collections angle detected. "
        text += (f"Eligible: {opp[0]['product']} — {opp[0]['rationale']}" if opp else "Pitch only as service improvement if customer has cash/retail collection need.")
        return self._base(text, ["CrossSellEngine", "analytics:cash_intensity", "SOP 07_cash_intensity_and_related_party"],
                          "Would QR/POS collections reduce cash handling and improve reconciliation for your retail collections?",
                          commercial_opportunity=opp[0] if opp else None,
                          crm_payload={"type": "opportunity", "opportunity_type": "Digital collections", "stage": "Needs validation"})

    def build_payroll_nudge(self, snippet: str) -> dict:
        opp = self._eligible("PRD-PAYROLL")[:1]
        return self._base(
            (f"Payroll topic detected. Eligible opportunity: {opp[0]['product']} — {opp[0]['rationale']}" if opp else "Payroll topic detected; check employee count and payroll transactions before pitching salary accounts."),
            ["CrossSellEngine", "transactions:payroll", "business_profile:employee_count"],
            "How many employees are currently on payroll, and would a salary-account package help your HR/admin team?",
            commercial_opportunity=opp[0] if opp else None,
            crm_payload={"type": "opportunity", "opportunity_type": "Payroll and salary account package", "stage": "Call nudge"},
        )

    # ---------- data-driven builders ----------
    def _service_requests(self) -> list[dict]:
        return self.s.where("service_requests", customer_id=self.cid)

    def build_service_status_nudge(self, snippet: str) -> dict:
        srs = self._service_requests()
        open_sr = [s for s in srs if (s.get("status") or "").lower() == "open"]
        if open_sr:
            top = sorted(open_sr, key=lambda s: 0 if (s.get("priority") or "").lower() == "high" else 1)[0]
            lines = "; ".join(f"{s['ticket_id']} {s.get('category','?')} ({s.get('priority','?')}, {s.get('customer_sentiment','?')})" for s in open_sr[:3])
            txt = (f"Customer is asking about cases/tickets. OPEN service request(s): {lines}. "
                   f"Address the open item first — {top.get('category','issue')} raised {top.get('created_date','-')}, "
                   f"SLA due {top.get('sla_due_date','-')}. Do not pivot to sales until this is acknowledged.")
            nxt = (f"I can see your open {top.get('category','request')} (ref {top.get('ticket_id','-')}). "
                   f"Let me give you the current status and the next step on that before anything else.")
            crm = {"type": "note", "subject": f"Discussed open SR {top.get('ticket_id','-')}", "priority": "High"}
        else:
            closed_recent = sorted(srs, key=lambda s: s.get("closed_date") or "", reverse=True)[:3]
            lines = "; ".join(f"{s['ticket_id']} {s.get('category','?')} ({s.get('status','?')})" for s in closed_recent) or "no service requests on record"
            txt = (f"Customer is asking about cases/tickets. No OPEN items. Recent history: {lines}. "
                   f"Confirm there is nothing pending and offer to log a new request if needed.")
            nxt = "I don't see any open cases right now. Your recent requests are all resolved — is there something new you'd like me to raise?"
            crm = {"type": "note", "subject": "Confirmed no open service requests"}
        return self._base(txt, ["service_requests", "SOP 09_escalation_and_human_handoff"], nxt, crm_payload=crm)

    def build_account_overview_nudge(self, snippet: str) -> dict:
        rvs = self.cust.get("relationship_value_score", "?")
        seg = self.cust.get("segment", "-")
        risk = self.cust.get("risk_category", "-")
        review = self.facility.get("review_due_date", "-")
        txt = (f"Customer wants an account overview. Snapshot: {self.cust.get('display_name','-')}, segment {seg}, "
               f"risk {risk}, relationship value {rvs}. Credits {self.conduct['credits_trend_label']} "
               f"{self.conduct['credits_trend_pct']}%, avg utilization {self.conduct['avg_utilization_pct']}% "
               f"(peak {self.conduct['peak_utilization_pct']}%), {self.conduct['cheque_return_count']} cheque return(s). "
               f"Facility review due {review}. Give a factual standing summary; do not quote pricing or commit changes.")
        nxt = ("Here's where your account stands today — facility utilization, recent conduct and the next review date. "
               "Would you like me to walk through any specific part?")
        return self._base(txt, ["customer_master", "analytics:utilization", "analytics:credits_trend", "loan_facilities"], nxt,
                          crm_payload={"type": "note", "subject": "Account overview shared on call"})

    def build_document_status_nudge(self, snippet: str) -> dict:
        docs = self.s.where("documents", customer_id=self.cid)
        blocking = [d for d in docs if (d.get("blocking_flag") == "Y") and d.get("status") in ("Pending", "Expired", "Overdue")]
        pending_req = [d for d in docs if d.get("required_flag") == "Y" and d.get("status") in ("Pending", "Expired", "Overdue")]
        focus = blocking or pending_req
        listed = ", ".join(f"{d['document_type']}={d['status']}" for d in focus[:4]) or "all required documents are current"
        flag = " (BLOCKING)" if blocking else ""
        txt = (f"Document topic. Outstanding required docs{flag}: {listed}. "
               f"Ask the customer to submit these; they gate any limit/renewal action.")
        nxt = (f"To move things forward we need: {', '.join(sorted({d['document_type'] for d in focus})) or 'nothing pending'}. "
               f"Can you share these so I can update your file?")
        return self._base(txt, ["document_status", "SOP 05_document_checklist_by_constitution"], nxt,
                          crm_payload={"type": "task", "title": "Collect outstanding documents", "priority": "High" if blocking else "Medium"})

    def build_kyc_nudge(self, snippet: str) -> dict:
        status = self.cust.get("kyc_status", "-")
        due = self.cust.get("next_kyc_due_date", "-")
        txt = (f"KYC topic. KYC status: {status}, next due {due}. "
               f"{'KYC is due — guide the customer on re-KYC documents and timeline.' if status.lower()=='due' else 'KYC is valid; confirm no update is needed.'}")
        nxt = ("Let me check your KYC — " + ("it's due for refresh, so I'll tell you exactly what's needed and by when." if status.lower()=="due"
               else "it's currently valid, so no action is needed right now."))
        return self._base(txt, ["customer_master:kyc_status", "SOP 05_document_checklist_by_constitution"], nxt,
                          crm_payload={"type": "task", "title": "KYC follow-up", "priority": "Medium" if status.lower()=="due" else "Low"})

    def build_transactions_nudge(self, snippet: str) -> dict:
        txns = recent_transactions(self.s, self.cid, 10)
        if not txns:
            reasoning = "Customer asked about recent transactions, but no transactions are on record for this account."
            nxt = "I'm not seeing recent transactions on this account right now — let me check with operations and call you back with the statement."
            return self._base(reasoning, ["transactions"], nxt, reasoning=reasoning,
                              crm_payload={"type": "note", "subject": "Transaction query - none on record"})
        # Build a short, speakable list of the most recent few
        def _one(t):
            amt = t.get("amount_inr") or 0
            try: amt_str = f"₹{float(amt):,.0f}"
            except Exception: amt_str = str(amt)
            drcr = "credit" if str(t.get("dr_cr","")).upper().startswith("C") else "debit"
            cp = t.get("counterparty_name") or t.get("description") or t.get("category_lvl1") or "—"
            return f"{t.get('txn_date','-')}: {amt_str} {drcr} ({cp})"
        top = txns[:5]
        listed = "; ".join(_one(t) for t in top)
        flagged = [t for t in txns if t.get("is_return") in ("Y", "true", "True") or t.get("anomaly_tag")]
        reasoning = (f"Customer asked about recent transactions. Last {len(top)} on record: {listed}."
                     + (f" Note {len(flagged)} flagged item(s) (return/anomaly) — be ready to explain if asked." if flagged else ""))
        # Speakable narrative — the RM can read the most recent couple aloud
        say_list = "; ".join(_one(t) for t in top[:3])
        nxt = (f"Sure — your most recent transactions are: {say_list}. "
               f"Would you like me to email the full statement for the last 10?")
        return self._base(reasoning, ["transactions", "analytics:cash_intensity"], nxt, reasoning=reasoning,
                          crm_payload={"type": "note", "subject": "Shared recent transactions on call"})

    def build_case_detail_nudge(self, snippet: str) -> dict:
        """Customer asks why a case/limit was rejected/closed, or wants detail on a
        specific past ticket. Look up the most relevant service request and explain it."""
        srs = self.s.where("service_requests", customer_id=self.cid)
        low = (snippet or "").lower()
        # Try to match the topic the customer referenced (limit, charges, cheque, etc.)
        topic_map = [("limit", "limit"), ("charge", "charge"), ("cheque", "cheque"),
                     ("net banking", "net banking"), ("statement", "statement"), ("kyc", "kyc")]
        focus = None
        for kw, _ in topic_map:
            if kw in low:
                focus = next((s for s in srs if kw in (s.get("category","").lower() + " " + s.get("description","").lower())), None)
                if focus:
                    break
        # else most recent closed case
        if not focus:
            closed = [s for s in srs if (s.get("status") or "").lower() == "closed"]
            focus = sorted(closed, key=lambda s: s.get("closed_date") or "", reverse=True)[0] if closed else None
        if not focus:
            reasoning = "Customer asked for the reason/detail on a past case, but no matching service request is on record."
            nxt = "Let me pull that case up properly and get back to you with the exact reason — I don't want to give you partial information."
            return self._base(reasoning, ["service_requests"], nxt, reasoning=reasoning,
                              crm_payload={"type": "note", "subject": "Case detail requested - not found"})
        cat = focus.get("category", "request")
        status = focus.get("status", "-")
        remark = focus.get("remarks") or "no additional remark recorded"
        created = focus.get("created_date", "-")
        closed_d = focus.get("closed_date", "-")
        reasoning = (f"Customer is asking about a specific case: {focus.get('ticket_id','-')} — {cat}, status {status} "
                     f"(raised {created}, closed {closed_d}). Recorded outcome/remark: '{remark}'. "
                     f"Explain factually; do not invent a rejection reason that isn't recorded.")
        if status.lower() == "closed":
            nxt = (f"Your {cat} request (ref {focus.get('ticket_id','-')}) was raised on {created} and closed on {closed_d}. "
                   f"The recorded outcome was: {remark}. Would you like me to reopen it or raise a fresh request?")
        else:
            nxt = (f"Your {cat} request (ref {focus.get('ticket_id','-')}) is currently {status}. "
                   f"Here's where it stands: {remark}. Shall I push for an update on your behalf?")
        return self._base(reasoning, ["service_requests", "SOP 09_escalation_and_human_handoff"], nxt, reasoning=reasoning,
                          crm_payload={"type": "note", "subject": f"Explained case {focus.get('ticket_id','-')}"})

    def build_data_fallback_nudge(self, snippet: str) -> dict | None:
        """Most material fact about THIS customer, surfaced when no intent matched.
        Returns the nudge with private _intent/_nudge_type/_priority/_crm_action keys
        that detect() promotes to the public fields."""
        # 1) open service request
        open_sr = [s for s in self._service_requests() if (s.get("status") or "").lower() == "open"]
        if open_sr:
            top = sorted(open_sr, key=lambda s: 0 if (s.get("priority") or "").lower() == "high" else 1)[0]
            n = self._base(
                f"Context: customer has an OPEN {top.get('category','request')} (ref {top.get('ticket_id','-')}, "
                f"{top.get('priority','?')} priority, sentiment {top.get('customer_sentiment','?')}) raised {top.get('created_date','-')}. "
                f"Proactively address it during this call.",
                ["service_requests"],
                f"While I have you — I see an open {top.get('category','request')} on your account. Let me update you on that.",
                crm_payload={"type": "note", "subject": f"Proactively surfaced open SR {top.get('ticket_id','-')}"})
            n.update({"_intent": "context_open_sr", "_nudge_type": "Service status", "_priority": "High", "_crm_action": "Add note"})
            return n
        # 2) expired/blocking required document
        docs = self.s.where("documents", customer_id=self.cid)
        blocking = [d for d in docs if d.get("blocking_flag") == "Y" and d.get("status") in ("Expired", "Overdue", "Pending")]
        if blocking:
            listed = ", ".join(f"{d['document_type']}={d['status']}" for d in blocking[:3])
            n = self._base(
                f"Context: BLOCKING document issue — {listed}. These gate any credit/renewal action; raise proactively.",
                ["document_status", "SOP 05_document_checklist_by_constitution"],
                f"One thing I should flag — we need your {blocking[0]['document_type']} updated; it's currently {blocking[0]['status'].lower()}.",
                crm_payload={"type": "task", "title": "Collect blocking documents", "priority": "High"})
            n.update({"_intent": "context_blocking_doc", "_nudge_type": "Document", "_priority": "High", "_crm_action": "Create task"})
            return n
        # 3) critical EWS signal
        critical = [s for s in self.ews if s.get("severity") == "Critical"] or [s for s in self.ews if s.get("severity") == "High"]
        if critical:
            sig = critical[0]
            n = self._base(
                f"Context: early-warning signal active — {sig.get('signal_type','?')} ({sig.get('severity','?')}). {sig.get('detail','')}".strip(),
                ["ews_engine"] + list(sig.get("evidence_refs", []) or []),
                "Before we go further, I'd like to understand a recent pattern on your account — may I ask a couple of questions?",
                crm_payload={"type": "note", "subject": f"EWS surfaced: {sig.get('signal_type','?')}"})
            n.update({"_intent": "context_ews", "_nudge_type": "Risk question", "_priority": "High", "_crm_action": "Add note"})
            return n
        # 4) renewal / review due
        review = self.facility.get("review_due_date")
        if review:
            pending = [d["document_type"] for d in docs if d.get("required_flag") == "Y" and d.get("status") in ("Pending", "Expired", "Overdue")]
            n = self._base(
                f"Context: facility review due {review}. Pending required docs: {', '.join(sorted(set(pending))) or 'none'}. "
                f"Good moment to set expectations on the renewal.",
                ["loan_facilities", "document_status", "SOP 02_working_capital_renewal"],
                f"Your facility review is due on {review}. Shall I walk you through what we'll need for a smooth renewal?",
                crm_payload={"type": "task", "title": "Prepare renewal pack", "priority": "Medium"})
            n.update({"_intent": "context_renewal", "_nudge_type": "Renewal", "_priority": "Medium", "_crm_action": "Add note"})
            return n
        # 5) top eligible cross-sell opportunity
        elig = self._eligible()
        if elig:
            opp = elig[0]
            n = self._base(
                f"Context: top eligible opportunity — {opp.get('product','?')}. {opp.get('rationale','')}".strip(),
                ["CrossSellEngine", "product_catalog"],
                f"When the time is right, {opp.get('product','a tailored facility')} could fit your needs — I can explain it whenever you'd like.",
                commercial_opportunity=opp,
                crm_payload={"type": "opportunity", "opportunity_type": opp.get("product", "Cross-sell"), "stage": "Call nudge"})
            n.update({"_intent": "context_opportunity", "_nudge_type": "Cross-sell", "_priority": "Low", "_crm_action": "Create opportunity"})
            return n
        return None
