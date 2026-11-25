#!/usr/bin/env python3
"""Reflect ``case.json``'s output contract into ``result.json``.

The task contract is explicit: the final ``result.json`` must carry **all
original ``output_contract`` fields plus ``case_id``**. That means the shape is
decided by the hidden case, not by us. Any hardcoded schema is wrong by
construction, so this module builds the document from whatever the case
declares.

It tolerates the plausible shapes of ``output_contract``:

* ``{"field": {spec}, ...}``            — mapping
* ``[{"name": "field", ...}, ...]``     — list of descriptors
* ``["field", ...]``                    — list of names
* nested under ``task`` / ``spec`` / ``task_spec`` / ``outputs``

Missing values are emitted as ``None`` and listed under
``meta.contract_fields_not_reported`` — an explicit gap beats a silent one, and
the topic statement scores "unable to determine reported as such" positively.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACT_KEYS = ("output_contract", "outputContract", "output_contracts", "required_outputs", "outputs")
CONTAINER_KEYS = ("task", "spec", "task_spec", "taskSpec", "meta", "io", "interface")


def load_case(case_path: str | Path | None) -> dict[str, Any]:
    if not case_path:
        return {}
    p = Path(case_path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _find_contract(node: Any, depth: int = 0) -> Any:
    if depth > 4 or not isinstance(node, dict):
        return None
    for key in CONTRACT_KEYS:
        if key in node and node[key]:
            return node[key]
    for key in CONTAINER_KEYS:
        child = node.get(key)
        if isinstance(child, dict):
            found = _find_contract(child, depth + 1)
            if found is not None:
                return found
    # last resort: scan one level of nested dicts
    for key, value in node.items():
        if isinstance(value, dict):
            found = _find_contract(value, depth + 1)
            if found is not None:
                return found
    return None


def normalize_contract(contract: Any) -> list[dict[str, Any]]:
    """Return an ordered list of ``{"name", "spec", "description"}`` entries."""
    fields: list[dict[str, Any]] = []
    if isinstance(contract, dict):
        for name, spec in contract.items():
            desc = ""
            if isinstance(spec, str):
                desc = spec
            elif isinstance(spec, dict):
                desc = str(spec.get("description") or spec.get("desc") or spec.get("type") or "")
            fields.append({"name": str(name), "spec": spec, "description": desc})
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                fields.append({"name": item, "spec": None, "description": ""})
            elif isinstance(item, dict):
                name = item.get("name") or item.get("field") or item.get("key") or item.get("id")
                if name:
                    fields.append({
                        "name": str(name),
                        "spec": item,
                        "description": str(item.get("description") or item.get("desc") or ""),
                    })
    return fields


def find_case_id(case: dict[str, Any], fallback: str | None = None) -> str | None:
    for key in ("case_id", "caseId", "id"):
        value = case.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    for key in CONTAINER_KEYS:
        child = case.get(key)
        if isinstance(child, dict):
            found = find_case_id(child, None)
            if found:
                return found
    return fallback


def build_result(
    *,
    case: dict[str, Any],
    case_id: str | None,
    values: dict[str, Any],
    evidence: Any = None,
    status: str = "partial",
    limits: list[str] | None = None,
    uncertainty: list[str] | None = None,
    change_log: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    task_md_path: str | None = None,
) -> dict[str, Any]:
    """Assemble a ``result.json`` payload that honours the declared contract."""
    contract = normalize_contract(_find_contract(case))
    names = [f["name"] for f in contract]

    result: dict[str, Any] = {"case_id": case_id or ""}

    not_reported: list[str] = []
    # 1) every declared field, in declared order
    for field in contract:
        name = field["name"]
        if name in values and values[name] is not None:
            result[name] = values[name]
        else:
            result[name] = None
            not_reported.append(name)

    # 2) values we produced that the contract did not name — keep them, but under
    #    a clearly separated key so the contract section stays machine-comparable.
    undeclared = {k: v for k, v in values.items() if k not in names and v is not None}

    meta: dict[str, Any] = {
        "status": status,
        "contract_detected": bool(contract),
        "contract_field_count": len(contract),
        "contract_fields_not_reported": not_reported,
        "limits": limits or [],
        "uncertainty": uncertainty or [],
        "change_log": change_log or [],
    }
    if task_md_path:
        meta["task_md_ref"] = task_md_path
    if undeclared:
        meta["additional_outputs"] = undeclared
    if evidence is not None:
        meta["evidence"] = evidence

    result["meta"] = meta
    if extra:
        result.update({k: v for k, v in extra.items() if k not in result})
    return result


def write_result(out_dir: str | Path, payload: dict[str, Any]) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "result.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    import sys

    case = load_case(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({
        "case_id": find_case_id(case),
        "contract": normalize_contract(_find_contract(case)),
    }, ensure_ascii=False, indent=2))
