# Outfit Engine

穿搭工坊的**无服务器出图引擎**：把 ComfyUI fork 当库用（不起 server、无队列、无 JSON 图），
把生产工作流固化为一条六段式模板管线，对上层暴露一个极简 HTTP 契约。
渲染结果与 ComfyUI 跑同一工作流**逐位一致**——因为节点实现就是 fork 里的真类，直接调用。

```
手机面板 ──> outfit-gateway(8188) ──> outfit-engine(8195) ──> ComfyUI fork（import，无进程）
                任务/账号/图库              六段式模板管线          models/ + comfy.* + nodes.py
                (outfit.db)               (photo+params→图)      （kernel/显存调度全复用）
```

## 为什么

- 生产路径是**固定管线 + 参数**，从来不需要"通用图执行服务器"；
- 去掉 server 进程 = 去掉端口开关、双层队列、/history 对账、prompt_id 映射；
- ComfyUI 保留为**设计工具**：画布上调工作流，定稿后用对齐工具同步进引擎配置。

## 快速开始（Windows / 笔记本 apex）

```bat
cd E:\github\outfit-engine
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
> Mac/标准 venv 用 `python -m outfit_engine ...` 等价。

## HTTP 契约

| 端点 | 说明 |
|---|---|
| `GET /health` | `{ok, version, device, stats:{queue,running,total}, models, ...}` |
| `POST /render` | JSON：`{photo, styles:[模板...], analysis?, quality?, seed1?, seed2?, width?, height?, steps?}` → `{rid}`；multipart：`photo_file`(二进制)+`job`(JSON) 供远端 worker 直传照片 |
| `GET /result/{rid}` | `{status: queued\|running\|done\|error, stage, images:[{filename,subfolder,type}], seed1, seed2, expected, error}` |
| `GET /view?filename=&type=output` | 取输出图（远端收图） |

- `photo` 必须已在引擎 input 目录（默认 = fork 的 `input/`，与 gateway 上传目录同一处）；
- `analysis` 缺省用 `defaults/analysis_fallback.json`（生产路径由 gateway 注入真实分析结果）；
- `styles` 为空 = 只出 Step1 基础图（expected=1），与 workflow 的 `simple/0` 循环等价；
- 输出文件名沿用 ComfyUI 规则（`Step1_00001_.png`、`Step2_00001_.png`...），gateway 的按前缀扫盘补账逻辑直接可用；
- 一次任务的 seed1（Step1）/ seed2（Step2 全部造型共用）不传则随机（10^15 量级，与 gateway 一致），结果里回传。

## 与 workflow.json 的对齐

配置不手抄：`scripts/extract_from_workflow.py` 从生产 workflow.json 提取
模型三件套、采样参数、Step1 模板、负面词，并把 workflow 的 sha256 写入
`config._aligned` 溯源。workflow 改版后重跑一遍即可校准。
完整节点映射与语义决策记录见 **[docs/alignment.md](docs/alignment.md)**。

质量三档（`fast 480×640/20`、`standard 960×1280/16`、`best 1440×1920/16`）
对齐自 outfit-gateway `compose_api.py` 的 QUALITY 表，只改 latent 尺寸与步数。

## Mac 部署

```bash
git clone <本仓库> ~/outfit-engine && cd ~/outfit-engine
python3 scripts/extract_from_workflow.py \
    --workflow ~/ComfyUI/ComfyUI/custom_nodes/outfit_studio/web/workflow.json \
    --config config.json --comfyui-root ~/ComfyUI/ComfyUI
PY=~/ComfyUI/venv/bin/python ./scripts/start_mac.sh   # host 改 0.0.0.0 供 LAN 调度
```

前提（两台机器已天然满足）：同一 ComfyUI fork、同三件套模型
（此前已做 SHA256 级核对一致）、`outfit_studio/web/workflow.json`。
**Mac 不需要安装任何 custom node**——业务逻辑（分析/模板替换）已编译进引擎。

## 更新（客户端无感）

引擎更新 = `git pull` + 重启进程。重启期间 gateway 的任务停在队列里，引擎回来
自动续跑——面板侧零感知。`/health` 带 `version` 字段，gateway 记录引擎版本。
引擎重启会丢内存中的 rid 状态（与 ComfyUI 重启清队列同语义），上层对账兜底。

## 安全边界

无鉴权，**只许**绑 `127.0.0.1` / 内网 / Tailscale。严禁套 Cloudflare 公网隧道。
