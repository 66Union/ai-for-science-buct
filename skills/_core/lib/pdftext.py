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
    """Return ``(objnum, dict_bytes, stream_bytes)`` for each object found.

    Stream boundaries are located the way a PDF reader does it, not by "first
    occurrence of the word stream":
      * ``stream`` must be followed by a line break (spec requires it), so a
        dictionary entry such as ``/Title (a data stream guide)`` cannot be
        mistaken for a stream;
      * ``/Length`` is trusted when present, which avoids truncation when the
        encoded data happens to contain ``endstream``.
    """
    out: list[tuple[int, bytes, bytes]] = []
    matches = list(OBJ_RE.finditer(raw))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end]
        endobj = body.find(b"endobj")
        dict_only = body[:endobj] if endobj != -1 else body

        stream_at = -1
        for candidate in re.finditer(rb"\bstream\r?\n", dict_only):
            stream_at = candidate.start()
            break
        if stream_at == -1:
            out.append((num, dict_only, b""))
            continue

        dict_part = dict_only[:stream_at]
        data_start = stream_at + len(b"stream")
        while data_start < len(body) and body[data_start:data_start + 1] in (b"\r", b"\n"):
            data_start += 1

        length_m = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", dict_part)
        if length_m:
            length = int(length_m.group(1))
            data = body[data_start:data_start + length]
        else:
            # fall back to the LAST endstream in this object: an encoded payload
            # containing the literal word "endstream" would otherwise truncate.
            last = body.rfind(b"endstream")
            data = body[data_start:last] if last > data_start else body[data_start:]
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


def _balanced_dict(text: bytes, key: bytes) -> bytes | None:
    """Return the ``<< ... >>`` value that follows ``/key`` in a dictionary."""
    m = re.search(rb"/" + key + rb"\s*<<", text)
    if not m:
        return None
    start = m.end() - 2
    depth = 0
    i = start
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == b"<<":
            depth += 1
            i += 2
            continue
        if pair == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
            continue
        i += 1
    return None


def _resource_dict(page_dict: bytes, by_num: dict[int, tuple[bytes, bytes]]) -> bytes:
    """Resolve the page's ``/Resources`` dictionary, inline or through a reference."""
    inline = _balanced_dict(page_dict, b"Resources")
    if inline is not None:
        return inline
    m = re.search(rb"/Resources\s+(\d+)\s+\d+\s+R", page_dict)
    if m:
        entry = by_num.get(int(m.group(1)))
        if entry:
            return entry[0]
    return b""


def _page_fonts(page_dict: bytes, by_num: dict[int, tuple[bytes, bytes]]
                ) -> tuple[dict[str, dict[int, str]], dict[str, bool]]:
    """Map **resource name** (``/F1``) to its ToUnicode CMap.

    The previous version keyed fonts by ``/BaseFont``, but content streams refer to
    them by resource name, so the CMap never matched and CID text decoded as
    cp1252 garbage. The mapping has to come from ``/Resources /Font``.
    """
    fonts: dict[str, dict[int, str]] = {}
    two_byte: dict[str, bool] = {}
    resources = _resource_dict(page_dict, by_num)
    if not resources:
        return fonts, two_byte
    font_dict = _balanced_dict(resources, b"Font")
    if font_dict is None:
        ref = re.search(rb"/Font\s+(\d+)\s+\d+\s+R", resources)
        if ref:
            entry = by_num.get(int(ref.group(1)))
            font_dict = entry[0] if entry else None
    if not font_dict:
        return fonts, two_byte

    for name, ref in re.findall(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R", font_dict):
        key = name.decode("latin-1")
        entry = by_num.get(int(ref))
        if not entry:
            continue
        font_obj = entry[0]
        cmap: dict[int, str] = {}
        tu = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", font_obj)
        if tu:
            tu_entry = by_num.get(int(tu.group(1)))
            if tu_entry:
                cmap = _parse_tounicode(tu_entry[1])
        is_type0 = bool(re.search(rb"/Subtype\s*/Type0", font_obj))
        fonts[key] = cmap
        two_byte[key] = is_type0 or (bool(cmap) and max(cmap) > 0xFF)
    return fonts, two_byte


def _page_order(page_objs: list[tuple[int, bytes]],
                by_num: dict[int, tuple[bytes, bytes]]) -> list[tuple[int, bytes]]:
    """Order pages by the ``/Pages /Kids`` tree, not by object appearance.

    Object order equals page order only in the simplest files; incremental updates
    append replacement page objects and the old ones stay behind, which shifted
    page numbers and made anchors point at the wrong page.
    """
    by_num_pages = {num: d for num, d in page_objs}
    root_kids: list[int] = []
    for num, (d, _s) in by_num.items():
        if re.search(rb"/Type\s*/Pages\b", d) and not re.search(rb"/Parent\b", d):
            kids = re.search(rb"/Kids\s*\[([^\]]*)\]", d)
            if kids:
                root_kids = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1))]
                break

    ordered: list[tuple[int, bytes]] = []
    seen: set[int] = set()

    def walk(nums: list[int], depth: int = 0) -> None:
        if depth > 12:
            return
        for n in nums:
            if n in seen:
                continue
            entry = by_num.get(n)
            if not entry:
                continue
            d = entry[0]
            if re.search(rb"/Type\s*/Pages\b", d):
                kids = re.search(rb"/Kids\s*\[([^\]]*)\]", d)
                if kids:
                    walk([int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1))], depth + 1)
                continue
            if n in by_num_pages:
                seen.add(n)
                ordered.append((n, by_num_pages[n]))

    walk(root_kids)
    if ordered:
        return ordered
    return page_objs


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


def _decode_bytes(data: bytes, cmap: dict[int, str], two_byte: bool) -> str:
    if cmap:
        if two_byte:
            text = "".join(cmap.get((data[j] << 8) | data[j + 1], "")
                           for j in range(0, len(data) - 1, 2))
        else:
            text = "".join(cmap.get(b, chr(b) if 32 <= b < 127 else "") for b in data)
        if text:
            return text
    return data.decode("cp1252", errors="replace")


def _string_value(raw: bytes, cmap: dict[int, str], two_byte: bool) -> str:
    # A hex string <00480065...> is as common as a literal one in real PDFs; the
    # previous version only understood "(...)", so every hex run of text vanished.
    if raw.startswith(b"<"):
        hexstr = re.sub(rb"\s+", b"", raw[1:-1] if raw.endswith(b">") else raw[1:])
        if len(hexstr) % 2:
            hexstr += b"0"
        try:
            return _decode_bytes(bytes.fromhex(hexstr.decode("ascii")), cmap, two_byte)
        except Exception:
            return ""
    body = raw[1:-1] if raw.endswith(b")") else raw[1:]
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
    return _decode_bytes(bytes(out), cmap, two_byte)


STRING_ANY_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>", re.S)


def _is_string(tok: Any) -> bool:
    return isinstance(tok, bytes) and (tok.startswith(b"(") or tok.startswith(b"<"))


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
    rise = 0.0
    hscale = 1.0
    stack: list[Any] = []
    operands: list[Any] = []

    def emit(raw: bytes) -> None:
        cmap = fonts.get(font, {})
        text = _string_value(raw, cmap, font_two_byte.get(font, False))
        if not text.strip():
            return
        trm = _mat_mul(tm, ctm)
        x, y = trm[4], trm[5] + rise * size
        # Tz scales the glyph advance horizontally; ignoring it drifted every
        # following span on the same line.
        w = len(text) * size * 0.5 * hscale
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
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm" and len(operands) >= 6:
            try:
                ctm = _mat_mul(tuple(float(v) for v in operands[-6:]), ctm)
            except Exception:
                pass
        elif op == "Ts" and operands:
            try:
                rise = float(operands[-1])
            except Exception:
                pass
        elif op == "Tz" and operands:
            try:
                hscale = float(operands[-1]) / 100.0
            except Exception:
                pass
        elif op == "BT":
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
            if _is_string(operands[-1]):
                emit(operands[-1])
        elif op == "'" and operands:
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
            if _is_string(operands[-1]):
                emit(operands[-1])
        elif op == '"' and len(operands) >= 3:
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -leading), tlm)
            tm = tlm
            if _is_string(operands[-1]):
                emit(operands[-1])
        elif op == "TJ" and operands:
            arr = operands[-1]
            if isinstance(arr, bytes):
                for sm in STRING_ANY_RE.finditer(arr):
                    emit(sm.group(0))
        operands = []

    # reading order: top-to-bottom, then left-to-right (handles simple 2-column
    # only approximately; callers that need column fidelity use pdf blocks below)
    spans.sort(key=lambda s: (-round(s.y1, 1), s.x0))
    return spans


def _column_split(spans: list[Span], page_width: float) -> float | None:
    """Return the gutter x of a two-column page, or None for single column.

    A two-column paper has a near-empty vertical band in the middle: hardly any
    span crosses it. Treating such a page as one flow merges the left and right
    columns into the same line, which is the classic "串栏" failure.
    """
    if len(spans) < 12 or page_width <= 0:
        return None
    mid = page_width / 2.0
    band = page_width * 0.05
    crossing = sum(1 for s in spans if s.x0 < mid - band and s.x1 > mid + band)
    left = sum(1 for s in spans if (s.x0 + s.x1) / 2 < mid)
    right = len(spans) - left - crossing
    # require both columns to be substantial and the gutter to be almost clean
    if crossing <= max(2, len(spans) * 0.03) and left >= 4 and right >= 4:
        return mid
    return None


def _cluster_lines(spans: list[Span], tol: float = 2.0,
                   page_width: float | None = None) -> list[list[Span]]:
    """Group spans into visual lines, one column at a time.

    Clustering tolerance follows the *line's* dominant size rather than a fixed
    2 pt: a superscript sits a few points above its baseline and used to become a
    line of its own, reordering scientific notation and units.
    """
    if not spans:
        return []
    split = _column_split(spans, page_width) if page_width else None

    groups: dict[int, list[Span]] = {}
    for span in spans:
        if split is None:
            column = 0
        elif span.x0 < split - 8 and span.x1 > split + 8:
            column = -1  # full-width element (title, wide figure caption)
        else:
            column = 0 if (span.x0 + span.x1) / 2 < split else 1
        groups.setdefault(column, []).append(span)

    lines: list[tuple[int, list[Span]]] = []
    for column, group in groups.items():
        group.sort(key=lambda s: (-round(s.y1, 1), s.x0))
        current: list[Span] = []
        baseline = 0.0
        for span in group:
            line_size = current[0].size if current else (span.size or 10.0)
            if current and abs(span.y1 - baseline) <= max(tol, 0.45 * (line_size or 10.0)):
                current.append(span)
            else:
                if current:
                    lines.append((column, sorted(current, key=lambda s: s.x0)))
                current = [span]
                baseline = span.y1
        if current:
            lines.append((column, sorted(current, key=lambda s: s.x0)))

    # Reading order is **column-major**, not row-major: sorting by y alone
    # interleaved the left and right columns line by line, which is exactly the
    # 串栏 failure. Full-width elements ride with the left column so a title or a
    # wide abstract still lands above the body.
    def column_rank(column: int) -> int:
        return 0 if column <= 0 else 1

    lines.sort(key=lambda item: (column_rank(item[0]), -round(item[1][0].y1, 1)))
    return [line for _column, line in lines]


def _line_text(line: list[Span]) -> str:
    """Join a line's spans, inserting a space only where layout implies one.

    ``"".join`` glued every word together ("Thisisatestsentence"); inserting a
    space unconditionally would break superscripts ("10 -3 mol"). A space is
    added only between same-size spans separated by a real gap.
    """
    parts: list[str] = []
    prev_x1: float | None = None
    prev_size = 0.0
    for span in line:
        if prev_x1 is not None:
            gap = span.x0 - prev_x1
            same_size = prev_size and abs(span.size - prev_size) <= 0.2 * prev_size
            if same_size and gap > max(1.5, 0.22 * prev_size):
                parts.append(" ")
        parts.append(span.text)
        prev_x1 = span.x1
        prev_size = span.size or prev_size
    return "".join(parts)


def extract(pdf_path: str | Path) -> dict[str, Any]:
    path = Path(pdf_path)
    raw = path.read_bytes()
    warnings: list[str] = []
    if not raw.lstrip().startswith(b"%PDF"):
        warnings.append("file does not start with %PDF header")

    objs = _objects(raw)
    by_num = {num: (d, s) for num, d, s in objs}

    # Honest reporting of the conditions that make extraction unreliable. Each one
    # used to fail silently while the page still claimed span-quality anchors.
    if b"/Encrypt" in raw[:4096] or re.search(rb"/Encrypt\s", raw):
        warnings.append("document is encrypted (/Encrypt present); text may be partial or absent")
    unsupported_filters = 0
    for _num, d, _s in objs:
        if b"/Filter" in d and not re.search(rb"/Filter\s*(/FlateDecode|\[\s*/FlateDecode)", d):
            unsupported_filters += 1
    if unsupported_filters:
        warnings.append(f"{unsupported_filters} stream(s) use a filter other than FlateDecode; "
                        "those streams were left undecoded")

    page_objs = [(num, d) for num, d, s in objs
                 if b"/Type" in d and re.search(rb"/Type\s*/Page\b", d)]
    page_objs = _page_order(page_objs, by_num)

    pages: list[Page] = []
    no_cmap_high_bytes = 0
    for idx, (num, d) in enumerate(page_objs, start=1):
        width, height = _mediabox(d)
        fonts, two_byte = _page_fonts(d, by_num)
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
        if any(ord(ch) > 0x7F for sp in spans for ch in sp.text):
            if not any(fonts.get(sp.font) for sp in spans):
                no_cmap_high_bytes += 1
        pages.append(Page(number=idx, width=width, height=height, spans=spans))

    if no_cmap_high_bytes:
        warnings.append(f"{no_cmap_high_bytes} page(s) contain non-ASCII text with no usable "
                        "ToUnicode CMap; those characters may be garbled")

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

    lines = _cluster_lines(page.spans, tol=line_tol, page_width=page.width)
    out: list[dict[str, Any]] = []
    current: list[list[Span]] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(_line_text(line) for line in current).strip()
        if not text:
            current.clear(); return
        x0 = min(s.x0 for line in current for s in line)
        y0 = min(s.y0 for line in current for s in line)
        x1 = max(s.x1 for line in current for s in line)
        y1 = max(s.y1 for line in current for s in line)
        block_size = max((s.size for line in current for s in line), default=marker_size[0])
        # Column starts are the real signal for tabular layout. The previous table
        # detector looked for 3+ spaces in this text - impossible after collapsing
        # whitespace, so it could only ever find blocks containing many numbers.
        x_starts = sorted({round(s.x0, 1) for line in current for s in line})
        col_gaps = sum(1 for a, b in zip(x_starts, x_starts[1:]) if b - a > 8.0)
        out.append({
            "block_id": f"p{page.number}-b{len(out) + 1}",
            "page": page.number,
            "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "text": re.sub(r"\s+", " ", text),
            "lines": len(current),
            "size": round(block_size, 2),
            "x_starts": x_starts[:40],
            "col_gaps": col_gaps,
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
