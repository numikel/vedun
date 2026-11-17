"""Fixtures for the HTTP service tests: a throw-away knowledge base with one synthetic document (9-9), fake
embeddings, isolated state / report / PDF folders. No network, no real keys."""

from __future__ import annotations

import base64
import copy
import time
from pathlib import Path

import pytest

from tests.helpers import BODY_V1, fake_converter, fake_embedders, make_pdf
from vedun import catalog as K
from vedun import config as C
from vedun import db as D

API_KEY = "test-api-key-not-real"
DASH_USER = "tester"
DASH_PASSWORD = "p@ss:word-not-real"  # a colon on purpose: only the first one separates user and password
BASIC = {"Authorization": "Basic " + base64.b64encode(f"{DASH_USER}:{DASH_PASSWORD}".encode()).decode()}
BEARER = {"Authorization": f"Bearer {API_KEY}"}
CSRF = {"X-Requested-With": "vedun"}


_ENTRIES = [  # (id, title, published): six documents of a remote catalog
    ("22-6", "Gate Inspection and Upkeep", "April 2026"),
    ("21-4", "Tool Storage in Workshops", "October 2026"),
    ("24-7", "Emergency Lighting Circuits", "April 2025"),
    ("25-3", "Plant Rooms and Adjoining Spaces", "July 2026"),
    ("26-0", "Ladders and Access Platforms", "January 2001"),
    ("27-11I", "Scaffold Board Treatments", "January 2022"),
]


def fake_catalog(as_ds: str = "9-9") -> K.Catalog:
    """Six entries; 21-4 (October 2026) renamed to ``as_ds`` so that it matches the test DB's 9-9 2026-10."""
    cat = K.Catalog()
    for i, (ds, title, published) in enumerate(_ENTRIES):
        ds = as_ds if ds == "21-4" else ds
        item = f"{i:08X}-0000-4000-8000-000000000000"
        cat.entries[ds] = K.CatalogEntry(ds, title, item, published, K.edition_from_published(published))
    return cat


class FakeSource:
    """A source plugin without network: ``fetch`` returns a copy of ``catalog`` (or calls it when callable),
    ``download`` writes ``pdf`` (or the next item of a list) and records the entries asked for."""

    name = "test-source"

    def __init__(self, catalog=None, pdf: bytes | list[bytes] = b"%PDF-1.7 fake"):
        self.catalog = catalog if catalog is not None else fake_catalog()
        self.pdf = pdf
        self.downloads: list[str] = []

    def fetch(self) -> K.Catalog:
        if callable(self.catalog):
            return self.catalog()
        return copy.deepcopy(self.catalog)

    def download(self, entry: K.CatalogEntry, dest: Path) -> int:
        self.downloads.append(entry.doc_id)
        data = self.pdf if isinstance(self.pdf, bytes) else self.pdf[len(self.downloads) - 1]
        dest.write_bytes(data)
        return len(data)


def use_source(monkeypatch, src) -> None:
    """Make ``src`` the configured source plugin for the code under test."""
    monkeypatch.setattr(K, "load_source", lambda: src)


@pytest.fixture()
def server_kb(tmp_path, monkeypatch):
    """Isolated environment; yields a namespace with the tmp paths."""
    pdf_dir = tmp_path / "pdf"
    repo = tmp_path / "repo"
    pdf_dir.mkdir()
    repo.mkdir()
    for k in ("TYPESAFE_API_KEY",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("VEDUN_REPO_DIR", str(repo))
    monkeypatch.setenv("VEDUN_API_KEY", API_KEY)
    monkeypatch.setenv("VEDUN_DASH_USER", DASH_USER)
    monkeypatch.setenv("VEDUN_DASH_PASSWORD", DASH_PASSWORD)
    monkeypatch.setenv("VEDUN_RATE_LIMIT_PER_MIN", "10000")
    monkeypatch.delenv("VEDUN_DAILY_BUDGET_USD", raising=False)
    monkeypatch.delenv("VEDUN_HOST_PDF_DIR", raising=False)
    monkeypatch.delenv("VEDUN_HOST_ROOT", raising=False)
    monkeypatch.delenv("VEDUN_HOST_MD_DIR", raising=False)
    for name, val in {
        "DB_PATH": tmp_path / "t.db",
        "MD_DIR": tmp_path / "md",
        "FIG_DIR": tmp_path / "figures",
        "REPORT_DIR": tmp_path / "reports",
        "DEFAULT_PDF_DIR": pdf_dir,
        "STATE_DIR": tmp_path / "state",
    }.items():
        monkeypatch.setattr(C, name, val)
    pdf = make_pdf(pdf_dir / "9-9_202610.pdf", [(50, 80, "2.3.2.4 Limit paint stock to 45 kg (99 lb) per cabinet.")])
    conn = D.connect(C.DB_PATH)
    D.replace_document(
        conn,
        pdf,
        text_models=["i1"],
        image_models=["i1"],
        md_dir=C.MD_DIR,
        fig_dir=C.FIG_DIR,
        embedders=fake_embedders(["i1"]),
        converter=fake_converter({"2026-10": BODY_V1}),
    )
    conn.close()

    class Env:
        pass

    env = Env()
    env.root, env.pdf_dir, env.repo = tmp_path, pdf_dir, repo
    return env


@pytest.fixture()
def raw_client(server_kb, monkeypatch):
    """Client without credentials (the real app, real middleware)."""
    from fastapi.testclient import TestClient

    from vedun.server import api
    from vedun.server.app import create_app

    emb = fake_embedders(["i1"])
    monkeypatch.setattr(api.hooks, "embedder_factory", lambda k: emb[k])
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def client(raw_client):
    """API client: Bearer key + the cross-site guard header on every request."""
    raw_client.headers.update({**BEARER, **CSRF})
    return raw_client


def wait_job(client, job_id: str, timeout: float = 60.0) -> dict:
    """Poll ``/api/jobs/{id}`` until the job is done or failed."""
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} still {j['status']} after {timeout}s")
