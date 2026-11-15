"""Jev (TypeSafe System One) reranker backend; needs ``TYPESAFE_API_KEY`` (kprun entry ``typesafe``)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from . import config as C
from .rerank import RERANK_STATS, RerankError, RerankStats


class JevBackend:
    """``jev-reranker`` (MIT) wrapper: pointwise-style relevance scores in 0..1 per document.

    ``rerank`` scores every document (threshold 0). ``relevance`` uses ``relevance_rerank`` with
    a threshold and returns only the evidence documents (an empty list = "no relevant clause").
    """

    def __init__(self, spec: C.RerankSpec) -> None:
        from jev_reranker import JevReranker

        self.key = spec.key
        self.spec = spec
        self.stats: RerankStats = RERANK_STATS.setdefault(spec.key, RerankStats())
        try:
            self._jev = JevReranker(dotenv_path=None)  # key from TYPESAFE_API_KEY only; no .env reading
        except Exception as e:  # noqa: BLE001 - ConfigurationError etc.
            raise RerankError(f"Jev unavailable: {e}") from e

    def _note(self, res: dict[str, Any], n_docs: int, lat: float) -> None:
        self.stats.calls += 1
        self.stats.docs += n_docs
        self.stats.latencies.append(lat)
        usage = res.get("usage") or (res.get("detail") or {}).get("usage") or {}
        self.stats.tokens += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        self.stats.extra["usage_sample"] = usage

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        t0 = time.monotonic()
        try:
            res = self._jev.rerank(query, list(documents), threshold=0.0, return_documents=False, detail=True)
        except Exception as e:  # noqa: BLE001
            self.stats.failures += 1
            self.stats.last_error = f"{type(e).__name__}: {e}"[:200]
            raise RerankError(self.stats.last_error) from e
        self._note(res, len(documents), time.monotonic() - t0)
        scores = [0.0] * len(documents)
        for r in res["results"]:
            scores[int(r["document_index"])] = float(r["score"])
        return scores

    def relevance(self, query: str, documents: Sequence[str], threshold: float = 0.2) -> list[tuple[int, float]]:
        """Documents that are evidence for ``query`` (score >= threshold), best first."""
        t0 = time.monotonic()
        try:
            res = self._jev.relevance_rerank(
                query, list(documents), threshold=threshold, return_documents=False, detail=True
            )
        except Exception as e:  # noqa: BLE001
            self.stats.failures += 1
            self.stats.last_error = f"{type(e).__name__}: {e}"[:200]
            raise RerankError(self.stats.last_error) from e
        self._note(res, len(documents), time.monotonic() - t0)
        return [(int(r["document_index"]), float(r["score"])) for r in res["results"]]
