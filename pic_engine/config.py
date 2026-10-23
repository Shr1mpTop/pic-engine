# -*- coding: utf-8 -*-
"""配置加载：config.json（机器本地，不入库）+ 内置默认值。

所有默认值都从生产现场逐一对齐提取（对齐工具 scripts/extract_from_workflow.py
可从 workflow.json 重新校准 prompts/models/sampler 三节）：
  - 模型三件套  <- workflow.json 的 UNETLoader/CLIPLoader/VAELoader
  - 采样参数    <- workflow.json 的 KSampler + TextEncodeQwenImage21.resolution
  - 质量三档    <- outfit-gateway/compose_api.py 的 QUALITY 表
  - Step1 模板与负面词 <- workflow.json 节点 670 / 459:474（extract 工具回填）
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# gateway compose_api.py 的 QUALITY 原样（宽, 高, 步数），三档都是 3:4
DEFAULT_QUALITY = {
    "fast": {"width": 480, "height": 640, "steps": 20},
    "standard": {"width": 960, "height": 1280, "steps": 16},
    "best": {"width": 1440, "height": 1920, "steps": 16},
}

DEFAULTS = {
    "comfyui_root": "",              # 必填：fork 检出目录（内含 comfy/ nodes.py ...）
    "host": "127.0.0.1",
    "port": 8195,
    "input_dir": "",                 # 空 = 沿用 fork 的 input/（与 gateway 上传目录一致）
    "output_dir": "",                # 空 = 沿用 fork 的 output/（Step1_/Step2_ 前缀历史连续）
    "models": {
        "unet": "NoctQ_V4_int8_convrot.safetensors",
        "clip": "qwen3vl_8b_int8_convrot.safetensors",
        "clip_type": "qwen_image",
        "vae": "qwen_image_2.1_vae_bf16.safetensors",
    },
    "sampler": {                     # workflow.json 两个 KSampler 的公共参数
        "steps": 16, "cfg": 1.0, "sampler_name": "euler",
        "scheduler": "simple", "denoise": 1.0, "resolution": 1080,
    },
    "cache": {"device": "auto", "dtype": "default"},   # QwenImage21Cache 参数
    "quality": DEFAULT_QUALITY,
    "prompts": {"step1_template": "", "negative": ""},  # 由 extract 工具回填，勿手抄
    "default_analysis_file": os.path.join(ROOT, "defaults", "analysis_fallback.json"),
    # 子任务分发（默认自动）：workers 里的远端引擎，谁在线谁参与（/health 探活），
    # 不在线自动全归本机——没有开关。权重 = 按实测速度自适应（scheduler 里 EMA），
    # worker 写 "weight" 可静态覆盖。叶子任务（skip_step1）绝不二次分发（防环）。
    "distribute": {
        "workers": [],               # 例: [{"url": "http://Mac:8195"}]（weight 可选）
        "timeout_s_per_image": 600,  # 远端子任务预算 = 120 + 条数×该值
        "min_styles": 2,             # 造型数低于该值不分发（省一次网络往返）
    },
}


def load(path):
    """读 config.json 并合并默认值；返回 dict。缺关键字段直接抛错（启动期失败优于运行期）。"""
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    if not isinstance(user, dict):
        raise ValueError("config.json 必须是 JSON 对象")
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    if not cfg["comfyui_root"]:
        raise ValueError("config.json 缺 comfyui_root（ComfyUI fork 检出目录）")
    cfg["comfyui_root"] = os.path.abspath(cfg["comfyui_root"])
    if not os.path.isfile(os.path.join(cfg["comfyui_root"], "nodes.py")):
        raise ValueError("comfyui_root 下没有 nodes.py：%s" % cfg["comfyui_root"])
    if not cfg["input_dir"]:
        cfg["input_dir"] = os.path.join(cfg["comfyui_root"], "input")
    if not cfg["output_dir"]:
        cfg["output_dir"] = os.path.join(cfg["comfyui_root"], "output")
    cfg["input_dir"] = os.path.abspath(cfg["input_dir"])
    cfg["output_dir"] = os.path.abspath(cfg["output_dir"])
    if not cfg["prompts"].get("step1_template") or not cfg["prompts"].get("negative"):
        raise ValueError("prompts.step1_template / prompts.negative 为空："
                         "先跑 scripts/extract_from_workflow.py 从 workflow.json 对齐")
    return cfg


def quality_params(cfg, quality, overrides):
    """档位 -> (width, height, steps)；请求里的显式覆盖优先。认不出的档位回落 standard。"""
    q = cfg["quality"].get(quality) or cfg["quality"]["standard"]
    w = int(overrides.get("width") or q["width"])
    h = int(overrides.get("height") or q["height"])
    steps = int(overrides.get("steps") or q["steps"])
    return w, h, steps
