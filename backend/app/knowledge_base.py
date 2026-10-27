"""
Knowledge Base retrieval (the `search_fund_docs` capability).

Primary path  : Knowledge Bases for Amazon Bedrock (semantic retrieval) when
                KNOWLEDGE_BASE_ID is configured and reachable.
Fallback path : a local keyword-scored search over the seed corpus in
                data/knowledge_base.json so the assistant works offline.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .aws_clients import bedrock_agent_runtime
from .config import get_settings

logger = logging.getLogger("mf_assistant.kb")

_DATA = Path(__file__).resolve().parent / "data" / "knowledge_base.json"
_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "of", "and", "or", "for", "in", "on",
    "what", "how", "do", "i", "me", "my", "can", "about", "with", "your", "you",
}


@lru_cache
def _local_corpus() -> list[dict[str, Any]]:
    with open(_DATA, encoding="utf-8") as fh:
        return json.load(fh)["documents"]


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _local_search(query: str, top_k: int) -> list[dict[str, Any]]:
    q_tokens = set(_tokenize(query))
    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in _local_corpus():
        haystack = f"{doc['title']} {doc['fund']} {doc['category']} {doc['content']}"
        d_tokens = _tokenize(haystack)
        if not d_tokens:
            continue
        overlap = sum(1 for t in d_tokens if t in q_tokens)
        # Light term-frequency weighting + title boost.
        title_boost = sum(2 for t in _tokenize(doc["title"]) if t in q_tokens)
        score = (overlap + title_boost) / (len(q_tokens) + 1)
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, doc in scored[:top_k]:
        results.append(
            {
                "title": doc["title"],
                "fund": doc["fund"],
                "category": doc["category"],
                "snippet": doc["content"],
                "pdf_url": doc["pdf_url"],
                "score": round(score, 3),
                "source": "local",
            }
        )
    return results


def _bedrock_kb_search(query: str, top_k: int) -> list[dict[str, Any]]:
    s = get_settings()
    resp = bedrock_agent_runtime().retrieve(
        knowledgeBaseId=s.KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": top_k}
        },
    )
    results = []
    for item in resp.get("retrievalResults", []):
        loc = item.get("location", {})
        uri = (
            loc.get("s3Location", {}).get("uri")
            or loc.get("webLocation", {}).get("url")
            or ""
        )
        results.append(
            {
                "title": item.get("metadata", {}).get("title", "Fund document"),
                "fund": item.get("metadata", {}).get("fund", ""),
                "category": item.get("metadata", {}).get("category", ""),
                "snippet": item.get("content", {}).get("text", ""),
                "pdf_url": uri,
                "score": round(item.get("score", 0.0), 3),
                "source": "bedrock-kb",
            }
        )
    return results


def search_fund_docs(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Retrieve the most relevant fund documents for a query."""
    s = get_settings()
    if s.KNOWLEDGE_BASE_ID:
        try:
            results = _bedrock_kb_search(query, top_k)
            if results:
                return results
            logger.info("Bedrock KB returned no results; using local corpus.")
        except Exception as exc:  # noqa: BLE001 - graceful degradation
            logger.warning("Bedrock KB retrieval failed (%s); using local corpus.", exc)
    return _local_search(query, top_k)
