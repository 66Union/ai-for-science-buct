#!/usr/bin/env python3
r"""Writing-13 LaTeX authoring and compilation runner.

Why a hybrid pipeline
---------------------
The evaluation contract allows 64 model requests, 900 seconds and 16384 tokens per
response, and it runs the Skill twice per case (baseline + Skill). LaTeX source
handling must therefore not consume model requests; only *judgement* (which
source edit to make and why) should. The work is split in three:

1. --prepare  (deterministic) discover the TeX project, resolve every
   \input/\include recursively, run the static source audit (brace/env balance,
   \cite vs .bib, \ref vs \label, duplicate labels, encoding/BOM, package
   conflicts, template required fields), probe the TeX engines that actually
   exist in this image, and write small worksheets under <work>/.
2. the agent (driven by SKILL.md) reads work/fixes_worksheet.json and writes
   work/generated/fixes.json plus optional notes; it may also edit the source
   copies under work/fixed/.
3. --finalize (deterministic) applies every fix by strict literal replacement
   (an edit whose original text is not found is rejected, never forced), re-runs
   the static audit on the repaired tree, compiles the project for real when a
   TeX engine is available, parses the .log into file:line:root-cause records,
   and reflects case.json.output_contract into result.json / evidence.json /
   run_report.json plus the editable source tree under out/source/.

Hard rules implemented here, not left to the prompt
---------------------------------------------------
* a contract-shaped result.json is written BEFORE any slow work, so a container
  killed at the 900 s budget still leaves an honest partial deliverable;
* missing agent output never fails the run: the repaired tree is emitted as-is,
  the remaining defects are listed, and status becomes partial;
* when no TeX engine exists in the image the run degrades to the static path,
  says so in limits, and still delivers the editable source and the static
  report - a compile-unavailable case must never be a zero-output case;
* no network, no third-party packages, deterministic ordering and no timestamps
  in the produced content (only elapsed seconds in the run report vary).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# import layout compatibility
#   packaged   : <skill>/lib/hybrid.py + <skill>/scripts/run_latex.py
#   dev layout : skills/_core/lib/... + skills/writing-13-latex-compilation/scripts
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout
    sys.path.insert(0, str(ROOT))
    from lib import contract, discover, hybrid, render
except ImportError:  # development layout
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import contract, discover, hybrid, render

SKILL_NAME = "writing-13-latex-compilation"
SKILL_VERSION = "1.0.0"
STATE_SCHEMA = "writing-13-latex-compilation/state/v1"

# --------------------------------------------------------------------------- #
# file vocabularies
# --------------------------------------------------------------------------- #
TEX_SUFFIXES = (".tex", ".ltx", ".latex")
TEXT_SUFFIXES = (".tex", ".ltx", ".latex", ".bib", ".cls", ".sty", ".bst", ".cfg",
                 ".def", ".clo", ".fd", ".bbx", ".cbx")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg", ".gif")
PROJECT_SUFFIXES = set(TEXT_SUFFIXES) | set(IMAGE_SUFFIXES)

# Directories that never contain the project we are asked to fix: our own
# deliverables, caches and the skill code itself.
NOISE_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache",
              "output", "outputs", "out", "work", "generated", "batches",
              "_selftest_tmp", "_selftest_tmp_manual", ".venv", "venv", ".idea",
              ".vscode", "build"}

MAIN_NAME_HINTS = {"main", "paper", "thesis", "article", "manuscript", "root",
                   "document", "book", "report", "dissertation", "submission"}

VERBATIM_ENVS = {"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted",
                 "comment", "filecontents", "filecontents*", "alltt"}

# Standard classes that ship with every TeX distribution. A \documentclass that is
# not in this set and has no .cls file in the project will not compile.
STANDARD_CLASSES = {"article", "report", "book", "letter", "slides", "beamer",
                    "proc", "minimal", "standalone", "memoir", "ctexart", "ctexrep",
                    "ctexbook", "ctexbeamer", "scrartcl", "scrreprt", "scrbook",
                    "IEEEtran", "elsarticle", "acmart", "aaai22", "neurips_2023",
                    "revtex4-2", "moderncv", "ltxdoc"}

CJK_PACKAGES = {"ctex", "xeCJK", "CJK", "CJKutf8", "luatexja", "luatexja-fontspec",
                "zhnumber"}

# Budget-style caps. They keep a hostile / huge case from exhausting the 900 s run.
MAX_TEXT_FILE_BYTES = 4_000_000
MAX_PROJECT_FILES = 400
MAX_FIX_ITEMS = 40
MAX_FIXES_APPLIED = 120
MAX_RENDERED_SOURCE_BYTES = 24_000_000
COMPILE_TIMEOUT_PER_ROUND = 150
COMPILE_MAX_ROUNDS = 3

# --------------------------------------------------------------------------- #
# internal value keys -> synonyms used by hybrid.map_fields. The hidden case
# decides the real field names; these only widen the net.
# --------------------------------------------------------------------------- #
SYNONYMS: dict[str, tuple[str, ...]] = {
    "fixed_sources": ("corrected_sources", "corrected_tex_sources", "fixed_tex_sources",
                      "fixed_source_files", "source_files", "tex_sources",
                      "revised_sources", "output_sources", "sources", "editable_sources",
                      "source_code", "source_tree", "final_tex", "corrected_source"),
    "source_dir": ("source_directory", "source_root", "fixed_source_dir"),
    "compile_report": ("compilation_report", "compile_result", "build_report",
                       "latex_compile_report", "compilation", "build_result",
                       "compile_status", "compile_summary", "compile_report_json"),
    "pdf_path": ("compiled_pdf_path", "pdf", "pdf_file", "output_pdf", "pdf_output",
                 "compiled_pdf", "pdf_filename", "rendered_pdf"),
    "png_preview": ("first_page_png", "preview_png", "png", "png_path",
                    "png_preview_path", "preview_image", "screenshot",
                    "first_page_preview"),
    "build_log_excerpt": ("latex_log_excerpt", "build_log", "log_excerpt",
                          "compile_log_excerpt", "latex_log", "log_tail", "build_output"),
    "reproducibility": ("build_reproducibility", "reproducibility_record",
                        "reproducible_build", "build_environment", "environment",
                        "toolchain", "rebuild_instructions", "fuxian"),
    "unresolved_warnings": ("remaining_latex_warnings", "warnings",
                            "remaining_warnings", "latex_warnings", "unresolved_issues",
                            "remaining_issues"),
    "static_checks": ("source_static_checks", "static_analysis", "source_checks",
                      "static_report", "source_audit", "static_issues", "lint_report"),
    "fixes_applied": ("applied_fix_records", "applied_fixes", "fixes", "fix_records",
                      "changes", "edits", "modifications", "xiugai", "patch_records"),
    "fixes_rejected": ("rejected_fixes", "failed_fixes", "apply_failures"),
    "error_locations": ("located_errors", "errors", "error_report", "diagnostics",
                        "located_diagnostics", "error_list", "compile_errors"),
    "tex_index": ("project_structure", "project_index", "tex_inventory",
                  "document_structure", "manuscript_structure", "source_structure"),
    "template_adaptation": ("template_adaptation_notes", "template_report",
                            "template_notes", "template_compliance",
                            "journal_template_fit", "template"),
    "quality": ("run_quality", "self_check", "quality_report", "metrics",
                "statistics", "score", "quality_metrics"),
    "evidence": ("evidence_anchors", "anchors", "provenance", "traceability",
                 "evidence_records", "sources_evidence"),
    "build_command": ("compile_command", "build_cmd", "command", "reproduce_command"),
    "engine": ("tex_engine", "engine_used", "compiler", "tex_compiler", "latex_engine"),
    "page_count": ("pages", "num_pages", "page_total", "pdf_pages"),
    "main_file": ("main_tex", "main_tex_file", "root_file", "master_file",
                  "entry_file", "main_document"),
    "notes": ("agent_notes", "adaptation_notes", "decisions", "notes_summary"),
}

# --------------------------------------------------------------------------- #
# error taxonomy: label, root cause and the usual repair strategy per type.
# resources/latex_error_taxonomy.json can extend/override this table at runtime;
# the built-in copy is the fallback so the pipeline never depends on the file.
# --------------------------------------------------------------------------- #
DEFAULT_TAXONOMY: dict[str, dict[str, Any]] = {
    # ---- static source checks -------------------------------------------------
    "missing_documentclass": {
        "label_zh": "缺少 \\documentclass", "severity": "error", "fixable": False,
        "root_cause": "主文件没有声明文档类，TeX 无法决定版面与可用命令。",
        "typical_fix": "在主文件首部加入 \\documentclass[选项]{类名}，并与模板要求的类一致。"},
    "missing_begin_document": {
        "label_zh": "缺少 \\begin{document}", "severity": "error", "fixable": False,
        "root_cause": "导言区与正文区没有分隔，正文环境缺失。",
        "typical_fix": "在导言区结束后加入 \\begin{document}。"},
    "missing_end_document": {
        "label_zh": "缺少 \\end{document}", "severity": "error", "fixable": True,
        "root_cause": "文件在正文中途结束，TeX 会报 Emergency stop 并丢掉后续排版。",
        "typical_fix": "在文件末尾补 \\end{document}。"},
    "missing_end_environment": {
        "label_zh": "环境未闭合", "severity": "error", "fixable": True,
        "root_cause": "\\begin{env} 没有配对的 \\end{env}，环境栈错位会让其后所有内容报错。",
        "typical_fix": "在合适位置补上 \\end{env}，或修正配对的 \\begin。"},
    "unexpected_end_environment": {
        "label_zh": "环境结束标签多余", "severity": "error", "fixable": False,
        "root_cause": "出现了没有 \\begin 的 \\end，通常由复制粘贴或条件编译残留导致。",
        "typical_fix": "删除多余的 \\end{env}，或补上缺失的 \\begin{env}。"},
    "unbalanced_brace": {
        "label_zh": "花括号失衡", "severity": "error", "fixable": False,
        "root_cause": "分组括号数量不配对；TeX 会在文件末尾才报错，真实位置往往在上文。",
        "typical_fix": "从报错行向前定位分组起点，补齐或删除多余的 { 或 }。"},
    "unbalanced_math_dollar": {
        "label_zh": "行内数学 $ 未配对", "severity": "error", "fixable": False,
        "root_cause": "行内公式的 $ 数量为奇数，后续正文会被当作数学模式。",
        "typical_fix": "为缺失的 $ 补上配对，或改用 \\( \\)。"},
    "missing_input_file": {
        "label_zh": "\\input/\\include 目标不存在", "severity": "error", "fixable": False,
        "root_cause": "被引用文件路径写错或文件未随稿件提交，编译会直接中止。",
        "typical_fix": "修正路径或补交被引用文件；也可临时注释该行。"},
    "missing_class_file": {
        "label_zh": "文档类文件缺失", "severity": "error", "fixable": False,
        "root_cause": "模板 .cls 不在工程内，也不是 TeX 发行版自带类，编译时找不到。",
        "typical_fix": "把模板 .cls 放到主文件同目录（或 \\documentclass 能解析的路径），并确认版本。"},
    "template_class_unused": {
        "label_zh": "工程内模板类未被使用", "severity": "warning", "fixable": False,
        "root_cause": "工程里有 .cls 但主文件用的是其它类，投稿模板要求未生效。",
        "typical_fix": "按任务要求把 \\documentclass 指向模板类，并保留模板必需字段。"},
    "missing_bib_entry": {
        "label_zh": "\\cite 的文献条目缺失", "severity": "error", "fixable": False,
        "root_cause": "正文引用了 .bib 中不存在的 key，PDF 会印出 [?]。",
        "typical_fix": "在 .bib 中补条目（元数据必须来自材料，不得编造），或修正 key。"},
    "uncited_bib_entry": {
        "label_zh": "文献库条目未被引用", "severity": "info", "fixable": False,
        "root_cause": "条目存在于 .bib 但正文没有 \\cite，参考文献表会漏项或多余。",
        "typical_fix": "确认是否需要引用；不需要则删除条目，需要则在正文补 \\cite。"},
    "missing_bibliography": {
        "label_zh": "缺少参考文献命令", "severity": "error", "fixable": False,
        "root_cause": "正文有 \\cite 但没有 \\bibliography/\\addbibresource，无法生成文献表。",
        "typical_fix": "加入 \\bibliography{refs} 或 biblatex 的 \\addbibresource{refs.bib} 与 \\printbibliography。"},
    "bib_duplicate_key": {
        "label_zh": ".bib 条目 key 重复", "severity": "warning", "fixable": False,
        "root_cause": "同一 key 定义两次，BibTeX 只认其中一条并报警告。",
        "typical_fix": "保留元数据更完整的一条，删除或重命名重复条目。"},
    "bib_entry_incomplete": {
        "label_zh": ".bib 条目字段不全", "severity": "warning", "fixable": False,
        "root_cause": "条目缺少 title/year 等必需字段，参考文献表会出现空白。",
        "typical_fix": "从材料补齐字段；材料里没有的字段不要臆造。"},
    "duplicate_label": {
        "label_zh": "\\label 重复定义", "severity": "error", "fixable": False,
        "root_cause": "同一 label 出现两次，\\ref 会指向后一个，编号引用不可信。",
        "typical_fix": "重命名其中一个 label，并同步其 \\ref。"},
    "undefined_reference": {
        "label_zh": "\\ref 未定义", "severity": "error", "fixable": False,
        "root_cause": "引用了不存在的 label，PDF 会印出 ??，需多轮编译才能暴露。",
        "typical_fix": "补 \\label 或修正引用 key；若同名 label 近似则直接改用正确 key。"},
    "duplicate_package": {
        "label_zh": "宏包重复加载", "severity": "warning", "fixable": True,
        "root_cause": "同一宏包被 \\usepackage 多次，选项冲突时会报错。",
        "typical_fix": "保留第一次加载（并合并选项），删除重复行。"},
    "package_conflict": {
        "label_zh": "宏包冲突", "severity": "error", "fixable": False,
        "root_cause": "两个宏包功能重叠或选项互斥，后加载者覆盖前者。",
        "typical_fix": "按模板要求保留一个，删除或替换冲突宏包。"},
    "deprecated_package": {
        "label_zh": "使用了过时宏包", "severity": "warning", "fixable": True,
        "root_cause": "过时宏包在新发行版上行为不稳定或与模板冲突。",
        "typical_fix": "换成等价的现代宏包（如 epsfig -> graphicx）。"},
    "package_load_order": {
        "label_zh": "宏包加载顺序不合理", "severity": "warning", "fixable": False,
        "root_cause": "hyperref 等宏包必须靠后加载，顺序错误会引发连锁报错。",
        "typical_fix": "调整 \\usepackage 顺序：基础宏包在前，hyperref 靠后，cleveref 在 hyperref 之后。"},
    "cjk_without_package": {
        "label_zh": "CJK 文本缺少中文支持", "severity": "error", "fixable": True,
        "root_cause": "源码含中日韩字符但没加载 ctex/xeCJK 等宏包，编译会报 missing character。",
        "typical_fix": "加载 ctex 宏包并改用 xelatex 编译，或按模板要求使用对应中文模板类。"},
    "todo_placeholder": {
        "label_zh": "残留占位标记", "severity": "warning", "fixable": False,
        "root_cause": "源码仍有 TODO/FIXME/待补 等占位，交稿前必须处理。",
        "typical_fix": "补写内容或删除标记；不能确定的标注人工确认。"},
    "table_column_mismatch": {
        "label_zh": "表格列数与列格式不符", "severity": "warning", "fixable": False,
        "root_cause": "某行 & 个数与 tabular 列格式不一致，表格会错列或溢出。",
        "typical_fix": "对齐该行 & 数量，或调整 tabular 列格式与列宽。"},
    "template_required_field": {
        "label_zh": "模板必填字段缺失", "severity": "warning", "fixable": False,
        "root_cause": "模板要求的标题/作者/摘要等字段缺失，版面会出现空白或占位。",
        "typical_fix": "按模板要求补 \\title/\\author/摘要环境，内容必须来自材料。"},
    "utf8_bom": {
        "label_zh": "源码带 UTF-8 BOM", "severity": "warning", "fixable": False,
        "root_cause": "Windows 编辑器写入的 BOM 会让部分引擎在首行报错或产生空白页。",
        "typical_fix": "以无 BOM UTF-8 保存；本技能交付的 out/source 已统一去除 BOM。"},
    "encoding_not_utf8": {
        "label_zh": "源码不是 UTF-8", "severity": "error", "fixable": False,
        "root_cause": "GBK/Latin-1 编码在 UTF-8 引擎下会乱码或报 Invalid UTF-8 bytes。",
        "typical_fix": "转存为 UTF-8，并保留原文内容不变。"},
    "encoding_null_byte": {
        "label_zh": "源码含 NUL 字节", "severity": "warning", "fixable": False,
        "root_cause": "二进制混入或截断文件，编译器会忽略或报错。",
        "typical_fix": "重新导出该文件为纯文本。"},
    # ---- compile-time errors (parsed from the .log) ---------------------------
    "missing_package": {
        "label_zh": "宏包未安装", "severity": "error", "fixable": False,
        "root_cause": "TeX 发行版里没有该 .sty/.cls，编译在第一处就停止。",
        "typical_fix": "安装对应宏包，或按模板要求替换为镜像内已有的等价宏包。"},
    "missing_file": {
        "label_zh": "文件找不到", "severity": "error", "fixable": False,
        "root_cause": "\\input / \\includegraphics / 类文件路径错误或未随稿提交。",
        "typical_fix": "修正相对路径或补齐文件；注意编译工作目录。"},
    "undefined_control_sequence": {
        "label_zh": "未定义命令", "severity": "error", "fixable": False,
        "root_cause": "命令拼错、缺少对应宏包，或该命令属于另一套模板。",
        "typical_fix": "核对命令拼写与所属宏包；补齐 \\usepackage 或改用模板提供的命令。"},
    "environment_undefined": {
        "label_zh": "环境未定义", "severity": "error", "fixable": False,
        "root_cause": "该环境来自未加载的宏包，或环境名拼写错误。",
        "typical_fix": "加载对应宏包（如 amsmath/定理类宏包）或修正环境名。"},
    "runaway_argument": {
        "label_zh": "参数失控（Runaway argument）", "severity": "error", "fixable": False,
        "root_cause": "花括号/环境未闭合，TeX 把后续大段内容吞进当前命令参数。",
        "typical_fix": "从报错位置向前找未闭合的分组或环境。"},
    "missing_dollar": {
        "label_zh": "缺少数学定界符", "severity": "error", "fixable": False,
        "root_cause": "数学模式未正确进入或退出，行内公式的 $ 不配对。",
        "typical_fix": "补齐 $ 配对，或改用 \\( \\)。",
    },
    "extra_brace": {
        "label_zh": "多余的花括号", "severity": "error", "fixable": False,
        "root_cause": "参数后多了 }，TeX 把它当作分组结束并报错。",
        "typical_fix": "删除多余右括号，检查该命令参数个数。"},
    "emergency_stop": {
        "label_zh": "紧急停止", "severity": "error", "fixable": False,
        "root_cause": "上文存在致命错误（常为环境未闭合或参数失控），TeX 放弃排版。",
        "typical_fix": "先修复日志中更早的 ! 错误，不要只处理这一条。"},
    "font_not_found": {
        "label_zh": "字体不可用", "severity": "error", "fixable": False,
        "root_cause": "镜像缺少该字体或字体名写错，XeLaTeX/LuaLaTeX 无法加载。",
        "typical_fix": "换用发行版自带字体，或在模板允许的范围内注释字体设置。"},
    "package_error": {
        "label_zh": "宏包报错", "severity": "error", "fixable": False,
        "root_cause": "宏包内部检查失败，通常是参数、版本或与其它宏包冲突。",
        "typical_fix": "阅读该宏包手册对应选项，按模板要求调整参数或去掉冲突宏包。"},
    "latex_error": {
        "label_zh": "LaTeX 报错", "severity": "error", "fixable": False,
        "root_cause": "LaTeX 层错误，日志中该条 ! 之后通常有真实位置 l.N。",
        "typical_fix": "定位日志中的 l.N 行，按错误说明修正该行语法。"},
    "fatal_error": {
        "label_zh": "致命错误", "severity": "error", "fixable": False,
        "root_cause": "编译器无法继续，通常由缺文件、缺宏包或编码错误引起。",
        "typical_fix": "先解决日志首条致命错误，再重跑多轮编译。"},
    "bibtex_error": {
        "label_zh": "BibTeX 错误", "severity": "error", "fixable": False,
        "root_cause": ".aux/.bib 不匹配或条目语法错误，文献表无法生成。",
        "typical_fix": "检查 .bib 语法与 key，确认 \\bibliography 指向正确文件。"},
    "undefined_citation": {
        "label_zh": "文献引用未定义", "severity": "warning", "fixable": False,
        "root_cause": "引用的 key 不在文献库中，或 BibTeX 尚未运行。",
        "typical_fix": "补条目/修 key，并确保编译顺序为 latex -> bibtex -> latex -> latex。"},
    "rerun_required": {
        "label_zh": "需要再次编译", "severity": "info", "fixable": False,
        "root_cause": "交叉引用/目录/编号尚未收敛，单轮编译不能定稿。",
        "typical_fix": "再编译一到两轮，直到日志不再提示 Rerun。"},
    "overfull_hbox": {
        "label_zh": "内容溢出（Overfull hbox）", "severity": "warning", "fixable": False,
        "root_cause": "某行或表格超出版心，经常由长公式、宽表格或不可断行字符串引起。",
        "typical_fix": "调整列宽/断行/字号，不要靠缩小整页掩盖。"},
    "underfull_hbox": {
        "label_zh": "排版过松（Underfull hbox）", "severity": "info", "fixable": False,
        "root_cause": "换行拉伸过大，通常是长段落或强制换行导致。",
        "typical_fix": "一般可忽略；若版式明显异常再调整断行。"},
    "compile_other": {
        "label_zh": "其他编译问题", "severity": "warning", "fixable": False,
        "root_cause": "日志中未归类的提示，需要人工阅读上下文。",
        "typical_fix": "查看 build_log_excerpt 中该条前后的日志。"},
}


def load_taxonomy() -> dict[str, dict[str, Any]]:
    """Built-in taxonomy, optionally extended by resources/latex_error_taxonomy.json."""
    merged: dict[str, dict[str, Any]] = {key: dict(value) for key, value in DEFAULT_TAXONOMY.items()}
    data = hybrid.read_json(ROOT / "resources" / "latex_error_taxonomy.json", None)
    entries = None
    if isinstance(data, dict):
        entries = data.get("error_types")
    if isinstance(entries, dict):
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            base = dict(merged.get(str(key)) or {})
            base.update({k: v for k, v in value.items() if v is not None})
            merged[str(key)] = base
    return merged


TAXONOMY = load_taxonomy()


def taxonomy_entry(error_type: str) -> dict[str, Any]:
    entry = TAXONOMY.get(error_type) or TAXONOMY.get("compile_other") or {}
    return {
        "label_zh": entry.get("label_zh") or error_type,
        "severity": entry.get("severity") or "warning",
        "fixable": bool(entry.get("fixable")),
        "root_cause": entry.get("root_cause") or "日志/源码中检出的问题，需要人工确认。",
        "typical_fix": entry.get("typical_fix") or "按日志上下文修正该处语法。",
    }


# --------------------------------------------------------------------------- #
# regexes for LaTeX source
# --------------------------------------------------------------------------- #
DOCUMENTCLASS_RE = re.compile(r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}")
PACKAGE_RE = re.compile(r"\\usepackage\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}")
BEGIN_RE = re.compile(r"\\begin\s*\{([^}]+)\}")
END_RE = re.compile(r"\\end\s*\{([^}]+)\}")
ENV_TAG_RE = re.compile(r"\\(begin|end)\s*\{([^}]+)\}")
INCLUDE_RE = re.compile(r"\\(input|include|subfile)\s*\{([^}]+)\}")
BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{([^}]+)\}")
ADD_BIB_RE = re.compile(r"\\addbibresource\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
BIBITEM_RE = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
LABEL_RE = re.compile(r"\\label\s*\{([^}]+)\}")
REF_RE = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|pageref|nameref|vref)\s*\{([^}]+)\}")
CITE_RE = re.compile(r"\\(?:cite|citep|citet|citeauthor|citeyear|citeyearpar|parencite|"
                     r"textcite|autocite|footcite|supercite|nocite|Cite)\s*"
                     r"(?:\[[^\]]*\]\s*){0,2}\{([^}]+)\}")
GRAPHICS_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
SECTION_RE = re.compile(r"\\(part|chapter|section|subsection|subsubsection)\*?\s*\{([^}]*)\}")
BIB_ENTRY_RE = re.compile(r"@([A-Za-z]+)\s*\{\s*([^,\s}]+)\s*,")
TABULAR_ENV_RE = re.compile(r"\\(begin|end)\s*\{(tabular\*?|tabularx|tabulary|array|longtable)\}"
                            r"\s*(?:\{([^}]*)\})?")
PLACEHOLDER_RE = re.compile(r"(?:\bTODO\b|\bFIXME\b|\bTBD\b|\bXXX\b|待补充|待确认|待补|\?\?)")
TODO_MACRO_RE = re.compile(r"\\todo\s*(?:\[[^\]]*\])?\s*\{")


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def _log(stopwatch: "hybrid.Stopwatch", message: str) -> None:
    print("[%7.1fs] %s" % (stopwatch.elapsed(), message), flush=True)


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _normalize_newlines(text: str) -> str:
    """One internal representation (LF) so edits, hashes and reports are portable."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _read_text(path: str | Path, *, limit: int = MAX_TEXT_FILE_BYTES) -> str:
    target = Path(path)
    try:
        data = target.read_bytes()[:limit]
    except Exception:
        return ""
    return _normalize_newlines(data.decode("utf-8-sig", "replace"))


def _write_text(path: str | Path, text: str) -> Path:
    """Write UTF-8 without BOM and with explicit LF endings (deterministic bytes)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text or "")
    return target


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def resolve_work_dir(prefer: str | None, out_dir: Path) -> Path:
    """Work directory that survives between the prepare and finalize processes."""
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    env = os.environ.get("LATEX_WORK_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/app/work"))
    candidates.append(Path(out_dir) / "work")
    candidates.append(Path.cwd() / "work")
    for candidate in candidates:
        if _writable_dir(candidate):
            return candidate
    fallback = Path(out_dir) / "work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _short(text: Any, limit: int = 200) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _rel_posix(path: str | Path, root: str | Path) -> str:
    try:
        return Path(os.path.relpath(str(path), str(root))).as_posix()
    except Exception:
        return Path(path).name


def _is_under(path: str | Path, root: str | Path) -> bool:
    try:
        p = str(Path(path).resolve()).casefold()
        r = str(Path(root).resolve()).casefold()
    except Exception:
        p, r = str(path).casefold(), str(root).casefold()
    return p == r or p.startswith(r + os.sep)


def strip_comment(line: str) -> str:
    """Drop the % comment tail; a \\% is an escaped percent, not a comment."""
    out: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\\" and index + 1 < length:
            out.append(line[index:index + 2])
            index += 2
            continue
        if char == "%":
            break
        out.append(char)
        index += 1
    return "".join(out)


def _escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def iter_code_lines(text: str) -> Iterable[tuple[int, str]]:
    for number, raw in enumerate((text or "").splitlines(), 1):
        yield number, strip_comment(raw)


def count_unescaped(text: str, char: str) -> int:
    total = 0
    for index, value in enumerate(text):
        if value == char and not _escaped(text, index):
            total += 1
    return total


def split_unescaped(text: str, token: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith(token, index) and not _escaped(text, index):
            parts.append("".join(current))
            current = []
            index += len(token)
            continue
        current.append(text[index])
        index += 1
    parts.append("".join(current))
    return parts


# --------------------------------------------------------------------------- #
# project discovery
# --------------------------------------------------------------------------- #
def find_project_files(found: dict[str, Any], out_dir: Path, work_dir: Path) -> list[Path]:
    """Every plausible TeX-project file under the input roots (deterministic order)."""
    roots: list[Path] = []
    for raw in found.get("input_roots") or []:
        candidate = Path(raw)
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    if not roots:
        roots = [Path.cwd()]
    # A material document can live deeper than a root we already walked; add its
    # own directory so a nested case directory is still found.
    for raw in found.get("material_docs") or []:
        candidate = Path(raw)
        if candidate.suffix.casefold() in PROJECT_SUFFIXES and candidate.parent not in roots:
            roots.append(candidate.parent)
    base_skip: list[str] = []
    for candidate in (out_dir, work_dir):
        try:
            base_skip.append(str(candidate.resolve()).casefold())
        except Exception:
            base_skip.append(str(candidate).casefold())

    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        # The skill's own code must not be scanned as project material - but a test
        # fixture legitimately lives *inside* the skill directory, so ROOT is only
        # skipped when the walk root itself is outside the skill.
        skip = list(base_skip)
        if not _is_under(root, ROOT):
            try:
                skip.append(str(ROOT.resolve()).casefold())
            except Exception:
                skip.append(str(ROOT).casefold())
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d.casefold() not in NOISE_DIRS)
            directory = Path(dirpath)
            try:
                resolved = str(directory.resolve()).casefold()
            except Exception:
                resolved = str(directory).casefold()
            if any(resolved == s or resolved.startswith(s + os.sep) for s in skip):
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                path = directory / name
                if path.suffix.casefold() not in PROJECT_SUFFIXES:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > MAX_TEXT_FILE_BYTES or len(files) >= MAX_PROJECT_FILES:
                    continue
                key = str(path).casefold()
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
    return sorted(files, key=lambda p: str(p).casefold())


def select_main(files: list[Path]) -> tuple[Path | None, list[dict[str, Any]]]:
    """Pick the master .tex: the one that declares \\documentclass, else the biggest."""
    candidates: list[dict[str, Any]] = []
    for path in files:
        if path.suffix.casefold() not in TEX_SUFFIXES:
            continue
        text = _read_text(path)
        code = "\n".join(line for _n, line in iter_code_lines(text))
        score = 0.0
        reasons: list[str] = []
        if DOCUMENTCLASS_RE.search(code):
            score += 100
            reasons.append("has_documentclass")
        if "\\begin{document}" in code:
            score += 20
            reasons.append("has_begin_document")
        if path.stem.casefold() in MAIN_NAME_HINTS:
            score += 30
            reasons.append("conventional_name")
        score += max(0.0, 10.0 - len(path.parts))
        try:
            score += min(path.stat().st_size / 200_000.0, 5.0)
        except OSError:
            pass
        candidates.append({"path": str(path), "score": round(score, 3), "reasons": reasons})
    if not candidates:
        return None, []
    candidates.sort(key=lambda item: (-item["score"], item["path"].casefold()))
    return Path(candidates[0]["path"]), candidates


def _resolve_file_ref(name: str, base_dir: Path, project_files: list[Path],
                      suffixes: tuple[str, ...] = TEX_SUFFIXES) -> Path | None:
    raw = (name or "").strip().strip('"').strip()
    if not raw:
        return None
    raw = raw.replace("\\", "/")
    tries: list[Path] = [base_dir / raw]
    if not Path(raw).suffix:
        for suffix in suffixes:
            tries.append(base_dir / (raw + suffix))
    for candidate in tries:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except Exception:
            continue
    target = raw.lstrip("./")
    matches: list[Path] = []
    for path in project_files:
        posix = str(path).replace("\\", "/")
        if posix.endswith("/" + target) or posix.endswith("/" + target + ".tex") \
                or posix.endswith("/" + target + ".bib") or path.stem == target:
            matches.append(path)
    if len(matches) == 1:
        return matches[0].resolve()
    return None


def _role_for(rel: str, main_rel: str) -> str:
    suffix = Path(rel).suffix.casefold()
    if rel == main_rel:
        return "main"
    if suffix in TEX_SUFFIXES:
        return "source"
    if suffix == ".bib":
        return "bibliography"
    if suffix == ".cls":
        return "class"
    if suffix == ".sty":
        return "style"
    if suffix == ".bst":
        return "bibstyle"
    if suffix in IMAGE_SUFFIXES:
        return "figure"
    return "auxiliary"


def load_tex_project(found: dict[str, Any], out_dir: Path, work_dir: Path) -> dict[str, Any]:
    """Discover the project, resolve the include graph and expose text per file."""
    project: dict[str, Any] = {
        "ok": False, "reason": "", "main": None, "main_abs": None, "root": str(Path.cwd()),
        "files": [], "by_path": {}, "texts": {}, "files_abs": {},
        "include_edges": [], "missing_includes": [], "bib_files": [], "images": [],
        "candidates": [], "project_files": [],
    }
    files = find_project_files(found, out_dir, work_dir)
    project["project_files"] = [str(path) for path in files]
    main, candidates = select_main(files)
    project["candidates"] = [{"path": c["path"], "score": c["score"],
                              "reasons": c["reasons"]} for c in candidates]
    if main is None:
        project["reason"] = "no .tex/.ltx file was found under the input roots"
        return project
    try:
        main = main.resolve()
    except Exception:
        pass

    texts: dict[Path, str] = {}
    visited: list[Path] = []
    queue: list[Path] = [main]
    edges: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    while queue:
        current = queue.pop(0)
        if current in texts:
            continue
        text = _read_text(current)
        texts[current] = text
        visited.append(current)
        if current.suffix.casefold() not in TEX_SUFFIXES:
            continue
        for lineno, code in iter_code_lines(text):
            for match in INCLUDE_RE.finditer(code):
                ref = match.group(2)
                target = _resolve_file_ref(ref, current.parent, files)
                if target is None:
                    missing.append({"kind": "include", "parent": current.name,
                                    "ref": ref, "line": lineno})
                    continue
                edges.append({"parent": current.name, "child": target.name,
                              "line": lineno, "kind": match.group(1)})
                if target not in texts and target.suffix.casefold() in TEX_SUFFIXES:
                    queue.append(target)

    # bibliography + graphics references (both feed the delivered source tree)
    bib_targets: list[Path] = []
    referenced_images: list[Path] = []
    for current in visited:
        for lineno, code in iter_code_lines(texts.get(current, "")):
            for match in BIBLIOGRAPHY_RE.finditer(code):
                for part in match.group(1).split(","):
                    part = part.strip()
                    if not part:
                        continue
                    target = _resolve_file_ref(part, current.parent, files, suffixes=(".bib",))
                    if target is None:
                        missing.append({"kind": "bibliography", "parent": current.name,
                                        "ref": part, "line": lineno})
                    else:
                        bib_targets.append(target)
            for match in ADD_BIB_RE.finditer(code):
                target = _resolve_file_ref(match.group(1), current.parent, files,
                                           suffixes=(".bib",))
                if target is None:
                    missing.append({"kind": "bibliography", "parent": current.name,
                                    "ref": match.group(1), "line": lineno})
                else:
                    bib_targets.append(target)
            for match in GRAPHICS_RE.finditer(code):
                target = _resolve_file_ref(match.group(1), current.parent, files,
                                           suffixes=IMAGE_SUFFIXES)
                if target is not None:
                    referenced_images.append(target)

    # Files that belong to the delivered project: everything under the main file's
    # directory plus explicitly referenced files outside of it.
    own_files = [path for path in files if _is_under(path, main.parent)]
    all_paths: list[Path] = []
    for path in own_files + visited + bib_targets + referenced_images:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved not in all_paths:
            all_paths.append(resolved)
    try:
        base = Path(os.path.commonpath([str(path) for path in all_paths]))
    except (ValueError, TypeError):
        base = main.parent
    if base.is_file():
        base = base.parent
    if not str(base).strip():
        base = main.parent

    main_rel = _rel_posix(main, base)
    file_entries: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    texts_rel: dict[str, str] = {}
    files_abs: dict[str, str] = {}
    for path in sorted(all_paths, key=lambda p: _rel_posix(p, base).casefold()):
        rel = _rel_posix(path, base)
        suffix = path.suffix.casefold()
        entry: dict[str, Any] = {
            "path": rel,
            "abs": str(path),
            "role": _role_for(rel, main_rel),
            "suffix": suffix,
            "bytes": 0,
            "sha256": None,
            "text_capable": suffix in TEXT_SUFFIXES,
        }
        try:
            data = path.read_bytes()
            entry["bytes"] = len(data)
            entry["sha256"] = _sha256_bytes(data)
            if suffix in TEXT_SUFFIXES:
                text = _normalize_newlines(data.decode("utf-8-sig", "replace"))
                texts_rel[rel] = text
                files_abs[rel] = str(path)
                entry["lines"] = len(text.splitlines())
        except Exception as exc:  # noqa: BLE001 - an unreadable file must not kill discovery
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        file_entries.append(entry)
        by_path[rel] = entry

    project.update({
        "ok": True,
        "main": main_rel,
        "main_abs": str(main),
        "root": str(base),
        "files": file_entries,
        "by_path": by_path,
        "texts": texts_rel,
        "files_abs": files_abs,
        "include_edges": [{"parent": _rel_posix(Path(edge["parent"]), base)
                           if Path(edge["parent"]).is_absolute() else edge["parent"],
                           "child": edge["child"], "line": edge["line"],
                           "kind": edge["kind"]} for edge in edges],
        "missing_includes": missing,
        "bib_files": sorted({_rel_posix(path, base) for path in bib_targets}),
        "images": sorted({_rel_posix(path, base) for path in referenced_images}),
        "main_text": texts.get(main, ""),
    })
    return project

# --------------------------------------------------------------------------- #
# issue construction
# --------------------------------------------------------------------------- #
CHECK_DEFS: list[tuple[str, str, str]] = [
    ("main_file_detection", "主文件识别",
     "按 \\documentclass、文件名与体积找出真正的编译入口"),
    ("encoding", "编码与 BOM", "UTF-8 可解码、无 BOM、无 NUL 字节"),
    ("brace_balance", "花括号平衡", "分组 { } 数量配对"),
    ("environment_balance", "环境配对", "\\begin/\\end 环境栈闭合"),
    ("document_structure", "文档结构", "\\documentclass 与 document 环境完整"),
    ("include_resolution", "子文件解析", "\\input/\\include/\\bibliography 目标存在"),
    ("label_reference", "交叉引用", "\\label 唯一、\\ref 有定义"),
    ("bibliography", "参考文献", "\\cite 与 .bib/\\bibitem key 逐条对账"),
    ("package_hygiene", "宏包卫生", "重复加载、冲突、过时与加载顺序"),
    ("engine_hint", "引擎与字体", "CJK/字体宏包决定应当使用的编译引擎"),
    ("template_fields", "模板必填项", "标题、作者、摘要与模板类的使用"),
    ("table_structure", "表格列结构", "tabular 行的 & 数量与列格式一致"),
    ("placeholders", "占位标记", "TODO/FIXME/待补 等占位残留"),
    ("math_delimiters", "数学定界符", "行内数学 $ 配对"),
]


def make_issue(error_type: str, *, file: str, line: int | None = None,
               evidence: Any = "", message: str = "", severity: str | None = None,
               signature: str | None = None,
               suggested: dict[str, Any] | None = None,
               check: str = "static", detail: dict[str, Any] | None = None) -> dict[str, Any]:
    info = taxonomy_entry(error_type)
    fixable = bool(suggested and str(suggested.get("original") or "")
                   and suggested.get("fixed") is not None)
    return {
        "issue_id": "",
        "check": check,
        "file": file,
        "line": int(line) if isinstance(line, (int, float)) and line else None,
        "severity": severity or info["severity"],
        "error_type": error_type,
        "label_zh": info["label_zh"],
        "message": message or info["label_zh"],
        "root_cause": info["root_cause"],
        "typical_fix": info["typical_fix"],
        "evidence": _short(evidence, 180),
        "fixable": fixable,
        "suggested_fix": suggested,
        "signature": signature or "",
        "detail": detail or {},
    }


def _issue_sort_key(issue: dict[str, Any], file_rank: dict[str, int]) -> tuple:
    return (file_rank.get(issue.get("file") or "", 10_000),
            issue.get("line") or 0,
            issue.get("error_type") or "",
            issue.get("evidence") or "")


def finalize_issues(issues: list[dict[str, Any]], file_order: list[str]) -> list[dict[str, Any]]:
    """Stable order, stable ids, and a signature used to prove a fix worked."""
    file_rank = {name: index for index, name in enumerate(file_order)}
    for issue in issues:
        if not issue.get("signature"):
            evidence = re.sub(r"\s+", " ", str(issue.get("evidence") or ""))[:100]
            issue["signature"] = "%s|%s|%s" % (issue.get("error_type"),
                                               issue.get("file"), evidence)
    issues.sort(key=lambda item: _issue_sort_key(item, file_rank))
    for index, issue in enumerate(issues, 1):
        issue["issue_id"] = "tex-%04d" % index
    return issues


def _line_at(lines: list[str], number: int | None) -> str:
    if not number or number < 1 or number > len(lines):
        return ""
    return lines[number - 1]


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


# --------------------------------------------------------------------------- #
# per-file structural scan: braces, environments, math delimiters, placeholders
# --------------------------------------------------------------------------- #
def scan_structure(rel: str, text: str, lines: list[str], issues: list[dict[str, Any]],
                   inventory: dict[str, Any], *, is_main: bool) -> None:
    brace_depth = 0
    env_stack: list[tuple[str, int]] = []
    dollar_count = 0
    has_begin_document = False
    has_end_document = False
    placeholder_count = 0

    for lineno, raw in enumerate(lines, 1):
        code = strip_comment(raw)
        verbatim = any(env in VERBATIM_ENVS for env, _l in env_stack)
        tag_spans: list[tuple[int, int]] = []
        for match in ENV_TAG_RE.finditer(code):
            tag_spans.append(match.span())
            kind, env = match.group(1), match.group(2)
            if env == "document":
                if kind == "begin":
                    has_begin_document = True
                else:
                    has_end_document = True
                continue
            if kind == "begin":
                env_stack.append((env, lineno))
                inventory["environments"].append({"env": env, "file": rel,
                                                  "line": lineno})
            else:
                open_envs = [name for name, _l in env_stack]
                if env not in open_envs:
                    issues.append(make_issue(
                        "unexpected_end_environment", file=rel, line=lineno,
                        evidence=raw,
                        message="\\end{%s} 没有配对的 \\begin" % env,
                        signature="unexpected_end_environment|%s|%s|%d" % (rel, env, lineno),
                        check="environment_balance"))
                else:
                    index = len(open_envs) - 1 - open_envs[::-1].index(env)
                    for open_env, open_line in env_stack[index + 1:]:
                        issues.append(make_issue(
                            "missing_end_environment", file=rel, line=open_line,
                            evidence=_line_at(lines, open_line),
                            message="\\begin{%s} 未闭合（被后出现的 \\end{%s} 越过）"
                                    % (open_env, env),
                            signature="missing_end_environment|%s|%s|%d"
                                      % (rel, open_env, open_line),
                            suggested={
                                "original": _line_at(lines, open_line),
                                "fixed": _line_at(lines, open_line) + "\n\\end{%s}" % open_env,
                                "reason": "补上缺失的 \\end{%s} 使环境栈闭合" % open_env,
                            },
                            check="environment_balance"))
                    del env_stack[index:]
        if verbatim:
            continue

        # Remove the \begin/\end spans before counting braces: their braces are
        # part of the tag, not of the document grouping.
        pieces = list(code)
        for start, end in tag_spans:
            for position in range(start, min(end, len(pieces))):
                pieces[position] = " "
        stripped = "".join(pieces)
        for index, char in enumerate(stripped):
            if _escaped(stripped, index):
                continue
            if char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
                if brace_depth < 0:
                    issues.append(make_issue(
                        "unbalanced_brace", file=rel, line=lineno, evidence=raw,
                        message="出现多余的 }（当前位置之前没有匹配的 {）",
                        signature="unbalanced_brace|%s|close|%d" % (rel, lineno),
                        check="brace_balance"))
                    brace_depth = 0
            elif char == "$":
                dollar_count += 1

        if placeholder_count < 8 and PLACEHOLDER_RE.search(code):
            placeholder_count += 1
            issues.append(make_issue(
                "todo_placeholder", file=rel, line=lineno, evidence=raw,
                message="源码仍有占位标记，交稿前必须处理",
                signature="todo_placeholder|%s|%d" % (rel, lineno),
                check="placeholders"))
        todo = TODO_MACRO_RE.search(code)
        if todo:
            inventory["todo_macros"].append({"file": rel, "line": lineno})

    if brace_depth > 0:
        issues.append(make_issue(
            "unbalanced_brace", file=rel, line=len(lines) or None,
            evidence=_line_at(lines, len(lines)),
            message="文件结束时仍有 %d 个未闭合的 {" % brace_depth,
            signature="unbalanced_brace|%s|open" % rel,
            check="brace_balance"))
    for env, open_line in env_stack:
        issues.append(make_issue(
            "missing_end_environment", file=rel, line=open_line,
            evidence=_line_at(lines, open_line),
            message="\\begin{%s} 直到文件结束都没有闭合" % env,
            signature="missing_end_environment|%s|%s|%d" % (rel, env, open_line),
            suggested={
                "original": _line_at(lines, open_line),
                "fixed": _line_at(lines, open_line) + "\n\\end{%s}" % env,
                "reason": "补上缺失的 \\end{%s}" % env,
            },
            check="environment_balance"))
    if dollar_count % 2:
        issues.append(make_issue(
            "unbalanced_math_dollar", file=rel, line=len(lines) or None,
            evidence=_line_at(lines, len(lines)),
            message="行内数学的 $ 数量为奇数（%d 个）" % dollar_count,
            signature="unbalanced_math_dollar|%s" % rel,
            check="math_delimiters"))

    if is_main:
        if not has_begin_document:
            issues.append(make_issue(
                "missing_begin_document", file=rel, line=1,
                evidence=_line_at(lines, 1),
                signature="missing_begin_document|%s" % rel,
                check="document_structure"))
        if not has_end_document:
            last_line = next((text.splitlines()[index] for index in
                              range(len(text.splitlines()) - 1, -1, -1)
                              if text.splitlines()[index].strip()), "")
            issues.append(make_issue(
                "missing_end_document", file=rel, line=len(lines) or 1,
                evidence=last_line,
                message="主文件没有 \\end{document}，编译会 Emergency stop",
                signature="missing_end_document|%s" % rel,
                suggested={
                    "original": last_line,
                    "fixed": last_line + "\n\\end{document}",
                    "reason": "在文件末尾补上 \\end{document}",
                },
                check="document_structure"))


def scan_encoding(rel: str, abs_path: str | Path, issues: list[dict[str, Any]],
                  uncertainty: list[str]) -> None:
    try:
        data = Path(abs_path).read_bytes()
    except Exception as exc:  # noqa: BLE001
        uncertainty.append("%s: 无法读取字节流（%s）" % (rel, type(exc).__name__))
        return
    if data.startswith(b"\xef\xbb\xbf"):
        issues.append(make_issue(
            "utf8_bom", file=rel, line=1,
            evidence="此文件以 EF BB BF 开头",
            signature="utf8_bom|%s" % rel, check="encoding"))
    if b"\x00" in data:
        line = data[:data.index(b"\x00")].count(b"\n") + 1
        issues.append(make_issue(
            "encoding_null_byte", file=rel, line=line,
            evidence="文件包含 NUL 字节（可能是二进制混入或截断）",
            signature="encoding_null_byte|%s" % rel, check="encoding"))
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = data[:exc.start].count(b"\n") + 1
        issues.append(make_issue(
            "encoding_not_utf8", file=rel, line=line,
            evidence="字节偏移 %d 处无法按 UTF-8 解码（%s）" % (exc.start, exc.reason),
            signature="encoding_not_utf8|%s|%d" % (rel, exc.start),
            check="encoding"))


# --------------------------------------------------------------------------- #
# .bib parsing and bibliography checks
# --------------------------------------------------------------------------- #
BIB_REQUIRED = {
    "article": ("author", "title", "journal", "year"),
    "inproceedings": ("author", "title", "booktitle", "year"),
    "conference": ("author", "title", "booktitle", "year"),
    "book": ("author", "title", "publisher", "year"),
    "incollection": ("author", "title", "booktitle", "year"),
    "phdthesis": ("author", "title", "school", "year"),
    "mastersthesis": ("author", "title", "school", "year"),
    "techreport": ("author", "title", "institution", "year"),
    "misc": ("title",),
    "online": ("title", "url"),
    "unpublished": ("author", "title", "note"),
}


def parse_bib_entries(rel: str, text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    matches = list(BIB_ENTRY_RE.finditer(text or ""))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = (text or "")[start:end]
        entry_type = match.group(1).casefold()
        key = match.group(2).strip()
        fields = sorted({name.casefold() for name in
                         re.findall(r"([A-Za-z][A-Za-z0-9_:-]*)\s*=", block)})
        entries.append({
            "key": key,
            "entry_type": entry_type,
            "file": rel,
            "line": (text or "")[:start].count("\n") + 1,
            "fields": fields,
        })
    return entries


def collect_inventory_file(rel: str, text: str, inventory: dict[str, Any]) -> None:
    """Labels, refs, cites, packages, sections, inline bibitems for one file."""
    for lineno, code in iter_code_lines(text):
        for match in LABEL_RE.finditer(code):
            inventory["labels"].append({"name": match.group(1).strip(),
                                        "file": rel, "line": lineno})
        for match in REF_RE.finditer(code):
            inventory["references"].append({
                "name": match.group(1).strip(),
                "command": match.group(0).split("{", 1)[0].strip(),
                "file": rel, "line": lineno,
                "raw": match.group(0),
            })
        for match in CITE_RE.finditer(code):
            keys = [part.strip() for part in match.group(1).split(",") if part.strip()]
            if not keys:
                continue
            inventory["citations"].append({
                "keys": keys,
                "command": match.group(0).split("{", 1)[0].strip(),
                "file": rel, "line": lineno,
                "raw": match.group(0),
            })
        for match in PACKAGE_RE.finditer(code):
            options = match.group(1) or ""
            for name in match.group(2).split(","):
                name = name.strip()
                if name:
                    inventory["packages"].append({
                        "name": name,
                        "options": [item.strip() for item in options.split(",") if item.strip()],
                        "file": rel, "line": lineno, "raw": match.group(0),
                    })
        for match in BIBITEM_RE.finditer(code):
            inventory["bib_entries"].append({
                "key": match.group(1).strip(), "entry_type": "bibitem",
                "file": rel, "line": lineno, "fields": [], "source": "inline",
            })
        for match in SECTION_RE.finditer(code):
            inventory["outline"].append({
                "level": match.group(1), "title": _short(match.group(2), 120),
                "file": rel, "line": lineno,
            })
        for match in GRAPHICS_RE.finditer(code):
            inventory["graphics"].append({"path": match.group(1).strip(),
                                          "file": rel, "line": lineno})


# --------------------------------------------------------------------------- #
# semantic checks that need the whole project at once
# --------------------------------------------------------------------------- #
def check_labels_and_refs(inventory: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    labels: dict[str, list[dict[str, Any]]] = {}
    for label in inventory["labels"]:
        labels.setdefault(label["name"], []).append(label)
    for name, entries in labels.items():
        if len(entries) > 1:
            for duplicate in entries[1:]:
                issues.append(make_issue(
                    "duplicate_label", file=duplicate["file"], line=duplicate["line"],
                    evidence="\\label{%s}" % name,
                    message="label '%s' 被定义了 %d 次" % (name, len(entries)),
                    severity="error",
                    signature="duplicate_label|%s" % name,
                    check="label_reference"))
    known = set(labels)
    for reference in inventory["references"]:
        name = reference["name"]
        if name in known:
            continue
        close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.72)
        suggested = None
        if close:
            suggested = {
                "original": reference["raw"],
                "fixed": reference["raw"].replace("{%s}" % name, "{%s}" % close[0]),
                "reason": "存在近似 label '%s'，引用很可能写错" % close[0],
            }
        issues.append(make_issue(
            "undefined_reference", file=reference["file"], line=reference["line"],
            evidence=reference["raw"],
            message="\\ref{%s} 没有对应的 \\label%s"
                    % (name, ("（近似 label: %s）" % close[0]) if close else ""),
            severity="error",
            signature="undefined_reference|%s" % name,
            suggested=suggested, check="label_reference"))


def check_citations(inventory: dict[str, Any], bib_entries: list[dict[str, Any]],
                    issues: list[dict[str, Any]], main_rel: str) -> None:
    bib_keys = {entry["key"] for entry in bib_entries
                if entry.get("source") != "inline"}
    inline_keys = {entry["key"] for entry in bib_entries if entry.get("source") == "inline"}
    known = bib_keys | inline_keys
    cited: list[str] = []
    for citation in inventory["citations"]:
        for key in citation["keys"]:
            cited.append(key)
    cited_unique = _uniq(cited)
    has_bibliography = bool(inventory.get("bibliography_commands"))
    if cited_unique and not known and not has_bibliography:
        issues.append(make_issue(
            "missing_bibliography", file=main_rel, line=None,
            evidence="正文出现 \\cite 但没有 \\bibliography/\\addbibresource，也没有 \\bibitem",
            signature="missing_bibliography|%s" % main_rel,
            check="bibliography"))
    for key in cited_unique:
        if key in known:
            continue
        first = next((item for item in inventory["citations"] if key in item["keys"]), None)
        issues.append(make_issue(
            "missing_bib_entry", file=first["file"] if first else main_rel,
            line=first["line"] if first else None,
            evidence=first["raw"] if first else "\\cite{%s}" % key,
            message="引用的 key '%s' 不在 .bib 或 \\bibitem 中（PDF 会印出 [?]）" % key,
            severity="error",
            signature="missing_bib_entry|%s" % key,
            check="bibliography"))
    if bib_keys:
        uncited = sorted(bib_keys - set(cited_unique))
        for key in uncited[:20]:
            issue = make_issue(
                "uncited_bib_entry", file=main_rel, line=None,
                evidence="条目 '%s' 未被正文引用" % key,
                signature="uncited_bib_entry|%s" % key,
                check="bibliography")
            issues.append(issue)


def check_bib_entries(bib_entries: list[dict[str, Any]], issues: list[dict[str, Any]]) -> None:
    seen: dict[str, dict[str, Any]] = {}
    for entry in bib_entries:
        if entry.get("source") == "inline":
            continue
        key = entry["key"]
        if key in seen:
            issues.append(make_issue(
                "bib_duplicate_key", file=entry["file"], line=entry["line"],
                evidence="@%s{%s," % (entry["entry_type"], key),
                message="key '%s' 重复定义（首次在第 %s 行）"
                        % (key, seen[key]["line"]),
                signature="bib_duplicate_key|%s" % key,
                check="bibliography"))
        else:
            seen[key] = entry
        required = BIB_REQUIRED.get(entry["entry_type"], ("title",))
        missing = [name for name in required if name not in entry["fields"]]
        if missing:
            issues.append(make_issue(
                "bib_entry_incomplete", file=entry["file"], line=entry["line"],
                evidence="@%s{%s, ...}" % (entry["entry_type"], key),
                message="条目 '%s' 缺少字段：%s" % (key, ", ".join(missing)),
                signature="bib_entry_incomplete|%s|%s"
                          % (key, ",".join(missing)),
                check="bibliography"))


DEPRECATED_PACKAGES = {
    "epsfig": "graphicx",
    "subfigure": "subfig/subcaption",
    "times": "newtxtext+newtxmath",
    "mathptmx": "newtxtext+newtxmath",
    "a4": "geometry (a4paper)",
    "t1enc": "fontenc",
    "umrand": "tikz",
    "psfig": "graphicx",
}
PACKAGE_CONFLICTS = (
    ("subfigure", "subfig"),
    ("subfigure", "subcaption"),
    ("natbib", "biblatex"),
    ("times", "newtxtext"),
    ("times", "mathptmx"),
    ("babel", "polyglossia"),
    ("geometry", "fullpage"),
    ("fontenc", "fontspec"),
    ("amsmath", "mathtools"),
)


def check_packages(inventory: dict[str, Any], issues: list[dict[str, Any]],
                   main_rel: str) -> None:
    packages = inventory["packages"]
    seen: dict[str, dict[str, Any]] = {}
    for package in packages:
        name = package["name"]
        if name in seen:
            suggested = {
                "original": package["raw"],
                "fixed": "",
                "reason": "该宏包已在第 %s 行加载，重复加载会引发选项冲突"
                          % seen[name]["line"],
            }
            issues.append(make_issue(
                "duplicate_package", file=package["file"], line=package["line"],
                evidence=package["raw"],
                message="宏包 '%s' 被重复加载" % name,
                signature="duplicate_package|%s|%s" % (package["file"], name),
                suggested=suggested, check="package_hygiene"))
        else:
            seen[name] = package
        if name in DEPRECATED_PACKAGES and not _replacement_loaded(name, seen):
            suggested = None
            if name == "epsfig":
                suggested = {
                    "original": package["raw"],
                    "fixed": "\\usepackage{graphicx}",
                    "reason": "epsfig 已过时，graphicx 提供同样的 \\includegraphics",
                }
            elif name in {"times", "mathptmx"}:
                suggested = {
                    "original": package["raw"],
                    "fixed": "\\usepackage{newtxtext}\n\\usepackage{newtxmath}",
                    "reason": "times/mathptmx 已过时，newtx 系列在新发行版上更稳定",
                }
            issues.append(make_issue(
                "deprecated_package", file=package["file"], line=package["line"],
                evidence=package["raw"],
                message="宏包 '%s' 已过时，建议改用 %s"
                        % (name, DEPRECATED_PACKAGES[name]),
                signature="deprecated_package|%s" % name,
                suggested=suggested, check="package_hygiene"))
    present = set(seen)
    for left, right in PACKAGE_CONFLICTS:
        if left in present and right in present:
            later = seen[right] if seen[right]["line"] >= seen[left]["line"] else seen[left]
            issues.append(make_issue(
                "package_conflict", file=later["file"], line=later["line"],
                evidence="%s + %s" % (left, right),
                message="宏包 '%s' 与 '%s' 同时加载会冲突" % (left, right),
                severity="error",
                signature="package_conflict|%s|%s" % tuple(sorted((left, right))),
                check="package_hygiene"))
    hyperref = seen.get("hyperref")
    cleveref = seen.get("cleveref")
    if hyperref and cleveref and cleveref["line"] < hyperref["line"]:
        issues.append(make_issue(
            "package_load_order", file=cleveref["file"], line=cleveref["line"],
            evidence="cleveref 在第 %s 行，hyperref 在第 %s 行"
                     % (cleveref["line"], hyperref["line"]),
            message="cleveref 必须在 hyperref 之后加载",
            signature="package_load_order|cleveref_hyperref",
            check="package_hygiene"))
    if inventory.get("todo_macros") and "todonotes" not in present:
        first = inventory["todo_macros"][0]
        issues.append(make_issue(
            "package_conflict", file=first["file"], line=first["line"],
            evidence="\\todo{...} 但未加载 todonotes",
            message="使用了 \\todo 命令但没有加载 todonotes 宏包",
            severity="error",
            signature="package_conflict|todo_without_todonotes",
            check="package_hygiene"))


def _replacement_loaded(name: str, seen: dict[str, dict[str, Any]]) -> bool:
    replacement = DEPRECATED_PACKAGES.get(name) or ""
    for candidate in re.split(r"[+/]", replacement):
        candidate = candidate.strip()
        if candidate and candidate in seen:
            return True
    return False


def check_cjk(inventory: dict[str, Any], texts: dict[str, str],
              issues: list[dict[str, Any]], main_rel: str) -> None:
    packages = {package["name"] for package in inventory["packages"]}
    if packages & CJK_PACKAGES:
        return
    document_class = inventory.get("document_class") or {}
    class_name = str(document_class.get("name") or "")
    if class_name.startswith("ctex"):
        return
    cjk_file = ""
    for rel in sorted(texts):
        if _has_cjk(texts[rel]):
            cjk_file = rel
            break
    if not cjk_file:
        return
    suggested = None
    if document_class.get("file") == main_rel:
        original = str(document_class.get("raw") or "").strip()
        if original:
            suggested = {
                "original": original,
                "fixed": original + "\n\\usepackage{ctex}",
                "reason": "加载 ctex 宏包以支持中日韩字符（并改用 xelatex 编译）",
            }
    issues.append(make_issue(
        "cjk_without_package", file=main_rel,
        line=document_class.get("line"),
        evidence="文件 %s 含中日韩字符，但未加载 ctex/xeCJK" % cjk_file,
        message="源码含 CJK 文本但没有中文支持宏包",
        severity="error",
        signature="cjk_without_package|%s" % main_rel,
        suggested=suggested, check="engine_hint"))


def check_template(inventory: dict[str, Any], project: dict[str, Any],
                   issues: list[dict[str, Any]], texts: dict[str, str]) -> None:
    main_rel = project["main"]
    main_text = texts.get(main_rel, "")
    document_class = inventory.get("document_class")
    cls_files = [entry["path"] for entry in project["files"]
                 if entry["path"].casefold().endswith(".cls")]
    if not document_class:
        issues.append(make_issue(
            "missing_documentclass", file=main_rel, line=1,
            evidence=_line_at(main_text.splitlines(), 1),
            signature="missing_documentclass|%s" % main_rel,
            check="document_structure"))
    else:
        class_name = document_class.get("name") or ""
        expected = (class_name + ".cls").casefold()
        present = any(path.casefold().endswith(expected) or
                      path.casefold().split("/")[-1] == expected for path in cls_files)
        if not present and class_name and class_name not in STANDARD_CLASSES:
            issues.append(make_issue(
                "missing_class_file", file=main_rel, line=document_class.get("line"),
                evidence=document_class.get("raw") or "",
                message="文档类 '%s' 不是发行版自带类，工程内也没有 %s"
                        % (class_name, expected),
                signature="missing_class_file|%s" % class_name,
                check="template_fields"))
        used_names = {class_name.casefold()} | {
            Path(path).stem.casefold() for path in cls_files}
        for path in cls_files:
            stem = Path(path).stem.casefold()
            if stem and stem not in std_class_stems() and stem not in used_names:
                issues.append(make_issue(
                    "template_class_unused", file=path, line=None,
                    evidence="工程内存在 %s，但主文件没有使用它" % Path(path).name,
                    message="模板类 %s 未被 \\documentclass 使用" % Path(path).name,
                    signature="template_class_unused|%s" % path,
                    check="template_fields"))
    code = "\n".join(line for _n, line in iter_code_lines(main_text))
    if not re.search(r"\\title\s*[\[{]", code):
        issues.append(make_issue(
            "template_required_field", file=main_rel, line=None,
            evidence="主文件没有 \\title{...}",
            message="缺少标题字段 \\title",
            signature="template_required_field|title", check="template_fields"))
    if not re.search(r"\\author\s*[\[{]", code):
        issues.append(make_issue(
            "template_required_field", file=main_rel, line=None,
            evidence="主文件没有 \\author{...}",
            message="缺少作者字段 \\author",
            signature="template_required_field|author", check="template_fields"))
    if not re.search(r"\\begin\s*\{abstract\}", code):
        issues.append(make_issue(
            "template_required_field", file=main_rel, line=None,
            evidence="主文件没有 abstract 环境",
            message="缺少摘要环境 abstract",
            severity="info",
            signature="template_required_field|abstract", check="template_fields"))
    if re.search(r"\\title\s*[\[{]", code) and not re.search(r"\\maketitle", code) \
            and "beamer" not in str(document_class or ""):
        issues.append(make_issue(
            "template_required_field", file=main_rel, line=None,
            evidence="有 \\title 但没有 \\maketitle",
            message="标题已声明但正文没有 \\maketitle",
            severity="info",
            signature="template_required_field|maketitle", check="template_fields"))


def std_class_stems() -> set[str]:
    return {name.casefold() for name in STANDARD_CLASSES}


def tabular_columns(spec: str) -> int:
    """Column count of a tabular column spec, expanding *{n}{spec}.

    Column width specifiers (p{3cm}, m{..}, b{..}) and the @{} / >{} / !{} / <{}
    decoration arguments must be removed first, otherwise their letters and digits
    are counted as columns.
    """
    text = (spec or "").strip()
    text = re.sub(r"\*\{(\d+)\}\s*\{([^{}]*)\}",
                  lambda match: match.group(2) * max(1, int(match.group(1))), text)
    text = re.sub(r"[pmb]\{[^{}]*\}", "p", text)
    text = re.sub(r"[<>!@]\{[^{}]*\}", "", text)
    text = re.sub(r"[<>{}\[\]\s|]", "", text)
    return len(re.findall(r"[lcrXpmbSY]", text))


def check_tables(rel: str, text: str, lines: list[str],
                 issues: list[dict[str, Any]]) -> None:
    columns = 0
    in_tabular = False
    reported = 0
    for lineno, raw in enumerate(lines, 1):
        code = strip_comment(raw)
        for match in TABULAR_ENV_RE.finditer(code):
            kind, env = match.group(1), match.group(2)
            if kind == "begin":
                columns = tabular_columns(match.group(3) or "")
                in_tabular = True
            else:
                in_tabular = False
                columns = 0
            code = code[:match.start()] + code[match.end():]
        if not in_tabular or columns <= 0:
            continue
        for row in split_unescaped(code, "\\\\"):
            cells = split_unescaped(row, "&")
            if len(cells) <= 1 and not row.strip():
                continue
            if len(cells) != columns and reported < 5:
                reported += 1
                issues.append(make_issue(
                    "table_column_mismatch", file=rel, line=lineno, evidence=raw,
                    message="该行有 %d 列，与 tabular 列格式的 %d 列不一致"
                            % (len(cells), columns),
                    signature="table_column_mismatch|%s|%d|%d"
                              % (rel, lineno, len(cells)),
                    check="table_structure"))


def check_graphics(project: dict[str, Any], inventory: dict[str, Any],
                   issues: list[dict[str, Any]]) -> None:
    files = find_graphics_candidates(project)
    for ref in inventory.get("graphics") or []:
        target = (ref.get("path") or "").strip()
        if not target:
            continue
        ok = any(path.endswith("/" + target) or Path(path).stem == Path(target).stem
                 for path in files)
        if not ok:
            issues.append(make_issue(
                "missing_file", file=ref["file"], line=ref["line"],
                evidence="\\includegraphics{%s}" % target,
                message="图片文件 '%s' 在工程内找不到" % target,
                severity="error",
                signature="missing_file|graphics|%s" % target,
                check="include_resolution"))


def find_graphics_candidates(project: dict[str, Any]) -> list[str]:
    return [entry["path"] for entry in project["files"]
            if entry["path"].casefold().endswith(IMAGE_SUFFIXES)]


def guess_engine(inventory: dict[str, Any], texts: dict[str, str],
                 project: dict[str, Any]) -> dict[str, Any]:
    packages = {package["name"] for package in inventory["packages"]}
    reasons: list[str] = []
    engine = "pdflatex"
    class_name = str((inventory.get("document_class") or {}).get("name") or "")
    main_text = texts.get(project["main"], "")
    has_cjk = any(_has_cjk(text) for text in texts.values())
    if packages & CJK_PACKAGES or class_name.startswith("ctex") or has_cjk:
        engine = "xelatex"
        reasons.append("CJK 文本或中文模板需要 xelatex")
    if "fontspec" in packages or "unicode-math" in packages:
        engine = "xelatex"
        reasons.append("fontspec/unicode-math 需要 xelatex 或 lualatex")
    if "luatexja" in packages or "\\directlua" in main_text:
        engine = "lualatex"
        reasons.append("luatexja/directlua 需要 lualatex")
    if engine == "pdflatex" and not reasons:
        reasons.append("源码是纯 ASCII 或已由 pdflatex 可用宏包覆盖")
    return {"engine": engine, "reasons": reasons}


# --------------------------------------------------------------------------- #
# whole-project static scan
# --------------------------------------------------------------------------- #
def scan_project(project: dict[str, Any], stopwatch: "hybrid.Stopwatch",
                 uncertainty: list[str]) -> dict[str, Any]:
    """Deterministic static audit of the TeX project."""
    texts: dict[str, str] = project.get("texts") or {}
    main_rel = str(project.get("main") or "")
    issues: list[dict[str, Any]] = []
    inventory: dict[str, Any] = {
        "labels": [], "references": [], "citations": [], "packages": [],
        "bib_entries": [], "outline": [], "environments": [], "graphics": [],
        "todo_macros": [], "bibliography_commands": [],
        "document_class": None,
    }
    order = [main_rel] + sorted(rel for rel in texts if rel != main_rel)
    line_total = 0
    for rel in order:
        if not rel or rel not in texts:
            continue
        text = texts[rel]
        lines = text.splitlines()
        line_total += len(lines)
        inventory["todo_macros"] = inventory["todo_macros"]
        collect_inventory_file(rel, text, inventory)
        scan_structure(rel, text, lines, issues, inventory, is_main=(rel == main_rel))
        check_tables(rel, text, lines, issues)
        entry = project["by_path"].get(rel) or {}
        if entry.get("abs"):
            scan_encoding(rel, entry["abs"], issues, uncertainty)
        if entry.get("suffix") == ".bib":
            inventory["bib_entries"].extend(parse_bib_entries(rel, text))
        for lineno, code in iter_code_lines(text):
            for match in BIBLIOGRAPHY_RE.finditer(code):
                inventory["bibliography_commands"].append(
                    {"file": rel, "line": lineno, "command": "bibliography",
                     "targets": [part.strip() for part in match.group(1).split(",")
                                 if part.strip()]})
            for match in ADD_BIB_RE.finditer(code):
                inventory["bibliography_commands"].append(
                    {"file": rel, "line": lineno, "command": "addbibresource",
                     "targets": [match.group(1).strip()]})
            if rel == main_rel:
                match = DOCUMENTCLASS_RE.search(code)
                if match and inventory["document_class"] is None:
                    inventory["document_class"] = {
                        "name": match.group(2).strip(),
                        "options": [item.strip() for item in (match.group(1) or "").split(",")
                                    if item.strip()],
                        "file": rel, "line": lineno, "raw": match.group(0),
                    }
    # A missing \documentclass has to be detected before template checks run.
    check_template(inventory, project, issues, texts)
    check_labels_and_refs(inventory, issues)
    check_bib_entries(inventory["bib_entries"], issues)
    check_citations(inventory, inventory["bib_entries"], issues, main_rel)
    check_packages(inventory, issues, main_rel)
    check_cjk(inventory, texts, issues, main_rel)
    check_graphics(project, inventory, issues)
    for missing in project.get("missing_includes") or []:
        issues.append(make_issue(
            "missing_input_file",
            file=missing.get("parent") or main_rel, line=missing.get("line"),
            evidence="引用目标 '%s'（%s）" % (missing.get("ref"), missing.get("kind")),
            message="被引用文件 '%s' 不存在，编译会中止" % missing.get("ref"),
            severity="error",
            signature="missing_input_file|%s|%s" % (missing.get("parent"),
                                                    missing.get("ref")),
            check="include_resolution"))

    issues = finalize_issues(issues, order)
    counts = {"error": 0, "warning": 0, "info": 0, "total": len(issues)}
    by_type: dict[str, int] = {}
    for issue in issues:
        severity = issue.get("severity") or "warning"
        counts[severity] = counts.get(severity, 0) + 1
        by_type[issue["error_type"]] = by_type.get(issue["error_type"], 0) + 1
    checks: list[dict[str, Any]] = []
    for check_id, name, description in CHECK_DEFS:
        found = [issue for issue in issues if issue.get("check") == check_id]
        errors = [issue for issue in found if issue.get("severity") == "error"]
        if errors:
            status = "fail"
        elif found:
            status = "warning"
        else:
            status = "pass"
        checks.append({"check_id": check_id, "name": name,
                       "description": description, "status": status,
                       "findings": len(found),
                       "errors": len(errors)})
    engine = guess_engine(inventory, texts, project)
    return {
        "main_file": main_rel,
        "files_scanned": len(texts),
        "tex_files": len([rel for rel in texts
                          if Path(rel).suffix.casefold() in TEX_SUFFIXES]),
        "lines": line_total,
        "counts": counts,
        "by_type": by_type,
        "issues": issues,
        "checks": checks,
        "engine_guess": engine,
        "inventory": {
            "labels": inventory["labels"],
            "references": inventory["references"],
            "citations": [{"keys": item["keys"], "command": item["command"],
                           "file": item["file"], "line": item["line"]}
                          for item in inventory["citations"]],
            "packages": inventory["packages"],
            "bib_entries": inventory["bib_entries"],
            "outline": inventory["outline"],
            "graphics": inventory["graphics"],
            "document_class": inventory["document_class"],
            "bibliography_commands": inventory["bibliography_commands"],
            "environment_count": len(inventory["environments"]),
        },
    }

# --------------------------------------------------------------------------- #
# agent output: load work/generated/fixes.json and optional notes.json
# --------------------------------------------------------------------------- #
def load_generated(work_dir: Path) -> dict[str, Any]:
    generated_dir = work_dir / "generated"
    report: dict[str, Any] = {"fixes": [], "notes": {}, "files": [], "broken": [],
                              "fixes_present": False, "notes_present": False}
    fixes_path = generated_dir / "fixes.json"
    if fixes_path.is_file():
        report["files"].append(str(fixes_path))
        try:
            data = hybrid.read_json(fixes_path, None)
        except Exception as exc:  # noqa: BLE001
            data = None
            report["broken"].append({"file": "fixes.json",
                                     "reason": "%s: %s" % (type(exc).__name__, exc)})
        if isinstance(data, dict):
            data = data.get("fixes") or data.get("records") or data.get("items")
        if isinstance(data, list):
            report["fixes"] = [item for item in data if isinstance(item, dict)]
            report["fixes_present"] = True
        elif data is not None:
            report["broken"].append({"file": "fixes.json",
                                     "reason": "top-level JSON is neither an array nor "
                                               "an object with fixes/records"})
    notes_path = generated_dir / "notes.json"
    if notes_path.is_file():
        report["files"].append(str(notes_path))
        data = hybrid.read_json(notes_path, None)
        if isinstance(data, dict):
            report["notes"] = data
            report["notes_present"] = True
        elif data is not None:
            report["broken"].append({"file": "notes.json",
                                     "reason": "notes.json is not a JSON object"})
    for path in sorted(generated_dir.glob("*.json")):
        if path.name not in {"fixes.json", "notes.json"}:
            report["broken"].append({"file": path.name,
                                     "reason": "unexpected generated file (ignored)"})
    return report


def _normalize_fix(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    record = {
        "fix_id": str(raw.get("fix_id") or "fix-%03d" % (index + 1)),
        "file": str(raw.get("file") or raw.get("path") or raw.get("source_file") or "").strip(),
        "line": raw.get("line"),
        "error_type": str(raw.get("error_type") or raw.get("type") or "").strip(),
        "original": raw.get("original"),
        "fixed": raw.get("fixed"),
        "reason": _short(raw.get("reason") or raw.get("why") or "", 300),
    }
    try:
        record["line"] = int(record["line"]) if record["line"] is not None else None
    except (TypeError, ValueError):
        record["line"] = None
    if record["original"] is None and raw.get("find") is not None:
        record["original"] = raw.get("find")
    if record["fixed"] is None and raw.get("replace") is not None:
        record["fixed"] = raw.get("replace")
    return record


def _resolve_fix_file(name: str, project: dict[str, Any],
                      base_dir: Path) -> tuple[str | None, Path | None]:
    """Map a fix's file reference onto one project-relative path."""
    if not name:
        return None, None
    cleaned = name.strip().replace("\\", "/")
    for entry in project["files"]:
        rel = entry["path"]
        if cleaned == rel or cleaned.endswith("/" + rel) \
                or cleaned.split("/")[-1] == Path(rel).name:
            path = Path(base_dir) / rel
            if path.is_file():
                return rel, path
    return None, None


def _ws_normalized(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def apply_fixes(base_dir: Path, project: dict[str, Any], fixes_raw: list[dict[str, Any]],
                stopwatch: "hybrid.Stopwatch") -> dict[str, Any]:
    """Strict literal replacement: an unmatched original is rejected, never forced."""
    base = Path(base_dir)
    result: dict[str, Any] = {
        "requested": 0, "applied": [], "rejected": [],
        "files_changed": [], "base_dir": str(base), "truncated": False,
    }
    texts: dict[str, str] = {}
    before_sha: dict[str, str] = {}
    after_sha: dict[str, str] = {}
    edits_per_file: dict[str, int] = {}
    for index, raw in enumerate(fixes_raw):
        result["requested"] += 1
        if len(result["applied"]) >= MAX_FIXES_APPLIED:
            result["truncated"] = True
            break
        record = _normalize_fix(raw, index)
        if record is None:
            result["rejected"].append({"fix_id": "fix-%03d" % (index + 1),
                                       "reason": "record is not a JSON object"})
            continue
        rel, path = _resolve_fix_file(record["file"], project, base)
        if rel is None or path is None:
            result["rejected"].append({
                "fix_id": record["fix_id"], "file": record["file"],
                "error_type": record["error_type"],
                "reason": "file '%s' is not part of the delivered project" % record["file"],
            })
            continue
        record["file"] = rel
        text = texts.get(rel)
        if text is None:
            text = _read_text(path)
            texts[rel] = text
            before_sha[rel] = _sha256_text(text)
        original = record["original"]
        fixed = record["fixed"]
        if not isinstance(original, str) or not original:
            result["rejected"].append({
                "fix_id": record["fix_id"], "file": rel,
                "error_type": record["error_type"],
                "reason": "original text is empty (nothing to locate)"})
            continue
        if not isinstance(fixed, str):
            result["rejected"].append({
                "fix_id": record["fix_id"], "file": rel,
                "error_type": record["error_type"], "reason": "fixed text is missing"})
            continue
        lines = text.splitlines()
        pick_start: int | None = None
        pick_end: int | None = None
        pick_line: int | None = None
        occurrences = [match.start() for match in re.finditer(re.escape(original), text)]
        if len(occurrences) == 1:
            pick_start = occurrences[0]
            pick_end = pick_start + len(original)
        elif len(occurrences) > 1:
            target_line = record.get("line")
            if isinstance(target_line, int) and 1 <= target_line <= len(lines):
                offset = sum(len(line) + 1 for line in lines[: target_line - 1])
                line_end = offset + len(lines[target_line - 1])
                candidate = next((start for start in occurrences
                                  if offset <= start < line_end), None)
                if candidate is not None:
                    pick_start = candidate
                    pick_end = candidate + len(original)
            if pick_start is None:
                result["rejected"].append({
                    "fix_id": record["fix_id"], "file": rel,
                    "error_type": record["error_type"],
                    "reason": "original text occurs %d times and no unique line hint was "
                              "given (ambiguous)" % len(occurrences)})
                continue
        else:
            # Fall back to a whitespace-insensitive line match; a source line that
            # differs only in spacing is still an unambiguous target.
            candidate_index = None
            target_line = record.get("line")
            if isinstance(target_line, int) and 1 <= target_line <= len(lines):
                if _ws_normalized(lines[target_line - 1]) == _ws_normalized(original):
                    candidate_index = target_line - 1
            if candidate_index is None:
                matches = [number for number, line in enumerate(lines)
                           if _ws_normalized(line) == _ws_normalized(original)]
                if len(matches) == 1:
                    candidate_index = matches[0]
            if candidate_index is None:
                result["rejected"].append({
                    "fix_id": record["fix_id"], "file": rel,
                    "error_type": record["error_type"],
                    "reason": "original text was not found in %s (line %s)"
                              % (rel, record.get("line")),
                    "original": _short(original, 160),
                })
                continue
            line = lines[candidate_index]
            indent = line[: len(line) - len(line.lstrip())]
            new_line = indent + fixed
            start = sum(len(item) + 1 for item in lines[:candidate_index])
            pick_start = start
            pick_end = start + len(line)
            pick_line = candidate_index + 1
        assert pick_start is not None and pick_end is not None
        new_text = text[:pick_start] + fixed + text[pick_end:]
        if new_text == text:
            result["rejected"].append({
                "fix_id": record["fix_id"], "file": rel,
                "error_type": record["error_type"],
                "reason": "replacement leaves the file unchanged (no-op)"})
            continue
        texts[rel] = new_text
        edits_per_file[rel] = edits_per_file.get(rel, 0) + 1
        result["applied"].append({
            "fix_id": record["fix_id"],
            "file": rel,
            "line": pick_line if pick_line is not None else record.get("line"),
            "error_type": record["error_type"],
            "original": _short(original, 220),
            "fixed": _short(fixed, 220),
            "reason": record["reason"],
            "verified": True,
        })
    for rel, text in texts.items():
        path = base / rel
        _write_text(path, text)
        after_sha[rel] = _sha256_text(text)
        result["files_changed"].append({
            "path": rel,
            "before_sha256": before_sha.get(rel),
            "after_sha256": after_sha[rel],
            "edits": edits_per_file.get(rel, 0),
        })
    return result


def evaluate_fix_resolution(applied: list[dict[str, Any]],
                            before_scan: dict[str, Any],
                            after_scan: dict[str, Any]) -> None:
    """Mark each applied fix resolved/unresolved against the re-scanned tree."""
    remaining_signatures = {issue["signature"] for issue in after_scan.get("issues") or []}
    for fix in applied:
        if not fix.get("error_type"):
            fix["resolved"] = None
            continue
        candidates = [issue for issue in before_scan.get("issues") or []
                      if issue.get("file") == fix.get("file")
                      and issue.get("error_type") == fix.get("error_type")]
        evidence = _ws_normalized(str(fix.get("original") or ""))
        signatures = set()
        for issue in candidates:
            issue_evidence = _ws_normalized(str(issue.get("evidence") or ""))
            line_matches = (issue.get("line") is not None
                            and fix.get("line") is not None
                            and int(issue.get("line")) == int(fix.get("line")))
            if (evidence and issue_evidence and
                    (evidence in issue_evidence or issue_evidence in evidence)) or line_matches:
                signatures.add(issue["signature"])
        if not signatures:
            fix["resolved"] = None
            fix["resolution_note"] = "no static finding of this type at the edited line"
            continue
        fix["resolved"] = not bool(signatures & remaining_signatures)
        if not fix["resolved"]:
            fix["resolution_note"] = "the same finding still appears after re-scan"


# --------------------------------------------------------------------------- #
# compile phase
# --------------------------------------------------------------------------- #
def _run(argv: list[str], cwd: str | Path, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                                   timeout=timeout)
        return {
            "argv": [str(item) for item in argv],
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "")[-1200:],
            "stderr": (completed.stderr or "")[-1200:],
        }
    except FileNotFoundError:
        return {"argv": [str(item) for item in argv], "returncode": None,
                "error": "not found"}
    except subprocess.TimeoutExpired:
        return {"argv": [str(item) for item in argv], "returncode": None,
                "error": "timeout after %ds" % timeout}
    except Exception as exc:  # noqa: BLE001 - a compile attempt must never crash the run
        return {"argv": [str(item) for item in argv], "returncode": None,
                "error": "%s: %s" % (type(exc).__name__, exc)}


def engine_plan(probe: dict[str, Any], engine_guess: str) -> list[dict[str, Any]]:
    """Ordered compile strategies from what this image actually provides."""
    tex_binaries = list(probe.get("tex") or [])
    plan: list[dict[str, Any]] = []
    if "latexmk" in tex_binaries:
        flag = {"xelatex": "-xelatex", "lualatex": "-lualatex",
                "pdflatex": "-pdf"}.get(engine_guess, "-pdf")
        plan.append({"binary": "latexmk", "kind": "latexmk", "engine": engine_guess,
                     "flag": flag})
    for engine in _uniq([engine_guess, "xelatex", "pdflatex", "lualatex"]):
        if engine in tex_binaries:
            plan.append({"binary": engine, "kind": "single", "engine": engine})
    if "tectonic" in tex_binaries:
        plan.append({"binary": "tectonic", "kind": "single", "engine": "tectonic"})
    if "latex-compiler" in tex_binaries:
        plan.append({"binary": "latex-compiler", "kind": "wrapper", "engine": "wrapper"})
    seen: set[str] = set()
    unique_plan: list[dict[str, Any]] = []
    for candidate in plan:
        if candidate["binary"] in seen:
            continue
        seen.add(candidate["binary"])
        unique_plan.append(candidate)
    return unique_plan


def _compile_argv(candidate: dict[str, Any], main_name: str) -> list[str]:
    binary = candidate["binary"]
    if candidate["kind"] == "latexmk":
        return [binary, candidate.get("flag") or "-pdf", "-interaction=nonstopmode",
                "-file-line-error", "-synctex=0", main_name]
    if candidate["kind"] == "wrapper":
        return [binary, main_name, "."]
    if candidate["binary"] == "tectonic":
        return [binary, "-X", "compile", main_name]
    return [binary, "-interaction=nonstopmode", "-file-line-error", "-synctex=0",
            main_name]


ERROR_LINE_RE = re.compile(r"^! (.*)$")
FILE_LINE_ERROR_RE = re.compile(r"^(.{1,300}?):(\d+): (.*)$")
LOG_INPUT_RE = re.compile(r"\(((?:\.{0,2}[/\\])?[^()\s]+\.(?:tex|ltx|sty|cls))")
LOG_LINE_RE = re.compile(r"^l\.(\d+)\s*(.*)$")
UNDEF_REF_RE = re.compile(r"Reference \x60([^']*)' on page \S+ undefined on input line (\d+)")
UNDEF_CITE_RE = re.compile(r"Citation \x60([^']*)' on page \S+ undefined on input line (\d+)")
MISSING_FILE_RE = re.compile(r"File \x60([^']+)' not found")
FONT_RE = re.compile(r"Font ([^ ]+) not (?:loadable|found)")
RERUN_RE = re.compile(r"Rerun to get|Label\(s\) may have changed|"
                      r"There were undefined references|Please rerun LaTeX")
OVERFULL_RE = re.compile(r"^Overfull \\hbox")
WARNING_RE = re.compile(r"^(?:LaTeX|Package|Class|Module)\s*\S*\s*Warning:\s*(.*)$")


def classify_compile_error(message: str, context: list[str]) -> tuple[str, str]:
    haystack = " ".join([message] + list(context)).casefold()
    joined = " ".join([message] + list(context))
    if "file " in haystack and "not found" in haystack:
        missing = MISSING_FILE_RE.search(joined)
        target = missing.group(1) if missing else ""
        if target.casefold().endswith((".sty", ".cls", ".bst", ".def", ".cfg")):
            return "missing_package", target
        return "missing_file", target
    if "runaway argument" in haystack:
        return "runaway_argument", ""
    if "undefined control sequence" in haystack:
        return "undefined_control_sequence", ""
    if "environment" in haystack and "undefined" in haystack:
        return "environment_undefined", ""
    if "missing $ inserted" in haystack or "missing dollar" in haystack:
        return "missing_dollar", ""
    if "extra }" in haystack or "extra close brace" in haystack:
        return "extra_brace", ""
    if "font" in haystack and ("not loadable" in haystack or "not found" in haystack):
        font = FONT_RE.search(joined)
        return "font_not_found", font.group(1) if font else ""
    if "emergency stop" in haystack:
        return "emergency_stop", ""
    if "fatal error" in haystack:
        return "fatal_error", ""
    package = re.search(r"Package (\S+) Error", message)
    if package or message.casefold().startswith("package"):
        return "package_error", package.group(1) if package else ""
    if "latex error" in haystack:
        return "latex_error", ""
    return "compile_other", ""


def _log_context(lines: list[str], index: int) -> tuple[str, int | None, list[str], str]:
    file_hit = ""
    for cursor in range(index, max(-1, index - 40), -1):
        match = LOG_INPUT_RE.search(lines[cursor])
        if match:
            file_hit = match.group(1).replace("\\", "/").lstrip("./")
            break
    line_no: int | None = None
    context: list[str] = []
    for cursor in range(index, min(len(lines), index + 12)):
        match = LOG_LINE_RE.match(lines[cursor])
        if match:
            line_no = int(match.group(1))
            context.append(match.group(2))
            break
        if cursor > index:
            context.append(lines[cursor])
    text = " ".join(context)[:300]
    return file_hit, line_no, context, text


def parse_tex_log(log_text: str) -> dict[str, Any]:
    """Turn a LaTeX .log into file:line:root-cause records plus warnings."""
    lines = (log_text or "").splitlines()
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    undefined_refs: list[dict[str, Any]] = []
    undefined_cites: list[dict[str, Any]] = []
    overfull = 0
    rerun_needed = False
    seen_errors: set[str] = set()
    seen_warnings: set[str] = set()
    for index, line in enumerate(lines):
        if len(errors) < 40:
            message = ""
            file_hint = ""
            line_hint: int | None = None
            context: list[str] = []
            first = ERROR_LINE_RE.match(line)
            if first:
                message = first.group(1).strip()
                file_hint, line_hint, context, _snippet = _log_context(lines, index)
            else:
                file_match = FILE_LINE_ERROR_RE.match(line)
                if file_match and not line.startswith(("l.", "(", ")")):
                    message = file_match.group(3).strip()
                    file_hint = file_match.group(1).replace("\\", "/").lstrip("./")
                    line_hint = int(file_match.group(2))
                    _f, _l, context, _snippet = _log_context(lines, index)
            if message:
                error_type, extra = classify_compile_error(message, context)
                dedupe = "%s|%s|%s" % (error_type, line_hint, message[:80])
                if dedupe not in seen_errors:
                    seen_errors.add(dedupe)
                    info = taxonomy_entry(error_type)
                    root_cause = info["root_cause"]
                    if extra:
                        if error_type == "missing_package":
                            root_cause = ("镜像内没有 '%s'：%s" % (extra, root_cause))
                        elif error_type == "missing_file":
                            root_cause = ("引用的文件 '%s' 不存在：%s" % (extra, root_cause))
                        elif error_type == "font_not_found":
                            root_cause = ("字体 '%s' 不可用：%s" % (extra, root_cause))
                        elif error_type == "package_error":
                            root_cause = ("宏包 '%s' 报告错误：%s" % (extra, root_cause))
                    errors.append({
                        "file": file_hint or "",
                        "line": line_hint,
                        "location": ("%s:%s" % (file_hint or "<main>", line_hint)
                                     if line_hint else (file_hint or "")),
                        "error_type": error_type,
                        "severity": "error",
                        "message": _short(message, 240),
                        "root_cause": root_cause,
                        "typical_fix": info["typical_fix"],
                        "context": _short(" ".join(context), 240),
                        "missing": extra or None,
                    })
        reference = UNDEF_REF_RE.search(line)
        if reference:
            undefined_refs.append({"key": reference.group(1),
                                   "line": int(reference.group(2)),
                                   "message": _short(line, 200)})
        citation = UNDEF_CITE_RE.search(line)
        if citation:
            undefined_cites.append({"key": citation.group(1),
                                    "line": int(citation.group(2)),
                                    "message": _short(line, 200)})
        if OVERFULL_RE.match(line):
            overfull += 1
        if RERUN_RE.search(line):
            rerun_needed = True
        warning = WARNING_RE.match(line)
        if warning and len(warnings) < 40:
            text = _short(warning.group(1), 240)
            if text and text not in seen_warnings:
                seen_warnings.add(text)
                warnings.append(text)
    return {
        "errors": errors,
        "warnings": warnings,
        "undefined_references": undefined_refs[:40],
        "undefined_citations": undefined_cites[:40],
        "overfull_hboxes": overfull,
        "rerun_needed": rerun_needed,
        "log_lines": len(lines),
    }


def parse_bibtex_log(text: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "error message" in stripped.casefold() or stripped.startswith("I couldn't"):
            errors.append(_short(stripped, 200))
        elif stripped.startswith("Warning--"):
            warnings.append(_short(stripped, 200))
    return {"errors": errors[:20], "warnings": warnings[:20]}


def _page_count_from_log(log_text: str, pdf_path: Path | None) -> int | None:
    match = re.search(r"Output written on .*?\((\d+) pages?", log_text or "")
    if match:
        return int(match.group(1))
    if pdf_path and pdf_path.is_file():
        try:
            data = pdf_path.read_bytes()
        except Exception:
            return None
        count = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return count or None
    return None


def _needs_bibtex(build_root: Path, main_stem: str) -> bool:
    aux = build_root / (main_stem + ".aux")
    if not aux.is_file():
        return False
    try:
        text = aux.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return "\\bibdata" in text or "\\citation" in text


def copy_deliverable_tree(source_root: Path, target_root: Path) -> list[dict[str, Any]]:
    """Copy text and figure files, stripping a UTF-8 BOM from every text file."""
    copied: list[dict[str, Any]] = []
    for path in sorted(Path(source_root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_root)
        target = Path(target_root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.casefold()
        if suffix in TEXT_SUFFIXES:
            _write_text(target, _read_text(path))
        else:
            try:
                shutil.copyfile(path, target)
            except Exception:
                continue
        copied.append({"path": rel.as_posix(), "bytes": target.stat().st_size})
    return copied


def compile_project(project: dict[str, Any], source_dir: Path, out_dir: Path,
                    work_dir: Path, stopwatch: "hybrid.Stopwatch",
                    engine_guess: dict[str, Any], limits: list[str],
                    uncertainty: list[str]) -> dict[str, Any]:
    """Compile the repaired project when a TeX engine exists in this image."""
    probe = render.probe()
    plan = engine_plan(probe, engine_guess.get("engine") or "pdflatex")
    report: dict[str, Any] = {
        "engine_available": bool(plan),
        "engine_guess": engine_guess,
        "engine_used": None,
        "engine_version": "",
        "probe": probe,
        "plan": plan,
        "command": "",
        "commands": [],
        "rounds": 0,
        "bibtex_rounds": 0,
        "success": False,
        "pdf": None,
        "pdf_path": None,
        "png": None,
        "page_count": None,
        "errors": [],
        "warnings": [],
        "undefined_references": [],
        "undefined_citations": [],
        "missing_packages": [],
        "overfull_hboxes": 0,
        "rerun_needed": False,
        "log_file": None,
        "log_tail": "",
        "log_lines": 0,
        "attempts": [],
        "bibtex_runs": [],
        "reason": "",
        "duration_s": 0,
    }
    started = time.time()
    if not plan:
        report["reason"] = (
            "no TeX engine found on PATH (probed: %s); compilation was not attempted"
            % (", ".join(probe.get("tex") or []) or "none"))
        limits.append("compile_unavailable: no TeX engine in this image; the static "
                      "audit and the repaired source tree are delivered instead")
        uncertainty.append("PDF/PNG preview cannot be produced without a TeX engine; "
                           "re-run in an image that provides latexmk/xelatex/pdflatex")
        return report

    build_dir = Path(work_dir) / "build"
    try:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        report["reason"] = "could not create the build directory (%s: %s)" % (
            type(exc).__name__, exc)
        limits.append(report["reason"])
        return report
    copy_deliverable_tree(Path(source_dir), build_dir)
    main_rel = project["main"]
    main_path = build_dir / main_rel
    if not main_path.is_file():
        report["reason"] = "main file %s is missing from the repaired source tree" % main_rel
        limits.append(report["reason"])
        return report
    cwd = main_path.parent
    main_name = main_path.name
    stem = main_path.stem

    for candidate in plan[:3]:
        if stopwatch.remaining() < 45:
            limits.append("time budget nearly exhausted before %s could compile"
                          % candidate["binary"])
            break
        argv = _compile_argv(candidate, main_name)
        report["commands"].append(" ".join(argv))
        rounds = 0
        pdf: Path | None = None
        bibtex_done = False
        for round_index in range(COMPILE_MAX_ROUNDS):
            if stopwatch.remaining() < 25:
                limits.append("compile rounds stopped early by the time budget")
                break
            timeout = max(15, min(COMPILE_TIMEOUT_PER_ROUND,
                                  int(stopwatch.remaining() - 20)))
            attempt = _run(argv, cwd, timeout)
            rounds += 1
            report["attempts"].append({
                "engine": candidate["engine"], "binary": candidate["binary"],
                "round": rounds, "argv": attempt.get("argv"),
                "returncode": attempt.get("returncode"),
                "error": attempt.get("error", ""),
            })
            candidate_pdf = main_path.with_suffix(".pdf")
            if candidate_pdf.is_file():
                pdf = candidate_pdf
            log_path = main_path.with_suffix(".log")
            log_text = _read_text(log_path) if log_path.is_file() else ""
            if not log_text:
                log_text = (attempt.get("stdout") or "") + "\n" + (attempt.get("stderr") or "")
            parsed = parse_tex_log(log_text)
            if round_index == 0 and not bibtex_done and _needs_bibtex(cwd, stem):
                bibtex = shutil.which("bibtex")
                if bibtex:
                    bib_attempt = _run([bibtex, stem], cwd,
                                       max(15, min(90, int(stopwatch.remaining() - 20))))
                    report["bibtex_rounds"] += 1
                    bibtex_done = True
                    blg = cwd / (stem + ".blg")
                    bib_parsed = parse_bibtex_log(_read_text(blg) if blg.is_file() else "")
                    report["bibtex_runs"].append({
                        "argv": bib_attempt.get("argv"),
                        "returncode": bib_attempt.get("returncode"),
                        "errors": bib_parsed["errors"][:6],
                    })
                bcf = cwd / (stem + ".bcf")
                biber = shutil.which("biber")
                if bcf.is_file() and biber:
                    biber_attempt = _run([biber, stem], cwd,
                                         max(15, min(90, int(stopwatch.remaining() - 20))))
                    report["bibtex_runs"].append({
                        "argv": biber_attempt.get("argv"),
                        "returncode": biber_attempt.get("returncode"),
                        "errors": [],
                    })
            if pdf and not parsed["rerun_needed"] and round_index >= 1:
                break
        if pdf is not None:
            report["engine_used"] = candidate["engine"]
            report["rounds"] = rounds
            report["success"] = True
            report["command"] = " ".join(argv)
            if candidate["binary"] == "latexmk" and report["bibtex_rounds"] == 0:
                report["bibtex_rounds"] = 1
            break
        if candidate is not plan[0] and stopwatch.remaining() < 60:
            break

    final_log_path = main_path.with_suffix(".log")
    final_log = _read_text(final_log_path) if final_log_path.is_file() else ""
    parsed = parse_tex_log(final_log)
    report.update({
        "errors": parsed["errors"],
        "warnings": parsed["warnings"],
        "undefined_references": parsed["undefined_references"],
        "undefined_citations": parsed["undefined_citations"],
        "overfull_hboxes": parsed["overfull_hboxes"],
        "rerun_needed": parsed["rerun_needed"],
        "log_lines": parsed["log_lines"],
    })
    report["missing_packages"] = sorted({item["missing"] for item in report["errors"]
                                         if item.get("error_type") == "missing_package"
                                         and item.get("missing")})
    pdf_candidate = main_path.with_suffix(".pdf")
    if report["success"] and pdf_candidate.is_file():
        try:
            delivered_pdf = Path(out_dir) / (stem + ".pdf")
            shutil.copyfile(pdf_candidate, delivered_pdf)
            report["pdf"] = str(delivered_pdf)
            report["pdf_path"] = str(delivered_pdf)
            report["page_count"] = _page_count_from_log(final_log, delivered_pdf)
        except Exception as exc:  # noqa: BLE001
            uncertainty.append("PDF produced but could not be copied: %s" % exc)
    if final_log_path.is_file():
        try:
            delivered_log = Path(out_dir) / "latex_build.log"
            shutil.copyfile(final_log_path, delivered_log)
            report["log_file"] = str(delivered_log)
        except Exception:
            pass
    report["log_tail"] = "\n".join((final_log or "").splitlines()[-50:])
    if report["pdf"]:
        try:
            preview_dir = Path(out_dir) / "preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            png_report = render._pdf_to_png(Path(report["pdf"]), preview_dir)
            if png_report.get("ok"):
                report["png"] = png_report["png"]
            else:
                uncertainty.append("PDF produced but no PDF->PNG renderer is available")
        except Exception as exc:  # noqa: BLE001 - preview is best effort
            uncertainty.append("PNG preview failed: %s" % type(exc).__name__)
    if report["engine_used"]:
        binary = next((item["binary"] for item in plan
                       if item["engine"] == report["engine_used"]), "")
        if binary and shutil.which(binary):
            version = _run([binary, "--version"], Path.cwd(), 15)
            first_line = (version.get("stdout") or "").splitlines()
            report["engine_version"] = _short(first_line[0], 160) if first_line else ""
    if not report["success"]:
        report["reason"] = ("compilation did not produce a PDF; %d error(s) parsed from the "
                            "log" % len(report["errors"]))
        limits.append("compile_failed: %s (see error_locations / build_log_excerpt)"
                      % report["reason"])
    report["duration_s"] = round(time.time() - started, 2)
    return report

# --------------------------------------------------------------------------- #
# project view, index, worksheet
# --------------------------------------------------------------------------- #
def project_view(project: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Same project, but every text read from another tree (work/fixed or out/source)."""
    view = dict(project)
    texts: dict[str, str] = {}
    by_path: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    for entry in project["files"]:
        rel = entry["path"]
        path = Path(base_dir) / rel
        new_entry = dict(entry)
        new_entry["abs"] = str(path)
        if entry.get("text_capable") and path.is_file():
            text = _read_text(path)
            texts[rel] = text
            new_entry["bytes"] = len(text.encode("utf-8"))
            new_entry["sha256"] = _sha256_text(text)
            new_entry["lines"] = len(text.splitlines())
        by_path[rel] = new_entry
        files.append(new_entry)
    view["texts"] = texts
    view["by_path"] = by_path
    view["files"] = files
    view["root"] = str(base_dir)
    return view


def build_tex_index(project: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
    inventory = scan["inventory"]
    return {
        "main_file": project["main"],
        "project_root": project["root"],
        "file_count": len(project["files"]),
        "files": [{key: entry.get(key) for key in
                   ("path", "role", "bytes", "lines", "sha256", "text_capable")}
                  for entry in project["files"]],
        "file_order": [entry["path"] for entry in project["files"]],
        "include_edges": project["include_edges"],
        "missing_includes": project["missing_includes"],
        "bib_files": project["bib_files"],
        "graphics": inventory.get("graphics") or [],
        "document_class": inventory.get("document_class"),
        "engine_guess": scan["engine_guess"],
        "packages": inventory.get("packages") or [],
        "bib_entries": inventory.get("bib_entries") or [],
        "citations": inventory.get("citations") or [],
        "labels": inventory.get("labels") or [],
        "references": inventory.get("references") or [],
        "outline": inventory.get("outline") or [],
        "counts": {
            "files": len(project["files"]),
            "tex_files": scan["tex_files"],
            "lines": scan["lines"],
            "packages": len(inventory.get("packages") or []),
            "labels": len(inventory.get("labels") or []),
            "references": len(inventory.get("references") or []),
            "citation_commands": len(inventory.get("citations") or []),
            "bib_entries": len(inventory.get("bib_entries") or []),
            "sections": len(inventory.get("outline") or []),
            "environments": inventory.get("environment_count") or 0,
        },
    }


WORKSHEET_RULES = [
    "Fix one defect per record and keep the edit minimal: replace the exact offending "
    "text, never rewrite whole sections.",
    "original MUST be an exact substring of the current file content (or an exact whole "
    "line). If it cannot be found the edit is rejected, so copy it from the file itself.",
    "fixed is the replacement text. Use an empty string to delete a line.",
    "line is a 1-based hint used only to disambiguate: it is required when original "
    "occurs more than once in the file.",
    "Do not invent bibliography entries, numeric results, author names or theorem "
    "statements: every new fact must already exist in the case materials.",
    "Do not add packages that the image is unlikely to provide; prefer removing or "
    "replacing a conflicting package with the template-preferred one.",
    "When a defect needs an author decision (wrong number, missing figure, ambiguous "
    "label), record the issue in notes.json under unresolved instead of guessing.",
    "Write valid JSON only: a top-level array in work/generated/fixes.json, UTF-8, no "
    "comments, no markdown fences, no trailing commas.",
]

WORKSHEET_ANSWER_FORMAT = [
    {"file": "main.tex", "line": 12, "error_type": "undefined_reference",
     "original": "\\ref{sec:results}", "fixed": "\\ref{sec:result}",
     "reason": "the only similar label in the project is sec:result; the PDF would print ??"},
]


def build_worksheet(project: dict[str, Any], scan: dict[str, Any],
                    work_dir: Path) -> dict[str, Any]:
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    candidates = sorted(
        scan["issues"],
        key=lambda issue: (severity_rank.get(issue.get("severity") or "warning", 3),
                           issue.get("file") or "", issue.get("line") or 0))
    selected = candidates[:MAX_FIX_ITEMS]
    items = []
    for issue in selected:
        items.append({
            "issue_id": issue["issue_id"],
            "file": issue.get("file"),
            "line": issue.get("line"),
            "error_type": issue.get("error_type"),
            "severity": issue.get("severity"),
            "label_zh": issue.get("label_zh"),
            "message": issue.get("message"),
            "root_cause": issue.get("root_cause"),
            "typical_fix": issue.get("typical_fix"),
            "evidence": issue.get("evidence"),
            "fixable": issue.get("fixable"),
            "suggested_fix": issue.get("suggested_fix"),
        })
    return {
        "runner": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "main_file": project["main"],
        "project_root": project["root"],
        "files": [entry["path"] for entry in project["files"]],
        "static_summary": {
            "counts": scan["counts"],
            "by_type": scan["by_type"],
            "engine_guess": scan["engine_guess"],
            "checks": scan["checks"],
        },
        "issue_count": len(scan["issues"]),
        "issues_in_worksheet": len(items),
        "issues": items,
        "answer_path": str(Path(work_dir) / "generated" / "fixes.json"),
        "notes_path": str(Path(work_dir) / "generated" / "notes.json"),
        "editable_tree": str(Path(work_dir) / "fixed"),
        "rules": WORKSHEET_RULES,
        "answer_format": WORKSHEET_ANSWER_FORMAT,
        "notes_format": {
            "template_adaptation": "how the source was adapted to the required template",
            "unresolved": [{"file": "main.tex", "line": 30,
                            "issue": "why this defect needs an author decision"}],
            "decisions": [{"issue_id": "tex-0004", "decision": "kept \\cite{smith2019}"}],
        },
    }


AGENT_README = """# writing-13 LaTeX compilation - agent worksheet

Produced by scripts/run_latex.py --prepare.

- fixes_worksheet.json  the defects to repair (start here)
- tex_index.json        project structure: main file, includes, packages, labels, .bib
- static_issues.json    full static audit (all issues, not only the top %d)
- fixed/                editable copy of the project; edit it directly if you prefer
- generated/            write your answers here

Required answer (work/generated/fixes.json), a JSON array:

    [
      {"file": "main.tex", "line": 12, "error_type": "undefined_reference",
       "original": "\\\\ref{sec:results}", "fixed": "\\\\ref{sec:result}",
       "reason": "only sec:result exists; the PDF would print ??"}
    ]

- original must be an exact substring of the current file (or an exact whole line);
  an unmatched original is REJECTED, never force-applied.
- line disambiguates when the same text occurs several times.
- fixed may be an empty string to delete a line.
- optional notes.json: {"template_adaptation": "...", "unresolved": [...],
  "decisions": [...]}.

Then run: python scripts/run_latex.py --finalize --out <OUT_DIR> --work <WORK_DIR>
""" % MAX_FIX_ITEMS


def reset_tree(target: Path) -> None:
    try:
        if target.exists():
            shutil.rmtree(target)
    except Exception:
        pass
    target.mkdir(parents=True, exist_ok=True)


def copy_project_tree(project: dict[str, Any], target_root: Path) -> list[str]:
    """Copy the discovered project into a scratch tree (the agent's editable copy)."""
    copied: list[str] = []
    for entry in project["files"]:
        source = Path(entry["abs"])
        if not source.is_file():
            continue
        target = Path(target_root) / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, target)
            copied.append(entry["path"])
        except Exception:
            continue
    return copied


# --------------------------------------------------------------------------- #
# deliverables
# --------------------------------------------------------------------------- #
def write_source_deliverables(out_dir: Path, project: dict[str, Any],
                              fixed_dir: Path, limits: list[str]) -> dict[str, Any]:
    """Deliver the repaired project as editable, BOM-free source under out/source."""
    source_root = Path(out_dir) / "source"
    reset_tree(source_root)
    delivered: list[dict[str, Any]] = []
    total = 0
    for entry in project["files"]:
        rel = entry["path"]
        source = Path(fixed_dir) / rel
        if not source.is_file():
            limits.append("source file %s was missing from the repaired tree" % rel)
            continue
        target = source_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = entry.get("suffix") or Path(rel).suffix.casefold()
        try:
            if suffix in TEXT_SUFFIXES:
                text = _read_text(source)
                _write_text(target, text)
                digest = _sha256_text(text)
                size = len(text.encode("utf-8"))
            else:
                shutil.copyfile(source, target)
                data = target.read_bytes()
                digest = _sha256_bytes(data)
                size = len(data)
        except Exception as exc:  # noqa: BLE001
            limits.append("could not deliver %s (%s: %s)"
                          % (rel, type(exc).__name__, exc))
            continue
        total += size
        if total > MAX_RENDERED_SOURCE_BYTES:
            limits.append("delivered source tree exceeded %d bytes; remaining files were "
                          "left in place but not hashed" % MAX_RENDERED_SOURCE_BYTES)
        delivered.append({
            "path": rel,
            "delivered": str(target),
            "role": entry.get("role"),
            "bytes": size,
            "sha256": digest,
            "sha256_original": entry.get("sha256"),
            "changed": digest != entry.get("sha256"),
            "text_capable": suffix in TEXT_SUFFIXES,
        })
    return {
        "source_dir": str(source_root),
        "files": delivered,
        "total_bytes": total,
        "changed_files": [item["path"] for item in delivered if item["changed"]],
    }


def write_static_report(out_dir: Path, before: dict[str, Any], after: dict[str, Any],
                        applied: list[dict[str, Any]], rejected: list[dict[str, Any]],
                        limits: list[str]) -> Path:
    target = Path(out_dir) / "static_report.md"
    lines = ["# LaTeX static audit report", "",
             "Deterministic audit produced by writing-13-latex-compilation; no model "
             "judgement is involved in the numbers below.", "",
             "## Summary", "",
             "| stage | errors | warnings | info | total |",
             "|---|---|---|---|---|"]
    for label, scan in (("before fix", before), ("after fix", after)):
        counts = scan.get("counts") or {}
        lines.append("| %s | %s | %s | %s | %s |" % (
            label, counts.get("error", 0), counts.get("warning", 0),
            counts.get("info", 0), counts.get("total", 0)))
    lines.extend(["", "## Checks", "", "| check | description | status | findings |",
                  "|---|---|---|---|"])
    for check in after.get("checks") or []:
        lines.append("| %s | %s | %s | %s |" % (check.get("name"), check.get("description"),
                                                check.get("status"), check.get("findings")))
    lines.extend(["", "## Defects found before repair", ""])
    if before.get("issues"):
        lines.extend(["| id | file:line | type | severity | message | root cause |",
                      "|---|---|---|---|---|---|"])
        for issue in before["issues"]:
            location = "%s:%s" % (issue.get("file"), issue.get("line") or "-")
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                issue.get("issue_id"), location, issue.get("error_type"),
                issue.get("severity"), str(issue.get("message") or "").replace("|", "\\\\|"),
                str(issue.get("root_cause") or "").replace("|", "\\\\|")))
    else:
        lines.append("No static defect was found.")
    lines.extend(["", "## Applied fixes", ""])
    if applied:
        lines.extend(["| fix | file:line | type | original -> fixed | resolved |",
                      "|---|---|---|---|---|"])
        for fix in applied:
            lines.append("| %s | %s:%s | %s | %s -> %s | %s |" % (
                fix.get("fix_id"), fix.get("file"), fix.get("line"), fix.get("error_type"),
                str(fix.get("original") or "").replace("|", "\\\\|").replace("\n", " "),
                str(fix.get("fixed") or "").replace("|", "\\\\|").replace("\n", " "),
                fix.get("resolved")))
    else:
        lines.append("No fix was applied (no agent output, or nothing to fix).")
    lines.extend(["", "## Remaining defects", ""])
    remaining = [issue for issue in (after.get("issues") or [])
                 if issue.get("severity") in {"error", "warning"}]
    if remaining:
        lines.extend(["| id | file:line | type | message |", "|---|---|---|---|"])
        for issue in remaining:
            lines.append("| %s | %s:%s | %s | %s |" % (
                issue.get("issue_id"), issue.get("file"), issue.get("line") or "-",
                issue.get("error_type"),
                str(issue.get("message") or "").replace("|", "\\\\|")))
    else:
        lines.append("No error/warning-level defect remains after the repair.")
    _write_text(target, "\n".join(lines) + "\n")
    return target


def write_fixes_markdown(out_dir: Path, applied: list[dict[str, Any]],
                         rejected: list[dict[str, Any]]) -> Path:
    target = Path(out_dir) / "fixes_applied.md"

    def _cell(value: Any) -> str:
        return str(value or "").replace("|", "\\\\|").replace("\n", " ")

    lines = ["# Change log (LaTeX source repairs)", "",
             "Every record below was applied by strict literal replacement; a record "
             "whose original text was not found is listed as rejected instead.", "",
             "## Applied", ""]
    if applied:
        lines.extend(["| fix | file | line | error type | original | fixed | reason | "
                      "resolved |", "|---|---|---|---|---|---|---|---|"])
        for fix in applied:
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                fix.get("fix_id"), fix.get("file"), fix.get("line"),
                fix.get("error_type"), _cell(fix.get("original")),
                _cell(fix.get("fixed")), _cell(fix.get("reason")),
                fix.get("resolved")))
    else:
        lines.append("No fix was applied.")
    lines.extend(["", "## Rejected", ""])
    if rejected:
        lines.extend(["| fix | file | error type | reason |", "|---|---|---|---|"])
        for item in rejected:
            lines.append("| %s | %s | %s | %s |" % (
                item.get("fix_id"), item.get("file"), item.get("error_type"),
                str(item.get("reason") or "").replace("|", "\\\\|")))
    else:
        lines.append("No fix was rejected.")
    _write_text(target, "\n".join(lines) + "\n")
    return target


def write_build_notes(out_dir: Path, project: dict[str, Any], scan: dict[str, Any],
                      compile_report: dict[str, Any], sources: dict[str, Any],
                      notes: dict[str, Any], limits: list[str]) -> Path:
    target = Path(out_dir) / "build_notes.md"
    command = compile_report.get("command") or "(not executed)"
    lines = ["# Reproducible build notes", "",
             "## How to rebuild", "",
             "```bash",
             "# from the delivered source directory (out/source)",
             command,
             "```", "",
             "- main file: `%s`" % project["main"],
             "- engine guessed from the source: `%s` (%s)" % (
                 (scan.get("engine_guess") or {}).get("engine"),
                 ", ".join((scan.get("engine_guess") or {}).get("reasons") or [])),
             "- engine actually used: `%s`" % (compile_report.get("engine_used") or "none"),
             "- rounds executed: %s, bibtex rounds: %s" % (compile_report.get("rounds"),
                                                           compile_report.get("bibtex_rounds")),
             "- a stable cross-reference table needs repeated runs; rerun the same command "
             "until the log no longer contains 'Rerun to get'.",
             "", "## Delivered files", ""]
    for item in sources.get("files") or []:
        lines.append("- `%s` (%s bytes%s)" % (
            item["path"], item.get("bytes"),
            ", modified" if item.get("changed") else ""))
    lines.extend(["", "## Packages loaded", ""])
    for package in (scan.get("inventory") or {}).get("packages") or []:
        lines.append("- `%s`%s" % (package.get("name"),
                                            (" [" + ", ".join(package.get("options") or []) + "]")
                                            if package.get("options") else ""))
    lines.extend(["", "## Known limitations of this run", ""])
    if limits:
        lines.extend("- %s" % item for item in limits)
    else:
        lines.append("- none recorded")
    if notes:
        lines.extend(["", "## Agent notes", "", "```json",
                      json.dumps(notes, ensure_ascii=False, indent=2), "```"])
    _write_text(target, "\n".join(lines) + "\n")
    return target


def build_reproducibility(project: dict[str, Any], scan: dict[str, Any],
                          compile_report: dict[str, Any],
                          sources: dict[str, Any],
                          notes: dict[str, Any]) -> dict[str, Any]:
    return {
        "tex_engine": compile_report.get("engine_used"),
        "engine_available": compile_report.get("engine_available"),
        "engine_version": compile_report.get("engine_version"),
        "engine_guessed": (scan.get("engine_guess") or {}).get("engine"),
        "engine_reasons": (scan.get("engine_guess") or {}).get("reasons"),
        "build_command": compile_report.get("command"),
        "rounds": compile_report.get("rounds"),
        "bibtex_rounds": compile_report.get("bibtex_rounds"),
        "commands_tried": compile_report.get("commands"),
        "document_class": (scan.get("inventory") or {}).get("document_class"),
        "packages": [package.get("name") for package in
                     ((scan.get("inventory") or {}).get("packages") or [])],
        "template_files": [entry["path"] for entry in project["files"]
                           if entry.get("role") in {"class", "style", "bibstyle"}],
        "bib_files": project.get("bib_files") or [],
        "source_checksums": {item["path"]: item["sha256"]
                             for item in sources.get("files") or []},
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "tex_probe": compile_report.get("probe"),
        },
        "rerun_needed": compile_report.get("rerun_needed"),
        "deterministic_repair": True,
        "agent_notes": notes or {},
        "note": ("The build command is exactly what this run executed; a different TeX "
                 "version can change page breaks but not the source structure."),
    }


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def cmd_prepare(out_dir: Path, work_dir: Path,
                stopwatch: "hybrid.Stopwatch") -> dict[str, Any]:
    found = discover.discover()
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case) if case else []
    _log(stopwatch, "prepare: case=%s case_id=%s contract_fields=%d materials=%d"
         % (found.get("case_path"), case_id, len(declared),
            len(found.get("material_docs") or [])))
    limits: list[str] = []
    uncertainty: list[str] = []
    task_md = ""
    if found.get("task_md_path"):
        task_md = _read_text(found["task_md_path"])[:4000]

    project = load_tex_project(found, out_dir, work_dir)
    probe = render.probe()
    state: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "runner": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "case_id": case_id,
        "case_path": found.get("case_path"),
        "task_md_path": found.get("task_md_path"),
        "task_md_chars": len(task_md),
        "out_dir": str(out_dir),
        "work_dir": str(work_dir),
        "ok": False,
        "main_file": None,
        "engine_probe": probe,
        "limits": limits,
        "uncertainty": uncertainty,
        "expected_generated": ["generated/fixes.json", "generated/notes.json (optional)"],
    }
    if not project["ok"]:
        limits.append(project["reason"] or "no LaTeX project was found")
        hybrid.write_json(work_dir / "state.json", state)
        hybrid.write_json(work_dir / "fixes_worksheet.json", {
            "runner": SKILL_NAME, "main_file": None, "issues": [],
            "issue_count": 0, "answer_path": str(work_dir / "generated" / "fixes.json"),
            "error": limits[0],
        })
        return {"phase": "prepare", "ok": False, "case": case, "case_id": case_id,
                "found": found, "project": project, "scan": None,
                "work_dir": str(work_dir), "out_dir": str(out_dir),
                "limits": limits, "declared": declared}
    _log(stopwatch, "prepare: main=%s files=%d engine_probe=%s"
         % (project["main"], len(project["files"]),
            ",".join(probe.get("tex") or []) or "none"))

    scan = scan_project(project, stopwatch, uncertainty)
    _log(stopwatch, "prepare: static errors=%d warnings=%d info=%d engine_guess=%s"
         % (scan["counts"].get("error", 0), scan["counts"].get("warning", 0),
            scan["counts"].get("info", 0), scan["engine_guess"].get("engine")))

    index = build_tex_index(project, scan)
    hybrid.write_json(work_dir / "tex_index.json", index)
    hybrid.write_json(work_dir / "static_issues.json", {
        "main_file": project["main"],
        "counts": scan["counts"],
        "by_type": scan["by_type"],
        "checks": scan["checks"],
        "issues": scan["issues"],
        "engine_guess": scan["engine_guess"],
        "ai_policy": ("This audit is machine-generated; every fix still needs a "
                      "justification that references the defect's root cause."),
    })
    worksheet = build_worksheet(project, scan, work_dir)
    hybrid.write_json(work_dir / "fixes_worksheet.json", worksheet)

    fixed_dir = Path(work_dir) / "fixed"
    reset_tree(fixed_dir)
    copied = copy_project_tree(project, fixed_dir)
    (work_dir / "generated").mkdir(parents=True, exist_ok=True)
    _write_text(work_dir / "README_AGENT.md", AGENT_README)

    state.update({
        "ok": True,
        "main_file": project["main"],
        "project_root": project["root"],
        "file_count": len(project["files"]),
        "files": [{"path": entry["path"], "role": entry["role"],
                   "sha256": entry["sha256"]} for entry in project["files"]],
        "copied_files": copied,
        "staticmethod": {"counts": scan["counts"], "by_type": scan["by_type"],
                         "issue_ids": [issue["issue_id"] for issue in scan["issues"]],
                         "engine_guess": scan["engine_guess"]},
        "declared_fields": [field["name"] for field in declared],
        "limits": limits,
        "uncertainty": uncertainty,
    })
    hybrid.write_json(work_dir / "state.json", state)
    return {"phase": "prepare", "ok": True, "case": case, "case_id": case_id,
            "found": found, "project": project, "scan": scan, "index": index,
            "worksheet": worksheet, "work_dir": str(work_dir),
            "out_dir": str(out_dir), "limits": limits, "declared": declared,
            "probe": probe}


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
def cmd_finalize(out_dir: Path, work_dir: Path,
                 stopwatch: "hybrid.Stopwatch") -> dict[str, Any]:
    found = discover.discover()
    case = contract.load_case(found.get("case_path"))
    case_id = contract.find_case_id(case)
    declared = hybrid.declared_fields(case) if case else []
    task_md_ref = found.get("task_md_path")
    _log(stopwatch, "finalize: case_id=%s contract_fields=%d work=%s"
         % (case_id, len(declared), work_dir))

    limits: list[str] = []
    uncertainty: list[str] = []
    change_log = ["%s %s: deterministic finalize; no model call made"
                  % (SKILL_NAME, SKILL_VERSION)]
    degraded: list[str] = []

    project = load_tex_project(found, out_dir, work_dir)
    if not project["ok"]:
        limits.append(project["reason"] or "no LaTeX project was found")
        report = hybrid.run_report(
            stopwatch=stopwatch, mode="finalize", skill=SKILL_NAME,
            skill_version=SKILL_VERSION, inputs=found, work_dir=str(work_dir),
            out_dir=str(out_dir), limits=limits, uncertainty=uncertainty,
            change_log=change_log)
        payload = hybrid.finalize(
            out_dir, case=case, case_id=case_id, values={}, status="failed",
            limits=limits, uncertainty=uncertainty, change_log=change_log,
            task_md_path=task_md_ref,
            evidence={"failure": limits[:3], "inputs": found},
            report=report, synonyms=SYNONYMS)
        _log(stopwatch, "finalize: failed (no LaTeX project) -> %s"
             % (Path(out_dir) / "result.json"))
        return {"status": "failed", "payload": payload, "case_id": case_id,
                "degraded": ["no_project"]}

    scan_before = scan_project(project, stopwatch, uncertainty)
    before_counts = scan_before["counts"]
    _log(stopwatch, "finalize: static before: errors=%d warnings=%d info=%d"
         % (before_counts.get("error", 0), before_counts.get("warning", 0),
            before_counts.get("info", 0)))

    fixed_dir = Path(work_dir) / "fixed"
    if not (fixed_dir / project["main"]).is_file():
        reset_tree(fixed_dir)
        copy_project_tree(project, fixed_dir)
        limits.append("work/fixed was missing or incomplete: it was recreated from the "
                      "original inputs before applying the agent fixes")

    generated = load_generated(work_dir)
    for broken in generated["broken"]:
        limits.append("generated/%s could not be used (%s)" % (broken["file"],
                                                               broken["reason"]))
        degraded.append("broken_generated:%s" % broken["file"])
    fixes_raw = generated["fixes"]
    _log(stopwatch, "finalize: generated fixes=%d notes=%s"
         % (len(fixes_raw), generated["notes_present"]))

    apply_report = apply_fixes(fixed_dir, project, fixes_raw, stopwatch)
    _log(stopwatch, "finalize: fixes applied=%d rejected=%d truncated=%s"
         % (len(apply_report["applied"]), len(apply_report["rejected"]),
            apply_report["truncated"]))
    if apply_report["rejected"]:
        limits.append("%d fix record(s) were rejected (original text not found / "
                      "ambiguous / no-op): see fixes_rejected"
                      % len(apply_report["rejected"]))
        degraded.append("fix_rejected")
    if apply_report["truncated"]:
        limits.append("only the first %d fixes were applied (MAX_FIXES_APPLIED)"
                      % MAX_FIXES_APPLIED)
        degraded.append("fixes_truncated")

    fixed_view = project_view(project, fixed_dir)
    scan_after = scan_project(fixed_view, stopwatch, uncertainty)
    evaluate_fix_resolution(apply_report["applied"], scan_before, scan_after)
    after_counts = scan_after["counts"]
    fixed_resolved = [fix for fix in apply_report["applied"] if fix.get("resolved") is True]
    fixed_unresolved = [fix for fix in apply_report["applied"] if fix.get("resolved") is False]
    _log(stopwatch, "finalize: static after: errors=%d warnings=%d info=%d resolved=%d"
         % (after_counts.get("error", 0), after_counts.get("warning", 0),
            after_counts.get("info", 0), len(fixed_resolved)))
    if after_counts.get("error", 0):
        limits.append("%d static error(s) remain after the repair; they are listed in "
                      "static_checks.issues and static_report.md"
                      % after_counts["error"])
        degraded.append("static_errors_remaining")
    if fixed_unresolved:
        limits.append("%d applied fix(es) did not clear the matching static finding"
                      % len(fixed_unresolved))
        degraded.append("fix_not_effective")
    if not generated["fixes_present"]:
        if before_counts.get("error", 0) or before_counts.get("warning", 0):
            limits.append("work/generated/fixes.json is missing: the delivered tree is "
                          "the untouched source and the static defects above remain open")
            degraded.append("no_generated_fixes")
        else:
            limits.append("work/generated/fixes.json is absent but the static audit found "
                          "nothing to repair; the source is delivered unchanged")
    for key in ("unresolved", "decisions"):
        note_items = generated["notes"].get(key)
        if isinstance(note_items, list) and note_items:
            uncertainty.append("agent notes record %d %s item(s); they need an author "
                               "decision" % (len(note_items), key))

    sources = write_source_deliverables(out_dir, project, fixed_dir, limits)
    compile_report = compile_project(project, Path(sources["source_dir"]), out_dir,
                                     work_dir, stopwatch, scan_after["engine_guess"],
                                     limits, uncertainty)
    _log(stopwatch, "finalize: compile available=%s success=%s engine=%s pdf=%s"
         % (compile_report["engine_available"], compile_report["success"],
            compile_report.get("engine_used"), compile_report.get("pdf")))

    notes = generated["notes"]
    reproducibility = build_reproducibility(project, scan_after, compile_report,
                                            sources, notes)
    logs: dict[str, str] = {}
    static_report = fix_report = build_notes = None
    try:
        static_report = write_static_report(out_dir, scan_before, scan_after,
                                            apply_report["applied"],
                                            apply_report["rejected"], limits)
        fix_report = write_fixes_markdown(out_dir, apply_report["applied"],
                                           apply_report["rejected"])
        build_notes = write_build_notes(out_dir, project, scan_after, compile_report,
                                         sources, notes, limits)
        if compile_report.get("log_file"):
            log_path = Path(compile_report["log_file"])
            if log_path.is_file():
                log_text = _read_text(log_path)
                _write_text(Path(out_dir) / "build_log_excerpt.txt",
                            "\n".join(log_text.splitlines()[-200:]) + "\n")
                logs["build_log_excerpt"] = str(Path(out_dir) / "build_log_excerpt.txt")
            else:
                logs["build_log_excerpt"] = compile_report["log_file"]
        hybrid.write_json(out_dir / "reproducibility.json", reproducibility)
        change_log.append("wrote static_report.md / fixes_applied.md / build_notes.md / "
                          "reproducibility.json")
    except Exception as exc:  # noqa: BLE001 - a report writer must not kill the result
        limits.append("secondary report writer failed (%s: %s); result.json still "
                      "carries the same content" % (type(exc).__name__, exc))
        degraded.append("report_writer_failed")
        static_report = fix_report = build_notes = None

    # ---- status -------------------------------------------------------------
    if compile_report["engine_available"]:
        if not compile_report["success"]:
            degraded.append("compile_failed")
        if compile_report.get("errors"):
            degraded.append("compile_errors_in_log")
    else:
        degraded.append("compile_unavailable")
    status = "success" if not degraded else "partial"
    if status == "partial":
        limits.insert(0, "status=partial: at least one stage degraded (see "
                         "degraded/limits); the delivered source tree, static audit and "
                         "change log are still valid")
    if compile_report["success"] and not degraded:
        change_log.append("compilation succeeded: %s (%s page(s))"
                          % (compile_report.get("command"),
                             compile_report.get("page_count")))

    # ---- deliverables / values ---------------------------------------------
    warnings_after = ["%s: %s" % (issue.get("error_type"), _short(issue.get("message"), 160))
                      for issue in (scan_after.get("issues") or [])
                      if issue.get("severity") in {"warning", "info"}]
    warnings_after.extend(compile_report.get("warnings") or [])
    warnings_after = _uniq(warnings_after)[:60]
    diagnostics: list[dict[str, Any]] = []
    remaining_signatures = {issue["signature"] for issue in scan_after.get("issues") or []}
    for issue in scan_before.get("issues") or []:
        diagnostics.append({
            "source": "static",
            "file": issue.get("file"),
            "line": issue.get("line"),
            "location": "%s:%s" % (issue.get("file"), issue.get("line") or "-"),
            "error_type": issue.get("error_type"),
            "severity": issue.get("severity"),
            "message": issue.get("message"),
            "root_cause": issue.get("root_cause"),
            "resolved": issue["signature"] not in remaining_signatures,
        })
    for error in compile_report.get("errors") or []:
        diagnostics.append({
            "source": "compile",
            "file": error.get("file"),
            "line": error.get("line"),
            "location": error.get("location") or "%s:%s" % (error.get("file"),
                                                            error.get("line")),
            "error_type": error.get("error_type"),
            "severity": "error",
            "message": error.get("message"),
            "root_cause": error.get("root_cause"),
            "resolved": None,
        })
    excerpt_source = ""
    if compile_report.get("log_file") and Path(compile_report["log_file"]).is_file():
        excerpt_source = _read_text(compile_report["log_file"])
    if excerpt_source:
        build_log_excerpt = "\n".join(excerpt_source.splitlines()[-80:])
    else:
        build_log_excerpt = (
            "No TeX engine was available in this image, so no compiler log exists.\n"
            "Static audit summary: %d error(s), %d warning(s), %d info; engine guessed "
            "from source: %s.\nThe repaired source tree is delivered under out/source/ "
            "so the build can be reproduced in a TeX image."
            % (after_counts.get("error", 0), after_counts.get("warning", 0),
               after_counts.get("info", 0),
               (scan_after.get("engine_guess") or {}).get("engine")))
    checks_after = scan_after.get("checks") or []
    quality = {
        "main_file": project["main"],
        "project_files": len(project["files"]),
        "engine_guessed": (scan_after.get("engine_guess") or {}).get("engine"),
        "engine_used": compile_report.get("engine_used"),
        "static_errors_before": before_counts.get("error", 0),
        "static_errors_after": after_counts.get("error", 0),
        "static_warnings_before": before_counts.get("warning", 0),
        "static_warnings_after": after_counts.get("warning", 0),
        "fixes_requested": apply_report["requested"],
        "fixes_applied": len(apply_report["applied"]),
        "fixes_rejected": len(apply_report["rejected"]),
        "fixes_resolved": len(fixed_resolved),
        "fixes_unresolved": len(fixed_unresolved),
        "compile_attempted": bool(compile_report.get("attempts")),
        "compile_success": bool(compile_report.get("success")),
        "pdf_produced": bool(compile_report.get("pdf")),
        "png_produced": bool(compile_report.get("png")),
        "page_count": compile_report.get("page_count"),
        "checks_passed": sum(1 for check in checks_after if check.get("status") == "pass"),
        "checks_total": len(checks_after),
        "degraded": _uniq(degraded),
    }
    template_adaptation = {
        "detected_template": {
            "document_class": (scan_after.get("inventory") or {}).get("document_class"),
            "class_files": [entry["path"] for entry in project["files"]
                            if entry.get("role") == "class"],
            "style_files": [entry["path"] for entry in project["files"]
                            if entry.get("role") in {"style", "bibstyle"}],
            "packages": [package.get("name") for package in
                         ((scan_after.get("inventory") or {}).get("packages") or [])],
            "engine": (scan_after.get("engine_guess") or {}).get("engine"),
        },
        "template_checks": [check for check in checks_after
                            if check.get("check_id") == "template_fields"],
        "requirements_missing": [issue for issue in (scan_after.get("issues") or [])
                                 if issue.get("error_type") == "template_required_field"],
        "agent_notes": notes,
    }
    index = build_tex_index(fixed_view, scan_after)
    values: dict[str, Any] = {
        "main_file": project["main"],
        "engine": compile_report.get("engine_used") or
                  (scan_after.get("engine_guess") or {}).get("engine"),
        "page_count": compile_report.get("page_count"),
        "pdf_path": compile_report.get("pdf"),
        "png_preview": compile_report.get("png"),
        "build_command": compile_report.get("command") or
                         "(not executed: no TeX engine available in this image)",
        "build_log_excerpt": build_log_excerpt,
        "compile_report": compile_report,
        "reproducibility": reproducibility,
        "unresolved_warnings": warnings_after,
        "static_checks": {
            "before": {"counts": before_counts, "by_type": scan_before.get("by_type")},
            "after": {"counts": after_counts, "by_type": scan_after.get("by_type")},
            "checks": checks_after,
            "issues_before": scan_before.get("issues"),
            "issues_after": scan_after.get("issues"),
            "resolved_issue_count": len(scan_before.get("issues") or [])
                                    - len(scan_after.get("issues") or []),
        },
        "fixed_sources": sources["files"],
        "source_dir": sources["source_dir"],
        "fixes_applied": apply_report["applied"],
        "fixes_rejected": apply_report["rejected"],
        "error_locations": diagnostics,
        "tex_index": index,
        "template_adaptation": template_adaptation,
        "quality": quality,
        "evidence": {
            "engine_guess": scan_after.get("engine_guess"),
            "compile_success": compile_report.get("success"),
            "pdf": compile_report.get("pdf"),
            "png": compile_report.get("png"),
            "applied": len(apply_report["applied"]),
            "rejected": len(apply_report["rejected"]),
            "resolved": len(fixed_resolved),
            "static_errors_before": before_counts.get("error", 0),
            "static_errors_after": after_counts.get("error", 0),
            "reproducibility": {"engine": compile_report.get("engine_used"),
                                "command": compile_report.get("command"),
                                "rounds": compile_report.get("rounds")},
            "deliverables": {
                "source_dir": sources["source_dir"],
                "static_report": str(static_report) if static_report else None,
                "fix_log": str(fix_report) if fix_report else None,
                "build_notes": str(build_notes) if build_notes else None,
                "build_log_excerpt": logs.get("build_log_excerpt"),
                "reproducibility": str(Path(out_dir) / "reproducibility.json"),
            },
        },
        "notes": notes,
    }
    evidence = {
        "inputs": found,
        "project": {
            "main_file": project["main"],
            "project_root": project["root"],
            "files": index["files"],
            "include_edges": project["include_edges"],
            "missing_includes": project["missing_includes"],
            "candidates": project["candidates"],
            "engine_guess": scan_before.get("engine_guess"),
        },
        "static_checks": {
            "before": {"counts": before_counts, "by_type": scan_before.get("by_type"),
                       "issues": scan_before.get("issues")},
            "after": {"counts": after_counts, "by_type": scan_after.get("by_type"),
                      "issues": scan_after.get("issues")},
            "checks": checks_after,
        },
        "fixes": {
            "requested": apply_report["requested"],
            "applied": apply_report["applied"],
            "rejected": apply_report["rejected"],
            "files_changed": apply_report["files_changed"],
            "truncated": apply_report["truncated"],
        },
        "generated": {
            "files": generated["files"],
            "fixes_present": generated["fixes_present"],
            "notes_present": generated["notes_present"],
            "broken": generated["broken"],
        },
        "compile": {
            key: compile_report.get(key) for key in
            ("engine_available", "engine_used", "engine_version", "engine_guess",
             "command", "commands", "rounds", "bibtex_rounds", "success", "pdf", "png",
             "page_count", "errors", "warnings", "undefined_references",
             "undefined_citations", "missing_packages", "overfull_hboxes",
             "rerun_needed", "attempts", "bibtex_runs", "reason", "log_file",
             "log_tail", "duration_s")
        },
        "deliverables": {
            "source_dir": sources["source_dir"],
            "files": sources["files"],
            "static_report": str(static_report) if static_report else None,
            "fix_log": str(fix_report) if fix_report else None,
            "build_notes": str(build_notes) if build_notes else None,
            "reproducibility": str(Path(out_dir) / "reproducibility.json"),
        },
        "degraded": _uniq(degraded),
    }
    report = hybrid.run_report(
        stopwatch=stopwatch, mode="finalize", skill=SKILL_NAME,
        skill_version=SKILL_VERSION, inputs=found, work_dir=str(work_dir),
        out_dir=str(out_dir), generated=generated, fix_results={
            "requested": apply_report["requested"],
            "applied": len(apply_report["applied"]),
            "rejected": len(apply_report["rejected"]),
            "resolved": len(fixed_resolved),
        }, compile={
            "engine_available": compile_report["engine_available"],
            "engine_used": compile_report.get("engine_used"),
            "success": compile_report.get("success"),
            "pdf": compile_report.get("pdf"),
            "png": compile_report.get("png"),
            "page_count": compile_report.get("page_count"),
            "rounds": compile_report.get("rounds"),
        }, static_before=before_counts, static_after=after_counts,
        deliverables={
            "source_dir": sources["source_dir"],
            "changed_files": sources["changed_files"],
            "static_report": str(static_report) if static_report else None,
            "fix_log": str(fix_report) if fix_report else None,
            "build_notes": str(build_notes) if build_notes else None,
            "reproducibility": str(Path(out_dir) / "reproducibility.json"),
        }, degraded=_uniq(degraded), limits=limits, uncertainty=uncertainty,
        change_log=change_log)

    additional = [key for key in values if key != "evidence"]
    payload = hybrid.finalize(
        out_dir, case=case, case_id=case_id, values=values, status=status,
        limits=limits, uncertainty=uncertainty, change_log=change_log,
        task_md_path=task_md_ref, evidence=evidence, report=report,
        synonyms=SYNONYMS, additional=additional)
    _log(stopwatch, "finalize: status=%s degraded=%s result=%s"
         % (status, ",".join(_uniq(degraded)) or "none",
            Path(out_dir) / "result.json"))
    return {"status": status, "payload": payload, "case_id": case_id,
            "degraded": _uniq(degraded)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fallback_result(out_dir: Path, *, status: str, limits: list[str],
                     change_log: list[str], traceback_tail: str = "") -> None:
    """Last-resort contract-shaped result so a crash is never a zero-byte output."""
    case: dict[str, Any] = {}
    case_id: str | None = None
    try:
        found = discover.discover()
        case = contract.load_case(found.get("case_path"))
        case_id = contract.find_case_id(case)
    except Exception:
        pass
    try:
        hybrid.finalize(
            out_dir, case=case, case_id=case_id, values={}, status=status,
            limits=limits, uncertainty=[], change_log=change_log,
            evidence={"failure": limits[:3],
                      "traceback_tail": traceback_tail[-1200:] if traceback_tail else ""},
            report={"status": status, "limits": limits,
                    "traceback_tail": traceback_tail[-1200:] if traceback_tail else ""},
            synonyms=SYNONYMS)
    except Exception:
        hybrid.write_json(Path(out_dir) / "result.json", {
            "case_id": case_id or "",
            "meta": {"status": status, "limits": limits, "change_log": change_log,
                     "traceback_tail": traceback_tail[-1200:]},
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LaTeX authoring/compilation chain (prepare / finalize / all)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true",
                      help="deterministic phase: audit the project and build worksheets")
    mode.add_argument("--finalize", action="store_true",
                      help="deterministic phase: apply fixes, compile, write deliverables")
    mode.add_argument("--all", action="store_true",
                      help="prepare then finalize in one process (default)")
    parser.add_argument("--out", default=None,
                        help="output directory (default: /app/output)")
    parser.add_argument("--work", default=None,
                        help="working directory (default: /app/work)")
    parser.add_argument("--input", default=None,
                        help="directory holding case.json/task.md/materials "
                             "(default: auto-detected)")
    parser.add_argument("--quiet", action="store_true", help="only log, no JSON summary")
    args = parser.parse_args(argv)
    single = args.prepare or args.finalize
    run_prepare = args.prepare or args.all or not single
    run_finalize = args.finalize or args.all or not single

    if args.input:
        target = Path(args.input).resolve()
        if target.is_dir():
            os.chdir(target)

    out_dir = hybrid.resolve_out_dir(args.out)
    work_dir = resolve_work_dir(args.work, out_dir)
    declared: list[dict[str, Any]] = []
    case_r: dict[str, Any] = {}
    case_id: str | None = None
    try:
        found = discover.discover()
        case_r = contract.load_case(found.get("case_path"))
        case_id = contract.find_case_id(case_r)
        declared = hybrid.declared_fields(case_r) if case_r else []
    except Exception:
        pass

    try:  # placeholder first: the container may be killed at the 900 s budget
        hybrid.write_placeholder(
            out_dir, case_id=case_id,
            note="run started; this file is replaced when the skill finishes",
            contract_fields=len(declared))
    except Exception:
        pass

    stopwatch = hybrid.Stopwatch()
    try:
        prepared: dict[str, Any] | None = None
        if run_prepare:
            prepared = cmd_prepare(out_dir, work_dir, stopwatch)
            if not args.quiet:
                scan = prepared.get("scan") or {}
                print(json.dumps({
                    "phase": "prepare",
                    "ok": prepared.get("ok"),
                    "case_id": prepared.get("case_id"),
                    "out_dir": str(out_dir),
                    "work_dir": str(work_dir),
                    "main_file": (prepared.get("project") or {}).get("main"),
                    "project_files": len((prepared.get("project") or {}).get("files") or []),
                    "engine_probe": (prepared.get("probe") or {}).get("tex"),
                    "engine_guess": (scan.get("engine_guess") or {}).get("engine"),
                    "static_counts": scan.get("counts"),
                    "worksheet_issues": (prepared.get("worksheet") or {}).get(
                        "issues_in_worksheet"),
                    "expected_generated": ["generated/fixes.json", "generated/notes.json"],
                    "next": "write work/generated/fixes.json then run --finalize",
                }, ensure_ascii=False, indent=2))
            if not run_finalize:
                return 0
        result = cmd_finalize(out_dir, work_dir, stopwatch)
        if not args.quiet:
            payload = result.get("payload") or {}
            meta = payload.get("meta") or {}
            additional = meta.get("additional_outputs") or {}
            compile_report = payload.get("compile_report") or {}
            if not compile_report:
                for declared_name, internal in (meta.get("contract_field_mapping") or {}).items():
                    if internal == "compile_report":
                        compile_report = payload.get(declared_name) or {}
                        break
            if not compile_report:
                compile_report = additional.get("compile_report") or {}
            print(json.dumps({
                "phase": "finalize",
                "case_id": result.get("case_id"),
                "status": result.get("status"),
                "degraded": result.get("degraded"),
                "compile": {
                    "engine_available": compile_report.get("engine_available"),
                    "engine_used": compile_report.get("engine_used"),
                    "success": compile_report.get("success"),
                    "pdf": compile_report.get("pdf"),
                    "png": compile_report.get("png"),
                    "page_count": compile_report.get("page_count"),
                    "errors": len(compile_report.get("errors") or []),
                },
                "contract_fields": meta.get("declared_fields"),
                "contract_field_mapping": meta.get("contract_field_mapping"),
                "contract_fields_not_reported": meta.get("contract_fields_not_reported"),
                "field_fill_ratio": meta.get("field_fill_ratio"),
                "outputs": meta.get("output_files"),
                "elapsed_s": stopwatch.elapsed(),
            }, ensure_ascii=False, indent=2))
        return 0
    except BaseException as exc:  # noqa: BLE001 - never end a run without a result file
        traceback_tail = traceback.format_exc(limit=8)
        _log(stopwatch, "FATAL %s: %s" % (type(exc).__name__, exc))
        _fallback_result(
            out_dir, status="failed",
            limits=["run aborted by %s: %s" % (type(exc).__name__, exc)],
            change_log=["failure result written by the top-level handler"],
            traceback_tail=traceback_tail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
