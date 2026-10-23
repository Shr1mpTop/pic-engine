# -*- coding: utf-8 -*-
"""命令行入口：
    python -m outfit_engine check  --config config.json     # 冒烟：配置/fork/模型文件/目录，不加载模型
    python -m outfit_engine serve  --config config.json     # 常驻服务
    python -m outfit_engine render --config config.json --photo x.jpg [--style ...] [--quality fast]
                                     [--seed1 N --seed2 N --analysis-file a.json]   # 本机同步出图（调试）
必须用 ComfyUI fork 的 Python 环境启动（torch/aiohttp 都在那边）。
"""
import argparse
import json
import os
import sys

from . import __version__


def cmd_check(args):
    from . import bootstrap, config
    cfg = config.load(args.config)
    print("engine %s" % __version__)
    print("fork: %s" % cfg["comfyui_root"])
    fork = bootstrap.bootstrap(cfg["comfyui_root"], cfg["input_dir"], cfg["output_dir"])
    print("节点导入: OK（nodes.py + nodes_qwen.py）")
    ok = True
    for kind, name in (("diffusion_models", cfg["models"]["unet"]),
                       ("text_encoders", cfg["models"]["clip"]),
                       ("vae", cfg["models"]["vae"])):
        p = fork.folder_paths.get_full_path(kind, name)
        print("  %-16s %-40s %s" % (kind, name, p or "!! 未找到"))
        ok = ok and p is not None
    for key in ("input_dir", "output_dir"):
        d = cfg[key]
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".engine_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        print("  %-16s %s （可写）" % (key, d))
    import torch
    print("torch %s  cuda=%s" % (torch.__version__, torch.cuda.is_available()))
    print("prompts: step1 模板 %d 字 / 负面词 %d 字" % (
        len(cfg["prompts"]["step1_template"]), len(cfg["prompts"]["negative"])))
    if not ok:
        print("!! 有模型文件缺失", file=sys.stderr)
        sys.exit(1)
    print("check 全部通过")


def cmd_serve(args):
    from . import config, server
    server.run(config.load(args.config))


def cmd_render(args):
    from . import bootstrap, config
    from .pipeline import Renderer
    cfg = config.load(args.config)
    fork = bootstrap.bootstrap(cfg["comfyui_root"], cfg["input_dir"], cfg["output_dir"])
    styles = list(args.style)
    if args.style_file:
        with open(args.style_file, "r", encoding="utf-8") as f:
            styles += [ln.strip() for ln in f if ln.strip()]
    analysis = None
    if args.analysis_file:
        with open(args.analysis_file, "r", encoding="utf-8") as f:
            analysis = json.load(f)
    params = {"photo": args.photo, "styles": styles, "analysis": analysis,
              "seed1": args.seed1, "seed2": args.seed2,
              "quality": args.quality}
    w, h, steps = config.quality_params(cfg, args.quality, {})
    params.update({"width": w, "height": h, "steps": steps})
    res = Renderer(cfg, fork).render(
        params, progress=lambda s: print("  [engine] %s" % s, flush=True))
    print(json.dumps(res, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(prog="outfit_engine")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check")
    p.add_argument("--config", default="config.json")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("serve")
    p.add_argument("--config", default="config.json")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("render")
    p.add_argument("--config", default="config.json")
    p.add_argument("--photo", required=True)
    p.add_argument("--style", action="append", default=[])
    p.add_argument("--style-file")
    p.add_argument("--quality", default="standard")
    p.add_argument("--seed1", type=int)
    p.add_argument("--seed2", type=int)
    p.add_argument("--analysis-file")
    p.set_defaults(fn=cmd_render)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
