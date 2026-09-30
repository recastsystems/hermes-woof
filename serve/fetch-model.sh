#!/usr/bin/env bash
# Download llama.cpp (prebuilt Linux CUDA release) and the Qwen3.8-27B GGUF files a profile needs.
#
# Usage: fetch-model.sh [demo|shared24|shared24v|card24|all]
# Settings (environment):
#   WOOF_LLM_HOME    target folder (default ~/woof-llm); files land in bin/ and models/
#   LLAMA_BUILD      llama.cpp release tag (default b11246, the build these scripts were checked on)
#   LLAMA_CUDA       CUDA flavour of the release asset (default 12.8; runs on any driver that
#                    supports CUDA 12.8 or newer, including RTX 50 series cards)
# Needs curl, plus aria2c when present (faster). No account or token is needed; the model is
# not gated. Every model file is checked against the SHA-256 published with it.
set -euo pipefail

PROFILE="${1:-demo}"
HOME_DIR="${WOOF_LLM_HOME:-$HOME/woof-llm}"
BUILD="${LLAMA_BUILD:-b11246}"
CUDA="${LLAMA_CUDA:-12.8}"
REPO="unsloth/Qwen3.8-27B-GGUF"
HF="https://huggingface.co/$REPO/resolve/main"
GH="https://github.com/ggml-org/llama.cpp/releases/download/$BUILD"
export HF_HUB_DISABLE_IMPLICIT_TOKEN=1

mkdir -p "$HOME_DIR/bin" "$HOME_DIR/models" "$HOME_DIR/dl"

get() {  # url dir name
  if command -v aria2c >/dev/null; then
    aria2c -q -x16 -s16 -c -d "$2" -o "$3" "$1"
  else
    curl -fsSL --retry 5 -C - -o "$2/$3" "$1"
  fi
}

case "$PROFILE" in
  demo)     FILES=(Qwen3.8-27B-UD-Q5_K_XL.gguf mmproj-BF16.gguf) ;;
  shared24) FILES=(Qwen3.8-27B-UD-Q4_K_S.gguf) ;;
  shared24v) FILES=(Qwen3.8-27B-UD-Q4_K_S.gguf mmproj-BF16.gguf) ;;
  card24)   FILES=(Qwen3.8-27B-UD-Q4_K_XL.gguf) ;;
  all)      FILES=(Qwen3.8-27B-UD-Q5_K_XL.gguf mmproj-BF16.gguf Qwen3.8-27B-UD-Q4_K_S.gguf Qwen3.8-27B-UD-Q4_K_XL.gguf) ;;
  *) echo "fetch-model: unknown profile '$PROFILE'" >&2; exit 2 ;;
esac

if [ ! -x "$HOME_DIR/bin/llama-server" ]; then
  echo "fetch-model: llama.cpp $BUILD (CUDA $CUDA)"
  get "$GH/llama-$BUILD-bin-ubuntu-cuda-$CUDA-x64.tar.gz" "$HOME_DIR/dl" "llama-$BUILD.tar.gz"
  get "$GH/cudart-llama-$BUILD-bin-ubuntu-cuda-$CUDA-x64.tar.gz" "$HOME_DIR/dl" "cudart-$BUILD.tar.gz"
  tar xzf "$HOME_DIR/dl/llama-$BUILD.tar.gz" -C "$HOME_DIR/bin" --strip-components=1
  tar xzf "$HOME_DIR/dl/cudart-$BUILD.tar.gz" -C "$HOME_DIR/bin" --strip-components=1
  rm -f "$HOME_DIR/dl/llama-$BUILD.tar.gz" "$HOME_DIR/dl/cudart-$BUILD.tar.gz"
fi

# Published SHA-256 of every file in the repo (the Hub tree API lists the LFS object ids).
TREE="$(curl -fsS "https://huggingface.co/api/models/$REPO/tree/main")"
for f in "${FILES[@]}"; do
  want="$(printf '%s' "$TREE" | python3 -c 'import json,sys; n=sys.argv[1]; print(next(((x.get("lfs") or {}).get("oid","") for x in json.load(sys.stdin) if x["path"]==n),""))' "$f")"
  [ -n "$want" ] || { echo "fetch-model: $f is not in $REPO" >&2; exit 2; }
  if [ -f "$HOME_DIR/models/$f" ] && [ ! -f "$HOME_DIR/models/$f.aria2" ] && \
     [ "$(cat "$HOME_DIR/models/$f.sha256" 2>/dev/null)" = "$want" ]; then
    echo "fetch-model: $f already present and checked"; continue
  fi
  if [ -f "$HOME_DIR/models/$f" ] && [ ! -f "$HOME_DIR/models/$f.aria2" ] && \
     [ "$(sha256sum "$HOME_DIR/models/$f" | cut -d' ' -f1)" = "$want" ]; then
    echo "$want" > "$HOME_DIR/models/$f.sha256"
    echo "fetch-model: $f already present, checksum recorded"; continue
  fi
  echo "fetch-model: $f"
  get "$HF/$f" "$HOME_DIR/models" "$f"
  have="$(sha256sum "$HOME_DIR/models/$f" | cut -d' ' -f1)"
  if [ "$have" != "$want" ]; then
    echo "fetch-model: checksum mismatch for $f (have $have, want $want)" >&2; exit 7
  fi
  echo "$have" > "$HOME_DIR/models/$f.sha256"
done
echo "fetch-model: done ($PROFILE) in $HOME_DIR"
