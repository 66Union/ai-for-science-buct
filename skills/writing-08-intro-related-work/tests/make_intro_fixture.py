#!/usr/bin/env python3
"""Build the synthetic fixture used by tests/test_intro.py and by SKILL.md.

The fixture is deliberately small but exercises every branch the adapter needs:

* a Markdown manuscript with an abstract, an (old) Introduction, a Related Work
  section, Methods, Results, Conclusion and a reference list;
* a BibTeX library whose entry order defines the numeric citation index, with one
  entry that is never cited and one citation token ([9]) that is not in the library;
* a support note (results summary) and a task.md;
* a case.json whose output_contract field names are deliberately NOT the internal
  names, so the test proves the reflection actually happens.

Usage: python tests/make_intro_fixture.py [outdir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Import-compatible in both layouts (packaged: <skill>/tests + <skill>/lib; dev:
# <repo>/skills/<topic>/tests + <repo>/skills/_core/lib). Only used for the header
# marker the other scripts check; the fixture itself needs nothing from the kernel.
ROOT = Path(__file__).resolve().parents[1]
try:  # pragma: no cover - layout probe
    sys.path.insert(0, str(ROOT))
    from lib import hybrid  # noqa: F401
except ImportError:  # pragma: no cover - development layout
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import hybrid  # noqa: F401

MANUSCRIPT = """# A Low-Temperature Nickel Catalyst for Selective Methane Reforming

## Abstract

Supported nickel catalysts are widely used in methane reforming because of their low cost and high activity [1]. Their practical use is limited by sintering of the nickel particles above 973 K [1,2]. We report a nickel catalyst that keeps 92 percent selectivity at 750 K by dispersing nickel on a ceria-doped silica support, and the selectivity survives five regeneration cycles.

## 1 Introduction

Methane reforming over supported nickel is the workhorse route to synthesis gas [1]. Steam reforming is usually run between 973 K and 1123 K, and the nickel particles grow during operation [1,2]. Coke formation on the same particles competes with reforming and shortens catalyst life [3]. Kinetic models that describe the rate over nickel assume a fixed active surface area [4]. However, the sensitivity of selectivity to the nickel particle size at low temperature remains unclear, and a route that keeps selectivity at 750 K has not been demonstrated.

## 2 Related Work

Smith and Lee [1] measured sintering of nickel on silica under steam reforming and reported particle growth above 973 K. Chen and Zhao [2] activated methane on nickel surfaces below 673 K. Wang [3] mapped coke formation pathways in dry reforming over nickel on alumina. Kumar and Patel [4] built a kinetic model that assumes a constant active area. A computational study of nickel surfaces [9] is not directly comparable because it uses a machine-learned potential.

## 3 Methods

The catalyst precursor was dissolved in ethanol and dried at 373 K. Nickel loading was 5 weight percent, determined by inductively coupled plasma optical emission spectroscopy. The rate was normalised per active site, following (Kumar, 2017). Samples were reduced in hydrogen at 723 K for two hours.

## 4 Results

Conversion and selectivity are summarised in Table 1. Selectivity peaks at 750 K and reaches 92 percent, while conversion rises monotonically with temperature. Figure 2 shows the particle size distribution before and after the stability test. The selectivity of the catalyst is maintained after five regeneration cycles, as shown in Figure 2.

## 5 Conclusion

Dispersing nickel on ceria-doped silica keeps selectivity at 750 K and avoids the sintering that is reported for conventional nickel catalysts [1,5].

## References

[1] Smith J., Lee A. Sintering of supported nickel catalysts under steam reforming. J. Catal. 2019.
[2] Chen L., Zhao M. Low-temperature methane activation on nickel surfaces. ACS Catal. 2020.
[3] Wang H. Coke formation pathways in dry reforming over Ni/Al2O3. Catal. Today 2018.
[4] Kumar R., Patel N. Kinetic modelling of steam methane reforming over nickel. Chem. Eng. J. 2017.
[5] Garcia M. Operando X-ray studies of nickel catalyst deactivation. Appl. Catal. A 2021.
"""

BIB = r"""@article{smith2019,
  author = {Smith, John and Lee, Ann},
  title = {Sintering of supported nickel catalysts under steam reforming},
  journal = {Journal of Catalysis},
  year = {2019},
  doi = {10.1000/jcat.2019.001},
  abstract = {We show that nickel particles on silica sinter above 973 K under steam reforming and that the active surface area halves within 40 hours on stream.}
}

@article{chen2020,
  author = {Chen, Lei and Zhao, Mei},
  title = {Low-temperature methane activation on nickel surfaces},
  journal = {ACS Catalysis},
  year = {2020},
  abstract = {Nickel surfaces activate methane below 673 K in ultra-high vacuum experiments and the activation barrier depends on the step density.}
}

@article{wang2018,
  author = {Wang, Hui},
  title = {Coke formation pathways in dry reforming over nickel on alumina},
  journal = {Catalysis Today},
  year = {2018},
  abstract = {Coke forms through methane decomposition and carbon monoxide disproportionation on nickel alumina catalysts and covers the active sites within a few hours.}
}

@article{kumar2017,
  author = {Kumar, Raj and Patel, Nita},
  title = {Kinetic modelling of steam methane reforming over nickel},
  journal = {Chemical Engineering Journal},
  year = {2017}
}

@article{garcia2021,
  author = {Garcia, Maria},
  title = {Operando X-ray studies of nickel catalyst deactivation},
  journal = {Applied Catalysis A},
  year = {2021},
  abstract = {Operando X-ray scattering resolves nickel particle growth during the first hours on stream and links it to a loss of active surface.}
}

@article{li2022,
  author = {Li, Wei and Tan, Bo},
  title = {Machine-learned interatomic potentials for nickel surfaces},
  journal = {Physical Review B},
  year = {2022}
}
"""

SUPPORT_NOTE = """# Results summary (supporting note)

- Table 1: conversion and selectivity versus temperature (700, 750, 800 K).
- Figure 2: particle size distribution before and after 120 hours on stream.
- Selectivity at 750 K is 92 percent; conversion at 750 K is 88 percent.
- Regeneration cycles: 5, with selectivity unchanged after each cycle.
"""

TASK_MD = """# Task

Write the Introduction and the Related Work section for the draft in this folder.

Requirements:

1. Background must point at a concrete problem in the cited literature, not at
   "increasing attention".
2. Related work must be grouped by comparison dimension (problem setting, method
   family, data or evaluation regime) and must state each work's limitation.
3. The research gap must be derived from the cited works and must say how this paper
   fills it.
4. Every contribution must be checkable against the manuscript's own text, figures or
   tables.
5. Every key claim must keep a citation or an explicit evidence location.

Materials: draft.md, refs.bib, notes_results.md.
"""

# Field names are intentionally different from the adapter's internal keys: this is
# what proves result.json is reflected from the contract and not hard coded.
CASE = {
    "case_id": "writing08-demo-002",
    "task": "Draft the Introduction and Related Work for draft.md using refs.bib.",
    "output_contract": {
        "opening_background": {"type": "array",
                               "description": "background paragraphs with citations"},
        "related_works_comparison": {"type": "object",
                                     "description": "grouped comparison of prior work"},
        "contribution_statements": {"type": "array",
                                    "description": "checkable contribution claims"},
        "gap_statement": {"type": "array",
                          "description": "gaps derived from the cited works"},
        "citation_audit": {"type": "object", "description": "citation coverage audit"},
        "claim_support_map": {"type": "object", "description": "claim to evidence map"},
        "revised_introduction": {"type": "object",
                                 "description": "editable manuscript with the new sections"},
        "self_check": {"type": "object", "description": "quality and degradation report"},
        "evidence_anchors": {"type": "array", "description": "anchors for key claims"},
        "target_journal_style": {"type": "object",
                                 "description": "journal template, unknown for this case"},
    },
    "material": {"manuscript": "draft.md", "library": "refs.bib",
                 "note": "synthetic fixture, not a real publication"},
}


def build(out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "case.json").write_text(json.dumps(CASE, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    (out / "task.md").write_text(TASK_MD, encoding="utf-8")
    (out / "draft.md").write_text(MANUSCRIPT, encoding="utf-8")
    (out / "refs.bib").write_text(BIB, encoding="utf-8")
    (out / "notes_results.md").write_text(SUPPORT_NOTE, encoding="utf-8")
    return out


def main() -> int:
    target = build(sys.argv[1] if len(sys.argv) > 1 else
                   Path(__file__).resolve().parent / "fixture")
    print(f"fixture written to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
