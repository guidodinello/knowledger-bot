#!/usr/bin/env bash
# Wrapper for transcribe.py — sets LD_LIBRARY_PATH for CUDA 12 libs bundled in .venv-transcribe
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/../.venv-transcribe"
PYTHON="$VENV/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Error: $VENV not found. Run: python3 -m venv .venv-transcribe && .venv-transcribe/bin/pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12" >&2
  exit 1
fi

NVIDIA_LIBS=$(find "$VENV/lib" -path "*/nvidia/*/lib" -type d 2>/dev/null | tr '\n' ':')
export LD_LIBRARY_PATH="${NVIDIA_LIBS}${LD_LIBRARY_PATH:-}"

exec "$PYTHON" "$SCRIPT_DIR/transcribe.py" "$@"
