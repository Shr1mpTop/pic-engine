# -*- coding: utf-8 -*-
r"""Runner 并发状态一致性 / 取消三态 / 内存清理 离线测试（DEV-001）。

【解释器要求】必须用 ComfyUI 内嵌解释器跑（pic_engine.runner 导入 pipeline -> torch，
系统 Python 3.14 无 torch，导入即失败）：

    E:\github\ComfyUI\python_embeded\python.exe _test_runner_state.py

【确定性】不连 ComfyUI、不连网络、不加载真模型（Renderer 用 FakeRenderer 注入）；
所有等待均为"有界条件轮询 / Event 同步点"，不依赖固定 sleep 时序，须 5 连跑全绿。

【合法状态跃迁】queued->running->done | queued->running->error（=failed） |
queued->running->cancelled | queued->cancelled（=pending->cancelled）；
状态串取值与线上契约一致：queued/running/done/error/cancelled。
"""
import os
import random
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pic_engine.pipeline import RenderCancelled  # noqa: E402
from pic_engine.runner import Runner  # noqa: E402

# 输出管道可能是 GBK（pwsh/CI 直管道）：钉死 UTF-8，✓/✗ 与中文在任何管道下不炸
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

TERMINAL = ("done", "error", "cancelled")
_LEGAL_PAIRS = {("queued", "running"), ("queued", "cancelled"),
                ("running", "done"), ("running", "error"), ("running", "cancelled")}
_LEGAL_FULL = {("queued", "running", "done"), ("queued", "running", "error"),
               ("queued", "running", "cancelled"), ("queued", "cancelled")}

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print("%s %s%s" % ("✓" if cond else "✗", name, ("  [%s]" % detail) if detail and not cond else ""))


class FakeRenderer:
    """替身渲染器：params["gate"] 存在则阻塞到测试放行（放行后先查协作取消），
    mode=="fail" 抛 ValueError；execs/tag 记录执行次数，entered 事件供测试确定性同步。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.execs = {}

    def render(self, params, progress=lambda s: None, should_cancel=None, on_images=None):
        tag = params["tag"]
        with self.lock:
            self.execs[tag] = self.execs.get(tag, 0) + 1
        if params.get("entered"):
            params["entered"].set()
        gate = params.get("gate")
        if gate is not None:
            gate.wait(timeout=30)          # 有界：测试忘放行也不会挂死整个进程
        if should_cancel is not None and should_cancel():
            raise RenderCancelled()
        if params.get("mode") == "fail":
            raise ValueError("boom")
        return {"images": [{"filename": "%s.png" % tag, "subfolder": "", "type": "output"}],
                "seed1": params.get("seed1"), "seed2": params.get("seed2"), "expected": 1}

    def exec_count(self, tag):
        with self.lock:
            return self.execs.get(tag, 0)


def cfg_base():
    return {"job_ttl": 3600.0, "job_max_done": 500}   # 测试期内不触发 TTL 清扫


def wait_event(ev, timeout=30):
    if not ev.wait(timeout):
        raise AssertionError("等待事件超时 %.0fs" % timeout)


def wait_settled(runner, timeout=120):
    """等所有已提交任务尘埃落定（有界轮询）：物理队列清空 + 无排队 + 无运行中。
    与统计口径解耦（清扫会削减 done 计数，故不用 done 数判断）。"""
    dl = time.time() + timeout
    while time.time() < dl:
        st = runner.stats()
        if st["qsize"] == 0 and st["queue"] == 0 and st["running"] == 0:
            return st
        time.sleep(0.002)
    raise AssertionError("任务未在 %.0fs 内全部终态：%s" % (timeout, runner.stats()))


def wait_snapshot(runner, rid, status, timeout=30):
    dl = time.time() + timeout
    snap = None
    while time.time() < dl:
        snap = runner.get(rid)
        if snap and snap["status"] == status:
            return snap
        time.sleep(0.002)
    raise AssertionError("rid=%s 未在 %.0fs 内到达 %s（实际 %s）"
                         % (rid, timeout, status, snap))


def hist_detail(runner, rid, expect=None):
    """状态轨迹合法性 -> (ok, detail)：首态 queued、逐对跃迁合法、全路径属于
    合法集合、末态=快照状态（可选：等于期望路径）。"""
    job = runner._jobs.get(rid)
    snap = runner.get(rid)
    if job is None or snap is None:
        return False, "job 已不在 _jobs（被清扫?）"
    hist = tuple(job.hist)
    good = (hist[0] == "queued"
            and all(p in _LEGAL_PAIRS for p in zip(hist, hist[1:]))
            and hist in _LEGAL_FULL
            and hist[-1] == snap["status"])
    if expect is not None:
        good = good and hist == tuple(expect)
    return good, "hist=%s status=%s" % (hist, snap["status"])


def check_hist(name, runner, rid, expect=None):
    good, detail = hist_detail(runner, rid, expect)
    check(name, good, detail)


def check_hist_all(name, runner, rids, expect=None):
    bad = [(rid, d) for rid in rids for good, d in (hist_detail(runner, rid, expect),) if not good]
    check(name, not bad, str(bad[:3]))


def check_invariant(name, st, n_jobs):
    good = (st["queue"] + st["running"] + st["done"] + st["error"] + st["cancelled"]
            == st["total"] == n_jobs) and st["qsize"] >= st["queue"]
    check(name, good, str(st))


# ============ 1) 并发提交 100 任务：无非法状态跃迁 + 统计一致 ============
r1 = Runner(cfg_base(), None, renderer=FakeRenderer())
params_list = [{"tag": "c%d" % i, "seed1": i} for i in range(100)]
with ThreadPoolExecutor(max_workers=20) as ex:
    rids1 = list(ex.map(lambda p: r1.submit(p), params_list))
st = wait_settled(r1)
check("100 并发提交 rid 唯一", len(set(rids1)) == 100)
check("100 任务全部 done", st["done"] == 100 and st["running"] == 0 and st["queue"] == 0, str(st))
check_hist_all("无非法状态跃迁（逐任务 hist 校验）", r1, rids1,
               expect=("queued", "running", "done"))
check("每个任务恰好执行一次",
      all(r1._renderer.exec_count("c%d" % i) == 1 for i in range(100)))
check_invariant("统计一致 queued+running+done+error+cancelled==total==len(_jobs)", st, len(r1._jobs))

# ============ 2) 排队取消：worker 从未执行 ============
r2 = Runner(cfg_base(), None, renderer=FakeRenderer())
gate = threading.Event()
entered = threading.Event()
ra = r2.submit({"tag": "A", "gate": gate, "entered": entered})
wait_event(entered)                    # worker 已阻塞在 A.render 内 -> 后续提交必然排队
rb = r2.submit({"tag": "B"})
check("B 尚在排队", r2.get(rb)["status"] == "queued")
res = r2.cancel(rb)
check("排队取消 outcome=queued-cancelled",
      res["outcome"] == "queued-cancelled" and res["status"] == "cancelled", str(res))
snap = r2.get(rb)
check("排队取消立即终态（status/done_ts/error）",
      snap["status"] == "cancelled" and snap["done_ts"] is not None and snap["error"] == "已取消")
check("排队取消重复取消=already-done", r2.cancel(rb)["outcome"] == "already-done")
gate.set()
st = wait_settled(r2)
check("排队取消的任务从未被 worker 执行", r2._renderer.exec_count("B") == 0)
check("取消残留 rid 被队列丢弃并计数", st["residue_dropped"] >= 1 and st["qsize"] == 0, str(st))
check("queue 指标不含已取消任务", st["queue"] == 0, str(st))
check("阻塞任务放行后正常完成", r2.get(ra)["status"] == "done")
check_hist("排队取消 hist 合法", r2, rb, expect=("queued", "cancelled"))

# ============ 3) 运行中取消：running->cancelled 且结果可查询 ============
r3 = Runner(cfg_base(), None, renderer=FakeRenderer())
gate3 = threading.Event()
entered3 = threading.Event()
rc = r3.submit({"tag": "C", "gate": gate3, "entered": entered3})
wait_event(entered3)
check("C 进入 running", r3.get(rc)["status"] == "running")
res = r3.cancel(rc)
check("运行中取消 outcome=running-cancelled 且状态仍 running",
      res["outcome"] == "running-cancelled" and res["status"] == "running", str(res))
check("取消旗标在快照可见（cancel_requested 新字段）",
      r3.get(rc)["cancel_requested"] is True)
gate3.set()                            # 放行后 FakeRenderer 立即查协作取消 -> RenderCancelled
snap = wait_snapshot(r3, rc, "cancelled")
check("运行中取消终态（cancelled/error/done_ts/started_ts）",
      snap["error"] == "已取消" and snap["done_ts"] is not None and snap["started_ts"] is not None)
check("C 恰好执行一次（协作取消发生在段落边界）", r3._renderer.exec_count("C") == 1)
check("已终态任务再取消=already-done", r3.cancel(rc)["outcome"] == "already-done")
check_hist("运行中取消 hist 合法", r3, rc, expect=("queued", "running", "cancelled"))

# ============ 4) 取消语义补全：not-found / 已完成取消 ============
check("未知 rid 取消=not-found", r3.cancel("r不存在的任务")["outcome"] == "not-found")
rd = r3.submit({"tag": "D"})
wait_snapshot(r3, rd, "done")
res = r3.cancel(rd)
check("已完成任务取消=already-done 且状态不变",
      res["outcome"] == "already-done" and res["status"] == "done", str(res))

# ============ 5) 失败路径：running->error，引擎存活 ============
rfail = r3.submit({"tag": "E", "mode": "fail"})
snap = wait_snapshot(r3, rfail, "error")
check("失败终态 error + 错误信息带异常类型",
      snap["status"] == "error" and snap["error"].startswith("ValueError")
      and snap["done_ts"] is not None)
rf = r3.submit({"tag": "F"})
wait_snapshot(r3, rf, "done")
check("单任务失败不带崩引擎（后续任务照常）", r3.get(rf)["status"] == "done")
check_hist("失败 hist 合法", r3, rfail, expect=("queued", "running", "error"))

# ============ 6) 内存清理：2000 微任务 + 上限 100 -> _jobs 收敛 ============
r4 = Runner({"job_ttl": 3600.0, "job_max_done": 100}, None, renderer=FakeRenderer())
N = 2000
for i in range(N):
    r4.submit({"tag": "m%d" % i})
st = wait_settled(r4)
check("2000 任务全部执行", sum(r4._renderer.execs.values()) == N)
check("_jobs 收敛到上限内（<=100）", len(r4._jobs) <= 100, "len=%d" % len(r4._jobs))
check("清理统计闭环 cleaned+保留==2000", st["cleaned"] + len(r4._jobs) == N,
      "cleaned=%s keep=%d" % (st["cleaned"], len(r4._jobs)))
check("清理后队列/运行归零", st["queue"] == 0 and st["running"] == 0 and st["qsize"] == 0, str(st))
check_invariant("清理后统计一致", st, len(r4._jobs))
check("上限清理保留的是最新完成的批次", len(r4._jobs) == 100, "keep=%d" % len(r4._jobs))

# ============ 7) TTL=0：完成即清，get 回落 404 语义 ============
r5 = Runner({"job_ttl": 0.0, "job_max_done": 500}, None, renderer=FakeRenderer())
rids5 = [r5.submit({"tag": "t%d" % i}) for i in range(5)]
wait_settled(r5)
dl = time.time() + 30
while time.time() < dl and any(r5.get(rid) is not None for rid in rids5):
    time.sleep(0.002)
check("TTL=0 完成即清：_jobs 归零", len(r5._jobs) == 0, "len=%d" % len(r5._jobs))
check("TTL=0 清理统计正确（cleaned==5）", r5.stats()["cleaned"] == 5, str(r5.stats()))
check("TTL=0 被清任务 get 返回 None（/result 404 语义）",
      all(r5.get(rid) is None for rid in rids5))

# ============ 8) 取消风暴：100 提交 + 每单立即取消，竞态下无非法跃迁 ============
r6 = Runner(cfg_base(), None, renderer=FakeRenderer())
rids6 = [r6.submit({"tag": "h%d" % i}) for i in range(100)]
outcomes = [r6.cancel(rid)["outcome"] for rid in rids6]
check("取消风暴 outcome 全部合法",
      set(outcomes) <= {"queued-cancelled", "running-cancelled", "already-done"},
      str(set(outcomes)))
st = wait_settled(r6)
check("风暴后全部终态、无 running/queue 残留",
      st["running"] == 0 and st["queue"] == 0 and st["qsize"] == 0, str(st))
check_hist_all("风暴 hist 全部合法（无非法跃迁）", r6, rids6)
check_invariant("风暴后统计一致", st, len(r6._jobs))

# ============ 9) 随机混合负载：统计一致性不变量全程成立 ============
rng = random.Random(20261113)          # 固定种子：确定性
r7 = Runner(cfg_base(), None, renderer=FakeRenderer())
gate7 = threading.Event()
entered7 = threading.Event()
rh = r7.submit({"tag": "H", "gate": gate7, "entered": entered7})
wait_event(entered7)
rids7 = [r7.submit({"tag": "s%d" % i}) for i in range(30)]
to_cancel = set(rng.sample(rids7, 10))
cancel_out = {rid: r7.cancel(rid)["outcome"] for rid in to_cancel}
check("worker 阻塞期间取消必为排队取消",
      set(cancel_out.values()) == {"queued-cancelled"}, str(set(cancel_out.values())))
st_gated = r7.stats()
check_invariant("阻塞期采样统计一致（worker 挂起：1 running + 20 queued + 10 cancelled）",
                st_gated, len(r7._jobs))
gate7.set()
violations, samples = [], 0
dl = time.time() + 60
while time.time() < dl:
    st = r7.stats()
    if (st["queue"] + st["running"] + st["done"] + st["error"] + st["cancelled"]
            != st["total"] or st["total"] != len(r7._jobs) or st["qsize"] < st["queue"]):
        violations.append(st)
    samples += 1
    if st["qsize"] == 0 and st["queue"] == 0 and st["running"] == 0:
        break
    time.sleep(0.001)
st = wait_settled(r7)
check("混合负载全程统计一致（采样 %d 次，0 违例）" % samples, not violations, str(violations[:2]))
check("混合负载终态分布正确（21 done + 10 cancelled）",
      st["done"] == 21 and st["cancelled"] == 10, str(st))
cancelled_tags = ["s%d" % rids7.index(rid) for rid in to_cancel]
check("被排队取消的 10 个任务从未执行",
      all(r7._renderer.exec_count(tag) == 0 for tag in cancelled_tags))
check_hist("混合负载 H hist 合法", r7, rh)
check_invariant("混合负载终局统计一致", st, len(r7._jobs))

print("==== Runner 状态/取消/清理测试 %s ====" % ("通过 ✓" if ok else "失败 ✗"))
sys.exit(0 if ok else 1)
