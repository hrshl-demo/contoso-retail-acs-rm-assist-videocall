"""
backend/app/services/search_unified.py

Unified RM search across three sources:
  - policy   : grounded SOP retrieval (reuses the AI Search index via search.retrieve)
  - product  : the product catalog + product rules (deterministic, local)
  - customer : a specific customer's own record (profile, threads, cases, conduct)

Intent is inferred from the query, or forced via `scope`. Every hit carries a
source label and a citation ref so the CRM chat can show grounding. Deterministic
for product/customer; policy uses AI Search when configured, with a graceful
not-configured signal (never fabricates policy text).
"""
from __future__ import annotations

import re

from app.store import DataStore


_CUSTOMER_RE = re.compile(r"(CTB-(?:MSME|RTL)-\d+)", re.I)


def _kw(query: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", query.lower()) if len(w) > 2]


# ----------------------------- product search -------------------------------
def _search_products(store: DataStore, query: str, top_k: int) -> list[dict]:
    kws = _kw(query)
    catalog = store.all("product_catalog")
    rules = store.all("product_rules")
    scored = []
    for p in catalog:
        hay = " ".join([
            p.get("name", ""), p.get("category", ""), p.get("fit_signals", ""),
            p.get("rationale_template", ""),
        ]).lower()
        score = sum(hay.count(k) for k in kws)
        # light boost for an exact-ish name token match
        if any(k in p.get("name", "").lower() for k in kws):
            score += 3
        if score:
            related = [r for r in rules if r.get("product", "").lower() in p.get("name", "").lower()
                       or p.get("name", "").lower().startswith(r.get("product", "").lower())]
            scored.append({
                "source": "product",
                "title": p.get("name"),
                "category": p.get("category"),
                "snippet": p.get("rationale_template"),
                "fit_signals": p.get("fit_signals"),
                "blocking_signals": p.get("blocking_signals"),
                "rules": [{"rule": r.get("interpretation"), "threshold": r.get("threshold")} for r in related[:3]],
                "ref": f"product_catalog:{p.get('product_id')}",
                "score": score,
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ----------------------------- customer search ------------------------------
def _search_customer(store: DataStore, cid: str, query: str, top_k: int) -> list[dict]:
    kws = _kw(query)
    out = []
    cust = store.one("customer_master", customer_id=cid)
    if cust:
        # build a profile snippet that carries actual numbers, not just field concat
        try:
            from app.services.collateral import build_evidence_pack
            ev = build_evidence_pack(store, cid)
            f, t, tb = ev["facility"], ev["turnover"], ev["top_buyer"]
            snip = (f"{ev['industry']} · since {cust.get('customer_since','')} ({ev.get('vintage_years','—')} yrs) · "
                    f"sanction {f['sanction_limit_text']}, util {f['utilisation_avg_30d_pct']}% avg · "
                    f"FY credits {t['fy_credits_text']}"
                    + (f" · top buyer {tb['name']} (~{tb['avg_monthly_text']}/month)" if tb and tb.get('name') else ""))
        except Exception:
            prof = store.one("business_profile", customer_id=cid) or {}
            snip = (f"{prof.get('industry_description','')} · {cust.get('constitution','')} · "
                    f"customer since {cust.get('customer_since','')} · consent {cust.get('consent_status','')}")
        out.append({
            "source": "customer", "title": f"{cust.get('display_name')} — profile",
            "snippet": snip, "ref": f"customer_master:{cid}", "score": 1,
        })
    # threads / cases / interactions matched by keyword
    for t in store.where("engagement_threads", customer_id=cid):
        hay = (t.get("topic", "") + " " + t.get("notes", "")).lower()
        s = sum(hay.count(k) for k in kws)
        if s:
            out.append({"source": "customer", "title": f"Thread · {t.get('topic')}",
                        "snippet": t.get("notes", ""), "ref": f"engagement_threads:{t.get('thread_id')}", "score": s + 1})
    for i in store.where("interactions", customer_id=cid):
        hay = (i.get("subject", "") + " " + i.get("summary", "")).lower()
        s = sum(hay.count(k) for k in kws)
        if s:
            out.append({"source": "customer", "title": f"Interaction · {i.get('subject')}",
                        "snippet": i.get("summary", ""), "ref": f"interactions:{i.get('interaction_id')}", "score": s})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_k]


# ----------------------------- policy search --------------------------------
def _search_policy(query: str, top_k: int) -> dict:
    try:
        from app.services.search import retrieve, SearchUnavailable
        try:
            res = retrieve(query, top_k)
            hits = []
            for r in (res.get("results") or []):
                hits.append({
                    "source": "policy",
                    "title": r.get("title") or r.get("source") or "SOP",
                    "snippet": (r.get("content") or "")[:280],
                    "ref": r.get("source") or "policy",
                    "score": r.get("reranker_score") or r.get("score") or 0,
                })
            return {"hits": hits, "configured": True}
        except SearchUnavailable:
            return {"hits": [], "configured": False}
    except Exception:
        return {"hits": [], "configured": False}


# ----------------------------- intent + orchestrate -------------------------
def _infer_scope(store: DataStore, query: str, cid: str | None) -> str:
    q = query.lower()
    if cid or _CUSTOMER_RE.search(query):
        return "customer"
    product_terms = ("product", "facility", "loan", "invoice", "discount", "forex",
                     "hedg", "pos", "payroll", "insurance", "letter of credit", "enhancement", "overdraft")
    policy_terms = ("policy", "sop", "rule", "covenant", "eligib", "kyc", "rekyc",
                    "compliance", "threshold", "guideline", "procedure")
    if any(t in q for t in policy_terms):
        return "policy"
    if any(t in q for t in product_terms):
        return "product"
    return "all"


def unified_search(store: DataStore, query: str, scope: str = "auto",
                   customer_id: str | None = None, top_k: int = 4) -> dict:
    if scope == "auto":
        scope = _infer_scope(store, query, customer_id)
    cid = customer_id
    m = _CUSTOMER_RE.search(query or "")
    if not cid and m:
        cid = m.group(1).upper()

    results, policy_configured = [], None
    if scope in ("product", "all"):
        results += _search_products(store, query, top_k)
    if scope in ("customer", "all") and cid:
        results += _search_customer(store, cid, query, top_k)
    if scope in ("policy", "all"):
        pol = _search_policy(query, top_k)
        results += pol["hits"]
        policy_configured = pol["configured"]

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    answerable = bool(results)
    return {
        "query": query,
        "resolved_scope": scope,
        "customer_id": cid,
        "results": results[: max(top_k, 6)],
        "policy_index_configured": policy_configured,
        "answerable": answerable,
        "grounding_note": "Results are retrieved from product catalog, customer records and the policy index. "
                          "Product and customer answers are deterministic; policy answers are grounded in the SOP index.",
    }
