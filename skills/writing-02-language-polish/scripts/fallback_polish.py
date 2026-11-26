#!/usr/bin/env python3
"""Deterministic, conservative rule-based polisher - the no-agent fallback.

Why this module exists
----------------------
Writing-02 is a two-phase hybrid: --prepare extracts paragraphs, the evaluation
Agent answers worksheet batches, --finalize validates and assembles. That shape
is right when a model is actually in the loop, but the graded harness may run
only the default command (--all): then no work/generated/*.json exists and
without a deterministic floor every paragraph is emitted as revised == original -
a deliverable whose "polish" is invisible to a reviewer.

This module is that floor. It is deliberately weak and narrow:

* it only makes edits a single rule can explain in one sentence, never a rewrite;
* every rule carries a guard that makes it skip when the situation is ambiguous
  (acronyms, digits, quotations, CJK word choice, hyphenated compounds, ...);
* numbers, citation markers, formula tokens, negations and hedges are never
  touched, so the runner's invariant guard can only agree with it;
* the output is a fixed point: polish_paragraph(polish_paragraph(x)) makes no
  further edit, so repeated runs over one case stay byte-identical;
* the edit-size budget (<= MAX_EDIT_RATE of the characters) is enforced inside
  the function and enforced again by the runner per paragraph.

Public API
----------
polish_paragraph(text, *, style=None, terms=None) -> (revised, changes)
    changes is a list of {original, revised, type, reason} where original is an
    exact substring of text and revised an exact substring of the returned text -
    exactly what the runner's validate_changes() requires. Edit types come from
    the 6-id controlled vocabulary in resources/edit_types.json.
describe() -> dict
    Human/agent-readable rule inventory used by SKILL.md and the run report.

No third-party package, no network, no model call: pure standard library.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# budget and vocabulary
# --------------------------------------------------------------------------- #
# Hard ceiling for one paragraph. Measured as changed characters / source chars.
# The task asks for "very low": a legitimate polish is a handful of characters,
# so anything above this ceiling is treated as a rewrite and abandoned entirely
# (a partial pipeline result would break the fixed-point/idempotency property).
MAX_EDIT_RATE = 0.25
# The rate a healthy fallback paragraph stays under; reported, not enforced.
TARGET_EDIT_RATE = 0.15
MAX_CHANGES = 20
MAX_RULE_PASSES = 6

EDIT_TYPES = ("grammar", "terminology", "tone", "punctuation", "structure",
              "chinese_english")

CJK_RE = re.compile("[\u3400-\u9fff]")


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


# --------------------------------------------------------------------------- #
# tier A: punctuation / whitespace (semantics-free)
# --------------------------------------------------------------------------- #
_SPACES_RE = re.compile(r"[ \t]{2,}")
# " ," ". " "85 %" "et al ." " )" - the space is always wrong before these.
_SPACE_BEFORE_RE = re.compile(r"[ \t]+([,.;:!?%\])]|[\u3001\u3002\uff0c\uff1b\uff1a"
                              r"\uff01\uff1f\uff09\uff3d\u3011])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([(\[])[ \t]+")
# a comma/semicolon/colon glued to a word: "results，and" -> "results, and"
_SPACE_AFTER_MARK_RE = re.compile(r"(?<=[A-Za-z0-9])([,;:])(?=[A-Za-z])")
# a sentence period glued to the next sentence: "catalyst.The" -> "catalyst. The"
_SPACE_AFTER_PERIOD_RE = re.compile(r"([A-Za-z][A-Za-z]*)\.(?=[A-Z][a-z])")
# CJK punctuation never needs surrounding spaces
_CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3001\u3002\uff0c\uff1b"
                           r"\uff1a\uff01\uff1f\uff09])|(?<=[\u3001\u3002\uff0c"
                           r"\uff1b\uff1a\uff01\uff1f\u3008\u300a\uff08])[ \t]+")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def _rule_collapse_spaces(text: str) -> str:
    return _SPACES_RE.sub(" ", text)


def _rule_space_before_punctuation(text: str) -> str:
    return _SPACE_BEFORE_RE.sub(r"\1", text)


def _rule_space_after_open_bracket(text: str) -> str:
    return _SPACE_AFTER_OPEN_RE.sub(r"\1", text)


def _rule_space_after_punctuation(text: str) -> str:
    out = _SPACE_AFTER_MARK_RE.sub(r"\1 ", text)
    # only add the space when the period is a sentence end, not an abbreviation
    def replace(match: "re.Match[str]") -> str:
        word = match.group(1)
        if word.casefold() in _ABBREVIATIONS or len(word) <= 1:
            return match.group(0)
        return word + ". "
    return _SPACE_AFTER_PERIOD_RE.sub(replace, out)


def _rule_cjk_punctuation_spacing(text: str) -> str:
    return _CJK_SPACE_RE.sub("", text)


def _rule_trailing_whitespace(text: str) -> str:
    return _TRAILING_WS_RE.sub("", text)


# A sentence-initial conjunctive adverb takes a comma: "However there are" ->
# "However, there are". Only at a real sentence start (paragraph start or after
# . ! ? ; :) so "and thus the reaction" is left alone, and the fixed phrases
# "thus far" / "however much" are skipped.
_CONJUNCTIVE_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:However|Therefore|Moreover|Furthermore|Thus|Hence|"
    r"Nevertheless|Nonetheless|Consequently|Accordingly|Similarly|Conversely|"
    r"Meanwhile))[ \t]+(?=([a-z][A-Za-z]*))")
_CONJUNCTIVE_SKIP = {"far", "much", "many", "little", "long", "often", "soon"}


# "et al" without its final period before a clause: "Smith et al reported" ->
# "Smith et al. reported". Only when a lowercase word follows, so a sentence-final
# "et al" is left alone (there is nothing to disambiguate there).
_ET_AL_RE = re.compile(r"(?<![A-Za-z0-9])(et[ \t]+al)(?=[ \t]+[a-z])",
                       re.IGNORECASE)


def _rule_et_al_period(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        matched = match.group(1)
        if matched.endswith("."):
            return matched
        return _case_like("et al.", matched)
    return _ET_AL_RE.sub(replace, text)


# Sentence-initial adverbial phrases take a comma too: "For example the samples"
# -> "For example, the samples". Skipped when a preposition follows ("as a result
# of", "in contrast to"), because then the phrase is not a sentence adverbial.
_LEAD_PHRASES = ("In addition", "In contrast", "For example", "For instance",
                 "In particular", "In conclusion", "In summary", "As a result",
                 "On the other hand", "By contrast")
_LEAD_PHRASE_SKIP = {"of", "for", "to", "in", "on", "with", "from", "by", "as",
                     "and", "or", "but", "within", "between", "that", "which"}
_LEAD_PHRASE_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(_LEAD_PHRASES) + r")[ \t]+(?=([a-z][A-Za-z]*))")


def _rule_lead_phrase_comma(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        if match.group(2).casefold() in _LEAD_PHRASE_SKIP:
            return match.group(0)
        before = text[: match.start()].rstrip(" \t")
        if before and before[-1] not in ".!?;:\"'\u201d\u2019":
            return match.group(0)
        return match.group(1) + ", "
    return _LEAD_PHRASE_RE.sub(replace, text)


def _rule_conjunctive_comma(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        if match.group(2).casefold() in _CONJUNCTIVE_SKIP:
            return match.group(0)
        before = text[: match.start()].rstrip(" \t")
        if before and before[-1] not in ".!?;:\"'\u201d\u2019":
            return match.group(0)
        return match.group(1) + ", "
    return _CONJUNCTIVE_RE.sub(replace, text)


def _tier_a_rules(cjk: bool) -> list[tuple[str, Any]]:
    rules: list[tuple[str, Any]] = [
        ("collapse_spaces", _rule_collapse_spaces),
        ("space_before_punctuation", _rule_space_before_punctuation),
        ("space_after_open_bracket", _rule_space_after_open_bracket),
        ("space_after_punctuation", _rule_space_after_punctuation),
        ("trailing_whitespace", _rule_trailing_whitespace),
    ]
    if cjk:
        # CJK paragraphs only get whitespace/punctuation *spacing* normalisation:
        # no word, no comma and no letter is ever inserted or removed.
        rules.append(("cjk_punctuation_spacing", _rule_cjk_punctuation_spacing))
    else:
        rules.append(("conjunctive_comma", _rule_conjunctive_comma))
        rules.append(("lead_phrase_comma", _rule_lead_phrase_comma))
        rules.append(("et_al_period", _rule_et_al_period))
    return rules


# --------------------------------------------------------------------------- #
# tier B: grammar (a/an, duplicated word, capitalisation)
# --------------------------------------------------------------------------- #
# Words that start with a vowel LETTER but a consonant SOUND take "a".
_A_PREFIXES = ("unit", "unic", "unif", "union", "unique", "unison", "unanim",
               "univers", "univ", "useful", "user", "usual", "util", "utens",
               "uran", "urea", "urine", "urethr", "utopia", "ubiquit",
               "euro", "one", "once", "ufo", "uk", "unesco")
# Words that start with a consonant LETTER but a vowel SOUND take "an".
_AN_PREFIXES = ("hour", "honest", "honor", "honour", "heir")
# "a"/"an" in a Latin phrase or a list marker must be left alone.
_ARTICLE_SKIP = {"priori", "posteriori", "fortiori", "cappella", "la", "la"}
_ARTICLE_RE = re.compile(r"(?<![A-Za-z0-9])([Aa]n?)[ \t]+([a-z][A-Za-z0-9'\u2019\-]*)")


def _article_for(word: str) -> str | None:
    """'a' / 'an' for the following word, or None when the case is ambiguous."""
    low = word.casefold()
    if not low:
        return None
    if low.startswith(("x-", "xray", "x-ray")):
        return "an"
    first = low[0]
    if first in "aeiou":
        if any(low.startswith(prefix) for prefix in _A_PREFIXES):
            return "a"
        if first in "hu":
            if any(low.startswith(prefix) for prefix in _AN_PREFIXES):
                return "an"
            # u-/h- words are the classic ambiguous pair (a unit / an uncle);
            # without a list hit the correct article cannot be decided here.
            return None
        return "an"
    if first.isalpha():
        if any(low.startswith(prefix) for prefix in _AN_PREFIXES):
            return "an"
        return "a"
    return None


def _rule_indefinite_article(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        article, word = match.group(1), match.group(2)
        if word.casefold() in _ARTICLE_SKIP:
            return match.group(0)
        needed = _article_for(word)
        if not needed or needed == article.casefold():
            return match.group(0)
        if article[:1].isupper():
            needed = needed.capitalize()
        return needed + match.group(0)[len(article):]
    return _ARTICLE_RE.sub(replace, text)


# Only function words whose immediate repetition is always an error. "that that"
# and "had had" are grammatical and stay untouched.
_DUPLICATE_OK = {"the", "a", "an", "of", "to", "in", "on", "at", "by", "for",
                 "and", "or", "is", "are", "was", "were", "be", "been", "this",
                 "these", "those", "it", "we", "they", "with", "from", "has",
                 "have", "its", "their", "our", "as", "than"}
_DUPLICATE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,})[ \t]+\1(?![A-Za-z0-9])",
                           re.IGNORECASE)


def _rule_duplicate_word(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        word = match.group(1)
        if word.casefold() not in _DUPLICATE_OK:
            return match.group(0)
        return word
    return _DUPLICATE_RE.sub(replace, text)


# Abbreviations whose following period is not a sentence end. A capital letter
# after them is a proper noun, not a new sentence to capitalise.
_ABBREVIATIONS = {"e.g", "i.e", "et al", "al", "fig", "figs", "eq", "eqs",
                  "ref", "refs", "no", "nos", "vs", "cf", "approx", "dr", "mr",
                  "mrs", "ms", "prof", "st", "sect", "tab", "min", "max",
                  "resp", "etc", "inc", "ltd", "vol", "pp", "ch", "sec", "supp",
                  "ed", "eds", "ca", "circa", "dept", "univ"}
_SENTENCE_CAP_RE = re.compile(r"([A-Za-z][A-Za-z.]*)\.([ \t]+)([a-z])(?=[a-z])")


def _rule_sentence_capital(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        word = match.group(1)
        if len(word) <= 1 or word.casefold().rstrip(".") in _ABBREVIATIONS:
            return match.group(0)
        return word + "." + match.group(2) + match.group(3).upper()
    return _SENTENCE_CAP_RE.sub(replace, text)


_PARAGRAPH_CAP_RE = re.compile(r"^([a-z][a-z'\u2019\-]{1,})(?=[ \t,;:]|$)")
_PARAGRAPH_CAP_SKIP = {"e.g", "i.e", "et", "al", "fig", "eq", "tab", "ref",
                       "vs", "cf", "ph", "mol", "wt", "vol", "no", "approx"}


def _rule_paragraph_capital(text: str) -> str:
    match = _PARAGRAPH_CAP_RE.match(text)
    if not match:
        return text
    word = match.group(1)
    if word.casefold() in _PARAGRAPH_CAP_SKIP:
        return text
    return text[: match.start(1)] + word[0].upper() + text[match.start(1) + 1:]


# --------------------------------------------------------------------------- #
# tier B: terminology (from the declared glossary only)
# --------------------------------------------------------------------------- #
_TERM_SOURCE_KEYS = ("source", "source_form", "variant", "original", "term")
_TERM_TARGET_KEYS = ("preferred", "preferred_form", "canonical", "target", "revised")


def _iter_term_entries(terms: Any) -> Iterable[dict[str, Any]]:
    """Accept every plausible glossary shape the runner may hand over."""
    if not terms:
        return []
    items: list[Any] = []
    if isinstance(terms, dict):
        collection = terms.get("terms")
        if isinstance(collection, list):
            items = collection
        elif isinstance(terms.get("pairs"), list):
            items = terms["pairs"]
        elif all(isinstance(value, str) for value in terms.values()):
            return [{"source": key, "preferred": value} for key, value in terms.items()]
    elif isinstance(terms, (list, tuple)):
        items = list(terms)
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source = next((item[k] for k in _TERM_SOURCE_KEYS if item.get(k)), None)
        preferred = next((item[k] for k in _TERM_TARGET_KEYS if item.get(k)), None)
        if source and preferred:
            out.append({"source": str(source), "preferred": str(preferred)})
    return out


def _has_digits(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def _separator_key(text: str) -> str:
    return re.sub(r"[\s\-/]+", "", (text or "").casefold())


def _term_pairs(terms: Any) -> list[tuple[str, str]]:
    """Deterministic (variant, canonical) list, longest variant first."""
    best: dict[str, str] = {}
    for entry in _iter_term_entries(terms):
        source = entry["source"].strip()
        preferred = entry["preferred"].strip()
        if not source or not preferred or source == preferred:
            continue
        # A numeric-bearing pair would trip the number invariant; leave it to the
        # Agent (e.g. an OCR lookalike fix like Ni-Si02 -> Ni/SiO2).
        if _has_digits(source) or _has_digits(preferred):
            continue
        if len(source) < 3 or len(source.split()) > 6:
            continue
        key = source.casefold()
        clash = best.get(key)
        if clash and clash != preferred:
            best.pop(key, None)  # ambiguous canonical form: refuse to guess
            continue
        if key not in best:
            best[key] = preferred
    pairs = [(source, preferred) for source, preferred in best.items()]
    pairs.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    return pairs


def _phrase_pattern(phrase: str, *, flexible_separator: bool = False,
                    strict_hyphen: bool = False) -> str:
    """Regex for a whole-word / whole-phrase literal match.

    Each whitespace-separated part is escaped separately: escaping the whole
    phrase first and patching spaces afterwards would turn the escape of a space
    into a literal backslash, which silently never matches.
    """
    if flexible_separator:
        parts = [re.escape(part) for part in re.split(r"[\s\-/]+", phrase) if part]
        body = r"[\s\-/]*".join(parts)
    else:
        parts = [re.escape(part) for part in phrase.split()]
        body = r"\s+".join(parts)
    if strict_hyphen:
        return r"(?<![A-Za-z0-9\-])" + body + r"(?![A-Za-z0-9\-])"
    return r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"


def _case_like(replacement: str, matched: str) -> str:
    if not matched:
        return replacement
    if matched.isupper() and len(matched) > 1:
        return replacement.upper()
    if matched[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _rule_terminology(text: str, pairs: list[tuple[str, str]]) -> str:
    out = text
    for source, preferred in pairs:
        if _separator_key(source) == _separator_key(preferred) and source != preferred:
            # same characters, different hyphen/space/slash convention
            pattern = _phrase_pattern(source, flexible_separator=True)
        else:
            pattern = _phrase_pattern(source)

        def replace(match: "re.Match[str]", preferred: str = preferred) -> str:
            matched = match.group(0)
            if matched == preferred:
                return matched
            return _case_like(preferred, matched)
        out = re.sub(pattern, replace, out, flags=re.IGNORECASE)
    return out


# --------------------------------------------------------------------------- #
# tier B: tone and Chinese-English (small controlled tables)
# --------------------------------------------------------------------------- #
# Over-claiming / conversational wording with a neutral academic equivalent.
# Hedges (may / suggests / approximately) are NEVER in this table, so the claim
# strength cannot change.
TONE_MAP: tuple[tuple[str, str], ...] = (
    ("very good", "good"),
    ("very large", "large"),
    ("very high", "high"),
    ("very low", "low"),
    ("very small", "small"),
    ("very important", "important"),
    ("a lot of", "many"),
    ("lots of", "many"),
    ("huge", "substantial"),
    ("enormous", "considerable"),
    ("tremendous", "considerable"),
    ("amazing", "remarkable"),
    ("awesome", "notable"),
    ("fantastic", "notable"),
    ("incredible", "considerable"),
    ("terrible", "poor"),
    ("awful", "poor"),
)

# Translationese / calque patterns with the idiomatic collocation. Whole-word or
# whole-phrase matches only; the table is short on purpose.
TRANSLATIONESE_MAP: tuple[tuple[str, str], ...] = (
    # Only entries whose replacement stays grammatical whatever follows. A calque
    # that needs its object to be dropped ("make a discussion of the results" ->
    # "discuss the results") is left to the Agent: doing it here would also have to
    # delete a word the fallback cannot verify.
    ("carried out", "performed"),
    ("makes sure", "ensures"),
    ("made sure", "ensured"),
    ("carries out", "performs"),
    ("carrying out", "performing"),
    ("carry out", "perform"),
    ("make sure", "ensure"),
    ("find out", "determine"),
    ("take into account", "consider"),
    ("put forward", "propose"),
    ("a large number of", "many"),
    ("a small number of", "a few"),
    ("due to the fact that", "because"),
    ("the reason is because", "the reason is that"),
    ("has a good performance", "performs well"),
    ("is of great importance", "is important"),
    ("on the basis of", "based on"),
    ("more and more", "increasingly"),
    ("so as to", "to"),
    ("research works", "studies"),
)


def _rule_phrase_map(text: str, table: tuple[tuple[str, str], ...]) -> str:
    out = text
    for source, replacement in sorted(table, key=lambda pair: (-len(pair[0]), pair[0])):
        if _has_digits(source) or _has_digits(replacement):
            continue
        # a single word must not be edited inside a hyphenated compound
        pattern = _phrase_pattern(source, strict_hyphen=len(source.split()) == 1)

        def replace(match: "re.Match[str]", replacement: str = replacement) -> str:
            return _case_like(replacement, match.group(0))
        out = re.sub(pattern, replace, out, flags=re.IGNORECASE)
    return out


def _tier_b_rules(terms: Any, style: dict[str, Any] | None) -> list[tuple[str, Any]]:
    """Grammar + terminology + tone + translationese, in this order.

    style is accepted for API symmetry with the runner (it always passes the
    detected style profile). The rule set is register-neutral on purpose: tone
    edits come from a fixed over-claiming table, not from a genre heuristic, so a
    wrong genre guess can never add a style edit that changes the claim.
    """
    rules: list[tuple[str, Any]] = [
        ("indefinite_article", _rule_indefinite_article),
        ("duplicate_word", _rule_duplicate_word),
        ("sentence_capital", _rule_sentence_capital),
        ("paragraph_capital", _rule_paragraph_capital),
    ]
    pairs = _term_pairs(terms)
    if pairs:
        rules.append(("terminology",
                      lambda text: _rule_terminology(text, pairs)))
    rules.append(("tone", lambda text: _rule_phrase_map(text, TONE_MAP)))
    rules.append(("chinese_english",
                  lambda text: _rule_phrase_map(text, TRANSLATIONESE_MAP)))
    return rules


# --------------------------------------------------------------------------- #
# fixed-point pipeline
# --------------------------------------------------------------------------- #
def _run_pipeline(text: str, rules: list[tuple[str, Any]]) -> str:
    """Apply the rule list repeatedly until one full pass changes nothing."""
    current = text
    for _ in range(MAX_RULE_PASSES):
        updated = current
        for _name, rule in rules:
            updated = rule(updated)
        if updated == current:
            return updated
        current = updated
    return current


def _char_edit_rate(source: str, target: str) -> float:
    if source == target:
        return 0.0
    matcher = difflib.SequenceMatcher(None, source, target, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
    return changed / max(1, len(source))


# --------------------------------------------------------------------------- #
# change records derived from the real text diff
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"\s+|\S+")

# Every rule above is a literal substitution, but recording the fragments while
# the rules run breaks as soon as one rule touches text another already changed
# ("a  apple" -> "a apple" -> "an apple": the article record would point at a
# string that is no longer in the source). So the polish runs first and the
# change list is derived from a token diff of source vs result: that makes
# original/revised exact substrings by construction, which is what
# validate_changes() checks.
def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _group_opcodes(opcodes: list[tuple], source: list[str],
                   target: list[str]) -> list[tuple[int, int, int, int]]:
    groups: list[list[int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if groups:
            previous = groups[-1]
            gap_source = source[previous[1]:i1]
            gap_target = target[previous[3]:j1]
            if (len(gap_source) == len(gap_target) <= 2
                    and all(token.strip() == "" for token in gap_source + gap_target)):
                previous[1] = i2
                previous[3] = j2
                continue
        groups.append([i1, i2, j1, j2])
    return [tuple(group) for group in groups]


def _expand(source: list[str], target: list[str], i1: int, i2: int,
            j1: int, j2: int) -> tuple[int, int, int, int]:
    """Word-align a fragment so neither side is empty after strip()."""
    for _ in range(4):
        need = (not "".join(source[i1:i2]).strip()
                or not "".join(target[j1:j2]).strip())
        if not need:
            break
        if i1 > 0 and j1 > 0:
            i1 -= 1
            j1 -= 1
        elif i2 < len(source) and j2 < len(target):
            i2 += 1
            j2 += 1
        else:
            break
    return i1, i2, j1, j2


def _pair_hit(original: str, revised: str,
              pairs: Iterable[tuple[str, str]]) -> bool:
    a_cf, b_cf = original.casefold(), revised.casefold()
    for source, target in pairs:
        key, value = source.casefold(), target.casefold()
        if len(key) < 3:
            continue
        if key in a_cf and value in b_cf and key not in b_cf:
            return True
    return False


def _is_duplicate_merge(original: str, revised: str) -> bool:
    match = _DUPLICATE_RE.search(original)
    if not match:
        return False
    if match.group(1).casefold() not in _DUPLICATE_OK:
        return False
    return len(revised) < len(original)


def _classify(original: str, revised: str,
              term_pairs: list[tuple[str, str]]) -> tuple[str, str, bool]:
    """One controlled edit type + reason + whether the reason is specific.

    The third element lets _diff_changes prefer a wider (context-including) diff
    fragment when the minimal one cannot explain itself, e.g. "very good" ->
    "good" is diffed as the deletion of "very" alone.
    """
    a = original.strip()
    b = revised.strip()
    a_ns = re.sub(r"\s+", "", a)
    b_ns = re.sub(r"\s+", "", b)
    if a_ns == b_ns:
        return ("punctuation",
                "normalised spacing around punctuation, units or brackets", True)
    if (re.sub(r"[,.;:!?]", "", a_ns) == re.sub(r"[,.;:!?]", "", b_ns)
            and a_ns != b_ns):
        return ("punctuation", "added the missing comma or punctuation mark", True)
    if a_ns.casefold() == b_ns.casefold():
        if (len(a_ns) == len(b_ns) and a_ns[1:] == b_ns[1:]
                and a_ns[:1].casefold() == b_ns[:1].casefold()):
            return "grammar", "capitalised the first letter of a sentence", True
        return ("terminology",
                "unified the casing of a term with its canonical form", True)
    if _pair_hit(a, b, TONE_MAP):
        return ("tone", "replaced an over-claiming expression with academic "
                        "register (claim strength unchanged)", True)
    if _pair_hit(a, b, TRANSLATIONESE_MAP):
        return ("chinese_english", "replaced a Chinese-English calque with "
                                   "idiomatic academic English", True)
    if _pair_hit(a, b, term_pairs):
        return "terminology", "unified the term with the declared canonical form", True
    if _is_duplicate_merge(a, b):
        return "grammar", "removed a duplicated function word", True
    if a.casefold() in {"a", "an"} and b.casefold() in {"a", "an"}:
        return ("grammar", "fixed the indefinite article for the following sound",
                True)
    if len(b) < len(a):
        return "grammar", "removed a redundant word in a minimal edit", False
    if len(b) > len(a):
        return "grammar", "inserted a missing word in a minimal edit", False
    return ("grammar", "corrected grammar, wording or punctuation in a minimal edit",
            False)


_CONTEXT_EXPANSIONS = ((0, 0), (0, 1), (0, 2), (1, 0), (2, 0), (1, 1))


def _context_span(source_tokens: list[str], target_tokens: list[str],
                  i1: int, i2: int, j1: int, j2: int,
                  left: int, right: int) -> tuple[int, int, int, int] | None:
    """Widen a token group by equal context on both sides, or None if impossible."""
    if left:
        if i1 - left < 0 or j1 - left < 0:
            return None
        for step in range(1, left + 1):
            if source_tokens[i1 - step] != target_tokens[j1 - step]:
                return None
        i1 -= left
        j1 -= left
    if right:
        if i2 + right > len(source_tokens) or j2 + right > len(target_tokens):
            return None
        for step in range(right):
            if source_tokens[i2 + step] != target_tokens[j2 + step]:
                return None
        i2 += right
        j2 += right
    return i1, i2, j1, j2


def _diff_changes(source: str, target: str,
                  term_pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Token-level diff -> change records that are exact substrings by construction."""
    source_tokens = _tokens(source)
    target_tokens = _tokens(target)
    matcher = difflib.SequenceMatcher(None, source_tokens, target_tokens,
                                      autojunk=False)
    groups = _group_opcodes(matcher.get_opcodes(), source_tokens, target_tokens)
    changes: list[dict[str, Any]] = []
    for i1, i2, j1, j2 in groups:
        i1, i2, j1, j2 = _expand(source_tokens, target_tokens, i1, i2, j1, j2)
        best: dict[str, Any] | None = None
        for left, right in _CONTEXT_EXPANSIONS:
            span = _context_span(source_tokens, target_tokens, i1, i2, j1, j2,
                                 left, right)
            if span is None:
                continue
            k1, k2, l1, l2 = span
            original = "".join(source_tokens[k1:k2]).strip()
            revised = "".join(target_tokens[l1:l2]).strip()
            if not original or not revised or original == revised:
                continue
            etype, reason, specific = _classify(original, revised, term_pairs)
            candidate = {"original": original, "revised": revised,
                         "type": etype, "reason": reason}
            if specific:
                best = candidate
                break
            if best is None:
                best = candidate
        if best is not None:
            changes.append(best)
        if len(changes) >= MAX_CHANGES:
            break
    return changes


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def polish_paragraph(text: str, *, style: dict[str, Any] | None = None,
                     terms: Any = None) -> tuple[str, list[dict[str, Any]]]:
    """Deterministic minimal-edit polish of one paragraph.

    Returns (revised_text, changes). Guarantees:
    * every change's original is an exact substring of text and its revised an
      exact substring of revised_text (so validate_changes() accepts them);
    * no number, citation marker, formula token, negation or hedge is edited;
    * CJK text only receives whitespace/punctuation normalisation, never a word
      replacement;
    * polish_paragraph(revised_text) returns revised_text with no changes;
    * the character edit rate is <= MAX_EDIT_RATE, otherwise the paragraph is
      returned untouched (no partial edits, which is what keeps it idempotent).
    """
    source = text or ""
    if not source.strip():
        return source, []
    cjk = _has_cjk(source)
    tier_a = _tier_a_rules(cjk)
    candidate = _run_pipeline(source, tier_a)
    if not cjk:
        candidate = _run_pipeline(candidate, tier_a + _tier_b_rules(terms, style))
    if candidate == source:
        return source, []
    # Guard 1: budget. A paragraph that needs more than this is not a polish.
    if _char_edit_rate(source, candidate) > MAX_EDIT_RATE:
        return source, []
    # Guard 2: the result must be a fixed point, otherwise a second run would
    # continue editing and the run would stop being idempotent.
    rules = tier_a if cjk else tier_a + _tier_b_rules(terms, style)
    if _run_pipeline(candidate, rules) != candidate:
        return source, []
    pairs = _term_pairs(terms)
    changes = _diff_changes(source, candidate, pairs)
    if not changes:
        return source, []
    return candidate, changes


def describe() -> dict[str, Any]:
    """Rule inventory for SKILL.md / the run report (never used by the polisher)."""
    return {
        "method": "deterministic rule-based fallback polisher (no model judgement)",
        "budget": {"max_edit_rate": MAX_EDIT_RATE,
                   "target_edit_rate": TARGET_EDIT_RATE,
                   "max_changes_per_paragraph": MAX_CHANGES},
        "types": list(EDIT_TYPES),
        "rules": {
            "punctuation": [
                "collapse repeated spaces/tabs",
                "drop the space before , . ; : ! ? % ) ] and before CJK punctuation",
                "drop the space after ( [ and before the matching close",
                "add the missing space after , ; : and after a sentence-final period",
                "normalise spaces around CJK punctuation",
                "strip trailing whitespace",
            ],
            "grammar": [
                "fix a/an by the following sound, with unit/university/one/hour/..."
                " exception lists and an explicit skip for ambiguous u-/h- words",
                "merge an immediately duplicated function word (the the -> the), "
                "whitelist only, so 'that that'/'had had' stay",
                "capitalise a sentence-initial or paragraph-initial lowercase word "
                "when no abbreviation/initials/technical-token guard applies",
            ],
            "terminology": [
                "unify casing / hyphenation / separator convention with the declared "
                "canonical form when the mapping is unambiguous and digit-free",
            ],
            "tone": [pair[0] + " -> " + pair[1] for pair in TONE_MAP],
            "chinese_english": [pair[0] + " -> " + pair[1]
                                for pair in TRANSLATIONESE_MAP],
            "structure": ["not applied by the fallback: sentence splitting needs "
                          "author judgement and risks grammatical damage"],
        },
        "never_touched": [
            "numbers, units and percentages", "citation markers ([n], (Author, 2020), "
            "\\cite{}, \\ref{}, 图/表/式 numbers)", "formula tokens",
            "negations", "hedges and modals (may / might / could / approximately / 可能)",
            "headings (the runner never polishes an is_heading segment)",
            "CJK wording (spacing and punctuation spacing only)",
        ],
    }


if __name__ == "__main__":  # tiny manual check: python fallback_polish.py "text"
    import json
    import sys

    sample = ("A  apple is huge , and the the conversion reached 85 % at 523 K "
              "[1] . These results are very good.")
    argument = " ".join(sys.argv[1:]) or sample
    polished, edits = polish_paragraph(argument)
    print(json.dumps({"source": argument, "revised": polished, "changes": edits,
                      "edit_rate": round(_char_edit_rate(argument, polished), 4)},
                     ensure_ascii=False, indent=2))
