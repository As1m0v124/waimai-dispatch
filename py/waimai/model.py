"""数据模型：点、商家、站点、订单、骑手，以及可调参数。

对应 Java 版的 Pt / Merchant / Stop / Order / Rider / OrderEvent / World.Config。
一比一对应是为了方便两边对照着看。

Python 这边能用 @dataclass，比 Java 少写很多样板代码；
但真正的领域逻辑（五个时间点、载客上限、路线缓存）都照搬，行为要一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set


# --------------------------------------------------------------- 点

@dataclass(frozen=True)
class Pt:
    """城市平面上的一个点，单位：米。"""

    x: float
    y: float

    def straight(self, other: "Pt") -> float:
        """直线距离（米）。"""
        return math.hypot(self.x - other.x, self.y - other.y)


# --------------------------------------------------------------- 商家

@dataclass
class Merchant:
    id: str
    name: str
    pt: Pt
    prep_sec: int          # 出餐所需时间（秒）


# --------------------------------------------------------------- 站点

class StopType(Enum):
    PICKUP = "PICKUP"
    DELIVERY = "DELIVERY"


@dataclass(frozen=True)
class Stop:
    """骑手路线上的一个站点：到商家取餐，或到顾客处送达。"""

    order_id: str
    type: StopType
    pt: Pt

    @property
    def is_pickup(self) -> bool:
        return self.type is StopType.PICKUP

    @property
    def is_delivery(self) -> bool:
        return self.type is StopType.DELIVERY

    def __str__(self) -> str:            # 调试用
        return ("取:" if self.is_pickup else "送:") + self.order_id


# --------------------------------------------------------------- 订单

class OrderStatus(Enum):
    POOLED = "POOLED"        # 在订单池
    ASSIGNED = "ASSIGNED"    # 已派单
    PICKED_UP = "PICKED_UP"  # 已取餐
    DELIVERED = "DELIVERED"  # 已送达


class AssignMode(Enum):
    AUTO = "AUTO"                    # 平台自动派单
    MANUAL_ASSIGN = "MANUAL_ASSIGN"  # 指派单
    REASSIGN = "REASSIGN"            # 调单


@dataclass
class OrderEvent:
    """订单全过程的一个事件，用于审计。"""

    t: int
    type: str
    detail: str


@dataclass
class Order:
    """一笔订单，携带全过程五个时间点。

    ① created_at  下单   ② dispatched_at 派单   ③ arrived_store_at 到店
    ④ picked_at   取餐   ⑤ delivered_at  送达

    时间戳字段用 0 表示「还没发生」—— 所以模拟时钟从 10:00:00 起步而不是 0，
    否则这两个含义会撞在一起。
    """

    id: str
    customer_name: str
    phone: str
    address: str
    note: str
    merchant_id: str
    merchant_pt: Pt
    dest_pt: Pt
    created_at: int
    ready_at: int                     # 商家预计出餐时刻
    status: OrderStatus = OrderStatus.POOLED

    rider_id: Optional[str] = None
    mode: Optional[AssignMode] = None
    tier: Optional[int] = None        # 1 顺路 / 2 无单 / 3 其他
    detour_m: float = 0.0             # 派单时给骑手路线带来的额外里程
    reassign_count: int = 0
    # 被推迟派单的次数（见 Config.postpone_poor_assignments）
    postpone_count: int = 0

    dispatched_at: int = 0
    arrived_store_at: int = 0
    picked_at: int = 0
    delivered_at: int = 0

    events: List[OrderEvent] = field(default_factory=list)

    @staticmethod
    def create(order_id: str, customer_name: str, phone: str, address: str, note: str,
               merchant: Merchant, dest: Pt, created_at: int) -> "Order":
        return Order(
            id=order_id, customer_name=customer_name, phone=phone,
            address=address, note=note,
            merchant_id=merchant.id, merchant_pt=merchant.pt, dest_pt=dest,
            created_at=created_at, ready_at=created_at + merchant.prep_sec,
        )

    def event(self, t: int, type_: str, detail: str) -> None:
        self.events.append(OrderEvent(t, type_, detail))

    # -------- 派单耗时口径（秒）；没发生就返回 None --------

    @property
    def wait_dispatch_sec(self) -> Optional[int]:
        """顾客等了多久才被派单。"""
        return self.dispatched_at - self.created_at if self.dispatched_at > 0 else None

    @property
    def to_store_sec(self) -> Optional[int]:
        """骑手多久赶到商家。"""
        if self.dispatched_at > 0 and self.arrived_store_at > 0:
            return self.arrived_store_at - self.dispatched_at
        return None

    @property
    def prep_wait_sec(self) -> Optional[int]:
        """到店后等出餐等了多久。"""
        if self.arrived_store_at > 0 and self.picked_at > 0:
            return self.picked_at - self.arrived_store_at
        return None

    @property
    def on_road_sec(self) -> Optional[int]:
        """路上送了多久。"""
        if self.picked_at > 0 and self.delivered_at > 0:
            return self.delivered_at - self.picked_at
        return None

    @property
    def total_sec(self) -> Optional[int]:
        """顾客总共等了多久。"""
        return self.delivered_at - self.created_at if self.delivered_at > 0 else None


# --------------------------------------------------------------- 骑手

class RiderStatus(Enum):
    ONLINE = "ONLINE"    # 上线，正常接单
    BUSY = "BUSY"        # 忙碌，系统不派给他


class Motion(Enum):
    IDLE = "IDLE"        # 空闲
    MOVING = "MOVING"    # 赶路中
    WAITING = "WAITING"  # 在商家等出餐


@dataclass
class Rider:
    id: str
    name: str
    phone: str
    pos: Pt
    max_orders: int = 5
    speed_mpm: float = 320.0          # 米/分钟
    status: RiderStatus = RiderStatus.ONLINE

    # 待服务站点，按顺序执行；只保留尚未完成的
    route: List[Stop] = field(default_factory=list)

    motion: Motion = Motion.IDLE
    wait_until: int = 0
    wait_order_id: Optional[str] = None

    delivered_count: int = 0
    distance_m: float = 0.0

    # ---- 当前正在走的一段路 ----
    # 从当前位置到 route[0] 的折线（含两端）。None 表示需要重新规划。
    leg: Optional[List[Pt]] = None
    leg_done: float = 0.0     # 已沿 leg 走过的几何长度（米）
    leg_total: float = 0.0    # leg 的几何总长
    leg_scale: float = 1.0    # 行驶里程 / 几何长度：抽象模式 1.4，路网模式 1.0

    # 从 route[0] 往后、沿路一直走到最后一站的整条折线（供前端画线）。
    # 刻意不含「当前位置到首站」那一段 —— 那一段每时每刻都在变，单独由 leg 提供。
    _tail: Optional[List[Pt]] = None

    def active_order_ids(self) -> Set[str]:
        """手上还没送达的订单号。"""
        return {s.order_id for s in self.route}

    def active_count(self) -> int:
        return len(self.active_order_ids())

    def can_take(self) -> bool:
        return self.status is RiderStatus.ONLINE and self.active_count() < self.max_orders

    def has_orders(self) -> bool:
        return len(self.route) > 0

    def invalidate_route(self) -> None:
        """路线一旦变化就把缓存的路径全部作废，下一拍按新路线重新规划。

        必须在这几处调用：派单写入新路线、调单摘掉订单、骑手到站、
        以及等出餐时把可送的站点提到队首。
        漏掉任何一处，骑手就会沿一条已经不存在的路继续走，或者前端画出错误的路线。
        """
        self.leg = None
        self.leg_done = 0.0
        self.leg_total = 0.0
        self.leg_scale = 1.0
        self._tail = None

    def tail_path(self, metric, tol_m: float) -> List[Pt]:
        """从首站到最后一站的沿路折线（带缓存）。

        路径规划较贵，所以只在路线变化后重算一次；抽稀是因为一条沿路路线
        动辄上百个点，抽稀后往往只剩十几个，而画出来肉眼没差别 ——
        这直接决定了每 500ms 一次的轮询响应大小。
        """
        if self._tail is not None:
            return self._tail
        if not self.route:
            self._tail = []
            return self._tail
        out: List[Pt] = [self.route[0].pt]
        for i in range(len(self.route) - 1):
            seg = metric.path(self.route[i].pt, self.route[i + 1].pt)
            # seg 的首点就是上一段的末点，跳过以免重复
            out.extend(seg[1:])
        from . import geom
        self._tail = geom.simplify(out, tol_m)
        return self._tail


# --------------------------------------------------------------- 可调参数

@dataclass
class Config:
    """可调参数。默认值是按「池子常年 0~3 单、骑手负载 30%~40%」调过的。"""

    dispatch_interval_sec: int = 120      # 派单节拍：每 2 分钟
    on_route_max_detour_m: float = 1000   # 顺路判定①：绝对绕路阈值
    on_route_max_detour_ratio: float = 0.6  # 顺路判定②：绕路 / 本单长度
    load_penalty_per_order_m: float = 400  # 负载均衡：每持有一单的惩罚里程
    default_max_orders: int = 5
    candidate_radius_m: float = 6000      # 候选骑手：离商家最远距离
    sla_minutes: int = 45                 # 准时率口径
    avoid_waiting: bool = True            # 出餐没好时先去送手上的餐
    auto_order: bool = True
    auto_order_every_sec: int = 60        # 自动下单间隔
    auto_order_jitter_sec: int = 25
    warmup_orders: int = 6
    zone_window_min: int = 60             # 区域研判的统计窗口；0 = 全部历史
    heat_cells: int = 8                   # 热力图把长边切成几格

    # ---- 停单（进单限制）----
    #
    # 站点不可能无限制接单。高峰期订单池堆一大堆待派单，既不现实也没意义 ——
    # 骑手根本送不过来，那些单只会一直等到超时。
    #
    # 所以当**待派单占比**达到阈值时就自动停单：
    #     待派单 ÷ (待派单 + 配送中)  ≥  stop_accept_pool_ratio
    #
    # 为什么用占比而不是「池子里有多少单」：10 单在 10 单的系统里是 100%（该停了），
    # 但在 200 单的系统里只占 5%（完全正常）。只数绝对值会把两种完全不同的情况
    # 混为一谈。
    #
    # 为什么还要一个最小单量下限：开局只有 1 单待派、0 单在途时占比是 100%，
    # 但这时候显然不该停单。少了这个下限，系统一启动就把自己锁死了。

    # 进单总开关：管理平台手动控制，关掉就一单都不进
    accept_orders: bool = True
    # 待派单占比达到这个值就自动停单（0~1）
    stop_accept_pool_ratio: float = 0.5
    # 当前单量低于这个数时不触发自动停单（防止开局误停）
    stop_accept_min_orders: int = 8
    # 系统里累计订单超过这个数就不再自动下单（防止长时间运行涨到爆）
    max_total_orders: int = 1500

    # ---- 推迟派单（Anticipatory Customer Assignment 的核心思想）----
    #
    # 参考 Ulmer et al. (2021) 的 ACA / TristanKruse 的 RMDP_Algorithm：
    # **不要订单一来就派出去，该等的时候要等。**
    #
    # 具体到这里的场景：如果一笔单最好的归宿也只是「兜底骑手」（要绕一大圈），
    # 那不如先不派 —— 下一轮可能来几笔同商家或同方向的单，届时它就能变成
    # 「顺路骑手」，多绕几百米顺走。这是真正把兜底转化成顺路的办法，
    # 而不是把顺路的判定阈值放宽（那只是把标签改好看）。
    #
    # 两条保险，避免等到超时：
    #   · 池子深（运力紧张）时不等 —— 越等越糟，必须立刻派出去
    #   · 等太久（postpone_max_wait_min）或推太多次就强制派出
    postpone_poor_assignments: bool = True
    # 只在待派单不超过「骑手数 × 这个倍数」时才推迟。池子深说明运力已经吃紧，
    # 越等越糟，这时候必须立刻派出去。
    postpone_max_pool_factor: float = 1.0
    # 一笔单最多等这么多分钟就必须派出去
    postpone_max_wait_min: int = 6
    # 一笔单最多被推迟几轮
    postpone_max_rounds: int = 3

    # 随机探索比例（ε-greedy）。默认很小：约 5% 的兜底单会**反着**处理
    # （该推迟的立刻派 / 该派的多等一轮）。
    #
    # 这不是"故意做坏事"，而是策略学习的前提。正常运行时策略是固定的，
    # 被推迟的单和没被推迟的单**不是同一批单**（本来就难派的才会被推迟），
    # 直接比较会把这种选择偏差当成"推迟有害/有益"。随机化一小部分决策之后，
    # 这部分样本里"推迟与否"与上下文无关，对照才是无偏的。
    # 代价是 ε 比例的单按非最优策略处理 —— 所以默认只有 0.05。
    explore_epsilon: float = 0.05

    def copy(self) -> "Config":
        return Config(**vars(self))
