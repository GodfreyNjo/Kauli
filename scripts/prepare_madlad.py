"""Fetch Google's MADLAD-400 3B translation model (Apache-2.0) and convert it to a compact CTranslate2 int8 folder that Kauli can run
on the server's CPU (kauli.providers.mt.MadladMT).

    python scripts/prepare_madlad.py [output_dir]

Download is about 12 GB, the converted folder about 3 GB. Run it on a machine with enough memory plus swap (about 12 GB in total: the model is
loaded in half precision, so the working size is about 6 GB) and copy the output folder to the server's
webapp/data/models/madlad400-3b-mt-ct2 (that folder is on the persistent volume, so redeploys keep it).
"""
import shutil
import sys
from pathlib import Path

REPO = "google/madlad400-3b-mt"


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "madlad400-3b-mt-ct2").resolve()
    from ctranslate2.converters import TransformersConverter
    from huggingface_hub import snapshot_download
    src = snapshot_download(REPO, allow_patterns=["*.json", "spiece.model", "model.safetensors"])
    # the command-line converter has no half-precision switch, the Python one does
    TransformersConverter(src, low_cpu_mem_usage=True, load_as_float16=True).convert(str(out), quantization="int8", force=True)
    shutil.copy(Path(src) / "spiece.model", out / "spiece.model")
    print(f"ready: {out}")


if __name__ == "__main__":
    main()
