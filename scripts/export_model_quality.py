"""Turn the language evaluation (scripts/eval_languages.py -> results.jsonl) into kauli/providers/own_model_quality.json, the file that decides
where our own models go first. Run again when the evaluation has more languages:

    python scripts/export_model_quality.py /root/eval/results.jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "kauli" / "providers" / "own_model_quality.json"


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/eval/results.jsonl")
    asr, mt = {}, {}
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        s, t = r.get("speech") or {}, r.get("translation") or {}
        if s.get("status") == "ok" and isinstance(s.get("error"), (int, float)):
            asr[r["code"]] = round(float(s["error"]), 3)
        if t.get("status") == "ok":
            mt[r["code"]] = {"from_en": t.get("from_en") if isinstance(t.get("from_en"), (int, float)) else None,
                             "to_en": t.get("to_en") if isinstance(t.get("to_en"), (int, float)) else None}
    OUT.write_text(json.dumps({"measured": date.today().isoformat(), "languages": len(set(asr) | set(mt)), "asr": dict(sorted(asr.items())),
                               "mt": dict(sorted(mt.items()))}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(asr)} languages with speech results, {len(mt)} with translation results -> {OUT}")


if __name__ == "__main__":
    main()
