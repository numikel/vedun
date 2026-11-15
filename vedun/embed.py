"""Embeddings client for OpenAI-compatible endpoints (default OpenRouter): batching, retries, cost guard, MRL truncation.

The API key is read from the model's ``api_key_env`` variable (default ``OPENROUTER_API_KEY``) at call time only.
It is never stored, printed or logged.
"""

from __future__ import annotations

import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    CHARS_PER_TOKEN,
    IMAGE_TOKENS_ESTIMATE,
    MAX_TOTAL_COST_USD,
    MODELS,
    ModelSpec,
)


class EmbeddingError(RuntimeError):
    """Raised when an embedding request ultimately fails."""


class BudgetExceeded(EmbeddingError):
    """Raised when the estimated or actual cost would exceed the cost guard."""


class TimeBudgetExceeded(EmbeddingError):
    """Raised when the per-model time budget for a run is used up."""


@dataclass
class CallStats:
    """Aggregated API statistics for one model within one process."""

    calls: int = 0
    failures: int = 0
    retries: int = 0
    items_ok: int = 0
    prompt_tokens: int = 0
    cost_usd: float = 0.0
    latencies: list[float] = field(default_factory=list)
    last_error: str = ""

    def summary(self) -> dict[str, float | int | str]:
        lat = sorted(self.latencies)
        return {
            "calls": self.calls,
            "failures": self.failures,
            "retries": self.retries,
            "items_ok": self.items_ok,
            "prompt_tokens": self.prompt_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "lat_median_s": round(lat[len(lat) // 2], 2) if lat else 0.0,
            "lat_max_s": round(lat[-1], 2) if lat else 0.0,
            "last_error": self.last_error[:200],
        }


#: Process-wide statistics, keyed by model key. Used by build + eval reports.
STATS: dict[str, CallStats] = {k: CallStats() for k in MODELS}


def truncate_normalize(vec: Sequence[float], dim: int) -> list[float]:
    """Matryoshka truncation: keep the first ``dim`` values and L2-normalise."""
    v = list(vec[:dim])
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def estimate_tokens(texts: Sequence[str]) -> int:
    """Conservative token estimate (characters / 3.5)."""
    return int(sum(len(t) for t in texts) / CHARS_PER_TOKEN) + len(texts)


def png_to_data_url(path: Path) -> str:
    """Encode a PNG file as a ``data:image/png;base64,...`` URL."""
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _is_retryable(code: int | None) -> bool:
    return code is None or code in (408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524)


class Embedder:
    """Embeds texts and images with one OpenRouter model.

    Args:
        spec: Model specification.
        total_cost_guard: Abort if cumulative cost (shared across the process) exceeds this.
        sleep: Injectable sleep for tests.
        transport: Injectable ``(url, headers, body) -> (status, json)``; used by tests.
    """

    _shared_cost: float = 0.0  # across all Embedder instances in the process

    def __init__(
        self,
        spec: ModelSpec,
        total_cost_guard: float = MAX_TOTAL_COST_USD,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[[str, dict[str, str], bytes], tuple[int | None, dict]] | None = None,
    ) -> None:
        self.spec = spec
        self.guard = total_cost_guard
        self._sleep = sleep
        self._transport = transport or self._http
        self.stats = STATS.setdefault(spec.key, CallStats())
        self.deadline: float | None = None

    # ---- budget -------------------------------------------------------------------------
    def start_run(self) -> None:
        """(Re)start the per-run time budget for this model."""
        self.deadline = time.monotonic() + self.spec.time_budget_s

    def estimate_cost(self, n_text_tokens: int = 0, n_images: int = 0) -> float:
        s = self.spec
        return (n_text_tokens * s.price_text_per_m + n_images * IMAGE_TOKENS_ESTIMATE * s.price_image_per_m) / 1e6

    def check_budget(self, extra: float) -> None:
        """Abort if the estimated cost of the next step would exceed the guard."""
        if Embedder._shared_cost + extra > self.guard:
            raise BudgetExceeded(
                f"cost guard: spent {Embedder._shared_cost:.4f} + est. {extra:.4f} USD > {self.guard:.2f} USD"
            )

    # ---- transport ----------------------------------------------------------------------
    @staticmethod
    def _http(url: str, headers: dict[str, str], body: bytes) -> tuple[int | None, dict]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                payload = {"error": {"message": str(e)}}
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            return None, {"error": {"message": f"{type(e).__name__}: {e}"}}

    def _request(self, inputs: list, dimensions: int | None = None) -> list[list[float]]:
        key = os.environ.get(self.spec.api_key_env)
        if not key:
            raise EmbeddingError(f"{self.spec.api_key_env} is not set (API key of embedding model {self.spec.key!r})")
        body_d: dict = {"model": self.spec.api_id, "input": inputs, "encoding_format": "float"}
        if dimensions:
            body_d["dimensions"] = dimensions
        body = json.dumps(body_d).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "vedun/0.1",
        }
        delay = 2.0
        last = ""
        for _attempt in range(self.spec.max_attempts):
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise TimeBudgetExceeded(f"{self.spec.key}: time budget used up ({last})")
            t0 = time.monotonic()
            status, payload = self._transport(self.spec.base_url, headers, body)
            lat = time.monotonic() - t0
            self.stats.calls += 1
            data = payload.get("data") if isinstance(payload, dict) else None
            if status == 200 and data:
                self.stats.latencies.append(lat)
                usage = payload.get("usage") or {}
                self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                # OpenRouter reports the price; other providers do not, so estimate it from tokens and the model price
                cost = (
                    float(usage["cost"])
                    if usage.get("cost") is not None
                    else self.estimate_cost(int(usage.get("prompt_tokens") or 0))
                )
                self.stats.cost_usd += cost
                Embedder._shared_cost += cost
                self.stats.items_ok += len(data)
                data = sorted(data, key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in data]
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            last = f"HTTP {status}: {str(err)[:200]}"
            self.stats.last_error = last
            self.stats.failures += 1
            if not _is_retryable(status) and status != 200:
                # 200 with an empty body is treated as retryable (flaky free provider)
                raise EmbeddingError(last)
            self.stats.retries += 1
            self._sleep(min(delay, 60.0))
            delay *= 2
        raise EmbeddingError(f"{self.spec.key}: giving up after retries ({last})")

    # ---- public API ---------------------------------------------------------------------
    def embed_texts(self, texts: Sequence[str], role: str = "doc") -> list[list[float]]:
        """Embed texts. ``role`` is ``"doc"`` or ``"query"`` (applies the model's template)."""
        tpl = self.spec.query_template if role == "query" else self.spec.doc_template
        prepared = [tpl.replace("{q}", t).replace("{d}", t) for t in texts]
        out: list[list[float]] = []
        bs = self.spec.batch_size
        self.check_budget(self.estimate_cost(n_text_tokens=estimate_tokens(prepared)))
        for i in range(0, len(prepared), bs):
            vecs = self._request(prepared[i : i + bs])
            out.extend(truncate_normalize(v, self.spec.dim) for v in vecs)
        return out

    def embed_images(self, paths: Sequence[Path], captions: Sequence[str] | None = None) -> list[list[float]]:
        """Embed PNG images (optionally joined with caption text) as data URLs."""
        if not self.spec.supports_image:
            raise EmbeddingError(f"{self.spec.key} does not support images")
        self.check_budget(self.estimate_cost(n_images=len(paths)))
        out: list[list[float]] = []
        bs = max(1, min(self.spec.batch_size, 4))
        for i in range(0, len(paths), bs):
            batch = []
            for j, p in enumerate(paths[i : i + bs]):
                content: list[dict] = []
                if captions and captions[i + j]:
                    content.append({"type": "text", "text": captions[i + j]})
                content.append({"type": "image_url", "image_url": {"url": png_to_data_url(p)}})
                batch.append({"content": content})
            vecs = self._request(batch)
            out.extend(truncate_normalize(v, self.spec.dim) for v in vecs)
        return out


def get_embedder(key: str) -> Embedder:
    """Factory for the model with the given registry key."""
    return Embedder(MODELS[key])
