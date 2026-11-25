#!/usr/bin/env python3
"""Turn an editable deliverable into a real rendered PDF/PNG preview, if possible.

The Harbor runtime contract says editable Office/slide/diagram outputs should also
have real rendered PDF/PNG previews for inspection. The runtime image advertises
resources named office-renderer, pdf-render-tool and latex-compiler, but their exact
command lines were never published - so nothing here assumes a tool exists or that
it takes a particular argument order.

Policy: probe -> try the documented guess -> if the guess fails, try the next
candidate -> if nothing works, say so explicitly in the returned report and let the
caller fall back to an HTML preview. A missing renderer is reported as a limitation,
never silently skipped and never faked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

PROBE_TIMEOUT = 10
RENDER_TIMEOUT = 180

# name -> candidate binaries (first match on PATH wins)
OFFICE_BINARIES = ("soffice", "libreoffice", "lowriter", "office-renderer")
PDF_TOOLS = ("pdftoppm", "pdftocairo", "mutool", "magick", "convert", "gs", "pdf-render-tool")
TEX_BINARIES = ("latexmk", "xelatex", "pdflatex", "lualatex", "tectonic", "latex-compiler")
DOC_CONVERTERS = ("pandoc",)


def _which(name: str) -> str | None:
    return shutil.which(name)


def probe() -> dict[str, Any]:
    """Report which rendering binaries this machine actually has."""
    found = {
        "office": [b for b in OFFICE_BINARIES if _which(b)],
        "pdf_to_image": [b for b in PDF_TOOLS if _which(b)],
        "tex": [b for b in TEX_BINARIES if _which(b)],
        "doc_converter": [b for b in DOC_CONVERTERS if _which(b)],
    }
    found["capabilities"] = {
        "docx_to_pdf": bool(found["office"] or found["doc_converter"]),
        "tex_to_pdf": bool(found["tex"]),
        "pdf_to_png": bool(found["pdf_to_image"]),
    }
    return found


def _run(argv: list[str], *, timeout: int = RENDER_TIMEOUT,
         cwd: str | Path | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout)
        return {"argv": argv, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-1500:], "stderr": (proc.stderr or "")[-1500:]}
    except FileNotFoundError:
        return {"argv": argv, "returncode": None, "error": "not found"}
    except subprocess.TimeoutExpired:
        return {"argv": argv, "returncode": None, "error": f"timeout after {timeout}s"}
    except Exception as exc:  # noqa: BLE001 - a renderer must never crash the run
        return {"argv": argv, "returncode": None, "error": f"{type(exc).__name__}: {exc}"}


def _first_pdf(directory: Path) -> Path | None:
    for candidate in sorted(directory.glob("*.pdf")):
        return candidate
    return None


def _office_to_pdf(src: Path, out_dir: Path) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for binary in OFFICE_BINARIES:
        path = _which(binary)
        if not path:
            continue
        guesses: list[list[str]] = []
        if "render" in binary:
            guesses.append([path, str(src), str(out_dir / f"{src.stem}.pdf")])
        guesses.append([path, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)])
        for argv in guesses:
            attempt = _run(argv)
            attempts.append(attempt)
            if _first_pdf(out_dir):
                return attempts
    for binary in DOC_CONVERTERS:
        path = _which(binary)
        if not path:
            continue
        attempt = _run([path, str(src), "-o", str(out_dir / f"{src.stem}.pdf")])
        attempts.append(attempt)
        if _first_pdf(out_dir):
            return attempts
    return attempts


def _tex_to_pdf(src: Path, out_dir: Path) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for binary in TEX_BINARIES:
        path = _which(binary)
        if not path:
            continue
        if binary == "latexmk":
            argv = [path, "-pdf", "-interaction=nonstopmode", "-halt-on-error",
                    f"-outdir={out_dir}", src.name]
        elif binary == "latex-compiler":
            argv = [path, str(src), str(out_dir)]
        else:
            argv = [path, "-interaction=nonstopmode", "-halt-on-error",
                    f"-output-directory={out_dir}", src.name]
        attempt = _run(argv, cwd=src.parent)
        attempts.append(attempt)
        if _first_pdf(out_dir):
            return attempts
    return attempts


def _pdf_to_png(pdf: Path, out_dir: Path, *, dpi: int = 120) -> dict[str, Any]:
    target = out_dir / f"{pdf.stem}-page1.png"
    for binary in PDF_TOOLS:
        path = _which(binary)
        if not path:
            continue
        if binary in {"pdftoppm", "pdftocairo"}:
            argv = [path, "-png", "-r", str(dpi), "-f", "1", "-singlefile",
                    str(pdf), str(target.with_suffix(""))]
        elif binary == "mutool":
            argv = [path, "draw", "-o", str(target), "-r", str(dpi), str(pdf), "1"]
        elif binary in {"magick", "convert"}:
            argv = [path, "-density", str(dpi), f"{pdf}[0]", str(target)]
        elif binary == "gs":
            argv = [path, "-dNOPAUSE", "-dBATCH", "-sDEVICE=png16m", f"-r{dpi}",
                    f"-sOutputFile={target}", str(pdf)]
        else:  # pdf-render-tool guess
            argv = [path, str(pdf), str(target)]
        attempt = _run(argv)
        if target.is_file() and target.stat().st_size > 0:
            return {"ok": True, "png": str(target), "attempt": attempt}
    return {"ok": False, "png": None, "reason": "no working PDF->PNG renderer"}


def render(src: str | Path, out_dir: str | Path, *, dpi: int = 120) -> dict[str, Any]:
    """Render one editable source into a PDF and a first-page PNG when possible."""
    source = Path(src)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "source": str(source),
        "probe": probe(),
        "attempts": [],
        "pdf": None,
        "png": None,
        "ok": False,
        "reason": "",
    }
    if not source.is_file():
        report["reason"] = f"source not found: {source}"
        return report

    suffix = source.suffix.lower()
    if suffix == ".pdf":
        pdf = source
    elif suffix in {".docx", ".pptx", ".odt", ".doc"}:
        report["attempts"] = _office_to_pdf(source, out)
        pdf = _first_pdf(out)
    elif suffix in {".tex", ".latex"}:
        report["attempts"] = _tex_to_pdf(source, out)
        pdf = _first_pdf(out)
    else:
        pdf = None
        report["reason"] = f"no renderer path for {suffix}"

    if pdf and pdf.is_file():
        report["pdf"] = str(pdf)
        png = _pdf_to_png(pdf, out, dpi=dpi)
        report["png_report"] = png
        if png.get("ok"):
            report["png"] = png["png"]
        report["ok"] = True
    else:
        report["reason"] = (report["reason"] or
                            "rendering unavailable: no office/TeX converter produced a PDF "
                            "in this image; an HTML/text preview was written instead")
    return report


def write_html_preview(html_text: str, out_dir: str | Path, stem: str = "preview") -> Path:
    target = Path(out_dir) / f"{stem}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_text, encoding="utf-8")
    return target
