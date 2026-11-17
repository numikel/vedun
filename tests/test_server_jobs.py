"""Background jobs and the upload endpoint. No network (source calls are faked)."""

from __future__ import annotations

import threading
import time

import pytest

from tests.helpers_server import FakeSource, fake_catalog, use_source, wait_job
from vedun import config as C
from vedun.server import audit, jobs


def test_job_contract_for_the_dashboard(client):
    assert {
        "id",
        "kind",
        "params",
        "status",
        "progress",
        "message",
        "log_tail",
        "created_at",
        "started_at",
        "finished_at",
        "result",
        "error",
    } <= set(jobs.JobInfo.__dataclass_fields__)
    with pytest.raises(ValueError):
        jobs.submit_job("explode", {})
    with pytest.raises(KeyError):
        jobs.get_job("nope")


def test_catalog_refresh_job_fills_source_item_id(client, monkeypatch):
    use_source(monkeypatch, FakeSource())
    jid = client.post("/api/jobs/catalog-refresh").json()["job_id"]
    j = wait_job(client, jid)
    assert j["status"] == "done", j
    assert j["result"]["n_entries"] == 6 and j["result"]["source_item_ids_filled"] == 1
    assert j["result"]["counts"] == {"current": 1, "missing_locally": 5}
    assert j["progress"] == 1.0 and j["started_at"] and j["finished_at"]
    ev = client.get("/api/lookup", params={"doc_id": "9-9", "ref": "2.3.2.4"}).json()["items"][0]["evidence"]
    assert ev["source_item_id"] and "source_item_id" not in ev["null_reasons"]
    assert jid in [x["id"] for x in client.get("/api/jobs").json()["jobs"]]


def test_catalog_refresh_failure_is_recorded_not_raised(client, monkeypatch):
    def boom():
        raise TimeoutError("source unreachable")

    use_source(monkeypatch, FakeSource(catalog=boom))
    j = wait_job(client, client.post("/api/jobs/catalog-refresh").json()["job_id"])
    assert j["status"] == "failed" and "unreachable" in j["error"] and j["finished_at"]
    assert jobs.worker_alive()  # the worker survived


def test_check_citations_job_and_latest(client, server_kb):
    assert client.get("/api/citations/latest").status_code == 404
    sub = server_kb.repo / "notes"
    sub.mkdir()
    (sub / "a.md").write_text("Wg ASC 9-9 pkt 2.3.2.4 oraz ASC 9-9 pkt 5.5.5.\n", encoding="utf-8")
    (server_kb.repo / "b.txt").write_text("ASC 1-1 pkt 2.2.2\n", encoding="utf-8")
    jid = client.post("/api/jobs/check-citations", json={"repo": "notes"}).json()["job_id"]
    j = wait_job(client, jid)
    assert j["status"] == "done" and j["result"]["counts"] == {"OK": 1, "UNKNOWN UNIT": 1, "DOCUMENT NOT INDEXED": 0}
    latest = client.get("/api/citations/latest").json()
    assert {r["status"] for r in latest["results"]} == {"OK", "UNKNOWN UNIT"} and latest["results"][0]["file"].endswith(
        "a.md"
    )
    jid = client.post("/api/jobs/check-citations").json()["job_id"]  # whole repo folder
    assert wait_job(client, jid)["result"]["n_citations"] == 3


def test_check_citations_refuses_paths_outside_the_repo(client, server_kb):
    for bad in (str(server_kb.root), "..", "../..", "/etc"):
        r = client.post("/api/jobs/check-citations", json={"repo": bad})
        assert r.status_code == 400
        assert "inside" in r.json()["error"]["message"] or "not a directory" in r.json()["error"]["message"]


def test_sync_noop_changes_nothing(client, server_kb):
    """The folder holds exactly the indexed PDF: the job plans 'unchanged' and touches nothing."""
    j = wait_job(client, client.post("/api/jobs/sync", data={"dry_run": "true"}).json()["job_id"])
    assert j["status"] == "done" and j["result"]["plan"]["unchanged"] == ["9-9"] and not j["result"]["plan"]["replace"]
    j = wait_job(client, client.post("/api/jobs/sync").json()["job_id"])
    assert j["status"] == "done" and j["result"]["results"] == [] and j["result"]["old_pdfs_deleted"] == {}
    assert j["cost_usd"] == 0 and (server_kb.pdf_dir / "9-9_202610.pdf").exists()


def test_sync_scope_never_grows_beyond_indexed_sheets(client, server_kb):
    (server_kb.pdf_dir / "1-1_202601.pdf").write_bytes(b"%PDF-1.7 other sheet")  # on disk, not indexed
    j = wait_job(client, client.post("/api/jobs/sync", data={"dry_run": "true"}).json()["job_id"])
    assert j["result"]["scope"] == ["9-9"] and j["result"]["plan"]["add"] == []


def test_first_sync_of_an_empty_knowledge_base_takes_the_whole_folder(client, server_kb, tmp_path, monkeypatch):
    """A new install: PDFs copied into the folder are indexed by the first sync, also when one file is uploaded."""
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "empty" / "vedun.db")
    (server_kb.pdf_dir / "1-1_202601.pdf").write_bytes(b"%PDF-1.7 on disk")
    j = wait_job(client, client.post("/api/jobs/sync", data={"dry_run": "true"}).json()["job_id"])
    assert j["result"]["scope"] == ["1-1", "9-9"] and j["result"]["plan"]["add"] == ["1-1", "9-9"]
    up = [("files", ("2-2_202601.pdf", b"%PDF-1.7 uploaded", "application/pdf"))]
    j = wait_job(client, client.post("/api/jobs/sync", data={"dry_run": "true"}, files=up).json()["job_id"])
    assert j["result"]["scope"] == ["1-1", "2-2", "9-9"]


def test_upload_validation(client, server_kb):
    pdf = ("1-1_202601.pdf", b"%PDF-1.7 x", "application/pdf")
    assert (
        client.post("/api/jobs/sync", files=[("files", ("evil.pdf", b"%PDF-1", "application/pdf"))]).status_code == 400
    )
    assert (
        client.post("/api/jobs/sync", files=[("files", ("..\\1-1_202601.pdf", b"x", "application/pdf"))]).status_code
        == 400
    )
    assert (
        client.post("/api/jobs/sync", files=[("files", ("1-1_202601.pdf", b"<html>", "application/pdf"))]).status_code
        == 400
    )
    assert client.post("/api/jobs/sync", files=[("files", pdf), ("files", pdf)]).status_code == 400  # duplicate names
    assert client.post("/api/jobs/sync", data={"only": "3-26; ../x"}).status_code == 400
    assert not list((server_kb.pdf_dir / ".incoming").glob("*.pdf"))  # nothing left staged after the refusals


def test_upload_size_cap(client, server_kb, monkeypatch):
    monkeypatch.setenv("VEDUN_MAX_UPLOAD_MB", "0.001")  # 1 kB
    r = client.post(
        "/api/jobs/sync", files=[("files", ("1-1_202601.pdf", b"%PDF-1.7 " + b"x" * 5000, "application/pdf"))]
    )
    assert r.status_code == 413 and not list((server_kb.pdf_dir / ".incoming").glob("*.pdf"))


def test_sync_failure_quarantines_the_new_pdf_and_keeps_the_old_one(client, server_kb, monkeypatch):
    """A new edition that cannot be converted must leave the indexed edition and its PDF untouched."""
    from vedun import db as D

    def fail(*a, **k):
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(D, "replace_document", fail)
    r = client.post("/api/jobs/sync", files=[("files", ("9-9_202701.pdf", b"%PDF-1.7 newer", "application/pdf"))])
    assert r.status_code == 202 and r.json()["uploaded"] == ["9-9_202701.pdf"]
    j = wait_job(client, r.json()["job_id"])
    assert j["status"] == "failed" and "exploded" in j["error"]
    assert (server_kb.pdf_dir / "9-9_202610.pdf").exists() and not (server_kb.pdf_dir / "9-9_202701.pdf").exists()
    assert (server_kb.pdf_dir / ".incoming" / "rejected" / "9-9_202701.pdf").exists()
    assert client.get("/api/lookup", params={"doc_id": "9-9", "ref": "2.3.2.4"}).json()["found"]


def test_old_pdf_is_retired_only_after_the_new_edition_is_indexed(client, server_kb, monkeypatch):
    from tests.helpers import BODY_V2, fake_converter, fake_embedders
    from vedun import db as D

    real = D.replace_document
    emb = fake_embedders(["i1"])
    monkeypatch.setattr(
        D,
        "replace_document",
        lambda conn, pdf, **kw: real(
            conn, pdf, **{**kw, "embedders": emb, "converter": fake_converter({"2027-01": BODY_V2})}
        ),
    )
    r = client.post("/api/jobs/sync", files=[("files", ("9-9_202701.pdf", b"%PDF-1.7 newer", "application/pdf"))])
    j = wait_job(client, r.json()["job_id"])
    assert j["status"] == "done", j
    assert j["result"]["old_pdfs_deleted"] == {"9-9": ["9-9_202610.pdf"]}
    assert [p.name for p in server_kb.pdf_dir.glob("*.pdf")] == ["9-9_202701.pdf"]
    assert client.get("/api/collection/9-9").json()["indexed_edition"] == "2027-01"
    assert j["result"]["results"][0]["action"] == "replaced" and j["result"]["report"].startswith("sync_")


def test_interrupted_jobs_are_failed_on_restart(client):
    from vedun.state import connect_state

    c = connect_state()
    c.execute(
        "INSERT INTO jobs(id, kind, params, status, message, created_at) VALUES('aaaaaaaaaaaa','sync','{}','running','x','2026-01-01T00:00:00+00:00')"
    )
    c.close()
    jobs.stop()
    jobs.start()
    j = jobs.get_job("aaaaaaaaaaaa")
    assert j.status == "failed" and "restart" in j.error


# ------------------------------------------------------------------ cancellation
FINAL = ("done", "failed", "cancelled")


def wait_final(client, job_id: str, timeout: float = 60.0) -> dict:
    """Like ``wait_job`` but also stops on ``cancelled``."""
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in FINAL:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still {j['status']} after {timeout}s")


class Gate:
    """Lets a test hold the worker thread inside a job at a chosen point (deterministic, no sleeps)."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def hit(self) -> None:
        self.reached.set()
        assert self.release.wait(30), "the test never released the worker"

    def open(self) -> None:
        self.release.set()


@pytest.fixture()
def gated_sheets(monkeypatch):
    """``D.replace_document`` with fakes; after the FIRST sheet it parks the worker on a gate."""
    from tests.helpers import BODY_V1, BODY_V2, fake_converter, fake_embedders
    from vedun import db as D

    gate = Gate()
    real = D.replace_document
    emb = fake_embedders(["i1"])
    conv = fake_converter({"2026-10": BODY_V1, "2027-01": BODY_V2})
    done: list[str] = []

    def wrapped(conn, pdf, **kw):
        r = real(conn, pdf, **{**kw, "embedders": emb, "converter": conv})
        done.append(r.doc_id)
        if len(done) == 1:
            gate.hit()
        return r

    monkeypatch.setattr(D, "replace_document", wrapped)
    gate.done = done
    return gate


def _pdfs(server_kb, *names):
    for n in names:
        (server_kb.pdf_dir / n).write_bytes(b"%PDF-1.7 " + n.encode())


def test_cancel_running_sync_stops_between_sheets(client, server_kb, gated_sheets):
    _pdfs(server_kb, "9-7_202610.pdf", "9-8_202610.pdf")
    jid = client.post("/api/jobs/sync", data={"only": "9-7 9-8"}).json()["job_id"]
    assert gated_sheets.reached.wait(30)  # 9-7 is committed, 9-8 not started
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 202 and r.json()["status"] == "running" and r.json()["cancel_requested"] is True
    assert client.get(f"/api/jobs/{jid}").json()["cancel_requested"] is True
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 202  # asking again is harmless
    gated_sheets.open()
    j = wait_final(client, jid)
    assert j["status"] == "cancelled" and j["error"] is None and j["cancel_requested"] is False
    assert j["result"]["cancelled"] is True and j["result"]["skipped"] == ["9-8"]
    assert [x["doc_id"] for x in j["result"]["results"]] == ["9-7"]
    assert j["message"].startswith("Cancelled: added 1") and "skipped 1 (9-8)" in j["message"]
    assert j["finished_at"] and j["progress"] < 1.0
    assert j["cost_usd"] is not None and j["api_calls"] is not None  # cost of the finished part is still recorded
    assert client.get("/api/collection/9-7").json()["indexed_edition"] == "2026-10"  # finished work stays
    assert client.get("/api/collection/9-8").json().get("indexed_edition") is None  # skipped sheet untouched
    mine = [(e["action"], e["outcome"], e["caller"]) for e in audit.read_tail(100) if e.get("job_id") == jid]
    assert ("job_cancel", "requested", "api") in mine and ("sync", "cancelled", "api") in mine


def test_cancel_queued_job_is_immediate_and_it_never_runs(client, server_kb, gated_sheets):
    _pdfs(server_kb, "9-7_202610.pdf")
    first = client.post("/api/jobs/sync", data={"only": "9-7"}).json()["job_id"]
    assert gated_sheets.reached.wait(30)  # the worker is busy with the first job
    staged = server_kb.pdf_dir / ".incoming" / "1-1_202601.pdf"
    queued = client.post(
        "/api/jobs/sync", files=[("files", ("1-1_202601.pdf", b"%PDF-1.7 staged", "application/pdf"))]
    ).json()["job_id"]
    assert staged.exists() and client.get(f"/api/jobs/{queued}").json()["status"] == "queued"
    r = client.post(f"/api/jobs/{queued}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["message"] == "Anulowane w kolejce"
    assert not staged.exists()  # the upload of a job that will never run is not left behind
    gated_sheets.open()
    assert wait_final(client, first)["status"] == "done"
    j = client.get(f"/api/jobs/{queued}").json()
    assert j["status"] == "cancelled" and j["started_at"] is None and j["cost_usd"] == 0  # the worker skipped it
    assert not (server_kb.pdf_dir / "1-1_202601.pdf").exists()
    assert any(e["action"] == "job_cancel" and e["outcome"] == "cancelled" for e in audit.read_tail(50))


def test_cancel_refusals(client, server_kb, monkeypatch):
    assert client.post("/api/jobs/aaaaaaaaaaaa/cancel").status_code == 404
    assert client.post("/api/jobs/not-a-job-id/cancel").status_code == 422
    done = wait_final(client, client.post("/api/jobs/sync", data={"dry_run": "true"}).json()["job_id"])
    r = client.post(f"/api/jobs/{done['id']}/cancel")
    assert r.status_code == 409 and "already done" in r.json()["error"]["message"]
    # a running catalog refresh is one HTTP request: no safe point, so it is refused instead of pretending
    gate = Gate()

    def slow():
        gate.hit()
        return fake_catalog("9-9")

    use_source(monkeypatch, FakeSource(catalog=slow))
    jid = client.post("/api/jobs/catalog-refresh").json()["job_id"]
    assert gate.reached.wait(30)
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 409 and "cannot be interrupted" in r.json()["error"]["message"]
    gate.open()
    assert wait_final(client, jid)["status"] == "done"


def test_cancel_sync_keeps_a_new_pdf_whose_sheet_was_skipped(client, server_kb, gated_sheets):
    """9-9 is indexed in edition 2026-10; a new edition was uploaded but the cancel hits before its turn."""
    _pdfs(server_kb, "9-7_202610.pdf")  # processed first (plan.add precedes plan.replace)
    r = client.post(
        "/api/jobs/sync",
        data={"only": "9-7 9-9"},
        files=[("files", ("9-9_202701.pdf", b"%PDF-1.7 newer", "application/pdf"))],
    )
    jid = r.json()["job_id"]
    assert gated_sheets.reached.wait(30)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 202
    gated_sheets.open()
    j = wait_final(client, jid)
    res = j["result"]
    assert j["status"] == "cancelled" and res["skipped"] == ["9-9"] and [x["doc_id"] for x in res["results"]] == ["9-7"]
    # neither quarantined as "rejected" nor allowed to retire the old edition
    assert (
        res["pdfs_rejected"] == []
        and res["old_pdfs_deleted"] == {}
        and res["pdfs_pending"] == {"9-9": "9-9_202701.pdf"}
    )
    assert (server_kb.pdf_dir / "9-9_202701.pdf").exists() and (server_kb.pdf_dir / "9-9_202610.pdf").exists()
    assert not (server_kb.pdf_dir / ".incoming" / "rejected").exists()
    assert client.get("/api/collection/9-9").json()["indexed_edition"] == "2026-10"  # old edition still served
    assert client.get("/api/lookup", params={"doc_id": "9-9", "ref": "2.3.2.4"}).json()["found"]
    assert any("run sync again" in n for n in res["notes"])
    # the next sync indexes the newer edition (the folder keeps both PDFs, plan_sync takes the newest)
    nxt = wait_final(client, client.post("/api/jobs/sync", data={"only": "9-9"}).json()["job_id"])
    assert nxt["status"] == "done" and nxt["result"]["results"][0]["action"] == "replaced"
    assert client.get("/api/collection/9-9").json()["indexed_edition"] == "2027-01"


def test_cancel_before_the_first_sheet_skips_everything_and_keeps_the_upload(client, server_kb, monkeypatch):
    """Cancel lands while the job is still before D.sync (here: parked in the plan step), so every sheet is skipped."""
    from vedun import db as D

    gate = Gate()
    real_plan = D.plan_sync

    def parked(*a, **k):
        gate.hit()  # first call is the job's own planning, before any sheet work
        return real_plan(*a, **k)

    monkeypatch.setattr(D, "plan_sync", parked)
    r = client.post("/api/jobs/sync", files=[("files", ("9-9_202701.pdf", b"%PDF-1.7 newer", "application/pdf"))])
    jid = r.json()["job_id"]
    assert gate.reached.wait(30)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 202
    monkeypatch.setattr(D, "plan_sync", real_plan)
    gate.open()
    j = wait_final(client, jid)
    assert j["status"] == "cancelled" and j["result"]["skipped"] == ["9-9"] and j["result"]["results"] == []
    assert (server_kb.pdf_dir / "9-9_202701.pdf").exists() and j["result"]["pdfs_pending"] == {"9-9": "9-9_202701.pdf"}


def test_cancel_check_citations_stores_no_partial_results(client, server_kb, monkeypatch):
    for n in ("a.md", "b.md"):
        (server_kb.repo / n).write_text("Wg ASC 9-9 pkt 2.3.2.4.\n", encoding="utf-8")
    gate = Gate()
    real = jobs.check_files
    calls: list[int] = []

    def parked(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        if len(calls) == 1:
            gate.hit()
        return out

    monkeypatch.setattr(jobs, "check_files", parked)
    jid = client.post("/api/jobs/check-citations").json()["job_id"]
    assert gate.reached.wait(30)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 202
    gate.open()
    j = wait_final(client, jid)
    assert j["status"] == "cancelled" and "results were not saved" in j["message"]
    assert client.get("/api/citations/latest").status_code == 404  # a partial scan never becomes "the latest"


def test_cancel_after_the_only_processed_sheet_failed_is_still_cancelled(client, server_kb, monkeypatch):
    """Without a cancel "every processed sheet failed" fails the job; with one the user's cancel is the outcome."""
    from vedun import db as D

    gate = Gate()
    calls: list[str] = []

    def failing(conn, pdf, **kw):
        calls.append(pdf.name)
        gate.hit()
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(D, "replace_document", failing)
    _pdfs(server_kb, "9-7_202610.pdf", "9-8_202610.pdf")
    jid = client.post("/api/jobs/sync", data={"only": "9-7 9-8"}).json()["job_id"]
    assert gate.reached.wait(30)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 202
    gate.open()
    j = wait_final(client, jid)
    assert j["status"] == "cancelled" and list(j["result"]["failed"]) == ["9-7"] and j["result"]["skipped"] == ["9-8"]
    assert calls == ["9-7_202610.pdf"]  # the second sheet was never attempted
