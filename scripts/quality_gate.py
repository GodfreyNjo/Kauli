"""The quality gate: new code must meet the standard, and old code may not get worse.

Run it before every commit and in CI:

    python scripts/quality_gate.py            # check: exits 1 and says what to fix
    python scripts/quality_gate.py --report   # the list of known debt, biggest first
    python scripts/quality_gate.py --update   # record an improvement (refuses to record anything that got worse)
    python scripts/quality_gate.py --init     # create the first baseline (only when there is none yet)

How it works. `quality-baseline.json` lists the shortcomings the code already had when the gate was introduced: lint
findings per file, functions longer than the limit, and files longer than the limit. Anything not in that list must be
clean. Anything in the list may stay, but may not grow. When someone fixes a listed item, `--update` shrinks the list,
so the debt only ever goes down.

The three checks (the limits are the constants below, explained in the engineering guide's coding standards):
  1. lint: `ruff` with the rules in pyproject.toml (PEP 8 style, bug patterns, naming, complexity)
  2. function length: no function longer than MAX_FUNCTION_LINES (the aim is 20)
  3. file length: no module longer than MAX_FILE_LINES (one module, one reason to change)
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / "quality-baseline.json"
CODE_DIRS = ("webapp", "kauli", "scripts")
MAX_FUNCTION_LINES = 40
TARGET_FUNCTION_LINES = 20
MAX_FILE_LINES = 800
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


# --------------------------------------------------------------------------------------------------- measuring
def python_files() -> list[Path]:
    """Every source file the gate looks at."""
    found = []
    for directory in CODE_DIRS:
        found += sorted((ROOT / directory).rglob("*.py"))
    return [p for p in found if "vendor" not in p.parts and "__pycache__" not in p.parts]


def lint_findings() -> list[dict]:
    """Every finding ruff reports, using the settings in pyproject.toml."""
    command = [sys.executable, "-m", "ruff", "check", *CODE_DIRS, "tests",
               "--output-format", "json", "--exit-zero", "--no-cache"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1) or not result.stdout.strip():
        raise SystemExit("ruff could not run. Install it: pip install -r requirements-dev.txt\n" + result.stderr[-400:])
    return json.loads(result.stdout)


def lint_counts(findings: list[dict]) -> dict[str, dict[str, int]]:
    """{relative file: {rule code: how many}}, for comparing with the baseline."""
    counts: dict[str, Counter] = {}
    for finding in findings:
        name = Path(finding["filename"]).resolve().relative_to(ROOT).as_posix()
        counts.setdefault(name, Counter())[finding["code"] or "syntax"] += 1
    return {name: dict(sorted(rules.items())) for name, rules in sorted(counts.items())}


def function_lengths(path: Path, root: Path = ROOT) -> dict[str, int]:
    """{"file::Class.method": number of lines} for every function longer than the limit."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    relative = path.relative_to(root).as_posix()
    long_ones: dict[str, int] = {}
    collect_long_functions(tree, "", relative, long_ones)
    return long_ones


def collect_long_functions(node: ast.AST, prefix: str, relative: str, found: dict[str, int]) -> None:
    """Walk the tree, remembering the class and function names above each function."""
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (*FUNCTION_NODES, ast.ClassDef)):
            collect_long_functions(child, prefix, relative, found)
            continue
        name = f"{prefix}{child.name}"
        size = child.end_lineno - child.lineno + 1
        if isinstance(child, FUNCTION_NODES) and size > MAX_FUNCTION_LINES:
            found[f"{relative}::{name}"] = size
        collect_long_functions(child, name + ".", relative, found)


def measure() -> dict:
    """Everything the gate records, in the shape stored in the baseline."""
    functions, files = {}, {}
    for path in python_files():
        functions.update(function_lengths(path))
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_FILE_LINES:
            files[path.relative_to(ROOT).as_posix()] = line_count
    return {"lint": lint_counts(lint_findings()),
            "function_lines": dict(sorted(functions.items())),
            "file_lines": dict(sorted(files.items()))}


# ---------------------------------------------------------------------------------------------------- comparing
def worse_lint(current: dict, baseline: dict) -> list[str]:
    problems = []
    for name, rules in current.items():
        for rule, count in rules.items():
            allowed = baseline.get(name, {}).get(rule, 0)
            if count > allowed:
                problems.append(f"{name}: {count} x {rule} (allowed {allowed}). Run: ruff check {name} --select {rule}")
    return problems


def size_message(name: str, size: int, what: str, limit: int, allowed: int | None) -> str:
    message = f"{name}: {size} lines. A {what} may be at most {limit} lines"
    if allowed is None:
        return message + "."
    return message + f" (this one was already {allowed}; it may not grow)."


def worse_sizes(current: dict, baseline: dict, what: str, limit: int) -> list[str]:
    problems = []
    for name, size in current.items():
        allowed = baseline.get(name)
        if size > (allowed if allowed is not None else limit):
            problems.append(size_message(name, size, what, limit, allowed))
    return problems


def compare(current: dict, baseline: dict) -> list[str]:
    """Everything in `current` that is worse than the baseline allows."""
    return (worse_lint(current["lint"], baseline.get("lint", {}))
            + worse_sizes(current["function_lines"], baseline.get("function_lines", {}), "function", MAX_FUNCTION_LINES)
            + worse_sizes(current["file_lines"], baseline.get("file_lines", {}), "module", MAX_FILE_LINES))


def load_baseline() -> dict:
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8")) if BASELINE_FILE.exists() else {}


def save_baseline(measured: dict) -> None:
    BASELINE_FILE.write_text(json.dumps(measured, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------------------------------- commands
def print_report(measured: dict) -> None:
    lint_total = sum(sum(rules.values()) for rules in measured["lint"].values())
    functions = measured["function_lines"]
    print(f"Lint findings still on the books: {lint_total} in {len(measured['lint'])} files")
    print(f"Functions over {MAX_FUNCTION_LINES} lines (the aim is {TARGET_FUNCTION_LINES}): {len(functions)}.")
    print("The longest:")
    for name, size in sorted(functions.items(), key=lambda item: -item[1])[:15]:
        print(f"  {size:>5}  {name}")
    modules = ", ".join(f"{n} ({s})" for n, s in measured["file_lines"].items())
    print(f"Modules over {MAX_FILE_LINES} lines: {modules}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="record an improvement (never a regression)")
    parser.add_argument("--report", action="store_true", help="print the known debt, biggest first")
    parser.add_argument("--init", action="store_true", help="create the first baseline (only when there is none)")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    measured = measure()
    if args.report:
        print_report(measured)
        return 0
    if args.init and not BASELINE_FILE.exists():
        save_baseline(measured)
        print(f"First baseline written to {BASELINE_FILE.name}. From now on the debt may only shrink.")
        return 0
    problems = compare(measured, load_baseline())
    if args.update and not problems:
        save_baseline(measured)
        print("Baseline updated: the debt list only got shorter.")
        return 0
    if problems:
        print("The quality gate found code that is worse than the standard allows:\n")
        print("\n".join(f"  - {line}" for line in problems))
        print("\nFix these (see the engineering guide, coding standards). Do not add them to the baseline.")
        return 1
    print("Quality gate passed: nothing is worse than the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
