"""
backend/app/services/breach_radar.py

Breach Radar — deterministic covenant & limit-headroom monitoring with a
forward projection ("days to breach") and a what-if deterioration simulator.

Philosophy mirrors analytics.py: NO LLM here. Every number is a transparent,
auditable calculation over the synthetic data the store already loads
(daily_limit_utilization, facility_covenants, stock_statements, daily_balances,
loan_facilities, cheque_returns). The dashboard narrates these numbers; this
engine computes them and returns the evidence refs that justify each one.

Guardrail: this engine NEVER recommends automated credit approval/sanction. It
surfaces headroom, projects deterioration, and suggests *review / document /
remediation* actions for a human RM — consistent with the rest of RM Assist.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from statistics import mean

from app.store import DataStore


# ----------------------------- small helpers --------------------------------
def _f(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_date(s: str):
    s = str(s or "")[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _days_until(date_s: str):
    d = _parse_date(date_s)
    if not d:
        return None
    return (d - date.today()).days


def _slope_per_day(points: list[tuple]) -> float:
    """Least-squares slope of y over an integer day index. points: [(day_idx, y)]."""
    n = len(points)
    if n < 2:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = mean(xs), mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


# ----------------------------- core engine ----------------------------------
class BreachRadar:
    # thresholds (POC policy proxies; surfaced to the UI so they're explicit)
    UTIL_WARN = 85.0          # utilization % considered "tight"
    UTIL_BREACH = 100.0       # over sanctioned limit / drawing power
    DP_COVER_MIN = 1.10       # min (stock + receivables) / outstanding before margin stress
    PROJECTION_WINDOW = 60    # days of recent history used for the trend slope
    PROJECTION_HORIZON = 90   # days forward we are willing to project

    def __init__(self, store: DataStore, customer_id: str):
        self.s = store
        self.cid = customer_id
        self.facility = store.one("facilities", customer_id=customer_id) or {}
        self.fac_id = self.facility.get("facility_id")
        self.util = self._sorted(store.where("utilization", customer_id=customer_id), "date")
        self.balances = self._sorted(store.where("daily_balances", customer_id=customer_id), "date")
        self.covenants = store.where("covenants", customer_id=customer_id)
        self.stock = self._sorted(store.where("stock_statements", customer_id=customer_id), "period")
        self.cheque_returns = store.where("cheque_returns", customer_id=customer_id)

    @staticmethod
    def _sorted(rows, key):
        return sorted(rows, key=lambda r: str(r.get(key, "")))

    # ----- latest snapshot -----
    def _latest_util(self) -> dict:
        return self.util[-1] if self.util else {}

    def _latest_stock(self) -> dict:
        received = [s for s in self.stock if str(s.get("status", "")).lower() == "received"]
        return (received or self.stock)[-1] if (received or self.stock) else {}

    # ----- recent-window slope of utilization % -----
    def _util_slope(self) -> tuple[float, list]:
        window = self.util[-self.PROJECTION_WINDOW:]
        if len(window) < 2:
            return 0.0, []
        base = _parse_date(window[0].get("date"))
        pts, spark = [], []
        for u in window:
            d = _parse_date(u.get("date"))
            if not d or not base:
                continue
            idx = (d - base).days
            y = _f(u.get("utilization_pct"))
            pts.append((idx, y))
            spark.append(round(y, 1))
        return _slope_per_day(pts), spark

    def _days_to_util_breach(self, current_util: float, slope: float) -> int | None:
        """How many days until utilization crosses the breach line, at current slope."""
        if slope <= 0:
            return None
        gap = self.UTIL_BREACH - current_util
        if gap <= 0:
            return 0
        days = gap / slope
        if days > self.PROJECTION_HORIZON:
            return None  # beyond the horizon we are willing to assert
        return int(round(days))

    # ----- DP coverage (security cover) -----
    def _dp_coverage(self) -> dict:
        stmt = self._latest_stock()
        latest = self._latest_util()
        outstanding = _f(latest.get("outstanding_inr")) or _f(self.facility.get("current_outstanding_inr"))
        secured = _f(stmt.get("stock_value_inr")) + _f(stmt.get("receivables_inr"))
        cover = (secured / outstanding) if outstanding else 0.0
        return {
            "stock_value_inr": _f(stmt.get("stock_value_inr")),
            "receivables_inr": _f(stmt.get("receivables_inr")),
            "secured_base_inr": secured,
            "outstanding_inr": outstanding,
            "cover_ratio": round(cover, 2),
            "min_cover_ratio": self.DP_COVER_MIN,
            "headroom_pct": round((cover - self.DP_COVER_MIN) / self.DP_COVER_MIN * 100, 1) if cover else 0.0,
            "period": stmt.get("period", ""),
        }

    # ----- covenant register with live status -----
    def _covenant_register(self) -> list[dict]:
        out = []
        for c in self.covenants:
            dtu = _days_until(c.get("due_date"))
            status = str(c.get("status", ""))
            state = "ok"
            if status.lower() in ("breached", "overdue"):
                state = "breach"
            elif status.lower() in ("pending", "due") and dtu is not None and dtu <= 7:
                state = "at_risk"
            elif dtu is not None and dtu < 0:
                state = "breach"
            out.append({
                "covenant_id": c.get("covenant_id"),
                "covenant_type": c.get("covenant_type"),
                "requirement": c.get("requirement_text"),
                "frequency": c.get("frequency"),
                "due_date": c.get("due_date"),
                "days_until_due": dtu,
                "status": status,
                "severity": c.get("severity", "Medium"),
                "breach_action": c.get("breach_action", ""),
                "state": state,
            })
        return out

    # ----- overall breach score (0-100, higher = closer to breach) -----
    def _score(self, current_util, days_to_breach, dp, covenant_states) -> tuple[int, str]:
        score = 0
        # utilization pressure
        if current_util >= self.UTIL_BREACH:
            score += 45
        elif current_util >= self.UTIL_WARN:
            score += 25 + (current_util - self.UTIL_WARN) / (self.UTIL_BREACH - self.UTIL_WARN) * 15
        else:
            score += current_util / self.UTIL_WARN * 15
        # trajectory
        if days_to_breach is not None:
            score += max(0, 25 * (1 - days_to_breach / self.PROJECTION_HORIZON))
        # DP coverage stress
        if dp["cover_ratio"] and dp["cover_ratio"] < self.DP_COVER_MIN:
            score += 18
        elif dp["cover_ratio"] and dp["cover_ratio"] < self.DP_COVER_MIN * 1.15:
            score += 9
        # covenant states
        score += 12 * sum(1 for s in covenant_states if s == "breach")
        score += 5 * sum(1 for s in covenant_states if s == "at_risk")
        # cheque returns are an independent stress flag
        score += min(10, 3 * len(self.cheque_returns))
        score = int(max(0, min(100, round(score))))
        if score >= 70:
            band = "Critical — pre-emptive remediation required"
        elif score >= 45:
            band = "Elevated — monitor closely and pre-position documents"
        elif score >= 25:
            band = "Watch — within tolerance, slope worth tracking"
        else:
            band = "Stable — comfortable headroom"
        return score, band

    # ----- public: the live radar snapshot -----
    def snapshot(self) -> dict:
        latest = self._latest_util()
        current_util = _f(latest.get("utilization_pct"))
        slope, spark = self._util_slope()
        dtb = self._days_to_util_breach(current_util, slope)
        dp = self._dp_coverage()
        covs = self._covenant_register()
        score, band = self._score(current_util, dtb, dp, [c["state"] for c in covs])

        sanction = _f(self.facility.get("sanction_limit_inr"))
        drawing_power = _f(latest.get("drawing_power_inr")) or _f(self.facility.get("drawing_power_inr"))
        outstanding = _f(latest.get("outstanding_inr")) or _f(self.facility.get("current_outstanding_inr"))
        available = _f(latest.get("available_limit_inr"))
        if not available and drawing_power:
            available = max(0.0, drawing_power - outstanding)

        return {
            "customer_id": self.cid,
            "facility_id": self.fac_id,
            "as_of": latest.get("date", ""),
            "breach_score": score,
            "breach_band": band,
            "headroom": {
                "sanction_limit_inr": sanction,
                "drawing_power_inr": drawing_power,
                "outstanding_inr": outstanding,
                "available_limit_inr": round(available, 2),
                "current_utilization_pct": round(current_util, 1),
                "utilization_warn_pct": self.UTIL_WARN,
                "utilization_breach_pct": self.UTIL_BREACH,
                "utilization_slope_per_day": round(slope, 3),
                "days_to_breach": dtb,
                "days_over_85_rolling_30": int(_f(latest.get("days_over_85_pct_rolling_30"))),
            },
            "dp_coverage": dp,
            "covenants": covs,
            "utilization_sparkline": spark,
            "stress_flags": {
                "cheque_returns": len(self.cheque_returns),
                "over_limit_now": latest.get("over_limit_flag") == "Y",
            },
            "evidence_refs": [
                "daily_limit_utilization", "facility_covenants",
                "stock_statements", "loan_facilities", "cheque_returns",
            ],
            "guardrail": "Headroom and projection are decision support only; no automated "
                         "sanction or limit approval. Actions are review/document/remediation.",
        }

    # ----- public: what-if deterioration simulator -----
    def simulate(self, scenario: dict) -> dict:
        """
        scenario keys (all optional, default 0):
          buyer_payment_delay_inr : a receivable that slips (reduces inflow / raises drawdown)
          delay_days              : how long it slips
          sales_drop_pct          : % drop in monthly sales receipts
          additional_drawdown_inr : extra utilisation the customer wants
        Re-computes projected utilisation, DP coverage and breach score.
        """
        base = self.snapshot()
        h = base["headroom"]
        dp = base["dp_coverage"]

        delay_inr = _f(scenario.get("buyer_payment_delay_inr"))
        delay_days = _f(scenario.get("delay_days"))
        sales_drop = _f(scenario.get("sales_drop_pct"))
        extra_draw = _f(scenario.get("additional_drawdown_inr"))

        sanction = h["sanction_limit_inr"] or 1.0
        drawing_power = h["drawing_power_inr"] or sanction
        outstanding = h["outstanding_inr"]

        # 1) a delayed receivable + extra drawdown push outstanding up
        proj_outstanding = outstanding + delay_inr + extra_draw
        # 2) base on drawing power (CC limits draw against DP, not just sanction)
        denom = drawing_power or sanction
        proj_util = (proj_outstanding / denom * 100) if denom else 0.0

        # 3) the same delayed receivable reduces the secured receivables base;
        #    a sales drop erodes the stock base over the delay window
        proj_receivables = max(0.0, dp["receivables_inr"] - delay_inr)
        proj_stock = dp["stock_value_inr"] * (1 - min(0.9, sales_drop / 100.0))
        proj_secured = proj_receivables + proj_stock
        proj_cover = (proj_secured / proj_outstanding) if proj_outstanding else 0.0

        # 4) project utilisation forward at the existing slope across the delay window
        slope = h["utilization_slope_per_day"]
        horizon = int(delay_days or 30)
        drift_util = proj_util + slope * horizon

        # 5) re-score with the projected figures
        cov_states = [c["state"] for c in base["covenants"]]
        sim = BreachRadar.__new__(BreachRadar)          # lightweight reuse of _score
        sim.UTIL_WARN, sim.UTIL_BREACH = self.UTIL_WARN, self.UTIL_BREACH
        sim.DP_COVER_MIN, sim.PROJECTION_HORIZON = self.DP_COVER_MIN, self.PROJECTION_HORIZON
        sim.cheque_returns = self.cheque_returns
        dp_proj = {"cover_ratio": round(proj_cover, 2), "min_cover_ratio": self.DP_COVER_MIN}
        proj_score, proj_band = BreachRadar._score(
            sim, min(drift_util, 130), self._days_to_util_breach(proj_util, slope), dp_proj, cov_states)

        # 6) deterministic recommended action (review/document/remediation only)
        actions = []
        if drift_util >= self.UTIL_BREACH:
            actions.append({
                "kind": "Remediation",
                "action": "Initiate a working-capital enhancement REVIEW (not approval) and stage the document checklist before the projected breach date.",
                "why": f"Projected utilisation reaches {round(drift_util)}% within ~{horizon} days under this scenario.",
                "refs": ["enhancement_assessor", "document_checklist_rules"],
            })
        if proj_cover and proj_cover < self.DP_COVER_MIN:
            actions.append({
                "kind": "Document",
                "action": "Request an updated stock + receivables statement to re-establish drawing-power cover.",
                "why": f"Projected security cover {proj_cover:.2f}x falls below the {self.DP_COVER_MIN:.2f}x floor.",
                "refs": ["stock_statements", "facility_covenants"],
            })
        if delay_inr > 0:
            actions.append({
                "kind": "Engagement",
                "action": "Confirm the delayed buyer receivable and consider invoice-discounting as a bridge, subject to eligibility.",
                "why": f"A delayed receivable of {self._inr(delay_inr)} is the primary driver in this scenario.",
                "refs": ["debtor_creditor_aging", "product_catalog"],
            })
        if not actions:
            actions.append({
                "kind": "Monitor",
                "action": "No pre-emptive action required; continue monthly covenant tracking.",
                "why": "Projected metrics remain within tolerance.",
                "refs": ["facility_covenants"],
            })

        return {
            "customer_id": self.cid,
            "scenario": {
                "buyer_payment_delay_inr": delay_inr,
                "delay_days": int(delay_days),
                "sales_drop_pct": sales_drop,
                "additional_drawdown_inr": extra_draw,
            },
            "baseline": {
                "utilization_pct": h["current_utilization_pct"],
                "cover_ratio": dp["cover_ratio"],
                "breach_score": base["breach_score"],
                "days_to_breach": h["days_to_breach"],
            },
            "projected": {
                "utilization_pct": round(proj_util, 1),
                "utilization_pct_after_window": round(drift_util, 1),
                "outstanding_inr": round(proj_outstanding, 2),
                "cover_ratio": round(proj_cover, 2),
                "breach_score": proj_score,
                "breach_band": proj_band,
                "crosses_breach_line": drift_util >= self.UTIL_BREACH,
            },
            "delta": {
                "utilization_pct": round(drift_util - h["current_utilization_pct"], 1),
                "cover_ratio": round(proj_cover - dp["cover_ratio"], 2),
                "breach_score": proj_score - base["breach_score"],
            },
            "recommended_actions": actions,
            "evidence_refs": base["evidence_refs"],
            "guardrail": base["guardrail"],
        }

    @staticmethod
    def _inr(n: float) -> str:
        n = _f(n)
        if abs(n) >= 1e7:
            return f"₹{n/1e7:.2f} Cr"
        if abs(n) >= 1e5:
            return f"₹{n/1e5:.2f} L"
        return f"₹{n:,.0f}"


# module-level convenience (mirrors command_center.py style)
def breach_radar(store: DataStore, customer_id: str) -> dict:
    return BreachRadar(store, customer_id).snapshot()


def breach_simulate(store: DataStore, customer_id: str, scenario: dict) -> dict:
    return BreachRadar(store, customer_id).simulate(scenario or {})
