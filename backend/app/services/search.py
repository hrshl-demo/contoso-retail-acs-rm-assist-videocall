"""
backend/app/services/search.py

RAG retrieval over the MSME policy index (AI Search), using hybrid retrieval
(keyword + vector) and semantic ranking. Embeddings come from the Foundry
text-embedding-3-small deployment via managed identity.

Grounding rule (blueprint 10.2): if nothing is retrieved above the floor, callers
must say the policy was not found rather than answer from model memory.
"""
from __future__ import annotations
import logging
from functools import lru_cache

from app.config import get_settings

log = logging.getLogger("contoso.msme.search")


class SearchUnavailable(Exception):
    pass


@lru_cache(maxsize=1)
def _clients():
    """Lazily build the Search + embedding clients with managed identity.
    Cached so we build them once. Raises SearchUnavailable if endpoints are unset
    (e.g. local dev without Azure) so callers can degrade gracefully."""
    s = get_settings()
    if not s.search_endpoint or not s.foundry_aoai_endpoint:
        raise SearchUnavailable("Search/Foundry endpoints not configured")
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from azure.search.documents import SearchClient
    from openai import OpenAI

    cred = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(cred, "https://ai.azure.com/.default")

    ep = s.foundry_aoai_endpoint.rstrip("/")
    if not ep.endswith("/openai/v1"):
        ep = ep.rstrip("/") + "/openai/v1"
    aoai = OpenAI(base_url=ep, api_key=token_provider)
    search = SearchClient(endpoint=s.search_endpoint, index_name=s.search_index_name, credential=cred)
    return search, aoai, s.foundry_embed_deployment


def retrieve(query: str, top_k: int = 4) -> dict:
    """Hybrid + semantic retrieval. Returns grounded chunks with source refs, or a
    'not found' signal callers must honor instead of answering from memory."""
    try:
        search, aoai, embed_dep = _clients()
    except SearchUnavailable as e:
        return {"grounded": False, "reason": str(e), "results": []}

    from azure.search.documents.models import VectorizedQuery
    vec = aoai.embeddings.create(model=embed_dep, input=[query]).data[0].embedding
    results = search.search(
        search_text=query,
        vector_queries=[VectorizedQuery(vector=vec, k_nearest_neighbors=top_k, fields="content_vector")],
        query_type="semantic",
        semantic_configuration_name="default-semantic",
        top=top_k,
        select=["chunk_id", "sop_id", "sop_title", "section_title", "content"],
    )
    hits = []
    for r in results:
        hits.append({
            "chunk_id": r["chunk_id"],
            "sop_id": r["sop_id"],
            "sop_title": r["sop_title"],
            "section_title": r["section_title"],
            "content": r["content"],
            "score": r.get("@search.score", 0),
            "reranker_score": r.get("@search.reranker_score"),
        })
    grounded = len(hits) > 0
    return {
        "grounded": grounded,
        "query": query,
        "results": hits,
        "note": ("Answer only from these sources; cite chunk_id/sop_title."
                 if grounded else
                 "No matching policy found. Tell the user the policy is not available; do not answer from memory."),
    }
