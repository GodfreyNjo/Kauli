#!/bin/bash
# One command to run every check before you commit or deploy:   bash scripts/check.sh
# Add "fast" to skip the full test suite:                        bash scripts/check.sh fast
set -e
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"

echo "1/3  quality gate (lint, function length, module length)"
"$PYTHON" scripts/quality_gate.py

echo "2/3  tests of the code that changed most often"
"$PYTHON" -m pytest -q tests/test_prepay.py tests/test_funnel_audit.py tests/test_pivot_pages.py tests/test_quality_gate.py

if [ "$1" != "fast" ]; then
  echo "3/3  the whole test suite"
  "$PYTHON" -m pytest -q
fi
echo "All checks passed."
