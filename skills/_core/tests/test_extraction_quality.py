#!/usr/bin/env python3
"""Quality tests for the extraction fixes: CID fonts, hex strings, columns, scripts.

Each test builds a PDF that reproduces one real-world layout the previous code got
wrong, then asserts the *specific* symptom is gone. Without these, "fixed" would
only mean "the old tests still pass".

Run:  python skills/_core/tests/test_extraction_quality.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import extract, pdftext  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def write_pdf(path: Path, objects: list[bytes]) -> None:
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))


def stream(dict_extra: bytes, data: bytes) -> bytes:
    return b"<< " + dict_extra + b" /Length " + str(len(data)).encode() + b" >>\nstream\n" + data + b"\nendstream"


def page_dict(content_num: int, fonts: bytes) -> bytes:
    return (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Contents " + str(content_num).encode() + b" 0 R "
            b"/Resources << /Font << " + fonts + b" >> >> >>")


def test_cid_font_hex_strings(tmp: Path) -> None:
    """A Type0/CID font addressed by resource name, text given as hex strings."""
    print("CID font + hex strings")
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"3 beginbfchar\n<0041> <0041>\n<0042> <0042>\n<0043> <0043>\n"
            b"endbfchar\nendcmap end end")
    content = b"BT /F1 12 Tf 72 720 Td <004100420043> Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page_dict(4, b"/F1 5 0 R"),
        stream(b"", content),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+SimSun "
        b"/Encoding /Identity-H /ToUnicode 6 0 R >>",
        stream(b"", cmap),
    ]
    pdf = tmp / "cid.pdf"
    write_pdf(pdf, objects)
    doc = extract.extract_document(pdf)
    text = doc["pages"][0].text if doc["pages"] else ""
    check("hex string text decoded through ToUnicode", "ABC" in text, f"got {text!r}")
    check("no cp1252 garbage", "\ufffd" not in text, f"got {text!r}")


def test_two_columns(tmp: Path) -> None:
    """Two columns must not interleave: left column reads through, then right."""
    print("two-column reading order")
    ops = ["BT"]
    for i in range(10):
        y = 760 - i * 20
        ops.append(f"/F1 10 Tf 1 0 0 1 50 {y} Tm (LEFT{i:02d}) Tj")
        ops.append(f"/F1 10 Tf 1 0 0 1 330 {y} Tm (RIGHT{i:02d}) Tj")
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page_dict(4, b"/F1 5 0 R"),
        stream(b"", content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = tmp / "twocol.pdf"
    write_pdf(pdf, objects)
    doc = extract.extract_document(pdf)
    page = doc["pages"][0]
    blocks = doc["blocks"]["1"]
    flat = " ".join(b["text"] for b in blocks)
    left_positions = [flat.find(f"LEFT{i:02d}") for i in range(10)]
    right_positions = [flat.find(f"RIGHT{i:02d}") for i in range(10)]
    check("all columns extracted", all(p >= 0 for p in left_positions + right_positions), flat[:120])
    check("left column stays contiguous",
          left_positions == sorted(left_positions), str(left_positions))
    check("right column stays contiguous",
          right_positions == sorted(right_positions), str(right_positions))
    check("left column read before right",
          max(left_positions) < min(right_positions),
          f"max_left={max(left_positions)} min_right={min(right_positions)}")
    check("gutter detected", pdftext._column_split(page.spans, page.width) is not None)


def test_superscript(tmp: Path) -> None:
    """A superscript belongs to its baseline line, not a line of its own."""
    print("superscript within one line")
    content = "\n".join([
        "BT",
        "/F1 10 Tf 1 0 0 1 72 700 Tm (10) Tj",
        "/F2 6 Tf 1 0 0 1 88 703.3 Tm (-3) Tj",
        "/F1 10 Tf 1 0 0 1 97 700 Tm (mol) Tj",
        "ET",
    ]).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page_dict(4, b"/F1 5 0 R /F2 6 0 R"),
        stream(b"", content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = tmp / "sup.pdf"
    write_pdf(pdf, objects)
    doc = extract.extract_document(pdf)
    blocks = doc["blocks"]["1"]
    line_count = sum(b["lines"] for b in blocks)
    merged = " ".join(b["text"] for b in blocks)
    check("superscript not split into its own line", line_count == 1, f"lines={line_count}")
    check("all three pieces present in order",
          merged.find("10") < merged.find("-3") < merged.find("mol"), merged)
    check("no space injected inside the exponent", "-3" in merged and " -3" not in merged, merged)


def test_stream_lookalike(tmp: Path) -> None:
    """A dictionary value containing the word 'stream' must not derail parsing."""
    print("stream keyword inside a string")
    content = b"BT /F1 12 Tf 72 720 Td (After) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> /Title (a data stream guide) >>",
        stream(b"", content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = tmp / "streamword.pdf"
    write_pdf(pdf, objects)
    doc = extract.extract_document(pdf)
    text = doc["pages"][0].text if doc["pages"] else ""
    check("text after a lookalike 'stream' is kept", "After" in text, f"got {text!r}")


def main() -> int:
    root = Path(__file__).resolve().parent / ".tmp-quality"
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        test_cid_font_hex_strings(root)
        test_two_columns(root)
        test_superscript(root)
        test_stream_lookalike(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all quality checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
