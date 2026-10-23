# -*- coding: utf-8 -*-
"""单 worker 渲染队列：GPU 串行，对应 ComfyUI 单实例一次跑一单的行为。

状态机 queued -> running -> done|error；/result 读快照。
引擎重启丢掉内存态（与 ComfyUI 重启清队列一致），上层 gateway 的
held/queued 对账与重派逻辑天然兼容——这就是"客户端无感更新"的底座：
更新引擎 = 拉代码 + 重启进程，任务在 gateway 侧排队等它回来。
"""
import queue
import threading
import time
import traceback
import uuid

from .pipeline import Renderer, RenderCancelled


class Job:
    __slots__ = ("rid", "params", "status", "stage", "images", "error",
                 "seed1", "seed2", "expected", "enqueued_ts", "started_ts", "done_ts",
                 "cancel_requested")

    def __init__(self, params):
        self.rid = "r" + uuid.uuid4().hex[:10]
        self.params = params
        self.status = "queued"
        self.stage = ""
        self.images = []
        self.error = None
        self.seed1 = None
        self.seed2 = None
        self.expected = 0
        self.enqueued_ts = time.time()
        self.started_ts = None
        self.done_ts = None
        self.cancel_requested = False

    def snapshot(self):
        return {k: getattr(self, k) for k in (
            "rid", "status", "stage", "images", "error", "seed1", "seed2",
            "expected", "enqueued_ts", "started_ts", "done_ts")}


class Runner:
    def __init__(self, cfg, fork):
        self.cfg = cfg
        self.fork = fork
        self._q = queue.Queue()
        self._jobs = {}
        self._lock = threading.Lock()
        self._renderer = None
        self._thread = threading.Thread(target=self._loop, name="render-worker", daemon=True)
        self._thread.start()

    # ---- 对外接口（HTTP 层用） ----

    def submit(self, params):
        job = Job(params)
        with self._lock:
            self._jobs[job.rid] = job
        self._q.put(job.rid)
        return job.rid

    def get(self, rid):
        with self._lock:
            job = self._jobs.get(rid)
            return job.snapshot() if job else None

    def cancel(self, rid):
        """协作取消：排队中立即作废；运行中在段落边界生效（引擎存活）。"""
        with self._lock:
            job = self._jobs.get(rid)
            if job is None:
                return None
            if job.status == "queued":
                job.status = "cancelled"
                job.error = "已取消"
                job.done_ts = time.time()
            elif job.status == "running":
                job.cancel_requested = True
            return job.status

    def stats(self):
        with self._lock:
            jobs = list(self._jobs.values())
        running = sum(1 for j in jobs if j.status == "running")
        return {"queue": self._q.qsize(), "running": running,
                "total": len(jobs)}

    # ---- worker ----

    def _loop(self):
        if self._renderer is None:
            self._renderer = Renderer(self.cfg, self.fork)
        while True:
            rid = self._q.get()
            with self._lock:
                job = self._jobs.get(rid)
            if job is None or job.status == "cancelled":
                continue
            job.status = "running"
            job.started_ts = time.time()
            try:
                res = self._renderer.render(
                    job.params, progress=lambda s, j=job: self._stage(j, s),
                    should_cancel=lambda j=job: j.cancel_requested)
                job.images = res["images"]
                job.seed1 = res["seed1"]
                job.seed2 = res["seed2"]
                job.expected = res["expected"]
                job.status = "done"
            except RenderCancelled:
                job.status = "cancelled"
                job.error = "已取消"
            except Exception as e:  # noqa: BLE001 - 单任务失败不许带崩引擎
                job.status = "error"
                job.error = "%s: %s" % (type(e).__name__, e)
                traceback.print_exc()
            finally:
                job.done_ts = time.time()

    def _stage(self, job, stage):
        with self._lock:
            if job.status == "running":
                job.stage = stage
