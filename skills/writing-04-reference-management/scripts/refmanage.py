#!/usr/bin/env python3
"""Deterministic reference-management logic: dedupe, formatting, renumbering.

The reading half lives in refparse.py. This module owns the *management* half:

* duplicate detection with explicit, reviewable criteria (same DOI, same
  normalised title, preprint vs published version, near-identical title) and a
  conservative policy: anything that could be a false merge becomes a
  needs-review item instead of a merge;
* GB/T 7714-2015 (numeric) and APA 7th (author-year) formatters built from the
  parsed fields only - no field is invented to make an entry look complete;
* citation renumbering (assignment by order of first appearance) and in-text
  marker rewriting for both conventions, including numeric <-> author-year
  conversion;
* consistency checks, metadata-issue detection and the deterministic
  claim-to-reference matching baseline used when the agent writes no judgement.

Standard library only, deterministic ordering, no network and no model calls.
"""

from __future__ import annotations

import re
from typing import Any

import refparse as R

# --------------------------------------------------------------------------- #
# title similarity
# --------------------------------------------------------------------------- #
_TITLE_TOKEN_STOP = {
    "the", "and", "for", "with", "from", "into", "under", "over", "study",
    "studies", "using", "based", "new", "novel", "toward", "towards", "via",
    "their", "this", "that", "these", "those", "between", "among", "effect",
    "effects", "role", "roles", "approach", "analysis",
}
PREPRINT_MARKERS = ("arxiv", "biorxiv", "medrxiv", "chemrxiv", "techrxiv",
                    "ssrn", "preprint", "research square", "osf", "psyarxiv",
                    "eartharxiv", "authorea", "vixra", "hal preprint")


def is_preprint(entry: dict[str, Any]) -> bool:
    haystack = " ".join(str(entry.get(field) or "") for field in
                        ("journal", "booktitle", "publisher", "note", "type", "url", "eprint"))
    haystack = haystack.casefold()
    return any(marker in haystack for marker in PREPRINT_MARKERS) or entry.get("type") == "preprint"


def _title_tokens(title: str) -> set[str]:
    text = R.strip_latex(title or "").casefold()
    return {token for token in re.findall(r"[a-z][a-z0-9\-]{2,}", text)
            if token not in _TITLE_TOKEN_STOP}


def _char_bigrams(text: str) -> set[str]:
    flat = R.norm_title(text)
    return {flat[index:index + 2] for index in range(len(flat) - 1)} if len(flat) >= 2 else set()


def title_similarity(left: str, right: str) -> float:
    """Symmetric similarity in [0, 1]; reproducible and explainable in words."""
    lflat, rflat = R.norm_title(left), R.norm_title(right)
    if not lflat or not rflat:
        return 0.0
    if lflat == rflat:
        return 1.0
    shorter, longer = (lflat, rflat) if len(lflat) <= len(rflat) else (rflat, lflat)
    if len(shorter) >= 12 and shorter in longer:
        return round(min(0.98, 0.86 + 0.12 * len(shorter) / len(longer)), 4)
    ltok, rtok = _title_tokens(left), _title_tokens(right)
    token_score = len(ltok & rtok) / len(ltok | rtok) if (ltok | rtok) else 0.0
    lbg, rbg = _char_bigrams(left), _char_bigrams(right)
    bigram_score = len(lbg & rbg) / len(lbg | rbg) if (lbg | rbg) else 0.0
    return round(max(token_score, bigram_score), 4)


def _year_int(entry: dict[str, Any]) -> int | None:
    try:
        return int(str(entry.get("year") or "")[:4])
    except (TypeError, ValueError):
        return None


def _field_filled(entry: dict[str, Any], field: str) -> bool:
    value = entry.get(field)
    return bool(value) if isinstance(value, list) else bool(str(value or "").strip())


def _is_bilingual_pair(left: dict[str, Any], right: dict[str, Any]) -> bool:
    lzh = R.cjk_ratio(left.get("title") or "") >= 0.3
    rzh = R.cjk_ratio(right.get("title") or "") >= 0.3
    return lzh != rzh

def _venue_tokens(entry: dict[str, Any]) -> set:
    text = " ".join(str(entry.get(field) or "") for field in ("journal", "booktitle", "publisher"))
    text = R.strip_latex(text).casefold()
    return {token for token in re.findall(r"[a-z][a-z0-9\-]{2,}", text)
            if token not in _TITLE_TOKEN_STOP and token not in {"journal", "science", "research"}}


def _bilingual_evidence(left: dict[str, Any], right: dict[str, Any]) -> list:
    """Bibliographic overlap that can justify flagging a Chinese/English pair.

    A Chinese title and a Latin title cannot be compared with the same similarity
    function, so the decision uses the other fields. Two or more shared signals are
    required; otherwise every Chinese paper of the same year would be flagged, and
    noise is worse than a missed duplicate here.
    """
    evidence = []
    if _year_int(left) is not None and _year_int(left) == _year_int(right):
        evidence.append("same_year")
    left_pages = _page_range(str(left.get("pages") or ""))
    right_pages = _page_range(str(right.get("pages") or ""))
    if left_pages and left_pages == right_pages:
        evidence.append("same_pages")
    if left.get("volume") and str(left["volume"]) == str(right.get("volume") or ""):
        evidence.append("same_volume")
    if left.get("issue") and str(left["issue"]) == str(right.get("issue") or ""):
        evidence.append("same_issue")
    shared_venues = sorted(_venue_tokens(left) & _venue_tokens(right))
    if shared_venues:
        evidence.append("shared_venue_token:" + shared_venues[0])
    signals = set(evidence)
    if "same_year" not in signals:
        return []
    if not ({"same_pages", "shared_venue_token"} & signals):
        return []
    if not ({"same_volume", "same_issue"} & signals):
        return []
    return evidence



# --------------------------------------------------------------------------- #
# duplicate detection
# --------------------------------------------------------------------------- #
class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def find_duplicates(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect duplicate entries with explicit criteria; never merge when unsure.

    Returns groups (mergeable), needs_review (a human/agent must decide) and
    conflicts (different DOIs on the same title - a metadata error, not a merge).
    """
    size = len(entries)
    uf = _UnionFind(size)
    edge_criteria: dict[tuple[int, int], dict[str, Any]] = {}
    needs_review: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    def record(left: int, right: int, confidence: float, criteria: str,
               rationale: str) -> None:
        key = (min(left, right), max(left, right))
        previous = edge_criteria.get(key)
        if previous is None or confidence > previous["confidence"]:
            edge_criteria[key] = {"confidence": confidence, "criteria": criteria,
                                  "rationale": rationale}
        if confidence >= 0.9:
            uf.union(left, right)

    # 1) same DOI - the strongest possible signal
    by_doi: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.get("doi"):
            by_doi.setdefault(entry["doi"], []).append(index)
    for doi, indexes in sorted(by_doi.items()):
        for position in range(1, len(indexes)):
            record(indexes[0], indexes[position], 1.0, "same_doi",
                   "identical normalised DOI " + doi)

    # 2) identical normalised title
    by_title: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.get("title_norm") and len(entry["title_norm"]) >= 8:
            by_title.setdefault(entry["title_norm"], []).append(index)
    for title_norm, indexes in sorted(by_title.items()):
        for position in range(1, len(indexes)):
            left, right = entries[indexes[0]], entries[indexes[position]]
            if left.get("doi") and right.get("doi") and left["doi"] != right["doi"]:
                conflicts.append({
                    "entry_a": left["ref_key"], "entry_b": right["ref_key"],
                    "criterion": "same_title_different_doi",
                    "detail": "titles normalise identically but DOIs differ (%s vs %s); "
                              "one record is a metadata error - resolve manually"
                              % (left["doi"], right["doi"]),
                })
                continue
            record(indexes[0], indexes[position], 0.95, "same_normalized_title",
                   "titles identical after normalisation: " + title_norm[:80])

    # 3) near-duplicate pairs: preprint/published, near-identical titles
    for left in range(size):
        for right in range(left + 1, size):
            a, b = entries[left], entries[right]
            if uf.find(left) == uf.find(right):
                continue
            if _is_bilingual_pair(a, b):
                evidence = _bilingual_evidence(a, b)
                if evidence:
                    needs_review.append({
                        "entry_a": a["ref_key"], "entry_b": b["ref_key"],
                        "criterion": "possible_bilingual_duplicate",
                        "confidence": round(min(0.6, 0.3 + 0.1 * len(evidence)), 2),
                        "detail": "one title is Chinese and one is not; shared bibliographic "
                                  "signals (" + ", ".join(evidence) + ") suggest a "
                                  "Chinese/English version of the same work - not merged "
                                  "because the two titles cannot be compared mechanically",
                        "evidence": evidence,
                        "title_a": a.get("title"), "title_b": b.get("title"),
                    })
                continue
            similarity = title_similarity(a.get("title", ""), b.get("title", ""))
            if similarity < 0.72:
                continue
            same_author = bool(R.norm_key(R.first_surname(a))
                               and R.norm_key(R.first_surname(a)) == R.norm_key(R.first_surname(b)))
            year_a, year_b = _year_int(a), _year_int(b)
            year_close = (year_a is not None and year_b is not None
                          and abs(year_a - year_b) <= 1)
            both_doi_conflict = bool(a.get("doi") and b.get("doi") and a["doi"] != b["doi"])
            preprint_pair = is_preprint(a) != is_preprint(b)

            if both_doi_conflict and similarity >= 0.85:
                conflicts.append({
                    "entry_a": a["ref_key"], "entry_b": b["ref_key"],
                    "criterion": "near_identical_title_different_doi",
                    "detail": "title similarity %.2f but DOIs differ (%s vs %s); "
                              "not merged" % (similarity, a.get("doi"), b.get("doi")),
                })
                continue
            # bilingual pairs are handled above (before the similarity floor)
            if preprint_pair and similarity >= 0.85:
                record(left, right, 0.92, "preprint_vs_published",
                       "title similarity %.2f and one record is a preprint (%s)"
                       % (similarity, "one side is a preprint record"))
                continue
            if similarity >= 0.96 and same_author:
                record(left, right, 0.9, "near_identical_title_same_author",
                       "title similarity %.2f with the same first author" % similarity)
                continue
            if similarity >= 0.9 and same_author and year_close:
                record(left, right, 0.9, "near_identical_title_same_author_year",
                       "title similarity %.2f, same first author, year within 1" % similarity)
                continue
            if similarity >= 0.86 and year_close and not both_doi_conflict:
                needs_review.append({
                    "entry_a": a["ref_key"], "entry_b": b["ref_key"],
                    "criterion": "possible_duplicate_similar_title",
                    "confidence": round(similarity, 2),
                    "detail": "title similarity %.2f and year within 1; different author "
                              "records can also share a title - not auto-merged" % similarity,
                    "title_a": a.get("title"), "title_b": b.get("title"),
                })

    components: dict[int, list[int]] = {}
    for index in range(size):
        components.setdefault(uf.find(index), []).append(index)
    groups = []
    for root, members in sorted(components.items()):
        if len(members) < 2:
            continue
        criteria = []
        confidence = 0.0
        rationales = []
        for left in members:
            for right in members:
                if left >= right:
                    continue
                edge = edge_criteria.get((min(left, right), max(left, right)))
                if not edge:
                    continue
                criteria.append(edge["criteria"])
                rationales.append(edge["rationale"])
                confidence = max(confidence, edge["confidence"])
        groups.append({
            "group_id": "dup-%03d" % (len(groups) + 1),
            "members": [entries[index]["ref_key"] for index in members],
            "member_indexes": members,
            "criteria": sorted(set(criteria)),
            "confidence": round(confidence, 2),
            "rationale": "; ".join(sorted(set(rationales))),
        })
    return {"groups": groups, "needs_review": needs_review, "conflicts": conflicts,
            "aliases": edge_criteria}


def _merge_group(members: list[dict[str, Any]], group: dict[str, Any]) -> dict[str, Any]:
    """Merge one group, preferring the published/complete record as canonical."""
    def priority(entry: dict[str, Any]) -> tuple:
        return (0 if not is_preprint(entry) else 1,
                0 if entry.get("doi") else 1,
                round(float(entry.get("completeness") or 0), 3) * -1,
                0 if entry.get("type") != "misc" else 1,
                len(entry.get("title") or "") * -1,
                str(entry.get("ref_key") or ""))

    ordered = sorted(members, key=priority)
    canonical = dict(ordered[0])
    canonical["aliases"] = sorted({alias for entry in ordered
                                   for alias in (entry.get("aliases") or [entry["ref_key"]])})
    canonical["merged_from"] = sorted(entry["ref_key"] for entry in ordered[1:])
    canonical["merge_group"] = group["group_id"]
    canonical["merge_criteria"] = list(group["criteria"])
    canonical["merge_confidence"] = group["confidence"]
    canonical["field_sources"] = dict(canonical.get("field_sources") or {})
    for field in ("authors", "authors_raw", "title", "journal", "booktitle", "publisher",
                  "address", "school", "edition", "volume", "issue", "pages", "doi",
                  "url", "eprint", "keywords", "abstract", "year"):
        if _field_filled(canonical, field):
            continue
        for other in ordered[1:]:
            if _field_filled(other, field):
                canonical[field] = other[field]
                canonical["field_sources"][field] = other["ref_key"]
                break
    canonical["title_norm"] = R.norm_title(canonical.get("title", ""))
    canonical["language"] = canonical.get("language") or R.detect_language(
        str(canonical.get("title") or "") + " " + str(canonical.get("authors_raw") or ""))
    canonical["completeness"] = max((float(entry.get("completeness") or 0) for entry in ordered),
                                    default=canonical.get("completeness") or 0)
    return canonical


def apply_duplicates(entries: list[dict[str, Any]], duplicate_report: dict[str, Any]) -> dict[str, Any]:
    """Return canonical entries, the alias map and the merge records."""
    alias_to_key: dict[str, str] = {}
    merged_keys: set[str] = set()
    merges = []
    for group in duplicate_report.get("groups") or []:
        members = [entries[index] for index in group["member_indexes"]]
        canonical = _merge_group(members, group)
        merges.append({
            "group_id": group["group_id"],
            "merged_into": canonical["ref_key"],
            "merged_keys": [entry["ref_key"] for entry in members if entry["ref_key"] != canonical["ref_key"]],
            "criteria": group["criteria"],
            "confidence": group["confidence"],
            "rationale": group["rationale"],
            "field_fills": canonical.get("field_sources") or {},
        })
        for entry in members:
            alias_to_key[R.norm_key(entry["ref_key"])] = canonical["ref_key"]
            for alias in entry.get("aliases") or []:
                alias_to_key[R.norm_key(alias)] = canonical["ref_key"]
        for member in members:
            if member["ref_key"] != canonical["ref_key"]:
                merged_keys.add(member["ref_key"])
        canonical_placeholder = canonical
        # replace the first member in place so ordering stays deterministic
        for index, entry in enumerate(entries):
            if entry["ref_key"] == members[0]["ref_key"]:
                entries[index] = canonical_placeholder
                break
    canonical_entries = [entry for entry in entries if entry["ref_key"] not in merged_keys]
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for entry in canonical_entries:
        if entry["ref_key"] in seen:
            continue
        seen.add(entry["ref_key"])
        unique.append(entry)
    return {"entries": unique, "alias_to_key": alias_to_key, "merges": merges,
            "removed": sorted(merged_keys)}

# --------------------------------------------------------------------------- #
# formatters
# --------------------------------------------------------------------------- #
STYLE_GBT = "gbt7714-2015"
STYLE_APA = "apa7"
_STYLE_LABELS = {"gbt7714-2015": "GB/T 7714-2015 (numeric)", "apa7": "APA 7th (author-year)"}
TYPE_MARK_GBT = {"journal": "J", "conference": "C", "book": "M", "chapter": "M",
                 "thesis": "D", "report": "R", "preprint": "EB/OL", "web": "EB/OL",
                 "patent": "P", "standard": "S", "misc": "Z"}


def _is_cjk_name(name: str) -> bool:
    return R.cjk_ratio(name or "") >= 0.4


def _initials(given: str, with_period: bool = False) -> str:
    """One initial per given-name token: "John" -> J, "John A." -> J A."""
    tokens = re.findall(r"[A-Za-z][A-Za-z\-']*\.?", given or "")
    letters = []
    for token in tokens:
        match = re.search(r"[A-Za-z]", token)
        if match:
            letters.append(match.group(0).upper())
    if not letters:
        return ""
    if with_period:
        return " ".join(letter + "." for letter in letters)
    return " ".join(letters)


def format_authors_gbt(entry: dict[str, Any]) -> str:
    authors = entry.get("authors") or []
    language = entry.get("language") or "en"
    names = []
    for author in authors:
        family = str(author.get("family") or "").strip()
        if not family:
            continue
        if _is_cjk_name(family):
            names.append(family)
        else:
            initials = _initials(author.get("given", ""))
            names.append((family.upper() + ((" " + initials) if initials else "")).strip())
    if not names:
        raw = str(entry.get("authors_raw") or "").strip()
        return R.strip_latex(raw) if raw else ""
    if len(names) <= 3:
        return ", ".join(names)
    tail = ", \u7b49" if language == "zh" else ", et al"
    return ", ".join(names[:3]) + tail


def format_authors_apa(entry: dict[str, Any]) -> str:
    authors = entry.get("authors") or []
    names = []
    for author in authors:
        family = str(author.get("family") or "").strip()
        if not family:
            continue
        if _is_cjk_name(family):
            names.append(family)
        else:
            initials = _initials(author.get("given", ""), with_period=True)
            names.append((family + (", " + initials if initials else "")).strip())
    if not names:
        return R.strip_latex(str(entry.get("authors_raw") or "")).strip()
    zh_names = any(_is_cjk_name(name) for name in names)
    if len(names) == 1:
        return names[0]
    if zh_names:
        return ", ".join(names)
    if len(names) <= 20:
        return ", ".join(names[:-1]) + ", & " + names[-1]
    return ", ".join(names[:19]) + ", ... " + names[-1]


def _page_range(pages: str, dash: str = "-") -> str:
    text = str(pages or "").strip()
    return re.sub(r"\s*[-\u2013\u2014~]\s*", dash, text) if text else ""


def _venues_gbt(entry: dict[str, Any]) -> list[str]:
    parts = []
    address = str(entry.get("address") or "").strip()
    publisher = str(entry.get("publisher") or entry.get("school") or "").strip()
    if address and publisher:
        parts.append(address + ": " + publisher)
    elif publisher:
        parts.append(publisher)
    elif address:
        parts.append(address)
    year = str(entry.get("year") or "").strip()
    if year:
        parts.append(year)
    return parts


def format_entry_gbt(entry: dict[str, Any]) -> str:
    """GB/T 7714-2015 style: author. title[type]. venue, year, vol(issue): pages."""
    if entry.get("raw_only") or not str(entry.get("title") or "").strip():
        return R.normalize_space(entry.get("source", {}).get("raw") or "")[:600]
    authors = format_authors_gbt(entry)
    title = str(entry.get("title") or "").strip().rstrip(". ")
    etype = entry.get("type") or "misc"
    mark = TYPE_MARK_GBT.get(etype, "Z")
    head = (authors + ". ") if authors else ""
    doi = entry.get("doi") or ""
    url = str(entry.get("url") or "").strip()
    pages = _page_range(entry.get("pages", ""))
    year = str(entry.get("year") or "").strip()

    if etype == "journal":
        body = title + "[" + mark + "]. " + str(entry.get("journal") or "").strip()
        tail = []
        if year:
            tail.append(year)
        if entry.get("volume"):
            segment = str(entry["volume"]).strip()
            if entry.get("issue"):
                segment += "(" + str(entry["issue"]).strip() + ")"
            tail.append(segment)
        text = body
        if tail:
            text += ", " + ", ".join(tail)
        if pages:
            text += ": " + pages
        text += "."
    elif etype in {"conference", "chapter"}:
        venue = str(entry.get("booktitle") or entry.get("journal") or "").strip()
        body = title + "[" + mark + "]//" + venue
        tail = _venues_gbt(entry)
        text = body
        if tail:
            text += ". " + ", ".join(tail)
        if pages:
            text += ": " + pages
        text += "."
    elif etype in {"book", "misc", "patent", "standard"}:
        body = title + "[" + mark + "]"
        if entry.get("edition"):
            body += ". " + str(entry["edition"]).strip()
        tail = _venues_gbt(entry)
        text = body + ((". " + ", ".join(tail)) if tail else "") + "."
    elif etype == "thesis":
        body = title + "[D]"
        tail = _venues_gbt(entry)
        text = body + ((". " + ", ".join(tail)) if tail else "") + "."
    elif etype == "report":
        body = title + "[R]"
        tail = _venues_gbt(entry)
        text = body + ((". " + ", ".join(tail)) if tail else "") + "."
    else:  # preprint / web
        body = title + "[" + mark + "]"
        if year:
            body += ". (" + year + ")"
        link = url or (("https://arxiv.org/abs/" + str(entry.get("eprint"))) if entry.get("eprint") else "")
        text = body + ((". " + link) if link else "") + "."
    text = head + text
    if doi:
        text = text.rstrip(". ") + ". DOI: " + doi + "."
    return R.normalize_space(text)


def _doi_link(entry: dict[str, Any]) -> str:
    if entry.get("doi"):
        return "https://doi.org/" + str(entry["doi"])
    return str(entry.get("url") or "").strip()


def _apa_head(authors: str) -> str:
    if not authors:
        return ""
    return (authors + ". ") if not authors.endswith(".") else (authors + " ")


def format_entry_apa(entry: dict[str, Any]) -> str:
    """APA 7th style: Author, A. A. (Year). Title. Venue, vol(issue), pages. DOI."""
    if entry.get("raw_only") or not str(entry.get("title") or "").strip():
        return R.normalize_space(entry.get("source", {}).get("raw") or "")[:600]
    authors = format_authors_apa(entry)
    title = str(entry.get("title") or "").strip().rstrip(". ")
    year = str(entry.get("year") or "").strip()
    year_label = ("(" + year + (str(entry.get("year_suffix") or "")) + ").") if year else "(n.d.)."
    etype = entry.get("type") or "misc"
    journal = str(entry.get("journal") or "").strip()
    booktitle = str(entry.get("booktitle") or "").strip()
    pages = _page_range(entry.get("pages", ""), "\u2013")
    publisher = str(entry.get("publisher") or entry.get("school") or "").strip()
    head = _apa_head(authors)

    if etype == "journal":
        venue = journal
        tail = ""
        if venue:
            tail = venue
            if entry.get("volume"):
                tail += ", " + str(entry["volume"]).strip()
            if entry.get("issue"):
                tail += "(" + str(entry["issue"]).strip() + ")"
            if pages:
                tail += ", " + pages
            tail += "."
        text = head + year_label + " " + title + "." + ((" " + tail) if tail else "")
    elif etype in {"conference", "chapter"}:
        venue = booktitle or journal
        tail = "In " + venue if venue else ""
        if pages:
            tail += (" (pp. " + pages + ")") if tail else ("pp. " + pages)
        if publisher:
            tail += (". " + publisher if tail else publisher)
        text = head + year_label + " " + title + "." + ((" " + tail + ".") if tail else "")
    elif etype == "book":
        edition = str(entry.get("edition") or "").strip()
        tail = publisher + ((" " + edition) if edition else "")
        text = head + year_label + " " + title + "." + ((" " + tail + ".") if tail else "")
    elif etype == "preprint":
        server = str(entry.get("journal") or entry.get("publisher") or "Preprint").strip()
        link = _doi_link(entry) or (("https://arxiv.org/abs/" + str(entry.get("eprint"))) if entry.get("eprint") else "")
        text = head + year_label + " " + title + " [Preprint]. " + server + ((" " + link) if link else "") + "."
    elif etype == "thesis":
        label = "[Doctoral dissertation" + ((", " + publisher) if publisher else "") + "]"
        text = head + year_label + " " + title + " " + label + "."
        link = _doi_link(entry)
        if link:
            text += " " + link
    elif etype == "report":
        text = head + year_label + " " + title + "."
        if publisher:
            text += " " + publisher + "."
        link = _doi_link(entry)
        if link:
            text += " " + link
    elif etype == "web":
        site = str(entry.get("publisher") or entry.get("journal") or "").strip()
        link = _doi_link(entry)
        text = head + year_label + " " + title + "."
        if site:
            text += " " + site + "."
        if link:
            text += " " + link
    else:
        text = head + year_label + " " + title + "."
        if publisher:
            text += " " + publisher + "."
        link = _doi_link(entry)
        if link and text.count(link) == 0:
            text += " " + link
    link = _doi_link(entry)
    if link and link not in text:
        text = text.rstrip(". ") + ". " + link
    return R.normalize_space(text)


def format_entry(entry: dict[str, Any], style: str) -> str:
    return format_entry_apa(entry) if style == STYLE_APA else format_entry_gbt(entry)


def format_reference_list(entries: list[dict[str, Any]], style: str) -> list[dict[str, Any]]:
    """Formatted list in the given order; numbers are trace ids, not text."""
    out = []
    for index, entry in enumerate(entries, start=1):
        text = format_entry(entry, style)
        number = int(entry.get("number") or index)
        prefix = "[%d] " % number if style == STYLE_GBT else ""
        out.append({
            "number": number,
            "ref_key": entry["ref_key"],
            "aliases": list(entry.get("aliases") or []),
            "style": style,
            "formatted": prefix + text,
            "text": text,
            "entry_type": entry.get("type"),
            "year": entry.get("year"),
            "title": entry.get("title"),
            "doi": entry.get("doi"),
            "merge_group": entry.get("merge_group"),
            "merged_from": list(entry.get("merged_from") or []),
            "field_sources": dict(entry.get("field_sources") or {}),
        })
    return out

# --------------------------------------------------------------------------- #
# renumbering plan
# --------------------------------------------------------------------------- #
def _label_for_author_year(entry: dict[str, Any], suffix: str = "") -> str:
    authors = entry.get("authors") or []
    year = str(entry.get("year") or "") + (str(entry.get("year_suffix") or "") or suffix)
    names = [str(author.get("family") or "").strip() for author in authors
             if str(author.get("family") or "").strip()]
    zh = (entry.get("language") or "en") == "zh" or any(_is_cjk_name(name) for name in names)
    if not names:
        return (entry.get("title") or "n.d.")[:20] + ", " + year
    if len(names) == 1:
        return names[0] + ", " + year
    if len(names) == 2:
        return (names[0] + "\u548c" + names[1] + ", " + year) if zh else (names[0] + " & " + names[1] + ", " + year)
    return (names[0] + "\u7b49, " + year) if zh else (names[0] + " et al., " + year)


def _narrative_label(entry: dict[str, Any], suffix: str = "") -> str:
    authors = entry.get("authors") or []
    year = str(entry.get("year") or "") + (str(entry.get("year_suffix") or "") or suffix)
    names = [str(author.get("family") or "").strip() for author in authors
             if str(author.get("family") or "").strip()]
    zh = (entry.get("language") or "en") == "zh" or any(_is_cjk_name(name) for name in names)
    if not names:
        return (entry.get("title") or "n.d.")[:20] + " (" + year + ")"
    if len(names) == 1:
        return names[0] + " (" + year + ")"
    if len(names) == 2:
        return (names[0] + "\u548c" + names[1] + " (" + year + ")") if zh else (names[0] + " and " + names[1] + " (" + year + ")")
    return (names[0] + "\u7b49 (" + year + ")") if zh else (names[0] + " et al. (" + year + ")")


def plan_renumbering(markers: list[dict[str, Any]], entries: list[dict[str, Any]],
                     style: str) -> dict[str, Any]:
    """Assign new numbers / author-year labels in document order.

    Numeric style: numbers follow order of first appearance, which is the rule
    GB/T 7714 uses. Author-year style: the list is sorted by first author, year,
    title, and same author+year pairs get an a/b/c suffix so labels stay unique.
    """
    by_key = {entry["ref_key"]: entry for entry in entries}
    ordered_keys: list[str] = []
    cited_first_seen: dict[str, int] = {}
    for marker in markers:
        if marker.get("nocite"):
            continue
        for target in marker.get("targets") or []:
            key = target.get("ref_key")
            if key and key in by_key and key not in cited_first_seen:
                cited_first_seen[key] = len(cited_first_seen) + 1
                ordered_keys.append(key)

    year_suffix: dict[str, str] = {}
    author_year_label: dict[str, str] = {}
    narrative_label: dict[str, str] = {}
    if style == STYLE_APA:
        def sort_key(entry: dict[str, Any]):
            return (R.norm_key(R.first_surname(entry)), str(entry.get("year") or ""),
                    R.norm_title(entry.get("title") or ""), entry["ref_key"])
        ordered = sorted((by_key[key] for key in ordered_keys), key=sort_key)
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in ordered:
            marker_key = R.norm_key(R.first_surname(entry)) + "|" + str(entry.get("year") or "")
            groups.setdefault(marker_key, []).append(entry)
        for marker_key in sorted(groups):
            members = groups[marker_key]
            if len(members) > 1:
                for index, entry in enumerate(members):
                    year_suffix[entry["ref_key"]] = chr(ord("a") + index) if index < 26 else ""
        entry_order = [entry["ref_key"] for entry in ordered]
        for key in entry_order:
            suffix = year_suffix.get(key, "")
            author_year_label[key] = _label_for_author_year(by_key[key], suffix)
            narrative_label[key] = _narrative_label(by_key[key], suffix)
        number_of = {key: index for index, key in enumerate(entry_order, start=1)}
    else:
        entry_order = list(ordered_keys)
        number_of = {key: index for index, key in enumerate(entry_order, start=1)}
        for key in entry_order:
            author_year_label[key] = _label_for_author_year(by_key[key])
            narrative_label[key] = _narrative_label(by_key[key])

    uncited = [entry["ref_key"] for entry in entries if entry["ref_key"] not in cited_first_seen]
    return {
        "style": style,
        "mode": "author_year" if style == STYLE_APA else "numeric",
        "entry_order": entry_order,
        "number_of": number_of,
        "author_year_label": author_year_label,
        "narrative_label": narrative_label,
        "year_suffix": year_suffix,
        "uncited": uncited,
        "cited_count": len(entry_order),
    }


def collapse_numbers(numbers: list[int]) -> str:
    """[1,2,3,5] -> '1-3,5' (GB/T convention for three or more consecutive)."""
    if not numbers:
        return ""
    ordered = sorted(set(numbers))
    pieces = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        run = ordered[end] - ordered[index] + 1
        if run >= 3:
            pieces.append("%d-%d" % (ordered[index], ordered[end]))
        else:
            pieces.extend(str(number) for number in ordered[index:end + 1])
        index = end + 1
    return ",".join(pieces)


def rewrite_citations(text: str, markers: list[dict[str, Any]], plan: dict[str, Any],
                      *, keep_latex: bool = False) -> dict[str, Any]:
    """Rewrite only the citation markers; every other character is preserved."""
    mode = plan["mode"]
    number_of = plan["number_of"]
    parts: list[str] = []
    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    last = 0
    for marker in sorted(markers, key=lambda item: item["start"]):
        keys = []
        for target in marker.get("targets") or []:
            key = target.get("ref_key")
            if key and key not in keys:
                keys.append(key)
        parts.append(text[last:marker["start"]])
        replacement = marker["raw"]
        note = ""
        if not keys:
            unresolved.append({"cite_id": marker.get("cite_id"), "raw": marker.get("raw"),
                               "style": marker.get("style"),
                               "reason": "no bibliography entry resolved"})
        elif marker.get("style") == "latex" and keep_latex:
            replacement = marker["raw"]
            note = "latex marker kept; ordering is handled by the tex bibliography"
        elif mode == "numeric":
            numbers = [number_of[key] for key in keys if key in number_of]
            if len(numbers) != len(keys):
                unresolved.append({"cite_id": marker.get("cite_id"), "raw": marker.get("raw"),
                                   "style": marker.get("style"),
                                   "reason": "entry cited but absent from the numbered list"})
                replacement = marker["raw"]
            else:
                label = collapse_numbers(numbers)
                if marker.get("narrative"):
                    author_text = (marker.get("items") or [{}])[0].get("author_text") or ""
                    replacement = (author_text + " [" + label + "]") if author_text else "[" + label + "]"
                else:
                    replacement = "[" + label + "]"
                note = "renumbered " + marker["raw"] + " -> " + replacement
        else:
            labels = [plan["author_year_label"][key] for key in keys if key in plan["author_year_label"]]
            if len(labels) != len(keys):
                unresolved.append({"cite_id": marker.get("cite_id"), "raw": marker.get("raw"),
                                   "style": marker.get("style"),
                                   "reason": "entry cited but absent from the author-year list"})
                replacement = marker["raw"]
            elif marker.get("narrative"):
                label = plan["narrative_label"][keys[0]]
                replacement = label
                note = "converted to narrative author-year: " + replacement
            else:
                unique_labels = sorted(set(labels))
                replacement = "(" + "; ".join(unique_labels) + ")"
                note = "converted to author-year: " + replacement
        parts.append(replacement)
        last = marker["end"]
        changes.append({
            "change_id": "chg-%04d" % (len(changes) + 1),
            "type": "citation_marker",
            "cite_id": marker.get("cite_id"),
            "ref_keys": keys,
            "before": marker["raw"],
            "after": replacement,
            "marker_style_before": marker.get("style"),
            "marker_style_after": plan["mode"],
            "page": marker.get("page"),
            "block_id": marker.get("block_id"),
            "sentence_id": marker.get("sentence_id"),
            "note": note,
        })
    parts.append(text[last:])
    return {"text": "".join(parts), "changes": changes, "unresolved": unresolved}


# --------------------------------------------------------------------------- #
# consistency checks and metadata issues
# --------------------------------------------------------------------------- #
def check_consistency(*, markers: list[dict[str, Any]], entries: list[dict[str, Any]],
                      plan: dict[str, Any], formatted: list[dict[str, Any]],
                      reference_section_found: bool, unresolved: list[dict[str, Any]],
                      style: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, status: str, detail: str, counts: dict[str, Any] | None = None) -> None:
        checks.append({"check_id": check_id, "status": status, "detail": detail,
                       "counts": counts or {}})

    total_markers = [marker for marker in markers if not marker.get("nocite")]
    resolved = sum(1 for marker in total_markers if marker.get("targets"))
    add("in_text_resolution", "pass" if resolved == len(total_markers) else "fail",
        "%d/%d in-text citation markers resolve to a bibliography entry"
        % (resolved, len(total_markers)),
        {"total": len(total_markers), "resolved": resolved,
         "unresolved": len(total_markers) - resolved})

    numbers = sorted(plan["number_of"].values())
    expected = list(range(1, len(numbers) + 1))
    add("numbering_continuous", "pass" if numbers == expected else "fail",
        "reference numbering is continuous 1..%d" % len(numbers) if numbers == expected
        else "reference numbering is not continuous: %s" % numbers,
        {"numbers": len(numbers)})

    numeric_markers = [marker for marker in total_markers
                       if marker.get("style") in {"numeric", "numeric_superscript"}]
    in_range = True
    for marker in numeric_markers:
        for number in marker.get("numbers") or []:
            if not (1 <= int(number) <= max(1, len(entries))):
                in_range = False
    add("in_text_numbers_in_range", "pass" if in_range else "warn",
        "every numeric marker points inside the reference list" if in_range
        else "some numeric markers point outside the reference list (out-of-date numbering)",
        {"numeric_markers": len(numeric_markers)})

    add("no_uncited_entries", "pass" if not plan["uncited"] else "warn",
        "every bibliography entry is cited in the text" if not plan["uncited"]
        else "%d bibliography entr%s never cited" % (len(plan["uncited"]),
                                                     "y is" if len(plan["uncited"]) == 1 else "ies are"),
        {"uncited": len(plan["uncited"]), "keys": plan["uncited"][:40]})

    styles_seen = sorted({str(marker.get("style")) for marker in total_markers})
    source_styles = {style for style in styles_seen
                     if style in {"numeric", "numeric_superscript", "author_year", "author_year_narrative", "latex"}}
    add("marker_style_uniform", "pass" if len({style.split("_")[0] for style in source_styles}) <= 1 else "warn",
        "in-text markers use one convention: %s" % (", ".join(styles_seen) or "none"),
        {"styles": styles_seen})

    remaining_duplicates = []
    seen_doi: dict[str, str] = {}
    seen_title: dict[str, str] = {}
    for entry in entries:
        if entry.get("doi"):
            if entry["doi"] in seen_doi:
                remaining_duplicates.append([seen_doi[entry["doi"]], entry["ref_key"]])
            seen_doi[entry["doi"]] = entry["ref_key"]
        if entry.get("title_norm") and len(entry["title_norm"]) >= 8:
            if entry["title_norm"] in seen_title:
                remaining_duplicates.append([seen_title[entry["title_norm"]], entry["ref_key"]])
            seen_title[entry["title_norm"]] = entry["ref_key"]
    add("dedupe_complete", "pass" if not remaining_duplicates else "fail",
        "no duplicate entries left in the bibliography" if not remaining_duplicates
        else "%d duplicate pair(s) survived dedupe" % len(remaining_duplicates),
        {"pairs": remaining_duplicates[:20]})

    incomplete = [entry["ref_key"] for entry in entries
                  if float(entry.get("completeness") or 1) < 1.0 and entry.get("parse_status") == "parsed"]
    add("field_completeness", "pass" if not incomplete else "warn",
        "all entries carry the fields the chosen style needs" if not incomplete
        else "%d entr%s missing required fields" % (len(incomplete),
                                                    "y" if len(incomplete) == 1 else "ies"),
        {"incomplete": incomplete[:40]})

    bad_doi = [entry["ref_key"] for entry in entries
               if entry.get("doi") and not re.fullmatch(r"10\.\d{4,9}/\S+", entry["doi"])]
    add("doi_format", "pass" if not bad_doi else "warn",
        "every DOI matches the 10.xxxx/yyy pattern" if not bad_doi
        else "%d DOI value(s) are malformed" % len(bad_doi), {"bad_doi": bad_doi[:40]})

    fabricated = []
    for item in formatted:
        entry = next((candidate for candidate in entries
                      if candidate["ref_key"] == item["ref_key"]), None)
        if not entry:
            continue
        allowed = " ".join(str(entry.get(field) or "") for field in
                           ("year", "volume", "issue", "pages", "doi", "url", "eprint"))
        allowed_numbers = {token for token in re.findall(r"\d+", allowed)}
        text_numbers = set(re.findall(r"\d+", item["text"]))
        unknown = sorted(text_numbers - allowed_numbers - {"2", "1", "0", "4", "7", "3", "5", "6", "8", "9"})
        if unknown:
            fabricated.append({"ref_key": entry["ref_key"], "numbers": unknown})
    add("no_fabricated_fields", "pass" if not fabricated else "fail",
        "every number in a formatted entry comes from a parsed field" if not fabricated
        else "%d formatted entr%s contains a number from no parsed field"
             % (len(fabricated), "y" if len(fabricated) == 1 else "ies"),
        {"offenders": fabricated[:20]})

    cited_expected = len(plan["entry_order"])
    add("reference_section_sync", "pass" if len(formatted) == cited_expected else "fail",
        "formatted list has one record per cited entry" if len(formatted) == cited_expected
        else "formatted list has %d records for %d cited entries" % (len(formatted), cited_expected),
        {"formatted": len(formatted), "cited_entries": cited_expected,
         "bibliography_entries": len(entries),
         "reference_section_found": reference_section_found})

    fails = sum(1 for check in checks if check["status"] == "fail")
    warns = sum(1 for check in checks if check["status"] == "warn")
    return {"checks": checks, "passed": len(checks) - fails - warns,
            "warnings": warns, "failures": fails, "ok": fails == 0,
            "marker_styles": styles_seen, "unresolved": unresolved}


def metadata_issues(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Field-level problems that must be reported, not silently repaired."""
    issues: list[dict[str, Any]] = []
    for entry in entries:
        key = entry.get("ref_key")
        etype = entry.get("type") or "misc"
        if entry.get("parse_status") != "parsed":
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "entry", "severity": "error",
                           "message": "reference entry could not be parsed into fields; "
                                      "it is passed through verbatim",
                           "value": (entry.get("source", {}).get("raw") or "")[:160]})
            continue
        if not entry.get("title"):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "title", "severity": "error",
                           "message": "title is required for every reference style", "value": ""})
        if not entry.get("year"):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "year", "severity": "error",
                           "message": "publication year is missing", "value": ""})
        else:
            try:
                year_value = int(entry["year"])
                if not 1800 <= year_value <= 2099:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                               "field": "year", "severity": "error",
                               "message": "publication year is not a plausible 4-digit year",
                               "value": entry["year"]})
        if etype == "journal":
            if not entry.get("journal"):
                issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                               "field": "journal", "severity": "error",
                               "message": "journal name is missing for a journal article", "value": ""})
            if not entry.get("volume"):
                issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                               "field": "volume", "severity": "warning",
                               "message": "volume is missing; the GB/T/APA string will be shorter than usual",
                               "value": ""})
            if not entry.get("pages"):
                issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                               "field": "pages", "severity": "warning",
                               "message": "page range is missing", "value": ""})
        if etype in {"conference", "chapter"} and not entry.get("booktitle"):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "booktitle", "severity": "error",
                           "message": "container title (booktitle) is missing for a conference/chapter entry",
                           "value": ""})
        if etype in {"book", "chapter", "report", "thesis"} and not (
                entry.get("publisher") or entry.get("school")):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "publisher", "severity": "warning",
                           "message": "publisher/school is missing", "value": ""})
        if not entry.get("authors"):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "authors", "severity": "warning",
                           "message": "author list is empty; the title will stand in for the responsibility statement",
                           "value": entry.get("authors_raw") or ""})
        if entry.get("doi") and not re.fullmatch(r"10\.\d{4,9}/\S+", entry["doi"]):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "doi", "severity": "warning",
                           "message": "DOI does not match the 10.xxxx/yyy pattern",
                           "value": entry.get("doi")})
        if entry.get("pages"):
            match = re.search(r"(\d+)\s*[-\u2013]\s*(\d+)", entry["pages"])
            if match and int(match.group(1)) > int(match.group(2)):
                issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                               "field": "pages", "severity": "warning",
                               "message": "page range is reversed", "value": entry["pages"]})
        if entry.get("type") == "misc" and not (entry.get("journal") or entry.get("publisher")):
            issues.append({"issue_id": "meta-%04d" % (len(issues) + 1), "ref_key": key,
                           "field": "type", "severity": "info",
                           "message": "entry type could not be inferred; formatted as [Z]",
                           "value": entry.get("source", {}).get("format") or ""})
    return issues


# --------------------------------------------------------------------------- #
# claim-to-reference baseline matching
# --------------------------------------------------------------------------- #
def ref_terms(entry: dict[str, Any]) -> list[str]:
    text = " ".join(str(entry.get(field) or "") for field in
                    ("title", "keywords", "journal", "booktitle"))
    terms = R.content_terms(text, 24)
    for author in entry.get("authors") or []:
        family = str(author.get("family") or "").strip()
        if family:
            terms.append(family)
    return sorted(set(term for term in terms if term))


def match_claim(sentence: str, cited_keys: list[str],
                entries_by_key: dict[str, dict[str, Any]],
                pool_keys: list[str]) -> dict[str, Any]:
    """Deterministic claim/reference relevance: lexical overlap plus author cues."""
    terms = R.content_terms(sentence, 14)
    sentence_flat = R.norm_title(sentence)
    matched = []
    for key in cited_keys:
        entry = entries_by_key.get(key)
        if not entry:
            continue
        overlap = R.term_overlap(terms, ref_terms(entry))
        author_mentioned = False
        for author in entry.get("authors") or []:
            family = str(author.get("family") or "").strip()
            if len(family) >= 3 and (family.casefold() in sentence.casefold()
                                     or R.norm_key(family) in sentence_flat):
                author_mentioned = True
                break
        score = overlap["coverage"]
        if author_mentioned:
            score = max(score, 0.5)
        # "unrelated" would claim a negative we cannot prove: the claim may be a
        # paraphrase or in another language. Absence of shared terms is "unverified".
        if overlap["shared_count"] >= 2 and score >= 0.2:
            relation = "supported"
        elif overlap["shared_count"] >= 1 or author_mentioned:
            relation = "partial"
        else:
            relation = "unverified"
        matched.append({
            "ref_key": key,
            "relation": relation,
            "confidence": round(min(0.95, 0.35 + 0.6 * score + (0.1 if author_mentioned else 0.0)), 2),
            "basis": "author_mention+lexical_overlap" if author_mentioned else "lexical_overlap",
            "shared_terms": overlap["shared"][:8],
            "term_coverage": overlap["coverage"],
        })
    alternatives = []
    for key in pool_keys:
        if key in cited_keys:
            continue
        entry = entries_by_key.get(key)
        if not entry:
            continue
        overlap = R.term_overlap(terms, ref_terms(entry))
        if overlap["shared_count"] >= 2 and overlap["coverage"] >= 0.18:
            alternatives.append({"ref_key": key, "shared_terms": overlap["shared"][:8],
                                 "term_coverage": overlap["coverage"]})
    alternatives.sort(key=lambda item: (-item["term_coverage"], item["ref_key"]))
    best_cited = max([item["term_coverage"] for item in matched], default=0.0)
    misattribution = bool(alternatives and matched
                          and alternatives[0]["term_coverage"] >= 0.35
                          and alternatives[0]["term_coverage"] > best_cited + 0.15)
    return {"claim_terms": terms, "matched_refs": matched,
            "candidate_alternatives": alternatives[:3],
            "possible_misattribution": misattribution}


# --------------------------------------------------------------------------- #
# export helpers (format conversion deliverable)
# --------------------------------------------------------------------------- #
def to_bibtex(entry: dict[str, Any]) -> str:
    etype = {"journal": "article", "conference": "inproceedings", "chapter": "incollection",
             "book": "book", "thesis": "phdthesis", "report": "techreport",
             "preprint": "unpublished", "web": "online", "misc": "misc"}.get(
                 entry.get("type") or "misc", "misc")
    authors = " and ".join(
        "%s, %s" % (author.get("family", ""), author.get("given", ""))
        if author.get("given") else str(author.get("family") or "")
        for author in entry.get("authors") or [] if author.get("family"))
    fields = [
        ("author", authors),
        ("title", entry.get("title")),
        ("journal", entry.get("journal")),
        ("booktitle", entry.get("booktitle")),
        ("year", entry.get("year")),
        ("volume", entry.get("volume")),
        ("number", entry.get("issue")),
        ("pages", _page_range(entry.get("pages", "")).replace("-", "--")),
        ("publisher", entry.get("publisher")),
        ("address", entry.get("address")),
        ("doi", entry.get("doi")),
        ("url", entry.get("url")),
    ]
    body = ",\n".join("  %s = {%s}" % (name, value) for name, value in fields if value)
    return "@" + etype + "{" + entry["ref_key"] + ",\n" + body + "\n}\n"


def to_ris(entries: list[dict[str, Any]]) -> str:
    type_map = {"journal": "JOUR", "conference": "CPAPER", "chapter": "CHAP",
                "book": "BOOK", "thesis": "THES", "report": "RPRT",
                "preprint": "UNPB", "web": "ELEC", "misc": "GEN"}
    lines = []
    for entry in entries:
        lines.append("TY  - " + type_map.get(entry.get("type") or "misc", "GEN"))
        for author in entry.get("authors") or []:
            family = str(author.get("family") or "").strip()
            given = str(author.get("given") or "").strip()
            if family:
                lines.append("AU  - " + (family + (", " + given if given else "")))
        for tag, field in (("TI", "title"), ("JO", "journal"), ("T2", "booktitle"),
                           ("PY", "year"), ("VL", "volume"), ("IS", "issue"),
                           ("SP", "pages"), ("DO", "doi"), ("UR", "url"),
                           ("PB", "publisher"), ("CY", "address")):
            value = str(entry.get(field) or "").strip()
            if value:
                lines.append(tag + "  - " + value)
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)
