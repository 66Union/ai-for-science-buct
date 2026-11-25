#!/usr/bin/env python3
"""Self-contained tests for the builtin PDF path and the contract reflection.

Run:  python skills/_core/tests/test_core.py

The PDF fixtures are generated here with plain string assembly, so the test needs
no PDF library and no network. That keeps the "does the fallback engine actually
work" question answerable on any machine, which matters because the evaluation
image's helper names are unknown to us.
"""

from __future__ import annotations

import json
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import anchors, contract, extract, pdftext  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def make_pdf(path: Path, lines: list[tuple[float, float, str]]) -> None:
    """Write a minimal single-page PDF with an uncompressed content stream."""
    ops = ["BT"]
    for x, y, text in lines:
        ops.append(f"/F1 12 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj")
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>"
    )
    objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def test_pdf_extraction(tmp: Path) -> None:
    print("builtin PDF extraction")
    pdf = tmp / "sample.pdf"
    make_pdf(pdf, [(72, 720, "Alpha beta gamma."), (72, 700, "Delta epsilon zeta."),
                   (72, 660, "Eta theta iota.")])
    doc = pdftext.extract(pdf)
    check("one page found", len(doc["pages"]) == 1, f"got {len(doc['pages'])}")
    check("text layer detected", doc["text_layer"] is True)
    page = doc["pages"][0]
    text = page.text
    check("first line extracted", "Alpha beta gamma." in text, repr(text[:80]))
    check("third line extracted", "Eta theta iota." in text, repr(text[:80]))
    check("spans carry bbox", all(len(s.as_dict()["bbox"]) == 4 for s in page.spans))

    blocks = pdftext.blocks(page)
    check("blocks produced", len(blocks) >= 1, f"got {len(blocks)}")
    check("block ids are page-scoped",
          all(b["block_id"].startswith("p1-") for b in blocks),
          str([b["block_id"] for b in blocks]))
    return doc


def test_contract(tmp: Path) -> None:
    print("output_contract reflection")
    case = {
        "case_id": "case-042",
        "task": {
            "output_contract": {
                "parsed_pages": "page-level parse result",
                "table_index": {"description": "table inventory", "type": "array"},
            }
        },
        "material": {"pdf": "/input/paper.pdf"},
    }
    case_path = tmp / "case.json"
    case_path.write_text(json.dumps(case), encoding="utf-8")
    loaded = contract.load_case(case_path)
    payload = contract.build_result(
        case=loaded,
        case_id=contract.find_case_id(loaded),
        values={"parsed_pages": [{"page": 1}], "extra_note": "kept aside"},
        status="partial",
        limits=["no OCR available"],
    )
    check("case_id preserved", payload["case_id"] == "case-042", payload.get("case_id", ""))
    check("declared field carried", payload["parsed_pages"] == [{"page": 1}])
    check("undeclared field left None (not invented)", payload["table_index"] is None)
    check("missing field recorded",
          payload["meta"]["contract_fields_not_reported"] == ["table_index"],
          str(payload["meta"]["contract_fields_not_reported"]))
    check("undeclared value kept aside",
          payload["meta"]["additional_outputs"]["extra_note"] == "kept aside")
    written = contract.write_result(tmp / "out", payload)
    check("result.json written", written.is_file())
    reloaded = json.loads(written.read_text(encoding="utf-8"))
    check("result.json round-trips", reloaded["case_id"] == "case-042")


def test_contract_empty(tmp: Path) -> None:
    print("no contract declared")
    payload = contract.build_result(case={}, case_id="c1", values={"a": 1}, status="partial")
    check("contract_detected false", payload["meta"]["contract_detected"] is False)
    check("no invented contract fields",
          payload["meta"]["contract_field_count"] == 0)
    check("value kept under additional_outputs",
          payload["meta"]["additional_outputs"]["a"] == 1)


def test_extract_chain(tmp: Path) -> None:
    print("extraction chain")
    pdf = tmp / "chain.pdf"
    make_pdf(pdf, [(72, 720, "Catalyst loading was 5 wt%."),
                   (72, 700, "Selectivity reached 92 percent."),
                   (72, 660, "The solvent was ethanol.")])
    doc = extract.extract_document(pdf)
    check("an engine answered", bool(doc["engine"]), str(doc["engine"]))
    check("pages returned", len(doc["pages"]) == 1, f"got {len(doc['pages'])}")
    check("blocks indexed by page", "1" in doc["blocks"], str(list(doc.get("blocks", {}))))
    check("anchor quality reported", doc["anchor_quality"] in {"span", "page", "text"},
          doc["anchor_quality"])
    check("text contains a numeric claim",
          any("92" in b["text"] for b in doc["blocks"]["1"]),
          str([b["text"][:40] for b in doc["blocks"]["1"]]))
    probe = extract.probe()
    check("probe always reports builtin", probe["builtin"]["ok"] is True)
    return doc


def test_anchors(tmp: Path) -> None:
    print("evidence anchors")
    pdf = tmp / "anchor.pdf"
    make_pdf(pdf, [(72, 720, "Catalyst loading was 5 wt% and selectivity 92 percent."),
                   (72, 700, "The solvent was ethanol at room temperature.")])
    doc = extract.extract_document(pdf)
    blocks = doc["blocks"]["1"]

    # a claim that the source really supports
    block = next((b for b in blocks if "Catalyst" in b["text"]), blocks[0])
    good = anchors.make_anchor(1, block, anchors.pick_quote(block["text"], ["Catalyst", "5 wt%"]))
    res = anchors.verify(doc, good)
    check("real quote verifies", res["match"] in anchors.GROUNDED, str(res))

    # a claim whose quote is not in the document
    fake = anchors.make_anchor(1, block, "Selectivity collapsed to 3 percent.")
    res_fake = anchors.verify(doc, fake)
    check("invented quote does not verify", res_fake["match"] not in anchors.GROUNDED, str(res_fake))

    # anchoring by keyword search
    auto = anchors.build(doc, "catalyst loading 5 wt%")
    check("auto-anchor finds a block", len(auto) >= 1, str(auto))
    report = anchors.verify_all(doc, [{"claim_id": "c1", "anchors": auto}])
    check("grounding rate computed", report["claims_total"] == 1, str(report))
    check("auto-anchor is grounded", report["grounded"] == 1, str(report))

    flag_case = [{"claim_id": "c2", "anchors": [fake]}]
    report2 = anchors.verify_all(doc, flag_case)
    check("ungrounded claim flagged", report2["ungrounded"] == 1, str(report2))
    check("flag attached to claim", "ungrounded" in flag_case[0]["flags"], str(flag_case[0]))


def main() -> int:
    # A workspace-local scratch dir: %TEMP% is outside the sandbox on this host.
    root = Path(__file__).resolve().parent / ".tmp"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        test_pdf_extraction(root)
        test_extract_chain(root)
        test_anchors(root)
        test_contract(root)
        test_contract_empty(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
