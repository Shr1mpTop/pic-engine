# -*- coding: utf-8 -*-
"""把 ComfyUI fork 当库用：sys.path 注入 -> 目录接管 -> 真节点类导入。

为什么可行（调研结论，docs/alignment.md 有完整证据）：
  1. fork 的 comfy/options.py 里 args_parsing 默认 False，comfy.cli_args 导入时
     走 parse_args([])，不会吞掉引擎自己的命令行参数；
  2. nodes.py 的导入链（comfy.* -> latent_preview -> cli_args）不触碰 server.py，
     预览回调只写控制台 ProgressBar，无服务器进程也能正常采样；
  3. 节点类就是普通 Python 类（V1 实例方法 / V3 类方法 execute），
     comfy-runtime、ComfyUI-SaveAsScript 都用同一方式直调。

引擎绝不复刻节点实现——import 真类，保证与 ComfyUI 跑出来的结果逐位一致。
"""
import os
import sys


class Fork:
    """已导入的节点类集合（属性访问，避免到处 import）。"""


def bootstrap(comfyui_root, input_dir, output_dir):
    root = os.path.abspath(comfyui_root)
    if root not in sys.path:
        sys.path.insert(0, root)

    import folder_paths
    folder_paths.set_input_directory(os.path.abspath(input_dir))
    folder_paths.set_output_directory(os.path.abspath(output_dir))

    import nodes as comfy_nodes
    from comfy_extras import nodes_qwen

    f = Fork()
    # V1 节点：实例化后调方法（与 ComfyUI-SaveAsScript 生成的脚本同一姿势）
    f.LoadImage = comfy_nodes.LoadImage
    f.UNETLoader = comfy_nodes.UNETLoader
    f.CLIPLoader = comfy_nodes.CLIPLoader
    f.VAELoader = comfy_nodes.VAELoader
    f.EmptyLatentImage = comfy_nodes.EmptyLatentImage
    f.KSampler = comfy_nodes.KSampler
    f.VAEDecode = comfy_nodes.VAEDecode
    f.SaveImage = comfy_nodes.SaveImage
    # V3 节点：类方法 execute(...) -> io.NodeOutput（支持下标解包）
    f.TextEncodeQwenImage21 = nodes_qwen.TextEncodeQwenImage21
    f.QwenImage21Cache = nodes_qwen.QwenImage21Cache
    f.folder_paths = folder_paths
    f.root = root
    return f
