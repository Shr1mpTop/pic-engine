# 对齐依据：workflow.json → 引擎六段式

本文记录引擎与 `outfit_studio/web/workflow.json`（生产工作流）的逐节点映射与全部语义决策。
改 workflow 后：先在 ComfyUI 画布上定稿，再跑 `scripts/extract_from_workflow.py` 校准配置。

## 一、节点映射

| workflow 节点 | 引擎段落 | 备注 |
|---|---|---|
| `LoadImage 470` | `f.LoadImage().load_image(photo)[0]` | 真 LoadImage：EXIF 转置/alpha/webp 行为原样 |
| `OutfitBodyAnalyzer 611` | **不在引擎内** | 生产路径 gateway 入队后自己做云端分析（SQLite 缓存+失败兜底），再把结果注入；引擎只接收 `analysis` |
| `OutfitTemplateApply 670` | `template.apply(analysis, step1_template)` | 语义复刻（30 行纯字符串操作），中文别名表一字不差 |
| `TextEncodeQwenImage21 459:474` | `TextEncodeQwenImage21.execute(clip, p1, negative, vae, 1080, {"image_1": 用户照片})` | 参考图=用户照片；positive/negative 各带 reference_latents |
| `QwenImage21Cache 459:469` | `QwenImage21Cache.execute(unet, "auto", "default")` | 模型加载后包裹一次，两种采样共用 |
| `ComfySwitchNode 459:468 (true)` + `EmptyLatentImage 459:456` | `EmptyLatentImage().generate(w, h, 1)` | switch 恒 true → 永远走空潜空间；TextEncode 的输出 2（参考 latent）不用 |
| `KSampler 459:458` | `KSampler().sample(model, seed1, steps, cfg=1, "euler", "simple", pos, neg, latent, 1.0)` | 参数由 extract 工具回填 config |
| `VAEDecode 459:457` + `SaveImage 528` | `VAEDecode().decode(...)` + `SaveImage().save_images(im, "Step1")` | 命名 `Step1_00001_.png` 由 fork 的 counter 逻辑决定，历史连续 |
| `StartLoop 530 (List)` + `CreateList 531/900` | `for style in styles:` | gateway 已把 sel/preset/custom 解析成 prompt 文本列表传给引擎 |
| `OutfitTemplateApply 626` | `template.apply(analysis, style)` | 每个造型一条模板 |
| `TextEncodeQwenImage21 523:516` | 同上，参考图 = **Step1 基础图** | 两段式的关键：造型图以基础图为参考 |
| `KSampler 523:513` | `sample(..., seed2, ...)` | 同一 job 所有造型共用 seed2（workflow 里循环体只有这一个 KSampler） |
| `VAEDecode 523:512` + `SaveImageAdvanced 525` | `save_images(im, "Step2")` | `SaveImageAdvanced` 的 png/8-bit/sRGB 参数与 `SaveImage` 默认一致，用后者 |
| `TextGenerate 459:500 / 523:522` | **未实现** | 上游 `ComfySwitchNode 459:484 / 523:520` 生产里为 false（增强器关闭）；extract 工具校验这一点 |

## 二、语义决策记录

1. **import 真节点，不复刻**。`nodes.py` 的导入链不触碰 `server.py`
   （latent_preview 只写控制台 ProgressBar）；`comfy.options.args_parsing`
   默认 False，`cli_args` 走 `parse_args([])`，天然支持嵌入式。显存调度
   （TE 8.9G / DiT 6.9G / VAE 0.6G 在 6GB 卡上的换入换出）由
   `comfy.model_management` 全权负责，与服务器进程内逐节点执行同行为。
2. **身材分析归 gateway**。生产里 `OutfitBodyAnalyzer` 节点从不真正执行——
   gateway 在派发前把它替换成固定 JSON（真实分析或兜底值）。引擎契约直接
   收 `analysis`，云端调用/缓存/降级链留在 gateway 一处。
3. **styles 是已解析文本**。gateway `_build_payload` 把 `sN`（styles 表）、
   `c`（自定义文案+头尾包装）、preset 指派全部解析成 prompt 文本列表；
   引擎收到的就是最终模板文本（可含 `{{占位符}}`），不再认识 sel 键。
4. **expected = len(styles) + 1**，与 gateway `prompt_meta` 的 CreateList 计数
   规则一致；styles 空 = 只出 Step1（对应 `simple/0` 循环）。
5. **种子**：请求可显式给 seed1/seed2（gateway 重试可比）；缺省随机 10^15
   （对齐 gateway `randomize_seeds`）。结果回传双种子供上层存档。
6. **输出目录沿用 fork 的 `output/`**：文件名 counter 历史连续，gateway
   `_scan_recover_outputs` 的按前缀扫盘、`/view` 直供逻辑全部免改。
7. **input 目录沿用 fork 的 `input/`**：gateway 上传流（`COMFY_DIR/input`）
   免改；远端 worker 场景引擎提供 multipart 直传（存成 `engine_<ms>_<hex>`）。

## 三、已知差异（相对 ComfyUI 服务器）

| 差异 | 影响 |
|---|---|
| 无 websocket 实时预览帧 | gateway 本就轮询 `/gw/jobs`；无感 |
| 无节点级输出缓存 | 固定管线每单全跑，缓存无意义 |
| 引擎重启丢内存 rid 状态 | 与 ComfyUI 重启清队列同语义，gateway 对账/重派兜底 |
| 无 ComfyUI 网页 UI | 保留 fork 里的 ComfyUI 本体作设计工具即可，两者互不干扰（但**不要同时跑**：显存只有一份，用谁开谁） |

## 四、对齐工具

```
python scripts/extract_from_workflow.py \
    --workflow <outfit_studio>/web/workflow.json --config config.json \
    [--comfyui-root ...] [--port 8195]
```

- 提取：模型三件套（含 clip_type）、采样参数（steps/cfg/sampler/scheduler/denoise/resolution）、
  Step1 模板、负面词；
- 校验：TextGenerate 上游 switch 必须 false，否则拒绝生成；
- 溯源：workflow sha256 + 时间戳写进 `config._aligned`；
- 幂等：只覆写 models/sampler/prompts 三节。
