# -*- coding: utf-8 -*-
"""单元模式离线测试：stub fork 替身跑通 pipeline 的 base/style/幂等跳过/整单路径。
不加载真模型（_ensure_models 置空），只验证编排逻辑与确定性落盘。"""
import json
import os
import shutil
import sys
import tempfile

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pic_engine.pipeline import Renderer  # noqa: E402

TMP = tempfile.mkdtemp(prefix="pic_units_")
CALLS = {"encode": 0, "load": [], "save_counter": 0}


class LoadImage:
    def load_image(self, name):
        CALLS["load"].append(name)
        return (torch.zeros((1, 8, 8, 3)), None)


class TextEncodeQwenImage21:
    @classmethod
    def execute(cls, clip, prompt, negative_prompt, vae, resolution, images):
        CALLS["encode"] += 1
        CALLS.setdefault("prompts", []).append(prompt)
        return (prompt, negative_prompt)


class EmptyLatentImage:
    def generate(self, width, height, batch):
        return (torch.zeros((batch, 4, 8, 8)),)


class KSampler:
    def sample(self, model, seed, steps, cfg, sampler_name, scheduler,
               positive, negative, latent_image, denoise):
        CALLS.setdefault("seeds", []).append(seed)
        return (latent_image,)


class VAEDecode:
    def decode(self, vae, samples):
        return (torch.rand((1, 8, 8, 3)),)


class SaveImage:
    def save_images(self, images, filename_prefix):
        CALLS["save_counter"] += 1
        return {"ui": {"images": [{"filename": "%s_00001_.png" % filename_prefix,
                                   "subfolder": "", "type": "output"}]}}


class Fork:
    pass


def make_cfg():
    out = os.path.join(TMP, "out")
    os.makedirs(out, exist_ok=True)
    return {"output_dir": out, "input_dir": os.path.join(TMP, "in"),
            "prompts": {"step1_template": "身材{{body_type}}，超高清", "negative": "neg"},
            "sampler": {"cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                        "denoise": 1.0, "resolution": 1080},
            "models": {}, "cache": {}, "low_ram": False}


f = Fork()
f.LoadImage = LoadImage
f.TextEncodeQwenImage21 = TextEncodeQwenImage21
f.EmptyLatentImage = EmptyLatentImage
f.KSampler = KSampler
f.VAEDecode = VAEDecode
f.SaveImage = SaveImage

r = Renderer(make_cfg(), f)
r._unet, r._clip, r._vae = object(), object(), object()   # 模型已就绪
r._ensure_models = lambda progress: None

ok = True


def check(name, cond):
    global ok
    ok = ok and cond
    print("%s %s" % ("✓" if cond else "✗", name))


def base_params():
    return {"photo": "p.jpg", "ref": "", "unit_key": "t_u0", "styles": [],
            "analysis": {"body_type": "高挑"}, "seed1": 42, "seed2": None,
            "width": 64, "height": 64, "steps": 2}


# 1) base 单元：渲染 + 确定性落盘
res = r.render(base_params())
p1 = os.path.join(r.cfg["output_dir"], "Step1_t_u0.png")
check("base 单元落盘确定性文件名", res["images"][0]["filename"] == "Step1_t_u0.png"
      and os.path.isfile(p1) and res["expected"] == 1)
check("base 单元模板吃到了分析值", CALLS["prompts"][-1] == "身材高挑，超高清")
check("base 单元用冻结种子", CALLS["seeds"][-1] == 42)

# 2) 幂等重跑：文件已在 → 零计算跳过
enc0 = CALLS["encode"]
res2 = r.render(base_params())
check("幂等跳过：不重跑、同文件", res2["images"][0]["filename"] == "Step1_t_u0.png"
      and CALLS["encode"] == enc0)

# 3) style 单元：ref 走 LoadImage [output] 注解，模板替换
enc0 = CALLS["encode"]
res3 = r.render({"photo": "", "ref": "Step1_t_u0.png", "unit_key": "t_u1",
                 "styles": ["风格{{skin_tone}}"], "analysis": {"skin_tone": "冷白"},
                 "seed1": None, "seed2": 7, "width": 64, "height": 64, "steps": 2})
p3 = os.path.join(r.cfg["output_dir"], "Step2_t_u1.png")
check("style 单元落盘 + ref 注解正确",
      res3["images"][0]["filename"] == "Step2_t_u1.png" and os.path.isfile(p3)
      and CALLS["load"][-1] == "Step1_t_u0.png [output]" and res3["expected"] == 1)
check("style 单元共用 seed2", CALLS["seeds"][-1] == 7 and CALLS["encode"] == enc0 + 1)

# 4) style 单元幂等重跑
enc0 = CALLS["encode"]
r.render({"photo": "", "ref": "Step1_t_u0.png", "unit_key": "t_u1",
          "styles": ["风格{{skin_tone}}"], "analysis": {}, "seed1": None, "seed2": 7,
          "width": 64, "height": 64, "steps": 2})
check("style 单元幂等跳过", CALLS["encode"] == enc0)

# 5) 常规整单（无 unit_key）：走 SaveImage，expected = styles+1
enc0 = CALLS["encode"]
res5 = r.render({"photo": "p.jpg", "ref": "", "unit_key": "", "styles": ["a", "b"],
                 "analysis": {}, "seed1": 1, "seed2": 2, "width": 64, "height": 64,
                 "steps": 2})
check("整单路径不变", res5["expected"] == 3 and res5["images"][0]["filename"]
      == "Step1_00001_.png" and CALLS["encode"] == enc0 + 3)

# 6) 原子落盘无 .tmp 残留
leftovers = [n for n in os.listdir(r.cfg["output_dir"]) if n.endswith(".tmp")]
check("无 .tmp 残留", not leftovers)

print("==== 单元模式测试 %s ====" % ("通过 ✓" if ok else "失败 ✗"))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok else 1)
