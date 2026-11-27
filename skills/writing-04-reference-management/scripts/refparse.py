#!/usr/bin/env python3
"""Deterministic parsing helpers for reference / citation management.

This module owns the *reading* half of the skill: text normalisation, LaTeX
unwrapping, bibliography parsers (BibTeX / RIS / EndNote / numbered plain
lists / JSON exports), citation-marker discovery (LaTeX, numeric, author-year,
Chinese variants, superscript numbering) and marker resolution against the
bibliography.

No third-party packages, no network, no model calls: everything here is a pure
function of the input bytes, which is what makes the 3-runs-per-case comparison
reproducible. English comments; the Chinese SKILL.md is the user-facing manual.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable

WS_RE = re.compile(r"\s+")

# Accent marks are referenced by hex escape so this source file never has to
# embed a literal backtick (which also keeps the file tooling-friendly).
ACCENT_CHARS = "\x22\x27\x60\x5e\x7e\x3d\x63\x76\x75\x48"
_ACCENTS = {
    "\x22": {"a": "a", "e": "e", "i": "i", "o": "o", "u": "u", "y": "y"},
    "\x27": {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "y": "ý",
             "A": "Á", "E": "É", "I": "Í", "O": "Ó", "U": "Ú", "Y": "Ý"},
    "\x60": {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù",
             "A": "À", "E": "È", "I": "Ì", "O": "Ò", "U": "Ù"},
    "\x5e": {"a": "â", "e": "ê", "i": "î", "o": "ô", "u": "û"},
    "\x7e": {"a": "ã", "n": "ñ", "o": "õ", "A": "Ã", "N": "Ñ", "O": "Õ"},
    "c": {"c": "ç", "C": "Ç"},
    "v": {"s": "š", "c": "č", "z": "ž", "S": "Š", "C": "Č", "Z": "Ž"},
    "=": {"a": "ā", "e": "ē", "i": "ī", "o": "ō", "u": "ū"},
    "u": {"a": "ă", "e": "ĕ", "i": "ĭ", "o": "ŏ", "u": "ŭ"},
    "H": {"o": "ő", "u": "ű", "O": "Ő", "U": "Ű"},
}
_ACCENT_RE = re.compile(r"\\([\x22\x27\x60\x5e\x7e\x3d\x63\x76\x75\x48])\s*\{?\s*([A-Za-z])\s*\}?")
_TEXTCMD_RE = re.compile(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?(?:\{([^{}]*)\})?")
_CMD_NOARG_RE = re.compile(r"\\[A-Za-z]+\*?")


def normalize_space(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("\u00ad", "").replace("\ufeff", "")
    return WS_RE.sub(" ", text).strip()


def strip_latex(text: str) -> str:
    """Turn a LaTeX / escaped field value into readable plain text."""
    if not text:
        return ""
    s = str(text)
    s = s.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_")
    s = s.replace("\\#", "#").replace("\\$", "$")
    s = s.replace("\\{", "{").replace("\\}", "}")
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\:", " ")
    s = s.replace("\\ ", " ").replace("\\!", "")
    s = s.replace("~", " ").replace("---", "\u2014").replace("--", "\u2013")

    def _accent(match):
        mark, letter = match.group(1), match.group(2)
        return (_ACCENTS.get(mark) or {}).get(letter, letter)

    s = _ACCENT_RE.sub(_accent, s)
    for _ in range(3):
        new = _TEXTCMD_RE.sub(lambda m: m.group(1) if m.group(1) is not None else "", s)
        if new == s:
            break
        s = new
    s = _CMD_NOARG_RE.sub("", s)
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\$([^$]*)\$", r"\1", s)
    return normalize_space(s)


def norm_title(text: str) -> str:
    flat = strip_latex(text or "")
    flat = unicodedata.normalize("NFKC", flat).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", flat)


def norm_doi(raw: str) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    s = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", s)
    s = re.sub(r"(?i)^doi:\s*", "", s)
    s = s.strip().rstrip(".,;\u3001\uff0c\uff1b\u3002)]}>")
    m = re.search(r"(10\.\d{4,9}/[^\s\"<>]+)", s)
    return m.group(1).lower() if m else ""


def norm_key(raw: str) -> str:
    # CJK is kept: author-year citations in Chinese resolve through family names,
    # and stripping them made every "张三（2020）" marker unresolvable.
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (raw or "").casefold())


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    total = len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", text))
    return round(cjk / total, 4) if total else 0.0


def detect_language(text: str) -> str:
    ratio = cjk_ratio(text)
    if ratio >= 0.25:
        return "zh"
    if ratio > 0.02:
        return "mixed"
    return "en"


# --------------------------------------------------------------------------- #
# authors
# --------------------------------------------------------------------------- #
_ET_AL_RE = re.compile(r"(?i)\bet\s+al\.?|\u7b49$|\betc\.?")
_GB_AUTHOR_RE = re.compile(r"^([A-Z][A-Za-z\-' ]*[A-Za-z])\s+((?:[A-Z]\.?\s*)+)$")
_INITIAL_RE = re.compile(r"^(?:[A-Z]\.?|[A-Z]\.?[A-Z]\.?)$")


APA_PAIRS_RE = re.compile(r"([A-Z][A-Za-z\u00c0-\u024f\-' ]+?),\s*((?:[A-Z]\.\s*)+)")


def parse_authors(raw: str) -> list:
    """Best-effort author list; never invents a name it cannot separate.

    Handles the four shapes a real bibliography export uses:
    * BibTeX      "Smith, John and Jones, Mary"   (surname, given)
    * GB/T        "SMITH J, JONES M"              (SURNAME Initials)
    * APA         "Smith, J., & Jones, M."        (surname, initials)
    * Chinese     "张三, 李四"                     (surname first)
    """
    text = normalize_space(str(raw or ""))
    if not text:
        return []
    text = text.replace("\uff1b", ";").replace("\uff0c", ",").replace("\u3001", ";")
    text = _ET_AL_RE.sub("", text)
    text = re.sub(r"(?i)\s+and\s+", ";", text)
    text = text.replace("&", ";")

    # APA style: "Family, I." pairs are unambiguous once separators are folded.
    pairs = list(APA_PAIRS_RE.finditer(text))
    if pairs:
        covered = sum(match.end() - match.start() for match in pairs)
        if covered >= max(6, int(len(text) * 0.55)):
            return [{"family": match.group(1).strip(), "given": normalize_space(match.group(2))}
                    for match in pairs]

    chunks = [chunk for chunk in text.split(";") if chunk.strip()]
    if len(chunks) == 1:
        only = chunks[0]
        parts = [part.strip() for part in only.split(",") if part.strip()]
        if len(parts) >= 2 and all(_GB_AUTHOR_RE.match(part) for part in parts):
            chunks = parts
        elif len(parts) >= 2 and all(
                len(re.findall(r"[\u4e00-\u9fff]", part)) >= 2
                and not re.search(r"[A-Za-z]", part) for part in parts):
            chunks = parts

    authors = []
    for chunk in chunks:
        chunk = chunk.strip().strip(".,")
        if not chunk:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff\s]{2,20}", chunk):
            authors.append({"family": chunk.replace(" ", ""), "given": ""})
            continue
        inner = list(APA_PAIRS_RE.finditer(chunk))
        if inner:
            for match in inner:
                authors.append({"family": match.group(1).strip(),
                                "given": normalize_space(match.group(2))})
            continue
        match = _GB_AUTHOR_RE.match(chunk)
        if match:
            authors.append({"family": match.group(1).strip().title(),
                            "given": normalize_space(match.group(2))})
            continue
        if "," in chunk:
            family, given = chunk.split(",", 1)
            authors.append({"family": family.strip(), "given": given.strip()})
            continue
        tokens = chunk.split()
        if len(tokens) == 1:
            authors.append({"family": tokens[0], "given": ""})
            continue
        if _INITIAL_RE.match(tokens[-1]) or len(tokens[-1].replace(".", "")) <= 2:
            authors.append({"family": " ".join(tokens[:-1]), "given": tokens[-1]})
        else:
            authors.append({"family": tokens[-1], "given": " ".join(tokens[:-1])})
    return authors


def first_surname(entry: dict) -> str:
    authors = entry.get("authors") or []
    return (authors[0].get("family") if authors else "") or ""


# --------------------------------------------------------------------------- #
# BibTeX
# --------------------------------------------------------------------------- #
BIB_TYPE_MAP = {
    "article": "journal", "inproceedings": "conference", "conference": "conference",
    "proceedings": "conference", "book": "book", "incollection": "chapter",
    "inbook": "chapter", "phdthesis": "thesis", "mastersthesis": "thesis",
    "techreport": "report", "misc": "misc", "online": "web", "www": "web",
    "unpublished": "preprint", "preprint": "preprint", "software": "misc",
    "dataset": "misc", "patent": "patent", "standard": "standard",
}
_BIB_FIELD_ALIASES = {
    "journal": "journal", "journaltitle": "journal", "booktitle": "booktitle",
    "author": "authors_raw", "editor": "editor", "title": "title",
    "year": "year", "date": "date", "volume": "volume", "number": "issue",
    "issue": "issue", "pages": "pages", "doi": "doi", "url": "url",
    "eprint": "eprint", "archiveprefix": "archive_prefix", "publisher": "publisher",
    "address": "address", "location": "address", "school": "school",
    "institution": "school", "note": "note", "keywords": "keywords",
    "abstract": "abstract", "edition": "edition", "series": "series",
    "isbn": "isbn", "issn": "issn",
}


def _scan_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the matching close character (quote aware)."""
    depth = 0
    in_quote = False
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return len(text)


def _bib_fields(body: str):
    depth = 0
    in_quote = False
    key_end = None
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == "," and depth == 0:
                key_end = i
                break
        i += 1
    if key_end is None:
        return "", {}
    key = body[:key_end].strip()
    rest = body[key_end + 1:]
    fields = {}
    pos = 0
    while pos < len(rest):
        while pos < len(rest) and rest[pos] in " \t\r\n,":
            pos += 1
        match = re.match(r"([A-Za-z][A-Za-z0-9_\-]*)\s*=", rest[pos:])
        if not match:
            nxt = rest.find(",", pos)
            if nxt < 0:
                break
            pos = nxt + 1
            continue
        name = match.group(1).lower()
        pos += match.end()
        while pos < len(rest) and rest[pos] in " \t\r\n":
            pos += 1
        if pos >= len(rest):
            break
        if rest[pos] == "{":
            end = _scan_balanced(rest, pos, "{", "}")
            value = rest[pos + 1:end - 1]
            pos = end
        elif rest[pos] == '"':
            end = pos + 1
            while end < len(rest) and not (rest[end] == '"' and rest[end - 1] != "\\"):
                end += 1
            value = rest[pos + 1:end]
            pos = end + 1
        else:
            start = pos
            while pos < len(rest) and rest[pos] not in ",\n\r":
                pos += 1
            value = rest[start:pos]
        fields[name] = value.strip()
        while pos < len(rest) and rest[pos] != ",":
            pos += 1
    return key, fields


def parse_bibtex(text: str, source_path: str = "") -> list:
    """Parse every regular entry of a .bib file; @string/@comment are skipped."""
    out = []
    pattern = re.compile(r"@([A-Za-z]+)\s*([{(])")
    pos = 0
    while True:
        match = pattern.search(text, pos)
        if not match:
            break
        etype = match.group(1).lower()
        open_ch = match.group(2)
        close_ch = "}" if open_ch == "{" else ")"
        end = _scan_balanced(text, match.end() - 1, open_ch, close_ch)
        pos = max(end, match.end() + 1)
        if etype in {"comment", "string", "preamble"}:
            continue
        key, fields = _bib_fields(text[match.end():end - 1])
        if not key and not fields:
            continue
        raw = {"ref_key": key, "type": BIB_TYPE_MAP.get(etype, "misc"),
               "source_format": "bibtex", "raw": text[match.start():end].strip()}
        for name, value in fields.items():
            mapped = _BIB_FIELD_ALIASES.get(name)
            if mapped and mapped not in raw:
                raw[mapped] = value.strip() if mapped in {"url", "doi"} else strip_latex(value)
        if raw.get("date") and not raw.get("year"):
            ym = re.search(r"((?:19|20)\d{2})", str(raw.get("date")))
            if ym:
                raw["year"] = ym.group(1)
        out.append(raw)
    return out


# --------------------------------------------------------------------------- #
# RIS / EndNote
# --------------------------------------------------------------------------- #
RIS_TYPE_MAP = {
    "JOUR": "journal", "JFULL": "journal", "EJOUR": "journal", "MGZN": "journal",
    "NEWS": "journal", "CONF": "conference", "CPAPER": "conference",
    "BOOK": "book", "EBOOK": "book", "CHAP": "chapter", "ECHAP": "chapter",
    "THES": "thesis", "RPRT": "report", "ELEC": "web", "GEN": "misc",
    "UNPB": "preprint", "PAT": "patent", "STAND": "standard", "DATA": "misc",
    "COMP": "misc", "SER": "misc",
}
_RIS_LINE_RE = re.compile(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$")


def parse_ris(text: str, source_path: str = "") -> list:
    """Parse a RIS export; multiple records per file are supported."""
    records = []
    current = None
    for line in text.splitlines():
        match = _RIS_LINE_RE.match(line.strip())
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "TY":
            if current is not None:
                records.append(current)
            current = {"_type": [value]}
            continue
        if current is None:
            continue
        if tag in {"ER", "E3"}:
            records.append(current)
            current = None
            continue
        current.setdefault(tag, []).append(value)
    if current is not None:
        records.append(current)

    def first(rec, *tags):
        for tag in tags:
            if rec.get(tag):
                return rec[tag][0]
        return ""

    out = []
    for rec in records:
        rtype = (rec.get("_type") or [""])[0].upper()
        raw = {
            "type": RIS_TYPE_MAP.get(rtype, "misc"),
            "authors_raw": "; ".join(rec.get("AU") or rec.get("A1") or []),
            "title": first(rec, "TI", "T1", "CT"),
            "journal": first(rec, "JO", "JF", "T2", "JA", "J1"),
            "booktitle": first(rec, "T2", "BT") if RIS_TYPE_MAP.get(rtype) in {"conference", "chapter"} else "",
            "year": first(rec, "PY", "Y1", "DA")[:4],
            "volume": first(rec, "VL"),
            "issue": first(rec, "IS"),
            "pages": (first(rec, "SP") + ("-" + first(rec, "EP") if first(rec, "EP") else "")) if first(rec, "SP") else "",
            "doi": first(rec, "DO"),
            "url": first(rec, "UR"),
            "publisher": first(rec, "PB"),
            "address": first(rec, "CY"),
            "keywords": "; ".join(rec.get("KW") or []),
            "abstract": first(rec, "AB", "N2"),
            "source_format": "ris",
            "raw": " | ".join(str(tag) + ": " + str(vals[0]) for tag, vals in rec.items() if tag != "_type"),
        }
        if not re.fullmatch(r"(?:19|20)\d{2}", raw["year"] or ""):
            ym = re.search(r"((?:19|20)\d{2})", first(rec, "PY", "Y1", "DA"))
            raw["year"] = ym.group(1) if ym else ""
        out.append(raw)
    return out


EN_TYPE_MAP = {
    "journal article": "journal", "book": "book", "book section": "chapter",
    "conference paper": "conference", "conference proceedings": "conference",
    "thesis": "thesis", "report": "report", "web page": "web",
    "generic": "misc", "unpublished work": "preprint", "preprint": "preprint",
}


def parse_endnote(text: str, source_path: str = "") -> list:
    """Parse EndNote tagged text (%A author, %T title, ...)."""
    groups = []
    current = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^(%[0-9A-Za-z@])\s*(.*)$", line)
        if not match:
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "%0" and current:
            groups.append(current)
            current = {}
        current.setdefault(tag, []).append(value)
    if current:
        groups.append(current)
    out = []
    for rec in groups:
        def first(*tags):
            for tag in tags:
                if rec.get(tag):
                    return rec[tag][0]
            return ""
        raw = {
            "type": EN_TYPE_MAP.get(first("%0").casefold(), "misc"),
            "authors_raw": "; ".join(rec.get("%A") or []),
            "title": first("%T"),
            "journal": first("%J", "%B"),
            "booktitle": first("%B") if first("%0").casefold() in {"book section", "conference paper"} else "",
            "year": first("%D")[:4],
            "volume": first("%V"),
            "issue": first("%N"),
            "pages": first("%P"),
            "doi": first("%R"),
            "url": first("%U"),
            "publisher": first("%I"),
            "address": first("%C"),
            "keywords": "; ".join(rec.get("%K") or []),
            "abstract": first("%X"),
            "source_format": "endnote",
            "raw": " | ".join(str(tag) + ": " + str(vals[0]) for tag, vals in rec.items()),
        }
        out.append(raw)
    return out


# --------------------------------------------------------------------------- #
# plain numbered reference list (GB/T 7714 or APA-like)
# --------------------------------------------------------------------------- #
_NUMBER_START_RE = re.compile(r"(?m)^[ \t]*(?:\[(\d{1,3})\]|(\d{1,3})[.)\u3001])\s+")
_TYPE_MARK_RE = re.compile(r"\[(J|M|C|D|R|S|P|N|G|Z|A|EB/OL|J/OL|M/CD|C/OL|DB/OL|CP/DK)\]")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})([a-z])?(?!\d)")
_DOI_RE = re.compile(r"(?i)(?:doi\s*[:\uff1a]\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s,;\uff0c\uff1b\u3002]+)")
_PAGES_RE = re.compile(r"(?<!\d)(\d{1,6})\s*[-\u2013\u2014~]\s*(\d{1,6})(?!\d)")
_VOL_ISSUE_RE = re.compile(r"(?<!\d)(\d{1,4})\s*[\(\uff08]\s*([0-9A-Za-z\-\u2013/]+)\s*[\)\uff09]")
_EN_YEAR_PAREN_RE = re.compile(r"\(((?:19|20)\d{2})([a-z])?\)")


def _looks_like_reference(text: str) -> bool:
    text = normalize_space(text)
    if len(text) < 25:
        return False
    return bool(_YEAR_RE.search(text)) and (len(re.findall(r"[.\u3002]", text)) >= 1
                                            or cjk_ratio(text) > 0.2)


def _trim_venue(venue: str) -> str:
    """Cut the volume/year/page tail off a venue string (keeps just the name)."""
    text = str(venue or "").strip()
    for pattern in (r"[,，]\s*(?:19|20)\d{2}\b",
                    r"[,，]\s*\d{1,4}\s*[\(（]",
                    r"[,，]\s*\d{1,6}\s*[-\u2013\u2014~]\s*\d{1,6}"):
        match = re.search(pattern, text)
        if match:
            return text[:match.start()].strip(" ,，.。")
    return text.strip(" ,，.。")


def _parse_single_plain(number: int, raw: str) -> dict:
    raw = normalize_space(raw)
    raw = re.sub(r"^[\s.\u3002]+", "", raw)
    entry = {
        "source_format": "plain", "raw": raw, "old_number": number,
        "title": "", "authors_raw": "", "journal": "", "booktitle": "",
        "publisher": "", "address": "", "year": "", "volume": "", "issue": "",
        "pages": "", "doi": "", "url": "", "keywords": "", "abstract": "",
        "year_suffix": "",
    }
    dm = _DOI_RE.search(raw)
    if dm:
        entry["doi"] = dm.group(1).rstrip(".")
        raw = raw.replace(dm.group(0), " ")
    for ym in _YEAR_RE.finditer(raw):
        if 1800 <= int(ym.group(1)) <= 2099:
            entry["year"] = ym.group(1)
            entry["year_suffix"] = ym.group(2) or ""
            break

    tmark = _TYPE_MARK_RE.search(raw)
    if tmark:
        mark = tmark.group(1)
        entry["type"] = {"J": "journal", "M": "book", "C": "conference",
                         "D": "thesis", "R": "report", "S": "standard",
                         "P": "patent", "N": "journal", "G": "misc",
                         "Z": "misc", "A": "journal", "EB/OL": "web",
                         "J/OL": "journal", "M/CD": "book", "C/OL": "conference",
                         "DB/OL": "web", "CP/DK": "misc"}.get(mark, "misc")
        head = raw[:tmark.start()].rstrip(" .\u3002")
        tail = raw[tmark.end():]
        segments = re.split(r"(?<=[.\u3002])\s*", head, maxsplit=1)
        if len(segments) == 2:
            entry["authors_raw"] = segments[0].strip(" .\u3002")
            entry["title"] = segments[1].strip(" .\u3002")
        else:
            entry["title"] = segments[0].strip(" .\u3002")
        venue = re.sub(r"^[.\u3002//\s]+", "", tail).strip()
        if entry["type"] == "conference" and "//" in tail:
            after = tail.split("//", 1)[1].strip()
            head_match = re.match(r"([^.\u3002]+)[.\u3002]\s*(.*)$", after)
            entry["booktitle"] = (head_match.group(1) if head_match else after).strip()
            venue = (head_match.group(2) if head_match else "").strip()
        elif entry["type"] in {"book", "chapter"}:
            pm = re.search(r"([^,:\uff0c\uff1a]+?)[:\uff1a]\s*([^,\uff0c]+?)[,\uff0c]\s*((?:19|20)\d{2})", venue)
            if pm:
                entry["address"] = pm.group(1).strip()
                entry["publisher"] = pm.group(2).strip()
            entry["booktitle"] = venue
        if entry["type"] == "conference":
            publication = re.search(r"([^,，:：]+?)[:：]\s*([^,，]+?)[,，]\s*((?:19|20)\d{2})", venue)
            if publication:
                entry["address"] = publication.group(1).strip()
                entry["publisher"] = publication.group(2).strip()
        if entry["type"] in {"journal", "conference"}:
            if entry["type"] == "journal":
                entry["journal"] = _trim_venue(venue)
            vi = _VOL_ISSUE_RE.search(venue)
            if vi:
                entry["volume"], entry["issue"] = vi.group(1), vi.group(2)
            pg = _PAGES_RE.search(venue)
            if pg:
                entry["pages"] = pg.group(1) + "-" + pg.group(2)
        entry["venue_raw"] = venue
        return entry

    pm = _EN_YEAR_PAREN_RE.search(raw)
    if pm and pm.start() > 8:
        entry["year"] = pm.group(1)
        entry["year_suffix"] = pm.group(2) or ""
        entry["authors_raw"] = raw[:pm.start()].strip(" .\u3002")
        rest = raw[pm.end():].strip(" .\u3002")
        segments = re.split(r"(?<=[.\u3002])\s+", rest, maxsplit=1)
        entry["title"] = segments[0].strip(" .\u3002")
        venue = segments[1].strip(" .\u3002") if len(segments) > 1 else ""
        entry["journal"] = _trim_venue(venue)
        vi = _VOL_ISSUE_RE.search(venue)
        if vi:
            entry["volume"], entry["issue"] = vi.group(1), vi.group(2)
        pg = _PAGES_RE.search(venue)
        if pg:
            entry["pages"] = pg.group(1) + "-" + pg.group(2)
        entry["type"] = "journal"
        entry["venue_raw"] = venue
        return entry

    segments = re.split(r"(?<=[.\u3002])\s*", raw, maxsplit=2)
    if len(segments) >= 2:
        entry["authors_raw"] = segments[0].strip(" .\u3002")
        entry["title"] = segments[1].strip(" .\u3002")
        entry["journal"] = segments[2].strip(" .\u3002") if len(segments) > 2 else ""
    else:
        entry["title"] = raw
    entry["type"] = "journal" if entry["year"] else "misc"
    entry["venue_raw"] = entry.get("journal", "")
    return entry


def _looks_like_entry_start(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped or len(stripped) < 20:
        return False
    if re.match(r"^(?:\[\d{1,3}\]|\d{1,3}[.)\u3001])\s", stripped):
        return True
    if re.match(r"^[A-Z\u4e00-\u9fff][^\n]{0,90}?\((?:19|20)\d{2}[a-z]?\)\.", stripped):
        return True
    if re.match(r"^[A-Z\u4e00-\u9fff]", stripped) and _TYPE_MARK_RE.search(stripped[:260]):
        return True
    return False


_INLINE_BRACKET_RE = re.compile(r"\[\s*(\d{1,3})\s*\]")


def parse_plain_reference_list(text: str, source_path: str = "") -> list:
    """Parse a numbered reference list; unnumbered lists fall back to starts.

    A PDF text layer frequently flattens a whole reference list onto a single
    line ("[1] ... [2] ... [3] ..."), so the strict line-start scan below sees
    exactly one entry and every in-text marker beyond [1] then resolves to
    nothing. When the strict scan finds fewer than two entries, retry with the
    bracketed marker wherever it is preceded by whitespace or sentence
    punctuation; a title that itself contains "[3]" is rare and the trace stays
    visible in citation_map.
    """
    text = unicodedata.normalize("NFKC", text or "")
    spans: list[tuple[int, int, int]] = []
    for match in _NUMBER_START_RE.finditer(text):
        spans.append((match.start(), match.end(), int(match.group(1) or match.group(2))))
    if len(spans) < 2:
        inline = [match for match in _INLINE_BRACKET_RE.finditer(text)
                  if match.start() == 0 or text[match.start() - 1] in " \t\r\n;:."]
        if len(inline) >= 2:
            spans = [(match.start(), match.end(), int(match.group(1))) for match in inline]
    out = []
    if len(spans) >= 2:
        for index, (start, end, number) in enumerate(spans):
            next_start = spans[index + 1][0] if index + 1 < len(spans) else len(text)
            raw = text[end:next_start]
            entry = _parse_single_plain(number, raw)
            entry["source_format"] = "plain"
            out.append(entry)
        return out

    # Unnumbered list: a new entry usually starts at a line that begins with a
    # capitalized author run followed by "(2020)." (APA) or contains a GB/T type
    # marker. Continuation lines stay with the entry they belong to.
    lines = text.splitlines()
    anchors = [index for index, line in enumerate(lines) if _looks_like_entry_start(line)]
    blocks = []
    if len(anchors) >= 2:
        for position, index in enumerate(anchors):
            end = anchors[position + 1] if position + 1 < len(anchors) else len(lines)
            blocks.append(" ".join(line.strip() for line in lines[index:end] if line.strip()))
    else:
        blocks = [par for par in re.split(r"\n\s*\n", text) if par.strip()]
        if not blocks:
            blocks = [line for line in lines if line.strip()]
    number = 0
    for block in blocks:
        if not _looks_like_reference(block):
            continue
        number += 1
        entry = _parse_single_plain(number, block)
        entry["source_format"] = "plain"
        out.append(entry)
    return out


def parse_json_bibliography(data: Any, source_path: str = "") -> list:
    """Accept list / dict shaped JSON exports (name heuristics only)."""
    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("references", "entries", "items", "records", "bibliography", "data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = [value for value in data.values() if isinstance(value, dict)]
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        def get(*names):
            for name in names:
                if row.get(name) not in (None, ""):
                    return str(row[name])
            return ""

        authors = row.get("authors_raw") or row.get("author") or row.get("authors") or ""
        if isinstance(authors, list):
            authors = "; ".join(
                (item.get("family", "") + (", " + item.get("given", "") if item.get("given") else ""))
                if isinstance(item, dict) else str(item) for item in authors)
        raw = {
            "ref_key": get("id", "key", "ref_key", "citation_key"),
            "type": get("type", "entry_type") or "misc",
            "authors_raw": authors,
            "title": get("title", "name"),
            "journal": get("journal", "venue", "container_title", "publication"),
            "booktitle": get("booktitle", "proceedings"),
            "year": get("year", "date", "published")[:4],
            "volume": get("volume", "vol"),
            "issue": get("issue", "number", "num"),
            "pages": get("pages", "page", "pagination"),
            "doi": get("doi", "DOI"),
            "url": get("url", "link"),
            "publisher": get("publisher"),
            "address": get("address", "place", "location"),
            "keywords": row.get("keywords") or "",
            "abstract": get("abstract", "summary"),
            "source_format": "json",
            "raw": json.dumps(row, ensure_ascii=False)[:1200],
        }
        out.append(raw)
    return out


def sniff_format(text: str, suffix: str = "") -> str:
    sample = (text or "")[:20000]
    stripped = sample.lstrip()
    if re.search(r"(?m)^\s*@[A-Za-z]+\s*[{(]", sample):
        return "bibtex"
    if re.search(r"(?m)^TY\s{1,2}-", sample):
        return "ris"
    if re.search(r"(?m)^%0\s", sample):
        return "endnote"
    if stripped.startswith("[") or stripped.startswith("{"):
        return "json"
    if suffix.lower() in {".bib", ".bibtex"}:
        return "bibtex"
    if suffix.lower() == ".ris":
        return "ris"
    if suffix.lower() in {".enw", ".endnote"}:
        return "endnote"
    if suffix.lower() == ".json":
        return "json"
    return "plain"


def parse_bibliography_text(text: str, suffix: str = "", source_path: str = ""):
    """Detect the export format of one file and parse it; returns (format, entries)."""
    fmt = sniff_format(text, suffix)
    if fmt == "bibtex":
        return fmt, parse_bibtex(text, source_path)
    if fmt == "ris":
        return fmt, parse_ris(text, source_path)
    if fmt == "endnote":
        return fmt, parse_endnote(text, source_path)
    if fmt == "json":
        try:
            data = json.loads(text)
        except Exception:
            return "json-unparsed", []
        return fmt, parse_json_bibliography(data, source_path)
    return fmt, parse_plain_reference_list(text, source_path)


# --------------------------------------------------------------------------- #
# reference section inside a manuscript
# --------------------------------------------------------------------------- #
REF_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\d+(?:\.\d+)*\.?[ \t]*)?"
    r"(references|reference list|bibliography|works cited|literature cited|"
    r"\u53c2\u8003\u6587\u732e|\u5f15\u7528\u6587\u732e|\u6587\u732e\u76ee\u5f55|\u53c3\u8003\u6587\u737b)[ \t]*[:\uff1a]?[ \t]*$")


def find_reference_section(text: str) -> dict:
    """Split a manuscript into body and embedded reference section."""
    matches = list(REF_HEADING_RE.finditer(text or ""))
    for match in reversed(matches):
        start = match.end()
        tail = text[start:]
        numbered = len(_NUMBER_START_RE.findall(tail))
        entry_like = numbered >= 1 or _looks_like_reference(tail[:400])
        if not entry_like:
            continue
        return {
            "found": True,
            "heading": normalize_space(match.group(0)),
            "heading_start": match.start(),
            "body": text[:match.start()],
            "reference_text": tail,
        }
    return {"found": False, "heading": "", "heading_start": None,
            "body": text or "", "reference_text": ""}


# --------------------------------------------------------------------------- #
# citation markers
# --------------------------------------------------------------------------- #
LATEX_CITE_RE = re.compile(r"\\([A-Za-z]*cite[A-Za-z]*)\*?(?:\[[^\]]*\]){0,2}\{([^{}]*)\}")
NUMERIC_RE = re.compile(r"(?<![A-Za-z0-9\[\uff3b])[\[\uff3b](\d{1,3}(?:\s*[-\u2013\u2014\uff0c,]\s*\d{1,3})*)[\]\uff3d](?!\s*\()")
SUP_LATEX_RE = re.compile(r"\^\s*\{?\s*[\[\uff3b]?\s*(\d{1,3}(?:\s*[-\u2013]\s*\d{1,3})*)\s*[\]\uff3d]?\s*\}?")
PAREN_RE = re.compile(r"[\(\uff08]([^()\uff08\uff09]{2,180})[\)\uff09]")
NARRATIVE_RE = re.compile(
    r"(?<![A-Za-z])([A-Z][A-Za-z\-']+(?:\s+et\s+al\.?)?(?:\u7b49)?|[\u4e00-\u9fff]{2,8}\u7b49?)\s*[\(\uff08]\s*((?:19|20)\d{2}[a-z]?)\s*[\)\uff09]")
SUP_DIGITS = str.maketrans("\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079", "0123456789")
SUP_CHARS_RE = re.compile(r"([\u2070\u00b9\u00b2\u00b3\u2074-\u2079]{1,6}(?:\s*[,\uff0c\-\u2013]\s*[\u2070\u00b9\u00b2\u00b3\u2074-\u2079]{1,6})?)")
AUTHOR_ITEM_RE = re.compile(r"^(?P<author>.+?)[\s,\uff0c]*(?P<year>(?:19|20)\d{2})(?P<suffix>[a-z])?$")
AUTHOR_TOKEN_RE = re.compile(r"[A-Z][A-Za-z\-']+|[\u4e00-\u9fff]{2,10}")


def expand_numeric(group: str) -> list:
    """Expand '1,3-5' / '1, 3-5' into [1, 3, 4, 5] preserving order."""
    numbers = []
    for part in re.split(r"[,\uff0c]", group or ""):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^(\d{1,3})\s*[-\u2013\u2014]\s*(\d{1,3})$", part)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start <= end and end - start <= 200:
                numbers.extend(range(start, end + 1))
            else:
                numbers.extend([start, end])
            continue
        if part.isdigit():
            numbers.append(int(part))
    seen = set()
    out = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            out.append(number)
    return out


def _parse_author_year_items(inner: str) -> list:
    items = []
    parts = re.split(r"[;\uff1b]", inner) if (";" in inner or "\uff1b" in inner) else [inner]
    for part in parts:
        part = normalize_space(part).strip(" ,\uff0c;\uff1b")
        if not part:
            continue
        match = AUTHOR_ITEM_RE.match(part)
        if not match:
            continue
        author = normalize_space(match.group("author"))
        author = re.sub(r"(?i)\bet\s+al\.?$", "", author).strip(" ,\uff0c")
        author = re.sub(r"(?i)^(?:and|&)\s+", "", author).strip()
        tokens = AUTHOR_TOKEN_RE.findall(author)
        if not tokens:
            continue
        items.append({"author": tokens[0], "author_text": author,
                      "year": match.group("year"), "suffix": match.group("suffix") or ""})
    return items


def find_citation_markers(text: str) -> list:
    """Find every in-text citation marker with its character span.

    Precedence matters: a LaTeX marker may contain brackets, a numeric marker
    must not be a markdown link, and an author-year group must not overlap a
    number that belongs to the surrounding prose. Candidates are accepted in
    precedence order and overlapping ones are dropped.
    """
    candidates = []
    if not text:
        return candidates

    for match in LATEX_CITE_RE.finditer(text):
        command = match.group(1).lower()
        payload = match.group(2).strip()
        keys = [key.strip() for key in payload.split(",") if key.strip()]
        if not keys:
            continue
        candidates.append({"start": match.start(), "end": match.end(),
                           "raw": match.group(0), "style": "latex", "keys": keys,
                           "nocite": command == "nocite", "precedence": 4})

    for match in NUMERIC_RE.finditer(text):
        numbers = expand_numeric(match.group(1))
        if not numbers:
            continue
        candidates.append({"start": match.start(), "end": match.end(),
                           "raw": match.group(0), "style": "numeric",
                           "numbers": numbers, "precedence": 3})

    for match in SUP_LATEX_RE.finditer(text):
        before = text[match.start() - 1] if match.start() > 0 else ""
        if before.isascii() and before.isalnum():
            continue  # exponent in a formula (10^{2}), not a citation
        numbers = expand_numeric(match.group(1))
        if not numbers:
            continue
        candidates.append({"start": match.start(), "end": match.end(),
                           "raw": match.group(0), "style": "numeric_superscript",
                           "numbers": numbers, "precedence": 3})

    for match in SUP_CHARS_RE.finditer(text):
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end():match.end() + 1]
        # m2, s-1, cm3 exponents are units, not citation markers
        if before.isascii() and before.isalnum():
            continue
        if after in {"/", "^", "-", "+"} and not after.isdigit():
            continue
        numbers = expand_numeric(match.group(1).translate(SUP_DIGITS))
        if not numbers:
            continue
        candidates.append({"start": match.start(), "end": match.end(),
                           "raw": match.group(0), "style": "numeric_superscript",
                           "numbers": numbers, "precedence": 2})

    for match in PAREN_RE.finditer(text):
        inner = match.group(1)
        if not re.search(r"(?:19|20)\d{2}", inner):
            continue
        items = _parse_author_year_items(inner)
        if not items:
            continue
        candidates.append({"start": match.start(), "end": match.end(),
                           "raw": match.group(0), "style": "author_year",
                           "items": items, "narrative": False, "precedence": 1})

    for match in NARRATIVE_RE.finditer(text):
        author = normalize_space(match.group(1))
        author = re.sub(r"(?i)\s*et\s+al\.?$", "", author).strip()
        tokens = AUTHOR_TOKEN_RE.findall(author)
        if not tokens:
            continue
        year = match.group(2)
        candidates.append({"start": match.start(), "end": match.end(),
                           "raw": match.group(0), "style": "author_year_narrative",
                           "items": [{"author": tokens[0], "author_text": author,
                                      "year": year[:4], "suffix": year[4:] or ""}],
                           "narrative": True, "precedence": 2})

    candidates.sort(key=lambda cand: (cand["start"], -cand["precedence"], -(cand["end"] - cand["start"])))
    accepted = []
    occupied = []
    for cand in candidates:
        if cand.get("nocite"):
            accepted.append(cand)
            occupied.append((cand["start"], cand["end"]))
            continue
        if any(cand["start"] < end and start < cand["end"] for start, end in occupied):
            continue
        accepted.append(cand)
        occupied.append((cand["start"], cand["end"]))
    accepted.sort(key=lambda cand: cand["start"])
    for index, cand in enumerate(accepted, start=1):
        cand["cite_id"] = "c%04d" % index
    return accepted


# --------------------------------------------------------------------------- #
# marker resolution
# --------------------------------------------------------------------------- #
def build_entry_lookup(entries: list) -> dict:
    lookup = {}
    for entry in entries:
        for key in [entry.get("ref_key")] + list(entry.get("aliases") or []):
            nk = norm_key(str(key or ""))
            if nk:
                lookup.setdefault(nk, entry)
    return lookup


def match_author_year(entries: list, author: str, year: str) -> list:
    """Entries whose first author and year match an author-year citation."""
    target = norm_key(author)
    if not target:
        return []
    matches = []
    for entry in entries:
        if str(entry.get("year") or "")[:4] != str(year)[:4]:
            continue
        families = [norm_key(item.get("family", "")) for item in (entry.get("authors") or [])]
        families = [family for family in families if family]
        if not families:
            continue
        if (families[0] == target or families[0].startswith(target)
                or target.startswith(families[0])):
            matches.append(entry)
            continue
        for family in families[1:]:
            if family == target:
                matches.append(entry)
                break
    return matches


def _disambiguate(candidates: list, suffix: str) -> dict:
    ordered = sorted(candidates, key=lambda entry: (str(entry.get("title") or ""),
                                                    str(entry.get("ref_key") or "")))
    if suffix:
        index = ord(suffix) - ord("a")
        if 0 <= index < len(ordered):
            return ordered[index]
    return ordered[0]


def resolve_markers(markers: list, entries: list, number_map: dict) -> dict:
    """Attach resolved entries to every marker; leave honest gaps unresolved."""
    lookup = build_entry_lookup(entries)
    unresolved = []
    for marker in markers:
        targets = []
        if marker["style"] == "latex":
            for key in marker.get("keys") or []:
                entry = lookup.get(norm_key(key))
                if entry:
                    targets.append({"ref_key": entry["ref_key"], "via": "cite_key:" + key})
                else:
                    unresolved.append({"cite_id": marker["cite_id"], "raw": marker.get("raw"),
                                       "reason": "cite key not in bibliography: " + key})
        elif marker["style"] in {"numeric", "numeric_superscript"}:
            for number in marker.get("numbers") or []:
                entry = number_map.get(int(number))
                if entry:
                    targets.append({"ref_key": entry["ref_key"], "via": "number:%d" % int(number)})
                else:
                    unresolved.append({"cite_id": marker["cite_id"], "raw": marker.get("raw"),
                                       "reason": "number not in reference list: %d" % int(number)})
        else:
            for item in marker.get("items") or []:
                candidates = match_author_year(entries, item.get("author", ""), item.get("year", ""))
                if len(candidates) == 1:
                    targets.append({"ref_key": candidates[0]["ref_key"],
                                    "via": "author_year:" + str(item.get("author")) + "," + str(item.get("year"))})
                elif len(candidates) > 1:
                    chosen = _disambiguate(candidates, item.get("suffix", ""))
                    targets.append({"ref_key": chosen["ref_key"],
                                    "via": "author_year:" + str(item.get("author")) + "," + str(item.get("year")),
                                    "ambiguous": True})
                else:
                    unresolved.append({"cite_id": marker["cite_id"], "raw": marker.get("raw"),
                                       "reason": "author-year not in bibliography: "
                                                 + str(item.get("author")) + " " + str(item.get("year"))})
        unique = []
        seen = set()
        for target in targets:
            if target["ref_key"] in seen:
                continue
            seen.add(target["ref_key"])
            unique.append(target)
        marker["targets"] = unique
        marker["resolved"] = bool(unique)
    return {"markers": markers, "unresolved": unresolved,
            "resolved_count": sum(1 for marker in markers if marker.get("resolved")),
            "total": len(markers)}


# --------------------------------------------------------------------------- #
# sentences and claim terms
# --------------------------------------------------------------------------- #
_ABBREV = {"al", "fig", "figs", "eq", "eqs", "ref", "refs", "no", "pp", "p", "vs",
           "e.g", "i.e", "etc", "dr", "mr", "mrs", "ms", "prof", "sec", "vol",
           "approx", "ca", "cf", "ed", "eds", "resp", "suppl", "tab"}
_SENT_STOP = set(".!?\u3002\uff01\uff1f;\uff1b")


def split_sentences(text: str) -> list:
    """Split into sentences while keeping character offsets.

    Offsets are what lets finalize rewrite the manuscript without re-generating
    it: only citation markers are replaced, every other character is copied
    verbatim. Boundaries inside LaTeX braces or math are skipped so a formula or
    a cite payload can never be cut in half.
    """
    text = text or ""
    out = []
    start = 0
    brace = 0
    in_math = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\\" and i + 1 < length:
            i += 2
            continue
        if ch == "$":
            in_math = not in_math
        elif ch == "{" and not in_math:
            brace += 1
        elif ch == "}" and not in_math:
            brace = max(0, brace - 1)
        elif ch == "\n" and brace == 0 and not in_math:
            following = text[i + 1:]
            if following.startswith("\n") or following.lstrip().startswith("#"):
                end = i + 1
                chunk = text[start:end]
                if chunk.strip():
                    out.append({"start": start, "end": end, "text": chunk})
                start = end
                i = end
                continue
        elif ch in _SENT_STOP and brace == 0 and not in_math:
            if ch == ".":
                prev = re.search(r"([A-Za-z.]+)$", text[:i])
                word = (prev.group(1) if prev else "").casefold().strip(".")
                if word in _ABBREV:
                    i += 1
                    continue
            end = i + 1
            while end < length and text[end] in " \t\r\n":
                end += 1
            chunk = text[start:end]
            if chunk.strip():
                out.append({"start": start, "end": end, "text": chunk})
            start = end
            i = end
            continue
        i += 1
    if start < length and text[start:].strip():
        out.append({"start": start, "end": length, "text": text[start:]})
    for index, sentence in enumerate(out, start=1):
        sentence["sentence_id"] = "s%04d" % index
        sentence["text"] = normalize_space(sentence["text"])
    return out


CLAIM_STOP = {
    "the", "and", "with", "that", "this", "these", "those", "from", "into", "were",
    "have", "been", "which", "their", "there", "however", "therefore", "although",
    "between", "within", "using", "used", "based", "also", "such", "more", "than",
    "figure", "table", "equation", "section", "respectively", "shown", "results",
    "study", "paper", "method", "methods", "data", "analysis", "conclusion",
    "can", "may", "might", "will", "would", "could", "should", "was", "are", "for",
    "but", "not", "our", "its", "has", "had", "all", "any", "one", "two", "three",
    "here", "thus", "then", "when", "where", "while", "after", "before", "under",
    "over", "about", "them", "they", "we", "you", "use", "used", "show", "shows",
    "shown", "found", "find", "report", "reported", "suggest", "suggests",
}
_CJK_CLAIM_STOP = set("\u7684\u4e86\u548c\u4e0e\u5728\u662f\u6709\u4e3a\u5bf9\u4e2d\u4e0a\u4e0b\u4e5f\u90fd\u800c\u53ca\u6216\u7b49\u8fd9\u90a3\u5176\u4e4b\u4ee5\u88ab\u5c06\u628a")


def content_terms(text: str, top: int = 12) -> list:
    """Deterministic content-term list used for claim/reference lexical matching."""
    norm = normalize_space(strip_latex(text or ""))
    counts = {}
    order = {}
    for word in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", norm):
        low = word.casefold()
        if low in CLAIM_STOP or len(low) < 3:
            continue
        counts[low] = counts.get(low, 0) + 1
        order.setdefault(low, len(order))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", norm):
        if len(run) <= 6:
            counts[run] = counts.get(run, 0) + 1
            order.setdefault(run, len(order))
        else:
            for index in range(len(run) - 1):
                bigram = run[index:index + 2]
                if bigram in _CJK_CLAIM_STOP:
                    continue
                counts[bigram] = counts.get(bigram, 0) + 1
                order.setdefault(bigram, len(order))
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]], kv[0]))
    return [term for term, _count in ranked[:top]]


def term_overlap(left: Iterable[str], right: Iterable[str]) -> dict:
    lset = {term.casefold() for term in left if term}
    rset = {term.casefold() for term in right if term}
    shared = sorted(lset & rset)
    union = lset | rset
    return {"shared": shared, "shared_count": len(shared),
            "jaccard": round(len(shared) / len(union), 4) if union else 0.0,
            "coverage": round(len(shared) / len(lset), 4) if lset else 0.0}


NEEDS_CITATION_MARKERS = (
    "%", "percent", "significant", "significantly", "increase", "increased",
    "decrease", "decreased", "higher", "lower", "correlat", "associated",
    "demonstrat", "reveal", "indicat", "observed", "found that", "report",
    "shown that", "suggest", "consistent with", "compared with",
    "\u8868\u660e", "\u663e\u793a", "\u53d1\u73b0", "\u8bc1\u660e", "\u663e\u8457",
    "\u63d0\u9ad8", "\u964d\u4f4e", "\u589e\u52a0", "\u51cf\u5c11", "\u76f8\u5173",
    "\u4e00\u81f4", "\u76f8\u6bd4", "\u4f18\u4e8e", "\u9ad8\u4e8e", "\u4f4e\u4e8e", "\u7ed3\u679c",
)
BOILERPLATE = (
    "this paper is organized", "the remainder of this", "in this section",
    "\u672c\u6587\u7ed3\u6784", "\u672c\u6587\u5b89\u6392", "\u5982\u4e0b", "next section",
    "following section", "as shown in table", "as shown in figure", "\u8be6\u89c1",
    "\u5982\u56fe", "\u5982\u8868",
)


def needs_citation(sentence: str) -> bool:
    """Heuristic: does this sentence make a factual claim that needs a source?

    Only used to build the insertion worksheet - it proposes, the agent decides.
    """
    text = normalize_space(sentence)
    if len(text) < 40:
        return False
    low = text.casefold()
    if any(pattern in low for pattern in BOILERPLATE):
        return False
    if re.search(r"[?!\uff1f\uff01]$", text):
        return False
    has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|mol|wt|K|\u00b0C|nm|um|mm|mg|g|h|min|s|eV|MPa|GPa)?", text))
    has_marker = any(marker in low for marker in NEEDS_CITATION_MARKERS)
    return (has_number and (has_marker or len(text) > 80)) or (has_marker and len(text) > 60)


# --------------------------------------------------------------------------- #
# entry normalisation
# --------------------------------------------------------------------------- #
REQUIRED_BY_TYPE = {
    "journal": ["authors", "title", "journal", "year", "volume", "pages"],
    "conference": ["authors", "title", "booktitle", "year", "pages"],
    "book": ["authors", "title", "publisher", "year"],
    "chapter": ["authors", "title", "booktitle", "year", "publisher"],
    "thesis": ["authors", "title", "school", "year"],
    "report": ["authors", "title", "publisher", "year"],
    "preprint": ["authors", "title", "year"],
    "web": ["authors", "title", "url", "year"],
    "misc": ["title"],
    "patent": ["authors", "title", "year"],
    "standard": ["title", "year"],
}


def _make_ref_key(authors: list, year: str, title: str, used: set, index: int) -> str:
    family = norm_key(authors[0].get("family", "")) if authors else ""
    base = (family + year) if family else ("ref%03d%s" % (index, year))
    if not base.strip():
        base = "ref%03d" % index
    key = base
    suffix = ord("a")
    while key in used:
        key = base + chr(suffix)
        suffix += 1
    return key


def normalize_entry(raw: dict, source: dict, index: int, used_keys: set) -> dict:
    """Turn a parser-shaped record into the canonical internal entry shape."""
    authors_raw = str(raw.get("authors_raw") or raw.get("authors") or "")
    authors = parse_authors(authors_raw)
    title = strip_latex(str(raw.get("title") or ""))
    year = str(raw.get("year") or "")
    year_match = re.search(r"(?:19|20)\d{2}", year)
    year = year_match.group(0) if year_match else ""
    year_suffix = str(raw.get("year_suffix") or "")
    if not year_suffix:
        suffix_match = re.search(r"(?:19|20)\d{2}([a-z])", str(raw.get("year") or ""))
        year_suffix = suffix_match.group(1) if suffix_match else ""

    key = str(raw.get("ref_key") or "").strip()
    if not key or norm_key(key) in used_keys:
        key = _make_ref_key(authors, year, title, used_keys, index)
    used_keys.add(norm_key(key))

    entry = {
        "ref_key": key,
        "aliases": [key],
        "type": str(raw.get("type") or "misc").lower(),
        "authors": authors,
        "authors_raw": normalize_space(authors_raw),
        "title": title,
        "title_norm": norm_title(title),
        "year": year,
        "year_suffix": year_suffix or "",
        "journal": strip_latex(str(raw.get("journal") or "")),
        "booktitle": strip_latex(str(raw.get("booktitle") or "")),
        "publisher": strip_latex(str(raw.get("publisher") or "")),
        "address": strip_latex(str(raw.get("address") or "")),
        "school": strip_latex(str(raw.get("school") or "")),
        "edition": strip_latex(str(raw.get("edition") or "")),
        "volume": strip_latex(str(raw.get("volume") or "")),
        "issue": strip_latex(str(raw.get("issue") or "")),
        "pages": strip_latex(str(raw.get("pages") or "")),
        "doi": norm_doi(str(raw.get("doi") or "")),
        "url": str(raw.get("url") or "").strip(),
        "eprint": str(raw.get("eprint") or "").strip(),
        "keywords": normalize_space(str(raw.get("keywords") or "")),
        "abstract": normalize_space(str(raw.get("abstract") or "")),
        "note": normalize_space(str(raw.get("note") or "")),
        "language": detect_language(title + " " + authors_raw),
        "source": {"file": source.get("file", ""),
                   "format": raw.get("source_format") or source.get("format", ""),
                   "raw": str(raw.get("raw") or "")[:4000],
                   "old_number": raw.get("old_number")},
        "old_number": raw.get("old_number"),
        "parse_status": "parsed" if title else "raw_only",
        "merged_from": [],
        "field_sources": {},
        "flags": [],
    }
    if not title:
        entry["flags"].append("missing_title")
    required = REQUIRED_BY_TYPE.get(entry["type"], ["title"])
    missing = []
    for field in required:
        value = entry.get(field)
        filled = bool(value) if isinstance(value, list) else bool(str(value or "").strip())
        if not filled:
            missing.append(field)
    entry["missing_fields"] = missing
    entry["completeness"] = round(1 - len(missing) / len(required), 4) if required else 1.0
    return entry
