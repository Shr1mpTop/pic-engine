# Gateway 桥接设计（下一阶段：接入生产）

目标：outfit-gateway 的派发目标从 ComfyUI `/prompt` 换成引擎 `/render`，
**面板与所有 `/gw/*` 接口零改动**（客户端无感）。ComfyUI 本体保留为设计工具。

## 契约翻译（dispatch 时刻，payload -> 引擎参数）

gateway 的 jobs 表在派发前已具备全部信息，无需改 enqueue：

| 引擎参数 | 来源（已有，零新增存储） |
|---|---|
| `photo` | payload 的 LoadImage 节点 `inputs.image`（现有 `photo_of()`） |
| `analysis` | `jobs.analysis` 列（ana_state ready/fallback 时已回写；pending 且过 ANA_DEADLINE 时引擎侧兜底） |
| `styles` | payload 的 StartLoop 列表链：`mode.list` -> CreateList（`inputs.inputN` 索引序）-> xstN 的 `value`；合并节点（xcmN）递归 input0/input1。空列表 = simple/0 = 只出 Step1 |
| `width/height` | payload 的 EmptyLatentImage 节点（patch_quality 已打进去） |
| `steps` | payload 的 KSampler 节点 |
| `seed1/seed2` | payload 两个 KSampler 的 seed（重试可比语义保留） |

## gateway 改动面（最小 diff）

1. 新文件 `engine_bridge.py`（~150 行）：
   - `params_from_payload(payload, analysis)` —— 上表的翻译器
   - `submit(cfg, params) -> rid`（POST /render）
   - `poll(cfg, rid) -> snapshot`（GET /result/{rid}）
   - `ingest_done(jid, user_id, images)` —— 直接调 gateway 现有 `ingest_output_file`（引擎与 gateway 同机，输出落在同一 output/ 目录，按文件名入账即可）
2. `gateway.py` dispatcher 加一个分支：`ENGINE_URL`（环境变量，默认空=走 ComfyUI）
   - 派发：`rid = engine_bridge.submit(...)`，jobs 表 `pid` 存 rid
   - 对账：轮询 `/result/{rid}` 替代 `/queue + /history` 两套；
     `status=done` -> ingest 图片 + 置 done；`error` -> 现有错误路径
   - 掉线：引擎重启（更新）期间连接失败 -> 任务留在原状态，下一轮重试
     （等价于现在 ComfyUI 掉线的行为，客户端无感的实现机制）
3. `gw_status` 的 `comfy` 字段改报引擎 `/health`（版本号顺手进 events 日志）。

## 风险与对策

- **引擎冷启动**（首次模型加载可达数分钟）：面板 comfy 卡语义改为"引擎卡"，
  启动即 import + 惰性加载，首个任务自己等（/render 排队即等待）；
- **双引擎显存冲突**：ComfyUI 与引擎绝不同时跑（6GB 卡只有一份显存），
  面板卡互斥提示；Mac worker 场景天然无冲突；
- **回滚**：`ENGINE_URL` 清空即回 ComfyUI 路径，payload 从未删除（与现行为一致），
  30 秒内可切回。

## 部署顺序（对齐检查单）

1. 备份 outfit.db（面板 gw 卡停机时 Copy-Item，运维手册既有流程）
2. 停 gateway -> 放入 engine_bridge.py + 打补丁 -> 起 gateway（ENGINE_URL 空，行为不变）
3. 开引擎（8195）跑冒烟 -> `ENGINE_URL=http://127.0.0.1:8195` 重启 gateway
4. 面板下一单真实任务验证全链路 -> 观察一轮 -> 面板卡语义改名
5. 运维手册 §新条目：pic-engine 服务、更新流程、回滚开关
