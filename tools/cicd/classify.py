"""Conservatively classify a Git range; unknown or unreadable means heavy CI."""

import os
import re
import subprocess
from typing import TypedDict


class Classification(TypedDict):
    docs_only: bool
    reason: str


DOC_FILES = frozenset(
    {
        b"README.md",
        b"LINKS.md",
        b"LICENSE",
        b"runner/README.md",
        b"frontend/README.md",
        b"web-product/README.md",
        b"web-demo/README.md",
    }
)
SHA = re.compile(r"[0-9a-f]{40}")


def _is_doc(path: bytes) -> bool:
    if path in DOC_FILES:
        return True
    parts = path.split(b"/")
    return (
        len(parts) >= 2
        and parts[0] == b"docs"
        and path.endswith(b".md")
        and all(part not in {b"", b".", b".."} for part in parts)
    )


def _docs_diff(data: bytes) -> bool:
    if not data or not data.endswith(b"\0"):
        return False
    fields = iter(data[:-1].split(b"\0"))
    for header in fields:
        metadata = header.split(b" ")
        if len(metadata) != 5 or not metadata[0].startswith(b":"):
            return False
        old_mode, new_mode = metadata[0][1:], metadata[1]
        status = metadata[4]
        if status == b"A":
            valid_modes = old_mode == b"000000" and new_mode == b"100644"
            count = 1
        elif status == b"D":
            valid_modes = old_mode == b"100644" and new_mode == b"000000"
            count = 1
        elif status == b"M" or re.fullmatch(rb"R[0-9]{1,3}", status):
            valid_modes = old_mode == new_mode == b"100644"
            count = 2 if status.startswith(b"R") else 1
        else:
            return False
        if not valid_modes:
            return False
        for _ in range(count):
            path = next(fields, None)
            if path is None or not _is_doc(path):
                return False
    return True


def classify(base: str, head: str, *, triple_dot: bool = False) -> Classification:
    if not all(SHA.fullmatch(value) and value != "0" * 40 for value in (base, head)):
        return {"docs_only": False, "reason": "missing or invalid commit SHA"}
    revision = f"{base}{'...' if triple_dot else '..'}{head}"
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--raw",
                "-z",
                "--find-renames",
                revision,
                "--",
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "docs_only": False,
            "reason": f"git diff failed: {type(error).__name__}",
        }
    docs_only = _docs_diff(result.stdout)
    return {
        "docs_only": docs_only,
        "reason": "only allowlisted documentation"
        if docs_only
        else "code, unknown, or empty diff",
    }


def main() -> None:
    decision = classify(
        os.environ.get("BASE_SHA", ""),
        os.environ.get("HEAD_SHA", ""),
        triple_dot=os.environ.get("EVENT_NAME") == "pull_request",
    )
    print(decision["reason"])
    # Only a fixed boolean is written to the Actions output protocol, never filenames.
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"docs_only={str(decision['docs_only']).lower()}\n")


if __name__ == "__main__":
    main()
