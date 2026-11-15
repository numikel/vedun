"""Pluggable rerankers: Cohere / Qwen3 via the OpenRouter rerank endpoint, Jev (TypeSafe).

A reranker maps ``(query, documents)`` to one relevance score per document (higher = better).
Reranking is optional (``search(..., reranker=None)``) and never applies to exact ``lookup``.
Secrets come from the environment (``kprun run openrouter typesafe -- ...``); never printed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config as C
from .embed import Embedder, EmbeddingError


class RerankError(RuntimeError):
    """Raised when a rerank request ultimately fails."""


@dataclass
class RerankStats:
    """Per-reranker call statistics for one process."""

    calls: int = 0
    failures: int = 0
    docs: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    latencies: list[float] = field(default_factory=list)
    last_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


RERANK_STATS: dict[str, RerankStats] = {k: RerankStats() for k in C.RERANKERS}


class Reranker(Protocol):
    """Interface of every reranker backend."""

    key: str

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """One relevance score per document, aligned with ``documents``."""
        ...


def _post_json(url: str, body: dict, key: str, timeout: int = 120) -> tuple[int | None, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "vedun/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {"error": {"message": str(e)}}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return None, {"error": {"message": f"{type(e).__name__}: {e}"}}


class OpenRouterReranker:
    """``POST /api/v1/rerank`` with ``{model, query, documents, top_n}``.

    ``instruction``: appended as a separate field if the model accepts it, otherwise folded into
    the query (``instruction_mode`` records which one worked; see :func:`probe_instruction`).
    """

    def __init__(
        self,
        spec: C.RerankSpec,
        instruction: str | None = None,
        instruction_mode: str = "query",  # "field" | "query" | "none"
        transport: Callable[[str, dict, str], tuple[int | None, dict]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.key = spec.key
        self.spec = spec
        self.instruction = instruction
        self.instruction_mode = instruction_mode if instruction else "none"
        self._transport = transport or (lambda url, body, k: _post_json(url, body, k))
        self._sleep = sleep
        self.stats = RERANK_STATS.setdefault(spec.key, RerankStats())

    def _body(self, query: str, documents: Sequence[str]) -> dict:
        q = query
        body: dict[str, Any] = {"model": self.spec.api_id, "documents": list(documents), "top_n": len(documents)}
        if self.instruction and self.instruction_mode == "field":
            body["instruction"] = self.instruction
        elif self.instruction and self.instruction_mode == "query":
            q = f"Instruct: {self.instruction}\nQuery: {query}"
        body["query"] = q
        return body

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RerankError("OPENROUTER_API_KEY is not set (run via kprun run openrouter -- ...)")
        body = self._body(query, documents)
        est = sum(len(d) for d in documents) / C.CHARS_PER_TOKEN + len(query) / C.CHARS_PER_TOKEN
        if Embedder._shared_cost + est * self.spec.price_per_m / 1e6 > C.MAX_TOTAL_COST_USD:
            raise RerankError("cost guard: pilot budget would be exceeded")
        delay, last = 2.0, ""
        for _ in range(self.spec.max_attempts):
            t0 = time.monotonic()
            status, payload = self._transport(C.RERANK_URL, body, key)
            lat = time.monotonic() - t0
            self.stats.calls += 1
            results = payload.get("results") if isinstance(payload, dict) else None
            if status == 200 and results:
                self.stats.latencies.append(lat)
                self.stats.docs += len(documents)
                usage = payload.get("usage") or {}
                tok = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
                cost = float(usage.get("cost") or 0.0)
                self.stats.tokens += tok
                self.stats.cost_usd += cost if cost else (tok or int(est)) * self.spec.price_per_m / 1e6
                Embedder._shared_cost += cost if cost else (tok or int(est)) * self.spec.price_per_m / 1e6
                if "usage" in payload:
                    self.stats.extra["usage_sample"] = usage
                scores = [0.0] * len(documents)
                for r in results:
                    scores[int(r["index"])] = float(r["relevance_score"])
                return scores
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            last = f"HTTP {status}: {str(err)[:200]}"
            self.stats.failures += 1
            self.stats.last_error = last
            if status is not None and status not in (408, 409, 425, 429, 500, 502, 503, 504):
                raise RerankError(last)
            self._sleep(delay)
            delay *= 2
        raise RerankError(f"{self.key}: giving up ({last})")


def get_reranker(key: str | None) -> Reranker | None:
    """Reranker for a registry key, or None when reranking is off."""
    if not key or key in ("off", "none"):
        return None
    spec = C.RERANKERS[key]
    if spec.backend == "openrouter":
        # R2 (Qwen3) is instruction-aware: the endpoint accepts an `instruction` field (tested 10.11.2026)
        return OpenRouterReranker(
            spec, instruction=C.RERANK_INSTRUCTION if spec.key == "r2" else None, instruction_mode="field"
        )
    if spec.backend == "jev":
        from .rerank_jev import JevBackend

        return JevBackend(spec)
    raise RerankError(f"unknown backend {spec.backend}")


__all__ = ["RERANK_STATS", "EmbeddingError", "OpenRouterReranker", "RerankError", "Reranker", "get_reranker"]
