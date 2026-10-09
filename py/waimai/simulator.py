"""模拟器：推进模拟时钟，让骑手沿路线跑，并打上订单的五个时间点。

对应 Java 版 Simulator。

① 下单 created_at   ② 派单 dispatched_at   ③ 到店 arrived_store_at
④ 取餐 picked_at    ⑤ 送达 delivered_at

时钟按固定的 1 模拟秒为步长推进（而不是一次跳一大步），
这样无论倍速多高，事件的先后顺序和到达时刻都是稳定的。
"""

from __future__ import annotations

import threading
import time

from . import geom, telemetry
from .dispatcher import round_ as dispatch_round
from .model import Motion, Order, OrderStatus, Pt, Rider, StopType
from .world import START_SECONDS, format_clock


class Simulator:
    """在后台线程里按真实时间推进模拟时钟。"""

    def __init__(self, world, tick_ms: int = 100):
        self.world = world
        self.tick_ms = tick_ms
        self._running = True
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="simulator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        world = self.world
        last = time.monotonic()
        while self._running:
            time.sleep(self.tick_ms / 1000.0)
            now = time.monotonic()
            real_dt = now - last
            last = now
            if real_dt > 1.0:
                real_dt = 1.0                # 卡顿后不要一次性补太多
            with world.lock:
                if not world.paused:
                    advance(world, real_dt * world.speed_factor)


def advance(world, sim_dt: float) -> None:
    """推进 sim_dt 模拟秒。"""
    left = sim_dt
    steps = 0
    while left > 1e-9 and steps < 20000:
        steps += 1
        dt = min(1.0, left)
        _step(world, dt)
        left -= dt


def _step(world, dt: float) -> None:
    """走一步：时钟 + 骑手移动 + 自动下单 + 到点派单。"""
    world.sim_seconds += dt
    for rider in world.riders.values():
        _move(world, rider, dt)
    _auto_order(world)
    _maybe_dispatch(world)


# ------------------------------------------------------------ 骑手移动

def _move(world, rider: Rider, dt: float) -> None:
    if not rider.route:
        if rider.motion is not Motion.WAITING:
            rider.motion = Motion.IDLE
        rider.invalidate_route()
        return

    # 在商家等出餐
    if rider.motion is Motion.WAITING:
        if world.now() < rider.wait_until:
            return
        waiting = world.orders.get(rider.wait_order_id) if rider.wait_order_id else None
        if waiting is not None and waiting.picked_at == 0:
            _complete_pickup(world, rider, waiting)
        rider.wait_order_id = None
        rider.wait_until = 0
        rider.invalidate_route()
        rider.motion = Motion.IDLE if not rider.route else Motion.MOVING
        if not rider.route:
            return

    budget = rider.speed_mpm / 60.0 * dt      # 本步能跑的「行驶里程」米数
    budget0 = budget
    guard = 0

    while budget > 1e-9 and rider.route and guard < 200:
        guard += 1

        # 需要就先规划出「当前位置 → 下一站」这一段路
        if rider.leg is None and not _plan_leg(world, rider):
            break

        # 到这一段路终点还剩多少行驶里程
        remain = (rider.leg_total - rider.leg_done) * rider.leg_scale

        if remain > budget:                   # 还没到，沿折线走一段
            raw = budget / rider.leg_scale    # 行驶里程 → 几何长度
            rider.leg_done += raw
            rider.pos = geom.point_at(rider.leg, rider.leg_done)
            rider.motion = Motion.MOVING
            budget = 0
            break

        # 走到了这一段的终点：站点的坐标就是折线的终点
        budget -= remain
        rider.leg_done = rider.leg_total
        rider.pos = rider.leg[-1]
        # 这一腿实际走了多少米，必须在 invalidate_route() **之前**取出来 ——
        # 那个调用会把 leg_done/leg_total 清零，之后就再也读不到了
        # （第一版就是这么写的，结果骑行观测一条都没落盘）。
        travelled_m = rider.leg_total * rider.leg_scale
        rider.invalidate_route()
        _arrive(world, rider, rider.route[0], travelled_m)
        if rider.motion is Motion.WAITING:
            rider.distance_m += budget0 - budget
            return

    rider.distance_m += budget0 - budget
    if not rider.route:
        rider.motion = Motion.IDLE
    elif rider.motion is not Motion.WAITING:
        rider.motion = Motion.MOVING


def _plan_leg(world, rider: Rider) -> bool:
    """规划「当前位置 → 下一站」这一段。

    leg_scale 是行驶里程与几何长度的比值：抽象模式的距离是 1.4 × 直线，
    而折线只有两个点、几何长度就是直线距离，所以比例是 1.4；
    路网模式下最短路权就是各段长度之和，比例正好是 1。
    这样两种模式共用一套沿折线推进的代码，不用分支。

    返回 False 表示没得走（路线空了）。
    """
    stop = rider.route[0]
    line = world.metric.path(rider.pos, stop.pt)
    if line is None or len(line) < 2:
        # 退化：已经在站上或路径为空，直接当作到达
        rider.invalidate_route()
        _arrive(world, rider, stop)
        return rider.motion is not Motion.WAITING and bool(rider.route)

    raw = geom.raw_length(line)
    travel = world.metric.metres(rider.pos, stop.pt)
    rider.leg = line
    rider.leg_done = 0.0
    rider.leg_total = raw
    rider.leg_scale = 1.0 if raw < 1e-6 else max(1e-6, travel / raw)
    return True


def _arrive(world, rider: Rider, stop, travelled_m: float = 0.0) -> None:
    """骑手到达一站。

    `travelled_m` 是刚走完那一腿的实际里程（米）。调用方必须在
    `invalidate_route()` 之前把它取出来传进来 —— 那个方法会清零腿状态。
    """
    order = world.orders.get(stop.order_id)
    if order is None:
        _remove_stop(rider, stop.order_id, stop.type)
        return

    if stop.is_pickup:
        if order.arrived_store_at == 0:
            order.arrived_store_at = world.now()            # ③ 到店
            order.event(world.now(), "到店", "骑手到达 " + _merchant_name(world, order))
            # 记一段骑行观测（派单→到店）。这是行程时间校准的输入之一：
            # 预测用的是"距离 ÷ 固定速度"，而真实速度只能从这些观测里学。
            telemetry.record_leg(world, travelled_m,
                                 order.arrived_store_at - order.dispatched_at, "to_store")
        if world.now() < order.ready_at:
            # 出餐还没好：手上还有别的餐就先送去，别干等
            if world.cfg.avoid_waiting:
                idx = _first_deliverable_index(world, rider)
                if idx > 0:
                    rider.route.insert(0, rider.route.pop(idx))
                    rider.invalidate_route()   # 队首换了，正在走的那段路作废
                    return                     # 回到 _move 继续走
            rider.motion = Motion.WAITING
            rider.wait_until = order.ready_at
            rider.wait_order_id = order.id
            return
        _complete_pickup(world, rider, order)
    else:
        order.delivered_at = world.now()                    # ⑤ 送达
        order.status = OrderStatus.DELIVERED
        _remove_stop(rider, order.id, stop.type)
        rider.delivered_count += 1
        order.event(world.now(), "送达",
                    f"餐品交给 {order.customer_name}（{order.address}）")
        world.add_log("送达", (
            f"{order.id} 由 {rider.id} {rider.name} 送达，"
            f"顾客总共等了 {(order.delivered_at - order.created_at) / 60.0:.0f} 分钟"))
        # 落盘结果 + 一段骑行观测（取餐→送达）。
        # 决策记录在派单时写、结果在这里写，读取时按 orderId 合并成完整样本。
        telemetry.record_leg(world, travelled_m,
                             order.delivered_at - order.picked_at, "to_customer")
        telemetry.record_outcome(world, order)


def _complete_pickup(world, rider: Rider, order: Order) -> None:
    order.picked_at = world.now()                           # ④ 取餐
    order.status = OrderStatus.PICKED_UP
    _remove_stop(rider, order.id, StopType.PICKUP)
    order.event(world.now(), "取餐", "骑手拿到餐品")


def _first_deliverable_index(world, rider: Rider) -> int:
    """找一个「餐已经在车上、可以去送」的送达站点下标，没有则返回 -1。"""
    for i in range(1, len(rider.route)):
        s = rider.route[i]
        if not s.is_delivery:
            continue
        o = world.orders.get(s.order_id)
        if o is not None and o.picked_at > 0:
            return i
    return -1


def _remove_stop(rider: Rider, order_id: str, stop_type) -> None:
    for i, s in enumerate(rider.route):
        if s.order_id == order_id and s.type is stop_type:
            rider.route.pop(i)
            return


def _merchant_name(world, order: Order) -> str:
    m = world.merchants.get(order.merchant_id)
    return m.name if m else order.merchant_id


# ------------------------------------------------------------ 自动下单

def _auto_order(world) -> None:
    if not world.cfg.auto_order:
        return
    if world.now() < world.next_auto_order_at:
        return

    # 停单期间不自动生成订单 —— 这正是「控制模拟时的订单数量」的执行点。
    # 注意要先把下一次的时间往后推，否则停单解除后会瞬间补一大堆积压订单。
    intake = world.intake_status()
    if not intake["open"]:
        world.next_auto_order_at = world.now() + max(5, world.cfg.auto_order_every_sec)
        if not world.intake_logged:
            world.add_log("停单", intake["reason"] + "，暂停自动生成订单")
            world.intake_logged = True
        return
    world.intake_logged = False

    # 累计订单封顶：长时间运行不至于把内存涨爆
    if len(world.orders) >= world.cfg.max_total_orders:
        world.next_auto_order_at = world.now() + 3600
        world.add_log("停单", f"累计订单已达上限 {world.cfg.max_total_orders}，停止自动生成")
        return

    order = world.auto_place_order()
    world.add_log("下单", f"顾客 {order.customer_name} 在 "
                          f"{_merchant_name(world, order)} 下单 {order.id}")

    # 抖动必须是整数：randrange() 收到浮点会抛 TypeError，而这个异常发生在
    # **模拟线程**里 —— 后果不是"这一拍跳过"，而是整条仿真静默停住（时钟不再走，
    # 界面看起来像卡死）。配置从哪来都要能扛住，所以这里强制取整。
    jitter = int(world.cfg.auto_order_jitter_sec or 0)
    delta = int(world.cfg.auto_order_every_sec or 0)
    if jitter > 0:
        delta += world.rng.randrange(jitter * 2 + 1) - jitter
    world.next_auto_order_at = world.now() + max(5, delta)


# ------------------------------------------------------------ 派单节拍

def _maybe_dispatch(world) -> None:
    if world.now() - world.last_dispatch_at >= world.cfg.dispatch_interval_sec:
        dispatch_round(world)
