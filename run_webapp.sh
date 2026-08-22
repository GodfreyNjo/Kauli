#!/bin/bash
source ~/kauli-env/.venv/bin/activate
cd "/mnt/c/Forge Project"
uvicorn webapp.app:app --host 0.0.0.0 --port 8000
