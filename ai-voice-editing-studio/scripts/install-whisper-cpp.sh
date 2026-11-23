#!/usr/bin/env bash
set -euo pipefail
# Optional third speech-engine fallback. Run on a Linux host with build tools.
ROOT="${WHISPER_CPP_DIR:-$HOME/whisper.cpp}"
MODEL_DIR="${WHISPER_CPP_MODEL_DIR:-$HOME/.cache/whisper.cpp}"
if ! command -v cmake >/dev/null || ! command -v git >/dev/null; then
  echo "Install git, cmake, and a C/C++ build toolchain first." >&2; exit 1
fi
if [ ! -d "$ROOT/.git" ]; then git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$ROOT"; fi
cmake -S "$ROOT" -B "$ROOT/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/build" --config Release -j "$(nproc)" --target whisper-cli
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/ggml-small.bin" ]; then
  curl -fL https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin -o "$MODEL_DIR/ggml-small.bin"
fi
printf '\nAdd these environment variables to the backend:\nWHISPER_CPP_MODEL_PATH=%s/ggml-small.bin\nPATH=%s/build/bin:$PATH\n' "$MODEL_DIR" "$ROOT"
