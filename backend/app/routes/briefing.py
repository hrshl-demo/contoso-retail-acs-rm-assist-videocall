"""
backend/app/routes/briefing.py

Start-of-day briefing + cross-sell endpoints (point 2). Bearer-protected.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.deps import require_bearer, get_store
from app.store import DataStore
from app.services.briefing import daily_briefing, customer_brief
from app.services.crosssell import opportunities
from app.services.narrative import playbook, persona_narrative, stakeholder_tree

router = APIRouter(prefix="/v1", tags=["briefing"], dependencies=[Depends(require_bearer)])


@router.get("/briefing/playbook/{customer_id}")
def get_playbook(customer_id: str, store: DataStore = Depends(get_store)):
    """Point 4 — on-the-fly AI RM playbook for the day's conversation."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    return playbook(store, customer_id)


@router.get("/customers/{customer_id}/stakeholders")
def get_stakeholders(customer_id: str, store: DataStore = Depends(get_store)):
    """Point 5 — stakeholder org tree for the persona map."""
    return stakeholder_tree(store, customer_id)


@router.get("/customers/{customer_id}/persona/{stakeholder_id}")
def get_persona_narrative(customer_id: str, stakeholder_id: str, store: DataStore = Depends(get_store)):
    """Point 5 — on-the-fly AI narrative tailored to one stakeholder persona."""
    res = persona_narrative(store, customer_id, stakeholder_id)
    if res.get("error") and "not found" in res["error"]:
        raise HTTPException(404, res["error"])
    return res


class ProgressiveStageBody(BaseModel):
    stage: int = 1
    force: bool = False
    focus_customer_id: str | None = None


@router.get("/customers/{customer_id}/relationship-story/status")
def get_relationship_story_status(customer_id: str, store: DataStore = Depends(get_store)):
    """Metadata for the progressive Customer Thesis / Daily Briefing story."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.briefing_story import customer_story_status
    return customer_story_status(store, customer_id)


@router.post("/customers/{customer_id}/relationship-story/stage")
def post_relationship_story_stage(customer_id: str, body: ProgressiveStageBody,
                                  store: DataStore = Depends(get_store)):
    """Run exactly one progressive relationship-intelligence chapter."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.briefing_story import CUSTOMER_STAGES, customer_story_stage
    if body.stage < 1 or body.stage > len(CUSTOMER_STAGES):
        raise HTTPException(422, f"Stage must be between 1 and {len(CUSTOMER_STAGES)}")
    try:
        return customer_story_stage(store, customer_id, body.stage, body.force)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/briefing/progressive/status")
def get_progressive_briefing_status(rm_id: str = "RM-1042", store: DataStore = Depends(get_store)):
    from app.services.briefing_story import portfolio_story_status
    return portfolio_story_status(store, rm_id)


@router.post("/briefing/progressive/stage")
def post_progressive_briefing_stage(body: ProgressiveStageBody, rm_id: str = "RM-1042",
                                    store: DataStore = Depends(get_store)):
    """Run one stage of the portfolio morning-briefing story."""
    from app.services.briefing_story import PORTFOLIO_STAGES, portfolio_story_stage
    if body.stage < 1 or body.stage > len(PORTFOLIO_STAGES):
        raise HTTPException(422, f"Stage must be between 1 and {len(PORTFOLIO_STAGES)}")
    try:
        return portfolio_story_stage(store, rm_id, body.stage, body.focus_customer_id, body.force)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/briefing/daily")
def get_daily_briefing(rm_id: str | None = None, store: DataStore = Depends(get_store)):
    return daily_briefing(store, rm_id)


# ---- AI intelligence endpoints (dynamic, LLM-grounded) ----
@router.get("/customers/{customer_id}/thesis")
def get_relationship_thesis(customer_id: str, store: DataStore = Depends(get_store)):
    """Dynamic, reasoned relationship thesis (replaces the static briefing card)."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.rm_intelligence import relationship_thesis
    return relationship_thesis(store, customer_id)


@router.get("/customers/{customer_id}/assist-search")
def get_assist_search(customer_id: str, q: str, scope: str = "customer", store: DataStore = Depends(get_store)):
    """Grounded RM-assist search over the customer's own data (PII-masked) + SOPs."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    if not (q or "").strip():
        raise HTTPException(400, "Query 'q' is required")
    from app.services.rm_intelligence import assist_search
    return assist_search(store, customer_id, q.strip(), scope=scope)


@router.get("/customers/{customer_id}/ews-reasoning")
def get_ews_reasoning(customer_id: str, store: DataStore = Depends(get_store)):
    """India-context early-warning reasoning per signal."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.rm_intelligence import ews_reasoning
    return ews_reasoning(store, customer_id)


@router.get("/customers/{customer_id}/collateral-pack")
def get_collateral_pack(customer_id: str, product_id: str | None = None, store: DataStore = Depends(get_store)):
    """Full, detailed personalised outreach + RM sell-sheet (not a one-liner)."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.rm_intelligence import collateral_pack
    return collateral_pack(store, customer_id, product_id)


@router.get("/briefing/dilo-reasoning")
def get_dilo_reasoning(rm_id: str = "RM-1042", store: DataStore = Depends(get_store)):
    """DILO/MILO with the AI rationale over the day plan + portfolio."""
    from app.services.rm_intelligence import dilo_reasoning
    return dilo_reasoning(store, rm_id)


@router.get("/customers/{customer_id}/persona-paths/{stakeholder_id}")
def get_persona_paths(customer_id: str, stakeholder_id: str, store: DataStore = Depends(get_store)):
    """Three grounded conversation simulations (happy/neutral/friction) for a stakeholder."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.rm_intelligence import persona_paths
    res = persona_paths(store, customer_id, stakeholder_id)
    if res.get("error"):
        raise HTTPException(404, res["error"])
    return res


@router.get("/customers/{customer_id}/breach-intelligence")
def get_breach_intelligence(customer_id: str, store: DataStore = Depends(get_store)):
    """AI reasoning over the breach-radar numbers."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.rm_intelligence import breach_intelligence
    return breach_intelligence(store, customer_id)


@router.get("/customers/{customer_id}/mission-action")
def get_mission_action(customer_id: str, title: str, kind: str = "", store: DataStore = Depends(get_store)):
    """AI write-up of how to action a single mission-board item."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    if not (title or "").strip():
        raise HTTPException(400, "Mission 'title' is required")
    from app.services.rm_intelligence import mission_action
    return mission_action(store, customer_id, title.strip(), kind)


@router.get("/customers/{customer_id}/briefing-studio")
def get_briefing_studio(customer_id: str, store: DataStore = Depends(get_store)):
    """Dynamic AI briefing studio for Relationship Thesis & Daily Briefing."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.demo_intelligence import briefing_studio
    return briefing_studio(store, customer_id)


@router.get("/customers/{customer_id}/briefing-drilldown")
def get_briefing_drilldown(
    customer_id: str,
    card_id: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    q: str = "",
    store: DataStore = Depends(get_store),
):
    """Dynamic drilldown for a clicked briefing card.

    Backward compatible: accepts either card_id=why-now or topic=why_now / topic=why-now.
    This prevents FastAPI 422 validation failures from older UI/test scripts.
    """
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    selected = (card_id or topic or "").strip()
    selected = selected.replace("_", "-").lower()
    aliases = {
        "why": "why-now",
        "why-now?": "why-now",
        "blockers": "blocker",
        "major-blocker": "blocker",
        "opening": "safe-language",
        "safe": "safe-language",
        "safe-customer-language": "safe-language",
        "next": "next-question",
        "next-clarifying-question": "next-question",
    }
    selected = aliases.get(selected, selected)
    if not selected:
        selected = "why-now"
    from app.services.demo_intelligence import briefing_drilldown
    return briefing_drilldown(store, customer_id, selected, q)


@router.get("/customers/{customer_id}/breach-income-copilot")
def get_breach_income_copilot(customer_id: str, store: DataStore = Depends(get_store)):
    """AI control tower over Breach Radar + Income Reconciliation."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    from app.services.demo_intelligence import breach_income_copilot
    return breach_income_copilot(store, customer_id)


@router.get("/briefing/customer/{customer_id}")
def get_customer_brief(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, "Customer not found")
    return customer_brief(store, customer_id)


@router.get("/customers/{customer_id}/cross-sell")
def get_cross_sell(customer_id: str, store: DataStore = Depends(get_store)):
    return {"customer_id": customer_id, "opportunities": opportunities(store, customer_id)}
