#!/usr/bin/env python3
"""Evidence anchors: make every claim point back to the source, verifiably.

All ten topics in this contest score the same way on this axis — "can each key
conclusion be traced back to a page / paragraph / table / formula position". So
the anchor is a first-class object, not a string appended at the end.

An anchor is only useful if it can be *checked*. ``verify`` re-reads the source
document and confirms the quoted text really occurs at the claimed block, which
turns "I cited page 4" into "I cited page 4, block p4-b3, and these 38
characters occur there". The verification result is stored next to the claim, so
a reviewer (or the judge) can independently confirm grounding — and a claim we
could not ground is marked as such instead of being silently upgraded.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalise for comparison: NFKC, collapse whitespace, casefold."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00ad", "")          # soft hyphen
    text = re.sub(r"-\s*\n\s*", "", text)      # de-hyphenate line breaks
    text = WS_RE.sub(" ", text)
    return text.strip().casefold()


def make_anchor(page: int, block: dict[str, Any], quote: str, note: str = "") -> dict[str, Any]:
    """Build an anchor from an extracted block and a quote taken from it."""
    return {
        "page": page,
        "block_id": block.get("block_id"),
        "bbox": block.get("bbox"),
        "quote": quote,
        "note": note,
    }


def pick_quote(block_text: str, claim_terms: Iterable[str] | None = None, width: int = 120) -> str:
    """Choose the shortest span of the block that best covers the claim terms.

    Deterministic on purpose: the same input always yields the same quote, which
    keeps repeated runs comparable (the contest asks for three repetitions).
    """
    text = WS_RE.sub(" ", block_text or "").strip()
    if not text:
        return ""
    if len(text) <= width:
        return text
    terms = [t for t in (claim_terms or []) if t]
    if terms:
        norm_text = normalize(text)
        best_start, best_hit = 0, -1
        for term in terms:
            pos = norm_text.find(normalize(term))
            if pos < 0:
                continue
            # approximate the character offset back onto the original string
            start = max(0, pos - width // 3)
            hits = sum(1 for t in terms if normalize(t) in normalize(text[start:start + width]))
            if hits > best_hit:
                best_hit, best_start = hits, start
        text = text[best_start:best_start + width]
    else:
        text = text[:width]
    return text.strip()


def _blocks_of(doc: dict[str, Any], page: int) -> list[dict[str, Any]]:
    return list((doc.get("blocks") or {}).get(str(page), []))


def verify(doc: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
    """Check one anchor against the extracted document.

    Returns ``{"match": "exact"|"normalised"|"block_only"|"page_only"|"missing", ...}``
    so the caller can tell a precise citation from a loose one.
    """
    page = anchor.get("page")
    quote = anchor.get("quote") or ""
    block_id = anchor.get("block_id")
    if page is None:
        return {"match": "missing", "reason": "anchor has no page"}

    blocks = _blocks_of(doc, page)
    target = None
    if block_id:
        target = next((b for b in blocks if b.get("block_id") == block_id), None)
    if target is None:
        # fall back to a scan of the page: the quote may have moved between runs
        for block in blocks:
            if normalize(quote) and normalize(quote) in normalize(block.get("text", "")):
                target = block
                break

    if target is None:
        page_text = " ".join(b.get("text", "") for b in blocks)
        if normalize(quote) and normalize(quote) in normalize(page_text):
            return {"match": "page_only", "page": page,
                    "reason": "quote found on the page but not in the cited block"}
        return {"match": "missing", "page": page, "reason": "quote not found on the page"}

    block_text = target.get("text", "")
    if quote and quote in block_text:
        return {"match": "exact", "page": page, "block_id": target.get("block_id")}
    if normalize(quote) and normalize(quote) in normalize(block_text):
        return {"match": "normalised", "page": page, "block_id": target.get("block_id")}
    if block_id and target.get("block_id") == block_id:
        return {"match": "block_only", "page": page, "block_id": block_id,
                "reason": "block exists but the quote text is not inside it"}
    return {"match": "missing", "page": page, "reason": "no matching block"}


GROUNDED = {"exact", "normalised"}


def _claim_supported(claim_text: str, quote: str) -> bool:
    """Does the quoted evidence actually mention what the claim is about?

    Without this check the pipeline verified its own output: the quote was cut
    from a block and then "verified" against that same block, so grounding_rate was
    always 1.0 even when the text was garbled or the columns were interleaved.
    Requiring claim terms to appear in the quote makes the number mean something.
    """
    terms = [t for t in _keywords(claim_text) if len(t) > 2]
    if not terms:
        return True  # nothing specific enough to check
    haystack = normalize(quote)
    if not haystack:
        return False
    hits = sum(1 for t in terms if normalize(t) in haystack)
    return hits >= max(1, min(2, len(terms) // 3))


def verify_all(doc: dict[str, Any], claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify a list of claims, each carrying an ``anchors`` list."""
    report: list[dict[str, Any]] = []
    grounded = ungrounded = 0
    for claim in claims:
        results = [verify(doc, a) for a in claim.get("anchors", [])]
        located = any(r["match"] in GROUNDED for r in results)
        # A *positional* claim ("this region on page 2 is a table candidate") is
        # about where something is, not what it says, so its evidence is the
        # location itself. Only content claims must have their terms quoted back.
        structural = str(claim.get("kind") or "") == "structural"
        supported = structural or any(
            r["match"] in GROUNDED and _claim_supported(claim.get("claim", ""), a.get("quote", ""))
            for r, a in zip(results, claim.get("anchors", []))
        )
        ok = located and supported
        if ok:
            grounded += 1
        else:
            ungrounded += 1
            if located and not supported:
                claim.setdefault("flags", []).append("evidence_does_not_mention_claim")
            else:
                claim.setdefault("flags", []).append("ungrounded")
        report.append({
            "claim_id": claim.get("claim_id"),
            "kind": claim.get("kind") or "content",
            "grounded": ok,
            "located": located,
            "supported_by_quote": supported,
            "checks": results,
        })
    total = grounded + ungrounded
    quality = doc.get("anchor_quality") or "text"
    rate = round(grounded / total, 4) if total else 0.0
    # An extraction that lost coordinates cannot support span-level verification;
    # report the ceiling instead of implying full confidence.
    ceiling = {"span": 1.0, "page": 0.7, "text": 0.4}.get(quality, 0.4)
    return {
        "claims_total": total,
        "grounded": grounded,
        "ungrounded": ungrounded,
        "grounding_rate": rate,
        "anchor_quality": quality,
        "confidence_ceiling": ceiling,
        "effective_confidence": round(min(rate, ceiling), 4),
        "per_claim": report,
    }


def build(doc: dict[str, Any], claim_text: str, page: int | None = None,
          block_id: str | None = None, terms: Iterable[str] | None = None,
          max_anchors: int = 2) -> list[dict[str, Any]]:
    """Find anchors for a claim by scanning extracted blocks for its terms."""
    terms = [t for t in (terms or _keywords(claim_text)) if len(t) > 1]
    if not terms:
        return []
    anchors: list[dict[str, Any]] = []
    pages = [page] if page else sorted(int(k) for k in (doc.get("blocks") or {}))
    for pg in pages:
        for block in _blocks_of(doc, pg):
            if block_id and block.get("block_id") != block_id:
                continue
            text = normalize(block.get("text", ""))
            if not text:
                continue
            hits = sum(1 for t in terms if normalize(t) in text)
            if hits == 0:
                continue
            anchors.append((hits, make_anchor(pg, block, pick_quote(block.get("text", ""), terms))))
        if len(anchors) >= max_anchors:
            break
    anchors.sort(key=lambda x: -x[0])
    return [a for _, a in anchors[:max_anchors]]


STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "by",
    "is", "are", "was", "were", "be", "been", "that", "this", "these", "those",
    "we", "our", "it", "its", "as", "at", "from", "than", "then", "which", "can",
}


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}|\d+(?:\.\d+)?", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        low = word.casefold()
        if low in STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(word)
    return out[:8]
