"""真实路网上的距离与路径。对应 Java 版 RoadMetric。

**性能是这个类的核心问题。** 插入启发式对每一对（骑手 × 订单）要问几千次
「这两点之间多远」，如果每次都跑一遍最短路，一轮派单要几秒到几十秒，完全不能接受。
（Python 比 Java 还慢一个数量级，所以这里更要小心。）

所以分成两条路：

* **代价查询 → O(1) 查表。** 把用到的点吸附到路网节点，然后**按节点缓存
  一整行最短路结果**（一次 Dijkstra 得到「这个点到图上所有点的距离」）。
  同一行会被反复复用，摊下来每次查询就是一次数组下标。
* **实际路径 → 按需算 + 缓存。** 只有真的要沿路走的那些腿才需要折线，
  每条腿用 A*（启发式就是直线距离）单独算，按 (起点,终点) 缓存。
  一次轮询也就十几条腿，完全跑得过来。

距离不满足「对称」—— 单行道就是不对称的。这与 Metric 的约定一致
（约定只要求三角不等式）。
"""

from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Tuple

from .metric import Metric
from .model import Pt
from .road_graph import RoadGraph

# 不可达时返回的距离。
#
# 必须是一个**足够大的有限常数**：route_planner.best_insertion 的剪枝依赖三角不等式，
# 而 float('inf') 会让 inf - inf = nan 参与比较，直接破坏剪枝的正确性。
# 常数 C 在「所有真实距离都远小于 C」时仍然满足三角不等式。
UNREACHABLE_M = 1.0e7

SNAP_START_M = 200.0
SNAP_RINGS = 12

# 缓存上限。骑手位置每一拍都在变，吸附到的节点也一直在变，
# 不设限的话距离矩阵会一直涨。涨满就把整个缓存丢掉重建 ——
# 它纯粹是缓存，丢了只影响速度、不影响正确性。
MAX_ROWS = 64
MAX_PATHS = 4096
MAX_SNAPS = 20000


class RoadMetric(Metric):
    def __init__(self, graph: RoadGraph):
        self.g = graph
        self.cell_m = max(50.0, self._estimate_cell_size(graph))
        self.grid_w = max(1, int(graph.width_m / self.cell_m) + 1)
        self.grid_h = max(1, int(graph.height_m / self.cell_m) + 1)

        # 网格索引：把坐标吸附到最近节点用
        counts = [0] * (self.grid_w * self.grid_h)
        for i in range(graph.node_count):
            counts[self._cell_of(graph.x[i], graph.y[i])] += 1
        self.cell_start = [0] * (len(counts) + 1)
        for i in range(len(counts)):
            self.cell_start[i + 1] = self.cell_start[i] + counts[i]
        self.cell_nodes = [0] * graph.node_count
        cursor = self.cell_start[:len(counts)]
        for i in range(graph.node_count):
            c = self._cell_of(graph.x[i], graph.y[i])
            self.cell_nodes[cursor[c]] = i
            cursor[c] += 1

        self._dist_rows: Dict[int, List[float]] = {}
        self._path_cache: Dict[int, List[Pt]] = {}
        self._snap_cache: Dict[int, int] = {}

        self.dijkstra_runs = 0
        self.astar_runs = 0

    @staticmethod
    def _estimate_cell_size(g: RoadGraph) -> float:
        n = min(g.edge_count, 5000)
        if n == 0:
            return 200.0
        return max(50.0, sum(g.edge_len[:n]) / n * 4)

    def _cell_of(self, x: float, y: float) -> int:
        cx = max(0, min(self.grid_w - 1, int(x / self.cell_m)))
        cy = max(0, min(self.grid_h - 1, int(y / self.cell_m)))
        return cy * self.grid_w + cx

    def stats(self) -> str:
        return (f"Dijkstra {self.dijkstra_runs} 次、A* {self.astar_runs} 次，"
                f"缓存 {len(self._dist_rows)} 行 / {len(self._path_cache)} 条路径")

    # ------------------------------------------------------------ 吸附

    def snap(self, p: Pt) -> int:
        """把任意点吸附到最近的路网节点，用环形扩张搜索，找不到再退回全量扫描。"""
        key = _snap_key(p)
        cached = self._snap_cache.get(key)
        if cached is not None:
            return cached

        g = self.g
        c0 = self._cell_of(p.x, p.y)
        cx0, cy0 = c0 % self.grid_w, c0 // self.grid_w
        best, best_d = -1, float("inf")

        for ring in range(SNAP_RINGS):
            lo = max(0, min(cy0 - ring, self.grid_h - 1))
            hi = max(0, min(cy0 + ring, self.grid_h - 1))
            left = max(0, min(cx0 - ring, self.grid_w - 1))
            right = max(0, min(cx0 + ring, self.grid_w - 1))
            for cy in range(lo, hi + 1):
                edge_row = (cy == lo or cy == hi)
                for cx in range(left, right + 1):
                    if not edge_row and cx != left and cx != right:
                        continue                    # 只扫这一圈的边
                    cell = cy * self.grid_w + cx
                    for a in range(self.cell_start[cell], self.cell_start[cell + 1]):
                        v = self.cell_nodes[a]
                        d = math.hypot(g.x[v] - p.x, g.y[v] - p.y)
                        if d < best_d:
                            best_d, best = d, v
            # 已经找到，并且这一圈的距离下界超过了当前最优，就可以停了
            if best >= 0 and best_d <= ring * self.cell_m:
                break

        if best < 0:                                # 兜底：全量扫一遍
            for v in range(g.node_count):
                d = math.hypot(g.x[v] - p.x, g.y[v] - p.y)
                if d < best_d:
                    best_d, best = d, v

        if len(self._snap_cache) >= MAX_SNAPS:
            self._snap_cache.clear()
        self._snap_cache[key] = best
        return best

    # ------------------------------------------------------------ 距离

    def metres(self, a: Pt, b: Pt) -> float:
        u, v = self.snap(a), self.snap(b)
        if u < 0 or v < 0:
            return UNREACHABLE_M
        d = self._row_distance(u, v)
        # 加上「真实点到吸附点」的两小段。吸附偏移通常只有几十米，
        # 但它让 metres() 与 path() 的折线长度严格一致（leg_scale 才能正好是 1）。
        g = self.g
        return (d + math.hypot(g.x[u] - a.x, g.y[u] - a.y)
                + math.hypot(g.x[v] - b.x, g.y[v] - b.y))

    def _row_distance(self, frm: int, to: int) -> float:
        if frm == to:
            return 0.0
        d = self._row(frm)[to]
        return d if math.isfinite(d) else UNREACHABLE_M

    def _row(self, frm: int) -> List[float]:
        """取（并按需计算）从 frm 出发到所有节点的最短路距离行。"""
        cached = self._dist_rows.get(frm)
        if cached is not None:
            return cached
        if len(self._dist_rows) >= MAX_ROWS:
            self._dist_rows.clear()
        self.dijkstra_runs += 1

        g = self.g
        n = g.node_count
        dist = [float("inf")] * n
        dist[frm] = 0.0
        done = bytearray(n)
        heap = [(0.0, frm)]

        while heap:
            d, u = heapq.heappop(heap)
            if done[u]:
                continue
            done[u] = 1
            for a in range(g.adj_start[u], g.adj_start[u + 1]):
                e = g.adj_edge[a]
                v = g.edge_to[e]
                nd = d + g.edge_len[e]
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(heap, (nd, v))

        self._dist_rows[frm] = dist
        return dist

    # ------------------------------------------------------------ 路径

    def path(self, a: Pt, b: Pt) -> List[Pt]:
        u, v = self.snap(a), self.snap(b)
        if u < 0 or v < 0:
            return [a, b]
        if u == v:
            # 同一个吸附点：起终之间没有可走的路，就是一段直线
            return [a, b]

        key = (u << 32) | (v & 0xFFFFFFFF)
        nodes = self._path_cache.get(key)
        if nodes is None:
            nodes = self._astar(u, v)
            if len(self._path_cache) >= MAX_PATHS:
                self._path_cache.clear()
            self._path_cache[key] = nodes
        if not nodes:
            return [a, b]                  # 不可达：退化成直线，至少画得出东西

        # 折线 = [真实起点] + [吸附点 … 吸附点] + [真实终点]，
        # 这样它的几何长度就正好等于 metres(a,b)（吸附偏移 + 图上的路 + 吸附偏移），
        # 于是 Rider 那边的 leg_scale 会精确地等于 1。
        out: List[Pt] = []
        if a.straight(nodes[0]) > 0.05:
            out.append(a)
        out.extend(nodes)
        if b.straight(nodes[-1]) > 0.05:
            out.append(b)
        return out

    def _astar(self, frm: int, to: int) -> List[Pt]:
        """A*：启发式用直线距离，在路网上比纯 Dijkstra 快得多。"""
        self.astar_runs += 1
        g = self.g
        n = g.node_count
        g_score = [float("inf")] * n
        prev = [-1] * n
        closed = bytearray(n)
        tx, ty = g.x[to], g.y[to]

        g_score[frm] = 0.0
        heap = [(math.hypot(g.x[frm] - tx, g.y[frm] - ty), frm)]

        while heap:
            _, u = heapq.heappop(heap)
            if closed[u]:
                continue
            closed[u] = 1
            if u == to:
                break
            for a in range(g.adj_start[u], g.adj_start[u + 1]):
                e = g.adj_edge[a]
                v = g.edge_to[e]
                if closed[v]:
                    continue
                tentative = g_score[u] + g.edge_len[e]
                if tentative < g_score[v]:
                    g_score[v] = tentative
                    prev[v] = u
                    heapq.heappush(heap, (tentative + math.hypot(g.x[v] - tx, g.y[v] - ty), v))

        if not math.isfinite(g_score[to]):
            return []
        rev = []
        cur = to
        while cur != -1:
            rev.append(g.point(cur))
            if cur == frm:
                break
            cur = prev[cur]
        rev.reverse()
        return rev

    @property
    def reversal_safe(self) -> bool:
        return False      # 单行道：反转一段路线是非法的走法

    @property
    def name(self) -> str:
        return f"真实路网（{self.g.name}）"

    # ------------------------------------------------------------ 布点

    def random_node_near(self, center: Pt, radius: float, rng) -> Pt:
        """在离 center 给定半径内，随机挑一个**路网节点**。

        路网模式下所有的商家和顾客地址都必须落在路上，否则「吸附」出来的
        连接段会画成一条脱离道路的直线，看着就不对。
        """
        g = self.g
        c0 = self._cell_of(center.x, center.y)
        cx0, cy0 = c0 % self.grid_w, c0 // self.grid_w
        reach = max(1, int(math.ceil(radius / self.cell_m)))
        cands = []

        for cy in range(max(0, cy0 - reach), min(self.grid_h - 1, cy0 + reach) + 1):
            for cx in range(max(0, cx0 - reach), min(self.grid_w - 1, cx0 + reach) + 1):
                cell = cy * self.grid_w + cx
                for a in range(self.cell_start[cell], self.cell_start[cell + 1]):
                    v = self.cell_nodes[a]
                    if math.hypot(g.x[v] - center.x, g.y[v] - center.y) <= radius:
                        cands.append(v)
        if not cands:
            return g.point(self.snap(center))

        # 太近的排除掉，否则所有顾客都会挤在商家门口
        min_d = radius * 0.2
        for _ in range(40):
            v = cands[rng.randrange(len(cands))]
            if math.hypot(g.x[v] - center.x, g.y[v] - center.y) >= min_d:
                return g.point(v)
        return g.point(cands[rng.randrange(len(cands))])

    def spread_nodes(self, count: int, rng) -> List[Pt]:
        """用「最远点采样」挑 count 个尽量分散的路网节点。

        用它来布商家和骑手：随机撒点容易全挤在市中心一小块，
        而最远点采样能保证覆盖整个区域，演示时好看得多。
        """
        g = self.g
        chosen: List[Pt] = []
        if g.node_count == 0:
            return chosen

        chosen.append(g.point(rng.randrange(g.node_count)))
        best = [float("inf")] * g.node_count

        while len(chosen) < count:
            last = chosen[-1]
            for i in range(g.node_count):
                d = math.hypot(g.x[i] - last.x, g.y[i] - last.y)
                if d < best[i]:
                    best[i] = d
            pick, pick_d = -1, -1.0
            for i in range(g.node_count):
                if best[i] > pick_d:
                    pick_d, pick = best[i], i
            if pick < 0:
                break
            chosen.append(g.point(pick))
        return chosen


def _snap_key(p: Pt) -> int:
    # 0.1m 精度就够区分不同地址了，同时能把同一个点的多次调用合并成一次
    return int(round(p.x * 10)) * 1_000_000_007 + int(round(p.y * 10))
