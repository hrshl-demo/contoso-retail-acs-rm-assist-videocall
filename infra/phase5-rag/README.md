# Phase 5 — RAG over MSME policy SOPs

Indexes the committed SOP corpus (docs/sop/*.md) into AI Search and wires grounded
retrieval into the Tool API for the RM chatbot (UC9).

## Self-contained rebuild contract
The SOPs are COMMITTED artifacts (one-time Foundry output). Phase 5 indexes
whatever is in docs/sop/. up.sh GUARDS against an empty corpus: if no SOPs are
present it stops and tells you to run `bash tools/generate/generate-all.sh` first.
So a rebuild-from-tar is fully self-contained; it can never silently index nothing.

## What it does
1. create_index.py — create/update `contoso-msme-policy-index` (vector + semantic)
2. index_sops.py — chunk by ## section, embed via text-embedding-3-small (MI), upload
3. rebuild Tool API image (now with search.py + /v1/rag/retrieve) and update the app

## Teardown
down.sh drops the SEARCH INDEX only (the Search service is Phase 2; docs/sop is
preserved). Wipe order: phase5 before phase4.

## Retrieval grounding (blueprint 10.2)
If nothing is retrieved, the service returns grounded=false with an explicit
instruction not to answer from model memory.
