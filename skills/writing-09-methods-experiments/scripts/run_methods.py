#!/usr/bin/env python3
"""Methods & Experiments chapter writer: materials in, contract-shaped result.json out.

Two-phase hybrid pipeline (see skills/_core/lib/hybrid.py for the shared helpers):

    prepare   - DETERMINISTIC. Scan every material file, parse config / log / code /
                README facts with file+line provenance, cut the methods draft into
                page/line-anchored segments, bind prose numbers to parameter names,
                detect conflicts and gaps, and write agent worksheets under
                work/batches/ plus work/params_candidates.json.
    (agent)   - judgement. The agent writes work/generated/methods.json and one
                answer file per batch worksheet. No model call happens here.
    finalize  - DETERMINISTIC. Re-derive the facts independently, verify every
                anchor back to the source text, refuse to invent numbers that are
                not in the materials, reflect case.json's output_contract and write
                result.json / evidence.json / run_report.json plus an editable
                docx+md+html deliverable.

The rubric of this topic punishes one failure mode above all others: a fluent
paragraph with a made-up value. So the central rule implemented here is
"trace or flag": every value the chapter states must be locatable in the supplied
materials, otherwise it is reported as untraceable/conflicting and turned into an
author question. Nothing is silently upgraded and nothing is invented.

Standard library only, no network, no model API, deterministic ordering.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# import compatibility: packaged layout (/skills/weekly-skill/lib) vs dev layout
# (skills/<challenge>/scripts + skills/_core/lib). Keep both working.
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
try:  # packaged layout: lib/ sits next to scripts/
    sys.path.insert(0, str(ROOT))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render
except ImportError:  # dev layout: shared kernel lives in skills/_core
    sys.path.insert(0, str(ROOT.parent / "_core"))
    from lib import anchors, contract, discover, docwrite, extract, hybrid, render

TOPIC = "writing-09-methods-experiments"
MAX_MATERIAL_FILES = 80
MAX_FILE_CHARS = 400_000
MAX_QUOTE = 200

# Directories that are never input material. "work" holds our own worksheets and
# generated answers, so scanning it would let the pipeline read its own output back
# as evidence - a self-proving loop.
SKIP_DIRS = {
    "output", "outputs", "result", "results", "work", "batches", "generated",
    "__pycache__", ".git", "node_modules", ".pytest_cache", ".cache",
    "_selftest_tmp", "_selftest_methods", "_selftest_hybrid",
}
SKIP_NAMES = {
    "case.json", "task.md", "instruction.md", "result.json", "evidence.json",
    "run_report.json", "params_candidates.json", "draft_segments.json", "gaps.json",
    "_template_methods.json", "package_manifest.json",
    # our own deliverables: scanning them back as material would be a self-proving loop
    "methods_section.md", "methods_section.html", "methods_section.docx",
    "methods_section.csv", "parameter_table.csv", "author_questions.md",
}

# --------------------------------------------------------------------------- #
# extraction rules. These live in resources/params_rules.json so a reviewer (or a
# reuser on another paper) can extend the vocabulary without touching code; the
# defaults below keep the skill working when the resource file is absent.
# --------------------------------------------------------------------------- #
DEFAULT_RULES: dict[str, Any] = {
    "version": "1.0",
    "config_suffixes": [".yaml", ".yml", ".json", ".ini", ".toml", ".cfg", ".conf", ".env"],
    "log_suffixes": [".log", ".out", ".err", ".stdout", ".stderr"],
    "code_suffixes": [".py", ".sh", ".m", ".r", ".jl", ".c", ".cc", ".cpp", ".h",
                      ".hpp", ".java", ".js", ".ts", ".go"],
    "doc_suffixes": [".md", ".txt", ".tex", ".rst", ".bib", ".csv", ".tsv", ".html",
                     ".pdf", ".docx", ".pptx", ".xml"],
    "readme_names": ["readme.md", "readme.txt", "readme", "requirements.txt",
                     "environment.yml", "environment.yaml", "setup.py", "setup.cfg",
                     "pyproject.toml", "makefile"],
    "draft_name_hints": ["method", "methods", "experiment", "experiments", "draft",
                         "section", "chapter", "manuscript", "paper", "writeup",
                         "write-up", "document", "稿", "正文", "方法", "实验"],
    # canonical parameter name -> known surface forms (lowercase). Used to bind a
    # prose mention ("learning rate is 0.01") to a material key ("learning_rate").
    "name_groups": {
        "learning_rate": ["lr", "learning-rate", "learningrate", "base_lr", "init_lr",
                          "learning rate"],
        "batch_size": ["bs", "batchsize", "batch", "train_batch_size", "batch size"],
        "epochs": ["epoch", "num_epochs", "n_epochs", "max_epochs", "max_epoch",
                   "num epoch", "training epochs"],
        "seed": ["random_seed", "seeds", "manual_seed", "torch_seed", "random seed"],
        "weight_decay": ["wd", "weight-decay", "weight decay", "l2_penalty"],
        "num_layers": ["n_layers", "layers", "depth", "num layer"],
        "hidden_size": ["hidden_dim", "d_model", "dim", "embedding_dim", "hidden size"],
        "dropout": ["dropout_rate", "p_drop", "dropout rate"],
        "warmup_steps": ["warmup", "warmup step"],
        "gradient_clip": ["clip_grad", "grad_clip", "max_grad_norm", "gradient clip"],
        "optimizer": ["optim", "optimiser"],
        "scheduler": ["lr_scheduler", "learning_rate_schedule", "schedule"],
        "momentum": ["beta1", "beta_1"],
        "repeats": ["n_runs", "num_runs", "runs", "repeat", "replicates", "repetitions",
                    "repeated", "times"],
        "num_workers": ["workers", "dataloader_workers"],
        "temperature": ["temp", "tau"],
        "top_k": ["topk", "top-k"],
        "top_p": ["topp", "top-p"],
        "train_ratio": ["train_split", "train_fraction"],
        "val_ratio": ["val_split", "validation_split", "val_fraction"],
        "test_ratio": ["test_split", "test_fraction"],
        "learning_rate_decay": ["lr_decay", "lr decay"],
        "num_gpus": ["gpus", "n_gpu", "num gpu"],
    },
    # generic surface forms that mark "this number is a parameter value" even when
    # the parameter is not present in any material (so the missing source is found).
    "param_name_tokens": ["learning rate", "lr", "batch size", "epochs", "seed",
                          "weight decay", "dropout", "momentum", "temperature",
                          "top-k", "top_k", "hidden size", "layers", "warmup",
                          "gradient clip", "num workers", "repeats", "runs"],
    "metric_names": ["accuracy", "acc", "top-1", "top-5", "f1", "auc", "precision",
                     "recall", "loss", "psnr", "ssim", "bleu", "rouge", "mse", "mae",
                     "rmse", "map", "iou"],
    "hardware_tokens": ["gpu", "cuda", "nvidia", "rtx", "a100", "h100", "v100",
                        "tpu", "cpu", "amd", "mps"],
    "limitation_markers": ["limitation", "limited", "caveat", "only", "cannot",
                           "not support", "fail", "restrict", "局限", "限制",
                           "仅适用", "不适用", "无法"],
    "generic_limitation_phrases": ["存在局限性", "有一定的局限性", "需要进一步研究",
                                   "需要更多研究", "有待进一步", "无法评估",
                                   "in general", "further research", "limited by the data",
                                   "受限于数据", "整体上", "as is well known"],
    "command_prefixes": ["python", "python3", "bash", "sh", "torchrun", "accelerate",
                         "deepspeed", "conda", "pip", "make", "npm", "Rscript", "julia"],
}

ELEMENTS = ("steps", "settings", "parameters", "datasets", "baselines",
            "repro_and_limitations")
ELEMENT_LABELS = {
    "steps": "有序可执行的方法步骤（输入/操作/输出/判断条件）",
    "settings": "实验设置（硬件、软件版本、随机种子、重复次数）",
    "parameters": "带名称/取值/单位/作用对象/来源的参数表",
    "datasets": "数据集（名称、版本、规模、预处理、划分比例与随机方式）",
    "baselines": "对照实验（基线对象与版本、同预算条件、指标与统计方式）",
    "repro_and_limitations": "复现清单（代码版本、环境、命令、数据获取）与限制条件",
}


def load_rules() -> tuple[dict[str, Any], str | None, str | None]:
    """Load resources/params_rules.json, merging over the built-in defaults.

    A missing or malformed resource file must never break the run: the defaults
    cover this topic and the degradation is reported by the caller.
    """
    path = ROOT / "resources" / "params_rules.json"
    rules = json.loads(json.dumps(DEFAULT_RULES))  # deep copy, stdlib only
    if not path.is_file():
        return rules, None, "resources/params_rules.json missing; built-in defaults used"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - never fail on an optional resource
        return rules, str(path), f"resources/params_rules.json unreadable ({type(exc).__name__}); defaults used"
    if isinstance(data, dict):
        for key, value in data.items():
            if key.startswith("_"):
                continue
            rules[key] = value
        return rules, str(path), None
    return rules, str(path), "resources/params_rules.json is not an object; defaults used"


# --------------------------------------------------------------------------- #
# small text helpers (deterministic, locale independent)
# --------------------------------------------------------------------------- #
def norm_name(name: str) -> str:
    text = str(name or "").strip().casefold()
    text = re.sub(r"[\s\-\./]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def canonical_name(name: str, rules: dict[str, Any]) -> str:
    """Map a surface key onto a canonical parameter name.

    "num_epochs" / "EPOCHS" / "epoch" all collapse to "epochs" so a config key and a
    prose phrase can be compared. Ambiguity is resolved by the alias table, never by
    guessing: an unknown key keeps its own normalised name.
    """
    base = norm_name(name)
    groups = rules.get("name_groups") or {}
    reverse: dict[str, str] = {}
    for canonical, aliases in groups.items():
        reverse[norm_name(canonical)] = norm_name(canonical)
        for alias in aliases or []:
            reverse[norm_name(alias)] = norm_name(canonical)
    if base in reverse:
        return reverse[base]
    for prefix in ("num_", "n_", "max_", "min_", "total_"):
        if base.startswith(prefix) and len(base) > len(prefix) + 2:
            stripped = base[len(prefix):]
            if stripped in reverse:
                return reverse[stripped]
            base = stripped
            break
    if base in reverse:
        return reverse[base]
    # constants such as DEFAULT_LR / MAX_BATCH_SIZE: a known alias as suffix wins,
    # longest alias first so "batch_size" beats "batch".
    for alias in sorted(reverse, key=len, reverse=True):
        if len(alias) >= 2 and base.endswith("_" + alias) and len(base) > len(alias) + 2:
            return reverse[alias]
    return base


def clean_number_text(text: str) -> str:
    s = str(text or "").replace(" ", "")
    if "," in s and "." in s:
        return s.replace(",", "")
    if re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+", s):
        return s.replace(",", "")
    return s.replace(",", ".")


def parse_number(raw: str) -> tuple[float | None, str]:
    """Split a numeric token into (value, unit). Non-numeric tokens yield (None, '')."""
    token = str(raw or "").strip()
    match = re.match(r"^([-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?)\s*([A-Za-z%/°]{0,10})$", token)
    if not match:
        return None, ""
    try:
        value = float(clean_number_text(match.group(1)))
    except Exception:
        return None, match.group(2)
    return value, match.group(2)


def number_equal(a: float, b: float, *, tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def unit_norm(unit: str) -> str:
    text = str(unit or "").strip().casefold()
    mapping = {
        "%": "%", "percent": "%", "pct": "%",
        "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
        "min": "min", "mins": "min", "minute": "min", "minutes": "min",
        "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
        "k": "k", "gb": "gb", "mb": "mb", "tb": "tb",
    }
    return mapping.get(text, text)


def quote_around(text: str, needle: str, width: int = MAX_QUOTE) -> str:
    """Quote the source line but centre it on the value so the value is inside.

    The independent check in finalize is "the value token appears in the quote it
    cites"; if a long config line were truncated blindly, that check would fail on
    correct data, so the quote window is built around the value.
    """
    line = (text or "").strip()
    if not line:
        return ""
    if len(line) <= width:
        return line
    hay = line.casefold()
    pos = hay.find(str(needle or "").casefold())
    if pos < 0:
        return line[:width]
    start = max(0, pos - width // 3)
    return line[start:start + width].strip()


NUMBER_RE = re.compile(hybrid.NUM_RE.pattern.replace("(?<![A-Za-z0-9])", "(?<![\\w.])", 1))
SCI_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?[eE][-+]?\d+(?![\w])")
HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+\S")


def find_number_mentions(text: str) -> list[dict[str, Any]]:
    """All numeric mentions in one line, with offsets, value and unit.

    Scientific notation is scanned first and masked before the general number regex
    runs: otherwise "5e-4" is reported as two numbers ("5" and "-4") and a perfectly
    sourced value looks invented.
    """
    out: list[dict[str, Any]] = []
    masked = text
    for match in SCI_RE.finditer(text):
        masked = masked[:match.start()] + " " * (match.end() - match.start()) + masked[match.end():]
        value = parse_number(match.group(0))[0]
        out.append({"raw": match.group(0), "start": match.start(), "end": match.end(),
                    "value": value, "unit": ""})
    for match in NUMBER_RE.finditer(masked):
        raw = match.group(0).strip()
        if not raw:
            continue
        value, unit = parse_number(raw)
        out.append({"raw": raw, "start": match.start(), "end": match.end(),
                    "value": value, "unit": unit})
    out.sort(key=lambda item: item["start"])
    return out


def number_context(line: str, start: int, end: int) -> str:
    """Classify the local context, so "top-1"/"ResNet-18" are not scored as values."""
    before = line[max(0, start - 14):start]
    token = line[start:end]
    if re.search(r"(?:top|k|n|@)\s*[-=]?\s*$", before, re.I) and not re.search(r"\d$", before):
        return "label"
    if re.search(r"[A-Za-z][-–—]\s*$", before) and re.fullmatch(r"\d{1,4}(?:\.\d+)?", token):
        return "model_suffix"
    if re.search(r"(?:fig(?:ure)?|table|tab|eq|equation|section|sec|step|epoch|v|version)\s*\.?\s*[-=]?\s*$",
                 before, re.I):
        return "label"
    if re.fullmatch(r"[-+]?\d{1,2}", token):
        return "small_integer"
    return "value"


def severity_of(mention: dict[str, Any], context: str) -> str:
    """High severity = a real value a reader would try to reproduce.

    Small ints and label numbers are still reported, but as advisory, because a
    chapter legitimately contains "top-1" and "ResNet-18" that no config file lists.
    """
    value = mention.get("value")
    unit = unit_norm(mention.get("unit") or "")
    if context in {"label", "model_suffix"}:
        return "low"
    if unit and unit not in {"", "-"}:
        return "high"
    if value is not None and abs(value) >= 100:
        return "high"
    if "." in str(mention.get("raw") or "").replace(",", ""):
        return "high"
    return "low"


def flatten_json(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                rows.extend(flatten_json(value, path))
            else:
                rows.append((path, "" if value is None else str(value)))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            path = f"{prefix}[{index}]"
            if isinstance(item, (dict, list)):
                rows.extend(flatten_json(item, path))
            else:
                rows.append((path, "" if item is None else str(item)))
    return rows


def value_unit(value: str) -> tuple[str, str]:
    """Attach a unit suffix when the value string carries one (e.g. 16GB, 6.2h)."""
    match = re.match(r"^([-+]?\d+(?:\.\d+)*(?:[eE][-+]?\d+)?)\s*([A-Za-z%/]{1,10})$", str(value or "").strip())
    if match:
        return match.group(1), match.group(2)
    return str(value or "").strip(), ""


def md_heading(line: str) -> tuple[int, str] | None:
    match = HEADING_LINE_RE.match(line)
    if match:
        text = line[match.end():].strip()
        stripped = line.strip()
        level = len(stripped) - len(stripped.lstrip("#"))
        return (min(max(level, 1), 4), text)
    match = NUMBERED_HEADING_RE.match(line)
    if match and len(line.strip()) <= 110 and not line.strip().endswith((".", "。")):
        depth = match.group(1).count(".") + 1
        return (min(depth, 4), line.strip())
    return None


# --------------------------------------------------------------------------- #
# materials: discovery, loading, classification
# --------------------------------------------------------------------------- #
def _is_material(path: Path, rules: dict[str, Any]) -> bool:
    name = path.name.casefold()
    if name in SKIP_NAMES or name.startswith("_template"):
        return False
    if name in {str(n).casefold() for n in (rules.get("readme_names") or [])}:
        return True
    suffix = path.suffix.casefold()
    allowed = set()
    for key in ("config_suffixes", "log_suffixes", "code_suffixes", "doc_suffixes"):
        allowed.update(str(s).casefold() for s in (rules.get(key) or []))
    return suffix in allowed


def _walk_materials(root: Path, rules: dict[str, Any], limit: int = 600) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.casefold() not in SKIP_DIRS)
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if _is_material(candidate, rules):
                out.append(candidate)
            if len(out) >= limit:
                return out
    return out


def _under(path: Path, parents: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    for parent in parents:
        try:
            resolved.relative_to(Path(parent).resolve())
            return True
        except Exception:
            continue
    return False


def scan_materials(roots: Iterable[Path], rules: dict[str, Any],
                   exclude: Iterable[Path] = ()) -> list[Path]:
    """Every material file under the input roots, deterministically ordered.

    discover.discover() deliberately excludes .json (it treats those as case files)
    and knows nothing about .log/.ini/.toml/.py, which are exactly the sources this
    topic must read. So the adapter runs its own bounded scan over the same roots and
    only records paths, never trusting filesystem order.
    """
    seen: set[str] = set()
    files: list[Path] = []
    excluded = [Path(path) for path in exclude]
    for root in roots:
        if not root.is_dir():
            continue
        for path in _walk_materials(root, rules):
            if excluded and _under(path, excluded):
                continue
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files[:MAX_MATERIAL_FILES]


def text_doc(text: str, rel: str) -> dict[str, Any]:
    """Wrap a text file as a verifiable document: one block per non-empty line.

    Page is always 1 and the bbox is a synthetic line box (y = -line), which is what
    lib/extract._extract_plain does for non-PDF input as well. Anchors stay checkable
    (block_id + quote); the honest limitation is recorded in the material metadata
    instead of pretending these are real page coordinates.
    """
    blocks: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()[:4000], start=1):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        blocks.append({
            "block_id": "L" + str(index).zfill(5),
            "bbox": [0.0, float(-index), float(len(stripped)), float(-index + 1)],
            "text": stripped.strip(),
            "line": index,
            "size": 10.0,
        })
    return {
        "engine": "text:" + (Path(rel).suffix.lstrip(".").casefold() or "txt"),
        "anchor_quality": "line",
        "path": rel,
        "pages": [],
        "blocks": {"1": blocks},
        "text_layer": bool(blocks),
        "total_chars": len(text),
        "warnings": ["text material: page is always 1, bbox is a synthetic line box"],
    }


def load_materials(paths: list[Path], roots: list[Path], rules: dict[str, Any],
                   stopwatch: hybrid.Stopwatch) -> list[dict[str, Any]]:
    """Read every material into text plus an anchorable document."""
    import time as _time

    materials: list[dict[str, Any]] = []
    deadline = _time.time() + max(30.0, hybrid.SOFT_DEADLINE_S - stopwatch.elapsed())
    for path in paths:
        if stopwatch.past_soft():
            break
        rel = str(path)
        for root in roots:
            try:
                rel = str(path.relative_to(root))
                break
            except ValueError:
                continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        text = ""
        doc: dict[str, Any] | None = None
        engine = "text"
        warnings: list[str] = []
        truncated = False
        try:
            if suffix in {".pdf", ".docx", ".pptx", ".xml"}:
                extracted = extract.extract_document(path, deadline=deadline)
                engine = str(extracted.get("engine"))
                warnings.extend(extracted.get("warnings") or [])
                doc = extracted
                parts: list[str] = []
                for page_key in sorted(extracted.get("blocks") or {}, key=lambda k: int(k)):
                    for block in extracted["blocks"][page_key]:
                        parts.append(str(block.get("text") or ""))
                text = "\n".join(parts)
                if len(text) > MAX_FILE_CHARS:
                    text = text[:MAX_FILE_CHARS]
                    truncated = True
            else:
                raw = path.read_text(encoding="utf-8-sig", errors="replace")
                if len(raw) > MAX_FILE_CHARS:
                    raw = raw[:MAX_FILE_CHARS]
                    truncated = True
                text = raw
                doc = text_doc(raw, rel)
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not stop the run
            materials.append({
                "path": str(path), "rel": rel, "name": path.name.casefold(),
                "suffix": suffix, "kind": "unreadable", "text": "", "lines": [],
                "doc": text_doc("", rel), "engine": "none", "anchor_quality": "text",
                "bytes": 0, "line_count": 0, "truncated": False,
                "warnings": ["read failed: " + type(exc).__name__ + ": " + str(exc)],
            })
            continue
        lines = text.splitlines()
        material = {
            "path": str(path), "rel": rel, "name": path.name.casefold(),
            "suffix": suffix, "kind": "other", "text": text, "lines": lines,
            "doc": doc or text_doc("", rel), "engine": engine,
            "anchor_quality": (doc or {}).get("anchor_quality") or "line",
            "bytes": path.stat().st_size if path.exists() else 0,
            "line_count": len(lines), "truncated": truncated, "warnings": warnings,
        }
        material["kind"] = classify(material, rules)
        materials.append(material)
    return materials


PROSE_MIN_CHARS = 200
PROSE_ALPHA_RATIO = 0.35


def classify(material: dict[str, Any], rules: dict[str, Any]) -> str:
    """config / log / code / readme / data / draft_candidate / document / other."""
    name = material["name"]
    suffix = material["suffix"]
    if name in {str(n).casefold() for n in (rules.get("readme_names") or [])}:
        return "readme"
    if suffix in {str(s).casefold() for s in (rules.get("config_suffixes") or [])}:
        return "config"
    if suffix in {str(s).casefold() for s in (rules.get("log_suffixes") or [])}:
        return "log"
    if suffix in {str(s).casefold() for s in (rules.get("code_suffixes") or [])}:
        return "code"
    if suffix in {".csv", ".tsv"}:
        return "data"
    if suffix in {".pdf", ".docx", ".pptx", ".xml"}:
        return "document"
    text = material["text"]
    if len(text) >= PROSE_MIN_CHARS:
        alpha = sum(1 for char in text if char.isalpha())
        if alpha / max(1, len(text)) >= PROSE_ALPHA_RATIO:
            return "draft_candidate"
    return "other"


def prose_score(material: dict[str, Any]) -> float:
    text = material["text"]
    return sum(1 for char in text if char.isalpha()) / max(1, len(text))


def pick_draft(materials: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any] | None:
    """Choose the chapter draft deterministically.

    Name hints dominate (a case ships methods_draft.md or 3_methods.tex), then
    text-level signals, then size. The choice is recorded in the run report so a
    reviewer can see which file was treated as the chapter being written.
    """
    hints = [str(h).casefold() for h in (rules.get("draft_name_hints") or [])]
    scored: list[tuple[float, int, str, dict[str, Any]]] = []
    for material in materials:
        if material["kind"] in {"config", "log", "code", "readme", "data", "unreadable"}:
            continue
        if material.get("line_count", 0) < 3 and material["kind"] != "document":
            continue
        if "task" in material["name"] or "instruction" in material["name"]:
            continue
        rel_low = material["rel"].casefold()
        score = 0.0
        for hint in hints:
            if hint in rel_low:
                score += 6.0 if len(hint) >= 4 else 3.0
        if "readme" in material["name"]:
            score -= 10.0
        rel_norm = rel_low.replace("\\", "/")
        if rel_norm.startswith("environment/") or "/environment/" in rel_norm:
            score += 5.0
        if any(part in {"out", "output", "outputs", "result", "results"}
               for part in rel_norm.split("/")[:-1]):
            score -= 25.0
        body_low = material["text"].casefold()
        for marker in ("## method", "# method", "methods and experiment", "## 方法",
                       "方法", "experiment", "实验"):
            if marker in body_low:
                score += 2.0
                break
        score += min(4.0, material.get("line_count", 0) / 40.0)
        score += prose_score(material) * 2.0
        scored.append((round(score, 4), material.get("line_count", 0), material["rel"], material))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return scored[0][3]


# --------------------------------------------------------------------------- #
# fact parsers: config / log / code / README
# --------------------------------------------------------------------------- #
KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_][A-Za-z0-9_\-\. ]*?)\s*[:=]\s*(.*?)\s*$")
LIST_ITEM_RE = re.compile(r"^(\s*)-\s+(.*?)\s*$")
SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
COMMENT_TAIL_RE = re.compile(r"\s+#.*$")
CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=\s*(.+?)\s*$")
ARG_RE = re.compile(r"""add_argument\(\s*["']--([A-Za-z0-9_\-]+)["'][^)]*?default\s*=\s*([^,)]+)""", re.S)
LOWER_CONST_RE = re.compile(
    r"^([a-z_][a-z0-9_]{2,})\s*(?::[^=]+)?=\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*$")
SEED_CALL_RE = re.compile(r"(?:manual_seed|random\.seed|np\.random\.seed|set_seed)\(\s*(\d+)\s*\)")
VERSION_PIN_RE = re.compile(r"([A-Za-z][\w\-\.]{1,40})\s*==\s*([0-9][\w\.\-+]*)")
CLI_RE = re.compile(r"^\s*(?:\$|>|#)?\s*((?:python3|python|bash|sh|torchrun|accelerate|"
                    r"deepspeed|conda|pip|make|npm|Rscript|julia)\b[^\n]{0,180})", re.I)


def _unquote(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _param(name: str, value: str, unit: str, *, origin: str, material: dict[str, Any],
           line: int, quote: str, confidence: float = 0.8,
           rules: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_name = str(name or "").strip()
    return {
        "name": raw_name,
        "canonical": canonical_name(raw_name, rules or DEFAULT_RULES),
        "value": str(value or "").strip(),
        "unit": str(unit or "").strip(),
        "origin": origin,
        "source_path": material["rel"],
        "line": int(line),
        "quote": quote_around(quote or "", value or raw_name),
        "confidence": confidence,
    }


def _find_key_line(lines: list[str], key: str) -> int:
    """Locate the 1-based line where a JSON/YAML key is defined."""
    token = str(key or "").split(".")[-1].split("[")[0].strip()
    if not token:
        return 0
    pattern = re.compile(r"""["']?""" + re.escape(token) + r"""["']?\s*[:=]""")
    for index, line in enumerate(lines, start=1):
        if pattern.search(line):
            return index
    for index, line in enumerate(lines, start=1):
        if token in line:
            return index
    return 0


def _yaml_parent(stack: list[tuple[int, str]], indent: int) -> str:
    for depth, key in reversed(stack):
        if depth < indent:
            return key
    return ""


def _parse_kv_lines(material: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Line-based key/value parser for YAML/INI/TOML/.env without third parties."""
    entries: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    section = ""
    for index, raw in enumerate(material["lines"], start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";", "//")):
            continue
        line = COMMENT_TAIL_RE.sub("", raw.rstrip()) if " #" in raw else raw.rstrip()
        if not line.strip():
            continue
        sect = SECTION_RE.match(line)
        if sect:
            section = sect.group(1).strip()
            stack = []
            continue
        item = LIST_ITEM_RE.match(line)
        if item:
            indent = len(item.group(1))
            value = _unquote(item.group(2).strip())
            parent = _yaml_parent(stack, indent) or (section or "list")
            value, unit = value_unit(value)
            entries.append(_param(parent + "[]", value, unit, origin="config",
                                  material=material, line=index, quote=raw,
                                  confidence=0.6, rules=rules))
            continue
        match = KV_RE.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        key = _unquote(match.group(2).strip())
        value = match.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([key_name for _, key_name in stack] + [key])
        if not value:
            stack.append((indent, key))
            continue
        full = section + "." + path if section else path
        value, unit = value_unit(_unquote(value))
        if value == "":
            continue
        entries.append(_param(full, value, unit, origin="config", material=material,
                              line=index, quote=raw, confidence=0.9, rules=rules))
        stack.append((indent, key))
    return entries


def parse_config(material: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    lines = material["lines"]
    if material["suffix"] == ".json":
        try:
            data = json.loads(material["text"])
        except Exception:
            data = None
        if isinstance(data, (dict, list)):
            entries: list[dict[str, Any]] = []
            for path, value in flatten_json(data):
                line = _find_key_line(lines, path) or 1
                source_line = lines[line - 1] if line - 1 < len(lines) else material["text"][:MAX_QUOTE]
                value_txt, unit = value_unit(value)
                entries.append(_param(path, value_txt, unit, origin="config",
                                      material=material, line=line, quote=source_line,
                                      confidence=0.9, rules=rules))
            if entries:
                return entries
    return _parse_kv_lines(material, rules)


LOG_KV_RE = re.compile(
    r"(?<![\w.])([A-Za-z_][A-Za-z0-9_\-\.]{1,40})\s*[=:]\s*"
    r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?[A-Za-z%/]{0,6})")
# Log keys that look like parameters but describe the device/process, not the setup.
LOG_PARAM_BLOCKLIST = {"cuda", "device", "gpu", "gpus", "id", "pid", "rank", "port",
                       "time", "timestamp", "date", "step", "iteration", "iter"}
LOG_ANOMALY_RE = re.compile(
    r"\b(warn|warning|error|nan|inf|oom|out of memory|skipped|failed|diverg|"
    r"overflow|underflow)\b", re.I)


def parse_log(material: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    params: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    hardware: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    metric_names = [str(m).casefold() for m in (rules.get("metric_names") or [])]
    hardware_tokens = [str(h).casefold() for h in (rules.get("hardware_tokens") or [])]
    for index, raw in enumerate(material["lines"], start=1):
        line = raw.rstrip()
        low = line.casefold()
        for match in LOG_KV_RE.finditer(line):
            name = match.group(1)
            value_txt = match.group(2).strip()
            value, unit = value_unit(value_txt)
            low_name = name.casefold()
            if low_name in LOG_PARAM_BLOCKLIST:
                continue
            if any(token in low_name for token in metric_names):
                metrics.append({"name": name, "value": value, "unit": unit,
                                "source_path": material["rel"], "line": index,
                                "quote": quote_around(line, value_txt)})
            else:
                params.append(_param(name, value, unit, origin="log", material=material,
                                     line=index, quote=line, confidence=0.75, rules=rules))
        for match in VERSION_PIN_RE.finditer(line):
            versions.append({"name": match.group(1), "version": match.group(2),
                             "source_path": material["rel"], "line": index,
                             "quote": quote_around(line, match.group(0))})
        if any(token in low for token in hardware_tokens):
            hardware.append({"text": line.strip(), "source_path": material["rel"],
                             "line": index, "quote": line.strip()[:MAX_QUOTE]})
        cli = CLI_RE.match(line)
        if cli:
            commands.append({"command": cli.group(1).strip(), "source_path": material["rel"],
                             "line": index, "quote": quote_around(line, cli.group(1))})
        if LOG_ANOMALY_RE.search(line):
            anomalies.append({"text": line.strip()[:300], "source_path": material["rel"],
                              "line": index, "quote": line.strip()[:MAX_QUOTE]})
    return {"params": params, "metrics": metrics, "versions": versions,
            "hardware": hardware[:12], "commands": commands[:12], "anomalies": anomalies[:20]}


def parse_code(material: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    params: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    for index, raw in enumerate(material["lines"], start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for match in ARG_RE.finditer(line):
            value, unit = value_unit(_unquote(match.group(2).strip()))
            params.append(_param(match.group(1), value, unit, origin="code",
                                 material=material, line=index, quote=line,
                                 confidence=0.85, rules=rules))
        const = CONST_RE.match(line)
        if const:
            raw_value = _unquote(const.group(2).strip().rstrip(","))
            value, unit = value_unit(raw_value)
            params.append(_param(const.group(1), value, unit, origin="code",
                                 material=material, line=index, quote=line,
                                 confidence=0.7, rules=rules))
        else:
            lower = LOWER_CONST_RE.match(line)
            if lower and " " not in lower.group(1):
                params.append(_param(lower.group(1), lower.group(2), "", origin="code",
                                     material=material, line=index, quote=line,
                                     confidence=0.55, rules=rules))
        for match in SEED_CALL_RE.finditer(line):
            params.append(_param("seed", match.group(1), "", origin="code",
                                 material=material, line=index, quote=line,
                                 confidence=0.9, rules=rules))
        for match in VERSION_PIN_RE.finditer(line):
            versions.append({"name": match.group(1), "version": match.group(2),
                             "source_path": material["rel"], "line": index,
                             "quote": quote_around(line, match.group(0))})
        cli = CLI_RE.match(line)
        if cli:
            commands.append({"command": cli.group(1).strip(), "source_path": material["rel"],
                             "line": index, "quote": quote_around(line, cli.group(1))})
    return {"params": params, "versions": versions, "commands": commands}


def parse_readme(material: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    hardware: list[dict[str, Any]] = []
    hardware_tokens = [str(h).casefold() for h in (rules.get("hardware_tokens") or [])]
    for index, raw in enumerate(material["lines"], start=1):
        line = raw.rstrip()
        cli = CLI_RE.match(line)
        if cli:
            commands.append({"command": cli.group(1).strip(), "source_path": material["rel"],
                             "line": index, "quote": quote_around(line, cli.group(1))})
        for match in VERSION_PIN_RE.finditer(line):
            versions.append({"name": match.group(1), "version": match.group(2),
                             "source_path": material["rel"], "line": index,
                             "quote": quote_around(line, match.group(0))})
        if any(token in line.casefold() for token in hardware_tokens) and ":" in line:
            hardware.append({"text": line.strip(), "source_path": material["rel"],
                             "line": index, "quote": line.strip()[:MAX_QUOTE]})
    return {"commands": commands[:20], "versions": versions[:20], "hardware": hardware[:8]}


def collect_facts(materials: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    """Run every parser over every material and de-duplicate the facts."""
    facts: dict[str, Any] = {
        "params": [], "metrics": [], "versions": [], "hardware": [], "commands": [],
        "anomalies": [], "by_kind": {},
    }
    for material in materials:
        kind = material["kind"]
        facts["by_kind"].setdefault(kind, []).append(material["rel"])
        if kind == "config":
            facts["params"].extend(parse_config(material, rules))
        elif kind == "log":
            parsed = parse_log(material, rules)
            facts["params"].extend(parsed["params"])
            facts["metrics"].extend(parsed["metrics"])
            facts["versions"].extend(parsed["versions"])
            facts["hardware"].extend(parsed["hardware"])
            facts["commands"].extend(parsed["commands"])
            facts["anomalies"].extend(parsed["anomalies"])
        elif kind == "code":
            parsed = parse_code(material, rules)
            facts["params"].extend(parsed["params"])
            facts["versions"].extend(parsed["versions"])
            facts["commands"].extend(parsed["commands"])
        elif kind == "readme":
            parsed = parse_readme(material, rules)
            facts["commands"].extend(parsed["commands"])
            facts["versions"].extend(parsed["versions"])
            facts["hardware"].extend(parsed["hardware"])
    seen: set[tuple] = set()
    deduped: list[dict[str, Any]] = []
    for entry in facts["params"]:
        key = (entry["canonical"], entry["value"], entry["source_path"], entry["line"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    deduped.sort(key=lambda e: (e["canonical"], e["source_path"], e["line"], e["value"]))
    facts["params"] = deduped
    return facts


# --------------------------------------------------------------------------- #
# parameter index, conflicts, and the material number pool
# --------------------------------------------------------------------------- #
def build_param_index(params: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]],
                                                             list[dict[str, Any]]]:
    """Group facts by canonical name and report name-level conflicts."""
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in params:
        index.setdefault(entry["canonical"], []).append(entry)
    origin_rank = {"config": 0, "code": 1, "log": 2}
    for name in index:
        index[name].sort(key=lambda e: (origin_rank.get(str(e.get("origin")), 3),
                                        e["source_path"], e["line"], e["value"]))
    conflicts: list[dict[str, Any]] = []
    for name in sorted(index):
        distinct: list[tuple[float, str]] = []
        for entry in index[name]:
            value, unit = parse_number(entry["value"])
            if value is None:
                continue
            unit_key = unit_norm(unit or entry["unit"])
            if any(number_equal(value, seen[0]) and unit_key == seen[1] for seen in distinct):
                continue
            distinct.append((value, unit_key))
        if len(distinct) > 1:
            conflicts.append({
                "name": name,
                "values": [entry["value"] for entry in index[name]],
                "sources": [{"path": entry["source_path"], "line": entry["line"],
                             "value": entry["value"], "origin": entry["origin"]}
                            for entry in index[name]],
                "severity": "high",
                "note": "the same parameter name carries different values across materials",
            })
    return index, conflicts


def build_variants(index: dict[str, list[dict[str, Any]]], rules: dict[str, Any]) -> dict[str, list[str]]:
    """canonical name -> surface forms to look for in prose."""
    groups = rules.get("name_groups") or {}
    variants: dict[str, list[str]] = {}
    for canonical in index:
        forms = {canonical, canonical.replace("_", " ")}
        for group, aliases in groups.items():
            if norm_name(group) == canonical:
                forms.add(str(group))
                forms.add(str(group).replace("_", " "))
                for alias in aliases or []:
                    forms.add(str(alias))
        variants[canonical] = sorted(form for form in forms if len(form) >= 2)
    for token in rules.get("param_name_tokens") or []:
        canonical = canonical_name(str(token), rules)
        forms = {str(token), str(token).replace("_", " ")}
        for group, aliases in groups.items():
            if norm_name(group) == canonical:
                forms.add(str(group))
                for alias in aliases or []:
                    forms.add(str(alias))
        if canonical not in variants:
            variants[canonical] = sorted(form for form in forms if len(form) >= 2)
    return variants


def variant_pattern(variant: str) -> re.Pattern:
    escaped = re.escape(variant)
    escaped = escaped.replace(r"\ ", r"[\s_\-]+")
    escaped = escaped.replace(r"\_", r"[\s_\-]+")
    escaped = escaped.replace(r"\-", r"[\s_\-]+")
    return re.compile(r"(?<![\w])" + escaped + r"(?![\w])", re.I)


def bind_param(line: str, start: int, end: int,
               variants: dict[str, list[str]]) -> tuple[str, str, int] | None:
    """Bind a numeric mention to the nearest parameter name in the same line.

    The window is 180 characters: a sentence such as "the learning rate is 0.01 and
    the batch size is 96" has to bind each value to its own name, so the closest
    surface form wins (ties are broken by canonical name, for reproducibility).
    """
    def gap(name_start: int, name_end: int) -> int:
        # distance between two spans; 0 when they touch. Using the span gap instead
        # of the name's start point is what keeps "learning rate is 0.01 and the
        # batch size is 96" from binding 0.01 to batch_size.
        if name_end <= start:
            return start - name_end
        if name_start >= end:
            return name_start - end
        return 0

    best: tuple[int, str, str] | None = None
    for canonical in sorted(variants):
        for variant in variants[canonical]:
            if len(variant) < 2:
                continue
            for match in variant_pattern(variant).finditer(line):
                distance = gap(match.start(), match.end())
                if distance > 180:
                    continue
                if best is None or distance < best[0] or (
                        distance == best[0] and canonical < best[1]):
                    best = (distance, canonical, match.group(0))
    if best is None:
        return None
    return best[1], best[2], best[0]


def build_number_pool(materials: list[dict[str, Any]], skip_rels: set[str]) -> dict[str, Any]:
    """Every numeric value in the materials, keyed for O(1) trace checks.

    Two pools are derived from this: source_numbers (materials minus the draft, used
    to judge what the draft claims) and all_numbers (materials including the draft,
    used to judge what the agent wrote). A number the agent copied from the draft is
    legitimate; a number the draft itself invented is not.
    """
    values: dict[tuple, dict[str, Any]] = {}
    raws: dict[str, dict[str, Any]] = {}
    source_values: dict[tuple, dict[str, Any]] = {}
    source_raws: dict[str, dict[str, Any]] = {}
    for material in sorted(materials, key=lambda m: m["rel"]):
        rel = material["rel"]
        doc = material["doc"] or {}
        page_blocks = doc.get("blocks") or {}
        for page_key in sorted(page_blocks, key=lambda k: int(k)):
            page_blocks_list = page_blocks[page_key]
            for block in page_blocks_list:
                text = str(block.get("text") or "")
                if not text:
                    continue
                for mention in find_number_mentions(text):
                    entry = {
                        "source": rel,
                        "page": int(page_key),
                        "block_id": block.get("block_id"),
                        "bbox": block.get("bbox"),
                        "line": block.get("line"),
                        "quote": quote_around(text, mention["raw"]),
                        "raw": mention["raw"],
                    }
                    raw_key = str(mention["raw"]).replace(" ", "").casefold()
                    raws.setdefault(raw_key, entry)
                    if mention["value"] is not None:
                        key = (round(mention["value"], 9), unit_norm(mention["unit"]))
                        values.setdefault(key, entry)
                    # The source pool must be built by *skipping the draft while
                    # inserting*: a setdefault-then-filter pass would lose a value
                    # that the draft happens to mention before its real source does
                    # (the draft is read before the log in path order).
                    if rel not in skip_rels:
                        source_raws.setdefault(raw_key, entry)
                        if mention["value"] is not None:
                            source_values.setdefault(
                                (round(mention["value"], 9), unit_norm(mention["unit"])), entry)
    return {
        "values": values,
        "raws": raws,
        "source_values": source_values,
        "source_raws": source_raws,
    }


def lookup_number(pool: dict[str, Any], mention: dict[str, Any], *, source_only: bool) -> dict[str, Any] | None:
    raw_key = str(mention["raw"]).replace(" ", "").casefold()
    raws = pool["source_raws"] if source_only else pool["raws"]
    values = pool["source_values"] if source_only else pool["values"]
    if mention.get("value") is None:
        return raws.get(raw_key)
    value, unit = mention["value"], unit_norm(mention.get("unit") or "")
    exact = values.get((round(value, 9), unit))
    if exact is not None:
        return exact
    for (seen_value, _seen_unit), entry in sorted(values.items(), key=lambda kv: kv[1]["source"]):
        if number_equal(value, seen_value):
            return entry
    return raws.get(raw_key)


# --------------------------------------------------------------------------- #
# draft analysis: sections, segments, prose numbers, dataset/baseline candidates
# --------------------------------------------------------------------------- #
METHOD_MARKERS = (
    "we use", "we train", "we split", "we evaluate", "we apply", "we implement",
    "we report", "we compare", "we preprocess", "we sample", "we measure",
    "is trained", "was trained", "are trained", "were trained", "is evaluated",
    "was evaluated", "we fine-tune", "we optimize", "we optimise", "we run",
    "采用", "使用", "训练", "划分", "评估", "对比", "测试", "预处理", "采样", "实现",
    "优化", "微调", "统计", "重复",
)


def detect_draft_sections(material: dict[str, Any]) -> list[dict[str, Any]]:
    """Markdown / numbered headings become sections so segments carry a chapter path."""
    doc = material["doc"] or {}
    sections: list[dict[str, Any]] = []
    counter = 0
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        for block in doc["blocks"][page_key]:
            heading = md_heading(str(block.get("text") or ""))
            if not heading:
                continue
            counter += 1
            sections.append({"id": "sec-" + str(counter), "title": heading[1],
                             "level": heading[0], "page": int(page_key),
                             "block_id": block.get("block_id")})
    return sections


def is_method_sentence(text: str) -> bool:
    low = str(text or "").casefold()
    return any(marker in low for marker in METHOD_MARKERS)


SENTENCE_BOUNDARY = re.compile(r"(?<=[\.\!\?。！？;；])\s+")


def sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Character span of the sentence containing a mention.

    Binding must stay inside one sentence: "The learning rate is 0.01 and the batch
    size is 96" is two bindings, and a line-level window would attach 0.01 to
    whichever name happened to be closest anywhere on the line.
    """
    spans: list[tuple[int, int]] = []
    last = 0
    for match in SENTENCE_BOUNDARY.finditer(text):
        spans.append((last, match.start()))
        last = match.end()
    spans.append((last, len(text)))
    for left, right in spans:
        if left <= start < right:
            return left, right
    return 0, len(text)


def analyse_mentions(material: dict[str, Any], pool: dict[str, Any],
                     variants: dict[str, list[str]], param_index: dict[str, list[dict[str, Any]]],
                     *, source_only: bool, cross_check: bool = True) -> list[dict[str, Any]]:
    """Every number the draft states, its bound parameter (if any) and its verdict.

    Verdicts:
      match            - bound to a parameter that the materials define with that value
      mismatch         - bound to a parameter but the materials define another value
      unit_mismatch    - same value, different unit
      traced_number    - the value occurs in the materials but no parameter binding
      untraceable      - the value does not occur in the materials at all
    """
    mentions: list[dict[str, Any]] = []
    doc = material["doc"] or {}
    for page_key in sorted((doc.get("blocks") or {}), key=lambda k: int(k)):
        for block in doc["blocks"][page_key]:
            text = str(block.get("text") or "")
            if not text or md_heading(text):
                continue
            for mention in find_number_mentions(text):
                context = number_context(text, mention["start"], mention["end"])
                left, right = sentence_span(text, mention["start"], mention["end"])
                sentence = text[left:right].strip()
                bound = None
                if context not in {"label", "model_suffix"}:
                    bound = bind_param(sentence, mention["start"] - left,
                                       mention["end"] - left, variants)
                record = {
                    "value_raw": mention["raw"],
                    "value": mention["value"],
                    "unit": mention["unit"],
                    "context": context,
                    "severity": severity_of(mention, context),
                    "source": material["rel"],
                    "page": int(page_key),
                    "block_id": block.get("block_id"),
                    "bbox": block.get("bbox"),
                    "line": block.get("line"),
                    "quote": quote_around(text, mention["raw"]),
                    "sentence": sentence[:280],
                    "bound_param": bound[0] if bound else None,
                    "bound_surface": bound[1] if bound else None,
                    "verdict": "untraceable",
                    "material_match": None,
                    "declared_values": [],
                }
                if bound:
                    entries = param_index.get(bound[0]) or []
                    numeric = []
                    for entry in entries:
                        value, unit = parse_number(entry["value"])
                        if value is not None:
                            numeric.append((value, unit or entry["unit"], entry))
                    if numeric:
                        equal = [item for item in numeric if number_equal(mention["value"], item[0])]
                        if equal:
                            same_unit = [item for item in equal
                                         if unit_norm(item[1]) == unit_norm(mention["unit"])
                                         or not unit_norm(item[1]) or not unit_norm(mention["unit"])]
                            if same_unit:
                                record["verdict"] = "match"
                            else:
                                record["verdict"] = "unit_mismatch"
                            record["material_match"] = equal[0][2]
                        else:
                            record["verdict"] = "mismatch"
                            record["material_match"] = numeric[0][2]
                        record["declared_values"] = [
                            {"value": item[2]["value"], "unit": item[1],
                             "source_path": item[2]["source_path"],
                             "line": item[2]["line"], "quote": item[2]["quote"]}
                            for item in numeric[:4]]
                        mentions.append(record)
                        continue
                found = lookup_number(pool, mention, source_only=source_only)
                # The literal-substring fallback must not run on the draft itself:
                # a number is trivially "found" in the line it was cut from.
                usage = None if source_only else material_text_has_usage(material, mention)
                if found is not None:
                    record["verdict"] = "traced_number"
                    record["material_match"] = found
                elif usage is not None:
                    record["verdict"] = "traced_number"
                    record["material_match"] = usage
                elif not cross_check:
                    # There is no configuration/log/code material to compare against:
                    # calling this "invented" would be wrong, so it is marked as
                    # unverifiable and explained in limits instead.
                    record["verdict"] = "unverifiable"
                    record["severity"] = "info"
                mentions.append(record)
    for index, record in enumerate(mentions, start=1):
        record["mention_id"] = "num-" + str(index).zfill(3)
        # A conflict between the draft and a material value is always high severity:
        # this is exactly the "number invented for a smooth sentence" failure mode.
        if record.get("verdict") in {"mismatch", "unit_mismatch"}:
            record["severity"] = "high"
    return mentions


def material_text_has_usage(material: dict[str, Any], mention: dict[str, Any]) -> dict[str, Any] | None:
    """Fallback: the number is a literal substring of a material line (e.g. inside a path)."""
    needle = str(mention["raw"]).strip()
    if len(needle) < 2:
        return None
    for index, line in enumerate(material.get("lines") or [], start=1):
        if needle in line:
            return {"source": material["rel"], "line": index,
                    "quote": quote_around(line, needle), "page": 1,
                    "block_id": "L" + str(index).zfill(5), "bbox": None,
                    "raw": needle}
    return None


KEYSET_RE = re.compile(r"\b(dataset|corpus|benchmark|数据|数据集|语料|评测集)\b", re.I)
BASELINE_RE = re.compile(r"\b(baseline|compared? (?:with|to|against)|对照|基线|oracle)\b", re.I)
SPLIT_RE = re.compile(r"\b(train(?:ing)?|val(?:idation)?|test|dev)\b[^\n]{0,40}?(\d{1,3})\s*[%/]",
                      re.I)
RATIO_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*[:/]\s*(\d{1,3}(?:\.\d+)?)\s*[:/]\s*(\d{1,3}(?:\.\d+)?)")
VERSION_TAG_RE = re.compile(r"\b[vV](\d+(?:\.\d+){0,2})\b|\bversion\s*[=: ]\s*([\w\.\-]+)", re.I)
SIZE_RE = re.compile(r"\b(\d[\d,\.]{0,14})\s*(images|samples|examples|records|sentences|"
                     r"documents|rows|patients|GB|MB|TB|tokens)\b", re.I)


def _candidate_from_line(kind: str, material: dict[str, Any], line_no: int, line: str,
                         rules: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": kind, "source_path": material["rel"], "line": line_no,
        "quote": line.strip()[:MAX_QUOTE], "text": line.strip()[:300],
    }
    version = None
    match = VERSION_TAG_RE.search(line)
    if match:
        version = match.group(1) or match.group(2)
    record["version"] = version
    ratio = RATIO_RE.search(line)
    if ratio:
        record["split_ratio"] = ratio.group(0).replace(" ", "")
    size = SIZE_RE.search(line)
    if size:
        record["size"] = (size.group(1) + " " + size.group(2)).strip()
    metric_names = [str(m).casefold() for m in (rules.get("metric_names") or [])]
    record["metrics"] = sorted({name for name in metric_names if name in line.casefold()})
    return record


def collect_candidates(materials: list[dict[str, Any]], facts: dict[str, Any],
                       rules: dict[str, Any]) -> dict[str, Any]:
    """Dataset / split / baseline candidates, each with file+line provenance."""
    datasets: list[dict[str, Any]] = []
    splits: list[dict[str, Any]] = []
    baselines: list[dict[str, Any]] = []
    for material in sorted(materials, key=lambda m: m["rel"]):
        if material["kind"] in {"unreadable"}:
            continue
        for index, raw in enumerate(material.get("lines") or [], start=1):
            line = raw.strip()
            if not line or md_heading(line):
                continue
            if KEYSET_RE.search(line):
                datasets.append(_candidate_from_line("dataset", material, index, line, rules))
            if re.search(r"\bsplit|划分|比例", line, re.I):
                splits.append(_candidate_from_line("split", material, index, line, rules))
            if BASELINE_RE.search(line):
                baselines.append(_candidate_from_line("baseline", material, index, line, rules))
    for entry in facts["params"]:
        low = entry["canonical"]
        if any(token in low for token in ("data", "dataset", "corpus", "split")):
            datasets.append({
                "kind": "dataset", "source_path": entry["source_path"], "line": entry["line"],
                "quote": entry["quote"], "text": entry["name"] + " = " + entry["value"],
                "version": None, "size": None, "split_ratio": None,
                "metrics": [], "value": entry["value"],
            })
    return {"datasets": datasets[:60], "splits": splits[:40], "baselines": baselines[:40]}


def analyse_draft(material: dict[str, Any] | None, materials: list[dict[str, Any]],
                  pool: dict[str, Any], variants: dict[str, list[str]],
                  param_index: dict[str, list[dict[str, Any]]],
                  cross_check: bool = True) -> dict[str, Any]:
    """Segments + prose numbers for the chosen draft (empty when no draft exists)."""
    if material is None:
        return {"present": False, "path": None, "segments": [], "sections": [],
                "mentions": [], "method_sentences": []}
    sections = detect_draft_sections(material)
    segments = hybrid.build_segments(material["doc"], {"sections": sections})
    mentions = analyse_mentions(material, pool, variants, param_index, source_only=True,
                               cross_check=cross_check)
    method_sentences: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "")
        if is_method_sentence(text):
            method_sentences.append({
                "seg_id": segment["seg_id"], "page": segment["page"],
                "line": (segment.get("bbox") or [None]) and _segment_line(segment),
                "text": text[:300],
            })
    return {"present": True, "path": material["rel"], "segments": segments,
            "sections": sections, "mentions": mentions,
            "method_sentences": method_sentences}


def _segment_line(segment: dict[str, Any]) -> int | None:
    bbox = segment.get("bbox") or []
    if len(bbox) == 4 and bbox[1] is not None:
        try:
            return int(round(-float(bbox[1])))
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# six-element coverage, gaps and author questions
# --------------------------------------------------------------------------- #
def element_evidence(analysis: dict[str, Any], facts: dict[str, Any],
                     candidates: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """What the materials actually support, per element of the topic rubric."""
    draft = analysis.get("draft") or {}
    method_sentences = draft.get("method_sentences") or []
    seed_entries = [entry for entry in facts["params"] if entry["canonical"] == "seed"]
    repeat_entries = [entry for entry in facts["params"] if entry["canonical"] == "repeats"]
    dataset_hits = [item for item in candidates["datasets"]
                    if item.get("version") or item.get("split_ratio") or item.get("size")]
    baseline_hits = candidates["baselines"]
    limitation_hits = list(facts["anomalies"])
    draft_text = ""
    if draft.get("present"):
        for material in analysis.get("materials") or []:
            if material["rel"] == draft.get("path"):
                draft_text = material["text"]
                break
    for marker in rules.get("limitation_markers") or []:
        if str(marker).casefold() in draft_text.casefold():
            limitation_hits.append({"text": str(marker), "source_path": draft.get("path"),
                                    "line": None, "quote": str(marker)})
            break

    def _pack(covered: bool, why: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        return {"covered": covered, "why": why, "evidence": evidence[:4]}

    return {
        "steps": _pack(
            len(method_sentences) >= 3,
            "draft contains " + str(len(method_sentences)) + " method-like sentence(s)",
            [{"text": item["text"][:160], "source_path": draft.get("path"),
              "line": item.get("line")} for item in method_sentences[:4]]),
        "settings": _pack(
            bool(facts["hardware"]) and bool(facts["versions"]) and bool(seed_entries),
            "hardware=" + str(len(facts["hardware"])) + ", software versions=" +
            str(len(facts["versions"])) + ", seeds=" + str(len(seed_entries)),
            [{"text": item["text"][:160], "source_path": item["source_path"],
              "line": item["line"]} for item in facts["hardware"][:2]] +
            [{"text": item["name"] + "==" + item["version"], "source_path": item["source_path"],
              "line": item["line"]} for item in facts["versions"][:2]] +
            [{"text": "seed=" + entry["value"], "source_path": entry["source_path"],
              "line": entry["line"]} for entry in seed_entries[:1]]),
        "parameters": _pack(
            len(facts["params"]) >= 3,
            str(len(facts["params"])) + " parameter fact(s) with file+line provenance",
            [{"text": entry["name"] + "=" + entry["value"],
              "source_path": entry["source_path"], "line": entry["line"]}
             for entry in facts["params"][:4]]),
        "datasets": _pack(
            bool(dataset_hits),
            str(len(candidates["datasets"])) + " dataset mention(s), " +
            str(len(dataset_hits)) + " with version/size/split",
            [{"text": item["text"][:160], "source_path": item["source_path"],
              "line": item["line"]} for item in dataset_hits[:3]]),
        "baselines": _pack(
            bool(baseline_hits),
            str(len(baseline_hits)) + " baseline/control mention(s)",
            [{"text": item["text"][:160], "source_path": item["source_path"],
              "line": item["line"]} for item in baseline_hits[:3]]),
        "repro_and_limitations": _pack(
            bool(facts["commands"]) and bool(limitation_hits),
            "commands=" + str(len(facts["commands"])) + ", limitation/anomaly signals=" +
            str(len(limitation_hits)),
            [{"text": item["command"][:160], "source_path": item["source_path"],
              "line": item["line"]} for item in facts["commands"][:2]] +
            [{"text": item["text"][:160], "source_path": item["source_path"],
              "line": item["line"]} for item in limitation_hits[:2]]),
    }


def build_gaps(analysis: dict[str, Any], facts: dict[str, Any], candidates: dict[str, Any],
               elements: dict[str, Any], conflicts: list[dict[str, Any]],
               rules: dict[str, Any]) -> dict[str, Any]:
    """Turn every uncovered element, mismatch and conflict into an explicit question."""
    gaps: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    draft = analysis.get("draft") or {}
    mentions = draft.get("mentions") or []

    if not draft.get("present"):
        gaps.append({"element": "draft", "severity": "blocking",
                     "detail": "no methods draft was found among the materials"})
        questions.append({"question": "请提供当前的方法与实验章节草稿（Markdown/LaTeX/DOCX/PDF 均可）。",
                          "why": "没有草稿就无法做逐句对齐，只能先从配置与日志生成参数表。",
                          "severity": "blocking", "evidence": []})

    for element in ELEMENTS:
        packed = elements.get(element) or {}
        if packed.get("covered"):
            continue
        gaps.append({"element": element, "severity": "blocking",
                     "detail": ELEMENT_LABELS.get(element, element) + " 在材料中证据不足：" +
                               str(packed.get("why") or "")})
        questions.append({
            "question": "请补充「" + ELEMENT_LABELS.get(element, element) + "」的原始材料"
                        "（配置、日志、代码、数据说明或作者口述均可）。",
            "why": "材料中找不到该要素的可核验来源；补写会变成无来源断言。",
            "severity": "blocking",
            "evidence": packed.get("evidence") or [],
        })

    for conflict in conflicts:
        gaps.append({"element": "parameters", "severity": "high",
                     "detail": "参数 " + conflict["name"] + " 在材料间取值冲突：" +
                               ", ".join(conflict["values"])})
        questions.append({
            "question": "参数 " + conflict["name"] + " 在不同材料中取值不一致（" +
                        ", ".join(conflict["values"]) + "），正文与参数表应以哪个为准？",
            "why": "配置/日志/代码互相矛盾，必须由作者确认后才可写入章节。",
            "severity": "high",
            "evidence": [{"text": item["path"] + ":" + str(item["line"]) + " = " + item["value"],
                          "source_path": item["path"], "line": item["line"]}
                         for item in conflict["sources"][:4]],
        })

    for record in mentions:
        if record.get("verdict") == "mismatch":
            declared = record.get("declared_values") or []
            target = declared[0]["value"] if declared else "?"
            gaps.append({"element": "parameters", "severity": "high",
                         "detail": "draft 写 " + str(record["value_raw"]) + "，材料写 " + str(target)})
            questions.append({
                "question": "草稿中 " + str(record.get("bound_param") or "该参数") + " 写作 " +
                            str(record["value_raw"]) + "，而材料中为 " + str(target) +
                            "（" + str(declared[0]["source_path"]) + ":" +
                            str(declared[0]["line"]) + "）。请确认哪个正确。",
                "why": "这是「为了段落顺畅补数值」最典型的情况，必须回源确认，不能自行改写。",
                "severity": "high",
                "evidence": [{"text": record.get("quote") or "", "source_path": record.get("source"),
                              "line": record.get("line")}] + [
                    {"text": item["quote"], "source_path": item["source_path"],
                     "line": item["line"]} for item in declared[:2]],
            })
        elif record.get("verdict") == "unit_mismatch":
            gaps.append({"element": "parameters", "severity": "medium",
                         "detail": "draft 数值单位与材料不一致：" + str(record["value_raw"])})
            questions.append({
                "question": "草稿中 " + str(record["value_raw"]) + " 的单位与材料记录不一致，请确认单位。",
                "why": "单位错误会使复现结果差一个数量级。",
                "severity": "medium", "evidence": [],
            })
        elif record.get("verdict") == "untraceable" and record.get("severity") == "high":
            gaps.append({"element": "parameters", "severity": "high",
                         "detail": "draft 数值在材料中无来源：" + str(record["value_raw"])})
            questions.append({
                "question": "草稿中的数值 " + str(record["value_raw"]) + " 在配置/日志/代码/数据说明中"
                            "都找不到来源，请提供来源或确认删除。",
                "why": "无法追溯的数值无法被复现，写入章节会构成无来源事实。",
                "severity": "high",
                "evidence": [{"text": record.get("quote") or "", "source_path": record.get("source"),
                              "line": record.get("line")}],
            })

    if not facts["commands"]:
        questions.append({"question": "请提供复现实验所用的命令（或仓库 README 中的运行说明）。",
                          "why": "没有命令，复现清单无法闭合。", "severity": "blocking",
                          "evidence": []})
    if not facts["versions"]:
        questions.append({"question": "请提供关键依赖与代码版本（如 torch==2.1.0、commit hash）。",
                          "why": "环境版本缺失会导致复现失败。", "severity": "blocking",
                          "evidence": []})
    if not any(entry["canonical"] == "seed" for entry in facts["params"]):
        questions.append({"question": "请提供随机种子（以及是否固定所有随机源）。",
                          "why": "没有种子无法复现划分与初始化。", "severity": "blocking",
                          "evidence": []})
    if not any(entry["canonical"] == "repeats" for entry in facts["params"]):
        questions.append({"question": "请说明实验重复次数与统计方式（均值/标准差/置信区间）。",
                          "why": "单次实验无法支撑稳定性结论。", "severity": "advisory",
                          "evidence": []})
    for anomaly in facts["anomalies"][:3]:
        questions.append({"question": "日志中出现异常（" + anomaly["text"][:80] + "），是否需要写入限制条件？",
                          "why": "日志异常往往对应具体失效场景，适合写进限制而不是套话。",
                          "severity": "advisory",
                          "evidence": [{"text": anomaly["text"], "source_path": anomaly["source_path"],
                                        "line": anomaly["line"]}]})
    gaps.sort(key=lambda item: (item["severity"] != "blocking", item["element"], item["detail"]))
    questions.sort(key=lambda item: (item["severity"] != "blocking", item["question"]))
    return {"gaps": gaps, "questions": questions}


# --------------------------------------------------------------------------- #
# one-shot deterministic analysis shared by prepare and finalize
# --------------------------------------------------------------------------- #
def analyze(found: dict[str, Any], rules: dict[str, Any],
            stopwatch: hybrid.Stopwatch,
            exclude: Iterable[Path] = ()) -> dict[str, Any]:
    """Scan + parse + cross-check, with no dependence on prepare having run.

    finalize calls this again instead of trusting work/params_candidates.json, so the
    verification pass is independent from the extraction pass: a stale or tampered
    worksheet cannot make an unsourced number look traced.
    """
    roots = [Path(root) for root in (found.get("input_roots") or []) if Path(root).is_dir()]
    if not roots:
        roots = [Path.cwd()]
    paths = scan_materials(roots, rules, exclude)
    materials = load_materials(paths, roots, rules, stopwatch)
    draft = pick_draft(materials, rules)
    facts = collect_facts(materials, rules)
    param_index, conflicts = build_param_index(facts["params"])
    variants = build_variants(param_index, rules)
    skip = {draft["rel"]} if draft else set()
    pool = build_number_pool(materials, skip)
    # Is there any independent evidence (config/log/code/README) to check the draft
    # against? Without it, "untraceable" would be an accusation we cannot support.
    cross_check = bool(facts["params"]) or any(
        material["kind"] in {"config", "log", "code", "readme"} for material in materials)
    draft_analysis = analyse_draft(draft, materials, pool, variants, param_index,
                                  cross_check=cross_check)
    candidates = collect_candidates(materials, facts, rules)
    analysis = {
        "materials": materials,
        "material_summary": [{
            "path": material["rel"], "kind": material["kind"],
            "bytes": material["bytes"], "lines": material["line_count"],
            "engine": material["engine"], "anchor_quality": material["anchor_quality"],
            "truncated": material["truncated"], "warnings": material["warnings"],
        } for material in materials],
        "draft": draft_analysis,
        "facts": facts,
        "param_index": param_index,
        "param_conflicts": conflicts,
        "variants": variants,
        "number_pool": pool,
        "candidates": candidates,
        "cross_check_available": cross_check,
    }
    elements = element_evidence(analysis, facts, candidates, rules)
    analysis["elements"] = elements
    analysis["gaps_questions"] = build_gaps(analysis, facts, candidates, elements, conflicts, rules)
    return analysis


# --------------------------------------------------------------------------- #
# agent-generated content: reading, normalising, verifying
# --------------------------------------------------------------------------- #
GENERATED_NAME = "methods.json"


def read_generated(work: Path) -> dict[str, Any]:
    """Read work/generated/methods.json plus every batch answer file."""
    generated_dir = work / "generated"
    methods: dict[str, Any] | None = None
    batch_records: list[dict[str, Any]] = []
    files: list[str] = []
    if generated_dir.is_dir():
        for path in sorted(generated_dir.glob("*.json")):
            if path.name.startswith("_"):
                continue
            files.append(str(path))
            if path.name == GENERATED_NAME:
                data = hybrid.read_json(path)
                if isinstance(data, dict):
                    methods = data
                continue
            if path.name.startswith("batch-"):
                batch_records.extend(hybrid.read_records(path))
    return {"methods": methods, "batch_records": batch_records, "files": files,
            "missing_batches": hybrid.missing_batches(work)}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [{"text": value}]
    return []


def _first(raw: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in raw:
            value = raw[key]
            if value not in (None, "", [], {}):
                return value
    return default


def _text_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("text", "content", "operation", "description", "value", "name"):
            if item.get(key):
                return str(item[key])
    return ""


def normalise_generated(raw: dict[str, Any] | None, rules: dict[str, Any]) -> dict[str, Any]:
    """Accept the documented shape and the obvious synonyms; never crash on quirks."""
    raw = raw or {}
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(_as_list(_first(raw, ("steps", "procedure", "methods_steps",
                                                      "step_list", "workflow"))), start=1):
        record = item if isinstance(item, dict) else {"operation": _text_of(item)}
        steps.append({
            "step_id": str(record.get("step_id") or record.get("id") or ("S" + str(index))),
            "order": index,
            "input": str(record.get("input") or ""),
            "operation": str(record.get("operation") or record.get("action")
                             or record.get("text") or ""),
            "output": str(record.get("output") or ""),
            "condition": str(record.get("condition") or record.get("branch")
                             or record.get("if") or ""),
            "source": str(record.get("source") or record.get("evidence") or ""),
            "note": str(record.get("note") or ""),
        })
    parameters: list[dict[str, Any]] = []
    for item in _as_list(_first(raw, ("parameter_table", "parameters", "params",
                                      "hyperparameters", "param_table"))):
        record = item if isinstance(item, dict) else {"value": _text_of(item)}
        parameters.append({
            "name": str(record.get("name") or record.get("param") or record.get("key") or ""),
            "value": str(record.get("value") or record.get("val") or ""),
            "unit": str(record.get("unit") or ""),
            "target": str(record.get("target") or record.get("applies_to")
                          or record.get("scope") or record.get("purpose") or ""),
            "source": str(record.get("source") or record.get("evidence") or ""),
        })
    datasets: list[dict[str, Any]] = []
    for item in _as_list(_first(raw, ("datasets", "data_assets", "dataset", "data"))):
        record = item if isinstance(item, dict) else {"name": _text_of(item)}
        datasets.append({
            "name": str(record.get("name") or ""),
            "version": str(record.get("version") or ""),
            "size": str(record.get("size") or record.get("scale") or ""),
            "preprocessing": str(record.get("preprocessing") or record.get("preprocess") or ""),
            "split": str(record.get("split") or record.get("split_ratio") or ""),
            "split_method": str(record.get("split_method") or record.get("randomization")
                                or record.get("seed") or ""),
            "subsets": [str(x) for x in _as_list(record.get("subsets"))],
            "source": str(record.get("source") or ""),
        })
    settings_raw = _first(raw, ("settings", "experimental_settings", "experiment_setup",
                                "setup", "environment"), default={})
    settings: dict[str, Any] = dict(settings_raw) if isinstance(settings_raw, dict) else {
        "notes": str(settings_raw)}
    baselines: list[dict[str, Any]] = []
    for item in _as_list(_first(raw, ("baselines", "baseline_table", "comparisons",
                                      "baseline"))):
        record = item if isinstance(item, dict) else {"name": _text_of(item)}
        baselines.append({
            "name": str(record.get("name") or ""),
            "version": str(record.get("version") or ""),
            "budget": str(record.get("budget") or record.get("same_budget") or ""),
            "metric": str(record.get("metric") or record.get("metrics") or ""),
            "result": str(record.get("result") or record.get("score") or ""),
            "source": str(record.get("source") or ""),
            "note": str(record.get("note") or ""),
        })
    repro_raw = _first(raw, ("reproducibility", "repro", "reproduction"), default={})
    repro = dict(repro_raw) if isinstance(repro_raw, dict) else {"notes": str(repro_raw)}
    limitations: list[dict[str, Any]] = []
    for item in _as_list(_first(raw, ("limitations", "limitation", "caveats", "boundaries"))):
        record = item if isinstance(item, dict) else {"limitation": _text_of(item)}
        limitations.append({
            "limitation": str(record.get("limitation") or record.get("text") or ""),
            "failure_scenario": str(record.get("failure_scenario") or record.get("failure")
                                    or record.get("when_it_fails") or ""),
            "scope": str(record.get("scope") or record.get("applies_to") or ""),
            "needs_author_confirmation": bool(record.get("needs_author_confirmation")
                                              or record.get("needs_confirmation") or False),
        })
    questions: list[dict[str, Any]] = []
    for item in _as_list(_first(raw, ("questions", "author_followups", "open_questions",
                                      "gaps", "needs_confirmation"))):
        record = item if isinstance(item, dict) else {"question": _text_of(item)}
        questions.append({"question": str(record.get("question") or _text_of(record)),
                          "why": str(record.get("why") or ""),
                          "severity": str(record.get("severity") or "advisory"),
                          "evidence": _as_list(record.get("evidence"))[:3]})
    section = str(_first(raw, ("methods_section", "chapter_body", "section", "methods_text",
                               "chapter", "text", "markdown"), default="") or "")
    return {
        "steps": steps, "parameter_table": parameters, "datasets": datasets,
        "settings": settings, "baselines": baselines, "reproducibility": repro,
        "limitations": limitations, "questions": questions, "methods_section": section,
        "agent_kind": str(raw.get("agent") or ""),
    }


def resolve_source(source: str, analysis: dict[str, Any]) -> tuple[dict[str, Any] | None, int | None]:
    """Resolve an agent-declared locator such as 'environment/configs/train.yaml:12'."""
    text = str(source or "").strip()
    if not text:
        return None, None
    line: int | None = None
    match = re.search(r"[:\s]\s*(\d{1,6})\s*$", text)
    if match:
        line = int(match.group(1))
        text = text[:match.start()].strip()
    text = text.strip().strip("' ,;.").strip()
    if not text:
        return None, line
    best = None
    for material in analysis["materials"]:
        rel = material["rel"].replace("\\", "/")
        needle = text.replace("\\", "/").lstrip("./")
        if needle == rel or needle.endswith("/" + rel) or rel.endswith("/" + needle):
            best = material
            break
        if Path(needle).name == Path(rel).name and best is None:
            best = material
    if best is not None and line is not None:
        limit = len(best.get("lines") or [])
        if limit and line > limit:
            line = None
    return best, line


def line_text(material: dict[str, Any], line: int | None) -> str:
    lines = material.get("lines") or []
    if line is None or not lines or line < 1 or line > len(lines):
        return ""
    return lines[line - 1]


def value_in_text(value: str, text: str) -> bool:
    """Does this text state that value? Numeric comparison, not string equality."""
    if not str(value or "").strip() or not text:
        return False
    if str(value).strip() in text:
        return True
    wanted, _wanted_unit = parse_number(str(value))
    if wanted is None:
        return str(value).strip().casefold() in text.casefold()
    for mention in find_number_mentions(text):
        if mention["value"] is not None and number_equal(wanted, mention["value"]):
            return True
    return False


def verify_parameter_rows(rows: list[dict[str, Any]], analysis: dict[str, Any],
                          rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify every agent-declared parameter against the materials.

    The agent's own 'source' string is checked, not trusted: the line it names must
    actually contain the value. If it does not, the row is re-traced to a real
    material; if nothing matches, the row is marked untraceable and left in place
    (flagged) rather than silently corrected to a plausible number.
    """
    verified: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "")
        value = str(row.get("value") or "")
        canonical = canonical_name(name, rules) if name else ""
        material, line = resolve_source(row.get("source"), analysis)
        source_text = line_text(material, line) if material is not None else ""
        claimed_ok = bool(material is not None) and value_in_text(value, source_text or "")
        entries = analysis["param_index"].get(canonical) or []
        matched = None
        declared: list[dict[str, Any]] = []
        for entry in entries:
            entry_value = str(entry.get("value") or "")
            if value and (value_in_text(entry_value, value) or value_in_text(value, entry_value)):
                matched = entry
                break
            entry_num, _unit = parse_number(entry_value)
            if entry_num is not None:
                declared.append({"value": entry_value, "source_path": entry["source_path"],
                                 "line": entry["line"], "quote": entry["quote"]})
        record = {
            "name": name, "value": value, "unit": str(row.get("unit") or ""),
            "target": str(row.get("target") or ""),
            "trace_status": "untraceable",
            "source_path": None, "line": None, "quote": "",
            "agent_source": str(row.get("source") or ""), "source_verified": False,
            "note": "",
        }
        if claimed_ok:
            record["trace_status"] = "traced"
            record["source_verified"] = True
            record["source_path"] = material["rel"]
            record["line"] = line
            record["quote"] = quote_around(source_text, value)
        elif matched is not None:
            record["trace_status"] = "traced"
            record["source_path"] = matched["source_path"]
            record["line"] = matched["line"]
            record["quote"] = matched["quote"]
            record["note"] = "agent source did not resolve; traced to the material instead"
        elif entries and declared:
            numeric_declared = [d for d in declared if parse_number(d["value"])[0] is not None]
            if numeric_declared and parse_number(value)[0] is not None:
                record["trace_status"] = "mismatch"
                record["source_path"] = numeric_declared[0]["source_path"]
                record["line"] = numeric_declared[0]["line"]
                record["quote"] = numeric_declared[0]["quote"]
                record["note"] = "materials define " + numeric_declared[0]["value"] + " here"
                record["declared_values"] = numeric_declared[:4]
        verified.append(record)
    return verified


def analyse_generated_section(section: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Find numbers in the agent's chapter that no supplied material supports.

    This is the check that implements "never invent a value for a smooth sentence".
    The generated text is treated as a document whose ''lines'' list is empty, so the
    literal-substring fallback cannot find the number inside itself (that would be a
    self-proving loop); only the real materials are searched.
    """
    if not section.strip():
        return {"present": False, "untraceable": [], "mismatched": [], "numbers_total": 0,
                "traced": 0}
    synthetic = {
        "rel": "generated:methods_section",
        "text": section,
        "lines": [],
        "doc": text_doc(section, "generated:methods_section"),
        "kind": "generated",
        "anchor_quality": "line",
    }
    mentions = analyse_mentions(synthetic, analysis["number_pool"], analysis["variants"],
                                analysis["param_index"], source_only=False)
    untraceable = [record for record in mentions
                   if record["verdict"] == "untraceable" and record["severity"] == "high"]
    mismatched = [record for record in mentions if record["verdict"] == "mismatch"]
    traced = [record for record in mentions if record["verdict"] in
              {"match", "traced_number", "unit_mismatch"}]
    return {"present": True, "numbers_total": len(mentions), "traced": len(traced),
            "untraceable": untraceable, "mismatched": mismatched,
            "low_severity_untraceable": [record for record in mentions
                                         if record["verdict"] == "untraceable"
                                         and record["severity"] != "high"]}


def compose_baseline_section(analysis: dict[str, Any]) -> str:
    """Deterministic fallback chapter, assembled only from material content."""
    draft = analysis.get("draft") or {}
    facts = analysis["facts"]
    lines: list[str] = [
        "> Deterministic baseline: no agent-generated chapter was found, so this text "
        "is assembled from the supplied materials. It is not a rewritten manuscript; "
        "it is the honest floor the reviewer can compare against.",
        "",
    ]
    if draft.get("present"):
        segments = draft.get("segments") or []
        by_section: dict[str, list[str]] = {}
        order: list[str] = []
        for segment in segments:
            text = str(segment.get("text") or "").strip()
            if not text or md_heading(text):
                continue
            if not is_method_sentence(text):
                continue
            title = str(segment.get("section_title") or "Methods")
            if title not in by_section:
                by_section[title] = []
                order.append(title)
            if len(by_section[title]) < 6:
                by_section[title].append(text)
        for title in order:
            lines.append("## " + title)
            for sentence in by_section[title]:
                lines.append(sentence)
            lines.append("")
        if not order:
            lines.append("## Methods")
            for segment in segments[:12]:
                text = str(segment.get("text") or "").strip()
                if text and not md_heading(text):
                    lines.append(text)
            lines.append("")
    else:
        lines.extend(["## Methods", "未能找到方法章节草稿；以下内容由配置文件、日志与代码直接汇总。", ""])

    if facts["params"]:
        lines.append("## 参数（来自配置/日志/代码）")
        for entry in facts["params"][:24]:
            unit = (" " + entry["unit"]) if entry["unit"] else ""
            lines.append("- " + entry["name"] + " = " + entry["value"] + unit +
                         "（来源：" + entry["source_path"] + ":" + str(entry["line"]) + "）")
        lines.append("")
    if facts["commands"]:
        lines.append("## 复现命令")
        for item in facts["commands"][:6]:
            lines.append("- " + item["command"] + "（来源：" + item["source_path"] + ":" +
                         str(item["line"]) + "）")
        lines.append("")
    if facts["versions"]:
        lines.append("## 环境版本")
        for item in facts["versions"][:8]:
            lines.append("- " + item["name"] + "==" + item["version"] + "（来源：" +
                         item["source_path"] + ":" + str(item["line"]) + "）")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


PLACEHOLDER_TEXTS = {"not recorded", "not recorded in the materials", "unknown", "n/a",
                    "na", "none", "tbd", "todo", "missing", "未记录", "未知", "暂无", "待补充", "-"}


def is_placeholder_text(text: str) -> bool:
    return str(text or "").strip().casefold() in PLACEHOLDER_TEXTS


GENERIC_LIMITATION_FALLBACK = "材料未给出可核验的限制条件；本段未编造具体失效场景，需作者补充。"


def limitation_specificity(item: dict[str, Any], rules: dict[str, Any]) -> str:
    """Judge a limitation on its wording only: specific / vague / generic."""
    text = (str(item.get("limitation") or "") + " " + str(item.get("failure_scenario") or "")).strip()
    if not text:
        return "empty"
    for phrase in rules.get("generic_limitation_phrases") or []:
        if str(phrase).casefold() in text.casefold():
            return "generic"
    scenario = str(item.get("failure_scenario") or "").strip()
    if len(scenario) < 12:
        return "vague"
    if re.search(r"\bwhen\b|\bif\b|\bonly\b|\bbeyond\b|\bbelow\b|\babove\b|"
                 r"当|如果|仅|超过|低于|不适用", scenario + " " + text, re.I):
        return "specific"
    return "vague"


def build_repro_checklist(analysis: dict[str, Any], generated: dict[str, Any],
                          rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Code version / environment / commands / seeds / data / hardware / repeats."""
    facts = analysis["facts"]
    repro = generated.get("reproducibility") or {}

    def _repro_value(*keys: str) -> str:
        for key in keys:
            if isinstance(repro, dict) and repro.get(key):
                value = repro[key]
                if isinstance(value, list):
                    text = "; ".join(str(item) for item in value if not is_placeholder_text(item))
                else:
                    text = "" if is_placeholder_text(value) else str(value)
                if text.strip():
                    return text
        return ""

    seeds = [entry for entry in facts["params"] if entry["canonical"] == "seed"]
    repeats = [entry for entry in facts["params"] if entry["canonical"] == "repeats"]
    datasets = analysis["candidates"]["datasets"]
    items: list[dict[str, Any]] = []

    def add(item: str, status: str, detail: str, sources: list[dict[str, Any]]) -> None:
        items.append({"item": item, "status": status, "detail": detail, "sources": sources[:4]})

    code_version = _repro_value("code_version", "version", "commit", "commit_hash")
    code_sources = [{"path": item["source_path"], "line": item["line"],
                     "quote": item["quote"]} for item in facts["versions"][:3]]
    if code_version:
        add("代码版本", "verified", "agent reported: " + code_version, code_sources)
    elif code_sources:
        add("代码版本", "partial", "材料中只有依赖版本，未见代码 commit/tag", code_sources)
    else:
        add("代码版本", "missing", "材料中未见代码版本或 commit", [])

    env = _repro_value("environment", "env", "packages")
    env_sources = [{"path": item["source_path"], "line": item["line"],
                    "quote": item["quote"]} for item in facts["versions"][:6]]
    if env:
        add("运行环境", "verified", "agent reported: " + env, env_sources)
    elif env_sources:
        add("运行环境", "verified", str(len(env_sources)) + " dependency version pin(s) found",
            env_sources)
    else:
        add("运行环境", "missing", "材料中未见依赖或环境版本", [])

    commands = _repro_value("commands", "command", "run")
    command_sources = [{"path": item["source_path"], "line": item["line"],
                        "quote": item["quote"]} for item in facts["commands"][:4]]
    if commands:
        add("运行命令", "verified", "agent reported: " + commands, command_sources)
    elif command_sources:
        add("运行命令", "verified", str(len(command_sources)) + " command(s) found in materials",
            command_sources)
    else:
        add("运行命令", "missing", "材料中未见可执行命令", [])

    seed_text = _repro_value("seeds", "seed", "random_seed")
    seed_sources = [{"path": entry["source_path"], "line": entry["line"],
                     "quote": entry["quote"]} for entry in seeds[:4]]
    if seed_text:
        add("随机种子", "verified", "agent reported: " + seed_text, seed_sources)
    elif seed_sources:
        add("随机种子", "verified", "seed = " + seeds[0]["value"], seed_sources)
    else:
        add("随机种子", "missing", "材料中未见随机种子", [])

    data_text = _repro_value("data", "dataset", "data_availability")
    data_sources = [{"path": item["source_path"], "line": item["line"],
                     "quote": item["quote"]} for item in datasets[:3]]
    if data_text:
        add("数据获取方式", "verified", "agent reported: " + data_text, data_sources)
    elif data_sources:
        add("数据获取方式", "partial", "材料中只有数据集说明，未见下载/获取方式", data_sources)
    else:
        add("数据获取方式", "missing", "材料中未见数据集获取方式", [])

    hardware_text = _repro_value("hardware", "device", "gpu")
    hardware_sources = [{"path": item["source_path"], "line": item["line"],
                         "quote": item["quote"]} for item in facts["hardware"][:3]]
    if hardware_text:
        add("硬件环境", "verified", "agent reported: " + hardware_text, hardware_sources)
    elif hardware_sources:
        add("硬件环境", "verified", "hardware line found in materials", hardware_sources)
    else:
        add("硬件环境", "missing", "材料中未见硬件说明", [])

    repeat_text = _repro_value("repeats", "repetitions", "n_runs")
    repeat_sources = [{"path": entry["source_path"], "line": entry["line"],
                       "quote": entry["quote"]} for entry in repeats[:3]]
    if repeat_text:
        add("重复与统计", "verified", "agent reported: " + repeat_text, repeat_sources)
    elif repeat_sources:
        add("重复与统计", "verified", "repeats = " + repeats[0]["value"], repeat_sources)
    else:
        add("重复与统计", "missing", "材料中未见重复次数或统计方式", [])
    return items


def merge_batch_coverage(analysis: dict[str, Any],
                         batch_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Which draft segments the agent accounted for, and which roles it assigned."""
    segments = (analysis.get("draft") or {}).get("segments") or []
    if not segments:
        return {"applicable": False, "total_segments": 0, "received": 0, "coverage": 0.0,
                "missing": [], "roles": {}, "by_segment": {}}
    merged = hybrid.merge_generated(segments, batch_records, required=())
    roles: dict[str, list[str]] = {}
    by_segment: dict[str, dict[str, Any]] = {}
    for seg_id, record in merged["by_id"].items():
        raw_roles = record.get("roles") or record.get("role") or record.get("elements")
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        if not isinstance(raw_roles, list):
            raw_roles = []
        cleaned = sorted({str(role).strip().casefold() for role in raw_roles if str(role).strip()})
        roles[seg_id] = cleaned
        by_segment[seg_id] = {"roles": cleaned, "note": str(record.get("note") or "")}
    return {"applicable": True, "total_segments": merged["total_segments"],
            "received": merged["received"], "coverage": merged["coverage"],
            "missing": merged["missing"], "unknown_ids": merged["unknown_ids"],
            "duplicates": merged["duplicates"], "roles": roles, "by_segment": by_segment}


# --------------------------------------------------------------------------- #
# anchors: every traceable conclusion carries page + block_id + bbox + quote
# --------------------------------------------------------------------------- #
def build_block_index(analysis: dict[str, Any]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for material in analysis["materials"]:
        by_line: dict[int, tuple[int, dict[str, Any]]] = {}
        by_block: dict[str, tuple[int, dict[str, Any]]] = {}
        blocks = (material["doc"] or {}).get("blocks") or {}
        for page_key in sorted(blocks, key=lambda k: int(k)):
            for block in blocks[page_key]:
                page = int(page_key)
                by_block[str(block.get("block_id"))] = (page, block)
                if block.get("line"):
                    by_line[int(block["line"])] = (page, block)
        index[material["rel"]] = {"lines": by_line, "blocks": by_block}
    return index


def locate_anchor(index: dict[str, Any], rel: str, *, page: int | None = None,
                  block_id: str | None = None, line: int | None = None,
                  quote: str = "") -> dict[str, Any] | None:
    info = index.get(str(rel)) or {}
    pick: tuple[int, dict[str, Any]] | None = None
    if block_id is not None and str(block_id) in (info.get("blocks") or {}):
        pick = info["blocks"][str(block_id)]
    elif line is not None and int(line) in (info.get("lines") or {}):
        pick = info["lines"][int(line)]
    elif page is not None:
        for block_page, block in sorted((info.get("blocks") or {}).values(),
                                        key=lambda item: str(item[1].get("block_id"))):
            if block_page == int(page):
                pick = (block_page, block)
                break
    if pick is None and quote:
        # last resort: the quote itself identifies the block (needed when a source is
        # a PDF and therefore has no line numbers)
        needle = anchors.normalize(quote)[:60]
        for block_page, block in sorted((info.get("blocks") or {}).values(),
                                        key=lambda item: str(item[1].get("block_id"))):
            if needle and needle in anchors.normalize(str(block.get("text") or "")):
                pick = (block_page, block)
                break
    if pick is None:
        return None
    page_no, block = pick
    anchor = anchors.make_anchor(page_no, block,
                                 quote or str(block.get("text") or "")[:MAX_QUOTE])
    anchor["source"] = str(rel)
    anchor["line"] = block.get("line")
    return anchor


def verify_claims(claims: list[dict[str, Any]], analysis: dict[str, Any]) -> dict[str, Any]:
    """Verify claims file-by-file with lib/anchors, then aggregate.

    anchors.verify_all expects one document; our evidence spans many source files, so
    claims are grouped by source file and each group is verified against its own
    document. A claim whose source cannot be found is always ungrounded - that is the
    intended signal, not an error to swallow.
    """
    by_rel = {material["rel"]: material for material in analysis["materials"]}
    quality_rank = {"span": 0, "line": 1, "page": 2, "text": 3}
    used_qualities: list[str] = []
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, claim in enumerate(claims):
        groups.setdefault(str(claim.get("source") or ""), []).append((index, claim))
    per_claim: list[dict[str, Any] | None] = [None] * len(claims)
    grounded = ungrounded = 0
    for rel in sorted(groups, key=lambda key: min(item[0] for item in groups[key])):
        group = groups[rel]
        material = by_rel.get(rel)
        if material is None:
            for index, claim in group:
                claim.setdefault("flags", []).append("source_not_found")
                per_claim[index] = {
                    "claim_id": claim.get("claim_id"),
                    "kind": claim.get("kind") or "content",
                    "grounded": False, "located": False, "supported_by_quote": False,
                    "checks": [],
                }
            ungrounded += len(group)
            continue
        sub = anchors.verify_all(material["doc"], [claim for _index, claim in group])
        used_qualities.append(str(material.get("anchor_quality") or "text"))
        for (index, _claim), item in zip(group, sub["per_claim"]):
            per_claim[index] = item
        grounded += sub["grounded"]
        ungrounded += sub["ungrounded"]
    total = len(claims)
    rate = round(grounded / total, 4) if total else 0.0
    # report the *weakest* anchor quality that evidence actually came from: a mixed
    # corpus with one PDF must not claim span-level confidence for line-anchored text.
    quality = "text"
    if used_qualities:
        quality = sorted(used_qualities, key=lambda q: quality_rank.get(q, 3))[-1]
    ceiling = {"span": 1.0, "page": 0.7, "line": 0.7, "text": 0.4}.get(quality, 0.4)
    return {
        "claims_total": total,
        "grounded": grounded,
        "ungrounded": ungrounded,
        "grounding_rate": rate,
        "anchor_quality": quality,
        "confidence_ceiling": ceiling,
        "effective_confidence": round(min(rate, ceiling), 4),
        "per_claim": [item for item in per_claim if item is not None],
    }


def ground_records(records: list[dict[str, Any]], index: dict[str, Any],
                   analysis: dict[str, Any]) -> dict[str, Any]:
    """Attach anchors to records, verify them, and write the verdicts back."""
    claims: list[dict[str, Any]] = []
    claim_records: list[dict[str, Any]] = []
    for record in records:
        # A conclusion that no material supports has no evidence to verify: it is a
        # reported gap, not a claim. Counting it as "ungrounded claim" would dilute
        # the grounding rate and hide the real signal (the untraceable verdict).
        if str(record.get("verdict")) in {"untraceable", "source_missing"}:
            record["anchored"] = False
            record["anchor_verified"] = False
            record["anchor_checks"] = []
            continue
        anchors_list: list[dict[str, Any]] = []
        hint = record.get("anchor_hint") or {}
        anchor = None
        if record.get("source_path"):
            anchor = locate_anchor(
                index, str(record["source_path"]),
                page=hint.get("page") if isinstance(hint, dict) else None,
                block_id=record.get("block_id") if record.get("block_id") else
                (hint.get("block_id") if isinstance(hint, dict) else None),
                line=record.get("line"),
                quote=str(record.get("quote") or ""),
            )
        if anchor is not None:
            anchors_list.append(anchor)
        claims.append({
            "claim_id": record.get("claim_id"),
            "kind": record.get("kind") or "content",
            "claim": str(record.get("claim") or ""),
            "source": record.get("source_path"),
            "anchors": anchors_list,
        })
        claim_records.append(record)
    report = verify_claims(claims, analysis)
    by_id = {item.get("claim_id"): item for item in report["per_claim"]}
    for record in claim_records:
        item = by_id.get(record.get("claim_id")) or {}
        record["anchored"] = bool(item.get("located"))
        record["anchor_verified"] = bool(item.get("grounded"))
        record["anchor_checks"] = item.get("checks") or []
        if item.get("flags"):
            record["flags"] = sorted(set(record.get("flags") or []) | set(item["flags"]))
        quote = str(record.get("quote") or "")
        value = str(record.get("value") or "")
        if value and quote:
            # independent of anchors.verify: does the quote actually state the value?
            record["value_in_quote"] = bool(value.strip() in quote or value_in_text(value, quote))
        declared = record.get("declared_values") or []
        if declared:
            record["material_value_in_quote"] = bool(
                declared[0].get("quote") and
                value_in_text(str(declared[0].get("value") or ""), str(declared[0]["quote"])))
    report["claims_excluded_no_source"] = len(records) - len(claims)
    return report


# --------------------------------------------------------------------------- #
# value builders: steps / parameter table / datasets / settings / baselines
# --------------------------------------------------------------------------- #
def baseline_parameter_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in analysis["facts"]["params"][:48]:
        rows.append({
            "name": entry["name"], "value": entry["value"], "unit": entry["unit"],
            "target": "", "source_path": entry["source_path"], "line": entry["line"],
            "quote": entry["quote"], "trace_status": "traced", "agent_source": "",
            "source_verified": True, "note": "deterministic baseline row from material",
        })
    return rows


def verify_step_rows(steps: list[dict[str, Any]], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        material, line = resolve_source(step.get("source"), analysis)
        record = dict(step)
        record["order"] = index
        record["step_id"] = str(step.get("step_id") or ("S" + str(index)))
        record["source_path"] = material["rel"] if material is not None else None
        record["line"] = line
        record["quote"] = quote_around(line_text(material, line), "") if material is not None else ""
        record["trace_status"] = "source_located" if material is not None else "source_missing"
        record["fields_complete"] = bool(step.get("input") and step.get("operation")
                                         and step.get("output"))
        record["claim"] = ("step " + record["step_id"] + " is derived from " +
                           str(step.get("source") or "no declared source"))
        record["kind"] = "structural"
        verified.append(record)
    return verified


def baseline_steps(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    draft = analysis.get("draft") or {}
    steps: list[dict[str, Any]] = []
    for index, sentence in enumerate(draft.get("method_sentences") or [], start=1):
        steps.append({
            "step_id": "S" + str(index),
            "order": index,
            "input": "",
            "operation": str(sentence.get("text") or ""),
            "output": "",
            "condition": "",
            "source": str(draft.get("path") or "") + ":" + str(sentence.get("line") or ""),
            "note": "deterministic baseline: sentence extracted from the draft, "
                    "input/output/condition still need author input",
            "baseline": True,
            "source_path": draft.get("path"),
            "line": sentence.get("line"),
            "quote": str(sentence.get("text") or "")[:MAX_QUOTE],
            "trace_status": "source_located",
            "fields_complete": False,
            "claim": "draft paragraph at line " + str(sentence.get("line")) +
                     " contains this method sentence",
            "kind": "structural",
        })
    return steps[:24]


DATASET_NAME_RE = re.compile(r"([A-Z][A-Za-z0-9]*(?:[ -][A-Z0-9][A-Za-z0-9\-]*)*)")


def dataset_name_from_candidate(item: dict[str, Any]) -> str:
    if item.get("value"):
        return str(item["value"]).strip()
    text = str(item.get("text") or item.get("quote") or "")
    match = DATASET_NAME_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()[:60]


def baseline_dataset_rows(analysis: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in analysis["candidates"]["datasets"][:20]:
        rows.append({
            "name": dataset_name_from_candidate(item),
            "version": item.get("version") or "",
            "size": item.get("size") or "",
            "preprocessing": "",
            "split": item.get("split_ratio") or "",
            "split_method": "",
            "subsets": item.get("metrics") or [],
            "source_path": item.get("source_path"),
            "line": item.get("line"),
            "quote": item.get("quote") or "",
            "trace_status": "candidate",
            "note": "candidate from material text; preprocessing/split method not stated",
            "claim": "dataset row " + dataset_name_from_candidate(item) + " found in: " +
                     str(item.get("quote") or item.get("text") or "")[:100],
        })
    return rows


def verify_dataset_rows(rows: list[dict[str, Any]], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for row in rows:
        material, line = resolve_source(row.get("source"), analysis)
        record = dict(row)
        record["source_path"] = material["rel"] if material is not None else None
        record["line"] = line
        record["quote"] = (quote_around(line_text(material, line), "") if material is not None
                           else str(row.get("quote") or ""))
        complete = bool(row.get("name")) and bool(row.get("version") or row.get("split"))
        record["trace_status"] = ("traced" if material is not None and complete
                                  else ("partial" if material is not None else "untraceable"))
        record["claim"] = ("dataset entry " + str(row.get("name") or "") + ": " +
                           str(row.get("quote") or line_text(material, line) or
                               row.get("source") or "")[:110])
        verified.append(record)
    return verified


def baseline_baseline_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in analysis["candidates"]["baselines"][:16]:
        rows.append({
            "name": dataset_name_from_candidate(item),
            "version": item.get("version") or "",
            "budget": "",
            "metric": ", ".join(item.get("metrics") or []),
            "result": "",
            "source_path": item.get("source_path"),
            "line": item.get("line"),
            "quote": item.get("quote") or "",
            "trace_status": "candidate",
            "note": "baseline mention found in material; budget/metric result not stated",
            "claim": "baseline row " + dataset_name_from_candidate(item) + " found in: " +
                     str(item.get("quote") or item.get("text") or "")[:100],
        })
    return rows


def verify_baseline_rows(rows: list[dict[str, Any]], analysis: dict[str, Any]) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for row in rows:
        material, line = resolve_source(row.get("source"), analysis)
        record = dict(row)
        record["source_path"] = material["rel"] if material is not None else None
        record["line"] = line
        record["quote"] = (quote_around(line_text(material, line), "") if material is not None
                           else str(row.get("quote") or ""))
        has_budget = bool(str(row.get("budget") or "").strip())
        record["trace_status"] = ("traced" if material is not None and has_budget else
                                  ("partial" if material is not None else "untraceable"))
        record["claim"] = ("baseline " + str(row.get("name") or "") + ": " +
                           str(row.get("quote") or line_text(material, line) or
                               row.get("source") or "")[:110])
        verified.append(record)
    return verified


def build_settings(analysis: dict[str, Any], generated: dict[str, Any],
                   rules: dict[str, Any]) -> dict[str, Any]:
    """Merge the agent's settings with what the materials provably contain."""
    facts = analysis["facts"]
    seeds = [entry["value"] for entry in facts["params"] if entry["canonical"] == "seed"]
    repeats = [entry["value"] for entry in facts["params"] if entry["canonical"] == "repeats"]
    derived = {
        "hardware": [item["text"] for item in facts["hardware"]][:6],
        "software": [item["name"] + "==" + item["version"] for item in facts["versions"]][:10],
        "seeds": sorted(set(seeds)),
        "repeats": sorted(set(repeats)),
        "metrics": sorted({str(item["name"]) for item in facts["metrics"]})[:10],
        "commands": [item["command"] for item in facts["commands"]][:8],
    }
    agent = generated.get("settings") or {}
    merged = dict(derived)
    if isinstance(agent, dict) and agent:
        merged["agent_reported"] = agent
    traced = bool(derived["hardware"] or derived["software"] or derived["seeds"])
    merged["trace_status"] = "traced" if traced else "missing_from_materials"
    merged["claim"] = "experiment settings are derived from material files"
    return merged


def build_limitations(analysis: dict[str, Any], generated: dict[str, Any],
                      rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Limitations must point at a concrete failure scenario, not at a platitude."""
    rows: list[dict[str, Any]] = []
    for item in generated.get("limitations") or []:
        quality = limitation_specificity(item, rules)
        rows.append({
            "limitation": str(item.get("limitation") or "").strip(),
            "failure_scenario": str(item.get("failure_scenario") or "").strip(),
            "scope": str(item.get("scope") or "").strip(),
            "needs_author_confirmation": bool(item.get("needs_author_confirmation"))
            or quality in {"vague", "generic", "empty"},
            "quality": quality,
            "trace_status": "author_reported",
        })
    if rows:
        return rows
    facts = analysis["facts"]
    for anomaly in facts["anomalies"][:4]:
        rows.append({
            "limitation": "训练/评测过程中出现过异常记录，可能影响结果稳定性",
            "failure_scenario": anomaly["text"][:200],
            "scope": anomaly["source_path"] + ":" + str(anomaly["line"]),
            "needs_author_confirmation": True,
            "quality": "specific",
            "trace_status": "derived_from_log",
        })
    for conflict in analysis["param_conflicts"][:3]:
        rows.append({
            "limitation": "参数在不同材料中取值不一致，按当前配置无法唯一复现",
            "failure_scenario": "参数 " + conflict["name"] + " 出现多个取值：" +
                                ", ".join(conflict["values"]),
            "scope": "; ".join(item["path"] + ":" + str(item["line"])
                               for item in conflict["sources"][:3]),
            "needs_author_confirmation": True,
            "quality": "specific",
            "trace_status": "derived_from_conflict",
        })
    for element in ELEMENTS:
        packed = analysis["elements"].get(element) or {}
        if packed.get("covered"):
            continue
        rows.append({
            "limitation": "材料未覆盖「" + ELEMENT_LABELS.get(element, element) + "」，相关表述需作者确认",
            "failure_scenario": "读者按现有材料复现时会缺少" +
                                ELEMENT_LABELS.get(element, element) + "，无法判断结果是否可重复",
            "scope": "material coverage",
            "needs_author_confirmation": True,
            "quality": "specific",
            "trace_status": "derived_from_gap",
        })
    if not rows:
        rows.append({
            "limitation": GENERIC_LIMITATION_FALLBACK,
            "failure_scenario": "",
            "scope": "",
            "needs_author_confirmation": True,
            "quality": "empty",
            "trace_status": "placeholder",
        })
    return rows


def build_traceability(analysis: dict[str, Any], values: dict[str, Any],
                       coverage: dict[str, Any], section_analysis: dict[str, Any]
                       ) -> list[dict[str, Any]]:
    """One record per conclusion that needs a source, plus the ones that lack one."""
    records: list[dict[str, Any]] = []
    counter = 0

    def next_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return prefix + "-" + str(counter).zfill(4)

    for mention in (analysis.get("draft") or {}).get("mentions") or []:
        verdict = mention.get("verdict") or "untraceable"
        severity = mention.get("severity") or "low"
        records.append({
            "claim_id": next_id("trace-num"),
            "kind": "content",
            "claim": ("draft states " + str(mention.get("value_raw")) + " in: " +
                      str(mention.get("sentence") or mention.get("quote") or "")[:110]),
            "verdict": verdict,
            "severity": severity if verdict != "traced_number" else "info",
            "source_path": mention.get("source"),
            "page": mention.get("page"),
            "block_id": mention.get("block_id"),
            "line": mention.get("line"),
            "quote": mention.get("quote") or "",
            "value": mention.get("value_raw"),
            "material_match": mention.get("material_match"),
            "declared_values": mention.get("declared_values") or [],
            "category": "draft_number",
        })

    for row in values.get("parameter_table") or []:
        if row.get("trace_status") == "untraceable":
            continue
        records.append({
            "claim_id": next_id("trace-param"),
            "kind": "content",
            "claim": "parameter " + str(row.get("name")) + " = " + str(row.get("value")) +
                     " is specified in the materials",
            "verdict": row.get("trace_status"),
            "severity": "high" if row.get("trace_status") == "mismatch" else "info",
            "source_path": row.get("source_path"),
            "page": None,
            "block_id": None,
            "line": row.get("line"),
            "quote": row.get("quote") or "",
            "value": row.get("value"),
            "declared_values": row.get("declared_values") or [],
            "category": "parameter",
        })

    for step in values.get("methods_steps") or []:
        records.append({
            "claim_id": next_id("trace-step"),
            "kind": "structural",
            "claim": str(step.get("claim") or "step source"),
            "verdict": step.get("trace_status"),
            "severity": "high" if step.get("trace_status") != "source_located" else "info",
            "source_path": step.get("source_path"),
            "page": None,
            "block_id": None,
            "line": step.get("line"),
            "quote": step.get("quote") or "",
            "category": "step",
        })

    for key, category in (("datasets", "dataset"), ("baselines", "baseline")):
        for row in values.get(key) or []:
            records.append({
                "claim_id": next_id("trace-" + category),
                "kind": "content",
                "claim": str(row.get("claim") or category + " source"),
                "verdict": row.get("trace_status"),
                "severity": "high" if row.get("trace_status") == "untraceable" else "info",
                "source_path": row.get("source_path"),
                "page": None,
                "block_id": None,
                "line": row.get("line"),
                "quote": row.get("quote") or "",
                "category": category,
            })

    for segment in (analysis.get("draft") or {}).get("segments") or []:
        roles = (coverage.get("roles") or {}).get(segment.get("seg_id")) or []
        if not roles or roles == ["none"]:
            continue
        records.append({
            "claim_id": next_id("trace-seg"),
            "kind": "structural",
            "claim": "draft segment " + str(segment.get("seg_id")) + " maps to role(s) " +
                     ", ".join(roles),
            "verdict": "role_assigned",
            "severity": "info",
            "source_path": (analysis.get("draft") or {}).get("path"),
            "page": segment.get("page"),
            "block_id": (segment.get("block_ids") or [None])[0],
            "line": _segment_line(segment),
            "quote": str(segment.get("text") or "")[:MAX_QUOTE],
            "category": "segment_role",
        })

    for record in section_analysis.get("untraceable") or []:
        records.append({
            "claim_id": next_id("trace-unsourced"),
            "kind": "content",
            "claim": "generated chapter states " + str(record.get("value_raw")) +
                     " which no material supports",
            "verdict": "untraceable",
            "severity": "high",
            "source_path": None,
            "page": None,
            "block_id": None,
            "line": None,
            "quote": record.get("sentence") or "",
            "value": record.get("value_raw"),
            "category": "generated_unsourced",
        })

    priority = {"mismatch": 0, "untraceable": 1, "unit_mismatch": 2, "source_missing": 3,
                "partial": 4, "traced": 5, "traced_number": 5, "match": 5,
                "source_located": 5, "role_assigned": 6, "candidate": 6}
    records.sort(key=lambda item: (priority.get(str(item.get("verdict")), 4),
                                   str(item.get("category")), item.get("claim_id") or ""))
    return records[:220]


def summarise_quality(analysis: dict[str, Any], values: dict[str, Any],
                      grounding: dict[str, Any], coverage: dict[str, Any],
                      section_analysis: dict[str, Any], generated_present: bool,
                      stopwatch: hybrid.Stopwatch) -> dict[str, Any]:
    """Machine-checked metrics. No timing here: result.json must be reproducible,
    so elapsed time lives in run_report.json only."""
    params = values.get("parameter_table") or []
    steps = values.get("methods_steps") or []
    datasets = values.get("datasets") or []
    baselines = values.get("baselines") or []
    repro = values.get("reproducibility_checklist") or []
    limitations = values.get("limitations") or []
    mentions = (analysis.get("draft") or {}).get("mentions") or []

    delivered = {
        "steps": bool(steps) and any(step.get("fields_complete") for step in steps),
        "settings": (values.get("experimental_settings") or {}).get("trace_status") == "traced",
        "parameters": bool(params) and any(row.get("trace_status") == "traced" for row in params),
        "datasets": bool(datasets) and any(row.get("trace_status") in {"traced", "partial"}
                                           or row.get("version") or row.get("split")
                                           for row in datasets),
        "baselines": bool(baselines),
        "repro_and_limitations": (sum(1 for item in repro if item.get("status") != "missing") >= 5
                                  and any(item.get("quality") == "specific"
                                          for item in limitations)),
    }
    ratio = round(sum(1 for value in delivered.values() if value) / len(delivered), 4)
    trace_counts: dict[str, int] = {}
    for row in params:
        key = str(row.get("trace_status"))
        trace_counts[key] = trace_counts.get(key, 0) + 1
    verdicts: dict[str, int] = {}
    for mention in mentions:
        key = str(mention.get("verdict"))
        verdicts[key] = verdicts.get(key, 0) + 1
    high_untraceable = sum(1 for mention in mentions
                           if mention.get("verdict") == "untraceable"
                           and mention.get("severity") == "high")
    return {
        "status_generated_content": generated_present,
        "delivered_six_element_coverage": delivered,
        "delivered_coverage_ratio": ratio,
        "material_element_coverage": {
            element: bool((analysis["elements"].get(element) or {}).get("covered"))
            for element in ELEMENTS},
        "material_coverage_ratio": round(
            sum(1 for element in ELEMENTS
                if (analysis["elements"].get(element) or {}).get("covered")) / len(ELEMENTS), 4),
        "step_count": len(steps),
        "steps_with_source": sum(1 for step in steps
                                 if step.get("trace_status") == "source_located"),
        "steps_with_complete_fields": sum(1 for step in steps if step.get("fields_complete")),
        "parameter_count": len(params),
        "parameter_trace_counts": dict(sorted(trace_counts.items())),
        "draft_number_mentions": len(mentions),
        "draft_number_verdicts": dict(sorted(verdicts.items())),
        "draft_number_untraceable_high": high_untraceable,
        "draft_number_unverifiable": sum(1 for mention in mentions
                                         if mention.get("verdict") == "unverifiable"),
        "generated_section_numbers": section_analysis.get("numbers_total", 0),
        "generated_section_untraceable": len(section_analysis.get("untraceable") or []),
        "generated_section_mismatched": len(section_analysis.get("mismatched") or []),
        "dataset_count": len(datasets),
        "baseline_count": len(baselines),
        "repro_items": len(repro),
        "repro_verified": sum(1 for item in repro if item.get("status") == "verified"),
        "repro_missing": sum(1 for item in repro if item.get("status") == "missing"),
        "limitation_count": len(limitations),
        "limitations_specific": sum(1 for item in limitations
                                    if item.get("quality") == "specific"),
        "limitations_generic": sum(1 for item in limitations
                                   if item.get("quality") in {"generic", "vague", "empty"}),
        "batch_coverage": coverage.get("coverage") if coverage.get("applicable") else None,
        "grounding_rate": grounding.get("grounding_rate"),
        "effective_confidence": grounding.get("effective_confidence"),
        "anchor_quality": grounding.get("anchor_quality"),
        "materials_scanned": len(analysis["materials"]),
    }


# --------------------------------------------------------------------------- #
# assemble every produced value, then decide the honest status
# --------------------------------------------------------------------------- #
def build_values(analysis: dict[str, Any], generated_info: dict[str, Any],
                 coverage: dict[str, Any], index: dict[str, Any],
                 rules: dict[str, Any], stopwatch: hybrid.Stopwatch) -> dict[str, Any]:
    generated_raw = generated_info.get("methods")
    generated = normalise_generated(generated_raw, rules) if generated_raw else None
    generated_present = bool(generated) and any([
        generated.get("steps"), generated.get("parameter_table"),
        generated.get("datasets"), generated.get("baselines"),
        generated.get("limitations"), str(generated.get("methods_section") or "").strip(),
    ])
    limits: list[str] = []
    uncertainty: list[str] = []
    change_log: list[str] = []

    # ---- steps ----
    if generated_present and generated.get("steps"):
        steps = verify_step_rows(generated["steps"], analysis)
        change_log.append("steps: adopted " + str(len(steps)) + " agent-declared step(s) and "
                          "verified their source locators")
    else:
        steps = baseline_steps(analysis)
        if steps:
            limits.append("steps: no agent-generated procedure; deterministic baseline extracted "
                          + str(len(steps)) + " method sentence(s) from the draft "
                          "(input/output/condition still empty)")
    # ---- parameter table ----
    if generated_present and generated.get("parameter_table"):
        params = verify_parameter_rows(generated["parameter_table"], analysis, rules)
        change_log.append("parameters: verified " + str(len(params)) +
                          " agent-declared row(s) against config/log/code")
    else:
        params = baseline_parameter_rows(analysis)
        if params:
            limits.append("parameters: no agent-generated table; baseline built from "
                          + str(len(params)) + " material fact(s) with file+line provenance")
    for row in params:
        if row.get("trace_status") == "untraceable":
            uncertainty.append("parameter without a material source: " + str(row.get("name")) +
                               "=" + str(row.get("value")))
        elif row.get("trace_status") == "mismatch":
            uncertainty.append("parameter value conflicts with the materials: " +
                               str(row.get("name")) + "=" + str(row.get("value")))
    # ---- datasets / baselines ----
    if generated_present and generated.get("datasets"):
        datasets = verify_dataset_rows(generated["datasets"], analysis)
    else:
        datasets = baseline_dataset_rows(analysis, rules)
        if datasets:
            limits.append("datasets: candidate rows extracted from material text; version/split "
                          "fields are only filled when the material states them")
    if generated_present and generated.get("baselines"):
        baselines = verify_baseline_rows(generated["baselines"], analysis)
    else:
        baselines = baseline_baseline_rows(analysis)
        if not baselines:
            limits.append("baselines: no baseline/control mention found in the materials")
    for row in datasets + baselines:
        if row.get("trace_status") == "untraceable":
            uncertainty.append("dataset/baseline claim without a resolvable source: " +
                               str(row.get("name")))
    # ---- settings / reproducibility / limitations ----
    settings = build_settings(analysis, generated or {}, rules)
    if settings.get("trace_status") != "traced":
        limits.append("settings: no hardware/software/seed fact could be traced in the materials")
    repro = build_repro_checklist(analysis, generated or {}, rules)
    for item in repro:
        if item.get("status") == "missing":
            uncertainty.append("reproduction item missing from materials: " + item["item"])
    limitations = build_limitations(analysis, generated or {}, rules)

    # ---- the chapter text ----
    generated_section = str((generated or {}).get("methods_section") or "").strip()
    baseline_used = not generated_section
    if baseline_used:
        section = compose_baseline_section(analysis)
        limits.append("chapter: no agent-generated methods_section; a deterministic baseline "
                      "was assembled from the draft and the materials")
    else:
        section = generated_section
    section_analysis = analyse_generated_section("" if baseline_used else generated_section,
                                                 analysis)
    for record in section_analysis.get("untraceable") or []:
        uncertainty.append("generated chapter states " + str(record.get("value_raw")) +
                           " with no material source (context: " +
                           str(record.get("sentence") or "")[:80] + ")")
    for record in section_analysis.get("mismatched") or []:
        declared = record.get("declared_values") or [{}]
        uncertainty.append("generated chapter value conflicts with materials: " +
                           str(record.get("value_raw")) + " vs " +
                           str(declared[0].get("value")) + " at " +
                           str(declared[0].get("source_path")))

    # ---- questions ----
    questions = list(analysis["gaps_questions"]["questions"])
    for item in (generated or {}).get("questions") or []:
        questions.append({"question": str(item.get("question") or ""),
                          "why": str(item.get("why") or ""),
                          "severity": str(item.get("severity") or "advisory"),
                          "evidence": item.get("evidence") or [],
                          "origin": "agent"})
    for row in params:
        if row.get("trace_status") == "untraceable":
            questions.append({
                "question": "参数 " + str(row.get("name")) + "=" + str(row.get("value")) +
                            " 在材料中找不到来源，请提供来源或删除该行。",
                "why": "参数表必须逐行可追溯，否则复现会失败。",
                "severity": "high", "evidence": [], "origin": "verification"})
        elif row.get("trace_status") == "mismatch":
            declared = (row.get("declared_values") or [{}])[0]
            questions.append({
                "question": "参数 " + str(row.get("name")) + " 在章节中写作 " + str(row.get("value")) +
                            "，材料为 " + str(declared.get("value")) + "，应以哪个为准？",
                "why": "这是正文与配置不一致的典型冲突。",
                "severity": "high",
                "evidence": [{"text": declared.get("quote") or "",
                              "source_path": declared.get("source_path"),
                              "line": declared.get("line")}], "origin": "verification"})
    for record in section_analysis.get("untraceable") or []:
        questions.append({
            "question": "章节中的数值 " + str(record.get("value_raw")) +
                        " 在材料中无来源，请提供来源或删除。",
            "why": "生成内容不得为了句子顺畅补数值。",
            "severity": "high",
            "evidence": [{"text": record.get("sentence") or "", "source_path": None,
                          "line": None}], "origin": "verification"})
    for record in section_analysis.get("mismatched") or []:
        declared = (record.get("declared_values") or [{}])[0]
        questions.append({
            "question": "章节中 " + str(record.get("bound_param") or "该参数") + " 写作 " +
                        str(record.get("value_raw")) + "，材料为 " +
                        str(declared.get("value")) + "，请确认。",
            "why": "生成章节与配置/日志冲突，属于必须回源确认的数值。",
            "severity": "high",
            "evidence": [{"text": record.get("sentence") or "",
                          "source_path": declared.get("source_path"),
                          "line": declared.get("line")}], "origin": "verification"})
    severity_rank = {"blocking": 0, "high": 1, "medium": 2, "advisory": 3}
    questions.sort(key=lambda item: (severity_rank.get(str(item.get("severity")), 3),
                                     str(item.get("question"))))
    for seq, item in enumerate(questions, start=1):
        item["question_id"] = "Q" + str(seq)
        item.setdefault("origin", "materials")

    # ---- traceability + anchors ----
    values: dict[str, Any] = {
        "methods_section": section,
        "methods_steps": steps,
        "parameter_table": params,
        "datasets": datasets,
        "experimental_settings": settings,
        "baselines": baselines,
        "reproducibility_checklist": repro,
        "limitations": limitations,
        "questions": questions,
    }
    records = build_traceability(analysis, values, coverage, section_analysis)
    grounding = ground_records(records, index, analysis)
    values["traceability"] = records

    # ---- quality + status ----
    quality = summarise_quality(analysis, values, grounding, coverage, section_analysis,
                                generated_present, stopwatch)
    values["quality"] = quality
    change_log.append("evidence: " + str(grounding["grounded"]) + "/" +
                      str(grounding["claims_total"]) + " claim(s) re-verified against the "
                      "source documents (grounding_rate=" +
                      str(grounding["grounding_rate"]) + ")")

    if analysis["materials"] and not analysis.get("cross_check_available", True):
        limits.append("no independent config/log/code material was supplied; draft numbers "
                      "could only be located inside the draft itself, so they are reported "
                      "as unverifiable instead of untraceable")
    status = "success"
    if not analysis["materials"]:
        status = "failed"
        limits.append("no material files were found under the input roots; nothing could be "
                      "extracted or verified")
    else:
        if not generated_present:
            status = "partial"
            limits.append("no agent-generated content was found under work/generated; the "
                          "deliverable is the deterministic baseline and must be reviewed")
        if coverage.get("applicable") and coverage.get("coverage", 1.0) < 1.0:
            status = "partial"
            limits.append("batch coverage " + str(coverage.get("coverage")) +
                          ": " + str(len(coverage.get("missing") or [])) +
                          " draft segment(s) have no agent answer")
        if quality["generated_section_untraceable"]:
            status = "partial"
            limits.append(str(quality["generated_section_untraceable"]) +
                          " number(s) in the generated chapter have no material source")
        if quality.get("generated_section_mismatched"):
            status = "partial"
            limits.append(str(quality["generated_section_mismatched"]) +
                          " number(s) in the generated chapter conflict with the materials")
        if quality["draft_number_untraceable_high"]:
            status = "partial"
            limits.append(str(quality["draft_number_untraceable_high"]) +
                          " high-severity draft number(s) are untraceable in the materials")
        if any(row.get("trace_status") == "mismatch" for row in params):
            status = "partial"
            limits.append("at least one parameter conflicts with the material value")
        if any(item.get("verdict") == "mismatch"
               for item in (analysis.get("draft") or {}).get("mentions") or []):
            status = "partial"
            limits.append("the draft disagrees with the materials on at least one value; "
                          "see questions for the author confirmation request")
        if quality["delivered_coverage_ratio"] < 1.0:
            status = "partial"
            missing = [key for key, covered in
                       quality["delivered_six_element_coverage"].items() if not covered]
            limits.append("elements still incomplete: " + ", ".join(missing))
        if grounding["grounding_rate"] < 0.6:
            limits.append("grounding_rate is low (" + str(grounding["grounding_rate"]) +
                          "); treat the anchors as line-level only")
    for material in analysis["material_summary"]:
        if material["warnings"]:
            uncertainty.extend(str(warning) for warning in material["warnings"][:2])
    return {
        "values": values,
        "status": status,
        "limits": limits,
        "uncertainty": uncertainty,
        "change_log": change_log,
        "grounding": grounding,
        "metrics": quality,
        "coverage": coverage,
        "section_analysis": section_analysis,
        "generated_present": generated_present,
        "baseline_used": baseline_used,
    }


# --------------------------------------------------------------------------- #
# editable deliverable: docx + markdown + html, plus tabular side files
# --------------------------------------------------------------------------- #
MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
MD_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
MD_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def md_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Tiny markdown -> docwrite block converter (headings, bullets, paragraphs)."""
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append({"type": "para", "text": " ".join(paragraph).strip()})
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            blocks.append({"type": "list", "items": list(bullets)})
            bullets.clear()

    for raw in str(markdown or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_bullets()
            continue
        heading = MD_HEADING.match(stripped)
        if heading:
            flush_paragraph()
            flush_bullets()
            blocks.append({"type": "heading", "level": min(len(heading.group(1)), 3),
                           "text": heading.group(2).strip()})
            continue
        bullet = MD_BULLET.match(line) or MD_ORDERED.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group(1).strip())
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            flush_bullets()
            blocks.append({"type": "para", "text": stripped.lstrip(">").strip()})
            continue
        flush_bullets()
        paragraph.append(stripped)
    flush_paragraph()
    flush_bullets()
    return blocks


def _table(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    return {"type": "table", "header": True, "rows": [headers] + rows}


def build_doc_blocks(values: dict[str, Any]) -> list[dict[str, Any]]:
    """The reviewer-facing deliverable, in reading order."""
    blocks: list[dict[str, Any]] = [
        {"type": "title", "text": "Methods and Experiments"},
        {"type": "para", "text": "Generated from the supplied materials; every value is "
                                 "traceable (see the traceability section). Unconfirmed items "
                                 "are listed at the end and must not be treated as settled."},
    ]
    blocks.extend(md_to_blocks(values.get("methods_section") or ""))
    steps = values.get("methods_steps") or []
    if steps:
        blocks.append({"type": "heading", "level": 1, "text": "Steps"})
        blocks.append(_table(
            ["#", "Input", "Operation", "Output", "Condition", "Source"],
            [[str(step.get("step_id")), str(step.get("input")), str(step.get("operation")),
              str(step.get("output")), str(step.get("condition")),
              str(step.get("source") or step.get("source_path") or "NOT TRACED")]
             for step in steps[:30]]))
    params = values.get("parameter_table") or []
    if params:
        blocks.append({"type": "heading", "level": 1, "text": "Parameter table"})
        blocks.append(_table(
            ["Name", "Value", "Unit", "Target", "Source", "Status"],
            [[str(row.get("name")), str(row.get("value")), str(row.get("unit")),
              str(row.get("target")),
              (str(row.get("source_path") or "") + (":" + str(row.get("line"))
                                                    if row.get("line") else "")),
              str(row.get("trace_status"))] for row in params[:48]]))
    datasets = values.get("datasets") or []
    if datasets:
        blocks.append({"type": "heading", "level": 1, "text": "Datasets"})
        blocks.append(_table(
            ["Name", "Version", "Size", "Split", "Split method", "Preprocessing", "Source"],
            [[str(row.get("name")), str(row.get("version")), str(row.get("size")),
              str(row.get("split")), str(row.get("split_method")),
              str(row.get("preprocessing")),
              str(row.get("source_path") or "")] for row in datasets[:20]]))
    settings = values.get("experimental_settings") or {}
    if settings:
        blocks.append({"type": "heading", "level": 1, "text": "Experimental settings"})
        items = []
        for key in ("hardware", "software", "seeds", "repeats", "metrics", "commands"):
            value = settings.get(key)
            if isinstance(value, list) and value:
                items.append(key + ": " + "; ".join(str(item) for item in value))
            elif isinstance(value, str) and value:
                items.append(key + ": " + value)
        if isinstance(settings.get("agent_reported"), dict):
            for key, value in sorted(settings["agent_reported"].items()):
                items.append(str(key) + ": " + str(value))
        blocks.append({"type": "list", "items": items or ["no settings traced"]})
    baselines = values.get("baselines") or []
    if baselines:
        blocks.append({"type": "heading", "level": 1, "text": "Baselines"})
        blocks.append(_table(
            ["Name", "Version", "Budget", "Metric", "Result", "Source", "Status"],
            [[str(row.get("name")), str(row.get("version")), str(row.get("budget")),
              str(row.get("metric")), str(row.get("result")),
              str(row.get("source_path") or ""), str(row.get("trace_status"))]
             for row in baselines[:16]]))
    repro = values.get("reproducibility_checklist") or []
    if repro:
        blocks.append({"type": "heading", "level": 1, "text": "Reproducibility checklist"})
        blocks.append(_table(["Item", "Status", "Detail"],
                             [[str(row.get("item")), str(row.get("status")),
                               str(row.get("detail"))] for row in repro]))
    limitations = values.get("limitations") or []
    if limitations:
        blocks.append({"type": "heading", "level": 1, "text": "Limitations"})
        blocks.append({"type": "list", "items": [
            str(row.get("limitation")) + " | failure scenario: " +
            str(row.get("failure_scenario") or "NOT SPECIFIED") +
            (" | needs author confirmation" if row.get("needs_author_confirmation") else "")
            for row in limitations[:20]]})
    questions = values.get("questions") or []
    if questions:
        blocks.append({"type": "heading", "level": 1, "text": "Author confirmation needed"})
        blocks.append({"type": "list", "items": [
            "[" + str(item.get("severity")) + "] " + str(item.get("question")) +
            (" (why: " + str(item.get("why")) + ")" if item.get("why") else "")
            for item in questions[:30]]})
    trace = values.get("traceability") or []
    problematic = [item for item in trace if item.get("verdict") in
                   {"mismatch", "untraceable", "unit_mismatch", "source_missing"}]
    if problematic:
        blocks.append({"type": "heading", "level": 1, "text": "Untraced or conflicting claims"})
        blocks.append({"type": "list", "items": [
            str(item.get("verdict")) + " | " + str(item.get("claim")) +
            " | source: " + str(item.get("source_path") or "none") +
            (":" + str(item.get("line")) if item.get("line") else "")
            for item in problematic[:30]]})
    quality = values.get("quality") or {}
    if quality:
        blocks.append({"type": "heading", "level": 1, "text": "Self-check metrics"})
        flat = [(key, value) for key, value in quality.items()
                if isinstance(value, (str, int, float, bool))]
        blocks.append(_table(["Metric", "Value"],
                             [[str(key), str(value)] for key, value in flat]))
    return blocks


def write_artifacts(out_dir: Path, values: dict[str, Any], stopwatch: hybrid.Stopwatch) -> dict[str, Any]:
    """Write the editable deliverable + a CSV + the author-question list + preview."""
    import csv

    out = Path(out_dir)
    blocks = build_doc_blocks(values)
    editable = docwrite.write_editable(out, "methods_section", blocks,
                                      title="Methods and Experiments")
    rendering: dict[str, Any] = {"attempted": False,
                                 "reason": "renderer not attempted (budget)"}
    if stopwatch.remaining() > 200:
        try:
            rendering = render.render(editable["docx"], out)
        except Exception as exc:  # noqa: BLE001 - rendering is optional polish
            rendering = {"attempted": True, "ok": False,
                         "reason": type(exc).__name__ + ": " + str(exc)}
        rendering["attempted"] = True
    else:
        rendering["reason"] = "skipped: time budget too tight for office conversion"

    csv_path = out / "parameter_table.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "value", "unit", "target", "source_path", "line",
                         "trace_status", "note"])
        for row in values.get("parameter_table") or []:
            writer.writerow([row.get("name"), row.get("value"), row.get("unit"),
                             row.get("target"), row.get("source_path"), row.get("line"),
                             row.get("trace_status"), row.get("note")])
    questions_path = out / "author_questions.md"
    lines = ["# 待作者确认清单", ""]
    for item in values.get("questions") or []:
        lines.append("- [" + str(item.get("severity")) + "] " + str(item.get("question")))
        if item.get("why"):
            lines.append("  - why: " + str(item.get("why")))
        for evidence in item.get("evidence") or []:
            lines.append("  - evidence: " + str(evidence.get("source_path")) + ":" +
                         str(evidence.get("line")) + " " + str(evidence.get("text"))[:160])
    questions_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return {
        "editable": editable,
        "parameter_csv": str(csv_path),
        "questions_md": str(questions_path),
        "preview_html": editable["html"],
        "rendering": rendering,
        "blocks": len(blocks),
    }


# --------------------------------------------------------------------------- #
# contract field synonyms: the hidden case names its own fields, so every internal
# value declares the surface forms it can reasonably answer to.
# --------------------------------------------------------------------------- #
FIELD_SYNONYMS: dict[str, list[str]] = {
    "methods_section": ["methods_chapter", "method_section", "methods_text", "methods",
                        "chapter", "section_text", "draft", "manuscript", "writeup",
                        "chapter_body", "chapter_text", "方法与实验", "方法", "正文", "章节"],
    "methods_steps": ["steps", "procedure", "procedures", "workflow", "process",
                      "process_steps", "step_list", "method_steps", "steps_list",
                      "步骤", "流程"],
    "parameter_table": ["parameters", "params", "hyperparameters", "param_table",
                        "hyperparameter_table", "parameters_table", "config_table",
                        "参数表", "参数"],
    "datasets": ["dataset", "data_assets", "dataset_table", "datasets_table", "data_card",
                 "data", "数据集", "数据"],
    "experimental_settings": ["settings", "experiment_settings", "experimental_setup",
                              "setup", "environment", "run_environment", "experiment_setup",
                              "实验设置", "运行环境"],
    "baselines": ["baseline", "baseline_table", "baseline_rows", "comparisons",
                  "comparison_baselines", "controls", "基线", "对照"],
    "reproducibility_checklist": ["reproducibility", "repro_manifest", "reproduction_manifest",
                                  "reproduction_checklist", "repro_checklist",
                                  "reproducibility_checklist_report", "复现清单", "复现"],
    "traceability": ["source_trace", "fact_source_map", "provenance", "sources",
                     "source_map", "number_provenance", "evidence", "trace",
                     "来源映射", "事实与来源", "追溯"],
    "limitations": ["limitation", "caveats", "scope_caveats", "boundaries", "risks",
                    "限制条件", "限制"],
    "quality": ["quality_report", "self_check", "self_audit", "metrics", "qa",
                "quality_metrics", "自检", "质量"],
    "questions": ["open_questions", "author_questions", "author_followups",
                  "needs_confirmation", "gaps", "followups", "待确认", "追问清单", "追问"],
    "change_log": ["changes", "edits", "modifications", "revision_log", "修改记录"],
}

ADDITIONAL_KEYS = ["methods_section", "methods_steps", "parameter_table", "datasets",
                   "experimental_settings", "baselines", "reproducibility_checklist",
                   "traceability", "limitations", "quality", "questions", "change_log"]


# --------------------------------------------------------------------------- #
# prepare stage
# --------------------------------------------------------------------------- #
def build_template() -> dict[str, Any]:
    """The worksheet shape for work/generated/methods.json (mirrored in SKILL.md)."""
    return {
        "_doc": [
            "Copy this file to work/generated/methods.json, fill the fields from the",
            "materials, then run: python scripts/run_methods.py --finalize --out /app/output",
            "Use 'path:line' locators for source, e.g. environment/configs/train.yaml:12.",
            "Never invent a value: if a number is not in the materials, leave it out and",
            "add a question instead.",
        ],
        "steps": [{"step_id": "S1", "input": "", "operation": "", "output": "",
                   "condition": "", "source": "environment/path/to/source.md:12"}],
        "parameter_table": [{"name": "", "value": "", "unit": "", "target": "",
                             "source": "environment/configs/train.yaml:12"}],
        "datasets": [{"name": "", "version": "", "size": "", "preprocessing": "",
                      "split": "", "split_method": "", "subsets": [],
                      "source": "environment/data/README.md:5"}],
        "settings": {"hardware": "", "software": "", "seeds": "", "repeats": "",
                     "metrics": ""},
        "baselines": [{"name": "", "version": "", "budget": "", "metric": "",
                       "result": "", "source": ""}],
        "reproducibility": {"code_version": "", "environment": "", "commands": [],
                            "seeds": []},
        "limitations": [{"limitation": "", "failure_scenario": "", "scope": "",
                         "needs_author_confirmation": True}],
        "methods_section": "## Methods\n(rewrite here; every number must come from the materials)",
        "questions": [{"question": "", "why": "", "severity": "advisory"}],
    }


def prepare_stage(ctx: dict[str, Any]) -> dict[str, Any]:
    """Deterministic extraction + agent worksheets. Writes nothing to result.json."""
    analysis = analyze(ctx["found"], ctx["rules"], ctx["stopwatch"],
                       exclude=[ctx["out_dir"], ctx["work_dir"]])
    ctx["analysis"] = analysis
    work = Path(ctx["work_dir"])
    draft = analysis["draft"]
    batches: list[dict[str, Any]] = []
    if draft.get("present") and draft.get("segments"):
        batches = hybrid.make_batches(draft["segments"], work, batch_size=6, batch_chars=6000)
    worksheet = {
        "generated_by": "prepare",
        "topic": TOPIC,
        "draft": {
            "path": draft.get("path"),
            "segment_count": len(draft.get("segments") or []),
            "section_count": len(draft.get("sections") or []),
            "mention_count": len(draft.get("mentions") or []),
        },
        "materials": analysis["material_summary"],
        "params": analysis["facts"]["params"],
        "param_conflicts": analysis["param_conflicts"],
        "draft_mentions": draft.get("mentions") or [],
        "facts": {
            "metrics": analysis["facts"]["metrics"],
            "versions": analysis["facts"]["versions"],
            "hardware": analysis["facts"]["hardware"],
            "commands": analysis["facts"]["commands"],
            "anomalies": analysis["facts"]["anomalies"],
        },
        "candidates": analysis["candidates"],
        "elements": analysis["elements"],
        "gaps": analysis["gaps_questions"]["gaps"],
        "questions": analysis["gaps_questions"]["questions"],
        "batches": [{"batch_id": item["batch_id"], "segment_count": item["segment_count"],
                     "worksheet": item["worksheet"]} for item in batches],
    }
    hybrid.write_json(work / "params_candidates.json", worksheet)
    hybrid.write_json(work / "draft_segments.json", {
        "generated_by": "prepare",
        "draft_path": draft.get("path"),
        "sections": draft.get("sections") or [],
        "segments": [{key: segment.get(key) for key in
                      ("seg_id", "order", "page", "block_ids", "section_title", "text")}
                     for segment in draft.get("segments") or []],
    })
    hybrid.write_json(work / "gaps.json", analysis["gaps_questions"])
    hybrid.write_json(work / "generated" / "_template_methods.json", build_template())
    return {
        "work_dir": str(work),
        "materials": len(analysis["materials"]),
        "draft": draft.get("path"),
        "segments": len(draft.get("segments") or []),
        "params": len(analysis["facts"]["params"]),
        "param_conflicts": len(analysis["param_conflicts"]),
        "draft_mentions": len(draft.get("mentions") or []),
        "batches": [item["batch_id"] for item in batches],
        "gaps": len(analysis["gaps_questions"]["gaps"]),
        "questions": len(analysis["gaps_questions"]["questions"]),
    }


# --------------------------------------------------------------------------- #
# finalize stage
# --------------------------------------------------------------------------- #
def finalize_stage(ctx: dict[str, Any]) -> dict[str, Any]:
    """Verify, degrade honestly when generated content is missing, reflect the contract."""
    if "analysis" not in ctx:
        ctx["analysis"] = analyze(ctx["found"], ctx["rules"], ctx["stopwatch"],
                                  exclude=[ctx["out_dir"], ctx["work_dir"]])
    analysis = ctx["analysis"]
    work = Path(ctx["work_dir"])
    generated_info = read_generated(work)
    coverage = merge_batch_coverage(analysis, generated_info["batch_records"])
    index = build_block_index(analysis)
    built = build_values(analysis, generated_info, coverage, index, ctx["rules"],
                         ctx["stopwatch"])
    values = built["values"]
    values["change_log"] = built["change_log"]
    artifacts = write_artifacts(ctx["out_dir"], values, ctx["stopwatch"])

    out_dir = Path(ctx["out_dir"])
    evidence = {
        "grounding": built["grounding"],
        "traceability": values.get("traceability") or [],
        "materials": analysis["material_summary"],
        "draft": {"path": (analysis.get("draft") or {}).get("path"),
                  "segments": len((analysis.get("draft") or {}).get("segments") or []),
                  "mentions": len((analysis.get("draft") or {}).get("mentions") or [])},
        "facts": {"params": len(analysis["facts"]["params"]),
                  "versions": len(analysis["facts"]["versions"]),
                  "commands": len(analysis["facts"]["commands"]),
                  "anomalies": len(analysis["facts"]["anomalies"])},
        "param_conflicts": analysis["param_conflicts"],
        "generated": {"present": built["generated_present"],
                      "baseline_used": built["baseline_used"],
                      "files": generated_info["files"],
                      "missing_batches": generated_info["missing_batches"],
                      "batch_coverage": coverage},
        "generated_section": {
            "numbers_total": built["section_analysis"].get("numbers_total", 0),
            "untraceable": built["section_analysis"].get("untraceable") or [],
            "mismatched": built["section_analysis"].get("mismatched") or [],
        },
        "elements": analysis["elements"],
        "artifacts": artifacts,
        "prepare_worksheet": str(work / "params_candidates.json")
        if (work / "params_candidates.json").is_file() else None,
        "limits": built["limits"],
        "uncertainty": built["uncertainty"][:60],
    }
    evidence_summary = {
        "grounding": built["grounding"],
        "traceability": {"records": len(values.get("traceability") or []),
                         "mismatch": sum(1 for item in values.get("traceability") or []
                                         if item.get("verdict") == "mismatch"),
                         "untraceable": sum(1 for item in values.get("traceability") or []
                                            if item.get("verdict") == "untraceable")},
        "materials_read": [material["path"] for material in analysis["material_summary"]],
        "batch_coverage": coverage.get("coverage"),
    }
    report = hybrid.run_report(
        stopwatch=ctx["stopwatch"],
        constants={
            "topic": TOPIC,
            "rules_resource": ctx.get("rules_path"),
            "rules_note": ctx.get("rules_note"),
            "phase_mode": ctx.get("mode"),
        },
        inputs=ctx["found"],
        work_dir=str(work),
        analysis={
            "materials": len(analysis["materials"]),
            "params": len(analysis["facts"]["params"]),
            "param_conflicts": len(analysis["param_conflicts"]),
            "draft_mentions": len((analysis.get("draft") or {}).get("mentions") or []),
            "gaps": len(analysis["gaps_questions"]["gaps"]),
            "questions": len(analysis["gaps_questions"]["questions"]),
            "elements": {element: bool((analysis["elements"].get(element) or {}).get("covered"))
                         for element in ELEMENTS},
        },
        generated=evidence["generated"],
        coverage=coverage,
        metrics=values.get("quality"),
        artifacts=artifacts,
        evidence_path=str(out_dir / "evidence.json"),
    )
    payload = hybrid.finalize(
        out_dir,
        case=ctx["case"],
        case_id=ctx["case_id"],
        values=values,
        status=built["status"],
        limits=built["limits"],
        uncertainty=built["uncertainty"],
        change_log=built["change_log"],
        task_md_path=ctx["found"].get("task_md_path"),
        evidence=evidence_summary,
        report=report,
        synonyms=FIELD_SYNONYMS,
        additional=ADDITIONAL_KEYS,
    )
    hybrid.write_json(out_dir / "evidence.json", {"case_id": ctx["case_id"], **evidence})
    return payload


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Methods & Experiments chapter writer (deterministic two-phase skill).")
    parser.add_argument("--prepare", action="store_true",
                        help="scan materials and write agent worksheets under --work")
    parser.add_argument("--finalize", action="store_true",
                        help="verify generated content and write result.json + deliverables")
    parser.add_argument("--all", action="store_true",
                        help="prepare then finalize in one run (default)")
    parser.add_argument("--out", default=None, help="output directory (default: /app/output)")
    parser.add_argument("--work", default=None, help="work directory (default: /app/work/<topic>)")
    parser.add_argument("--input", default=None, help="extra input root to scan first")
    return parser.parse_args(argv)


def discover_inputs(ns: argparse.Namespace) -> dict[str, Any]:
    found = discover.discover()
    roots = [Path(root) for root in (found.get("input_roots") or [])]
    if ns.input:
        extra = Path(ns.input)
        if extra.is_dir():
            roots = [extra] + [root for root in roots if root != extra]
            for candidate in (extra / "case.json", extra / "input" / "case.json"):
                if candidate.is_file():
                    found["case_path"] = str(candidate)
                    break
    seen: set[str] = set()
    ordered: list[str] = []
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(str(root))
    found["input_roots"] = ordered or [str(Path.cwd())]
    return found


def resolve_out_dir(prefer: str | None, found: dict[str, Any]) -> Path:
    candidate = prefer or found.get("output_dir")
    return hybrid.resolve_out_dir(candidate)


def resolve_work_dir(prefer: str | None, out_dir: Path) -> Path:
    candidates: list[Path] = []
    if prefer:
        candidates.append(Path(prefer))
    candidates.append(Path("/app/work") / TOPIC)
    candidates.append(Path(out_dir) / "work")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:
            continue
    fallback = Path(out_dir) / "work"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def write_failure(out_dir: Path, *, case: dict[str, Any], case_id: str,
                  limits: list[str], traceback_tail: str) -> None:
    """Last-resort result.json: never leave the container with zero output."""
    try:
        hybrid.finalize(
            out_dir, case=case, case_id=case_id, values={}, status="failed",
            limits=limits, uncertainty=["the run did not finish; every field is a placeholder"],
            change_log=["failure result written by the top-level handler"],
            evidence=None,
            report={"traceback_tail": traceback_tail[-1500:],
                    "generated_by": "top-level failure handler"},
            synonyms=FIELD_SYNONYMS, additional=ADDITIONAL_KEYS)
    except BaseException:
        try:
            hybrid.write_placeholder(out_dir, case_id=case_id,
                                     note="run failed before a result could be assembled")
        except BaseException:
            pass


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(argv)
    mode = "all"
    if ns.prepare and not (ns.finalize or ns.all):
        mode = "prepare"
    elif ns.finalize and not (ns.prepare or ns.all):
        mode = "finalize"

    stopwatch = hybrid.Stopwatch()
    out_dir: Path | None = None
    case: dict[str, Any] = {}
    case_id = ""
    try:
        found = discover_inputs(ns)
        rules, rules_path, rules_note = load_rules()
        case = contract.load_case(found.get("case_path")) or {}
        case_id = contract.find_case_id(case) or ""
        declared = contract.normalize_contract(contract._find_contract(case)) if case else []
        out_dir = resolve_out_dir(ns.out, found)
        work_dir = resolve_work_dir(ns.work, out_dir)

        # Zero-output guard, two layers: a bare placeholder immediately, then a
        # contract-shaped skeleton (all declared fields, declared-type placeholders)
        # before any slow work starts. 900 s can kill the container at any moment.
        hybrid.write_placeholder(out_dir, case_id=case_id,
                                 contract_fields=len(declared))
        try:
            hybrid.finalize(
                out_dir, case=case, case_id=case_id, values={}, status="partial",
                limits=["run started; this file is replaced when the run finishes"],
                change_log=["contract-shaped skeleton written at start of run"],
                evidence=None, report=None, synonyms=FIELD_SYNONYMS,
                additional=ADDITIONAL_KEYS)
        except BaseException:
            pass

        ctx: dict[str, Any] = {
            "mode": mode, "found": found, "rules": rules, "rules_path": rules_path,
            "rules_note": rules_note, "case": case, "case_id": case_id,
            "declared": declared, "out_dir": out_dir, "work_dir": work_dir,
            "stopwatch": stopwatch,
        }
        summary: dict[str, Any] = {"mode": mode, "case_id": case_id,
                                   "contract_fields": len(declared),
                                   "out_dir": str(out_dir), "work_dir": str(work_dir)}
        if rules_note:
            summary["rules_note"] = rules_note
        if mode in ("prepare", "all"):
            summary["prepare"] = prepare_stage(ctx)
        if mode in ("finalize", "all"):
            payload = finalize_stage(ctx)
            meta = payload.get("meta") or {}
            summary["finalize"] = {
                "status": meta.get("status"),
                "contract_field_mapping": meta.get("contract_field_mapping"),
                "contract_fields_not_reported": meta.get("contract_fields_not_reported"),
                "field_fill_ratio": meta.get("field_fill_ratio"),
                "grounding_rate": ((meta.get("evidence") or {}).get("grounding") or {}
                                   ).get("grounding_rate"),
                "limits": meta.get("limits"),
            }
            summary["outputs"] = meta.get("output_files")
        summary["elapsed_s"] = stopwatch.elapsed()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except BaseException as exc:  # noqa: BLE001 - a run must always leave a result
        tb = traceback.format_exc(limit=8)
        print("[fatal] " + type(exc).__name__ + ": " + str(exc), flush=True)
        print(tb, flush=True)
        target = out_dir or Path(ns.out or "output")
        write_failure(target, case=case, case_id=case_id,
                      limits=["run aborted by " + type(exc).__name__ + ": " + str(exc)],
                      traceback_tail=tb)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
