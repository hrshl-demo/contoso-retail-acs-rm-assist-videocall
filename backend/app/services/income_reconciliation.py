"""
backend/app/services/income_reconciliation.py

UC (replaces the what-if simulator): AI Income Reconciliation & Turnover
Triangulation.

A credit RM's hardest pre-renewal task is reconciling three INDEPENDENT measures
of the same MSME's revenue:
  1. GST-declared sales            (gst_returns_monthly.gst_sales_inr)
  2. Bank credits into the account (transactions, CR legs -> monthly)
  3. Audited turnover              (financial_statements_summary.turnover_inr, pro-rated)

When these diverge, *why?* Under-banking of cash receipts, related-party round-
tripping, channel sales outside the account, or simply timing. This engine:

  - builds a month-by-month triangulation of GST vs bank credits,
  - benchmarks both against the pro-rated audited turnover run-rate,
  - correlates divergence with cash-intensity and related-party transaction flags,
  - emits DETERMINISTIC, explainable divergence findings (auditable backbone),

and the route adds a grounded LLM analyst narrative on top (never invents figures,
never alleges wrongdoing — uses clarification-seeking language).

Design mirrors EWSEngine: rule-based, explainable, responsible. Produces INDICATORS
requiring clarification, never conclusions of misconduct.
"""
from __future__ import annotations
from statistics import mean

from app.store import DataStore
from app.services.analytics import AccountConduct


def _f(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


class IncomeReconciliation:
    # divergence thresholds (synthetic policy). |gst - bank| / max(...) as %.
    VARIANCE_WATCH = 8.0      # %, watch
    VARIANCE_HIGH = 15.0      # %, material
    CASH_INTENSITY_ELEVATED = 10.0   # % of credits

    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.cust = store.one("customer_master", customer_id=customer_id) or {}
        self.conduct = AccountConduct(store, customer_id).summary()
        self.gst = sorted(store.where("gst", customer_id=customer_id),
                          key=lambda r: r.get("period", ""))
        self.fin = store.one("financials", customer_id=customer_id) or {}
        self.txns = store.where("transactions", customer_id=customer_id)

    # ---- per-month cash / related-party share of credits ----
    def _monthly_flags(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for t in self.txns:
            if t.get("dr_cr") != "CR":
                continue
            mo = (t.get("txn_date") or "")[:7]
            if not mo:
                continue
            d = out.setdefault(mo, {"credit": 0.0, "cash": 0.0, "related": 0.0})
            amt = _f(t.get("amount_inr"))
            d["credit"] += amt
            if t.get("is_cash") == "Y":
                d["cash"] += amt
            if t.get("is_related_party") == "Y":
                d["related"] += amt
        return out

    def triangulate(self) -> dict:
        bank_credits = self.conduct.get("monthly_credits", {})  # {YYYY-MM: inr}
        flags = self._monthly_flags()

        # pro-rated audited turnover run-rate (annual / 12) as a benchmark line
        turnover = _f(self.fin.get("turnover_inr"))
        monthly_turnover_runrate = round(turnover / 12.0, 2) if turnover else 0.0

        rows = []
        for g in self.gst:
            period = g.get("period")
            gst_sales = _f(g.get("gst_sales_inr"))
            bank = round(_f(bank_credits.get(period)), 2)
            mf = flags.get(period, {})
            cash_share = round((mf.get("cash", 0.0) / mf["credit"] * 100), 1) if mf.get("credit") else 0.0
            rp_share = round((mf.get("related", 0.0) / mf["credit"] * 100), 1) if mf.get("credit") else 0.0
            # divergence magnitude: prefer seeded variance_vs_bank_credits_pct if present
            # (use absolute value — the sign is captured separately by `direction`),
            # else compute |gst - bank| / max(gst, bank).
            seeded = g.get("variance_vs_bank_credits_pct")
            if seeded not in (None, ""):
                variance_pct = round(abs(_f(seeded)), 1)
            else:
                base = max(gst_sales, bank) or 1.0
                variance_pct = round(abs(gst_sales - bank) / base * 100, 1)
            direction = "gst_above_bank" if gst_sales > bank else ("bank_above_gst" if bank > gst_sales else "aligned")
            rows.append({
                "period": period,
                "gst_sales_inr": round(gst_sales, 2),
                "bank_credits_inr": bank,
                "turnover_runrate_inr": monthly_turnover_runrate,
                "variance_pct": variance_pct,
                "direction": direction,
                "cash_share_pct": cash_share,
                "related_party_share_pct": rp_share,
                "filing_status": g.get("filing_status"),
                "trend_tag": g.get("trend_tag"),
            })

        findings = self._findings(rows)
        # aggregate view
        gst_total = sum(r["gst_sales_inr"] for r in rows)
        bank_total = sum(r["bank_credits_inr"] for r in rows)
        agg_variance = round(abs(gst_total - bank_total) / (max(gst_total, bank_total) or 1) * 100, 1)
        return {
            "customer_id": self.cid,
            "display_name": self.cust.get("display_name"),
            "fy": self.fin.get("fy"),
            "audited_turnover_inr": turnover,
            "monthly_turnover_runrate_inr": monthly_turnover_runrate,
            "months": rows,
            "aggregate": {
                "gst_sales_total_inr": round(gst_total, 2),
                "bank_credits_total_inr": round(bank_total, 2),
                "variance_pct": agg_variance,
                "gst_vs_turnover_pct": round((gst_total / turnover * 100), 1) if turnover else None,
                "bank_vs_turnover_pct": round((bank_total / turnover * 100), 1) if turnover else None,
            },
            "findings": findings,
            "evidence_refs": ["gst_returns_monthly", "transactions", "financial_statements_summary",
                              "analytics:cash_intensity"],
            "guardrail": ("Divergences are INDICATORS requiring clarification, not findings of "
                          "misreporting. Confirm cause with the customer before any action."),
        }

    def _findings(self, rows: list[dict]) -> list[dict]:
        f: list[dict] = []
        if not rows:
            return f
        high_months = [r for r in rows if r["variance_pct"] >= self.VARIANCE_HIGH]
        watch_months = [r for r in rows if self.VARIANCE_WATCH <= r["variance_pct"] < self.VARIANCE_HIGH]
        gst_above = [r for r in rows if r["direction"] == "gst_above_bank" and r["variance_pct"] >= self.VARIANCE_WATCH]
        cash_heavy = [r for r in rows if r["cash_share_pct"] >= self.CASH_INTENSITY_ELEVATED]
        rp_present = [r for r in rows if r["related_party_share_pct"] > 0]

        if high_months:
            periods = ", ".join(r["period"] for r in high_months[:4])
            f.append(self._finding(
                "Material GST vs bank divergence", "High",
                f"{len(high_months)} month(s) with >= {self.VARIANCE_HIGH:.0f}% variance ({periods}).",
                "Reconcile declared sales against banked receipts; ask for the channel of unbanked sales.",
                ["gst_returns_monthly", "transactions"]))
        elif watch_months:
            f.append(self._finding(
                "GST vs bank divergence (watch)", "Medium",
                f"{len(watch_months)} month(s) with {self.VARIANCE_WATCH:.0f}-{self.VARIANCE_HIGH:.0f}% variance.",
                "Monitor; confirm timing differences vs structural under-banking.",
                ["gst_returns_monthly", "transactions"]))

        if gst_above and cash_heavy:
            f.append(self._finding(
                "Possible under-banking of cash sales", "High",
                f"GST sales exceed bank credits in {len(gst_above)} month(s) while cash credits are "
                f"elevated (>= {self.CASH_INTENSITY_ELEVATED:.0f}% of credits in {len(cash_heavy)} month(s)).",
                "Ask the customer to route cash sales through the account; request latest GSTR-3B for tie-out.",
                ["analytics:cash_intensity", "gst_returns_monthly"]))

        if rp_present:
            f.append(self._finding(
                "Related-party flows in credits", "Medium",
                f"Related-party credits present in {len(rp_present)} month(s); exclude before reading sales growth.",
                "Identify the related entity and confirm these are genuine sales, not rotation of funds.",
                ["transactions:is_related_party", "SOP 07_cash_intensity_and_related_party"]))

        # turnover triangulation: does annualised bank/GST line up with audited turnover?
        turnover = _f(self.fin.get("turnover_inr"))
        if turnover:
            gst_total = sum(r["gst_sales_inr"] for r in rows)
            if gst_total and abs(gst_total - turnover) / turnover * 100 >= 12:
                hi, lo = ("GST sales", "audited turnover") if gst_total > turnover else ("audited turnover", "GST sales")
                f.append(self._finding(
                    "GST vs audited turnover gap", "Medium",
                    f"Full-year GST sales (~Rs {gst_total:,.0f}) differ from audited turnover (~Rs {turnover:,.0f}) by >= 12%.",
                    f"Clarify why {hi} exceeds {lo}; reconcile with the financial statements and GST annual return.",
                    ["gst_returns_monthly", "financial_statements_summary"]))
        if not f:
            f.append(self._finding(
                "Income sources reconcile", "Info",
                "GST sales, bank credits and audited turnover are broadly consistent across the period.",
                "No reconciliation action needed; income quality supports a normal review.",
                ["gst_returns_monthly", "transactions", "financial_statements_summary"]))
        return f

    def _finding(self, ftype, severity, evidence, action, refs):
        return {
            "customer_id": self.cid, "finding_type": ftype, "severity": severity,
            "evidence_metric": evidence, "recommended_action": action,
            "false_positive_guardrail": "Indicator only; confirm cause before acting. Do not allege misreporting.",
            "evidence_refs": refs,
        }


def income_reconciliation(store: DataStore, customer_id: str) -> dict:
    return IncomeReconciliation(store, customer_id).triangulate()
