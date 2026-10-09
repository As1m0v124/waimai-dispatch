"""折线几何工具：按弧长量长度、按弧长取点、抽稀。对应 Java 版 Geom。"""

from __future__ import annotations

import math
from typing import List, Optional

from .model import Pt


def raw_length(line: Optional[List[Pt]]) -> float:
    """折线的**原始几何长度**（米）：逐段直线距离求和。

    刻意不乘 1.4 之类的街面系数：路径上的相邻点已经是道路本身的折点，
    段与段之间的直线距离就是这条路段的长度。
    """
    if not line or len(line) < 2:
        return 0.0
    return sum(line[i].straight(line[i + 1]) for i in range(len(line) - 1))


def point_at(line: Optional[List[Pt]], s: float) -> Optional[Pt]:
    """折线上距起点 s 米处的点（超出范围就取端点）。"""
    if not line:
        return None
    if len(line) == 1 or s <= 0:
        return line[0]

    acc = 0.0
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        seg = a.straight(b)
        if acc + seg >= s:
            f = 0.0 if seg <= 1e-9 else (s - acc) / seg
            return Pt(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f)
        acc += seg
    return line[-1]


def _perpendicular_distance(p: Pt, a: Pt, b: Pt) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    len2 = dx * dx + dy * dy
    if len2 < 1e-12:
        return p.straight(a)
    t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2
    t = max(0.0, min(1.0, t))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def simplify(line: Optional[List[Pt]], tol_m: float) -> Optional[List[Pt]]:
    """抽稀折线，只保留偏离弦超过 tol_m 米的点（Douglas–Peucker）。

    用途：把骑手路线多段线塞进每 500ms 一次的轮询响应里。
    一条沿路的路线动辄上百个点，抽稀后往往只剩十来个，而画出来肉眼没差别。
    """
    if line is None or len(line) <= 2:
        return line

    keep = [False] * len(line)
    keep[0] = keep[-1] = True

    # 用显式栈而不是递归：路线长的时候递归会爆栈
    stack = [(0, len(line) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        a, b = line[lo], line[hi]
        max_dev, max_idx = -1.0, -1
        for i in range(lo + 1, hi):
            dev = _perpendicular_distance(line[i], a, b)
            if dev > max_dev:
                max_dev, max_idx = dev, i
        if max_dev > tol_m and max_idx > 0:
            keep[max_idx] = True
            stack.append((lo, max_idx))
            stack.append((max_idx, hi))

    return [p for p, k in zip(line, keep) if k]


def distance_to_polyline(p: Pt, line: Optional[List[Pt]]) -> float:
    """点到折线的最近距离（米）。用来检查「骑手是不是真的走在路上」。"""
    if not line:
        return float("inf")
    if len(line) == 1:
        return p.straight(line[0])
    return min(_perpendicular_distance(p, line[i], line[i + 1])
               for i in range(len(line) - 1))
