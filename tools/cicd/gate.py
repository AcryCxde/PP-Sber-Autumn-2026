"""Stable required check: skipped heavy jobs require proven docs-only."""

import json
import os
from collections.abc import Mapping

HEAVY_JOBS = frozenset(
    {"build", "python", "frontend", "web-product", "compose", "schema-postgres"}
)


def gate_passes(needs: object) -> bool:
    if not isinstance(needs, Mapping) or set(needs) != HEAVY_JOBS | {"classify"}:
        return False
    classifier = needs["classify"]
    if not isinstance(classifier, Mapping) or classifier.get("result") != "success":
        return False
    outputs = classifier.get("outputs")
    if not isinstance(outputs, Mapping) or outputs.get("docs_only") not in {
        "true",
        "false",
    }:
        return False
    allowed = {"success", "skipped"} if outputs["docs_only"] == "true" else {"success"}
    return all(
        isinstance(needs[job], Mapping) and needs[job].get("result") in allowed
        for job in HEAVY_JOBS
    )


def main() -> int:
    try:
        needs = json.loads(os.environ.get("NEEDS_JSON", ""))
    except json.JSONDecodeError:
        print("gate failed: invalid needs JSON")
        return 1
    passed = gate_passes(needs)
    print(
        "gate passed"
        if passed
        else "gate failed: classification or required jobs not successful"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
