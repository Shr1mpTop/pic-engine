# -*- coding: utf-8 -*-
"""对齐工具：从 outfit_studio/web/workflow.json 提取引擎配置，防手抄漂移。

提取（全部取"第一个"出现，与生产 workflow 的两段重复块一致）：
  models.unet/clip/clip_type/vae <- 第一个 UNETLoader / CLIPLoader / VAELoader
  sampler.*                      <- 第一个 KSampler + 第一个 TextEncodeQwenImage21.resolution
  prompts.step1_template         <- 唯一一个模板为字符串常量的 OutfitTemplateApply（节点 670；
                                    另一个 626 的模板来自循环列表，不是它）
  prompts.negative               <- 第一个 TextEncodeQwenImage21.negative_prompt

同时校验 TextGenerate 增强器路径的上游 switch 必须是 false（引擎未实现该路径），
并把 workflow 的 sha256 写进 config._aligned 做溯源。

用法：
  python scripts/extract_from_workflow.py --workflow <workflow.json> \
      --config config.json [--comfyui-root E:/github/ComfyUI/ComfyUI] [--port 8195]
  # 生成样例：--config config.example.json --comfyui-root <任意占位>
幂等：只覆写 models/sampler/prompts 三节，config 其余字段原样保留。
纯标准库，任何 Python 3.8+ 可跑。
"""
import argparse
import hashlib
import json
import os
import sys
import time

ENGINE_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8195,
    "cache": {"device": "auto", "dtype": "default"},
}


def extract(wf):
    unet = clip = clip_type = vae = None
    ks = te = None
    step1_template = negative = None
    for nid, node in wf.items():
        ct = node.get("class_type")
        ins = node.get("inputs") or {}
        if ct == "UNETLoader" and unet is None:
            unet = ins.get("unet_name")
        elif ct == "CLIPLoader" and clip is None:
            clip, clip_type = ins.get("clip_name"), ins.get("type")
        elif ct == "VAELoader" and vae is None:
            vae = ins.get("vae_name")
        elif ct == "KSampler" and ks is None:
            ks = ins
        elif ct == "TextEncodeQwenImage21" and te is None:
            te = ins
        elif ct == "OutfitTemplateApply" and isinstance(ins.get("template"), str) \
                and step1_template is None:
            step1_template = ins.get("template")
    missing = [k for k, v in {
        "UNETLoader": unet, "CLIPLoader": clip, "VAELoader": vae,
        "KSampler": ks, "TextEncodeQwenImage21": te,
        "OutfitTemplateApply(字符串模板)": step1_template}.items() if not v]
    if missing:
        raise SystemExit("workflow 里找不到：%s" % "、".join(missing))
    negative = te.get("negative_prompt") or ""
    sampler = {
        "steps": ks.get("steps"), "cfg": ks.get("cfg"),
        "sampler_name": ks.get("sampler_name"), "scheduler": ks.get("scheduler"),
        "denoise": ks.get("denoise", 1.0), "resolution": te.get("resolution", 1024),
    }
    return {"models": {"unet": unet, "clip": clip, "clip_type": clip_type, "vae": vae},
            "sampler": sampler,
            "prompts": {"step1_template": step1_template, "negative": negative}}


def check_textgen_switches(wf):
    """TextGenerate 的上游 ComfySwitchNode 必须为 false（增强器路径未实现）。"""
    problems = []
    for nid, node in wf.items():
        if node.get("class_type") != "TextGenerate":
            continue
        for sid, sw in wf.items():
            if sw.get("class_type") != "ComfySwitchNode":
                continue
            on_true = sw.get("inputs", {}).get("on_true")
            if isinstance(on_true, list) and on_true and on_true[0] == nid \
                    and sw.get("inputs", {}).get("switch") is True:
                problems.append("节点 %s 的增强器开关（%s）是 true" % (nid, sid))
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--comfyui-root")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    with open(args.workflow, "r", encoding="utf-8") as f:
        wf = json.load(f)
    raw = json.dumps(wf, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()[:16]
    extracted = extract(wf)

    problems = check_textgen_switches(wf)
    if problems:
        print("!! 增强器开关异常（引擎未实现该路径，需先在 workflow 里关掉）：")
        for p in problems:
            print("   - %s" % p)
        sys.exit(1)

    cfg = dict(ENGINE_DEFAULTS)
    if os.path.isfile(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    if args.comfyui_root:
        cfg["comfyui_root"] = args.comfyui_root
    if args.port:
        cfg["port"] = args.port
    for section in ("models", "sampler", "prompts"):
        cfg.setdefault(section, {}).update(extracted[section])
    cfg["_aligned"] = {"workflow": os.path.abspath(args.workflow),
                       "workflow_sha256": sha, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(args.config, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("已对齐 -> %s" % args.config)
    print("  模型: %s / %s(%s) / %s" % (extracted["models"]["unet"],
          extracted["models"]["clip"], extracted["models"]["clip_type"],
          extracted["models"]["vae"]))
    print("  采样: %s 步 / cfg=%s / %s+%s / 分辨率基准 %s" % (
        extracted["sampler"]["steps"], extracted["sampler"]["cfg"],
        extracted["sampler"]["sampler_name"], extracted["sampler"]["scheduler"],
        extracted["sampler"]["resolution"]))
    print("  文案: step1 模板 %d 字 / 负面词 %d 字 / workflow sha256 %s" % (
        len(extracted["prompts"]["step1_template"]),
        len(extracted["prompts"]["negative"]), sha))


if __name__ == "__main__":
    main()
