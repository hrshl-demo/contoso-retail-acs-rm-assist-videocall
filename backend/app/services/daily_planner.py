"""
backend/app/services/daily_planner.py

DILO / MILO for the RM.

DILO (Day-In-Life-Of) — a time-blocked, ordered plan for the RM's day, built from
the live priority queue, open high-priority tasks, and SLA-due items. Deterministic
ordering: risk/service-recovery first, then renewals due, then growth.

MILO (Month/Day-In-Life-Of snapshot) — a performance snapshot from the RM daily
activity log (rm_daily_activity.csv): yesterday vs trailing average, SLA adherence,
totals for the period, and simple momentum indicators.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean

from app.store import DataStore
from app.services.portfolio import priority_queue


def _parse(d):
    for f in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(d)[:10], f).date()
        except (ValueError, TypeError):
            continue
    return None


def _rm_activity_rows(store: DataStore, rm_id: str):
    """Rows for rm_id, or — if that RM has none — the RM with the most activity history.
    Keeps the leadership/reports view populated regardless of which RM id the UI passes."""
    rows = sorted([r for r in store.all("rm_activity") if r.get("rm_id") == rm_id],
                  key=lambda r: r.get("activity_date", ""))
    if rows:
        return rm_id, rows
    by_rm: dict = {}
    for r in store.all("rm_activity"):
        by_rm.setdefault(r.get("rm_id"), []).append(r)
    if not by_rm:
        return rm_id, []
    best = max(by_rm, key=lambda k: len(by_rm[k]))
    return best, sorted(by_rm[best], key=lambda r: r.get("activity_date", ""))


# ------------------------------- DILO ---------------------------------------
def build_dilo(store: DataStore, rm_id: str = "RM-1042") -> dict:
    from app.services.collateral import build_evidence_pack
    pq = priority_queue(store)
    queue = pq.get("queue", []) if isinstance(pq, dict) else pq

    bucket_rank = {"Risk Watch": 0, "Renewal": 1, "Growth": 2}
    ordered = sorted(queue, key=lambda c: (bucket_rank.get(c.get("bucket"), 3),
                                           -int(c.get("relationship_value_score", 0) or 0)))
    slots = ["09:30", "10:30", "11:30", "12:30", "14:30", "15:30", "16:30", "17:00"]
    plan = []
    for i, c in enumerate(ordered[: len(slots)]):
        cid = c.get("customer_id")
        bucket = c.get("bucket", "")
        try:
            ev = build_evidence_pack(store, cid)
        except Exception:
            ev = None
        # action + concrete why + concrete prep, all grounded in the customer's own evidence
        if bucket == "Risk Watch":
            action = "Service-recovery call"
            why_bits = []
            if ev and ev["stress"]["cheque_returns_total"]:
                why_bits.append(f"{ev['stress']['cheque_returns_total']} cheque return(s) this cycle")
            if ev and ev["stress"]["open_service_tickets"]:
                why_bits.append(f"{ev['stress']['open_service_tickets']} open service ticket(s)")
            if ev and ev["stress"]["pending_covenants"]:
                cv = ev["stress"]["pending_covenants"][0]
                why_bits.append(f"covenant pending: {cv['type']} (due {cv['due']})")
            if not why_bits and ev:
                why_bits.append(f"utilisation {ev['facility']['utilisation_avg_30d_pct']}% avg, peak {ev['facility']['utilisation_peak_30d_pct']}%")
            why = " · ".join(why_bits) or c.get("reason", "")
            prep_bits = []
            if ev and ev["stress"]["pending_covenants"]:
                prep_bits.append(f"pull the {ev['stress']['pending_covenants'][0]['type']}")
            if ev:
                prep_bits.append("open breach radar for this customer")
            prep = "; ".join(prep_bits) or "Review breach radar + open tickets before the call"
        elif bucket == "Renewal":
            action = "Renewal preparation"
            why_bits = []
            if ev and ev["facility"].get("review_due_date"):
                why_bits.append(f"facility review due {ev['facility']['review_due_date']}")
            if ev and ev["stress"]["pending_covenants"]:
                why_bits.append(f"{len(ev['stress']['pending_covenants'])} covenant(s) pending")
            if not why_bits and ev:
                why_bits.append(f"FY credits {ev['turnover']['fy_credits_text']}, utilisation {ev['facility']['utilisation_avg_30d_pct']}%")
            why = " · ".join(why_bits) or c.get("reason", "")
            prep = "confirm document checklist; pull last 3 stock statements + bureau"
        else:  # Growth
            action = "Growth / cross-sell discussion"
            why_bits = []
            if ev:
                why_bits.append(f"utilisation {ev['facility']['utilisation_avg_30d_pct']}% on a {ev['facility']['sanction_limit_text']} line")
                if ev["top_buyer"] and ev["top_buyer"].get("name"):
                    why_bits.append(f"top buyer {ev['top_buyer']['name']} (~{ev['top_buyer']['avg_monthly_text']}/month)")
                if ev["stress"]["open_threads"]:
                    why_bits.append(f"open thread: {ev['stress']['open_threads'][0]['topic']}")
            why = " · ".join(why_bits) or c.get("reason", "")
            prep = "load the eligible-offer email (enhancement / invoice discounting) and confirm contact"
        plan.append({
            "slot": slots[i],
            "customer_id": cid,
            "customer": c.get("display_name"),
            "bucket": bucket,
            "action": action,
            "why": why,
            "prep": prep,
            "critical_signals": c.get("critical_signals", 0),
            "blocking_documents": c.get("blocking_documents", 0),
        })

    open_tasks = [t for t in store.all("tasks")
                  if str(t.get("rm_id", rm_id)) == rm_id and str(t.get("status", "")).lower() in ("open", "pending", "")]
    high_tasks = [t for t in open_tasks if str(t.get("priority", "")).lower() in ("high", "critical")]
    today = date(2026, 5, 27)
    due_soon = []
    for t in open_tasks:
        dd = _parse(t.get("due_date"))
        if dd and dd <= today + timedelta(days=2):
            due_soon.append({"task": t.get("title") or t.get("task_id"),
                             "customer_id": t.get("customer_id"),
                             "due_date": t.get("due_date"),
                             "overdue": dd < today})

    return {
        "rm_id": rm_id,
        "as_of": today.isoformat(),
        "headline": f"{len(plan)} priority conversations planned · {len(high_tasks)} high-priority tasks · {len(due_soon)} items due within 48h",
        "time_blocks": plan,
        "task_load": {
            "open_total": len(open_tasks),
            "high_priority": len(high_tasks),
            "due_within_48h": len(due_soon),
            "due_items": sorted(due_soon, key=lambda x: x["due_date"]),
        },
        "focus_theme": ("Heavy risk/service-recovery day — protect the book first"
                        if plan and plan[0]["bucket"] == "Risk Watch"
                        else "Balanced day — renewals and growth"),
        "guardrail": "A suggested plan; the RM re-orders as needed. No customer action is auto-executed.",
    }


# ------------------------------- MILO ---------------------------------------
def build_milo(store: DataStore, rm_id: str = "RM-1042") -> dict:
    rm_id, rows = _rm_activity_rows(store, rm_id)
    if not rows:
        return {"rm_id": rm_id, "available": False,
                "note": "No activity history found for this RM."}

    metrics = ["calls_made", "meetings_held", "tasks_closed",
               "documents_collected", "opportunities_logged", "tickets_resolved"]

    def num(r, k):
        try:
            return float(r.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    latest = rows[-1]
    trailing = rows[:-1][-10:] or rows[:-1] or [latest]

    snapshot = []
    for m in metrics:
        y = num(latest, m)
        avg = mean(num(r, m) for r in trailing) if trailing else y
        delta = y - avg
        snapshot.append({
            "metric": m.replace("_", " ").title(),
            "yesterday": int(y),
            "trailing_avg": round(avg, 1),
            "delta": round(delta, 1),
            "trend": "up" if delta > 0.5 else "down" if delta < -0.5 else "flat",
        })

    sla_due = sum(num(r, "sla_due") for r in rows)
    sla_met = sum(num(r, "sla_met") for r in rows)
    sla_pct = round(sla_met / sla_due * 100, 1) if sla_due else 100.0

    period_totals = {m: int(sum(num(r, m) for r in rows)) for m in metrics}
    credits_latest = num(latest, "portfolio_credits_inr")
    credits_first = num(rows[0], "portfolio_credits_inr")
    credits_mom = round((credits_latest - credits_first) / credits_first * 100, 1) if credits_first else 0.0

    return {
        "rm_id": rm_id,
        "available": True,
        "as_of": latest.get("activity_date"),
        "period_days": len(rows),
        "headline": f"SLA adherence {sla_pct}% over {len(rows)} working days · "
                    f"{period_totals['calls_made']} calls · {period_totals['tasks_closed']} tasks closed",
        "yesterday_snapshot": snapshot,
        "sla": {"due": int(sla_due), "met": int(sla_met), "adherence_pct": sla_pct},
        "period_totals": period_totals,
        "portfolio_credits": {
            "latest_inr": int(credits_latest),
            "period_change_pct": credits_mom,
            "trend": "up" if credits_mom > 0 else "down" if credits_mom < 0 else "flat",
        },
        "guardrail": "Activity metrics are from the RM activity log; indicative performance view only.",
    }


def build_activity_series(store: DataStore, rm_id: str = "RM-1042") -> dict:
    """Daily activity time series for leadership/MD trend charts (calls, SLA, credits, ...)."""
    rm_id, rows = _rm_activity_rows(store, rm_id)

    def num(r, k):
        try:
            return float(r.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    series = [{
        "date": r.get("activity_date"),
        "calls": int(num(r, "calls_made")),
        "meetings": int(num(r, "meetings_held")),
        "tasks_closed": int(num(r, "tasks_closed")),
        "tickets_resolved": int(num(r, "tickets_resolved")),
        "opportunities": int(num(r, "opportunities_logged")),
        "documents": int(num(r, "documents_collected")),
        "sla_due": int(num(r, "sla_due")),
        "sla_met": int(num(r, "sla_met")),
        "credits_inr": int(num(r, "portfolio_credits_inr")),
    } for r in rows]
    return {"rm_id": rm_id, "available": bool(series), "points": len(series), "series": series}
