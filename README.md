# Pic Engine

穿搭工坊的**无服务器出图引擎**：把 ComfyUI fork 当库用（不起 server、无队列、无 JSON 图），
把生产工作流固化为一条六段式模板管线，对上层暴露一个极简 HTTP 契约。
渲染结果与 ComfyUI 跑同一工作流**逐位一致**——因为节点实现就是 fork 里的真类，直接调用。

```
手机面板 ──> outfit-gateway(8188) ──> pic-engine(8195) ──> ComfyUI fork（import，无进程）
                任务/账号/图库              六段式模板管线          models/ + comfy.* + nodes.py
                (outfit.db)               (photo+params→图)      （kernel/显存调度全复用）
```

## 为什么

- 生产路径是**固定管线 + 参数**，从来不需要"通用图执行服务器"；
- 去掉 server 进程 = 去掉端口开关、双层队列、/history 对账、prompt_id 映射；
- ComfyUI 保留为**设计工具**：画布上调工作流，定稿后用对齐工具同步进引擎配置。

## 快速开始（Windows / 笔记本 apex）

```bat
cd E:\github\pic-engine
set PY=E:\github\ComfyUI\python_embeded\python.exe

:: 1) 从生产 workflow.json 对齐配置（生成 config.json，机器本地文件，不入库）
%PY% scripts\extract_from_workflow.py ^
    --workflow E:\github\ComfyUI\ComfyUI\custom_nodes\outfit_studio\web\workflow.json ^
    --config config.json --comfyui-root E:\github\ComfyUI\ComfyUI

:: 2) 冒烟（不加载模型：校验配置/fork/模型文件/目录可写）
%PY% run.py check --config config.json

:: 3) 常驻服务（或直接 scripts\start_windows.bat）
%PY% run.py serve --config config.json

:: 4) 本机同步出一张试试（调试用，走完整管线）
%PY% run.py render --config config.json ^
    --photo cyz_1.jpg --style "人物身材{{body_type}}，{{skin_tone}}，超高清" --quality fast
```

> Windows 嵌入式解释器必须走 `run.py`（`._pth` 隔离模式不含 cwd）；
> Mac/标准 venv 用 `python -m pic_engine ...` 等价。

## HTTP 契约

| 端点 | 说明 |
|---|---|
| `GET /health` | `{ok, version, device, stats:{queue,running,total}, models, ...}` |
| `POST /render` | JSON：`{photo|ref, unit_key?, styles:[模板...], analysis?, quality?, seed1?, seed2?, width?, height?, steps?}` → `{rid}`；multipart：`photo_file`(二进制)+`job`(JSON) 供远端 worker 直传照片 |
| `GET /result/{rid}` | `{status: queued\|running\|done\|error\|cancelled, stage, images:[{filename,subfolder,type}], seed1, seed2, expected, error}` |
| `POST /cancel/{rid}` | 协作取消：排队中立即作废；运行中在段落边界生效（引擎存活） |
| `GET /view?filename=&type=output` | 取输出图（远端收图） |

- `photo`（input 目录，整单/Step1 单元）与 `ref`（output 目录的 Step1 基础图，Step2
  单元）**二选一**；ref 以 `文件名 [output]` 注解走 fork 的真 LoadImage，预处理零复刻；
- `unit_key` = 断点续跑的幂等锚点：给了就确定性落盘 `Step1_/Step2_{unit_key}.png`，
  **渲染前文件已在 → 直接返回（零计算跳过）**；写入走 tmp+os.replace 原子落地，
  "文件存在 ⇒ 完整成品"这条不变量供上层跳过/认领逻辑依赖；
- `analysis` 缺省用 `defaults/analysis_fallback.json`（生产路径由 gateway 注入真实分析结果）；
- `styles` 为空 = 只出 Step1 基础图（expected=1），与 workflow 的 `simple/0` 循环等价；
  带 `ref` 时只渲染 styles 里的造型（单元模式，expected=单数）；
- 不带 `unit_key` 的调用（`run.py render`、手工调试）保持旧行为：输出按 ComfyUI
  规则计数命名（`Step1_00001_.png`...）；
- 一次任务的 seed1（Step1）/ seed2（Step2 全部造型共用）不传则随机（10^15 量级，与
  gateway 一致），结果里回传。生产路径由 gateway 在单元物化时刻冻结种子，重试可重现。

## 断点续跑（v0.4.0 起）

任务清单不在引擎里，在 outfit-gateway 的 `job_units` 表（一张成品图 = 一个原子单元：
u0 = Step1 基础图，u1..N = 逐造型，idx 序即依赖序）。引擎于是可以保持无状态：
重启/崩溃丢 rid 只损失正在画的那一张，已完成单元靠确定性文件名直接认领，
未完成/失败单元由 gateway 原地重派。引擎自身只需要 `unit_key` 这一个配合点。

## 与 workflow.json 的对齐

配置不手抄：`scripts/extract_from_workflow.py` 从生产 workflow.json 提取
模型三件套、采样参数、Step1 模板、负面词，并把 workflow 的 sha256 写入
`config._aligned` 溯源。workflow 改版后重跑一遍即可校准。
完整节点映射与语义决策记录见 **[docs/alignment.md](docs/alignment.md)**。

质量三档（`fast 480×640/20`、`standard 960×1280/16`、`best 1440×1920/16`）
对齐自 outfit-gateway `compose_api.py` 的 QUALITY 表，只改 latent 尺寸与步数。

## Mac 部署

```bash
git clone https://github.com/Shr1mpTop/pic-engine.git && cd pic-engine
cp config.example.json config.json
#   改两处：comfyui_root 指向 Mac 的 ComfyUI fork 目录；host 改 0.0.0.0（供 LAN 调度）
PY=~/ComfyUI/venv/bin/python ./scripts/start_mac.sh   # 用跑 ComfyUI 的那个解释器
```

预检（不加载模型）：`$PY -m pic_engine check --config config.json`

要点：

- **配置对齐走仓库**：`config.example.json` 就是对齐产物（`_aligned` 带 workflow
  sha256 + 时间戳溯源）。PC 端 workflow 改版后跑 extract 工具重新生成并提交，
  Mac `git pull` 即同步——**Mac 不需要 outfit_studio / workflow.json**；
- **Mac 不需要安装任何 custom node**：业务逻辑（分析注入、模板替换）已编译进引擎；
- ⚠️ **内存门槛**：整套权重 int8 常驻 ≈16.4GB（TE 8.9 + DiT 6.9 + VAE 0.6），且
  convrot 在 Apple MPS 无原生核（逐层反量化=CPU 模拟）。**实测 12GB 统一内存的
  MacBook Air M2 会整机换页（swap 吃满、采样停滞、损耗 SSD）→ 不能当 worker**；
  可用门槛 ≥32GB 内存，或改用更小的模型组合；
- bootstrap 在 Mac 上自动退回经典 ModelPatcher（comfy-aimdo 仅 Win/Linux，
  `init()` 因不支持的操作系统返回 False，`_enable_dynamic_vram` 据此跳过），
  无需任何平台条件配置；
- 前提：同一 ComfyUI fork、同三件套模型（两机已做 SHA256 级核对一致）。

## 更新（客户端无感）

- 引擎更新 = `git pull` + 重启进程（**面板 engine 卡开/关**，或 `run.py serve`）。
  重启期间 gateway 的任务停在队列里，引擎回来自动续跑——面板侧零感知；
- `/health` 带 `version` 字段，gateway 记录引擎版本；
- 引擎重启会丢内存中的 rid 状态（与 ComfyUI 重启清队列同语义），上层对账兜底
  （零产出自动重排 / 部分产出保留成图）。

## 安全边界

无鉴权，**只许**绑 `127.0.0.1` / 内网 / Tailscale。严禁套 Cloudflare 公网隧道。
