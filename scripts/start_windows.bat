@echo off
rem 用 ComfyUI fork 的嵌入式 Python 启动引擎（torch/aiohttp/PIL 全在里头）
rem run.py 是必须的：嵌入式解释器的 ._pth 隔离模式不含 cwd，-m 找不到包
rem 端口默认 8195；改监听范围改 config.json 的 host
cd /d %~dp0..
E:\github\ComfyUI\python_embeded\python.exe run.py serve --config config.json
