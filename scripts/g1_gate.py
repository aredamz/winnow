"""G1 gate runner (PLAN.md F1): index the demo repo, ask 5 questions, check citations.

Usage: uv run python scripts/g1_gate.py /path/to/winnow-demo-shop
Exit 0 when all 5 answers cite the required files.
"""

from __future__ import annotations

import re
import subprocess
import sys

import yaml

CASES = "eval/cases/g1_questions.yaml"
CITATION_RE = re.compile(r"([\w./-]+\.py):(\d+)-(\d+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def run(*args: str) -> str:
    # Fixed argv, no shell: inputs are repo paths and our own question strings.
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600)  # noqa: S603
    return ANSI_RE.sub("", proc.stdout + proc.stderr)


def main(demo_path: str) -> None:
    cases = yaml.safe_load(open(CASES, encoding="utf-8"))
    print(f"indexing {demo_path} ...")
    print(run("uv", "run", "winnow", "index", demo_path)[-500:])
    failures = 0
    for case in cases:
        out = run("uv", "run", "winnow", "ask", demo_path, case["question"])
        cited = {m.group(1) for m in CITATION_RE.finditer(out)}
        missing = [p for p in case["must_cite"] if p not in cited]
        status = "PASS" if not missing else "FAIL"
        if missing:
            failures += 1
        print(f"[{status}] {case['id']}: cited={sorted(cited)} missing={missing}")
    print("G1 PASS: 5/5" if failures == 0 else f"G1 FAIL: {failures} question(s) missed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main(sys.argv[1])
