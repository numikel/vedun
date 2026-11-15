---
name: vedun-add-documents
description: Add, update or remove documents in a vedun knowledge base, and set up the corpus profile so vedun recognises the user's document numbers. Use when the user wants to add PDFs, load a new edition of a standard, sees files rejected or missing after a sync, or has documents numbered differently than "4-12".
---

# Add documents to vedun

## How vedun identifies documents

- One PDF = one edition of one document. The number and edition come from the **file name**:
  `<doc_id>_<YYYYMM>.pdf`, e.g. `4-12_202603.pdf` (document `4-12`, edition March 2026).
- `doc_id` must match the profile's `doc_id_pattern` (default `\d{1,2}-\d{1,3}[A-Z]?`, i.e. `4-12`, `10-3A`).
- One edition per document: a newer `YYYYMM` replaces the older one after it is indexed; the old PDF is then deleted.
- Born-digital PDFs only (OCR is off): scanned images give empty documents.

## 1. Look at the files first

List the user's PDFs. For each, propose `<doc_id>_<YYYYMM>.pdf` from the printed number and edition date (read the
first page if needed). Show the proposed renames as a table and ask before renaming anything.

## 2. If the numbering does not fit the default pattern

Documents like `ISO-45001`, `EN 54-2` or `SOP-017` need a profile. Ask the user how their documents are cited, then
edit `<install folder>/data/vedun.config.json` (keep existing keys):

```json
{
  "ui_language": "en",
  "corpus_name": "Quality procedures",
  "doc_id_pattern": "SOP-\\d{3}",
  "doc_label": "{doc_id}",
  "citation_template": {"en": "{doc_id} \"{title}\" ({edition}), {locator}"}
}
```

- `doc_id_pattern` is a regex without groups, matched against the part of the file name before `_YYYYMM`. Test it
  on every file name before saving (a quick Python `re.fullmatch` is enough).
- `doc_label` goes into every embedded chunk: decide it before the first sync (changing it later means re-embedding).
- Restart after editing the profile: `vedun stop && vedun start` (Windows: `.\vedun.ps1 stop`, `.\vedun.ps1 start`).
All keys: `docs/reference.md#corpus-profile` in the vedun repository.

## 3. Add and sync

- **Empty knowledge base (first sync):** copy the renamed PDFs into `<install folder>/pdf/` and press **Sync** on
  the dashboard page **Sync**: it takes every document in the folder.
- **Later:** the user adds the PDFs on the **Sync** page (upload) and presses **Sync**. A later sync covers the
  documents already indexed plus the files added there: a PDF only copied into the folder is **not** picked up.
  It converts and embeds only new or changed files; the page shows progress and a report. Conversion runs locally and
  can take minutes per long document; the first run downloads the conversion models (~0.5 GB).
- Scripted alternative (the API key must already be in the environment, never pasted in chat): upload the files as
  multipart `files`, or name documents already in the `pdf` folder with `only`:
  `curl -X POST -H "Authorization: Bearer $VEDUN_API_KEY" -H "X-Requested-With: vedun" -F files=@4-12_202603.pdf http://127.0.0.1:8765/api/jobs/sync`
  then poll `GET /api/jobs/<job_id>`.
- Never run a sync from the host command line while the container runs (two writers on one database).

## 4. Check the result

- Dashboard **Documents**: every new document listed with its edition.
- Files that failed conversion are moved to `pdf/.incoming/rejected/`; the previous edition stays. Common causes:
  a name that does not match `doc_id_pattern`, a scanned PDF, a damaged file.
- Known limit: ids ending in a letter (`7-11A`) are skipped by sync for now.
- A document with huge numeric tables can convert for hours: add its id to `fast_table_documents` in the profile.
- After a **new edition**: open the sync report (dashboard **Reports**): removed, new, changed and probably renumbered
  clauses. Then run **Citations in repo** (notes folder `<install folder>/notes/`) to find references that no longer
  point anywhere, and help the user fix them.

## Removing a document

Delete its PDF from `pdf/` and run a sync: documents whose PDF is gone are removed from the knowledge base. Ask first:
this cannot be undone except by adding the file again.
