# vedun reference

Technical reference for developers and for people who run vedun without the installer. User-facing instructions are
in the [README](../README.md); installer flags in [`scripts/README.md`](../scripts/README.md).

Examples use an invented collection whose documents are numbered like `4-12` and whose clauses look like `2.3.1`.

## Concepts

- Identity of a document = its number `doc_id` (e.g. `4-12`), read from the file name `<doc_id>_<YYYYMM>.pdf`.
  **One edition per `doc_id`** at any time; `YYYYMM` is the edition.
- Identity of a unit = `doc_id` + `unit_id` (`2.3.1`, `Table 2.4`, `Figure 3.1`, `Appendix A`). Page numbers are
  internal locators only (rendering PNGs, opening the PDF); they are never part of keys or citations.
- Everything is local files plus one SQLite file (FTS5 + sqlite-vec). Only embeddings and the optional reranker call
  APIs (OpenRouter, Typesafe).

## Layout

```
<root>/                  project folder (VEDUN_ROOT; /data/kb in the container)
  vedun.db               the database (documents, units, chunks, FTS5, one vec0 table per model, renames)
  md/<doc_id>_<YYYYMM>.md     converted Markdown (front matter, <!-- page: N --> markers, real heading levels)
  figures/<doc_id>_<ed>/      figure crops (PNG) and on-demand page renders
  reports/               sync_<date>.md edition-change reports, audit.jsonl
  vedun.config.json      corpus profile (below)
  .state/ (.state-docker/ in the container)   jobs, catalog cache, budget ledger
vedun/                   Python package: convert, units, db, search, evidence, rerank, quote, check_citations, cli,
                         catalog, state, documents; server/ = REST API, jobs, MCP; web/ = dashboard
```

Paths are configurable through `VEDUN_ROOT`, `VEDUN_DB`, `VEDUN_MD_DIR`, `VEDUN_FIG_DIR`, `VEDUN_REPORT_DIR`,
`VEDUN_PDF_DIR` (default `<root>/pdf`), `VEDUN_STATE_DIR`, `VEDUN_CONFIG` and `VEDUN_REPO_DIR` (the folder
`check-citations` scans).

## Corpus profile

`<root>/vedun.config.json` (or `VEDUN_CONFIG`). `{"extends": "<path>"}` loads a base profile first (relative to the
file that names it); local keys override it. Example: [`vedun.config.example.json`](../vedun.config.example.json).

| Key | Meaning |
|---|---|
| `corpus_name` | name of the collection (dashboard subtitle, API title, MCP instructions) |
| `doc_id_pattern` | regex of a document number, no groups (default `\d{1,2}-\d{1,3}[A-Z]?`); also validates API input |
| `doc_label` | how a document is named in text, e.g. `"Std {doc_id}"`; part of every embedded chunk, so changing it means re-embedding |
| `citation_template`, `locators` | ready-to-paste citations per language (`{doc_id}`, `{title}`, `{edition}`, `{locator}`), validated at start |
| `citation_prefix_pattern` | regex of the words that may precede a document number in notes (for `check-citations`) |
| `edition_label` | which printed lines carry the original and revision dates (`{MONTH}` = "<Month> <YYYY>") |
| `title_strip_patterns`, `boilerplate_patterns` | text removed from titles / page furniture removed from the Markdown |
| `changes_unit_id` | the clause that lists changes between editions (quoted in sync reports) |
| `clause_ref_words`, `value_units` | words after which a number is a clause reference / a value, not a new clause |
| `fast_table_documents` | documents whose tables use Docling's FAST TableFormer (ACCURATE can take hours on huge numeric tables) |
| `w_fts`, `w_vec`, `rrf_k`, `rerank_candidates`, `reranker`, `rerank_instruction` | retrieval defaults (0.4 / 0.6 / 60 / 30 / none) |
| `text_models`, `image_models`, `default_text_model`, `default_image_model`, `models` | embedding models; `models` defines or overrides one on any OpenAI-compatible `/embeddings` endpoint: `{"<key>": {"api_id", "dim", "base_url", "api_key_env", "query_template", "price_text_per_m", …}}`, key `[a-z0-9_]{1,16}` |
| `source_plugin` | Python file that provides newer editions ([Writing a source plugin](#writing-a-source-plugin)) |
| `mcp_tool_prefix` | prefix of the MCP tool names (default `kb` → `kb_lookup`) |
| `citation_skip_dirs` | folders `check-citations` skips |
| `ui_language` | default dashboard language, `en` or `pl` (a visitor can switch; `?lang=` and a cookie win) |

## Install from source

The core (search an existing knowledge base, lookup, verify) needs only `sqlite-vec`, `pyyaml`, `pypdfium2`. Extras:
`convert` (Docling + torch, PDF → Markdown, ~3 GB), `server` (API, dashboard, MCP), `jev` (reranker), `all`.

- development: `uv sync` (the `dev` group pulls `vedun[all]`, pytest, ruff); `vedun` = `python -m vedun`
- search-only host: `uv sync --no-dev`; with the service: `uv sync --no-dev --extra server`
- the Docker image installs all extras

PDF text (titles, edition labels, `verify_quote`) is read with pypdfium2 (Apache-2.0/BSD); there is no AGPL dependency.

## Edition routine

1. Put the new PDF next to the old one (same `doc_id`, newer `YYYYMM`) or upload it in the dashboard.
2. `vedun sync` (dashboard **Sync**, or `python -m vedun sync --pdf-dir <dir>`): converts and embeds only new or changed
   documents, replaces the old edition in one transaction, removes documents whose PDF is gone; idempotent.
3. Read `reports/sync_<date>.md`: removed ids, new ids, same-id units whose text changed (short diff), probable
   renumbering (old id → new id by text similarity ≥ 0.75, weaker candidates 0.60–0.75) and the document's own changes
   section verbatim.
4. `check-citations` over your notes and fix what it reports (file:line, citation, status, suggestion).

`python -m vedun doctor` checks integrity (counts per document consistent across units, FTS5 and every vec0 table,
orphans, one edition per `doc_id`, md/figure folders matched to the DB); exit code 1 on problems.
On Windows, if `uv run <tool>` fails with "Failed to canonicalize script path", use `.venv\Scripts\python.exe -m …`.

## CLI

```
python -m vedun sync --pdf-dir <dir> [--only 4-12 ...] [--no-embed] [--dry-run]
python -m vedun lookup 4-12 "2.3.1" [--children] [--neighbors] [--json]
python -m vedun section 4-12 2.3
python -m vedun toc 4-12 2.3 --depth 2
python -m vedun fulltext '"permanently connected"' --syntax fts5
python -m vedun search "how often must fire doors be inspected" [--rerank r1|r2|r3|off] [--w-fts 0.6 --w-vec 0.4]
python -m vedun verify 4-12 2.3.1 "at least once a week"
python -m vedun documents | figure 4-12 "Figure 3.1" | page 4-12 21
python -m vedun check <files> | check-citations --repo <notes folder> | doctor | migrate | remove <doc_id>
```

Commands that call APIs (`sync` with embeddings, `search`) need `OPENROUTER_API_KEY` (and `TYPESAFE_API_KEY` for the
reranker) in the environment. `fulltext`, `lookup`, `toc`, `verify`, `doctor`, `check*` need no key.

## Library and MCP tools

All functions take an open connection (`vedun.db.connect()`), return typed dataclasses (`.to_dict()` is JSON-safe), and
every hit is an `Evidence` record. `limit` is capped at 50; `offset` (MCP: `cursor`) paginates.

| Function (MCP tool) | Use it when |
|---|---|
| `lookup` (`kb_lookup`) | document and clause/table/figure are known; forgiving input (`2.3.1`, `Section 2.3.1`, `Tab. 2.4`); unknown id → `found=False` + nearest ids (+ "possibly renumbered to" after a sync); never fuzzy |
| `toc` (`kb_toc`) | structure under a prefix: ids, kinds, headings, child counts, no text |
| `fulltext` (`kb_fulltext`) | exact phrase, number, boolean or proximity query; FTS5/BM25 only, no network; `syntax="safe"` (AND-ed terms, quoted phrases) or `"fts5"` (`"phrase"`, `prefix*`, `AND/OR/NOT`, `NEAR(a b, 5)`) |
| `search` (`kb_search`) | descriptive question in any language: exact-id shortcut, then weighted RRF `w_fts/(k+rank_fts) + w_vec/(k+rank_vec)`, figures merged, optional reranker on the top 30; without a reranker key the fused order is kept and `rerank_error` says why |
| `get_document` (`kb_get_document`) | a whole document or chapter: `meta` (title, edition, structure, host and container paths of PDF and Markdown), `md` (paged text, optionally one subtree), `pdf` (path and URL only, never bytes) |
| `figure`, `page_image` (`kb_figure`, `kb_page_image`) | PNG of a figure crop / a rendered page (110 dpi) with its Evidence |
| `verify_quote` (`kb_verify_quote`) | before a quote leaves: verbatim in that unit AND in the PDF text layer (whitespace, hyphenation and quote typography normalised; digits and letters exact); reports the unit that really contains it and the closest match |
| `list_documents` (`kb_documents`) | which documents and editions are indexed, and their catalog status |
| `check_citations` (`kb_check_citations`) | validate every citation in a text against the current editions: OK / UNKNOWN UNIT (nearest, renumbered) / DOCUMENT NOT INDEXED |

Admin functions (not MCP tools): `sync`, `replace_document`, `remove_document`, `doctor`.

### The Evidence record

- Document: `doc_id, document_title, edition, edition_label` (as printed), `edition_label_source, source_pdf, source_sha256,
  source_item_id, indexed_at`.
- Unit: `unit_id, unit_kind, title, parent_path, page_start/page_end` (locators only), `md_path, md_line_start/md_line_end`.
- Evidence: `text` (verbatim unit text; capped in search hits, full in lookup; `text_chars`, `text_truncated`),
  `snippet` (FTS5 `snippet()` with `**highlight**`), `figure_png`, `page_png`.
- Retrieval: `match_type` (exact|search|lookup|figure), `final_ranker`, `fusion, w_fts, w_vec, final_rank, bm25, bm25_rank,
  vector{model:{distance,similarity,rank}}, rrf, rerank{reranker:score}, rerank_error`.
- Citation: `citation_pl`, `citation_en` from the profile's `citation_template`, without page numbers.
- Host paths `source_pdf_host`, `md_path_host`, `figure_png_host` (mapped through `VEDUN_HOST_PDF_DIR` / `VEDUN_HOST_ROOT`
  when the service runs in a container), so an agent on the host can open the files directly.

Unknown fields are `null` and explained in `null_reasons`; nothing is omitted silently.

## Pipeline notes

- Conversion: Docling with OCR off (born-digital PDFs) and TableFormer ACCURATE. Clause numbers come from Docling list
  markers; headings are rebuilt to real Markdown levels from the numbering. Running headers/footers and the table of
  contents are stripped. Tables stay Markdown tables; tables split over pages are merged.
- Units: one per numbered clause; tables and figures are their own units; lettered items and bullets stay in their
  parent clause. When a source repeats an id, all copies are stored; `lookup` returns the first and lists the rest in
  `duplicates`.
- Chunks (for vectors): contextual prefix `<doc_label> <title> - <ancestor headings> - <id> <heading>`; long clauses are
  split at about 380 tokens with 50 tokens overlap; big tables are split in row groups with caption and header repeated.
- Embeddings: one vec0 table per model (`vec_<key>` text, `figvec_<key>` figure images); every row carries `doc_id` and
  `edition`. Default model `i1` = `google/gemini-embedding-2` (text and figures in one vector space).
- FTS5 tokenizer `porter unicode61 remove_diacritics 2`: English stemming, diacritics folded, dotted ids tokenised so
  `2.3.1` is a phrase query.

## Server

One FastAPI process serves the REST API (`/api`, OpenAPI at `/api/docs`), the dashboard (`/`, `/static`) and MCP
(`/mcp`). A missing optional module is logged and skipped; it never takes the API down.

```
# host, no container (secrets from the environment)
python -m uvicorn vedun.server.app:app --host 127.0.0.1 --port 8765
# container from this checkout: compose.yaml; end-user image: compose.release.yaml (what the installer uses)
docker compose up -d --build
```

### Security

- **Fail closed.** If `VEDUN_API_KEY`, `VEDUN_DASH_USER` or `VEDUN_DASH_PASSWORD` is unset, every protected route answers
  503 and the log shows one clear error at startup. Only `GET /api/health` is open (no data, no key names).
- **`/api/*`**: `Authorization: Bearer <VEDUN_API_KEY>` or `X-API-Key`, or HTTP Basic with the dashboard credentials.
  **Dashboard, `/static`, `/media`**: HTTP Basic. **`/mcp`**: the API key only. Comparisons are constant-time; secrets are
  never logged, echoed or written to the audit log.
- **In-process dashboard calls** carry a random per-process internal token (memory only).
- **Cross-site guard.** Every `POST` and the paid `GET /api/search` require `X-Requested-With: vedun` (or `HX-Request:
  true`), `Sec-Fetch-Site` absent/`same-origin`/`none`, and an allowed `Origin` if present (`VEDUN_ALLOWED_ORIGINS`).
  A wrong `Host` header gets 400 (`VEDUN_ALLOWED_HOSTS`, DNS rebinding).
- **Rate limit** 60 requests/min per credential on `/api` and `/mcp` (`VEDUN_RATE_LIMIT_PER_MIN`), then 429.
- **Daily budget** `VEDUN_DAILY_BUDGET_USD` (default 3.0) for embedding and rerank calls, per UTC day; when used up,
  `search` answers from BM25 only (`budget_exhausted: true`) and a sync that needs embeddings fails at once.
- **Audit log** `reports/audit.jsonl`: job submissions and results, downloads, authentication failures (throttled),
  refused cross-site requests, rate-limit hits, MCP tool calls (name and outcome). Never credentials, queries or content.
- **Container**: non-root (uid 10001 by default), `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem with a
  `/tmp` tmpfs, notes mount read-only, port published on `127.0.0.1` only.

### MCP server (`/mcp`)

- Protocol revision 2026-07-28 (stateless: `server/discover`, per-request `_meta`) with the official Python SDK `mcp` 2.x;
  the same endpoint also answers handshake-era clients (2025-11-25 and older) by their `MCP-Protocol-Version` header.
  Stateless HTTP, one JSON body per POST; `GET`/`DELETE` → 405.
- `server/discover` returns name, version, capabilities (`tools` only) and `instructions` (a tool-selection guide).
- All tools are `readOnlyHint: true`; `openWorldHint: true` only for `kb_search` (it calls paid APIs).
- Results carry `structuredContent` conforming to each tool's `outputSchema` and a self-sufficient text block; the two
  image tools return content blocks only.
- Size and paging: at most 20 000 characters of text and 40 000 of structured content per call; `next_cursor` continues
  (a cursor is bound to its tool and arguments).
- Errors: invalid input, a malformed FTS5 expression, a bad cursor, an unknown document/prefix/figure/page are tool
  errors (`isError: true`, the message says what to do next).
- Authorization: an API key (Bearer or `X-API-Key`). The server is single-user, bound to `127.0.0.1`, checks `Host` and
  `Origin`, rate-limits and audits; it forwards no token anywhere, so OAuth is deliberately not implemented. Revisit if
  the service is ever exposed beyond localhost or shared between people.

### REST API (`/api`, JSON, errors as `{"error": {"code", "message"}}`)

| Endpoint | Purpose |
|---|---|
| `GET /health` | open: `{status, version, time}`, 503 when the DB does not open |
| `GET /stats` | corpus counts, retrieval defaults, catalog status counts, job cost, budget, last sync |
| `GET /collection?q=&status=&limit=&offset=` | `{documents: [...]}` with `catalog_status` (`current`, `outdated`, `missing_locally`, `withdrawn`, `unknown`) |
| `GET /collection/{doc_id}` | one document: index info, catalog status, unit counts |
| `GET /toc/{doc_id}?prefix=&depth=` | structure, no text |
| `GET /lookup?doc_id=&ref=&ref=` | exact lookup; `found=false` + nearest ids is a normal 200 |
| `GET /fulltext?q=&doc_id=&kind=&syntax=safe\|fts5` | BM25 only; a malformed FTS5 expression is a 400 |
| `GET /search?q=&doc_id=&kind=&w_fts=&w_vec=&rerank=&include_figures=` | hybrid search |
| `POST /verify` `{doc_id, ref, quote}` | `verify_quote` |
| `POST /check-citations` `{text}` | validate citations in a text |
| `POST /jobs/check-citations` `{repo?}` | scan the notes folder (paths outside `VEDUN_REPO_DIR` refused); `GET /citations/latest` |
| `POST /jobs/catalog-refresh` | re-read the source catalog (needs a `source_plugin`) |
| `POST /jobs/sync` (multipart: `files`, `fetch_from_source`, `only`, `include_missing`, `dry_run`, `force`, `no_embed`) | index uploaded PDFs (max 50 MB, `%PDF-` checked) and/or newer editions from the source |
| `GET /jobs`, `GET /jobs/{id}` | job history / one job |
| `GET /figures/{doc_id}/{figure_id}.png`, `GET /pages/{doc_id}/{page}.png` | images |
| `GET /reports`, `GET /reports/{name}` | sync reports |
| `GET /documents`, `GET /documents/{doc_id}` | whole-document access: metadata, structure, host and container paths |
| `GET /documents/{doc_id}.md?section=&offset=&max_chars=&full=` | the Markdown, optionally one subtree, paginated |
| `GET /documents/{doc_id}.pdf` | the original PDF |

`doc_id` must match the profile's `doc_id_pattern`; limits are capped; report names cannot contain a path. Scripts send
`-H "X-Requested-With: vedun"` on POST and on `/api/search`.

### Jobs

One worker thread and a FIFO queue: the knowledge base has a single writer (`sync`) and conversions must not overlap.
History lives in `state.db`, not in `vedun.db`; a job still running when the process died is marked failed on the next
start. Each finished job records `cost_usd` and `api_calls`.

`sync` never widens a collection silently: its scope is `only`, or by default the indexed documents plus the files it
was given. The one exception is an empty knowledge base: its first sync takes every document in the PDF folder. A new edition is placed next to the old PDF; the document is replaced in one transaction; only then is the old
PDF deleted. If conversion or indexing fails, the new PDF goes to `<pdf_dir>/.incoming/rejected/` and the old edition
stays. Known limit: `plan_sync` ignores files whose id ends with a letter suffix (`7-11A_202301.pdf`).

### Writing a source plugin

A source plugin tells vedun where newer editions of your documents come from. With one, the dashboard shows which
documents are out of date and **Sync** can fetch the new PDFs; without one, you add PDFs yourself and the source
options are hidden. The plugin is one Python file; a complete, working example is
[`examples/source_plugin.py`](../examples/source_plugin.py) (a JSON catalogue published at a URL).

1. **Copy the example** next to your profile (the install folder's `data/`) and point the profile at it:
   `{"source_plugin": "source_plugin.py"}` in `data/vedun.config.json` (the path is relative to the profile).
2. **Set `name`**: the publisher shown in the dashboard ("Fetch newer editions from <name>").
3. **`fetch()`** returns a `Catalog` whose `entries` map each document id to a `CatalogEntry(doc_id, title, item_id,
   published, edition)`: `item_id` is whatever `download` needs (a URL, an API id), `published` is "<Month> <YYYY>" and
   `edition_from_published()` turns it into `YYYY-MM`. Raise on any error: the previous snapshot stays.
4. **`download(entry, dest)`** writes the PDF of `entry` to `dest` and returns its size. `catalog.http_json` and
   `http_download` make sequential requests with a pause, an identifying User-Agent, a `%PDF` check and a size cap.
5. **Check it**: restart vedun, open **Sync** and press **Refresh the catalog**; the job log names every entry it
   dropped and why.

What vedun does with it (`vedun.catalog`): it validates every entry (the id must match `doc_id_pattern`, the edition
must be `YYYY-MM`, others are dropped with a warning), caches the snapshot in `state.db`, labels each document
`current`, `outdated`, `missing_locally` or `withdrawn` (newest local PDF vs the catalogue's month), downloads next to
the old edition and retires the old PDF only after the new one is indexed. The protocol is `catalog.Source`.

The plugin is code that vedun imports: only point `source_plugin` at files you wrote or trust, and respect the
publisher's terms of use (many forbid automated downloads).

### Container layout

- Image (python:3.13-slim + uv, non-root user 10001): about 3 GB on disk, because Docling runs inside it; torch comes from
  the PyTorch CPU index (no CUDA). Idle memory about 60 MB; a long conversion needs several GB.
- `compose.yaml` (development) mounts the project folder at `/data/kb`, `./pdf` (or `VEDUN_PDF_HOST`) at `/data/pdf`,
  `./notes` (or `VEDUN_REPO_HOST`) at `/repo` read-only; your own folders go into `compose.override.yaml` (not tracked).
- `compose.release.yaml` (end users) mounts `./data`, `./pdf`, `./notes` of the install folder and runs under
  `VEDUN_UID`/`VEDUN_GID` (the launcher sets them: your uid on Linux Docker, 0 on rootless Podman).
- Everything that matters is a host bind mount; the only named volume is `vedun_hf` (`/hf`: conversion models, a
  re-downloadable cache). One uvicorn worker on purpose. Do not run a host `sync` while the container is up (two writers
  on one SQLite file).

### Paths in Evidence

`source_pdf` is built from the configured PDF folder (`VEDUN_PDF_DIR`) and the stored file name, `md_path` and figure
paths from `VEDUN_MD_DIR` / `VEDUN_FIG_DIR`; the same DB therefore answers with host paths on the host and container
paths in the container. `VEDUN_HOST_ROOT` / `VEDUN_HOST_PDF_DIR` tell a containerised service how the host sees them.
