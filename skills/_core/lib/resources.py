#!/usr/bin/env python3
"""Zero-dependency reader for the platform resource manifest (environment/resources.yaml).

Why this module exists
----------------------
The Harbor/Paper2Task task profile tells authors to enumerate every bundled
resource in environment/resources.yaml so the solving Agent can see *what* it was
given and where it lives. A Skill that ignores that entry point silently
re-discovers material by file suffix, and on a case whose manuscript is named
unconventionally it can pick the wrong document (or miss a supplementary file the
case declares). Reading the manifest turns "whatever happens to be in the folder"
into "the resources the task declares", which is both more correct and directly
visible in the result.

Why a hand-written reader
-------------------------
The platform deliberately supports only a *conservative* YAML subset (a top-level
non-empty "resources" list with plain "key: value" fields) and adds no YAML
runtime dependency. A real YAML parser is unavailable in the budgeted container,
so this module implements exactly that subset, strictly and line by line:

* tolerated : UTF-8 with or without BOM, "#" full-line and trailing comments,
  single/double quoted scalars, block sequences ("- name: x" followed by indented
  keys, or a bare "-" then keys), blank lines, "---" markers, tabs, CRLF endings;
* rejected  : anything that is not a plain scalar (nested maps, flow collections)
  is recorded as an error instead of being guessed at, and a "path" that is
  absolute, uses ".." or resolves outside environment/ is recorded and never
  read. A suspected credential literal / signed URL is recorded too, because the
  task profile forbids one in the manifest.

Public API
----------
find_manifest(roots) -> Path | None
    Deterministically locate environment/resources.yaml under any root.
load_manifest(path, environment_root=None) -> dict
    {resources, existing, missing, external, tools, errors, warnings, source,
    environment_root, parsed} - path entries are resolved relative to
    environment_root (default: the manifest's own directory).
summarize(manifest) -> dict
    Compact, JSON-safe digest for the result metalog and run reports.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

MANIFEST_NAMES = ("resources.yaml", "resources.yml")

# The plain fields the platform documents for one resource entry. Unknown
# fields are kept as warnings, not errors: the guide says "fields such as", so a
# future additive field must not break an otherwise usable manifest.
PLAIN_FIELDS = ("name", "path", "url", "description",
                "credential_source", "credential_env")

# A resource is treated as an executable/tool candidate when its name or
# description talks about a tool, or when its file name is a known renderer.
TOOL_WORDS = ("tool", "renderer", "render", "compiler", "compile", "office",
              "latex", "pandoc", "soffice", "libreoffice", "pdftoppm",
              "pdftocairo", "mutool", "ghostscript", "executable", "binary",
              "cli", "converter")
KNOWN_TOOL_BASENAMES = ("soffice", "libreoffice", "lowriter", "office-renderer",
                        "pandoc", "latexmk", "xelatex", "pdflatex", "lualatex",
                        "tectonic", "latex-compiler", "pdftoppm", "pdftocairo",
                        "mutool", "magick", "convert", "gs", "pdf-render-tool")

# A credential literal (or an expiring signed URL) must never appear in the
# manifest. Only the names / environment-variable declarations are allowed.
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|sk-[A-Za-z0-9]{8,}"
    r"|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S{6,}"
    r"|X-Amz-Signature=|X-Amz-Credential=|sig=|se=\d{8}T)"
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

MAX_ERRORS = 24
MAX_WARNINGS = 24


# --------------------------------------------------------------------------- #
# manifest discovery
# --------------------------------------------------------------------------- #
def _candidate_paths(root: Path) -> list[Path]:
    """Every place a manifest may live for one input root, most specific first."""
    if root.is_file():
        root = root.parent
    return [
        root / "resources.yaml", root / "resources.yml",
        root / "environment" / "resources.yaml",
        root / "environment" / "resources.yml",
    ]


def find_manifest(roots: Iterable[Any]) -> Path | None:
    """Locate the resource manifest under any of the given roots.

    Deterministic: roots are visited in the order given and the first existing
    candidate wins, so repeated runs on one input resolve the same file.
    """
    seen: set[str] = set()
    for raw in roots or []:
        if not raw:
            continue
        try:
            root = Path(str(raw))
        except Exception:
            continue
        for candidate in _candidate_paths(root):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


# --------------------------------------------------------------------------- #
# the conservative YAML subset
# --------------------------------------------------------------------------- #
def _clip(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _strip_comment(line: str) -> str:
    """Drop a trailing "#" comment, but never inside a quoted scalar."""
    out: list[str] = []
    quote = ""
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            continue
        if char == "#" and (not out or out[-1] in " \t" or not "".join(out).strip()):
            break
        out.append(char)
    return "".join(out).rstrip()


def _parse_quoted(raw: str) -> tuple[str, str | None]:
    """Return (value, error) for one scalar; quotes are removed when balanced."""
    text = (raw or "").strip()
    if not text:
        return "", None
    first = text[0]
    if first in {"'", '"'}:
        if len(text) >= 2 and text[-1] == first:
            return text[1:-1].strip(), None
        return text[1:].strip(), "unterminated quoted value %r" % _clip(text)
    return text, None


def _assign(target: dict[str, str], body: str, lineno: int,
            errors: list[str], warnings: list[str]) -> None:
    if ":" not in body:
        errors.append("line %d: expected 'key: value' inside a resource entry, got %r"
                      % (lineno, _clip(body)))
        return
    key_raw, _, value_raw = body.partition(":")
    key = key_raw.strip().strip("'\"")
    if not key:
        errors.append("line %d: empty field name" % lineno)
        return
    if key.casefold() not in PLAIN_FIELDS:
        warnings.append("line %d: unsupported field %r kept as text "
                        "(supported: %s)" % (lineno, key, ", ".join(PLAIN_FIELDS)))
        return
    value, error = _parse_quoted(value_raw)
    if error:
        errors.append("line %d: %s" % (lineno, error))
    canon = key.casefold()
    if canon in target and target[canon]:
        errors.append("line %d: duplicate field %r in one resource entry" % (lineno, key))
        return
    target[canon] = value


def parse_manifest_text(text: str) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """Parse the conservative YAML subset; returns (entries, errors, warnings)."""
    entries: list[dict[str, str]] = []
    errors: list[str] = []
    warnings: list[str] = []
    lines = (text or "").splitlines()
    index = 0
    found_resources = False
    resources_indent = 0
    while index < len(lines):
        raw_line = lines[index]
        lineno = index + 1
        index += 1
        line = _strip_comment(raw_line.expandtabs(4))
        if not line.strip() or line.strip() in {"---", "..."}:
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not found_resources:
            if ":" not in stripped:
                errors.append("line %d: expected a top-level 'key: value' entry, got %r"
                              % (lineno, _clip(stripped)))
                continue
            key, _, value = stripped.partition(":")
            key = key.strip().strip("'\"")
            if key != "resources":
                # Other top-level keys are not part of the resource entry point.
                continue
            found_resources = True
            resources_indent = indent
            if value.strip():
                errors.append("line %d: 'resources' must introduce a block sequence, "
                              "not an inline value (%r)" % (lineno, _clip(value)))
            continue
        # inside "resources:"
        is_item = stripped == "-" or stripped.startswith("- ")
        if is_item:
            body = stripped[1:].strip()
            current: dict[str, str] = {}
            entries.append(current)
            resources_indent = indent
            if body:
                _assign(current, body, lineno, errors, warnings)
            continue
        if indent <= resources_indent:
            # A new top-level block ends the sequence; only "resources" matters.
            found_resources = False
            index -= 1  # re-read this line outside the block
            continue
        if not entries:
            errors.append("line %d: content appears before the first resource "
                          "item (%r)" % (lineno, _clip(stripped)))
            continue
        _assign(entries[-1], stripped, lineno, errors, warnings)
    return entries, errors[:MAX_ERRORS], warnings[:MAX_WARNINGS]


# --------------------------------------------------------------------------- #
# path safety
# --------------------------------------------------------------------------- #
def _relative_parts(raw: str) -> tuple[list[str], str | None]:
    """Split a declared relative path; reject absolute/escaping forms."""
    text = (raw or "").strip().replace("\\", "/")
    if not text:
        return [], "empty path"
    if _WINDOWS_DRIVE_RE.match(text):
        return [], "absolute Windows path is not allowed"
    if text.startswith(("/", "~", "file:")):
        return [], "absolute path is not allowed"
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return [], "path must not contain '..'"
    return parts, None


def _inside(root: Path, target: Path) -> bool:
    try:
        root_s = str(root.resolve())
        target_s = str(target.resolve())
    except Exception:
        return False
    return target_s == root_s or target_s.startswith(root_s + os.sep)


def _looks_like_tool(entry: dict[str, Any]) -> bool:
    name = str(entry.get("name") or "").casefold()
    description = str(entry.get("description") or "").casefold()
    path = str(entry.get("path") or "")
    basename = path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    haystack = name + " " + description
    if any(word in haystack for word in TOOL_WORDS):
        return True
    if basename in KNOWN_TOOL_BASENAMES:
        return True
    if basename.endswith((".exe", ".cmd", ".bat", ".sh")):
        return True
    return False


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_manifest(path: Any, environment_root: Any = None) -> dict[str, Any]:
    """Parse one manifest and resolve every declared path beneath the root."""
    manifest_path = Path(str(path))
    env_root = Path(str(environment_root)) if environment_root else manifest_path.parent
    result: dict[str, Any] = {
        "source": str(manifest_path),
        "environment_root": str(env_root),
        "resources": [],
        "existing": [],
        "missing": [],
        "external": [],
        "tools": [],
        "errors": [],
        "warnings": [],
        "parsed": False,
    }
    try:
        text = manifest_path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:  # a reader never raises at the caller
        result["errors"].append("manifest could not be read: %s" % type(exc).__name__)
        return result

    entries, errors, warnings = parse_manifest_text(text)
    result["errors"].extend(errors)
    result["warnings"].extend(warnings)
    if not entries:
        result["errors"].append("no resource entries were found under the top-level "
                                "'resources' key")

    for position, entry in enumerate(entries, start=1):
        item: dict[str, Any] = dict(entry)
        item["index"] = position
        name = (entry.get("name") or entry.get("path") or entry.get("url")
                or "resource-%d" % position)
        item["name"] = name
        # credential hygiene: a literal secret or a signed/expiring URL is an error
        for field in ("credential_source", "credential_env", "url", "description"):
            value = entry.get(field) or ""
            if value and _SECRET_RE.search(value):
                result["errors"].append(
                    "resource %r: field %r looks like a credential literal or a "
                    "signed/expiring URL; only names and env-var declarations are "
                    "allowed" % (_clip(name, 40), field))
        declared_env = entry.get("credential_env") or ""
        if declared_env and not _ENV_NAME_RE.match(declared_env):
            result["warnings"].append(
                "resource %r: credential_env %r is not a plain environment variable "
                "name" % (_clip(name, 40), _clip(declared_env, 40)))

        path_raw = entry.get("path") or ""
        url_raw = entry.get("url") or ""
        if path_raw:
            parts, problem = _relative_parts(path_raw)
            if problem:
                item["status"] = "rejected"
                item["reason"] = problem
                result["errors"].append("resource %r: %s (%r)"
                                        % (_clip(name, 40), problem, _clip(path_raw, 80)))
            else:
                target = env_root.joinpath(*parts)
                if not _inside(env_root, target):
                    item["status"] = "rejected"
                    item["reason"] = "resolves outside environment/"
                    result["errors"].append("resource %r resolves outside environment/"
                                            % _clip(name, 40))
                elif target.is_file():
                    item["status"] = "existing"
                    item["absolute"] = str(target)
                    item["bytes"] = _size_of(target)
                    result["existing"].append(item)
                else:
                    item["status"] = "missing"
                    result["missing"].append(item)
        elif url_raw:
            item["status"] = "external"
            result["external"].append(item)
        else:
            item["status"] = "declared"
            result["warnings"].append("resource %r declares neither a path nor a url"
                                      % _clip(name, 40))

        if _looks_like_tool(item):
            absolute = item.get("absolute")
            tool = {"name": name, "path": path_raw or None, "url": url_raw or None,
                    "declared": item.get("status") != "rejected",
                    "exists": item.get("status") == "existing",
                    "absolute": absolute,
                    "executable": bool(absolute and os.path.isfile(str(absolute)))}
            result["tools"].append(tool)

        result["resources"].append(item)

    result["parsed"] = bool(entries)
    return result


def _size_of(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def summarize(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Compact JSON-safe digest for result.json / run_report.json."""
    if not manifest:
        return {"present": False, "note": "no resources.yaml manifest"}
    resources = []
    for item in manifest.get("resources") or []:
        resources.append({
            "name": item.get("name"),
            "path": item.get("path"),
            "url": item.get("url"),
            "description": item.get("description"),
            "status": item.get("status"),
            "bytes": item.get("bytes"),
        })
    return {
        "present": True,
        "entry_point": "environment/resources.yaml",
        "source": manifest.get("source"),
        "environment_root": manifest.get("environment_root"),
        "counts": {
            "declared": len(resources),
            "existing": len(manifest.get("existing") or []),
            "missing": len(manifest.get("missing") or []),
            "external": len(manifest.get("external") or []),
            "tools": len(manifest.get("tools") or []),
        },
        "resources": resources,
        "existing": [item.get("name") for item in manifest.get("existing") or []],
        "missing": [{"name": item.get("name"), "path": item.get("path")}
                    for item in manifest.get("missing") or []],
        "external": [{"name": item.get("name"), "url": item.get("url")}
                     for item in manifest.get("external") or []],
        "tools": manifest.get("tools") or [],
        "errors": (manifest.get("errors") or [])[:10],
        "warnings": (manifest.get("warnings") or [])[:10],
        "note": ("declared resources are read from environment/resources.yaml; a "
                 "declared path that is absent is reported, never guessed at"),
    }


def manifest_paths(manifest: dict[str, Any] | None) -> list[Path]:
    """Absolute paths of the declared resources that really exist."""
    out: list[Path] = []
    for item in (manifest or {}).get("existing") or []:
        absolute = item.get("absolute")
        if not absolute:
            continue
        try:
            path = Path(str(absolute))
        except Exception:
            continue
        if path.is_file():
            out.append(path)
    return out


def tool_dirs(manifest: dict[str, Any] | None) -> list[Path]:
    """Directories of declared tools that exist, so a probe can find them on PATH."""
    out: list[Path] = []
    seen: set[str] = set()
    for tool in (manifest or {}).get("tools") or []:
        absolute = tool.get("absolute")
        if not absolute or not tool.get("exists"):
            continue
        try:
            directory = Path(str(absolute)).parent
        except Exception:
            continue
        key = str(directory)
        if key not in seen and directory.is_dir():
            seen.add(key)
            out.append(directory)
    return out


if __name__ == "__main__":  # tiny CLI: useful when debugging a case by hand
    import json
    import sys

    roots = sys.argv[1:] or [os.getcwd()]
    found = find_manifest(roots)
    payload = load_manifest(found, found.parent) if found else {
        "source": None, "resources": [], "existing": [], "missing": [],
        "tools": [], "errors": ["no resources.yaml manifest under %s" % roots],
        "warnings": []}
    print(json.dumps(summarize(payload), ensure_ascii=False, indent=2))
