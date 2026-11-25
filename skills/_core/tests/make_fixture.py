#!/usr/bin/env python3
"""Build a synthetic but realistic fixture: a case.json plus a paper-like PDF.

This doubles as the skill's documented minimal example: the same generator makes
the input that ``SKILL.md`` shows, so the example in the docs cannot drift from
what the code actually consumes.

Usage:  python skills/_core/tests/make_fixture.py [outdir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PAGES = [
    [
        (72, 760, "/F2 16 Tf (Structure and Reactivity of Supported Ni Catalysts) Tj"),
        (72, 736, "/F1 10 Tf (A. Author, B. Author - Institute of Catalysis) Tj"),
        (72, 700, "/F2 12 Tf (Abstract) Tj"),
        (72, 682, "/F1 10 Tf (We report a supported nickel catalyst that reaches 92 percent) Tj"),
        (72, 668, "/F1 10 Tf (selectivity for methane reforming at 750 K and 1 atm.) Tj"),
        (72, 620, "/F2 12 Tf (1 Introduction) Tj"),
        (72, 602, "/F1 10 Tf (Supported metal catalysts are widely used in reforming reactions.) Tj"),
        (72, 588, "/F1 10 Tf (Earlier work reported lower selectivity under similar conditions [3].) Tj"),
    ],
    [
        (72, 760, "/F2 12 Tf (2 Methods) Tj"),
        (72, 742, "/F1 10 Tf (The catalyst precursor was dissolved in ethanol and dried at 373 K.) Tj"),
        (72, 728, "/F1 10 Tf (Nickel loading was 5 wt percent, determined by ICP-OES.) Tj"),
        (72, 700, "/F2 12 Tf (3 Results) Tj"),
        (72, 682, "/F1 10 Tf (Conversion and selectivity are summarized below.) Tj"),
        (72, 664, "/F1 10 Tf (Temperature K    Conversion pct    Selectivity pct) Tj"),
        (72, 650, "/F1 10 Tf (700              71                84) Tj"),
        (72, 636, "/F1 10 Tf (750              88                92) Tj"),
        (72, 622, "/F1 10 Tf (800              95                79) Tj"),
        (72, 596, "/F1 10 Tf (The rate constant follows k = A exp\\(-Ea/RT\\)      \\(1\\)) Tj"),
    ],
    [
        (72, 760, "/F2 12 Tf (4 Discussion) Tj"),
        (72, 742, "/F1 10 Tf (Selectivity peaks at 750 K and does not increase above that point.) Tj"),
        (72, 728, "/F1 10 Tf (Sintering is the likely cause, as reported for similar systems [4].) Tj"),
        (72, 700, "/F2 12 Tf (References) Tj"),
        (72, 682, "/F1 10 Tf ([1] Smith J. et al. J. Catal. 2019, 371, 112-120.) Tj"),
        (72, 668, "/F1 10 Tf ([2] Chen L. et al. ACS Catal. 2020, 10, 4455-4463.) Tj"),
        (72, 654, "/F1 10 Tf ([3] Wang H. et al. Catal. Today 2018, 316, 88-95.) Tj"),
        (72, 640, "/F1 10 Tf ([4] Garcia M. et al. Appl. Catal. A 2021, 610, 117940.) Tj"),
    ],
]


def make_pdf(path: Path) -> None:
    objects: list[bytes] = []
    page_obj_start = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_obj_start + 2 * i} 0 R" for i in range(len(PAGES)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(PAGES)} >>".encode())

    for index, lines in enumerate(PAGES):
        ops = ["BT"]
        for x, y, op in lines:
            ops.append(f"1 0 0 1 {x} {y} Tm {op}")
        ops.append("ET")
        content = "\n".join(ops).encode("latin-1")
        page_num = page_obj_start + 2 * index
        content_num = page_num + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Contents {content_num} 0 R "
            f"/Resources << /Font << /F1 {page_obj_start + 2 * len(PAGES)} 0 R "
            f"/F2 {page_obj_start + 2 * len(PAGES) + 1} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                       + content + b"\nendstream")

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


CASE = {
    "case_id": "demo-case-001",
    "task": "Parse the supplied paper into structured, page-anchored content.",
    "output_contract": {
        "parsed_pages": "page-level blocks with page number and bbox",
        "section_outline": {"description": "heading hierarchy with page anchors", "type": "array"},
        "table_index": {"description": "candidate tables with page and confidence", "type": "array"},
        "formula_index": {"description": "candidate formulas with numbering", "type": "array"},
        "reference_list": {"description": "parsed bibliography entries", "type": "object"},
        "full_text": "the extracted text, page order preserved",
    },
    "material": {"file": "paper.pdf", "note": "synthetic fixture, not a real publication"},
}


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "fixture"
    out.mkdir(parents=True, exist_ok=True)
    (out / "case.json").write_text(json.dumps(CASE, ensure_ascii=False, indent=2), encoding="utf-8")
    make_pdf(out / "paper.pdf")
    (out / "task.md").write_text(
        "# Task\n\nParse `paper.pdf` into structured content.\n\n"
        "Required: page-level blocks, section outline, table candidates, "
        "formula candidates, reference list, and full text. "
        "Every structural finding must point back to a page.\n",
        encoding="utf-8")
    print(f"fixture written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
