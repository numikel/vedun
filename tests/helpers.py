"""Test helpers: synthetic documents, a fake converter and a fake embedding transport."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

from vedun import config as C
from vedun.convert import ConvertInfo, edition_tag, parse_pdf_name
from vedun.embed import Embedder

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)))
    + struct.pack(">I", 12)
    + b"IDAT"
    + zlib.compress(b"\x00\xff\x00\x00")
    + struct.pack(">I", zlib.crc32(b"IDAT" + zlib.compress(b"\x00\xff\x00\x00")))
    + struct.pack(">I", 0)
    + b"IEND"
    + struct.pack(">I", zlib.crc32(b"IEND"))
)


def make_md(ds: str, edition: str, body: str, title: str = "Test Sheet") -> str:
    """Markdown with front matter, as written by the converter."""
    return f"---\nds: {ds}\ntitle: {title}\nedition: {edition}\nsource_pdf: x.pdf\n---\n\n{body}\n"


BODY_V1 = """<!-- page: 2 -->

## 1.0 SCOPE

This code part covers testing.

### 1.2 Changes

October 2026. Fully revised.

A. Added things.

## 2.0 SAFETY REQUIREMENTS

### 2.3 Protection

2.3.2.1 Treat storage of stacked scaffold boards up to 2 m (6.5 ft) high as incidental to the workshop occupancy.

2.3.2.2 Provide emergency lights rated for the corridor class with a minimum output of 3 W per fitting.

<!-- page: 3 -->

2.3.2.3 Provide a battery reserve for 90 minutes with a safety margin included in the load.

**Table 9a. Door maintenance schedule**

| Item | Frequency |
|---|---|
| Inspect fire door hinges | semiannually |
| Test all closers | annually |

**Fig. 2.3.1. Exit route diagram**

![Fig. 2.3.1. Exit route diagram](../figures/x/fig_p003_01.png)

2.3.2.4 Limit paint stock to 45 kg (99 lb) per cabinet.

A. First lettered item.

B. Second lettered item.
"""

# edition 2: 2.3.2.1 -> 2.3.7.1.1 (renumbered, text identical), 2.3.2.4 edited, 2.3.2.2 removed,
# Table 9a -> Table 2.15.1.1 (same content), new clause 2.3.7.2.
BODY_V2 = """<!-- page: 2 -->

## 1.0 SCOPE

This code part covers testing.

### 1.2 Changes

April 2027. Renumbered clauses.

## 2.0 SAFETY REQUIREMENTS

### 2.3 Protection

2.3.7.1.1 Treat storage of stacked scaffold boards up to 2 m (6.5 ft) high as incidental to the workshop occupancy.

<!-- page: 3 -->

2.3.2.3 Provide a battery reserve for 90 minutes with a safety margin included in the load.

**Table 2.15.1.1. Door maintenance schedule**

| Item | Frequency |
|---|---|
| Inspect fire door hinges | semiannually |
| Test all closers | annually |

**Fig. 2.3.1. Exit route diagram**

![Fig. 2.3.1. Exit route diagram](../figures/x/fig_p003_01.png)

2.3.2.4 Limit paint stock to 60 kg (132 lb) per cabinet.

2.3.7.2 A brand new requirement about something entirely different and unrelated.
"""


def fake_converter(bodies: dict[str, str]):
    """Converter writing synthetic Markdown (``bodies``: edition -> body) instead of running Docling."""

    def convert(pdf_path: Path, md_dir: Path, fig_root: Path) -> ConvertInfo:
        ds, ed = parse_pdf_name(pdf_path)
        tag = edition_tag(ed)
        md_dir.mkdir(parents=True, exist_ok=True)
        fig_dir = fig_root / f"{ds}_{tag}"
        fig_dir.mkdir(parents=True, exist_ok=True)
        (fig_dir / "fig_p003_01.png").write_bytes(PNG_1x1)
        md = md_dir / f"{ds}_{tag}.md"
        md.write_text(make_md(ds, ed, bodies[ed]), encoding="utf-8")
        sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        return ConvertInfo(
            ds,
            ed,
            md,
            fig_dir,
            3,
            0.1,
            0.03,
            10,
            1,
            sha,
            "Test Sheet",
            "fake 0",
            "April 2019, Amended October 2026",
            "printed on the sheet",
            str(pdf_path.resolve()),
        )

    return convert


def fake_transport(url: str, headers: dict, body: bytes):
    """Deterministic fake of the OpenRouter embeddings endpoint (no network)."""
    req = json.loads(body)
    data = []
    for i, item in enumerate(req["input"]):
        key = json.dumps(item, sort_keys=True) if not isinstance(item, str) else item
        h = hashlib.sha256(key.encode()).digest()
        vec = [(b - 127.5) / 127.5 for b in h[:16]] + [0.0] * 4080  # >= any configured dim
        data.append({"index": i, "embedding": vec})
    return 200, {"data": data, "usage": {"prompt_tokens": 10, "cost": 0.0}}


def fake_embedders(keys: list[str]) -> dict[str, Embedder]:
    """Embedders for the given model keys wired to the fake transport."""
    return {k: Embedder(C.MODELS[k], transport=fake_transport, sleep=lambda s: None) for k in keys}


def make_pdf(path: Path, lines: list[tuple[float, float, str]], title: str = "") -> Path:
    """Write a real one-page PDF (Helvetica 10 pt) with ``(x, y_from_top, text)`` lines; no PDF library needed."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")

    stream = "".join(f"BT /F1 10 Tf {x} {842 - y} Td ({esc(t)}) Tj ET\n" for x, y, t in lines).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        f"<< /Title ({esc(title)}) >>".encode("latin-1"),
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path
