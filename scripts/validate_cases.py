#!/usr/bin/env python3
"""Validate the Prismloop APP Case discovery boundary without cloud access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
INDEX_PATH = CASES / "index.json"
CASE_ID = re.compile(r"^APP-[A-Z0-9][A-Z0-9-]{2,63}$")
SECRET_MARKERS = re.compile(
    r"(?i)(authorization\s*[:=]|bearer\s+[a-z0-9._-]+|gh[oprsu]_[a-z0-9]+|password\s*[:=]|secret\s*[:=])"
)


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.relative_to(ROOT)}: invalid JSON: {exc}") from exc


def main() -> int:
    errors: list[str] = []
    index = load_json(INDEX_PATH)
    if not isinstance(index, dict):
        errors.append("cases/index.json must be an object")
        index = {}
    if index.get("schema_version") != "prismloop.app-case-index.v1":
        errors.append("cases/index.json has an unsupported schema_version")
    if index.get("discovery_rule") != "listed_only":
        errors.append("cases/index.json must use discovery_rule=listed_only")

    listed: list[str] = []
    suites = index.get("suites", [])
    if not isinstance(suites, list):
        errors.append("cases/index.json suites must be an array")
        suites = []
    suite_ids: set[str] = set()
    for suite in suites:
        if not isinstance(suite, dict):
            errors.append("every suite must be an object")
            continue
        suite_id = suite.get("suite_id")
        if not isinstance(suite_id, str) or not suite_id:
            errors.append("every suite needs suite_id")
        elif suite_id in suite_ids:
            errors.append(f"duplicate suite_id: {suite_id}")
        else:
            suite_ids.add(suite_id)
        files = suite.get("case_files", [])
        if not isinstance(files, list):
            errors.append(f"suite {suite_id!r} case_files must be an array")
            continue
        listed.extend(str(item) for item in files)

    if len(listed) != len(set(listed)):
        errors.append("a Case file may be listed only once")

    discovered = {
        str(path.relative_to(ROOT))
        for path in (CASES / "suites").rglob("*.json")
    }
    listed_set = set(listed)
    for path in sorted(discovered - listed_set):
        errors.append(f"unlisted Case file: {path}")
    for relative in sorted(listed_set - discovered):
        errors.append(f"listed Case file does not exist: {relative}")

    case_ids: set[str] = set()
    for relative in sorted(listed_set & discovered):
        path = ROOT / relative
        case = load_json(path)
        if not isinstance(case, dict):
            errors.append(f"{relative}: Case must be an object")
            continue
        required = {
            "schema_version", "case_id", "title", "owner", "status", "priority",
            "design_ref", "environments", "preconditions", "steps", "assertions",
            "evidence_required", "teardown",
        }
        missing = sorted(required - set(case))
        if missing:
            errors.append(f"{relative}: missing fields: {', '.join(missing)}")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            errors.append(f"{relative}: invalid case_id")
        elif case_id in case_ids:
            errors.append(f"duplicate case_id: {case_id}")
        else:
            case_ids.add(case_id)
        raw = path.read_text(encoding="utf-8")
        if SECRET_MARKERS.search(raw):
            errors.append(f"{relative}: possible plaintext secret marker")

    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(f"Prismloop Case index valid: suites={len(suite_ids)} cases={len(case_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

