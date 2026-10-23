#!/bin/bash
# Mac 侧启动：PY 指向跑 ComfyUI 的那个解释器（venv/conda 均可），例如：
#   PY=~/ComfyUI/venv/bin/python ./scripts/start_mac.sh
cd "$(dirname "$0")/.."
PY="${PY:-python3}"
exec "$PY" -m pic_engine serve --config config.json
