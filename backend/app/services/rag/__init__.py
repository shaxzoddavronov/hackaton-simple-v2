"""RAG layer.

External contract:
  - :func:`indexer.reindex_workspace` — full rebuild of a workspace's chunks.
  - :func:`indexer.reindex_api_catalog` — rebuild of global API endpoint chunks.
  - :func:`indexer.reindex_document` — rebuild a single uploaded document.
  - :func:`retriever.retrieve` — top-K semantic search for a user message.
  - :func:`differ.schema_changed` — bool on whether two bundles differ structurally.

Embeddings come from a local Triton Inference Server; see
:mod:`app.services.rag.triton_client`. The retriever transparently falls
back to BM25 (``services.schema_pruner``) when Triton is unreachable —
this keeps the agent functional during Triton restarts.
"""
from __future__ import annotations
