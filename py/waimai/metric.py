"""距离/路径抽象。对应 Java 版的 Metric / StraightMetric。

上层（插入启发式、派单器、模拟器）只认这个接口，不关心底下是「直线 × 1.4」
还是「路网最短路」。移植路网模式时只要再写一个 RoadMetric 实现即可，上层不用动。

**实现必须满足三角不等式**：metres(a,c) <= metres(a,b) + metres(b,c)。
RoutePlanner.best_insertion 里有基于当前最优值的剪枝，而那个剪枝的正确性依赖
「插入代价非负」，后者又直接来自三角不等式。一旦被破坏，更优的候选会被静默剪掉 ——
结果是算错，而不只是不够优。

因此：不可达的点对必须返回一个**足够大的有限常数**，不能返回 inf。
常数 C 在「所有真实距离都远小于 C」时仍然满足三角不等式（a≤b+C、b≤a+C、C≤C+C），
而 inf 会让 inf - inf = nan 直接污染代价计算。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from .model import Pt


class Metric(ABC):
    """两点之间的行驶代价。"""

    @abstractmethod
    def metres(self, a: Pt, b: Pt) -> float:
        """两点之间的行驶距离（米）。"""

    @abstractmethod
    def path(self, a: Pt, b: Pt) -> List[Pt]:
        """两点之间的实际路径（含起点和终点）。"""

    @property
    def reversal_safe(self) -> bool:
        """路线区间反转是否物理可行。

        2-opt 会把一段路线的行进方向整个反过来，这在单行道上不合法，
        所以路网模式返回 False，RoutePlanner.improve 会跳过 2-opt、
        只用 Or-opt（只搬移片段、不反转，方向安全）。
        """
        return True

    @property
    @abstractmethod
    def name(self) -> str:
        """供界面展示的名字。"""


class StraightMetric(Metric):
    """抽象城市模式的距离：直线距离 × 1.4，当作街面里程的近似。"""

    STREET_FACTOR = 1.4

    def metres(self, a: Pt, b: Pt) -> float:
        return a.straight(b) * self.STREET_FACTOR

    def path(self, a: Pt, b: Pt) -> List[Pt]:
        return [a, b]

    @property
    def name(self) -> str:
        return "抽象城市（1.4 × 直线距离）"
