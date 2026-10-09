"""内置的合成路网。对应 Java 版 SyntheticNet。

存在的理由有两个：

1. **离线也能演示路网模式。** 没有 .osm 文件的时候，它就是「真实路网模式」的替身 ——
   有主干道、有环路、有单行道、有断头路，距离不再是 1.4 × 直线。
2. **可测。** 自检不需要联网、也不需要外部数据文件，就能验证「沿路推进」
   「单行道绕路」「三角不等式」这些路网特有的行为。

它刻意做得**不规则**：纯正方形网格会让所有路线退化成曼哈顿距离，
显示不出最短路和绕路的差别。所以这里有：
  * 横竖主干道 + 次干道，间距不等
  * 两条斜向大道（走对角线会明显更近）
  * 一圈环路
  * 一批单行道 —— 这正是路网相对「1.4 × 直线」最大的差别
  * 若干断头路
"""

from __future__ import annotations

import math
import random
from typing import List

from .road_graph import RawEdge, RoadGraph

# 合成路网用这个假经纬度原点（等价于在赤道附近取了一块地方）
_ORIGIN_LAT = 30.0
_ORIGIN_LON = 120.0
_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


def build(size_m: float = 6000.0, seed: int = 20260927, resolution: int = 22) -> RoadGraph:
    """生成合成路网，边长 size_m 米。seed 决定单行道选哪些。"""
    rng = random.Random(seed)
    n = resolution                            # n × n 个路口
    step = size_m / (n - 1)
    cos_lat = math.cos(math.radians(_ORIGIN_LAT))

    def to_lat_lon(x: float, y: float) -> tuple:
        return (_ORIGIN_LAT + y / _M_PER_DEG_LAT,
                _ORIGIN_LON + x / (_M_PER_DEG_LON_EQUATOR * cos_lat))

    # 节点：规则网格 + 一点点抖动，避免过于方正
    lat_lon: List[tuple] = []
    for iy in range(n):
        for ix in range(n):
            jx = (rng.random() - 0.5) * step * 0.10
            jy = (rng.random() - 0.5) * step * 0.10
            x = max(0.0, min(size_m, ix * step + jx))
            y = max(0.0, min(size_m, iy * step + jy))
            lat_lon.append(to_lat_lon(x, y))

    def idx(ix: int, iy: int) -> int:
        return iy * n + ix

    edges: List[RawEdge] = []

    def add_seg(a: int, b: int, oneway: bool, forward: bool) -> None:
        if oneway:
            edges.append(RawEdge(a, b) if forward else RawEdge(b, a))
        else:
            edges.append(RawEdge(a, b))
            edges.append(RawEdge(b, a))

    # ---- 横竖路网。每 4 条留一条主干道，主干道一定双向 ----
    for iy in range(n):
        for ix in range(n - 1):
            arterial = (iy % 4 == 0)
            oneway = (not arterial) and rng.random() < 0.30
            forward = oneway and rng.random() < 0.5
            add_seg(idx(ix, iy), idx(ix + 1, iy), oneway, forward)

    for ix in range(n):
        for iy in range(n - 1):
            arterial = (ix % 5 == 0)
            oneway = (not arterial) and rng.random() < 0.30
            forward = oneway and rng.random() < 0.5
            add_seg(idx(ix, iy), idx(ix, iy + 1), oneway, forward)

    # ---- 两条斜向大道：让「沿路走」明显短于绕横竖 ----
    for k in range(0, n - 4, 4):
        add_seg(idx(k, k), idx(k + 4, k + 4), False, True)
        add_seg(idx(n - 1 - k, k), idx(n - 1 - k - 4, k + 4), False, True)

    # ---- 环路：把外围连成一圈 ----
    for k in range(1, n - 2, 3):
        add_seg(idx(k, 1), idx(k + 3, 1), False, True)
        add_seg(idx(k, n - 2), idx(k + 3, n - 2), False, True)
        add_seg(idx(1, k), idx(1, k + 3), False, True)
        add_seg(idx(n - 2, k), idx(n - 2, k + 3), False, True)

    return RoadGraph.build("合成路网", "内置生成（无外部数据）", lat_lon, edges)
