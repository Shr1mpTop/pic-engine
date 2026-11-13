# -*- coding: utf-8 -*-
"""单 worker 渲染队列：GPU 串行，对应 ComfyUI 单实例一次跑一单的行为。

状态机 queued -> running -> done|error|cancelled（另含 queued -> cancelled
排队取消、running -> cancelled 段落边界协作取消）；/result 读快照。
引擎重启丢掉内存态（与 ComfyUI 重启清队列一致），上层 gateway 的
held/queued 对账与重派逻辑天然兼容——这就是"客户端无感更新"的底座：
更新引擎 = 拉代码 + 重启进程，任务在 gateway 侧排队等它回来。

并发一致性（DEV-001）：status / started_ts / done_ts / error / 结果字段的
每一次读写都在 self._lock 内进行，_set_status 是唯一状态写入口并校验
跃迁合法性（非法跃迁直接 RuntimeError 暴露，锁下并发不可能触达）。
旧实现 worker 绕锁直写，与 cancel() 的 queued->cancelled 竞态：已取消
任务会被 worker 拉起执行（取消丢失 + 非法跃迁）——本版消除。

取消语义 cancel(rid) 返回 {"rid", "outcome", "status"}，outcome：
  queued-cancelled   排队取消：状态即终态 cancelled，worker 取到后丢弃不执行
  running-cancelled  运行中取消：只置 cancel_requested，段落边界生效
  already-done       任务已在终态（done/error/cancelled），取消无效
  not-found          rid 未知或其完成任务已被清扫

内存治理：完成任务（done/error/cancelled）按 job_ttl 秒 + job_max_done
条数上限清扫（submit / 每单完成 / sweep() 触发，最旧优先），清理量计入
stats()["cleaned"]。queued/running 永不触碰。
"""
import queue
import threading
import time
import traceback
import uuid

from .pipeline import Renderer, RenderCancelled

_QUEUED = "queued"
_RUNNING = "running"
_TERMINAL = ("done", "error", "cancelled")

# 合法跃迁表：终态一律封死。cancel 排队取消走 queued->cancelled，
# 协作取消走 running->cancelled；不存在其它入边。
_LEGAL_NEXT = {
    "queued": {"running", "cancelled"},
    "running": {"done", "error", "cancelled"},
    "done": set(),
    "error": set(),
    "cancelled": set(),
}


class Job:
    __slots__ = ("rid", "params", "status", "stage", "images", "error",
                 "seed1", "seed2", "expected", "enqueued_ts", "started_ts", "done_ts",
                 "cancel_requested", "hist")

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
        self.hist = ["queued"]         # 实际走过的状态轨迹（诊断/测试用，随清扫一起释放）

    def snapshot(self):
        snap = {k: getattr(self, k) for k in (
            "rid", "status", "stage", "images", "error", "seed1", "seed2",
            "expected", "enqueued_ts", "started_ts", "done_ts")}
        snap["cancel_requested"] = self.cancel_requested   # 新增字段：运行中取消的可见旗标
        return snap


class Runner:
    def __init__(self, cfg, fork, renderer=None):
        self.cfg = cfg
        self.fork = fork
        self._q = queue.Queue()
        self._jobs = {}
        self._lock = threading.Lock()
        # 完成任务清扫参数：保留秒数 + 最大条数（0 条 = 完成即清）
        self._job_ttl = float(cfg.get("job_ttl", 3600.0))
        self._job_max_done = max(0, int(cfg.get("job_max_done", 500)))
        self._cleaned_total = 0          # 累计清扫完成态任务数
        self._last_sweep = 0             # 最近一次清扫删除数
        self._last_sweep_ts = None
        self._residue_dropped = 0        # worker 丢弃的取消残留队列条目数
        # 测试接缝：注入替身 renderer 则跳过真管线构造（生产不传，行为不变）
        self._renderer = renderer
        self._thread = threading.Thread(target=self._loop, name="render-worker", daemon=True)
        self._thread.start()

    # ---- 状态机（唯一写入口；调用方必须已持 self._lock） ----

    def _set_status(self, job, status):
        """校验并落一个状态跃迁，顺带盖 started_ts / done_ts。非法跃迁抛 RuntimeError。"""
        allowed = _LEGAL_NEXT.get(job.status)
        if not allowed or status not in allowed:
            raise RuntimeError("rid=%s 非法状态跃迁 %s -> %s" % (job.rid, job.status, status))
        now = time.time()
        if status == _RUNNING:
            job.started_ts = now
        elif status in _TERMINAL:
            job.done_ts = now
        job.status = status
        job.hist.append(status)
        return now

    # ---- 对外接口（HTTP 层用） ----

    def submit(self, params):
        job = Job(params)
        with self._lock:
            self._jobs[job.rid] = job          # 先入账本再入队：worker 取到 rid 时账本必有
            self._sweep_locked()
        self._q.put(job.rid)
        return job.rid

    def get(self, rid):
        with self._lock:
            job = self._jobs.get(rid)
            return job.snapshot() if job else None

    def cancel(self, rid):
        """取消任务，返回 {"rid", "outcome", "status"}（语义见模块 docstring）。
        排队取消立即作废（worker 不会执行）；运行中为协作取消，段落边界生效。"""
        with self._lock:
            job = self._jobs.get(rid)
            if job is None:
                return {"rid": rid, "outcome": "not-found", "status": None}
            if job.status == _QUEUED:
                job.error = "已取消"
                job.cancel_requested = True
                self._set_status(job, "cancelled")     # 排队取消：与 worker 的取队同锁，绝不竞速
                outcome = "queued-cancelled"
            elif job.status == _RUNNING:
                job.cancel_requested = True            # worker 在段落边界轮询该旗标
                outcome = "running-cancelled"
            else:
                outcome = "already-done"               # done/error/cancelled：终态不可取消
            return {"rid": rid, "outcome": outcome, "status": job.status}

    def stats(self):
        """队列治理指标。全部计数在锁内从状态派生，恒满足：
        queue + running + done + error + cancelled == total == len(_jobs)。
        queue   = 真实待执行数（排队取消的不计——旧实现取 _q.qsize() 会把取消残留计入）
        running = 活动任务数（与 queue 分离的两个指标）
        qsize   = 物理队列长度（含待 worker 丢弃的取消残留，仅供诊断）
        cleaned = 累计清扫的完成任务数（TTL/上限内存治理的清理统计）"""
        with self._lock:
            counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "cancelled": 0}
            for j in self._jobs.values():
                counts[j.status] = counts.get(j.status, 0) + 1
            return {
                "queue": counts["queued"],
                "running": counts["running"],
                "done": counts["done"],
                "error": counts["error"],
                "cancelled": counts["cancelled"],
                "total": len(self._jobs),
                "qsize": self._q.qsize(),
                "residue_dropped": self._residue_dropped,
                "cleaned": self._cleaned_total,
                "last_sweep": self._last_sweep,
                "last_sweep_ts": self._last_sweep_ts,
            }

    def sweep(self):
        """手动触发一次完成任务清扫（TTL + 上限），返回本次删除数。"""
        with self._lock:
            return self._sweep_locked()

    # ---- worker ----

    def _loop(self):
        if self._renderer is None:
            self._renderer = Renderer(self.cfg, self.fork)
        while True:
            rid = self._q.get()
            with self._lock:
                job = self._jobs.get(rid)
                if job is None or job.status != _QUEUED:
                    # 取消残留（排队取消后条目仍会流出队列）等一律丢弃不执行：
                    # "排队取消绝不被渲染"由这把锁保证，丢弃量计入 residue_dropped。
                    if job is not None:
                        self._residue_dropped += 1
                    continue
                self._set_status(job, _RUNNING)        # 与 cancel() 同锁串行，无 TOCTOU 窗口
            try:
                res = self._renderer.render(
                    job.params, progress=lambda s, j=job: self._stage(j, s),
                    should_cancel=lambda j=job: self._cancel_requested(j),
                    on_images=lambda imgs, j=job: self._set_images(j, imgs))
                with self._lock:
                    job.images = list(res["images"])
                    job.seed1 = res["seed1"]
                    job.seed2 = res["seed2"]
                    job.expected = res["expected"]
                    self._set_status(job, "done")
            except RenderCancelled:
                with self._lock:
                    self._set_status(job, "cancelled")
                    job.error = "已取消"
            except Exception as e:  # noqa: BLE001 - 单任务失败不许带崩引擎
                with self._lock:
                    self._set_status(job, "error")
                    job.error = "%s: %s" % (type(e).__name__, e)
                try:
                    traceback.print_exc()
                except OSError:      # 打印通道坏了（坏句柄）也不能让 worker 退出
                    pass
            finally:
                with self._lock:
                    if job.status not in _TERMINAL:
                        # 兜底：任何漏标终态的路径不许把任务挂死在 running
                        self._set_status(job, "error")
                        job.error = job.error or "worker 兜底终态"
                    self._sweep_locked()               # 每单完成即清扫，_jobs 有界

    def _cancel_requested(self, job):
        with self._lock:
            return job.cancel_requested

    def _stage(self, job, stage):
        with self._lock:
            if job.status == _RUNNING:
                job.stage = stage

    def _set_images(self, job, images):
        """增量出图：pipeline 每存一张就回报一次，/result 的 images 随之增长。"""
        with self._lock:
            if job.status == _RUNNING:
                job.images = list(images)

    # ---- 完成任务清扫（持锁调用） ----

    def _sweep_locked(self):
        """清理完成态任务：超 TTL 全删；仍超 job_max_done 再删最旧的。
        queued/running 永不触碰。返回本次删除数，累计进 cleaned 统计。"""
        now = time.time()
        finished = sorted((j for j in self._jobs.values() if j.status in _TERMINAL),
                          key=lambda j: j.done_ts or 0.0)
        drop = set()
        for j in finished:
            if now - (j.done_ts or 0.0) >= self._job_ttl:
                drop.add(j.rid)
        keep = [j for j in finished if j.rid not in drop]
        excess = len(keep) - self._job_max_done
        if excess > 0:
            drop.update(j.rid for j in keep[:excess])
        for rid in drop:
            del self._jobs[rid]
        if drop:
            self._cleaned_total += len(drop)
            self._last_sweep = len(drop)
            self._last_sweep_ts = now
        return len(drop)
