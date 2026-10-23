# -*- coding: utf-8 -*-
"""六段式渲染管线 —— outfit_studio/web/workflow.json 的无图直译。

节点映射（完整依据见 docs/alignment.md）：
  LoadImage 470                     -> 加载用户照片
  OutfitTemplateApply 670           -> template.apply（基础图模板 + 分析值）
  TextEncodeQwenImage21 459:474     -> 编码（参考图 = 用户照片，resolution 1080）
  QwenImage21Cache 459:469          -> unet 一次性包裹（device auto / dtype default）
  EmptyLatentImage 459:456 (switch) -> 空潜空间 (w, h)
  KSampler 459:458                  -> 采样 seed1 / steps / cfg=1 / euler / simple
  VAEDecode 459:457 + SaveImage 528 -> Step1_*.png
  StartLoop 530 (List 模式)         -> for style in styles
  OutfitTemplateApply 626           -> template.apply（造型模板 + 分析值）
  TextEncodeQwenImage21 523:516     -> 编码（参考图 = Step1 基础图）
  KSampler 523:513                  -> 采样 seed2（同 job 所有造型共用一个种子）
  VAEDecode 523:512 + SaveImageAdv 525 -> Step2_*.png
TextGenerate 增强器路径（459:484 / 523:520）生产里 switch=false，引擎不实现。

子任务模式（skip_step1=true，多机分发时远端跑的形态）：photo 就是基础图，
跳过 Step1 直接进造型段——让远端引擎无需知道完整工作流。
"""
import json
import os
import random
import time

from . import template


class RenderCancelled(Exception):
    """协作取消：should_cancel 回调在段落边界检查并抛出，整单作废、引擎存活。"""


class Renderer:
    """模型三件套惰性加载 + 单任务渲染。只在 worker 线程使用（GPU 串行）。
    显存调度完全交给 fork 的 comfy.model_management：TE/DiT/VAE 之间的换入换出
    与 ComfyUI 服务器进程内逐节点执行时的行为一致。"""

    def __init__(self, cfg, fork):
        self.cfg = cfg
        self.f = fork
        self._unet = None
        self._clip = None
        self._vae = None
        self.models_loaded_ts = None

    # ---- 模型 ----

    def _ensure_models(self, progress):
        if self._unet is not None:
            return
        progress("loading_models")
        t0 = time.monotonic()
        unet = self.f.UNETLoader().load_unet(self.cfg["models"]["unet"], "default")[0]
        unet = self.f.QwenImage21Cache.execute(
            unet, self.cfg["cache"]["device"], self.cfg["cache"]["dtype"])[0]
        clip = self.f.CLIPLoader().load_clip(
            self.cfg["models"]["clip"], self.cfg["models"].get("clip_type", "qwen_image"),
            "default")[0]
        vae = self.f.VAELoader().load_vae(self.cfg["models"]["vae"])[0]
        self._unet, self._clip, self._vae = unet, clip, vae
        self.models_loaded_ts = time.time()
        progress("models_loaded %.0fs" % (time.monotonic() - t0))

    # ---- 段落原语（每段对应一个真节点，参数原样） ----

    def _encode(self, prompt, negative, ref_image):
        out = self.f.TextEncodeQwenImage21.execute(
            clip=self._clip, prompt=prompt, negative_prompt=negative,
            vae=self._vae, resolution=self.cfg["sampler"]["resolution"],
            images={"image_1": ref_image})
        return out[0], out[1]          # (positive, negative)；输出 2 的参考 latent 不用（switch=true 走空潜空间）

    def _sample(self, positive, negative, seed, steps, width, height):
        latent = self.f.EmptyLatentImage().generate(width, height, 1)[0]
        return self.f.KSampler().sample(
            model=self._unet, seed=seed, steps=steps,
            cfg=self.cfg["sampler"]["cfg"],
            sampler_name=self.cfg["sampler"]["sampler_name"],
            scheduler=self.cfg["sampler"]["scheduler"],
            positive=positive, negative=negative,
            latent_image=latent,
            denoise=self.cfg["sampler"]["denoise"])[0]

    def _save(self, images, prefix):
        res = self.f.SaveImage().save_images(images=images, filename_prefix=prefix)
        return (res.get("ui") or {}).get("images") or []

    def analysis_of(self, params):
        a = params.get("analysis")
        if a is None or a == "":
            with open(self.cfg["default_analysis_file"], "r", encoding="utf-8") as f:
                a = json.load(f)
        return a

    # ---- 段落：Step1（基础图） ----

    def render_step1(self, params, photo_img, progress, check_cancel):
        """Step1 段。返回 (im1, images, seed1)。"""
        seed1 = params.get("seed1")
        if not isinstance(seed1, int) or seed1 < 0:
            seed1 = random.randrange(10 ** 15)     # 与 gateway randomize_seeds 同量级
        width, height = int(params["width"]), int(params["height"])
        steps = int(params["steps"])
        negative = self.cfg["prompts"]["negative"]
        progress("step1_encode")
        p1 = template.apply(self.analysis_of(params), self.cfg["prompts"]["step1_template"])
        pos, neg = self._encode(p1, negative, photo_img)
        check_cancel()
        progress("step1_sample")
        lat1 = self._sample(pos, neg, seed1, steps, width, height)
        progress("step1_decode")
        im1 = self.f.VAEDecode().decode(vae=self._vae, samples=lat1)[0]
        return im1, self._save(im1, "Step1"), seed1

    # ---- 段落：Step2（造型，参考 = 基础图） ----

    def render_styles(self, im1, styles, analysis, seed2, steps, width, height,
                      progress=lambda s: None, check_cancel=lambda: None, tag="style",
                      save_prefix="Step2"):
        """每条风格一张（同批共用 seed2）。返回 images 清单（顺序 = styles 顺序）。
        save_prefix：落盘前缀。多机分发时本机份额用暂存前缀（Step2hold）先落盘，
        收集阶段再改名成 Step2 统一编号——解码张量绝不跨长 I/O 持有（aimdo
        内存管理下延迟持有会损坏，实测 (1,1,480,4) TypeError）。"""
        images = []
        negative = self.cfg["prompts"]["negative"]
        for i, style in enumerate(styles):
            check_cancel()
            progress("%s_%d_of_%d" % (tag, i + 1, len(styles)))
            p2 = template.apply(analysis, style)
            pos2, neg2 = self._encode(p2, negative, im1)
            lat2 = self._sample(pos2, neg2, seed2, steps, width, height)
            im2 = self.f.VAEDecode().decode(vae=self._vae, samples=lat2)[0]
            images += self._save(im2, save_prefix)
        return images

    # ---- 主流程 ----

    def render(self, params, progress=lambda s: None, should_cancel=None):
        """params: photo/styles/analysis/seed1/seed2/width/height/steps/skip_step1。
        返回 {images, seed1, seed2, expected}；任何失败抛异常，引擎进程保持存活。
        should_cancel：段落边界轮询，真则抛 RenderCancelled（gateway 取消/看门狗用）。"""
        def check_cancel():
            if should_cancel and should_cancel():
                raise RenderCancelled()

        photo = os.path.basename(str(params["photo"]))
        styles = [str(s) for s in (params.get("styles") or [])]
        analysis = self.analysis_of(params)
        seed2 = params.get("seed2")
        if not isinstance(seed2, int) or seed2 < 0:
            seed2 = random.randrange(10 ** 15)
        width = int(params["width"])
        height = int(params["height"])
        steps = int(params["steps"])

        self._ensure_models(progress)
        check_cancel()
        photo_img = self.f.LoadImage().load_image(photo)[0]

        if params.get("skip_step1"):
            # 子任务模式：photo 就是协调者发来的基础图，只跑造型段
            images = self.render_styles(photo_img, styles, analysis, seed2, steps,
                                        width, height, progress, check_cancel,
                                        tag="style")
            return {"images": images, "seed1": None, "seed2": seed2,
                    "expected": len(styles)}

        im1, images, seed1 = self.render_step1(params, photo_img, progress, check_cancel)
        images += self.render_styles(im1, styles, analysis, seed2, steps,
                                     width, height, progress, check_cancel)
        return {"images": images, "seed1": seed1, "seed2": seed2,
                "expected": len(styles) + 1}
