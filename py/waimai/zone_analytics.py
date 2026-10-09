"""区域研判：把地图切成网格，按格子统计单量 / 准时率 / 运力，给出「哪里要加人、哪里可以轮休」。

对应 Java 版 ZoneAnalytics。

为什么是网格热力图而不是模糊的热力斑块：运营要知道的是「这一块该加几个人」，
模糊的斑块好看但没法对应到具体决策。网格能直接把每个格子的需求、运力、缺口列成表，
颜色只是让人一眼看出哪里有压力。

运力模型是「观测值」而不是拍脑袋的常数：每个骑手每小时能送多少单，是算出来的。
样本太少时返回 None，界面显示「数据不足」而不是编一个数。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from .model import OrderStatus, RiderStatus
from .world import START_SECONDS

# 观测产能的最小样本量：少于这些就认为数据不足
MIN_SERVICE_SAMPLES = 3
MIN_IN_FLIGHT = 5
MIN_DELIVERED_SAMPLES = 3


@dataclass
class Zone:
    """一片区域的研判结果。"""

    row: int
    col: int
    x0: float
    y0: float
    x1: float
    y1: float

    orders: int            # 窗口内下单量（按送达点归属）
    delivered: int         # 窗口内已完成
    on_time: int
    late: int
    active: int            # 当前在途
    merchant_orders: int   # 取餐点落在本格的窗口内单量
    riders_here: int       # 骑手此刻物理位置在本格
    serving_riders: int    # 有订单落在本格的骑手数（运力投入）
    rider_load: int        # 这些骑手手上的总单量

    avg_total_min: float
    avg_wait_dispatch_min: float
    on_time_rate: Optional[float]    # None = 窗口内还没有已完成单
    demand_per_hour: float
    capacity_per_hour: float
    pressure: Optional[float]        # 需求 / 运力；None = 没有运力或数据不足
    need_riders: int
    delta: int                       # 正数=建议增派，负数=建议撤下
    verdict: str                     # SURGE / TIGHT / QUALITY / OK / SLACK / IDLE
    cause: str                       # 一句话说明为什么这么判


@dataclass
class Report:
    cells_x: int
    cells_y: int
    cell_m: float
    window_min: int
    hours_elapsed: float
    throughput_per_rider_hour: Optional[float]
    throughput_method: Optional[str]   # little / delivered / None
    riders_online: int
    total_demand_per_hour: float
    global_need_riders: int
    suggest_add: int
    suggest_rest: int
    data_sufficient: bool
    zones: List[Zone] = field(default_factory=list)


VERDICT_LABELS = {
    "SURGE": "需要增派",
    "TIGHT": "偏紧",
    "QUALITY": "准时率偏低",
    "OK": "正常",
    "SLACK": "单少可轮休",
    "IDLE": "无单",
}

_VERDICT_RANK = {"SURGE": 0, "TIGHT": 1, "QUALITY": 2, "OK": 3, "SLACK": 4, "IDLE": 5}


def verdict_label(v: str) -> str:
    return VERDICT_LABELS.get(v, v)


def analyze(world, window_min: int, cells: int) -> Report:
    """算一份研判报告。

    window_min <= 0 表示「全部历史」；cells 是长边切几格。
    """
    cells = max(3, min(24, cells))
    cell_m = max(world.width_m, world.height_m) / cells
    cells_x = max(1, math.ceil(world.width_m / cell_m))
    cells_y = max(1, math.ceil(world.height_m / cell_m))

    now = world.now()
    if window_min > 0:
        since = max(START_SECONDS, now - window_min * 60)
    else:
        since = START_SECONDS
    hours = max(1.0 / 60, (now - since) / 3600.0)

    n = cells_x * cells_y
    orders = [0] * n
    delivered = [0] * n
    on_time = [0] * n
    late = [0] * n
    active = [0] * n
    merchant_orders = [0] * n
    total_sec_sum = [0] * n
    total_sec_cnt = [0] * n
    wait_sec_sum = [0] * n
    wait_sec_cnt = [0] * n
    serving = [set() for _ in range(n)]

    sla_sec = world.cfg.sla_minutes * 60

    for o in world.orders.values():
        di = _idx_of(world, o.dest_pt, cell_m, cells_x, cells_y)
        mi = _idx_of(world, o.merchant_pt, cell_m, cells_x, cells_y)
        in_window = o.created_at >= since

        if in_window and di >= 0:
            orders[di] += 1
        if in_window and mi >= 0:
            merchant_orders[mi] += 1

        # 在途：不管什么时候下的，只要还没送达就算当前压力
        if di >= 0 and o.status in (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP):
            active[di] += 1

        # 已完成：按「送达时间」落窗口，这样窗口内的服务品质才是当期的
        if o.status is OrderStatus.DELIVERED and o.delivered_at >= since and di >= 0:
            delivered[di] += 1
            t = o.total_sec
            if t is not None:
                total_sec_sum[di] += t
                total_sec_cnt[di] += 1
                if t <= sla_sec:
                    on_time[di] += 1
                else:
                    late[di] += 1
            w = o.wait_dispatch_sec
            if w is not None:
                wait_sec_sum[di] += w
                wait_sec_cnt[di] += 1

        # 运力投入：这个骑手此刻在为哪些格子服务
        if (o.rider_id and di >= 0
                and o.status in (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP)):
            serving[di].add(o.rider_id)

    riders_here = [0] * n
    for r in world.riders.values():
        ri = _idx_of(world, r.pos, cell_m, cells_x, cells_y)
        if ri >= 0:
            riders_here[ri] += 1

    # ---- 观测产能 ----
    # 两种估法，优先用更稳的那个：
    #
    # ① 数送达量：窗口内送达 ÷ 骑手数 ÷ 小时数。直观，但**开局阶段会严重低估** ——
    #    骑手手上正攒着第一轮单还没送出去，送达量远远落后于实际干活的速度，
    #    于是「建议增派」会被算得虚高。
    #
    # ② Little 法则：在制品 ÷ 周期时间。整个车队每小时能完成多少单，
    #    等于「同时在手上的单量 ÷ 每单平均耗时」。
    #
    #    这里有个容易踩的坑：周期时间必须用**派单到送达**（骑手真正在服务的时长），
    #    不能用「下单到送达」—— 后者把在订单池里排队等派单的时间也算进去了。
    #    一旦积压，排队时间会占大头，周期时间被撑大，产能被算得极低，
    #    于是「需要增派 76 人」这种离谱结论就出来了。
    delivered_in_window = 0
    service_sum = 0
    service_cnt = 0
    for o in world.orders.values():
        if o.status is not OrderStatus.DELIVERED:
            continue
        if o.delivered_at >= since:
            delivered_in_window += 1
        if o.dispatched_at > 0 and o.delivered_at > o.dispatched_at:
            service_sum += o.delivered_at - o.dispatched_at   # 服务时长，不含排队
            service_cnt += 1

    riders_online = sum(1 for r in world.riders.values()
                        if r.status is RiderStatus.ONLINE)
    riders_total = len(world.riders)

    in_flight = sum(1 for o in world.orders.values()
                    if o.status in (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP))

    throughput: Optional[float] = None
    method: Optional[str] = None

    if riders_total > 0 and service_cnt >= MIN_SERVICE_SAMPLES and in_flight >= MIN_IN_FLIGHT:
        service_hours = service_sum / 60.0 / service_cnt / 60.0   # 分钟 → 小时
        if service_hours > 0:
            throughput = (in_flight / service_hours) / riders_total
            method = "little"
    if throughput is None and delivered_in_window >= MIN_DELIVERED_SAMPLES \
            and riders_total > 0 and hours > 0.05:
        throughput = delivered_in_window / riders_total / hours
        method = "delivered"

    # ---- 逐格研判 ----
    zones: List[Zone] = []
    suggest_add = 0
    suggest_rest = 0

    for i in range(n):
        row, col = divmod(i, cells_x)
        x0, y0 = col * cell_m, row * cell_m

        demand_per_hour = orders[i] / hours
        serving_n = len(serving[i])
        load_sum = sum(world.riders[rid].active_count()
                       for rid in serving[i] if rid in world.riders)

        capacity = 0.0 if throughput is None else serving_n * throughput
        if throughput is not None and capacity > 0:
            pressure = demand_per_hour / capacity
        elif throughput is not None and demand_per_hour <= 0:
            pressure = 0.0
        else:
            pressure = None

        if throughput is None or throughput <= 0:
            need = serving_n
            delta = 0
        else:
            need = math.ceil(demand_per_hour / throughput)
            delta = need - serving_n

        on_time_rate = None
        if delivered[i] > 0:
            on_time_rate = on_time[i] * 100.0 / delivered[i]

        avg_total = 0.0 if total_sec_cnt[i] == 0 else total_sec_sum[i] / 60.0 / total_sec_cnt[i]
        avg_wait = 0.0 if wait_sec_cnt[i] == 0 else wait_sec_sum[i] / 60.0 / wait_sec_cnt[i]

        verdict = _verdict_of(orders[i], delivered[i], on_time_rate, pressure, serving_n, delta)
        cause = _cause_of(verdict, pressure, serving_n, on_time_rate, avg_wait, delta)

        if delta > 0 and orders[i] > 0:
            suggest_add += delta
        if delta < 0:
            suggest_rest += -delta

        zones.append(Zone(
            row=row, col=col, x0=x0, y0=y0, x1=x0 + cell_m, y1=y0 + cell_m,
            orders=orders[i], delivered=delivered[i], on_time=on_time[i], late=late[i],
            active=active[i], merchant_orders=merchant_orders[i],
            riders_here=riders_here[i], serving_riders=serving_n, rider_load=load_sum,
            avg_total_min=round(avg_total, 1), avg_wait_dispatch_min=round(avg_wait, 1),
            on_time_rate=None if on_time_rate is None else round(on_time_rate, 1),
            demand_per_hour=round(demand_per_hour, 1), capacity_per_hour=round(capacity, 1),
            pressure=None if pressure is None else round(pressure, 2),
            need_riders=need, delta=delta, verdict=verdict, cause=cause,
        ))

    # 有单的排前面，其次按压力/单量降序，让运营一眼看到重点
    zones.sort(key=lambda z: (_VERDICT_RANK.get(z.verdict, 9), -z.orders))

    total_demand = sum(orders) / hours
    if throughput is None or throughput <= 0:
        global_need = riders_online
    else:
        global_need = math.ceil(total_demand / throughput)

    # 全局可休息人数不能把运力砍到需求之下
    rest_cap = max(0, riders_online - global_need)
    suggest_rest = min(suggest_rest, rest_cap)

    return Report(
        cells_x=cells_x, cells_y=cells_y, cell_m=round(cell_m, 1),
        window_min=window_min, hours_elapsed=round(hours, 1),
        throughput_per_rider_hour=None if throughput is None else round(throughput, 2),
        throughput_method=method,
        riders_online=riders_online, total_demand_per_hour=round(total_demand, 1),
        global_need_riders=global_need, suggest_add=suggest_add, suggest_rest=suggest_rest,
        data_sufficient=throughput is not None, zones=zones,
    )


def _idx_of(world, p, cell_m: float, cells_x: int, cells_y: int) -> int:
    """格子定位；点在范围外返回 -1（浮点误差可能让边缘点刚好越界，夹一下）。"""
    if p.x < -cell_m or p.y < -cell_m:
        return -1
    col = max(0, min(cells_x - 1, int(math.floor(p.x / cell_m))))
    row = max(0, min(cells_y - 1, int(math.floor(p.y / cell_m))))
    return row * cells_x + col


def _verdict_of(orders: int, delivered: int, on_time_rate: Optional[float],
                pressure: Optional[float], serving_riders: int, delta: int) -> str:
    """给一个格子下结论。

    关键是把两类问题分开 —— 它们看着都是「超时」，但处方完全不同：

    * **SURGE（运力不足）**：需求压过运力，或者有单却完全没人管。处方是加人。
    * **QUALITY（准时率偏低）**：单量不多、运力也够（压力低甚至建议撤人），
      但准时率掉下去了。这种情况多半不是本地运力问题，而是**等派单太久**
      （2 分钟节拍 + 排队）或者商家出餐慢。处方是查调度/商家，不是往这块加骑手 ——
      把它判成「需要增派」会把人派到本来就已经富余的地方去。
    """
    if orders == 0 and delivered == 0 and serving_riders == 0:
        return "IDLE"

    no_capacity = orders > 0 and serving_riders == 0
    pressure_high = pressure is not None and pressure >= 1.2
    quality_bad = on_time_rate is not None and on_time_rate < 90 and delivered >= 3

    if no_capacity or pressure_high:
        return "SURGE"
    if quality_bad:
        # 运力明明够（还建议撤人）却不准时 → 是调度排队的问题，不是缺人
        return "QUALITY" if delta <= 0 else "SURGE"
    if pressure is not None and pressure >= 0.8:
        return "TIGHT"
    if orders == 0 and serving_riders > 0:
        return "SLACK"
    if orders > 0 or pressure is not None:
        return "OK"
    return "IDLE"


def _cause_of(verdict: str, pressure: Optional[float], serving_riders: int,
              on_time_rate: Optional[float], avg_wait: float, delta: int) -> str:
    """一句话说明「为什么这么判」，让人知道该改什么。"""
    p = _fmt2(pressure)
    otr = _fmt1(on_time_rate)
    if verdict == "SURGE":
        return ("本区有单但没有任何骑手在服务" if serving_riders == 0
                else "需求超过运力")
    if verdict == "QUALITY":
        if avg_wait > 8:
            return (f"运力够（压力 {p}），准时 {otr}% 偏低；平均等派单 {avg_wait:.1f} 分钟，"
                    f"更可能是派单排队而不是缺人")
        return f"运力够（压力 {p}）但准时仅 {otr}%，需排查商家出餐或路况"
    if verdict == "TIGHT":
        return f"运力偏紧，压力 {p}"
    if verdict == "SLACK":
        return f"本区没有新单，可撤下 {abs(delta)} 人"
    if verdict == "OK":
        return "运力与需求匹配"
    return ""


def _fmt1(v: Optional[float]) -> str:
    return "—" if v is None else str(round(v, 1))


def _fmt2(v: Optional[float]) -> str:
    return "—" if v is None else str(round(v, 2))


def to_json(rep: Report) -> dict:
    """转成给前端 / 大模型看的扁平结构。"""
    method_labels = {
        "little": "Little 法则（在制品 ÷ 服务时长）",
        "delivered": "窗口内送达量统计",
    }
    return {
        "cellsX": rep.cells_x,
        "cellsY": rep.cells_y,
        "cellM": rep.cell_m,
        "windowMin": rep.window_min,
        "hoursElapsed": rep.hours_elapsed,
        "throughputPerRiderHour": rep.throughput_per_rider_hour,
        "throughputMethod": rep.throughput_method,
        "throughputMethodLabel": method_labels.get(rep.throughput_method, "数据不足"),
        "ridersOnline": rep.riders_online,
        "totalDemandPerHour": rep.total_demand_per_hour,
        "globalNeedRiders": rep.global_need_riders,
        "suggestAdd": rep.suggest_add,
        "suggestRest": rep.suggest_rest,
        "dataSufficient": rep.data_sufficient,
        "zones": [
            {
                "row": z.row, "col": z.col,
                "x0": z.x0, "y0": z.y0, "x1": z.x1, "y1": z.y1,
                "orders": z.orders, "ordersActive": z.active, "delivered": z.delivered,
                "onTime": z.on_time, "late": z.late,
                "merchantOrders": z.merchant_orders,
                "ridersHere": z.riders_here, "servingRiders": z.serving_riders,
                "riderLoad": z.rider_load,
                "avgTotalMin": z.avg_total_min,
                "avgWaitDispatchMin": z.avg_wait_dispatch_min,
                "onTimeRate": z.on_time_rate,
                "demandPerHour": z.demand_per_hour,
                "capacityPerHour": z.capacity_per_hour,
                "pressure": z.pressure,
                "needRiders": z.need_riders,
                "delta": z.delta,
                "verdict": z.verdict,
                "verdictLabel": verdict_label(z.verdict),
                "cause": z.cause,
            }
            for z in rep.zones
        ],
    }
