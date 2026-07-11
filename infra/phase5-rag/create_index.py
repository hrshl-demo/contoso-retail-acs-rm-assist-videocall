#!/usr/bin/env python3
"""
infra/phase5-rag/create_index.py

Creates (or updates) the 'contoso-msme-policy-index' AI Search index with:
  - chunk_id (key)
  - sop_id, sop_title, section_title, section_number  (filterable scalars)
  - content (full text, searchable)
  - content_vector (1536 dims, HNSW, cosine)
  - semantic configuration named 'default-semantic' that prioritizes content +
    section_title for reranking

Auth: Entra (signed-in user from VM). Your user has 'Search Service Contributor'
from Phase 2.
"""
from __future__ import annotations
import os
import sys
import logging

from azure.identity import DefaultAzureCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SimpleField,
    SearchableField,
    SearchFieldDataType,
    VectorSearch,
    VectorSearchAlgorithmConfiguration,
    HnswAlgorithmConfiguration,
    HnswParameters,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("create_index")

INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", "contoso-retail-policy-index")
EMBED_DIMS = 1536


def build_index() -> SearchIndex:
    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="sop_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="sop_title", type=SearchFieldDataType.String),
        SearchableField(name="section_title", type=SearchFieldDataType.String),
        SimpleField(name="section_number", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="standard.lucene"),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIMS,
            vector_search_profile_name="hnsw-cosine-profile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="hnsw-cosine",
                parameters=HnswParameters(m=4, ef_construction=400, ef_search=500, metric="cosine"),
            )
        ],
        profiles=[
            VectorSearchProfile(name="hnsw-cosine-profile", algorithm_configuration_name="hnsw-cosine"),
        ],
    )

    semantic_config = SemanticConfiguration(
        name="default-semantic",
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="section_title"),
            content_fields=[SemanticField(field_name="content")],
            keywords_fields=[SemanticField(field_name="sop_title")],
        ),
    )
    semantic_search = SemanticSearch(configurations=[semantic_config])

    return SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )


def main() -> int:
    endpoint = os.environ["SEARCH_ENDPOINT"]
    log.info("Using Search endpoint: %s", endpoint)

    cred = DefaultAzureCredential()
    client = SearchIndexClient(endpoint=endpoint, credential=cred)

    index = build_index()
    log.info("Creating or updating index '%s'...", INDEX_NAME)
    client.create_or_update_index(index)
    log.info("Index '%s' is ready.", INDEX_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
