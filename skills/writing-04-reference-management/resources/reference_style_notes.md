# Reference style and dedupe notes (reusable resource)

This note is the quick-reference companion to SKILL.md. It collects the field
rules the formatters implement and the duplicate criteria the report uses, so a
reviewer can check a formatted entry or a merge decision without reading code.

## 1. GB/T 7714-2015 (numeric / 顺序编码制)

| Type | Template |
|---|---|
| Journal article | Author. Title[J]. Journal, Year, Volume(Issue): Pages. DOI: 10.xxxx/yyy. |
| Book | Author. Title[M]. Edition. Place: Publisher, Year: Pages. |
| Chapter / conference | Author. Title[C]//Proceedings title. Place: Publisher, Year: Pages. |
| Thesis | Author. Title[D]. Place: School, Year. |
| Report | Author. Title[R]. Place: Publisher, Year. |
| Preprint / web | Author. Title[EB/OL]. (Year). URL |

Author rule: <= 3 authors are all listed; more than 3 -> first 3 then ", et al."
(Latin script) or ", 等" (Chinese). Latin names are "SURNAME I I" (surname upper
case, initials without periods). Chinese names are written as the family name.

Numbering rule: numbers follow order of first appearance in the body; the list is
numbered 1..N with no gaps; consecutive runs of three or more collapse to "1-3".
Uncited entries are not listed (they are reported in quality/uncited_entries).

## 2. APA 7th (author-date)

| Type | Template |
|---|---|
| Journal article | Author, A. A., & Author, B. B. (Year). Title. Journal, Volume(Issue), pages. https://doi.org/... |
| Book | Author, A. A. (Year). Title (edition). Publisher. |
| Chapter | Author, A. A. (Year). Title. In E. Editor (Ed.), Book title (pp. x-y). Publisher. |
| Conference paper | Author, A. A. (Year). Title. In Proceedings title (pp. x-y). Publisher. |
| Preprint | Author, A. A. (Year). Title [Preprint]. Server. URL |
| Thesis | Author, A. A. (Year). Title [Doctoral dissertation, School]. URL |

Author rule: list up to 20 authors with ", & " before the last; more than 20 lists
the first 19, then ", ... ", then the last author. Initials carry periods. Year
missing -> (n.d.). Page ranges use an en dash. Chinese author strings are not
inverted and do not use "&".

In-text rule: one author "(Smith, 2020)"; two authors "(Smith & Jones, 2020)";
three or more "(Smith et al., 2020)"; same author and year get a/b suffixes
assigned by title order. Narrative markers keep the author in the sentence
("Smith et al. (2020) reported ..."). Chinese: "（张三等, 2020）".

## 3. Duplicate criteria (as implemented)

| Criterion | Confidence | Auto-merge |
|---|---|---|
| Same normalised DOI | 1.00 | yes |
| Same normalised title (LaTeX/punctuation/case removed) | 0.95 | yes, unless both DOIs differ -> conflict |
| Near-identical title + one side is a preprint record | 0.92 | yes |
| Title similarity >= 0.96 + same first author | 0.90 | yes |
| Title similarity >= 0.90 + same first author + year within 1 | 0.90 | yes |
| Chinese title vs Latin title + >= 2 shared bibliographic signals (year, pages, volume, venue token) | 0.30-0.60 | no: needs_review |
| Title similarity >= 0.86 + year within 1, different authors | - | no: needs_review |
| Near-identical title but conflicting DOIs | - | no: conflicts (metadata error) |

Canonical record preference: published over preprint, DOI present, higher field
completeness, non-misc type, longer title. Missing fields are filled from the
other members and each fill is recorded in field_sources. All member keys stay as
aliases, so in-text citations keep resolving after a merge.

## 4. Machine checks a reviewer can rerun

* numbering 1..N continuous, no duplicate numbers;
* every in-text marker resolves (or is listed in unresolved);
* every bibliography entry is cited (or listed in uncited_entries);
* DOI matches 10.\d{4,9}/\S+;
* every number inside a formatted entry comes from one of its parsed fields
  (no_fabricated_fields);
* formatted list length == number of cited entries;
* every claim/marker/entry anchor re-verified against the extracted source
  (exact / normalised / block_only / page_only / missing).
