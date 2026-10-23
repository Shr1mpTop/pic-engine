# -*- coding: utf-8 -*-
"""Pic Engine —— 穿搭工坊的无服务器出图引擎。

把 ComfyUI fork 当库用（无 server、无队列、无 JSON 图），把生产工作流
固化为一条六段式模板管线，对上层（outfit-gateway）暴露一个极简 HTTP 契约：
    POST /render {photo, styles, analysis, quality, ...} -> {rid}
    GET  /result/{rid} -> {status, images, error}
语义与 outfit_studio/web/workflow.json 逐节点对齐，对齐依据见 docs/alignment.md。
"""

__version__ = "0.2.0"
