"""Fetch Google's MADLAD-400 3B translation model (Apache-2.0) and convert it to a compact CTranslate2 int8 folder that Kauli can run
on the server's CPU (webapp/madlad_mt: kauli.providers.mt.MadladMT).

    python scripts/prepare_madlad.py [output_dir]

Download is about 12 GB, the converted folder about 3 GB. Run it on a machine with 6+ GB free RAM and copy the output folder to the
server's webapp/data/models/madlad400-3b-mt-ct2 (that folder is on the persistent volume, so redeploys keep it).
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "google/madlad400-3b-mt"


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "madlad400-3b-mt-ct2").resolve()
    from huggingface_hub import snapshot_download
    src = snapshot_download(REPO, allow_patterns=["*.json", "spiece.model", "model.safetensors"])
    subprocess.run([sys.executable, "-m", "ctranslate2.converters.transformers", "--model", src, "--output_dir", str(out),
                    "--quantization", "int8", "--low_cpu_mem_usage", "--force"], check=True)
    shutil.copy(Path(src) / "spiece.model", out / "spiece.model")
    print(f"ready: {out}")


if __name__ == "__main__":
    main()
