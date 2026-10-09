"""更贴近现实的合成路网：环放射城市 + 河流瓶颈。

对应 Java 版的 SyntheticNet 是「均匀方格 + 少量对角线」，它有个根本短板：
**没有瓶颈**。均匀网格里任意两点之间总有大量等长路径，绕路永远很温和，
于是「顺路判定」「候选骑手筛选」这些逻辑根本压不出边界情况。

这个生成器刻意做出真实城市的结构特征：

1. **环放射骨架**：中心密集、向外递减的同心环 + 放射状主干道。
   这不是拍脑袋 —— 它天然带来「市中心路网密、城郊稀」这个真实特征，
   也让「骑手在郊区很难顺路」这类现象自然出现。
   （代价是它更像巴黎/莫斯科那种环放射城市，而不是方正的棋盘城市；
     要棋盘城市可以用 `--osm synthetic`。）

2. **自然屏障**：一条河把城市切成两半，只有 3 座桥。
   这是整个设计里最重要的一条 —— 它是**真的瓶颈**：
   跨河的订单最短路会被硬生生拉长，「顺路」判断在桥附近会失效，
   骑手会被迫绕远。这些正是需要在测试里压出来的行为。

3. **断头路**：城郊一批尽头小路，考验「吸附到最近路网节点」的健壮性。

4. **单行道集中在市中心**，且成对出现（相邻街道方向相反）——
   真实的单行系统就是这么组织的，也让 2-opt 在市中心彻底不合法。

5. **道路长度呈长尾分布**：多数是短支路，少数是穿城长干道。

生成完全由种子决定，所以测试可复现。
"""

from __future__ import annotations

import math
import random
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from .model import Pt
from .projection import Projection
from .road_graph import RawEdge, RoadGraph

# 合成路网用这个假经纬度原点（等价于在赤道附近取了一块地方）
_ORIGIN_LAT = 30.0
_ORIGIN_LON = 120.0
_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


def build(size_m: float = 6000.0, seed: int = 20260927,
          rings: int = 10, block_spacing_m: float = 180.0,
          bridges: int = 3) -> RoadGraph:
    """生成一块环放射 + 河流的城市路网。

    参数：
      size_m           城市边长（米）
      rings            同心环数量（越多越密）
      block_spacing_m  街区目标尺度（米）。环上节点数按「周长 ÷ 这个值」推出来，
                       所以每个环的街区尺度大致恒定，而不是角度均分 ——
                       角度均分会让市中心挤成一团、城郊稀得离谱。
      bridges          跨河桥梁数量。桥越少瓶颈越狠。
    """
    rng = random.Random(seed)
    cx = cy = size_m / 2.0
    r_max = size_m * 0.47

    cos_lat = math.cos(math.radians(_ORIGIN_LAT))

    def to_lat_lon(x: float, y: float) -> Tuple[float, float]:
        return (_ORIGIN_LAT + y / _M_PER_DEG_LAT,
                _ORIGIN_LON + x / (_M_PER_DEG_LON_EQUATOR * cos_lat))

    # ---- 环的半径：r_k = r_max · (k/(rings-1))^1.3 ----
    # 指数 >1 让内圈间距明显小于外圈，形成「市中心密、城郊疏」
    def ring_radius(k: int) -> float:
        return r_max * (k / (rings - 1)) ** 1.3

    # ---- 每个环上的节点数与角度 ----
    # 角度不按等分，而是按「周长 ÷ 目标街区尺度」决定节点数，
    # 这样各环的街区尺度大致恒定。
    ring_nodes: List[List[int]] = []          # ring_nodes[k] = 该环的节点下标列表
    node_xy: List[Tuple[float, float]] = []
    node_angle: List[float] = []
    node_ring: List[int] = []

    center = len(node_xy)
    node_xy.append((cx, cy))
    node_angle.append(0.0)
    node_ring.append(0)
    ring_nodes.append([center])

    for k in range(1, rings):
        r = ring_radius(k)
        circumference = 2 * math.pi * r
        count = max(6, int(round(circumference / block_spacing_m)))
        # 每个环的第一条辐条角度整体扭一点，避免所有环的接缝连成一条直线
        base = rng.random() * 2 * math.pi
        idxs: List[int] = []
        for s in range(count):
            ang = base + 2 * math.pi * s / count
            # 径向和角向都加抖动，破坏完美的圆形感
            rr = r * (1.0 + (rng.random() - 0.5) * 0.10)
            aa = ang + (rng.random() - 0.5) * (2 * math.pi / count) * 0.28
            idxs.append(len(node_xy))
            node_xy.append((cx + rr * math.cos(aa), cy + rr * math.sin(aa)))
            node_angle.append(aa)
            node_ring.append(k)
        ring_nodes.append(idxs)

    # ---- 主干道：少数几条放射线 + 两条环线 ----
    # 放射主干道用角度来识别（每隔若干条辐条一条），它在每一环上都存在
    arterial_every = max(3, rings // 2)
    radial_arterial_angles: Set[int] = set()     # 以「辐条序号」记，见下面映射
    ring_arterial = {max(1, rings // 3), max(2, rings - 2)}

    # 每个环上「第 s 个节点」的辐条序号需要归一化：各环节点数不同，
    # 所以用角度来对齐 —— 把角度量化到 arterial_every 个扇区。
    def spoke_sector(k: int, i: int) -> int:
        ang = node_angle[i] % (2 * math.pi)
        sectors = arterial_every * 4
        return int(ang / (2 * math.pi) * sectors) % sectors

    arterial_sectors = {s for s in range(0, arterial_every * 4, 4)}

    edges: List[RawEdge] = []
    edge_kind: List[str] = []      # 与 edges 平行：arterial / ring / local

    def add_edge(a: int, b: int, kind: str, oneway: bool, forward: bool = True) -> None:
        if a == b:
            return
        edges.append(RawEdge(a, b))
        edge_kind.append(kind)
        if not oneway:
            edges.append(RawEdge(b, a))
            edge_kind.append(kind)

    # ---- 径向边 ----
    for k in range(rings - 1):
        inner, outer = ring_nodes[k], ring_nodes[k + 1]
        for i_outer, b in enumerate(outer):
            # 把外环节点按角度配到内环最近的那个
            a = _nearest_by_angle(inner, node_angle, node_angle[b])
            if a is None:
                continue
            arterial = spoke_sector(k + 1, b) in arterial_sectors
            if arterial:
                add_edge(a, b, "arterial", False)
            elif rng.random() < 0.80:
                add_edge(a, b, "local", False)

    # ---- 环向边 ----
    for k in range(1, rings):
        ring = ring_nodes[k]
        arterial_ring = k in ring_arterial
        n = len(ring)
        for i in range(n):
            j = (i + 1) % n
            a, b = ring[i], ring[j]
            if arterial_ring:
                add_edge(a, b, "ring", False)
            elif rng.random() < 0.72:
                add_edge(a, b, "local", False)

    # ---- 少数几条穿城长干道：让道路长度呈长尾分布 ----
    # 注意 _angular_gap 返回的是**劣弧**（≤ π），所以不能用 "> π" 当条件 —— 那永远不成立。
    # 要长弦就要大的夹角，也就是劣弧接近 π。
    for _ in range(3):
        ring = ring_nodes[rings - 1]
        a = ring[rng.randrange(len(ring))]
        b = ring[rng.randrange(len(ring))]
        if a != b and _angular_gap(node_angle[a], node_angle[b]) > 2.2:
            add_edge(a, b, "arterial", False)

    # ---- 河流：把城市切成两半，只留几座桥 ----
    # 河是一条南北向的曲线。用 x > river_x(y) 判定在河的哪一侧。
    river_amp = size_m * 0.055
    river_phase = rng.random() * 6.28
    river_center_x = cx * 0.86

    def river_x(y: float) -> float:
        return river_center_x + river_amp * math.sin(y / (size_m * 0.16) + river_phase)

    def side_of(x: float, y: float) -> int:
        return 1 if x > river_x(y) else -1

    node_side = [side_of(x, y) for (x, y) in node_xy]

    # 桥的位置：沿河均匀放几座
    bridge_pts: List[Tuple[float, float]] = []
    for i in range(bridges):
        by = size_m * (i + 1) / (bridges + 1)
        bridge_pts.append((river_x(by), by))
    bridge_radius = size_m * 0.075

    kept: List[RawEdge] = []
    kept_kind: List[str] = []
    dropped_by_river = 0
    for e, kind in zip(edges, edge_kind):
        a, b = e.frm, e.to
        if node_side[a] != node_side[b]:
            # 跨河的边：只有落在桥附近的才保留
            mx = (node_xy[a][0] + node_xy[b][0]) / 2
            my = (node_xy[a][1] + node_xy[b][1]) / 2
            near_bridge = any(math.hypot(mx - bx, my - by) <= bridge_radius
                              for (bx, by) in bridge_pts)
            if not near_bridge:
                dropped_by_river += 1
                continue
        kept.append(e)
        kept_kind.append(kind)
    edges, edge_kind = kept, kept_kind

    # ---- 断头路：城郊砍掉一批连接，形成尽头小路 ----
    # 允许把度数为 2 的节点砍到 1 —— 否则永远造不出真正的 cul-de-sac。
    # 底线是「不能把节点变成孤点」（度数 0），那会让它彻底不可达。
    degree: Dict[int, int] = {}
    for e in edges:
        degree[e.frm] = degree.get(e.frm, 0) + 1
        degree[e.to] = degree.get(e.to, 0) + 1
    cut_budget = max(4, len(edges) // 40)
    cut = 0
    order = list(range(len(edges)))
    rng.shuffle(order)
    for ei in order:
        if cut >= cut_budget:
            break
        e = edges[ei]
        if edge_kind[ei] != "local":
            continue
        if node_ring[e.frm] < rings - 4:        # 只动城郊
            continue
        if degree.get(e.frm, 0) <= 1 or degree.get(e.to, 0) <= 1:
            continue
        edges[ei] = None
        edge_kind[ei] = None
        degree[e.frm] -= 1
        degree[e.to] -= 1
        cut += 1
    edges = [e for e in edges if e is not None]
    edge_kind = [k for k in edge_kind if k is not None]

    # ---- 单行道：集中在市中心，且成对（相邻街道反向）----
    # 先把「成对出现的双向边」去重成一条条**街道**，再决定每条街是双向还是单向，
    # 最后按需展开。这样不会出现「只加了一半」的残边。
    inner_radius = r_max * 0.42
    streets: List[Tuple[int, int, str]] = []
    seen_street: Set[Tuple[int, int]] = set()
    for i, e in enumerate(edges):
        a, b = (e.frm, e.to) if e.frm < e.to else (e.to, e.frm)
        if (a, b) in seen_street:
            continue
        seen_street.add((a, b))
        streets.append((a, b, edge_kind[i]))

    final: List[RawEdge] = []
    oneway_count = 0
    for a, b, kind in streets:
        r = math.hypot((node_xy[a][0] + node_xy[b][0]) / 2 - cx,
                       (node_xy[a][1] + node_xy[b][1]) / 2 - cy)
        if kind == "local" and r < inner_radius and rng.random() < 0.45:
            # 成对单行：按街道走向决定方向，相邻街道自然相反
            mid_ang = math.atan2((node_xy[a][1] + node_xy[b][1]) / 2 - cy,
                                 (node_xy[a][0] + node_xy[b][0]) / 2 - cx)
            forward = math.cos(mid_ang * 3) >= 0
            final.append(RawEdge(a, b) if forward else RawEdge(b, a))
            oneway_count += 1
        else:
            final.append(RawEdge(a, b))
            final.append(RawEdge(b, a))
    edges = final

    # ---- 断头路：城郊整条整条地砍掉街道，做出真正的 cul-de-sac ----
    # 两个坑都踩过，写在这里免得再犯：
    #   ① 必须整条街一起删（两个方向都删），只删一个方向会把它变成单行道而不是断头路；
    #   ② 度数要按**街道**数，不能按有向边数 —— 一条双向街会给两端各加 2，
    #      用它当判据会以为节点还很"富裕"，结果一条断头路都砍不出来。
    street_deg: Dict[int, int] = {}
    street_seen: Set[Tuple[int, int]] = set()
    for e in edges:
        a, b = (e.frm, e.to) if e.frm < e.to else (e.to, e.frm)
        if (a, b) in street_seen:
            continue
        street_seen.add((a, b))
        street_deg[a] = street_deg.get(a, 0) + 1
        street_deg[b] = street_deg.get(b, 0) + 1

    # 从「两端最空闲」的街道开始砍，这样自然会把城郊的枝桠变成尽头小路
    candidates = sorted(street_seen,
                        key=lambda ab: street_deg.get(ab[0], 0) + street_deg.get(ab[1], 0))
    cut_budget = max(4, len(candidates) // 25)
    cut = 0
    for (a, b) in candidates:
        if cut >= cut_budget:
            break
        if node_ring[a] < rings - 4 and node_ring[b] < rings - 4:
            continue                                  # 只动城郊
        if street_deg.get(a, 0) < 2 or street_deg.get(b, 0) < 2:
            continue                                  # 砍完不能变成孤点
        _remove_street(edges, a, b)
        street_deg[a] -= 1
        street_deg[b] -= 1
        cut += 1
    edges = [e for e in edges if e is not None]

    # ---- 只保留最大连通分量 ----
    # 河流可能把几个节点彻底切开（它们的边全跨河、又都不在桥附近）。
    # 留着它们的后果很严重：RoadMetric 会返回「不可达」，
    # 于是落在那些节点上的顾客地址永远送不到，订单会一直卡着。
    # OSM 解析那边也是这么处理的，两边行为保持一致。
    node_xy, edges, dropped_nodes = _keep_largest_component(node_xy, edges)

    graph = RoadGraph.build("模拟城区", "内置生成：环放射 + 河流瓶颈",
                            [to_lat_lon(x, y) for (x, y) in node_xy], edges)

    # 记录这次生成的关键参数，供界面和测试读
    graph.build_info = {
        "generator": "realistic",
        "rings": rings,
        "blockSpacingM": block_spacing_m,
        "bridges": bridges,
        "droppedByRiver": dropped_by_river,
        "droppedUnreachable": dropped_nodes,
        "onewayStreets": oneway_count,
        "deadEndCuts": cut,
        "seed": seed,
    }
    return graph


def _keep_largest_component(node_xy: List[Tuple[float, float]],
                            edges: List[RawEdge]) -> Tuple[List[Tuple[float, float]],
                                                          List[RawEdge], int]:
    """丢掉不在最大弱连通分量里的节点，并把剩下的节点下标压紧。"""
    n = len(node_xy)
    adj: List[List[int]] = [[] for _ in range(n)]
    for e in edges:
        adj[e.frm].append(e.to)
        adj[e.to].append(e.frm)

    comp = [-1] * n
    sizes: List[int] = []
    for s in range(n):
        if comp[s] >= 0:
            continue
        cid = len(sizes)
        size = 0
        comp[s] = cid
        queue = deque([s])
        while queue:
            u = queue.popleft()
            size += 1
            for v in adj[u]:
                if comp[v] < 0:
                    comp[v] = cid
                    queue.append(v)
        sizes.append(size)

    if not sizes:
        return node_xy, edges, 0
    best = max(range(len(sizes)), key=lambda i: sizes[i])
    keep = [c == best for c in comp]
    dropped = n - sizes[best]
    if dropped == 0:
        return node_xy, edges, 0

    remap = [-1] * n
    kept_xy: List[Tuple[float, float]] = []
    for i in range(n):
        if keep[i]:
            remap[i] = len(kept_xy)
            kept_xy.append(node_xy[i])
    kept_edges = [RawEdge(remap[e.frm], remap[e.to]) for e in edges
                  if remap[e.frm] >= 0 and remap[e.to] >= 0]
    return kept_xy, kept_edges, dropped


def _remove_street(edges: List[Optional[RawEdge]], a: int, b: int) -> None:
    """整条街一起删（两个方向都删）。

    只删一个方向会把它变成单行道而不是断头路 —— 这是这里最容易写错的地方。
    """
    for i, e in enumerate(edges):
        if e is None:
            continue
        if (e.frm == a and e.to == b) or (e.frm == b and e.to == a):
            edges[i] = None


def _nearest_by_angle(candidates: List[int], angles: List[float],
                      target: float) -> Optional[int]:
    """在候选节点里找角度上最接近 target 的那个。"""
    if not candidates:
        return None
    best, best_d = None, float("inf")
    for c in candidates:
        d = _angular_gap(angles[c], target)
        if d < best_d:
            best_d, best = d, c
    return best


def _angular_gap(a: float, b: float) -> float:
    d = abs((a - b) % (2 * math.pi))
    return min(d, 2 * math.pi - d)


# ------------------------------------------------------------ 网络特征统计

def stats(graph: RoadGraph, samples: int = 240, sources: int = 16,
          seed: int = 7) -> dict:
    """算一块路网的结构特征。用途有两个：界面上展示，以及测试里断言。

    **绕路比（road_distance / straight_distance）是最能说明问题的指标**：
      · 完全自由的直线距离 → 1.00
      · 规则方格 → 约 1.27（曼哈顿距离）
      · 真实城市 → 约 1.25 ~ 1.45，且尾部更长（受河流、单行道、断头路影响）
    所以它既能验证「这个网络确实是路网而不是网格」，也能验证「瓶颈确实存在」。
    """
    from .road_metric import UNREACHABLE_M, RoadMetric

    rng = random.Random(seed)
    n = graph.node_count

    # ---- 度数：按「路口连着几条街」算，不按有向边算 ----
    # 一条双向街会给两端各贡献 2 条有向边，用它统计会把断头路（1 条街）看成度数 2，
    # 于是永远数不出尽头小路。所以要按**不同的邻居**去重。
    neighbors: List[Set[int]] = [set() for _ in range(n)]
    for i in range(graph.edge_count):
        a, b = graph.edge_from[i], graph.edge_to[i]
        neighbors[a].add(b)
        neighbors[b].add(a)
    street_deg = [len(s) for s in neighbors]

    # ---- 单行道：没有反向边的那种 ----
    edge_set = {(graph.edge_from[i], graph.edge_to[i]) for i in range(graph.edge_count)}
    oneway = sum(1 for i in range(graph.edge_count)
                 if (graph.edge_to[i], graph.edge_from[i]) not in edge_set)

    # ---- 断头路：只连着一条街的节点 ----
    dead_ends = sum(1 for d in street_deg if d == 1)
    isolated = sum(1 for d in street_deg if d == 0)

    # ---- 边长分布 ----
    lengths = sorted(graph.edge_len)
    def pct(p: float) -> float:
        if not lengths:
            return 0.0
        return lengths[min(len(lengths) - 1, int(len(lengths) * p))]

    # ---- 连通分量 ----
    comps = _component_sizes(graph)

    # ---- 绕路比 ----
    # 用「少数几个源点各跑一次 Dijkstra」而不是「几百对点各跑一次」——
    # 前者只要十几个距离行，后者会把行缓存冲爆。
    rm = RoadMetric(graph)
    ratios: List[float] = []
    used_sources = 0
    for _ in range(sources * 4):
        if used_sources >= sources:
            break
        u = rng.randrange(n)
        row = rm._row(u)
        used_sources += 1
        for _ in range(samples // sources):
            v = rng.randrange(n)
            if v == u:
                continue
            d = row[v]
            if not math.isfinite(d) or d >= UNREACHABLE_M:
                continue
            straight = graph.point(u).straight(graph.point(v))
            if straight < 50:                    # 太近的点比值噪声大
                continue
            ratios.append(d / straight)
    ratios.sort()

    def rpct(p: float) -> Optional[float]:
        if not ratios:
            return None
        return round(ratios[min(len(ratios) - 1, int(len(ratios) * p))], 3)

    return {
        "nodes": n,
        "edges": graph.edge_count,
        "segments": graph.seg_count,
        "extentM": [round(graph.width_m), round(graph.height_m)],
        "onewayEdges": oneway,
        "onewayPct": round(oneway * 100.0 / max(1, graph.edge_count), 1),
        "deadEnds": dead_ends,
        "isolatedNodes": isolated,
        "components": len(comps),
        "largestComponentPct": round(max(comps) * 100.0 / max(1, n), 1) if comps else 0.0,
        "degreeMean": round(sum(street_deg) / max(1, n), 2),
        "degreeMax": max(street_deg) if street_deg else 0,
        "degreeMin": min(street_deg) if street_deg else 0,
        "edgeLenP50": round(pct(0.5), 1),
        "edgeLenP95": round(pct(0.95), 1),
        "edgeLenMax": round(lengths[-1], 1) if lengths else 0.0,
        "detourMean": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "detourP50": rpct(0.5),
        "detourP95": rpct(0.95),
        "detourMax": rpct(1.0),
        "detourSamples": len(ratios),
        "build": getattr(graph, "build_info", {}),
    }


def _component_sizes(graph: RoadGraph) -> List[int]:
    """弱连通分量的节点数（忽略方向）。"""
    n = graph.node_count
    adj: List[List[int]] = [[] for _ in range(n)]
    for i in range(graph.edge_count):
        a, b = graph.edge_from[i], graph.edge_to[i]
        adj[a].append(b)
        adj[b].append(a)

    seen = bytearray(n)
    sizes: List[int] = []
    for s in range(n):
        if seen[s]:
            continue
        size = 0
        seen[s] = 1
        queue = deque([s])
        while queue:
            u = queue.popleft()
            size += 1
            for v in adj[u]:
                if not seen[v]:
                    seen[v] = 1
                    queue.append(v)
        sizes.append(size)
    return sizes
