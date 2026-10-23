# -*- coding: utf-8 -*-
"""HTTP 契约。四个端点，全部无鉴权——只许绑 127.0.0.1 / 内网 / Tailscale，
严禁套 Cloudflare 公网隧道（与 ComfyUI API 同一安全定位）。

GET  /health           存活 + 版本 + 队列态（gateway/面板探活用）
POST /render           提交渲染（JSON 或 multipart：远端 worker 直传照片）
GET  /result/{rid}     轮询结果：status/stage/images/seeds
GET  /view             取输出/输入图片（gateway 远端收图用）
"""
import json
import os
import re
import time
import uuid

from aiohttp import web

from . import __version__
from . import bootstrap
from . import config as config_mod

SAFE_NAME = re.compile(r"^[\w.\-]+$")
UPLOAD_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _resolve_params(cfg, data):
    """请求体 -> 引擎参数。photo 必须已存在于 input 目录。"""
    photo = os.path.basename(str(data.get("photo") or ""))
    if not photo:
        raise ValueError("缺 photo")
    if not os.path.isfile(os.path.join(cfg["input_dir"], photo)):
        raise ValueError("照片不在引擎 input 目录：%s" % photo)
    quality = str(data.get("quality") or "standard")
    w, h, steps = config_mod.quality_params(cfg, quality, data)
    return {
        "photo": photo,
        "styles": [str(s) for s in (data.get("styles") or [])],
        "analysis": data.get("analysis"),
        "seed1": data.get("seed1"),
        "seed2": data.get("seed2"),
        "width": w, "height": h, "steps": steps,
        "quality": quality,
    }


def make_app(cfg, runner, device):
    async def health(request):
        return web.json_response({
            "ok": True, "version": __version__, "device": device,
            "stats": runner.stats(),
            "models": cfg["models"],
            "input_dir": cfg["input_dir"], "output_dir": cfg["output_dir"],
            "now": time.time(),
        })

    async def render(request):
        if request.content_type.startswith("multipart/"):
            # 远端 worker：photo_file（二进制）+ job（JSON 字段）
            data, raw, ext = {}, None, ".jpg"
            reader = await request.multipart()
            async for part in reader:
                if part.name == "photo_file":
                    raw = await part.read(decode=False)
                    ext = os.path.splitext(part.filename or "")[1].lower() or ".jpg"
                elif part.name == "job":
                    try:
                        data = json.loads((await part.read()).decode("utf-8-sig"))
                    except (ValueError, UnicodeDecodeError):
                        return web.json_response({"error": "job 字段不是合法 JSON"}, status=400)
            if raw:
                if len(raw) > 32 * 1024 * 1024:
                    return web.json_response({"error": "照片过大（>32MB）"}, status=413)
                if ext not in UPLOAD_EXT:
                    ext = ".jpg"
                name = "engine_%d_%s%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6], ext)
                with open(os.path.join(cfg["input_dir"], name), "wb") as f:
                    f.write(raw)
                data["photo"] = name
        else:
            # utf-8-sig：容忍带 BOM 的 JSON（Windows 工具链常态，实测 PS5.1 直踩）
            raw = await request.read()
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        try:
            params = _resolve_params(cfg, data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        rid = runner.submit(params)
        return web.json_response({"rid": rid})

    async def result(request):
        rid = request.match_info["rid"]
        snap = runner.get(rid)
        if snap is None:
            return web.json_response({"error": "未知任务"}, status=404)
        return web.json_response(snap)

    async def cancel(request):
        rid = request.match_info["rid"]
        st = runner.cancel(rid)
        if st is None:
            return web.json_response({"error": "未知任务"}, status=404)
        return web.json_response({"rid": rid, "status": st})

    async def view(request):
        fn = request.query.get("filename", "")
        typ = request.query.get("type", "output")
        if typ not in ("input", "output") or not SAFE_NAME.match(fn):
            return web.json_response({"error": "非法请求"}, status=400)
        path = os.path.join(cfg["output_dir"] if typ == "output" else cfg["input_dir"], fn)
        if not os.path.isfile(path):
            return web.json_response({"error": "未找到"}, status=404)
        return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post("/render", render)
    app.router.add_get("/result/{rid}", result)
    app.router.add_post("/cancel/{rid}", cancel)
    app.router.add_get("/view", view)
    return app


def run(cfg):
    fork = bootstrap.bootstrap(cfg["comfyui_root"], cfg["input_dir"], cfg["output_dir"],
                               cfg.get("comfy_args"))
    import comfy.model_management as mm
    try:
        device = str(mm.get_torch_device())
    except Exception:  # noqa: BLE001 - 探活信息失败不阻断启动
        device = "unknown"
    from .runner import Runner
    runner = Runner(cfg, fork)
    print("[pic-engine %s] %s:%s  device=%s  fork=%s" % (
        __version__, cfg["host"], cfg["port"], device, cfg["comfyui_root"]), flush=True)
    web.run_app(make_app(cfg, runner, device), host=cfg["host"],
                port=int(cfg["port"]), print=None, access_log=None)
