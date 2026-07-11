#!/usr/bin/env python3
"""
infra/phase5-rag/index_sops.py

Walks docs/sop/, chunks each SOP by '## N.' section, embeds each chunk via
text-embedding-3-small on the existing Foundry account, and uploads the
documents to AI Search.

Auth: Entra (signed-in user from VM). Your user has:
  - 'Cognitive Services OpenAI User' on the AIServices account (Phase 2)
  - 'Search Index Data Contributor' on the Search service (Phase 2)

Required env vars:
  SEARCH_ENDPOINT          https://srch-...search.windows.net/
  SEARCH_INDEX_NAME        contoso-retail-policy-index
  FOUNDRY_AOAI_ENDPOINT    https://<aiservices-account>.services.ai.azure.com/openai/v1
  FOUNDRY_EMBED_DEPLOYMENT text-embedding-3-small
  SOP_DIR                  /path/to/docs/sop
"""
from __future__ import annotations
import os
import sys
import logging
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from openai import OpenAI

# Local import (this file lives in infra/phase5-rag/)
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from sop_chunker import chunk_all_sops, SopChunk

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("index_sops")


def _aoai_endpoint() -> str:
    """Strip /openai/... suffix that Phase 2 wrote to KV — the SDK adds it back."""
    ep = os.environ["FOUNDRY_AOAI_ENDPOINT"]
    for suffix in ("/openai/v1/", "/openai/v1", "/openai/", "/openai", "/"):
        if ep.endswith(suffix):
            return ep[: -len(suffix)]
    return ep


def embed_batch(client: OpenAI, deployment: str, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. text-embedding-3-small accepts up to 2048 inputs per call."""
    resp = client.embeddings.create(model=deployment, input=texts)
    # response.data is in input order
    return [d.embedding for d in resp.data]


def main() -> int:
    sop_dir = Path(os.environ["SOP_DIR"])
    search_endpoint = os.environ["SEARCH_ENDPOINT"]
    index_name = os.environ.get("SEARCH_INDEX_NAME", "contoso-retail-policy-index")
    embed_deployment = os.environ["FOUNDRY_EMBED_DEPLOYMENT"]

    log.info("Sop dir:           %s", sop_dir)
    log.info("Search endpoint:   %s", search_endpoint)
    log.info("Index:             %s", index_name)
    log.info("Embed deployment:  %s", embed_deployment)

    chunks: list[SopChunk] = chunk_all_sops(sop_dir)
    log.info("Chunked %d sections from %d SOP file(s)",
             len(chunks), len({c.sop_id for c in chunks}))
    if not chunks:
        log.error("No chunks produced — check SOP_DIR")
        return 1

    # ---- Embeddings (one batch, since we have ~65 chunks well under any limit) ----
    cred = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(cred, "https://ai.azure.com/.default")
    ep = os.environ["FOUNDRY_AOAI_ENDPOINT"].rstrip("/")
    if not ep.endswith("/openai/v1"):
        ep = ep.rstrip("/") + "/openai/v1"
    aoai = OpenAI(base_url=ep, api_key=token_provider)
    log.info("Embedding %d chunks via %s...", len(chunks), embed_deployment)
    t0 = time.monotonic()
    vectors = embed_batch(aoai, embed_deployment, [c.content for c in chunks])
    log.info("Embedded in %.2fs (avg %.1f dims, expected 1536)",
             time.monotonic() - t0, sum(len(v) for v in vectors) / max(len(vectors), 1))

    # ---- Upload to Search ----
    search = SearchClient(endpoint=search_endpoint, index_name=index_name, credential=cred)
    docs = []
    for c, v in zip(chunks, vectors):
        docs.append({
            "chunk_id": c.chunk_id,
            "sop_id": c.sop_id,
            "sop_title": c.sop_title,
            "section_title": c.section_title,
            "section_number": c.section_number,
            "content": c.content,
            "content_vector": v,
        })

    log.info("Uploading %d documents to Search...", len(docs))
    t0 = time.monotonic()
    # merge_or_upload is idempotent — re-runs replace existing docs by key (chunk_id)
    result = search.merge_or_upload_documents(documents=docs)
    succeeded = sum(1 for r in result if r.succeeded)
    log.info("Uploaded %d/%d (%.2fs)", succeeded, len(docs), time.monotonic() - t0)

    if succeeded < len(docs):
        for r in result:
            if not r.succeeded:
                log.error("  failed: key=%s err=%s", r.key, r.error_message)
        return 2

    # ---- Optional: quick sanity query ----
    log.info("Sanity query: 'what documents are required before OD renewal'")
    sanity_vec = embed_batch(aoai, embed_deployment, ["what documents are required before OD renewal"])[0]
    from azure.search.documents.models import VectorizedQuery
    results = search.search(
        search_text="what documents are required before OD renewal",
        vector_queries=[VectorizedQuery(vector=sanity_vec, k_nearest_neighbors=3, fields="content_vector")],
        top=3,
        select=["chunk_id", "section_title"],
    )
    for r in results:
        log.info("  - %s :: %s  (score=%.3f)",
                 r["chunk_id"], r["section_title"], r.get("@search.score", 0))

    return 0


if __name__ == "__main__":
    sys.exit(main())
