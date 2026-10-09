"""路径规划：最便宜插入启发式 + 路线内局部搜索。对应 Java 版 RoutePlanner。

核心公式来自 jsprit / VROOM 的插入代价计算：

    Δ(prev, k, next) = d(prev, k) + d(k, next) - d(prev, next)

即把新站点 k 塞进已有路线的一段弧 (prev, next) 里，要多跑多少路。

外卖是「取送配对」问题（PDP）：同一单的取餐必须先于送达。
这里不用额外机制，只靠枚举下标时的约束「送达下标 > 取餐下标」来表达 ——
这也是 VROOM heuristics.cpp 里的做法。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Set

from .metric import Metric
from .model import Order, Pt, Rider, Stop, StopType


@dataclass
class Insertion:
    """一次插入的结果。"""

    route: List[Stop]      # 插入后的完整路线（不含骑手当前位置）
    extra_m: float         # 相比原路线多跑的里程（米）


# ------------------------------------------------------------ 插入代价

def _insert_delta(metric: Metric, start: Optional[Pt], seq: Sequence[Stop],
                  gap: int, node: Pt) -> float:
    """把 node 插入到 seq 的第 gap 个位置要多跑多少米。

    seq 之前还有一个当前位置 start（None 表示 seq 已经包含了起点）。
    """
    prev = start if gap == 0 else seq[gap - 1].pt
    if gap == len(seq):
        # 追加到末尾：没有旧弧被占掉
        return metric.metres(prev, node)
    nxt = seq[gap].pt
    return metric.metres(prev, node) + metric.metres(node, nxt) - metric.metres(prev, nxt)


def best_insertion(metric: Metric, rider: Rider, order: Order, cap: int,
                   min_gap: int = 0) -> Optional[Insertion]:
    """为一笔订单找出对某骑手最划算的插入方式。

    枚举「取餐插入位」×「送达插入位」，其中送达位严格在取餐位之后 ——
    这一条循环边界就是取送先后的全部约束。

    min_gap: 允许的最小取餐插入位。骑手正在商家等出餐时传 1，
             保证不会把新站点插到他脚下还没取到餐的那一站之前。

    返回 None 表示该骑手装不下（超出载客上限）。
    """
    start = rider.pos
    base = rider.route
    best_route: Optional[List[Stop]] = None
    best_extra = float("inf")

    lo = max(0, min(min_gap, len(base)))
    for gp in range(lo, len(base) + 1):
        cost_pickup = _insert_delta(metric, start, base, gp, order.merchant_pt)

        # 单是取餐一项就已经超预算的，剪掉。
        # 这一步的安全性依赖 metres() 满足三角不等式，见 metric.py 的说明。
        if cost_pickup >= best_extra:
            continue

        with_pickup = list(base)
        with_pickup.insert(gp, Stop(order.id, StopType.PICKUP, order.merchant_pt))

        # 送达必须插在取餐之后 → gd 从 gp + 1 起步
        for gd in range(gp + 1, len(with_pickup) + 1):
            extra = cost_pickup + _insert_delta(metric, None, with_pickup, gd, order.dest_pt)
            if extra >= best_extra:
                continue

            cand = list(with_pickup)
            cand.insert(gd, Stop(order.id, StopType.DELIVERY, order.dest_pt))
            if not feasible(cand, cap):
                continue

            best_extra = extra
            best_route = cand

    if best_route is None:
        return None
    return Insertion(best_route, max(0.0, best_extra))


# ------------------------------------------------------------ 可行性

def feasible(route: Sequence[Stop], cap: int) -> bool:
    """路线是否合法：每单取餐在送达之前，且车上同时携带的订单数不超过接单上限。"""
    picked: Set[str] = set()
    load = 0
    for s in route:
        if s.is_pickup:
            if s.order_id in picked:      # 同一单取两次
                return False
            picked.add(s.order_id)
            load += 1
            if load > cap:
                return False
        else:
            if s.order_id not in picked:  # 没取餐就送达
                return False
            picked.discard(s.order_id)
            load -= 1
    return True


def total_metres(metric: Metric, start: Pt, route: Sequence[Stop]) -> float:
    """从 start 出发依次走完 route 的总里程（米）。"""
    total = 0.0
    cur = start
    for s in route:
        total += metric.metres(cur, s.pt)
        cur = s.pt
    return total


# ------------------------------------------------------------ 局部搜索

def improve(metric: Metric, start: Pt, route: List[Stop], cap: int,
            lock_prefix: int = 0) -> None:
    """原地优化一条路线：先 2-opt 区间反转，再 Or-opt 片段搬家（长度 1~3）。

    每一步都要求仍然满足取送顺序和载客上限，且总里程严格下降才接受。
    lock_prefix 个前导站点固定不动（骑手正站在那一站，位置不能重排）。
    """
    if len(route) < 3:
        return

    lock = max(0, min(lock_prefix, len(route)))
    best = total_metres(metric, start, route)
    changed = True
    guard = 0

    while changed and guard < 80:
        guard += 1
        changed = False
        n = len(route)

        # ---- 2-opt：反转 route[i..j]（i 必须落在锁定前缀之后）----
        # 只有在「路线反转物理可行」时才做。单行道网络上反转区间会把每条内部弧的
        # 行进方向也反过来，那是不合法的走法；所以路网模式跳过它，只靠 Or-opt。
        if metric.reversal_safe:
            found = False
            for i in range(lock, n - 1):
                for j in range(i + 1, n):
                    cand = list(route)
                    cand[i:j + 1] = reversed(cand[i:j + 1])
                    if not feasible(cand, cap):
                        continue
                    c = total_metres(metric, start, cand)
                    if c + 1e-6 < best:
                        route[:] = cand
                        best = c
                        changed = found = True
                        break
                if found:
                    break
            if changed:
                continue

        # ---- Or-opt：把长度 1~3 的片段搬到别的位置 ----
        # 只搬移、不反转，所以无论有没有单行道都成立。
        found = False
        for length in range(1, 4):
            for i in range(lock, n - length + 1):
                seg = route[i:i + length]
                rest = route[:i] + route[i + length:]
                for k in range(lock, len(rest) + 1):
                    if k == i:            # 原位不动，不可能更优
                        continue
                    cand = rest[:k] + seg + rest[k:]
                    if not feasible(cand, cap):
                        continue
                    c = total_metres(metric, start, cand)
                    if c + 1e-6 < best:
                        route[:] = cand
                        best = c
                        changed = found = True
                        break
                if found:
                    break
            if found:
                break


# ------------------------------------------------------------ 顺路判定

def is_on_route(extra_m: float, direct_m: float, cfg) -> bool:
    """判断新订单对某骑手算不算「顺路」。

    两条判定取或：绝对绕路不超过阈值，或者相对绕路比例不超过阈值。

    extra_m:  塞进骑手现有路线后多跑的里程
    direct_m: 这一单取餐点到顾客处的距离（本单自身长度）
    """
    if extra_m <= cfg.on_route_max_detour_m:
        return True
    return direct_m > 1 and extra_m / direct_m <= cfg.on_route_max_detour_ratio
