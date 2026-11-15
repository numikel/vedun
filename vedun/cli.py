"""Command line: ``python -m vedun <command>`` (see README for the full list)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as C
from . import db as D
from . import search as S
from .check_citations import check_files, format_table, iter_text_files
from .quote import verify_quote


def _models(arg: str | None, env: str, default: list[str]) -> list[str]:
    raw = arg or C.env(env)
    keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else list(default)
    bad = [k for k in keys if k not in C.MODELS]
    if bad:
        raise SystemExit(f"unknown model key(s) {bad}; known: {sorted(C.MODELS)}")
    return keys


def _dump(obj: object) -> None:
    print(json.dumps(obj.to_dict() if hasattr(obj, "to_dict") else obj, indent=2, ensure_ascii=False))


def _show(e, full: bool = False) -> None:  # noqa: ANN001
    r = e.retrieval
    print(f"{e.citation_en}   [{e.unit_kind}, {r.match_type}/{r.final_ranker}, rank {r.final_rank}]")
    if e.snippet:
        print("   ", e.snippet)
    elif full:
        print(e.text)
    else:
        print("   ", e.text[:240].replace("\n", " "))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vedun", description=f"vedun: {C.CORPUS_NAME}")
    p.add_argument("--db", type=Path, default=None, help="SQLite file (default: env VEDUN_DB or <root>/vedun.db)")
    p.add_argument("--json", action="store_true", help="print JSON (Evidence records) instead of text")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_sync_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--pdf-dir", type=Path, default=C.DEFAULT_PDF_DIR)
        sp.add_argument("--only", nargs="*", help="limit to these document ids, e.g. 3-26 2-81")
        sp.add_argument(
            "--text-models", help="comma list of model keys for text chunks (default: config.TEXT_MODEL_KEYS = i1)"
        )
        sp.add_argument("--image-models", help="comma list of model keys for figures (default i1)")
        sp.add_argument("--no-embed", action="store_true", help="convert + index only; no API calls")
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--force", action="store_true", help="re-index even if unchanged (reuses the conversion)")

    add_sync_args(sub.add_parser("sync", help="bring the DB in line with the PDF folder"))
    add_sync_args(sub.add_parser("build", help="alias of sync (use with --only)"))

    sp = sub.add_parser("lookup", help="exact lookup by document + unit id(s), forgiving input")
    sp.add_argument("doc_id")
    sp.add_argument("refs", nargs="+")
    sp.add_argument("--children", action="store_true")
    sp.add_argument("--neighbors", action="store_true")
    sp = sub.add_parser("section", help="clause with children as one text")
    sp.add_argument("doc_id")
    sp.add_argument("ref")
    sp.add_argument("--no-children", action="store_true")
    sp.add_argument("--max-chars", type=int, default=20000)
    sp = sub.add_parser("toc", help="structure of a document or branch")
    sp.add_argument("doc_id")
    sp.add_argument("prefix", nargs="?")
    sp.add_argument("--depth", type=int)
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--offset", type=int, default=0)
    sp = sub.add_parser("fulltext", help="FTS5/BM25 only (no vectors, no API)")
    sp.add_argument("query")
    sp.add_argument("--doc-id", nargs="*")
    sp.add_argument("--kind", nargs="*")
    sp.add_argument("--syntax", choices=["safe", "fts5"], default="safe")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--offset", type=int, default=0)
    sp = sub.add_parser("search", help="blind/semantic: weighted RRF (+ reranker)")
    sp.add_argument("query")
    sp.add_argument("--doc-id", nargs="*")
    sp.add_argument("--kind", nargs="*")
    sp.add_argument("--w-fts", type=float)
    sp.add_argument("--w-vec", type=float)
    sp.add_argument("--rerank", default="default", help="r1 | r2 | r3 | off")
    sp.add_argument("--mode", choices=["hybrid", "bm25", "vector"], default="hybrid")
    sp.add_argument("--model", default=C.DEFAULT_TEXT_MODEL)
    sp.add_argument("--no-figures", action="store_true")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--offset", type=int, default=0)
    sp = sub.add_parser("verify", help="verify a quote against the unit and the PDF text layer")
    sp.add_argument("doc_id")
    sp.add_argument("ref")
    sp.add_argument("quote")
    sp = sub.add_parser("documents", help="list indexed documents")
    sp.add_argument("--filter")
    sp = sub.add_parser("check", help="validate citations in files")
    sp.add_argument("files", nargs="+", type=Path)
    sp = sub.add_parser("check-citations", help="validate citations in a whole folder (*.md, *.txt)")
    sp.add_argument("--repo", type=Path, required=True)
    sp.add_argument("--problems-only", action="store_true")
    sub.add_parser("doctor", help="DB integrity check (exit 1 on problems)")
    sub.add_parser("migrate", help="bring an older DB file to the current schema (stop the server first)")
    sp = sub.add_parser("remove", help="remove a document completely")
    sp.add_argument("doc_id")
    sp = sub.add_parser("figure", help="PNG of a figure")
    sp.add_argument("doc_id")
    sp.add_argument("figure_id")
    sp = sub.add_parser("page", help="PNG of a page (rendered on demand)")
    sp.add_argument("doc_id")
    sp.add_argument("page", type=int)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    args = build_parser().parse_args(argv)
    conn = D.connect(args.db)  # migrates an older file on open
    cmd, as_json = args.cmd, args.json
    if cmd == "migrate":
        print("schema:", conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        return 0
    if cmd in ("sync", "build"):
        tm = _models(args.text_models, "TEXT_MODELS", C.TEXT_MODEL_KEYS)
        im = _models(args.image_models, "IMAGE_MODELS", C.IMAGE_MODEL_KEYS)
        res = D.sync(
            conn,
            args.pdf_dir,
            only=args.only,
            text_models=tm,
            image_models=im,
            embed=not args.no_embed,
            dry_run=args.dry_run,
            force=args.force,
            log=print,
        )
        p = res.plan
        print(f"plan: add {p.add} replace {p.replace} unchanged {len(p.unchanged)} remove {p.remove}")
        for w in p.warnings:
            print("WARNING:", w)
        for r in res.results:
            ms = ", ".join(f"{m.model_key}/{m.kind}:{m.status} {m.n_ok}/{m.n_expected}" for m in r.models)
            print(
                f"{r.action} {r.doc_id} {r.old_edition}->{r.new_edition}: {r.n_units} units, "
                f"{r.n_chunks} chunks, {r.n_figures} figures, {r.convert_seconds:.0f}s | {ms}"
            )
        for ds, outs in res.healed.items():
            print(f"healed {ds}: " + ", ".join(f"{m.model_key}/{m.kind}:{m.status}" for m in outs))
        for ds, err in res.failed.items():
            print(f"FAILED {ds} (previous state kept): {err}")
        if res.report_path:
            print("report:", res.report_path)
        from .embed import STATS, Embedder

        for k, st in STATS.items():
            if st.calls:
                print(f"api {k} ({C.MODELS[k].api_id}):", st.summary())
        print(f"sync took {res.seconds:.1f}s; logged API cost USD {Embedder._shared_cost:.5f}")
        return 0
    if cmd == "lookup":
        r = S.lookup(
            conn, args.doc_id, args.refs, include_children=args.children, neighbors=args.neighbors, with_images=True
        )
        if as_json:
            _dump(r)
        for it in r.items:
            if as_json:
                continue
            if it.found and it.evidence:
                _show(it.evidence, full=True)
                if it.message:
                    print("NOTE:", it.message)
                for c in it.children:
                    print(f"  + {c.unit_id}: {c.text[:120]!r}")
                if args.neighbors:
                    print(f"  prev: {it.prev_id}  next: {it.next_id}")
            else:
                print(f"NOT FOUND {it.requested!r}: {it.message}")
                if it.possibly_renumbered_to:
                    print("  possibly renumbered to:", ", ".join(it.possibly_renumbered_to))
                if it.nearest:
                    print("  nearest ids:", ", ".join(it.nearest))
        return 0 if r.found else 1
    if cmd == "section":
        r = S.get_section(conn, args.doc_id, args.ref, not args.no_children, args.max_chars)
        if as_json:
            _dump(r)
        else:
            print(r.text + ("\n[truncated]" if r.truncated else "") if r.found else "NOT FOUND: " + r.message)
        return 0 if r.found else 1
    if cmd == "toc":
        r = S.toc(conn, args.doc_id, args.prefix, args.depth, args.limit, args.offset)
        if as_json:
            _dump(r)
        else:
            print(r.message or f"{C.doc_label(r.doc_id)} {r.document_title} ({r.edition_label}) - {r.total} units")
            for i in r.items:
                print(f"{'  ' * i.depth}{i.unit_id}  [{i.kind}, {i.n_children} sub]  {i.title[:80]}")
        return 0 if r.found else 1
    if cmd == "fulltext":
        r = S.fulltext(conn, args.query, args.doc_id, args.kind, args.limit, args.offset, args.syntax)
        if as_json:
            _dump(r)
        elif not r.ok:
            print("ERROR:", r.error)
        else:
            print(f"{r.total} matches for {r.match_expression}")
            for e in r.hits:
                _show(e)
        return 0 if r.ok else 1
    if cmd == "search":
        r = S.search(
            conn,
            args.query,
            args.doc_id,
            args.kind,
            w_fts=args.w_fts,
            w_vec=args.w_vec,
            rerank=None if args.rerank == "off" else args.rerank,
            include_figures=not args.no_figures,
            limit=args.limit,
            offset=args.offset,
            mode=args.mode,
            model=args.model,
        )
        if as_json:
            _dump(r)
        else:
            for n in r.notes:
                print("NOTE:", n)
            for e in r.hits:
                _show(e)
        return 0
    if cmd == "verify":
        r = verify_quote(conn, args.doc_id, args.ref, args.quote)
        _dump(r) if as_json else print(
            ("MATCH" if r.match else "NO MATCH"),
            "-",
            r.message,
            *(["\nclosest:", r.closest, f"({r.closest_unit_id}, {r.closest_ratio})"] if r.closest else []),
        )
        return 0 if r.match else 1
    if cmd == "documents":
        documents = S.list_documents(conn, args.filter)
        if as_json:
            _dump({"documents": [s.to_dict() for s in documents]})
        else:
            for s in documents:
                print(
                    f"{C.doc_label(s.doc_id):8} {s.edition_label or s.edition:40} {s.pages:3}p {s.n_units:4}u {s.n_chunks:4}c {s.n_figures}f  {s.title}  {s.models}"
                )
        return 0
    if cmd in ("check", "check-citations"):
        if cmd == "check":
            files, base = args.files, None
        else:
            files, base = iter_text_files(args.repo), args.repo
        results = check_files(conn, files, base)
        print(format_table(results, only_problems=cmd == "check-citations" and args.problems_only))
        n = {s: sum(r.status == s for r in results) for s in ("OK", "UNKNOWN UNIT", "DOCUMENT NOT INDEXED")}
        print(f"\n{len(results)} citations in {len(files)} files: {n}")
        return 1 if n["UNKNOWN UNIT"] else 0
    if cmd == "doctor":
        problems = D.doctor(conn, pdf_dir=C.DEFAULT_PDF_DIR)
        for p in problems:
            print("PROBLEM:", p)
        print("doctor:", "OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if cmd == "remove":
        ok = D.remove_document(conn, args.doc_id)
        print("removed" if ok else "not indexed")
        return 0 if ok else 1
    if cmd == "figure":
        r = S.figure(conn, args.doc_id, args.figure_id)
        _dump(r) if as_json else print(r.path if r.ok else "NOT FOUND: " + r.message)
        return 0 if r.ok else 1
    if cmd == "page":
        r = S.page_image(conn, args.doc_id, args.page)
        _dump(r) if as_json else print(r.path if r.ok else "ERROR: " + r.message)
        return 0 if r.ok else 1
    return 2
