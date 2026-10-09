"""数据飞轮的地基：把决策和结果**脱敏**落盘，append-only，有轮转上限。

为什么要有这个模块：这套系统的订单、决策、事件原来全在内存里，`reset` 或退出就没了。
而最富的信息甚至活不过一次函数调用 —— `dispatcher._choose` 里每档最优的成本、
候选骑手有几个，都随函数返回一起消失。没有这些数据，"随使用升级"根本无从谈起：
模型没有输入。

**脱敏是硬约束**（用户明确选择的）：只落盘坐标、订单号、时间戳、决策特征、结果指标。
**不含顾客姓名、电话、地址。** 客户地址文本一旦落盘就会长期留在本机，
而它对学习没有任何必要 —— 学习要的是"哪里、什么时候、有多忙、结果如何"。

三条设计原则：

· **append-only + 轮转上限**：文件只追加（崩溃也最多丢最后一行），
  单个文件写满换下一个，总量到上限就删最老的。磁盘不会无限涨。
· **绝不打断业务**：记录失败只计数，不抛异常 —— 它在派单主循环和送达路径里，
  抛出去等于把仿真打断。
· **结果回填**：决策落盘时还不知道好不好，送达时按 (runId, orderId) 补一条结果记录。
  读取时按 orderId 合并，就能得到"当时什么情况 → 做了什么 → 结果如何"的完整样本。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import obs, paths

# 单个文件上限与保留份数：1 MiB × 8 ≈ 8 MiB 上限，够几千笔订单的样本
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_FILES = 8

# 三类记录分别落一个文件：决策（含候选事实）、订单结果、骑行观测
KIND_DECISION = "decisions"
KIND_OUTCOME = "outcomes"
KIND_LEG = "legs"

_lock = threading.Lock()
_dir: Optional[Path] = None
_files: Dict[str, "FileSink"] = {}
_enabled = True
_counts: Dict[str, int] = {}


class FileSink:
    """一个 append-only 的 JSONL 文件，按体积轮转。"""

    def __init__(self, directory: Path, kind: str):
        self.directory = directory
        self.kind = kind
        self.fh = None
        self.path: Optional[Path] = None
        self.bytes = 0

    def _open(self) -> None:
        day = datetime.now().strftime("%Y%m%d")
        for seq in range(MAX_FILES + 1):
            name = f"{self.kind}-{day}.jsonl" if seq == 0 else f"{self.kind}-{day}.{seq}.jsonl"
            p = self.directory / name
            try:
                size = p.stat().st_size if p.is_file() else -1
            except OSError:
                size = -1
            if size < 0 or size < MAX_FILE_BYTES:
                self.fh = open(p, "a", encoding="utf-8")
                self.path = p
                self.bytes = max(0, size)
                self._prune()
                return
        # 全部写满：覆盖最老的那个（宁可轮掉旧数据，也不无限增长）
        p = self.directory / f"{self.kind}-{day}.{MAX_FILES}.jsonl"
        self.fh = open(p, "w", encoding="utf-8")
        self.path = p
        self.bytes = 0

    def _prune(self) -> None:
        files = sorted(self.directory.glob(f"{self.kind}-*.jsonl"),
                       key=lambda f: f.stat().st_mtime if f.exists() else 0)
        for old in files[:-MAX_FILES]:
            try:
                old.unlink()
            except OSError:
                pass

    def write(self, rec: dict) -> None:
        if self.fh is None or self.bytes >= MAX_FILE_BYTES:
            if self.fh is not None:
                try:
                    self.fh.close()
                except OSError:
                    pass
                self.fh = None
            self._open()
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.fh.write(line)
        self.fh.flush()
        self.bytes += len(line.encode("utf-8"))

    def close(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


# ------------------------------------------------------------ 开关

def configure(directory: Optional[Path] = None, enabled: bool = True) -> None:
    """打开落盘。directory=None 时用 data/learn/。"""
    global _dir, _enabled
    with _lock:
        close()
        _enabled = enabled
        _dir = Path(directory) if directory else paths.under_data("learn")
        try:
            _dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _dir = None


def close() -> None:
    for sink in _files.values():
        sink.close()
    _files.clear()


def data_dir() -> Optional[Path]:
    return _dir


def counts() -> dict:
    return dict(_counts)


def reset_counts() -> None:
    _counts.clear()


# ------------------------------------------------------------ 写

def _write(kind: str, rec: dict) -> None:
    if not _enabled or _dir is None:
        return
    try:
        with _lock:
            sink = _files.get(kind)
            if sink is None:
                sink = FileSink(_dir, kind)
                _files[kind] = sink
            sink.write(rec)
            _counts[kind] = _counts.get(kind, 0) + 1
    except (OSError, TypeError, ValueError):
        # 落盘失败不影响业务：这条数据丢了就丢了，仿真不能停
        obs.counter("telemetry_write_failed")


def _round_pt(p) -> Optional[List[float]]:
    if p is None:
        return None
    return [round(float(p.x), 1), round(float(p.y), 1)]


def load_context(world) -> dict:
    """决策当时的负载上下文 —— 这是后面做"分场景学习"的自变量。"""
    riders = list(world.riders.values())
    cap = sum(r.max_orders for r in riders) or 1
    load = sum(r.active_count() for r in riders)
    free = sum(1 for r in riders if r.can_take())
    return {
        "load": round(load / cap, 4),
        "free": free,
        "pool": len(world.pool),
        "riders": len(riders),
        "orders": len(world.orders),
    }


def record_decision(world, order, tier: Optional[int], detour_m: float,
                    rider_id: Optional[str], mode: str, postponed: bool,
                    candidates: Optional[dict] = None,
                    explored: bool = False) -> None:
    """落盘一条派单决策。**不含姓名、电话、地址。**

    `candidates` 是"除了被选中的，还有哪些选择、各要多少代价"的摘要
    （每档最优成本 + 候选数）。这部分以前是直接丢掉的，而它正是
    "当时的决策合不合理"唯一能验证的地方。

    `explored=True` 表示这次是按随机探索（ε-greedy）做出的、可能违反当前策略的决定。
    策略学习**只信这些样本** —— 正常决策是按固定策略选的，直接拿来做对照会带上
    选择偏差（难派的单才被推迟），把偏差误读成因果。
    """
    _write(KIND_DECISION, {
        "runId": world.run_id,
        "orderId": order.id,
        "t": int(order.created_at),
        "assignedAt": int(world.now()),
        "waitSec": int(world.now() - order.created_at),
        "merchant": _round_pt(order.merchant_pt),
        "dest": _round_pt(order.dest_pt),
        "straightM": round(order.merchant_pt.straight(order.dest_pt), 1),
        "tier": tier,
        "detourM": round(float(detour_m), 1),
        "riderId": rider_id,
        "mode": mode,
        "postponed": bool(postponed),
        "explored": bool(explored),
        "postponeCount": order.postpone_count,
        "ctx": load_context(world),
        "cand": candidates or {},
    })


def record_postpone(world, order, best_tier: int, best_detour_m: float,
                    explored: bool = False) -> None:
    """落盘一条"本来要兜底、但我们决定先不派"的决定。

    这条数据是推迟策略学习的**负样本**：它记录了"不等的话会是什么结果"。
    没有它，学习器只能看到派出去的那些单（幸存者偏差）。

    `explored=True` 表示这次推迟是随机探索的结果（本来该派、被 ε 概率改成等一轮），
    策略学习只信这类样本 —— 其余的推迟都是"本来就难派"的选择结果，带偏差。
    """
    _write(KIND_DECISION, {
        "runId": world.run_id,
        "orderId": order.id,
        "t": int(order.created_at),
        "assignedAt": None,
        "waitSec": int(world.now() - order.created_at),
        "merchant": _round_pt(order.merchant_pt),
        "dest": _round_pt(order.dest_pt),
        "straightM": round(order.merchant_pt.straight(order.dest_pt), 1),
        "tier": best_tier,
        "detourM": round(float(best_detour_m), 1),
        "riderId": None,
        "mode": "POSTPONE",
        "postponed": True,
        "explored": bool(explored),
        "postponeCount": order.postpone_count,
        "ctx": load_context(world),
        "cand": {},
    })


def record_outcome(world, order) -> None:
    """送达后落盘结果（回填样本的另一半）。"""
    _write(KIND_OUTCOME, {
        "runId": world.run_id,
        "orderId": order.id,
        "t": int(order.delivered_at),
        "createdAt": int(order.created_at),
        "dispatchedAt": int(order.dispatched_at) or None,
        "pickedAt": int(order.picked_at) or None,
        "totalSec": int(order.total_sec or 0),
        "waitDispatchSec": int(order.wait_dispatch_sec or 0),
        "toStoreSec": int(order.to_store_sec or 0),
        "onRoadSec": int(order.on_road_sec or 0),
        "tier": order.tier,
        "detourM": round(float(order.detour_m), 1),
        "postponeCount": order.postpone_count,
        "reassignCount": order.reassign_count,
        "riderId": order.rider_id,
        "slaMin": world.cfg.sla_minutes,
        "onTime": bool(order.total_sec is not None
                       and order.total_sec <= world.cfg.sla_minutes * 60),
    })


def record_leg(world, distance_m: float, duration_sec: float, kind: str) -> None:
    """落盘一段骑行观测（距离, 时长）—— 行程时间校准的输入。

    `kind` 区分"去商家"和"从商家到顾客"：两段的平均速度不一样
    （找店、上楼、等出餐），混在一起拟合出来的速度对两段都不准。
    """
    if distance_m <= 0 or duration_sec <= 0:
        return
    _write(KIND_LEG, {
        "runId": world.run_id,
        "t": int(world.now()),
        "kind": kind,
        "distanceM": round(float(distance_m), 1),
        "durationSec": round(float(duration_sec), 2),
        "mps": round(float(distance_m) / float(duration_sec), 3),
        "network": world.network_id,
    })


# ------------------------------------------------------------ 读

def records(kind: str) -> List[dict]:
    """读出某个 kind 的全部记录（跨文件、按时间顺序）。"""
    if _dir is None:
        return []
    out: List[dict] = []
    for p in sorted(_dir.glob(f"{kind}-*.jsonl")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue                          # 崩溃留下的半行，跳过
        except OSError:
            continue
    out.sort(key=lambda r: (r.get("t") or 0))
    return out


def joined_samples() -> List[dict]:
    """决策 ⨝ 结果，按 orderId（同一 run 内）合并。

    这是学习器真正要吃的样本：一行里既有"当时的处境和选择"，也有"后来的结果"。
    只有两条都在的样本才有用 —— 半条样本学不出东西，所以这里做内连接。
    """
    decisions: Dict[tuple, dict] = {}
    for d in records(KIND_DECISION):
        # 同一笔单可能有多条决策记录（被推迟过就会有多条），保留最后一条派出去的
        key = (d.get("runId"), d.get("orderId"))
        prev = decisions.get(key)
        if prev is None or (d.get("assignedAt") and not prev.get("assignedAt")):
            decisions[key] = d
    out = []
    for o in records(KIND_OUTCOME):
        d = decisions.get((o.get("runId"), o.get("orderId")))
        if d is None:
            continue
        merged = dict(d)
        merged["outcome"] = o
        out.append(merged)
    return out


def stats() -> dict:
    """给 /api/learn 和 CLI 看的概况。"""
    counts = {k: len(records(k)) for k in (KIND_DECISION, KIND_OUTCOME, KIND_LEG)}
    files = []
    if _dir is not None:
        for p in sorted(_dir.glob("*.jsonl")):
            try:
                files.append({"name": p.name, "bytes": p.stat().st_size})
            except OSError:
                continue
    return {"enabled": _enabled, "dir": str(_dir) if _dir else None,
            "counts": counts, "files": files,
            "samples": len(joined_samples()),
            "note": "只存坐标/订单号/时间/决策特征/结果，不含姓名、电话、地址。"}
