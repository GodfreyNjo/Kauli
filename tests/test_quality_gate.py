"""The quality gate (scripts/quality_gate.py).

It must catch new debt, let old debt stay without growing, and pass on the codebase as it stands.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("quality_gate", ROOT / "scripts" / "quality_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def write_module(tmp_path, body_lines: int) -> Path:
    header = "def tidy():\n    return 1\n\n\nclass Thing:\n    def long_method(self):\n"
    source = header + "        total = 1\n" * body_lines + "        return total\n"
    path = tmp_path / "module.py"
    path.write_text(source, encoding="utf-8")
    return path


def measured(lint=None, functions=None, files=None) -> dict:
    return {"lint": lint or {}, "function_lines": functions or {}, "file_lines": files or {}}


def test_a_function_longer_than_the_limit_is_found_with_its_class_and_name(tmp_path):
    path = write_module(tmp_path, gate.MAX_FUNCTION_LINES + 5)
    found = gate.function_lengths(path, root=tmp_path)
    assert list(found) == ["module.py::Thing.long_method"]
    assert found["module.py::Thing.long_method"] > gate.MAX_FUNCTION_LINES


def test_a_short_function_is_fine(tmp_path):
    assert gate.function_lengths(write_module(tmp_path, 5), root=tmp_path) == {}


def test_old_debt_may_stay_or_shrink_but_not_grow():
    baseline = measured({"a.py": {"E501": 3}}, {"a.py::old": 100}, {"big.py": 900})
    same = measured({"a.py": {"E501": 3}}, {"a.py::old": 100}, {"big.py": 900})
    better = measured({"a.py": {"E501": 1}})
    grew = measured({"a.py": {"E501": 4}}, {"a.py::old": 120}, {"big.py": 950})
    assert gate.compare(same, baseline) == []
    assert gate.compare(better, baseline) == []
    assert len(gate.compare(grew, baseline)) == 3


def test_new_debt_is_refused():
    baseline = measured({"a.py": {"E501": 3}})
    fresh = measured({"b.py": {"F401": 1}}, {"b.py::fresh": 60}, {"fresh.py": 801})
    problems = gate.compare(fresh, baseline)
    assert len(problems) == 3
    assert any("b.py" in line and "F401" in line for line in problems)


def test_the_codebase_passes_the_gate_as_it_stands():
    ruff_check = [sys.executable, "-m", "ruff", "--version"]
    if subprocess.run(ruff_check, capture_output=True, check=False).returncode:
        pytest.skip("ruff is not installed (pip install -r requirements-dev.txt)")
    command = [sys.executable, "scripts/quality_gate.py"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout[-2000:]
