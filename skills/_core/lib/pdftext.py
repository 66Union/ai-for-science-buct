#!/usr/bin/env python3
"""Minimal, dependency-free PDF text extraction.

Why this exists: the evaluation image declares a ``pdf-text-tool`` resource, but
its exact interface is not known to us, and we must not assume it. This module is
the last resort in the extraction chain: it uses only the standard library, so
the skill still produces page-anchored text when no PDF library or helper CLI is
present.

What it handles
- brute-force object scan (no xref parsing): works on most linearised and
  incremental PDFs
- ``FlateDecode`` content streams
- text operators ``BT/ET Tf Td TD Tm T* Tj TJ ' "`` with real 3x3 matrix maths,
  so x/y coordinates of every string are usable as anchors
- ``/ToUnicode`` CMaps (bfchar + bfrange) per font, falling back to cp1252

What it deliberately does not do: OCR (no text layer → we report
``text_layer: false`` rather than inventing text), tagged-PDF structure, or
figure interpretation. Those limits are surfaced to the caller, never hidden.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
STRING_RE = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)


@dataclass
class Span:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 0.0
    font: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)],
            "size": round(self.size, 2),
            "font": self.font,
        }


@dataclass
class Page:
    number: int
    width: float = 0.0
    height: float = 0.0
    spans: list[Span] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(s.text for s in self.spans if s.text.strip())


def _decode_stream(dict_bytes: bytes, data: bytes) -> bytes:
    if b"/FlateDecode" in dict_bytes:
        for candidate in (data, data.strip(b"\r\n")):
            try:
                return zlib.decompress(candidate)
            except Exception:
                continue
        try:
            return zlib.decompressobj().decompress(data)
        except Exception:
            return b""
    return data


def _objects(raw: bytes) -> list[tuple[int, bytes, bytes]]:
    """Return ``(objnum, dict_bytes, stream_bytes)`` for each object found."""
    out: list[tuple[int, bytes, bytes]] = []
    matches = list(OBJ_RE.finditer(raw))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end]
        sidx = body.find(b"stream")
        if sidx == -1:
            out.append((num, body[: max(0, body.find(b"endobj"))], b""))
            continue
        dict_part = body[:sidx]
        rest = body[sidx + len(b"stream"):]
        rest = rest.lstrip(b"\r\n")
        eidx = rest.find(b"endstream")
        data = rest[:eidx] if eidx != -1 else rest
        out.append((num, dict_part, _decode_stream(dict_part, data)))
    return out


def _num(pattern: bytes, blob: bytes, default: float = 0.0) -> float:
    m = re.search(pattern, blob)
    if not m:
        return default
    try:
        return float(m.group(1))
    except Exception:
        return default


def _mediabox(dict_bytes: bytes) -> tuple[float, float]:
    m = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", dict_bytes)
    if m:
        x0, y0, x1, y1 = (float(m.group(i)) for i in range(1, 5))
        return abs(x1 - x0), abs(y1 - y0)
    return 595.0, 842.0  # A4 fallback


def _parse_tounicode(cmap: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                code = int(src, 16)
                text = bytes.fromhex(dst.decode()).decode("utf-16-be", errors="replace")
                mapping[code] = text
            except Exception:
                continue
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                a, b = int(lo, 16), int(hi, 16)
                base = bytes.fromhex(dst.decode()).decode("utf-16-be", errors="replace")
                if not base:
                    continue
                for offset in range(0, min(b - a + 1, 4096)):
                    mapping[a + offset] = base[:-1] + chr(ord(base[-1]) + offset)
            except Exception:
                continue
    return mapping


def _string_value(raw: bytes, cmap: dict[int, str], two_byte: bool) -> str:
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == 0x5C and i + 1 < len(body):  # backslash escape
            nxt = body[i + 1]
            simple = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                      0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            if 0x30 <= nxt <= 0x37:
                oct_digits = body[i + 1:i + 4]
                m = re.match(rb"[0-7]{1,3}", oct_digits)
                if m:
                    out.append(int(m.group(0), 8) & 0xFF); i += 1 + len(m.group(0)); continue
            i += 2
            continue
        out.append(ch)
        i += 1
    if cmap:
        if two_byte:
            text = "".join(cmap.get((out[j] << 8) | out[j + 1], "")
                           for j in range(0, len(out) - 1, 2))
        else:
            text = "".join(cmap.get(b, chr(b) if 32 <= b < 127 else "") for b in out)
        if text:
            return text
    return out.decode("cp1252", errors="replace")


TOKEN_RE = re.compile(
    rb"\((?:\\.|[^\\()])*\)"        # literal string
    rb"|<[0-9A-Fa-f\s]*>"           # hex string
    rb"|\[[^\]]*\]"                 # array
    rb"|/[^\s/\[\]()<>]+"           # name
    rb"|[+-]?\d*\.?\d+"             # number
    rb"|[A-Za-z'\"*]+",             # operator
    re.S,
)


def _mat_mul(m: tuple[float, ...], n: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D,
            c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def _extract_page_spans(content: bytes, fonts: dict[str, dict[int, str]],
                         font_two_byte: dict[str, bool]) -> list[Span]:
    spans: list[Span] = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    tm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    tlm = tm
    leading = 0.0
    size = 10.0
    font = ""
    stack: list[Any] = []
    operands: list[Any] = []

    def emit(raw: bytes) -> None:
        cmap = fonts.get(font, {})
        text = _string_value(raw, cmap, font_two_byte.get(font, False))
        if not text.strip():
            return
        trm = _mat_mul(tm, ctm)
        x, y = trm[4], trm[5]
        w = len(text) * size * 0.5
        spans.append(Span(text=text, x0=x, y0=y - size * 0.8, x1=x + w, y1=y + size * 0.25,
                          size=size, font=font))

    for m in TOKEN_RE.finditer(content):
        tok = m.group(0)
        if tok.startswith(b"(") or tok.startswith(b"<"):
            operands.append(tok); continue
        if tok.startswith(b"["):
            operands.append(tok); continue
        if tok.startswith(b"/"):
            operands.append(tok); continue
        if re.fullmatch(rb"[+-]?\d*\.?\d+", tok):
            try:
                operands.append(float(tok))
            except Exception:
                operands.append(0.0)
            continue
        op = tok.decode("latin-1", errors="replace")
        if op == "BT":
            tm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0); tlm = tm
        elif op == "ET":
            pass
        elif op == "Tf" and len(operands) >= 2:
            font = operands[-2].decode("latin-1") if isinstance(operands[-2], bytes) else str(operands[-2])
            font = font.lstrip("/")
            try:
                size = float(operands[-1])
            except Exception:
                pass
        elif op == "TL" and operands:
            try:
                leading = float(operands[-1])
            except Exception:
                pass
        elif op in ("Td", "TD") and len(operands) >= 2:
            try:
                tx, ty = float(operands[-2]), float(operands[-1])
            except Exception:
                tx = ty = 0.0
            if op == "TD":
                leading = -ty
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
            tm = tlm
        elif op == "Tm" and len(operands) >= 6:
            try:
                tm = tuple(float(v) for v in operands[-6:])  # type: ignore[assignment]
            except Exception:
                tm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            tlm = tm
        elif op == "T*":
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
        elif op == "Tj" and operands:
            if isinstance(operands[-1], bytes) and operands[-1].startswith(b"("):
                emit(operands[-1])
        elif op == "'" and operands:
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
            if isinstance(operands[-1], bytes) and operands[-1].startswith(b"("):
                emit(operands[-1])
        elif op == '"' and len(operands) >= 3:
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
            if isinstance(operands[-1], bytes) and operands[-1].startswith(b"("):
                emit(operands[-1])
        elif op == "TJ" and operands:
            arr = operands[-1]
            if isinstance(arr, bytes):
                for sm in STRING_RE.finditer(arr):
                    emit(sm.group(0))
        operands = []

    # reading order: top-to-bottom, then left-to-right (handles simple 2-column
    # only approximately; callers that need column fidelity use pdf blocks below)
    spans.sort(key=lambda s: (-round(s.y1, 1), s.x0))
    return spans


def _cluster_lines(spans: list[Span], tol: float = 2.0) -> list[list[Span]]:
    lines: list[list[Span]] = []
    for span in spans:
        placed = False
        for line in lines:
            if abs(line[0].y1 - span.y1) <= tol:
                line.append(span); placed = True; break
        if not placed:
            lines.append([span])
    for line in lines:
        line.sort(key=lambda s: s.x0)
    lines.sort(key=lambda line: -line[0].y1)
    return lines


def extract(pdf_path: str | Path) -> dict[str, Any]:
    path = Path(pdf_path)
    raw = path.read_bytes()
    warnings: list[str] = []
    if not raw.lstrip().startswith(b"%PDF"):
        warnings.append("file does not start with %PDF header")

    objs = _objects(raw)
    by_num = {num: (d, s) for num, d, s in objs}

    # Font maps: name -> cmap, resolved per document (approximate but harmless).
    fonts: dict[str, dict[int, str]] = {}
    two_byte: dict[str, bool] = {}
    for num, d, s in objs:
        if b"/Type" in d and b"/Font" in d:
            name_m = re.search(rb"/BaseFont\s*/([#\w+\-.]+)", d)
            key = name_m.group(1).decode("latin-1") if name_m else f"F{num}"
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", d)
            cmap: dict[int, str] = {}
            if tu:
                ref = int(tu.group(1))
                if ref in by_num:
                    cmap = _parse_tounicode(by_num[ref][1])
            fonts[key] = cmap
            if cmap:
                two_byte[key] = max(cmap) > 0xFF

    page_objs = [(num, d) for num, d, s in objs if b"/Type" in d and re.search(rb"/Type\s*/Page\b", d)]
    pages: list[Page] = []
    for idx, (num, d) in enumerate(page_objs, start=1):
        width, height = _mediabox(d)
        content = b""
        refs = re.findall(rb"/Contents\s+(\d+)\s+\d+\s+R", d)
        arr = re.search(rb"/Contents\s*\[([^\]]*)\]", d)
        if arr:
            refs = re.findall(rb"(\d+)\s+\d+\s+R", arr.group(1))
        for ref in refs:
            entry = by_num.get(int(ref))
            if entry:
                content += entry[1] + b"\n"
        spans = _extract_page_spans(content, fonts, two_byte) if content else []
        pages.append(Page(number=idx, width=width, height=height, spans=spans))

    total_chars = sum(len(p.text) for p in pages)
    text_layer = total_chars > 20
    if not text_layer:
        warnings.append("no extractable text layer; the document is likely a scan and needs OCR")

    return {
        "engine": "builtin",
        "path": str(path),
        "pages": pages,
        "text_layer": text_layer,
        "total_chars": total_chars,
        "warnings": warnings,
        "objects": len(objs),
    }


def blocks(page: Page, line_tol: float = 2.0, gap_ratio: float = 1.6,
           size_tol: float = 1.0) -> list[dict[str, Any]]:
    """Cluster a page into paragraph-level blocks with bbox.

    Splitting keys, in order of importance:
      * a vertical gap wider than ``gap_ratio`` x the line size
      * a **font-size change** (a 12 pt heading over 10 pt body is only 2 pt, so
        an absolute threshold of 1 pt is needed; a proportional one misses it)
      * the first line of a page

    Blocks are also tagged ``likely_heading`` when they are a single line set
    noticeably larger than the page's median size — the section detector uses
    that hint instead of guessing from the text shape alone.
    """
    spans_sizes = sorted(s.size for s in page.spans if s.size)
    median = spans_sizes[len(spans_sizes) // 2] if spans_sizes else 10.0

    lines = _cluster_lines(page.spans, tol=line_tol)
    out: list[dict[str, Any]] = []
    current: list[list[Span]] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join("".join(s.text for s in line) for line in current).strip()
        if not text:
            current.clear(); return
        x0 = min(s.x0 for line in current for s in line)
        y0 = min(s.y0 for line in current for s in line)
        x1 = max(s.x1 for line in current for s in line)
        y1 = max(s.y1 for line in current for s in line)
        block_size = max((s.size for line in current for s in line), default=marker_size[0])
        out.append({
            "block_id": f"p{page.number}-b{len(out) + 1}",
            "page": page.number,
            "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "text": re.sub(r"\s+", " ", text),
            "lines": len(current),
            "size": round(block_size, 2),
            "likely_heading": bool(len(current) == 1 and block_size > median + 0.9
                                   and len(re.sub(r"\s+", " ", text)) <= 110),
        })
        current.clear()

    marker_size = [median]
    prev_y: float | None = None
    prev_size = 0.0
    for line in lines:
        if not line:
            continue
        size = max((s.size for s in line), default=10.0) or 10.0
        if prev_y is not None:
            gap = prev_y - line[0].y1
            size_changed = abs(size - prev_size) >= size_tol
            if gap > size * gap_ratio or size_changed:
                flush()
        current.append(line)
        prev_y = line[0].y1
        prev_size = size
    flush()
    return out
