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
"""
import gc
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
        self.low_ram = bool(cfg.get("low_ram"))

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

    # ---- low_ram 顺序驻留：单阶段只驻留当前要用的模型 ----
    # 12GB 级内存机器（如 MacBook Air M2）：三件套同时驻留 16.4GB > RAM →
    # 整机换页，一步都跑不完。此模式按阶段装载/释放：编码(TE+VAE≈9.5G) →
    # 采样(DiT 6.9G) → 解码(VAE 0.65G)，峰值永远低于总 RAM。

    def _load_te(self):
        if self._clip is None:
            self._clip = self.f.CLIPLoader().load_clip(
                self.cfg["models"]["clip"],
                self.cfg["models"].get("clip_type", "qwen_image"), "default")[0]

    def _load_vae(self):
        if self._vae is None:
            self._vae = self.f.VAELoader().load_vae(self.cfg["models"]["vae"])[0]

    def _load_dit(self):
        if self._unet is None:
            unet = self.f.UNETLoader().load_unet(self.cfg["models"]["unet"], "default")[0]
            self._unet = self.f.QwenImage21Cache.execute(
                unet, self.cfg["cache"]["device"], self.cfg["cache"]["dtype"])[0]

    def _release(self, keep=None):
        """释放除 keep 外的模型引用并交还显存/内存（keep: "te"/"dit"/"vae"）。"""
        if keep != "te":
            self._clip = None
        if keep != "dit":
            self._unet = None
        if keep != "vae":
            self._vae = None
        gc.collect()
        import comfy.model_management as mm
        mm.unload_all_models()
        mm.soft_empty_cache()
        gc.collect()

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
                      progress=lambda s: None, check_cancel=lambda: None, tag="style"):
        """每条风格一张（同批共用 seed2）。返回 images 清单（顺序 = styles 顺序）。"""
        images = []
        negative = self.cfg["prompts"]["negative"]
        for i, style in enumerate(styles):
            check_cancel()
            progress("%s_%d_of_%d" % (tag, i + 1, len(styles)))
            p2 = template.apply(analysis, style)
            pos2, neg2 = self._encode(p2, negative, im1)
            lat2 = self._sample(pos2, neg2, seed2, steps, width, height)
            im2 = self.f.VAEDecode().decode(vae=self._vae, samples=lat2)[0]
            images += self._save(im2, "Step2")
        return images

    # ---- 主流程 ----

    def _render_low_ram(self, params, progress, check_cancel):
        """low_ram 编排：TE 编码 → 卸 TE → DiT 采样 → 卸 DiT → VAE 解码。
        同阶段批处理（多造型一次编码/一次采样），把模型切换次数压到最少；
        参考图（Step2 用 Step1 解码图）以张量形态跨阶段传递。"""
        photo = os.path.basename(str(params["photo"]))
        styles = [str(s) for s in (params.get("styles") or [])]
        analysis = self.analysis_of(params)
        negative = self.cfg["prompts"]["negative"]
        steps = int(params["steps"])
        width, height = int(params["width"]), int(params["height"])
        seed2 = params.get("seed2")
        if not isinstance(seed2, int) or seed2 < 0:
            seed2 = random.randrange(10 ** 15)
        photo_img = self.f.LoadImage().load_image(photo)[0]

        def encode_all(prompts, ref):
            self._load_te()
            self._load_vae()
            conds = []
            for i, p in enumerate(prompts):
                check_cancel()
                progress("low_ram_encode_%d_of_%d" % (i + 1, len(prompts)))
                conds.append(self._encode(template.apply(analysis, p), negative, ref))
            self._release(keep="vae")
            return conds

        def sample_all(conds, seed):
            self._load_dit()
            lats = []
            for i, (pos, neg) in enumerate(conds):
                check_cancel()
                progress("low_ram_sample_%d_of_%d" % (i + 1, len(conds)))
                lats.append(self._sample(pos, neg, seed, steps, width, height))
            self._release(keep="vae")
            return lats

        def decode_all(lats):
            ims = []
            for lat in lats:
                check_cancel()
                ims.append(self.f.VAEDecode().decode(vae=self._vae, samples=lat)[0])
            return ims

        seed1 = params.get("seed1")
        if not isinstance(seed1, int) or seed1 < 0:
            seed1 = random.randrange(10 ** 15)

        progress("low_ram_step1")
        cond1 = encode_all([self.cfg["prompts"]["step1_template"]], photo_img)[0]
        lat1 = sample_all([cond1], seed1)[0]
        im1 = decode_all([lat1])[0]
        images = self._save(im1, "Step1")

        if styles:
            conds = encode_all(styles, im1)     # 参考 = Step1 基础图
            lats = sample_all(conds, seed2)
            for im in decode_all(lats):
                images += self._save(im, "Step2")
        self._release()
        return {"images": images, "seed1": seed1, "seed2": seed2,
                "expected": len(styles) + 1}

    def render(self, params, progress=lambda s: None, should_cancel=None):
        """params: photo/styles/analysis/seed1/seed2/width/height/steps。
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

        if self.low_ram:
            return self._render_low_ram(params, progress, check_cancel)

        self._ensure_models(progress)
        check_cancel()
        photo_img = self.f.LoadImage().load_image(photo)[0]

        im1, images, seed1 = self.render_step1(params, photo_img, progress, check_cancel)
        images += self.render_styles(im1, styles, analysis, seed2, steps,
                                     width, height, progress, check_cancel)
        return {"images": images, "seed1": seed1, "seed2": seed2,
                "expected": len(styles) + 1}
