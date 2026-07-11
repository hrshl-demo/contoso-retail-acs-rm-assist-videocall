"""
backend/app/routes/workspace.py

RM workspace endpoints (portfolio-level, not tied to one customer view):
  - POST /v1/search                       unified RAG search (product/customer/policy)
  - GET  /v1/customers/{cid}/offers        marketable (eligible + consented) offers
  - POST /v1/customers/{cid}/collateral/email  personalised marketing email
  - GET  /v1/rm/{rm_id}/dilo               daily plan
  - GET  /v1/rm/{rm_id}/milo               daily performance snapshot
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_bearer, get_store
from app.store import DataStore
from app.services.search_unified import unified_search
from app.services.collateral import generate_email, eligible_offers, build_evidence_pack
from app.services.daily_planner import build_dilo, build_milo, build_activity_series

router = APIRouter(prefix="/v1", tags=["workspace"], dependencies=[Depends(require_bearer)])


@router.get("/customers/{customer_id}/raw-facts")
def raw_facts(customer_id: str, store: DataStore = Depends(get_store)):
    """The Core-CRM 'raw facts' view: the deterministic evidence pack the data
    actually supports, displayed AS-IS so the demo contrast with RM Assist
    (which composes insight from the same facts) is explicit."""
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return build_evidence_pack(store, customer_id)


class SearchBody(BaseModel):
    query: str
    scope: str = "auto"            # auto | product | customer | policy | all
    customer_id: str | None = None
    top_k: int = 4


@router.post("/search")
def search(body: SearchBody, store: DataStore = Depends(get_store)):
    return unified_search(store, body.query, body.scope, body.customer_id, body.top_k)


@router.get("/customers/{customer_id}/offers")
def offers(customer_id: str, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return eligible_offers(store, customer_id)


class CollateralBody(BaseModel):
    product_id: str


@router.post("/customers/{customer_id}/collateral/email")
def collateral_email(customer_id: str, body: CollateralBody, store: DataStore = Depends(get_store)):
    if not store.one("customer_master", customer_id=customer_id):
        raise HTTPException(404, f"Customer {customer_id} not found")
    return generate_email(store, customer_id, body.product_id)


@router.get("/rm/{rm_id}/dilo")
def dilo(rm_id: str, store: DataStore = Depends(get_store)):
    return build_dilo(store, rm_id)


@router.get("/rm/{rm_id}/milo")
def milo(rm_id: str, store: DataStore = Depends(get_store)):
    return build_milo(store, rm_id)


@router.get("/rm/{rm_id}/activity-series")
def activity_series(rm_id: str, store: DataStore = Depends(get_store)):
    return build_activity_series(store, rm_id)
