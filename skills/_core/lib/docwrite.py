#!/usr/bin/env python3
"""Write editable deliverables without third-party packages.

Two of the writing topics ask for an editable artifact, and the runtime image is
only promised to provide a generic scientific Python stack. So the writers here use
the standard library only:

* Markdown  - the reviewable source of truth;
* HTML      - a self-contained preview that opens without any renderer;
* DOCX      - a real OOXML zip (python-docx, when installed, is not required).

The DOCX writer deliberately supports the small block vocabulary the writing
adapters need (headings, paragraphs, bullet lists, tables, page breaks). It is not
a general Word library, and the SKILL.md says so.
"""

from __future__ import annotations

import html
import zipfile
from pathlib import Path
from typing import Any, Iterable

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr>
    <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimSun"/>
    <w:sz w:val="22"/><w:szCs w:val="22"/>
  </w:rPr></w:rPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
    <w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
    <w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
    <w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
    <w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
</w:styles>"""

_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def _xml_escape(text: str) -> str:
    return (html.escape(str(text or ""), quote=False)
            .replace("\x00", "").replace("\x0b", " "))


def _run(text: str, *, bold: bool = False, italic: bool = False) -> str:
    props = ""
    if bold or italic:
        props = "<w:rPr>" + ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "") + "</w:rPr>"
    return f'<w:r>{props}<w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r>'


def _paragraph(block: dict[str, Any]) -> str:
    kind = block.get("type") or "para"
    text = block.get("text") or ""
    runs = block.get("runs")
    if kind in {"heading", "title"}:
        level = int(block.get("level") or 1)
        style = "Title" if kind == "title" else f"Heading{min(max(level, 1), 3)}"
        return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{_run(text)}</w:p>'
    if kind == "list":
        items = block.get("items") or []
        out = []
        for item in items:
            out.append(f'<w:p><w:pPr><w:ind w:left="360"/></w:pPr>'
                       f'{_run("bullet ")}{_run(str(item))}</w:p>')
        return "".join(out)
    if runs:
        body = "".join(_run(run.get("text", ""), bold=run.get("bold", False),
                            italic=run.get("italic", False)) for run in runs)
        return f"<w:p>{body}</w:p>"
    return f"<w:p>{_run(text)}</w:p>"


def _table(block: dict[str, Any]) -> str:
    rows = block.get("rows") or []
    header = bool(block.get("header", True))
    out = ['<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>'
           '<w:tblBorders>'
           '<w:top w:val="single" w:sz="8"/><w:left w:val="single" w:sz="4"/>'
           '<w:bottom w:val="single" w:sz="8"/><w:right w:val="single" w:sz="4"/>'
           '<w:insideH w:val="single" w:sz="4"/><w:insideV w:val="single" w:sz="4"/>'
           '</w:tblBorders></w:tblPr>']
    for index, row in enumerate(rows):
        out.append("<w:tr>")
        for cell in row:
            bold = header and index == 0
            out.append(f"<w:tc><w:tcPr/><w:p>{_run(str(cell), bold=bold)}</w:p></w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _document_xml(blocks: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            parts.append(_paragraph({"type": "para", "text": str(block)}))
            continue
        kind = block.get("type") or "para"
        if kind == "page_break":
            parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        elif kind == "table":
            parts.append(_table(block))
            parts.append("<w:p/>")
        elif kind == "raw_xml":
            parts.append(str(block.get("xml") or ""))
        else:
            parts.append(_paragraph(block))
    body = "".join(parts)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document {_NS}><w:body>{body}'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
            '</w:sectPr></w:body></w:document>')


def write_docx(path: str | Path, blocks: Iterable[dict[str, Any]]) -> Path:
    """Write a real .docx (OOXML zip) from a small block list."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/styles.xml", _STYLES)
        archive.writestr("word/document.xml", _document_xml(blocks))
    return target


def blocks_to_markdown(blocks: Iterable[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            lines.append(str(block))
            continue
        kind = block.get("type") or "para"
        if kind == "title":
            lines.append(f"# {block.get('text', '')}")
        elif kind == "heading":
            level = min(max(int(block.get("level") or 1), 1), 6)
            lines.append(f"{'#' * (level + 1)} {block.get('text', '')}")
        elif kind == "list":
            lines.extend(f"- {item}" for item in (block.get("items") or []))
        elif kind == "table":
            rows = block.get("rows") or []
            if rows:
                width = max(len(row) for row in rows)
                for index, row in enumerate(rows):
                    cells = [str(c).replace("|", "\\|") for c in row] + [""] * (width - len(row))
                    lines.append("| " + " | ".join(cells) + " |")
                    if index == 0:
                        lines.append("|" + "|".join([" --- "] * width) + "|")
        elif kind == "page_break":
            lines.append("\n---\n")
        else:
            text = block.get("text") or ""
            if text:
                lines.append(text)
        if kind in {"para", "list", "title", "heading"}:
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def blocks_to_html(blocks: Iterable[dict[str, Any]], *, title: str = "Deliverable") -> str:
    out: list[str] = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:Georgia,serif;max-width:52rem;margin:2rem auto;"
        "line-height:1.55;padding:0 1rem}table{border-collapse:collapse;margin:1rem 0}"
        "td,th{border:1px solid #999;padding:.35rem .6rem;vertical-align:top}"
        "th{background:#f2f2f2}code{background:#f6f6f6;padding:0 .2rem}"
        ".meta{color:#666;font-size:.85rem}</style></head><body>",
    ]
    in_list = False
    for block in blocks:
        if not isinstance(block, dict):
            out.append(f"<p>{html.escape(str(block))}</p>")
            continue
        kind = block.get("type") or "para"
        if in_list and kind != "list":
            out.append("</ul>")
            in_list = False
        if kind == "title":
            out.append(f"<h1>{html.escape(block.get('text', ''))}</h1>")
        elif kind == "heading":
            level = min(max(int(block.get("level") or 1), 1), 5) + 1
            out.append(f"<h{level}>{html.escape(block.get('text', ''))}</h{level}>")
        elif kind == "list":
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.extend(f"<li>{html.escape(str(item))}</li>" for item in (block.get("items") or []))
        elif kind == "table":
            out.append("<table>")
            for index, row in enumerate(block.get("rows") or []):
                tag = "th" if index == 0 and block.get("header", True) else "td"
                out.append("<tr>" + "".join(f"<{tag}>{html.escape(str(cell))}</{tag}>"
                                            for cell in row) + "</tr>")
            out.append("</table>")
        elif kind == "page_break":
            out.append('<hr style="page-break-after:always">')
        else:
            text = block.get("text") or ""
            if text:
                out.append(f"<p>{html.escape(text)}</p>")
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def write_editable(out_dir: str | Path, stem: str,
                   blocks: Iterable[dict[str, Any]], *, title: str = "Deliverable") -> dict[str, Any]:
    """Write the same content as .docx + .md + .html and report what was written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    block_list = list(blocks)
    docx_path = write_docx(out / f"{stem}.docx", block_list)
    md_path = out / f"{stem}.md"
    md_path.write_text(blocks_to_markdown(block_list), encoding="utf-8")
    html_path = out / f"{stem}.html"
    html_path.write_text(blocks_to_html(block_list, title=title), encoding="utf-8")
    return {
        "docx": str(docx_path),
        "markdown": str(md_path),
        "html": str(html_path),
        "block_count": len(block_list),
        "bytes": {"docx": docx_path.stat().st_size, "markdown": md_path.stat().st_size,
                  "html": html_path.stat().st_size},
    }
