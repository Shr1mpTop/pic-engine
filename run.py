# -*- coding: utf-8 -*-
r"""Windows 嵌入式 Python 引导器。

python_embeded 的 python313._pth 是隔离模式（sys.path 固定，不含 cwd），
`python -m pic_engine` 找不到包；本文件在运行时把仓库根插进 sys.path
再调 CLI。标准解释器（Mac venv 等）用 `python -m pic_engine` 亦可。

    E:\github\ComfyUI\python_embeded\python.exe run.py check --config config.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pic_engine.__main__ import main

if __name__ == "__main__":
    main()
