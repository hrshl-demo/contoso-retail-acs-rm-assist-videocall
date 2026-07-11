"""
backend/app/routes/rag.py

/v1/rag/retrieve — grounded policy retrieval for the RM chatbot (UC9).
Bearer-protected. Returns source-cited chunks or an explicit not-found signal.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import require_bearer
from app.services.search import retrieve

router = APIRouter(prefix="/v1/rag", tags=["rag"], dependencies=[Depends(require_bearer)])


class RetrieveBody(BaseModel):
    query: str
    top_k: int = 4


@router.post("/retrieve")
def rag_retrieve(body: RetrieveBody):
    return retrieve(body.query, body.top_k)
