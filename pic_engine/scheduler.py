# -*- coding: utf-8 -*-
"""子任务调度器（方案一）：Step1 本机 → 造型段按权重切给远端引擎并发 → 收集重编号。

设计要点：
- gateway 零改动：仍是一单一个 rid、N+1 张图；分发完全藏在引擎内部。
- Step1 只在协调者本机渲染一次（跨硬件非确定性 + 依赖关系决定），PNG 字节
  发给远端（无损；与"用户照片本就从盘加载"同一质量口径）。
- 远端跑同一份引擎代码的叶子任务（skip_step1=true，绝不二次分发——防环）。
- 本地份额延迟落盘（只留张量），收集阶段按风格原始顺序统一写盘：
  计数器顺序 = 风格顺序，gateway 的 _img_sort_key 映射无缝。
- 失败语义：派发前 /health 剔除不健康 worker（全部不健康 = 退化单机）；
  远端子任务失败/超时 → 该段风格由本机兜底补渲染（GPU 此时已空闲）。
- 取消：段落边界 + 轮询循环检查，取消向远端子任务透传。
"""
import json
import os
import random
import threading
import time
import urllib.request
import uuid

from .pipeline import RenderCancelled

_BOUNDARY = "picengine%s" % uuid.uuid4().hex[:16]


# ---------------- 实测速度（EMA 自适应权重，本地持久化） ----------------

_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "distribute_state.json")
_speeds = {}         # {label: 秒/张}；label = "local" 或 worker url（懒加载自状态文件）


def _load_speeds():
    global _speeds
    if not _speeds:
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _speeds = json.load(f)
        except Exception:  # noqa: BLE001 - 没有历史=大家都从权重 1 起步
            _speeds = {}
    return _speeds


def _record_speed(label, n_images, elapsed):
    if n_images <= 0 or elapsed <= 0:
        return
    sp = round(elapsed / n_images, 2)
    speeds = _load_speeds()
    old = speeds.get(label)
    speeds[label] = sp if old is None else round(0.6 * old + 0.4 * sp, 2)
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(speeds, f)
    except OSError:
        pass


def _weights_for(cfg, workers):
    """本机 + 各远端的切分权重：worker 配置写了 weight 用静态值（人工覆盖），
    否则按实测速度自适应（EMA 秒/张，快→权重大）；没测过 = 1。本机永远参与。"""
    speeds = _load_speeds()
    out = []
    for label in ["local"] + [w["url"] for w in workers]:
        static = next((w.get("weight") for w in workers if w["url"] == label), None)
        if static:
            out.append(float(static))
        elif label in speeds and float(speeds[label]) > 0:
            out.append(1.0 / max(float(speeds[label]), 0.1))
        else:
            out.append(1.0)
    return out


# ---------------- 远端 HTTP（worker 线程内同步调用） ----------------

def _url(worker, path):
    return worker["url"].rstrip("/") + path


def workers_of(cfg):
    """配置里的远端引擎列表（剔除本机自环）。空 = 永远本机单机。"""
    dist = cfg.get("distribute") or {}
    workers = [w for w in (dist.get("workers") or []) if w.get("url")]
    own = "%s:%s" % (cfg.get("host"), cfg.get("port"))
    return [w for w in workers if own not in w["url"]]


def should_distribute(cfg, params):
    """没有开关：workers 里谁在线（/health）谁参与，全不在线=自动本机。"""
    workers = workers_of(cfg)
    if not workers or params.get("skip_step1"):
        return False
    styles = params.get("styles") or []
    min_styles = int((cfg.get("distribute") or {}).get("min_styles", 2))
    return len(styles) >= max(1, min_styles)


def _get_json(worker, path, timeout):
    with urllib.request.urlopen(_url(worker, path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def healthy_workers(workers, timeout=3):
    """并发探活，返回在线 worker 列表；全挂 = 空列表（调用方退化单机）。"""
    out, lock = [], threading.Lock()

    def probe(w):
        try:
            with urllib.request.urlopen(_url(w, "/health"), timeout=timeout) as r:
                if r.status == 200:
                    with lock:
                        out.append(w)
        except Exception:  # noqa: BLE001 - 探活失败=这台不参与本轮
            pass

    ts = [threading.Thread(target=probe, args=(w,)) for w in workers]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout + 1)
    return out


def _split(total, weights):
    """weights[0] = 本机。返回各份条数（和 = total）；整除余数归本机（尾部）。"""
    wsum = sum(weights) or 1.0
    ns = [int(total * w / wsum) for w in weights]
    ns[0] += total - sum(ns)
    return ns


def _multipart(photo_name, photo_bytes, job_bytes):
    parts = []
    for name, value in (("job", job_bytes.decode("utf-8")),):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (_BOUNDARY, name, value)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"photo_file\"; "
                  "filename=\"%s\"\r\nContent-Type: image/png\r\n\r\n"
                  % (_BOUNDARY, photo_name)).encode("utf-8"))
    parts.append(photo_bytes)
    parts.append(("\r\n--%s--\r\n" % _BOUNDARY).encode("utf-8"))
    return b"".join(parts)


def _post_render(worker, photo_bytes, job, timeout):
    body = _multipart("step1.png", photo_bytes, json.dumps(job, ensure_ascii=False).encode("utf-8"))
    req = urllib.request.Request(
        _url(worker, "/render"), data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % _BOUNDARY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode("utf-8"))
    rid = res.get("rid")
    if not rid:
        raise RuntimeError("worker 拒绝: %s" % str(res)[:120])
    return rid


def _poll_done(worker, rid, n_images, timeout_s_per_image, progress, should_cancel,
               label, deadline):
    """轮询远端子任务到终态；超时/失败/取消分别抛/返回。返回 images 清单。"""
    budget = 120 + timeout_s_per_image * max(1, n_images)
    end = min(deadline, time.monotonic() + budget)
    while True:
        if should_cancel and should_cancel():
            raise RenderCancelled()
        if time.monotonic() > end:
            raise TimeoutError("远端子任务超时（%ds）" % budget)
        try:
            snap = _get_json(worker, "/result/" + rid, 10)
        except Exception:  # noqa: BLE001 - 网络抖动轮内重试
            time.sleep(2)
            continue
        st = snap.get("status")
        if st == "done":
            return snap.get("images") or []
        if st in ("error", "cancelled"):
            raise RuntimeError("远端%s: %s" % (st, str(snap.get("error"))[:120]))
        progress("fanout[%s] %s" % (label, snap.get("stage") or st))
        time.sleep(2)


def _download(worker, filename, timeout=60):
    with urllib.request.urlopen(_url(worker, "/view?filename=%s&type=output"
                                     % urllib.request.quote(filename)), timeout=timeout) as r:
        return r.read()


def _cancel_remote(worker, rid):
    try:
        req = urllib.request.Request(_url(worker, "/cancel/" + rid), method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:  # noqa: BLE001 - 取消失败不影响主流程
        pass


# ---------------- 协调者 ----------------

def coordinate(renderer, params, progress, should_cancel):
    """Step1 本机 → styles 切分派发 → 收集重编号。返回与 Renderer.render 同构的 dict。"""
    cfg = renderer.cfg
    dist = cfg.get("distribute") or {}
    timeout_per_image = int(dist.get("timeout_s_per_image", 600))

    styles = [str(s) for s in (params.get("styles") or [])]
    workers = healthy_workers(workers_of(cfg))
    if not workers:                      # 全部不健康：退化单机（现状行为）
        return renderer.render(params, progress, should_cancel)

    counts = _split(len(styles), _weights_for(cfg, workers))        # [本机, 远端...]
    ranges, pos = [], 0
    for n in counts[1:]:                 # 远端先拿前面的风格段
        ranges.append((pos, pos + n))
        pos += n
    local_range = (pos, len(styles))
    for worker, (a, b) in zip(workers, ranges):
        progress("fanout: %s 分到 %d 条" % (worker["url"], b - a))

    def check_cancel():
        if should_cancel and should_cancel():
            raise RenderCancelled()

    # ---- 子任务① Step1：本机 ----
    renderer._ensure_models(progress)
    check_cancel()
    photo = os.path.basename(str(params["photo"]))
    photo_img = renderer.f.LoadImage().load_image(photo)[0]
    im1, images, seed1 = renderer.render_step1(params, photo_img, progress, check_cancel)
    analysis = renderer.analysis_of(params)
    seed2 = params.get("seed2")
    if not isinstance(seed2, int) or seed2 < 0:
        seed2 = random.randrange(10 ** 15)
    width, height = int(params["width"]), int(params["height"])
    steps = int(params["steps"])

    # Step1 PNG 字节（无损回读刚落盘的基础图，作为远端 Step2 的参考输入）
    step1_filename = images[0]["filename"]
    step1_bytes = open(os.path.join(cfg["output_dir"], step1_filename), "rb").read()

    # ---- 子任务②③ 远端派发（网络线程），本机份额同时开跑 ----
    deadline = time.monotonic() + 120 + timeout_per_image * (len(styles) + 1)
    chunks = []                          # [{styles, pngs?, err}]
    for worker, (a, b) in zip(workers, ranges):
        chunks.append({"worker": worker, "label": worker["url"].split("//")[-1][:24],
                       "styles": styles[a:b], "a": a, "b": b,
                       "rid": None, "pngs": None, "err": None})

    def run_remote(ch):
        try:
            job = {"skip_step1": True, "styles": ch["styles"], "analysis": analysis,
                   "seed2": seed2, "width": width, "height": height, "steps": steps}
            t0 = time.monotonic()
            ch["rid"] = _post_render(ch["worker"], step1_bytes, job, 30)
            pngs = _poll_done(ch["worker"], ch["rid"], len(ch["styles"]),
                              timeout_per_image,
                              lambda s: progress("fanout[%s] %s" % (ch["label"], s)),
                              should_cancel, ch["label"], deadline)
            ch["pngs"] = [_download(ch["worker"], im["filename"]) for im in pngs]
            _record_speed(ch["worker"]["url"], len(ch["styles"]),
                          time.monotonic() - t0)
        except RenderCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 失败段由本机兜底
            ch["err"] = "%s: %s" % (type(e).__name__, e)

    remotes = [c for c in chunks if c["styles"]]
    for ch in remotes:
        ch["thread"] = threading.Thread(target=run_remote, args=(ch,), daemon=True)
        ch["thread"].start()

    # 本机份额：尾部风格段，先落盘到暂存前缀（Step2hold），收集阶段改名进 Step2
    # 序列（解码张量绝不跨网络等待持有——见 render_styles 注释）
    local_styles = styles[local_range[0]:local_range[1]]
    local_files, t_local = [], time.monotonic()
    if local_styles:
        local_files = renderer.render_styles(
            im1, local_styles, analysis, seed2, steps, width, height,
            progress, check_cancel, tag="local_style",
            save_prefix="Step2hold")
        _record_speed("local", len(local_styles), time.monotonic() - t_local)

    deadline_hit = False
    for ch in remotes:
        ch["thread"].join(timeout=max(1, int(deadline - time.monotonic())))
        if ch["thread"].is_alive():
            ch["err"] = "远端超时"
            if ch["rid"]:
                _cancel_remote(ch["worker"], ch["rid"])
            deadline_hit = True
    check_cancel()

    # ---- 收集：按风格原始顺序统一落盘（远端字节 + 本机兜底张量） ----
    def alloc_png(data):
        folder, filename, counter, subfolder, _ = renderer.f.folder_paths.get_save_image_path(
            "Step2", cfg["output_dir"], width, height)
        file = "%s_%05d_.png" % (filename, counter)
        with open(os.path.join(folder, file), "wb") as f:
            f.write(data)
        return {"filename": file, "subfolder": subfolder, "type": "output"}

    for ch in remotes:
        if ch["err"] or ch["pngs"] is None or len(ch["pngs"]) != len(ch["styles"]):
            progress("fanout[%s] 失败（%s），本机兜底 %d 条"
                     % (ch["label"], ch["err"] or "产出不符", len(ch["styles"])))
            fallback = renderer.render_styles(
                im1, ch["styles"], analysis, seed2, steps, width, height,
                progress, check_cancel, tag="fallback_%d" % ch["a"])
            ch["local_files"] = fallback
        else:
            ch["local_files"] = [alloc_png(data) for data in ch["pngs"]]

    # 汇总：远端段在前（风格序靠前），本机段（改名）在尾部；段内已按风格序
    ordered = list(images)                                   # Step1 在首位
    for ch in remotes:
        ordered += ch["local_files"]
    for hold in local_files:
        ordered += [_rename_to_step2(renderer, cfg, hold["filename"], width, height)]

    return {"images": ordered, "seed1": seed1, "seed2": seed2,
            "expected": len(styles) + 1,
            "distributed": True, "degraded": deadline_hit}


def _rename_to_step2(renderer, cfg, hold_filename, width, height):
    """Step2hold_XXXXX_.png -> Step2_YYYYY_.png（顺序计数器，os.replace 零转码）。"""
    src = os.path.join(cfg["output_dir"], hold_filename)
    folder, filename, counter, subfolder, _ = renderer.f.folder_paths.get_save_image_path(
        "Step2", cfg["output_dir"], width, height)
    dst = "%s_%05d_.png" % (filename, counter)
    os.replace(src, os.path.join(folder, dst))
    return {"filename": dst, "subfolder": subfolder, "type": "output"}
