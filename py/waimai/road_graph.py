"""可骑行的有向路网。对应 Java 版 RoadGraph。

节点带投影后的米坐标（x/y）；边是**有向**的，因为单行道只允许一个方向，
这让最短路自然地把单行道带来的绕路算进去 —— 这也正是真实路网和「1.4 × 直线」
最大的差别所在。

邻接表用 CSR（压缩稀疏行）存：adj_edge[adj_start[u]:adj_start[u+1]] 是节点 u 的出边下标。
相比每个节点一个 list，这样省掉几万个对象头和扩容开销 —— 在 Python 里这个差别更明显。

另外单独存一份**去重的无向线段表**（seg_a/seg_b）专供渲染：
一条双向道路只有一条线段，不会画两遍。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import Pt
from .projection import Projection


@dataclass(frozen=True)
class RawEdge:
    """建图时收集的一条原始边。

    只带两端节点，**不带长度** —— 长度统一在建图时按投影后的米坐标算，
    这样就不会出现「解析阶段用一套公式、建图阶段用另一套」导致的不一致。
    """

    frm: int
    to: int


class RoadGraph:
    def __init__(self, name: str, source: str, proj: Projection,
                 xs: List[float], ys: List[float],
                 lats: List[float], lons: List[float],
                 adj_start: List[int], adj_edge: List[int],
                 edge_from: List[int], edge_to: List[int], edge_len: List[float],
                 seg_a: List[int], seg_b: List[int],
                 width_m: float, height_m: float):
        self.name = name
        self.source = source
        self.proj = proj
        self.x = xs
        self.y = ys
        self.lat = lats
        self.lon = lons
        self.node_count = len(xs)
        self.adj_start = adj_start
        self.adj_edge = adj_edge
        self.edge_from = edge_from
        self.edge_to = edge_to
        self.edge_len = edge_len
        self.edge_count = len(edge_from)
        self.seg_a = seg_a
        self.seg_b = seg_b
        self.seg_count = len(seg_a)
        self.width_m = width_m
        self.height_m = height_m

    def point(self, node: int) -> Pt:
        return Pt(self.x[node], self.y[node])

    def describe(self) -> str:
        return (f"{self.name}：{self.node_count} 个节点，{self.edge_count} 条有向边，"
                f"范围 {self.width_m / 1000:.1f} × {self.height_m / 1000:.1f} km（{self.source}）")

    # ------------------------------------------------------------ 建图

    @staticmethod
    def build(name: str, source: str, node_lat_lon: List[Optional[Tuple[float, float]]],
              raw_edges: List[RawEdge]) -> "RoadGraph":
        """从「节点坐标 + 边表」装配出一个图。"""
        n = len(node_lat_lon)

        present = [ll for ll in node_lat_lon if ll is not None]
        if not present:
            raise ValueError("路网里没有任何节点")
        min_lat = min(ll[0] for ll in present)
        max_lat = max(ll[0] for ll in present)
        min_lon = min(ll[1] for ll in present)
        max_lon = max(ll[1] for ll in present)

        proj = Projection(min_lat, min_lon)

        xs = [0.0] * n
        ys = [0.0] * n
        lats = [0.0] * n
        lons = [0.0] * n
        for i, ll in enumerate(node_lat_lon):
            if ll is None:
                continue
            p = proj.to_metres(ll[0], ll[1])
            xs[i], ys[i] = p.x, p.y
            lats[i], lons[i] = ll[0], ll[1]

        # 只保留两端都存在的边，长度按投影后的米坐标算
        kept = []
        for e in raw_edges:
            if not (0 <= e.frm < n and 0 <= e.to < n):
                continue
            if e.frm == e.to:                     # 自环没有意义
                continue
            if node_lat_lon[e.frm] is None or node_lat_lon[e.to] is None:
                continue
            kept.append(e)

        edge_from = [e.frm for e in kept]
        edge_to = [e.to for e in kept]
        edge_len = [math.hypot(xs[e.to] - xs[e.frm], ys[e.to] - ys[e.frm]) for e in kept]

        # CSR 邻接表
        out_deg = [0] * n
        for f in edge_from:
            out_deg[f] += 1
        adj_start = [0] * (n + 1)
        for i in range(n):
            adj_start[i + 1] = adj_start[i] + out_deg[i]
        adj_edge = [0] * len(kept)
        cursor = adj_start[:n]
        for e_idx, f in enumerate(edge_from):
            adj_edge[cursor[f]] = e_idx
            cursor[f] += 1

        # 渲染用的去重无向线段：一条双向道路只留一条
        seen = set()
        seg_a, seg_b = [], []
        for e_idx in range(len(kept)):
            lo = min(edge_from[e_idx], edge_to[e_idx])
            hi = max(edge_from[e_idx], edge_to[e_idx])
            key = lo * n + hi
            if key in seen:
                continue
            seen.add(key)
            seg_a.append(lo)
            seg_b.append(hi)

        width = max((xs[i] for i in range(n) if node_lat_lon[i] is not None), default=0.0)
        height = max((ys[i] for i in range(n) if node_lat_lon[i] is not None), default=0.0)

        return RoadGraph(name, source, proj, xs, ys, lats, lons,
                         adj_start, adj_edge, edge_from, edge_to, edge_len,
                         seg_a, seg_b, width, height)
