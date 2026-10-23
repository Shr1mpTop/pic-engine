# -*- coding: utf-8 -*-
"""分发端到端断言（前置：mock worker :8299 与 引擎 :8196（config_test.json）已在跑）。
提交 4 条风格 → 断言：Step1×1 + Step2×4；远端段（style_index=0,1 的 mock 假图）
排在 Step2 前列，本机段（无 style_index 元数据）在后——即收集顺序 = 风格顺序。"""
import glob
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8196"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "..", "ComfyUI", "ComfyUI", "output")
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "..", "ComfyUI", "ComfyUI", "output"))

photo = sys.argv[1]
req = {"photo": photo, "quality": "fast", "seed1": 42, "seed2": 4242,
       "styles": ["人物身材{{body_type}}，{{skin_tone}}，白色T恤牛仔长裤，街头随拍，超高清",
                  "人物身材{{body_type}}，{{skin_tone}}，黑色连衣裙，影棚柔光，全身照，超高清",
                  "人物身材{{body_type}}，{{skin_tone}}，米色风衣，黄昏逆光，街拍，超高清",
                  "人物身材{{body_type}}，{{skin_tone}}，运动套装，健身房环境光，半身照，超高清"]}
body = json.dumps(req).encode()
r = urllib.request.Request(BASE + "/render", data=body,
                           headers={"Content-Type": "application/json"})
rid = json.loads(urllib.request.urlopen(r, timeout=30).read())["rid"]
print("rid =", rid)

for _ in range(150):
    time.sleep(3)
    snap = json.loads(urllib.request.urlopen(BASE + "/result/" + rid, timeout=10).read())
    print("  status=%s stage=%s" % (snap["status"], snap["stage"]))
    if snap["status"] in ("done", "error", "cancelled"):
        break

assert snap["status"] == "done", "未正常完成: %r" % snap.get("error")
step2 = sorted((im for im in snap["images"] if im["filename"].startswith("Step2")),
               key=lambda im: im["filename"])
step1 = [im for im in snap["images"] if im["filename"].startswith("Step1")]
print("Step1=%d Step2=%d" % (len(step1), len(step2)))
assert len(step1) == 1 and len(step2) == 4, "张数不对"

# 自适应切分：远端段（mock，带 style_index）必须按序在前，本机段（无元数据）在尾。
# 切分比例随实测速度 EMA 变化，所以只断言"有序前缀 + None 收尾"，不断言具体条数。
idxs = []
for im in step2:
    from PIL import Image
    path = os.path.join(OUT, im["filename"])
    im_obj = Image.open(path)
    im_obj.load()
    idxs.append(im_obj.text.get("style_index"))
print("Step2 style_index 序列:", idxs)
meta = [int(v) for v in idxs if v is not None]
nones = [v for v in idxs if v is None]
assert meta == sorted(meta) and len(set(meta)) == len(meta) and meta == list(range(len(meta))), \
    "远端段序号必须从 0 连续递增: %s" % meta
assert len(nones) == len(idxs) - len(meta), "None（本机段）必须全部在尾部"
print("==== 分发端到端测试 通过 ✓（远端 %d + 本机 %d）====" % (len(meta), len(nones)))
