"""Writes the module map page of the engineering guide from the modules' own docstrings.

    python scripts/generate_module_map.py          # rewrite webapp/staff_docs/engineering/08-module-map.md
    python scripts/generate_module_map.py --check  # exit 1 if the committed page is out of date (used by tests)

The map is generated, never typed, so it cannot drift from the code. To improve a line in the map, improve the first
sentence of that module's docstring. A module without a docstring shows up as "(no description yet)", which is a
prompt to write one.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "webapp" / "staff_docs" / "engineering" / "08-module-map.md"
SECTIONS = (
    ("webapp", "The website and its background work (`webapp/`)"),
    ("kauli", "The processing pipeline: transcription, translation, voices (`kauli/`)"),
    ("scripts", "Command-line tools (`scripts/`)"),
)
HEADER = """# Module map

Every Python module, with the first sentence of its own description. This page is **generated** by
`scripts/generate_module_map.py` from the modules' docstrings. Do not edit it by hand: improve the module's
docstring instead, then run the script.

"""


def first_sentence(path: Path) -> str:
    """The module's own one-line description: its docstring up to the first full stop or blank line."""
    docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    paragraph = docstring.strip().split("\n\n")[0].replace("\n", " ").strip()
    sentence = paragraph.split(". ")[0].rstrip(".")
    return (sentence[:200] + "...") if len(sentence) > 200 else (sentence or "(no description yet)")


def table_for(directory: str) -> list[str]:
    rows = ["| Module | Lines | What it is for |", "|---|---:|---|"]
    for path in sorted((ROOT / directory).rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in path.parts or "vendor" in path.parts:
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        name = path.relative_to(ROOT / directory).as_posix()
        rows.append(f"| `{name}` | {lines} | {first_sentence(path).replace('|', '/')} |")
    return rows


def build_page() -> str:
    parts = [HEADER]
    for directory, title in SECTIONS:
        parts.append(f"## {title}\n\n" + "\n".join(table_for(directory)) + "\n\n")
    return "".join(parts).rstrip() + "\n"


def main(argv: list[str]) -> int:
    page = build_page()
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != page:
            print("The module map is out of date. Run: python scripts/generate_module_map.py")
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
