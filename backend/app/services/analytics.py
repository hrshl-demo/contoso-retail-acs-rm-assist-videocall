"""
backend/app/services/analytics.py

The deterministic, explainable analytical core of RM Assist. No LLM here — these
are the transparent calculations the assistant cites as evidence. (The LLM layer,
added later, narrates these numbers; it never invents them.)

Mirrors the loans POC philosophy: a clean rule-based engine whose outputs are
auditable. Every method returns both the computed numbers AND the evidence refs
that justify them, so the dashboard/memo/chat can show "why".
"""
from __future__ import annotations
from collections import defaultdict
from statistics import mean

from app.store import DataStore


class AccountConduct:
    """12-month account-conduct metrics for one customer (blueprint UC3)."""

    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.txns = store.where("transactions", customer_id=customer_id)
        self.util = store.where("utilization", customer_id=customer_id)
        self.gst = store.where("gst", customer_id=customer_id)
        self.cheque_returns = store.where("cheque_returns", customer_id=customer_id)

    def _monthly_credits(self) -> dict[str, float]:
        m = defaultdict(float)
        for t in self.txns:
            if t["dr_cr"] == "CR":
                m[t["txn_date"][:7]] += float(t["amount_inr"])
        return dict(sorted(m.items()))

    def _monthly_debits(self) -> dict[str, float]:
        m = defaultdict(float)
        for t in self.txns:
            if t["dr_cr"] == "DR" and t["is_return"] != "Y":
                m[t["txn_date"][:7]] += float(t["amount_inr"])
        return dict(sorted(m.items()))

    def summary(self) -> dict:
        credits = self._monthly_credits()
        debits = self._monthly_debits()
        months = list(credits.keys())
        avg_credit = mean(credits.values()) if credits else 0.0

        # credits trend: first quarter vs last quarter
        vals = list(credits.values())
        first_q = mean(vals[:3]) if len(vals) >= 3 else (vals[0] if vals else 0)
        last_q = mean(vals[-3:]) if len(vals) >= 3 else (vals[-1] if vals else 0)
        trend_pct = ((last_q - first_q) / first_q * 100) if first_q else 0.0

        # cash intensity
        total_credit = sum(credits.values()) or 1.0
        cash_credit = sum(float(t["amount_inr"]) for t in self.txns
                          if t["dr_cr"] == "CR" and t["is_cash"] == "Y")
        cash_pct = cash_credit / total_credit * 100

        # utilization
        util_vals = [float(u["utilization_pct"]) for u in self.util]
        avg_util = mean(util_vals) if util_vals else 0.0
        peak_util = max(util_vals) if util_vals else 0.0
        over_limit_days = sum(1 for u in self.util if u["over_limit_flag"] == "Y")
        days_over_85 = sum(1 for u in self.util if float(u["utilization_pct"]) > 85)

        # counterparty concentration (top buyer share of sales receipts)
        buyer_credit = defaultdict(float)
        for t in self.txns:
            if t["category_lvl1"] == "Sales receipt":
                buyer_credit[t["counterparty_name"]] += float(t["amount_inr"])
        top_buyer, top_buyer_val = ("", 0.0)
        if buyer_credit:
            top_buyer, top_buyer_val = max(buyer_credit.items(), key=lambda kv: kv[1])
        sales_total = sum(buyer_credit.values()) or 1.0
        concentration_pct = top_buyer_val / sales_total * 100

        return {
            "customer_id": self.cid,
            "months_covered": len(months),
            "avg_monthly_credit_inr": round(avg_credit, 2),
            "credits_trend_pct": round(trend_pct, 1),
            "credits_trend_label": "rising" if trend_pct > 5 else ("declining" if trend_pct < -5 else "stable"),
            "monthly_credits": {k: round(v, 2) for k, v in credits.items()},
            "monthly_debits": {k: round(v, 2) for k, v in debits.items()},
            "cash_intensity_pct": round(cash_pct, 1),
            "avg_utilization_pct": round(avg_util, 1),
            "peak_utilization_pct": round(peak_util, 1),
            "over_limit_days": over_limit_days,
            "days_over_85_pct": days_over_85,
            "top_counterparty": top_buyer,
            "top_counterparty_concentration_pct": round(concentration_pct, 1),
            "cheque_return_count": len(self.cheque_returns),
            "evidence_refs": [
                f"transactions:{len(self.txns)} rows",
                f"utilization:{len(self.util)} days",
                f"cheque_returns:{len(self.cheque_returns)}",
            ],
        }


class EWSEngine:
    """Early-warning signal classifier (blueprint UC6). Rule-based, explainable,
    and responsible: it produces INDICATORS requiring clarification, never fraud
    labels. Severity follows the synthetic policy thresholds (knowledge_base)."""

    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.conduct = AccountConduct(store, customer_id).summary()
        self.cheque_returns = store.where("cheque_returns", customer_id=customer_id)
        self.docs = store.where("documents", customer_id=customer_id)
        self.insurance = store.where("insurance", customer_id=customer_id)
        self.repayments = store.where("repayments", customer_id=customer_id)

    def signals(self) -> list[dict]:
        sigs: list[dict] = []
        c = self.conduct

        # 1. Declining credits
        if c["credits_trend_pct"] < -10:
            sigs.append(self._sig("Declining credits", "High",
                f"Monthly credits trend {c['credits_trend_pct']}% across the year.",
                "Customer call + debtor aging + watchlist review",
                "Explain decline without overclaiming; consider seasonality/receivable delay.",
                ["analytics:credits_trend"]))

        # 2. Cheque returns
        insufficient = [r for r in self.cheque_returns if r["return_reason"] == "Insufficient funds"]
        if len(self.cheque_returns) >= 3:
            sigs.append(self._sig("Cheque return", "High",
                f"{len(self.cheque_returns)} cheque returns ({len(insufficient)} insufficient funds).",
                "Discuss collections; debtor concentration review",
                "Distinguish technical returns (e.g. signature mismatch) from funds shortfall.",
                ["cheque_returns"]))
        elif len(self.cheque_returns) >= 1:
            sigs.append(self._sig("Cheque return", "Medium",
                f"{len(self.cheque_returns)} cheque return(s).",
                "Monitor; confirm cause", "Lower severity if technical reason.",
                ["cheque_returns"]))

        # 3. Overutilization — distinguish growth vs stress by credits trend
        if c["peak_utilization_pct"] > 95 or c["over_limit_days"] > 0:
            if c["credits_trend_label"] == "declining":
                sigs.append(self._sig("Overutilization", "Medium",
                    f"Peak utilization {c['peak_utilization_pct']}%, {c['over_limit_days']} over-limit day(s) with DECLINING credits.",
                    "Watchlist; no enhancement",
                    "High utilization + weak credits = stress, not growth.",
                    ["utilization", "analytics:credits_trend"]))
            # if rising credits, high utilization is growth-linked => NOT an EWS

        # 4. Delayed interest servicing
        delayed = [r for r in self.repayments if r["payment_status"] == "Delayed"]
        if len(delayed) >= 2:
            sigs.append(self._sig("Delayed interest", "Medium",
                f"{len(delayed)} delayed interest-servicing events.",
                "Confirm cash-flow timing", "Note dates; not conclusive of distress alone.",
                ["repayments"]))

        # 5. Document blockers (insurance expired / stock statement overdue)
        expired_ins = [i for i in self.insurance if i["status"] == "Expired"]
        overdue_stock = [d for d in self.docs if d["document_type"] == "Stock statement" and d["status"] in ("Expired", "Overdue", "Pending") and d.get("blocking_flag") == "Y"]
        blockers = []
        if expired_ins: blockers.append("insurance expired")
        if overdue_stock: blockers.append("stock statement overdue")
        if blockers:
            sigs.append(self._sig("Document overdue", "Critical",
                f"Blocking documents: {', '.join(blockers)}.",
                "Document remediation; block enhancement",
                "State as blocker, not as default; renewal recommendation withheld until addressed.",
                ["documents", "insurance"]))

        # 6. Cash spike
        if c["cash_intensity_pct"] > 10:
            sigs.append(self._sig("Cash spike", "Medium",
                f"Cash deposits {c['cash_intensity_pct']}% of credits (elevated).",
                "Ask for business explanation",
                "Do not allege wrongdoing; request explanation and record it.",
                ["analytics:cash_intensity"]))

        return sigs

    def _sig(self, stype, severity, evidence, action, guardrail, refs):
        return {
            "customer_id": self.cid, "signal_type": stype, "severity": severity,
            "evidence_metric": evidence, "recommended_action": action,
            "false_positive_guardrail": guardrail, "evidence_refs": refs,
        }


class EnhancementAssessor:
    """Limit-enhancement opportunity detection (blueprint UC5). Recommends a review
    ONLY when evidence supports it; blocks on policy/data gaps. Never an approval."""

    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.conduct = AccountConduct(store, customer_id).summary()
        self.ews = EWSEngine(store, customer_id).signals()
        self.facility = store.one("facilities", customer_id=customer_id)
        self.docs = store.where("documents", customer_id=customer_id)

    def assess(self) -> dict:
        c = self.conduct
        critical = [s for s in self.ews if s["severity"] == "Critical"]
        rising = c["credits_trend_label"] == "rising"
        clean_returns = c["cheque_return_count"] <= 1

        blockers = []
        if critical:
            blockers.append("critical document/risk blocker present")
        if not rising:
            blockers.append("credits not on a rising trend")
        if not clean_returns:
            blockers.append("cheque-return history not clean")

        eligible = len(blockers) == 0
        stance = ("Initiate enhancement review subject to credit appraisal"
                  if eligible else "Do not enhance; stabilize conduct first")

        band = ""
        if eligible and self.facility:
            cur = float(self.facility["sanction_limit_inr"])
            band = f"{int(cur*1.5)}-{int(cur*1.67)}"  # ~1.2cr -> 1.8-2.0cr

        caveats = []
        if c["top_counterparty_concentration_pct"] > 35:
            caveats.append(f"buyer concentration ~{c['top_counterparty_concentration_pct']}%")
        pending = [d["document_type"] for d in self.docs if d["status"] in ("Pending", "Expired") and d["required_flag"] == "Y"]
        if pending:
            caveats.append("pending: " + ", ".join(sorted(set(pending))))

        return {
            "customer_id": self.cid,
            "eligible_for_review": eligible,
            "stance": stance,
            "recommended_band_inr": band,
            "blockers": blockers,
            "caveats": caveats,
            "evidence": {
                "credits_trend": c["credits_trend_label"],
                "credits_trend_pct": c["credits_trend_pct"],
                "avg_utilization_pct": c["avg_utilization_pct"],
                "days_over_85_pct": c["days_over_85_pct"],
                "cheque_return_count": c["cheque_return_count"],
            },
            "disclaimer": "Recommendation only. Enhancement requires credit appraisal; not an approval.",
        }
