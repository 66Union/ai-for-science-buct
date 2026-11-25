#!/usr/bin/env python3
"""Synthetic fixtures for the reference-management self-test.

The fixtures are invented for testing only - they are not real papers and must
never be presented as formal case results. Two scenarios are built:

* gbt  - Chinese manuscript, numeric [n] citations, embedded numbered reference
  list, plus a BibTeX export that repeats some entries (same DOI, arXiv preprint
  of a published paper, Chinese/English pair) and adds one never-cited entry.
  Target style: GB/T 7714-2015.
* apa  - English manuscript with author-year markers and a RIS export.
  Target style: APA 7th.

The case.json contracts deliberately use field names that differ from the
internal keys, which is what proves the contract reflection really maps instead
of hardcoding a schema.
"""

from __future__ import annotations

import json
from pathlib import Path

GBT_MANUSCRIPT = """# 镍基催化剂在CO2加氢中的研究进展

## 摘要

本文综述了负载型镍催化剂在CO2加氢反应中的研究进展。文献报道CO2转化率可达85%[2]，甲烷选择性约为92%[5]。负载型镍催化剂在523 K下表现出较高活性[2,6]。表征结果表明金属分散度与活性显著相关[4]。

## 1 引言

CO2加氢制甲烷是重要的碳资源利用途径[5]。已有研究表明，Ni/Al2O3催化剂在低温下具有较高的CO2转化率（约80%）[6]，但积碳问题仍然存在[1]。近年来，负载型金属簇也被用于该反应[3]。上述结果与Wang等（2021）的报道相符。

## 2 方法

催化剂采用浸渍法制备，Ni负载量为10 wt%。表征采用XRD和TEM。反应在固定床反应器中进行。

## 3 结果与讨论

催化剂的比表面积通常在150-300 m²/g之间，这一参数会显著影响催化活性与长期稳定性。XRD结果表明Ni物种高度分散[4]。活性测试显示，在523 K时CO2转化率达到85%[2]。与文献报道一致[1,3]，选择性随温度升高而提高。催化剂稳定性测试表明，反应100 h后活性下降约15%[5]。

## 4 结论

镍基催化剂在CO2加氢反应中表现出良好性能，未来可进一步优化载体与助剂。

## 参考文献

[1] SMITH J, JONES M. Nickel catalysts for CO2 hydrogenation[J]. Journal of Catalysis, 2020, 12(3): 45-67.
[2] 张三, 李四. 镍基催化剂研究[J]. 催化学报, 2021, 41(3): 100-110.
[3] LEE C, WANG L, BROWN K, et al. Supported metal clusters for selective hydrogenation[C]//Proc. Int. Conf. Catalysis. New York: ACS, 2019: 1-10.
[4] 王五. 催化剂表征方法[M]. 北京: 科学出版社, 2018.
[5] CHEN H, ZHAO Y. Carbon deposition on Ni-based catalysts[J]. Catalysis Today, 2019, 330: 88-95. DOI: 10.1016/j.cattod.2018.11.032
[6] WANG X, LI Y. Single-atom Ni catalysts for CO2 methanation[J]. Nature Catalysis, 2021, 4(2): 210-220.
"""

GBT_BIB = """@article{smith2020,
  author = {Smith, John and Jones, Mary},
  title = {Nickel catalysts for {CO2} hydrogenation},
  journal = {Journal of Catalysis},
  year = {2020}, volume = {12}, number = {3}, pages = {45--67},
  doi = {10.1016/j.jcat.2020.01.001}
}
@article{smith2020dup,
  author = {Smith, John and Jones, Mary},
  title = {Nickel catalysts for CO2 hydrogenation},
  journal = {Journal of Catalysis},
  year = {2020}, volume = {12}, number = {3}, pages = {45--67},
  doi = {https://doi.org/10.1016/J.JCAT.2020.01.001}
}
@misc{smith2020pre,
  author = {Smith, John and Jones, Mary},
  title = {Nickel catalysts for CO2 hydrogenation},
  year = {2019}, eprint = {2001.00001}, archivePrefix = {arXiv},
  journal = {arXiv preprint arXiv:2001.00001}
}
@inproceedings{lee2019,
  author = {Lee, Chris and Wang, Li and Brown, Kate and Green, Tom},
  title = {Supported metal clusters for selective hydrogenation},
  booktitle = {Proc. Int. Conf. Catalysis}, year = {2019}, pages = {1--10},
  publisher = {ACS}, address = {New York}
}
@article{chen2019,
  author = {Chen, Hui and Zhao, Yan},
  title = {Carbon deposition on Ni-based catalysts},
  journal = {Catalysis Today}, year = {2019}, volume = {330}, pages = {88--95},
  doi = {10.1016/j.cattod.2018.11.032}
}
@article{uncited2022,
  author = {Brown, Kate},
  title = {A survey of catalyst deactivation},
  journal = {Catalysis Reviews}, year = {2022}, volume = {5}, number = {1}, pages = {1--20}
}
@article{zhang2021en,
  author = {Zhang, San and Li, Si},
  title = {Research on nickel-based catalysts},
  journal = {Chinese Journal of Catalysis}, year = {2021},
  volume = {41}, number = {3}, pages = {100--110}
}
"""

APA_MANUSCRIPT = """# Nickel catalysts for CO2 hydrogenation: a short review

## Abstract

Supported nickel catalysts convert CO2 at 85% at 523 K (Smith, 2020). The methane
selectivity reaches 92% (Jones et al., 2019). Metal dispersion correlates with
activity (Chen & Zhao, 2019). Single-atom catalysts were also reported (Wang & Li, 2021).

## 1 Introduction

CO2 hydrogenation is an important route for carbon utilisation (Smith, 2020).
Earlier work showed that Ni/Al2O3 converts around 80% of CO2 at low temperature
(Jones et al., 2019), although carbon deposition remains a problem (Chen & Zhao, 2019).

## 2 Methods

Catalysts were prepared by incipient wetness with 10 wt% Ni. Characterisation used
XRD and TEM. Activity was measured in a fixed-bed reactor.

## 3 Results

XRD showed highly dispersed Ni species (Chen & Zhao, 2019). Conversion reached 85%
at 523 K (Smith, 2020). Selectivity improved with temperature (Jones et al., 2019).
After 100 h the activity dropped by 15% (Chen & Zhao, 2019).

## References

Smith, J., & Jones, M. (2020). Nickel catalysts for CO2 hydrogenation. Journal of Catalysis, 12(3), 45-67.
Jones, M., Lee, C., & Brown, K. (2019). Supported metal clusters for selective hydrogenation. In Proc. Int. Conf. Catalysis (pp. 1-10).
Chen, H., & Zhao, Y. (2019). Carbon deposition on Ni-based catalysts. Catalysis Today, 330, 88-95.
Wang, X., & Li, Y. (2021). Single-atom Ni catalysts for CO2 methanation. Nature Catalysis, 4(2), 210-220.
"""

APA_RIS = """TY  - JOUR
AU  - Smith, John
AU  - Jones, Mary
TI  - Nickel catalysts for CO2 hydrogenation
JO  - Journal of Catalysis
PY  - 2020
VL  - 12
IS  - 3
SP  - 45
EP  - 67
DO  - 10.1016/j.jcat.2020.01.001
ER  -
TY  - CPAPER
AU  - Jones, Mary
AU  - Lee, Chris
AU  - Brown, Kate
TI  - Supported metal clusters for selective hydrogenation
T2  - Proc. Int. Conf. Catalysis
PY  - 2019
SP  - 1
EP  - 10
ER  -
TY  - JOUR
AU  - Chen, Hui
AU  - Zhao, Yan
TI  - Carbon deposition on Ni-based catalysts
JO  - Catalysis Today
PY  - 2019
VL  - 330
SP  - 88
EP  - 95
DO  - 10.1016/j.cattod.2018.11.032
ER  -
TY  - JOUR
AU  - Wang, Xin
AU  - Li, Yan
TI  - Single-atom Ni catalysts for CO2 methanation
JO  - Nature Catalysis
PY  - 2021
VL  - 4
IS  - 2
SP  - 210
EP  - 220
ER  -
"""

TASK_GBT = """# 任务

把稿件整理为可提交版本，参考文献采用 **GB/T 7714-2015（顺序编码制）**。

要求：
1. 正文引用编号按首次出现顺序重排，正文与文末列表同步。
2. 合并重复文献（同一 DOI、预印本与正式版）。
3. 输出可编辑稿件与同步的参考文献表。
"""

TASK_APA = """# Task

Normalise the manuscript to **APA 7th edition** (author-date) and produce an
editable manuscript plus a synchronised reference list. Merge duplicates and
report metadata problems.
"""

CASE_GBT = {
    "case_id": "fixture-citation-gbt-001",
    "target_style": "GB/T 7714-2015",
    "task": "引用编号重排 + 文献去重 + 格式转换 + 逐句论断核对",
    "output_contract": {
        "manuscript_final": {"type": "string", "description": "可编辑稿件全文（引用标记已同步）"},
        "works_cited": {"type": "array", "description": "文末参考文献表"},
        "intext_marker_trace": {"type": "array", "description": "每条正文引用的定位与改写记录"},
        "merged_bibliography_groups": {"type": "array", "description": "去重合并组与判据"},
        "field_problems": {"type": "array", "description": "元数据缺失/格式问题"},
        "sentence_level_support": {"type": "array", "description": "逐句论断-文献匹配"},
        "provenance_map": {"type": "array", "description": "证据锚点"},
        "target_style": {"type": "string", "description": "采用的参考文献规范"},
        "unmodelled_field_xyz": {"type": "string", "description": "故意声明但不可能产出的字段"},
    },
}

CASE_APA = {
    "case_id": "fixture-citation-apa-002",
    "target_style": "APA 7th",
    "task": "author-year 转换 + 重复合并 + 引用一致性",
    "output_contract": [
        {"name": "revised_manuscript", "type": "string"},
        {"name": "reference_entries", "type": "array"},
        {"name": "citation_locations", "type": "array"},
        {"name": "duplicate_groups", "type": "array"},
        {"name": "metadata_issues", "type": "array"},
        {"name": "claim_matches", "type": "array"},
        {"name": "evidence_anchors", "type": "array"},
        {"name": "limitations", "type": "array"},
    ],
}


def build_fixtures(root: Path) -> dict:
    """Create both fixtures under root and return their paths."""
    root.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in ("gbt", "apa"):
        paths[name] = root / name
        paths[name].mkdir(parents=True, exist_ok=True)
    (paths["gbt"] / "manuscript.md").write_text(GBT_MANUSCRIPT, encoding="utf-8")
    (paths["gbt"] / "references.bib").write_text(GBT_BIB, encoding="utf-8")
    (paths["gbt"] / "task.md").write_text(TASK_GBT, encoding="utf-8")
    (paths["gbt"] / "case.json").write_text(json.dumps(CASE_GBT, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    (paths["apa"] / "manuscript.md").write_text(APA_MANUSCRIPT, encoding="utf-8")
    (paths["apa"] / "refs.ris").write_text(APA_RIS, encoding="utf-8")
    (paths["apa"] / "task.md").write_text(TASK_APA, encoding="utf-8")
    (paths["apa"] / "case.json").write_text(json.dumps(CASE_APA, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    return paths
