"""派单器：三档优先级 + 指派单 + 调单。对应 Java 版 Dispatcher。

每一轮把订单池里的订单按「① 顺路骑手 → ② 无订单骑手 → ③ 其他骑手」的顺序挑人，
前面的找不到才找后面的。同一档内按「额外绕路 + 负载惩罚 × 手持单数」取最小者。

顺路的判定方式：把新订单试着塞进骑手已有的路线，看要多绕多少路，绕得少就算顺路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import obs, route_planner as rp, telemetry
from .model import (AssignMode, Merchant, Motion, Order, OrderStatus, Pt, Rider,
                    RiderStatus, Stop, StopType)


@dataclass
class Choice:
    """一轮派单的候选方案。"""

    rider: Rider
    ins: rp.Insertion
    tier: int            # 1 顺路 / 2 无单 / 3 其他
    score: float
    reason: str
    candidates: dict = field(default_factory=dict)   # 候选摘要（供落盘/事后复盘）


# ------------------------------------------------------------ 自动派单

def round_(world) -> int:
    """跑一轮派单，返回本轮成功派出的订单数。"""
    world.dispatch_rounds += 1
    world.last_dispatch_at = world.now()
    round_no = world.dispatch_rounds

    pending = world.pooled_orders()
    if not pending:
        world.add_log("派单", f"第 {round_no} 轮派单：订单池为空，无单可派")
        return 0

    # 难派的先派。
    #
    # 贪心地「谁先下单谁先派」有个毛病：好派的单会先把空车骑手占掉，
    # 等到难派的单轮到的时候，可选骑手反而更少了。
    # 反过来先安排难派的（可选骑手最少的），再让好派的去挑剩下的，
    # 整体更容易都落进已有的路线里 —— jsprit / VROOM 用的 regret-k 就是这个思路。
    # 这里用 regret-2：候选择优与次优的差距越大，说明这单越「没得挑」，越该先派。
    pending = _order_by_regret(world, pending)

    tier_count = {1: 0, 2: 0, 3: 0}
    stuck = 0
    postponed = 0

    for order in pending:
        # 这轮进行中可能已经被「指派单」拿走了，跳过
        if order.status is not OrderStatus.POOLED or not world.in_pool(order.id):
            continue

        pick = _choose(world, order)
        if pick is None:
            stuck += 1
            order.event(world.now(), "派单失败", "本轮没有可用骑手，留在订单池")
            world.add_log("留池", f"第 {round_no} 轮：{order.id} 无人可派，留在订单池等下一轮")
            continue

        # 推迟：最好的归宿也只是「兜底骑手」时，先不派，等下一轮看看有没有顺路的
        should = _should_postpone(world, order, pick)
        explored = False

        # 随机探索（ε-greedy）：以很小的概率**改用另一个动作**。
        #
        # 为什么必须探索：策略学习要用"同一处境下两种做法的结果"来比较，
        # 而正常运行时策略是固定的 —— 被推迟的单和没被推迟的单不是同一批单
        # （本来就难派的才被推迟），直接比会把选择偏差当成"推迟有害"。
        # 随机化一小部分决策，这部分样本的对照才是无偏的。
        #
        # 探索时**等概率取一个动作**，而不是简单取反：
        # 取反会在"本该立刻派"占多数的场景（高负载就是这样）里几乎只产出
        # "推迟"臂的样本，另一臂一条都攒不到 —— 实测高负载下 24:0，
        # 这种数据学不出任何东西。等概率取样保证两臂都在涨。
        eps = float(getattr(world.cfg, "explore_epsilon", 0.0) or 0.0)
        if pick.tier == 3 and eps > 0 and world.rng.random() < eps:
            explored = True
            should = world.rng.random() < 0.5

        if should:
            order.postpone_count += 1
            postponed += 1
            # 记下"不等的话会是什么结果"。这是推迟策略学习的负样本 ——
            # 只看派出去的单会产生幸存者偏差（看不到被我们主动放弃的那些选择）。
            # explored 要一起传：不然"探索出来的推迟"会被当成普通推迟，
            # 学习器里一条可用的对照样本都拿不到（实测只攒到 3 条就是这么来的）。
            telemetry.record_postpone(world, order, pick.tier, pick.ins.extra_m,
                                      explored=explored)
            obs.counter("dispatch_postponed")
            if explored:
                obs.counter("dispatch_explored")
            world.add_log("推迟", (
                f"{order.id} 本轮最好的选择也只是兜底骑手（{pick.reason}），"
                f"先不派，等下一轮看看有没有顺路的"))
            continue

        _apply(world, order, pick, AssignMode.AUTO, explored=explored)
        tier_count[pick.tier] = tier_count.get(pick.tier, 0) + 1
        if explored:
            # 探索了多少次要能看见。策略学习的进度就看这个数 ——
            # 它一直是 0 的话，"样本不足"就永远解除不了，得先去查为什么没探索。
            obs.counter("dispatch_explored")

    world.add_log("派单", (
        f"第 {round_no} 轮派单：池中 {len(pending)} 单 → "
        f"顺路 {tier_count[1]}、无单 {tier_count[2]}、其他 {tier_count[3]}，"
        f"推迟 {postponed}，留池 {stuck}"))
    return tier_count[1] + tier_count[2] + tier_count[3]


def _should_postpone(world, order: Order, pick: Choice) -> bool:
    """这单该不该先不派、留到下一轮？

    只有「最好的归宿也只是兜底」才值得等 —— 顺路和无单档都已经是好结果，没必要等。

    三条保险：
      · 池子深（超过 有余量的骑手数 × factor）说明运力已经吃紧，越等越糟，必须立刻派
      · 已经等超过 postpone_max_wait_min 分钟 → 不能再等，会超时
      · 已经被推迟过 postpone_max_rounds 轮 → 不能再推，否则可能永远派不出去
    """
    cfg = world.cfg
    if not cfg.postpone_poor_assignments:
        return False
    if pick.tier != 3:
        return False
    if order.postpone_count >= cfg.postpone_max_rounds:
        return False

    # 注意这里要自己算「已经等了多久」，不能用 order.wait_dispatch_sec ——
    # 那个属性的定义是「顾客等到被派单花了多久」，只有**派出之后**才有值，
    # 没派出去时返回 None。用它当等待上限会导致上限永远不生效、订单被无限推迟。
    waited = world.now() - order.created_at
    if waited >= cfg.postpone_max_wait_min * 60:
        return False

    # 等下去的前提是「真的还有闲置运力」—— 一个有余量的骑手都没有的话，
    # 再等也不会冒出顺路的机会。
    #
    # 闸门的分母必须是**还有余量的骑手**，不是骑手总数：已经打满上限的骑手帮不上忙，
    # 拿总人数当分母会在高峰期把闸门放得太宽。实测（3 单/分钟、90 分钟）：分母用总人数时
    # 有 33 次推迟在高峰期找上了门，顺路占比反而从 29% 掉到 25%、兜底从 52% 涨到 55%。
    # 一笔在排队的单子，只有在「能立刻接它的骑手比它还多」时，等待才是免费的。
    free = sum(1 for r in world.riders.values() if r.can_take())
    if free == 0:
        return False
    return len(world.pool) <= max(1, int(free * cfg.postpone_max_pool_factor))


def _order_by_regret(world, pending: List[Order]) -> List[Order]:
    """按 regret-2 降序排列：候选择优与次优差距越大的单，越该先派。

    代价是对每笔单多算一轮全部骑手 —— 池子通常只有个位数到几十单，完全跑得动。
    """
    scored = []
    for o in pending:
        options = []
        for r in world.riders.values():
            if not r.can_take():
                continue
            if world.metric.metres(r.pos, o.merchant_pt) > world.cfg.candidate_radius_m:
                continue
            ins = rp.best_insertion(world.metric, r, o, r.max_orders, 0)
            if ins is not None:
                options.append(ins.extra_m)
        if not options:
            scored.append((float('inf'), o))          # 无处可派的最优先处理
        elif len(options) == 1:
            scored.append((float('inf'), o))          # 只有一个选择，没得挑
        else:
            options.sort()
            scored.append((options[1] - options[0], o))
    # regret 大的先派；相同则早下单的先派（保持公平）
    scored.sort(key=lambda t: (-t[0] if t[0] != float('inf') else float('-inf'),
                               t[1].created_at))
    return [o for _s, o in scored]


def _choose(world, order: Order) -> Optional[Choice]:
    """为一笔订单挑骑手：先看顺路，再看无单，最后兜底。"""
    cfg = world.cfg
    metric = world.metric
    direct_m = metric.metres(order.merchant_pt, order.dest_pt)
    by_tier: dict = {}
    # 候选摘要：每档有几个候选、各档最优代价是多少。
    # 以前这些在函数返回时就没了 —— 而它们正是"当时这个决策合不合理"
    # 唯一能事后验证的东西（比如"当时确实没有顺路可选" vs "有但没选"）。
    cand = {"n1": 0, "n2": 0, "n3": 0, "best1": None, "best2": None, "best3": None}

    for rider in world.riders.values():
        if not rider.can_take():                                     # 忙碌或已到接单上限
            continue
        if metric.metres(rider.pos, order.merchant_pt) > cfg.candidate_radius_m:
            continue

        # 正在商家等出餐的骑手，当前位置那一段不能动
        min_gap = 1 if rider.motion is Motion.WAITING else 0
        ins = rp.best_insertion(metric, rider, order, rider.max_orders, min_gap)
        if ins is None:
            continue

        has_orders = rider.has_orders()
        on_route = has_orders and rp.is_on_route(ins.extra_m, direct_m, cfg)
        tier = 2 if not has_orders else (1 if on_route else 3)

        score = ins.extra_m + cfg.load_penalty_per_order_m * rider.active_count()
        if tier == 1:
            reason = f"顺路骑手，多绕 {ins.extra_m:.0f}m"
        elif tier == 2:
            reason = f"无订单骑手，空驶 {ins.extra_m:.0f}m"
        else:
            reason = f"兜底骑手，多绕 {ins.extra_m:.0f}m"

        cand[f"n{tier}"] += 1
        best_key = f"best{tier}"
        if cand[best_key] is None or ins.extra_m < cand[best_key]:
            cand[best_key] = round(float(ins.extra_m), 1)
        cand["directM"] = round(float(direct_m), 1)

        c = Choice(rider, ins, tier, score, reason)
        if tier not in by_tier or c.score < by_tier[tier].score:
            by_tier[tier] = c

    pick = by_tier.get(1) or by_tier.get(2) or by_tier.get(3)
    if pick is not None:
        pick.candidates = cand          # 挂上去，让 _apply 能一起落盘
    return pick


def _apply(world, order: Order, choice: Choice, mode: AssignMode,
           explored: bool = False) -> None:
    """把订单落到骑手路线上。"""
    rider = choice.rider
    lock_prefix = 1 if rider.motion is Motion.WAITING else 0

    rider.route[:] = choice.ins.route
    rp.improve(world.metric, rider.pos, rider.route, rider.max_orders, lock_prefix)
    rider.invalidate_route()   # 路线变了，正在走的那一段路作废，下一拍重新规划

    order.rider_id = rider.id
    order.status = OrderStatus.ASSIGNED
    order.dispatched_at = world.now()
    order.mode = mode
    order.tier = choice.tier
    order.detour_m = choice.ins.extra_m
    world.remove_from_pool(order.id)

    if rider.motion is not Motion.WAITING:
        rider.motion = Motion.MOVING

    label = {AssignMode.AUTO: "派单",
             AssignMode.MANUAL_ASSIGN: "指派单",
             AssignMode.REASSIGN: "调单"}[mode]
    order.event(world.now(), label, f"派给 {rider.id} {rider.name}（{choice.reason}）")
    world.add_log(label if mode is not AssignMode.AUTO else "派单",
                  f"{order.id} → {rider.id} {rider.name}（{choice.reason}）")

    # 落盘这条决策（含候选摘要）。人工指派/调单也记 —— 它们是策略学习里
    # 重要的"人工干预样本"，不记的话学习器会以为系统一直是全自动的。
    telemetry.record_decision(world, order, choice.tier, choice.ins.extra_m,
                              rider.id, label, postponed=order.postpone_count > 0,
                              candidates=choice.candidates, explored=explored)


# ------------------------------------------------------------ 人工干预

def manual_assign(world, order_id: str, rider_id: str) -> Optional[str]:
    """指派单：订单还在池子里时，管理人员直接指定一个骑手。

    不走平台的派单算法，但路线插在哪一段仍然由路径规划算。
    返回 None 表示成功，否则返回错误说明。
    """
    order = world.orders.get(order_id)
    if order is None:
        return f"订单不存在：{order_id}"
    if order.status is not OrderStatus.POOLED or not world.in_pool(order_id):
        return (f"订单 {order_id} 已经派出去了（状态 {_status_text(order)}），"
                f"指派单只能用于还在订单池里的订单")

    rider = world.riders.get(rider_id)
    if rider is None:
        return f"骑手不存在：{rider_id}"
    if rider.status is not RiderStatus.ONLINE:
        return f"骑手 {rider.id} 当前是「忙碌」，不接单"
    if rider.active_count() >= rider.max_orders:
        return f"骑手 {rider.id} 已达接单上限（{rider.max_orders} 单）"

    metric = world.metric
    min_gap = 1 if rider.motion is Motion.WAITING else 0
    ins = rp.best_insertion(metric, rider, order, rider.max_orders, min_gap)
    if ins is None:
        return f"骑手 {rider.id} 装不下这一单"

    direct_m = metric.metres(order.merchant_pt, order.dest_pt)
    on_route = rider.has_orders() and rp.is_on_route(ins.extra_m, direct_m, world.cfg)
    tier = 2 if not rider.has_orders() else (1 if on_route else 3)

    _apply(world, order, Choice(rider, ins, tier, 0.0, "指派单（人工指定，算法不参与）"),
           AssignMode.MANUAL_ASSIGN)
    return None


def reassign(world, order_id: str, to_rider_id: str) -> Optional[str]:
    """调单：把已经在某个骑手手上的订单转派给另一个骑手。

    已经取餐的订单不允许调单 —— 餐已经在原骑手手上，换人对不上。
    """
    order = world.orders.get(order_id)
    if order is None:
        return f"订单不存在：{order_id}"
    if order.status is OrderStatus.POOLED:
        return f"订单 {order_id} 还在订单池里，请用「指派单」"
    if order.status is OrderStatus.DELIVERED:
        return f"订单 {order_id} 已送达，无法调单"
    if order.picked_at > 0:
        return f"订单 {order_id} 已经取餐了，餐品在原骑手手上，不能再调单"

    src = world.riders.get(order.rider_id) if order.rider_id else None
    dst = world.riders.get(to_rider_id)
    if dst is None:
        return f"骑手不存在：{to_rider_id}"
    if src is not None and src.id == dst.id:
        return "目标骑手与原骑手是同一个人"

    if dst.status is not RiderStatus.ONLINE:
        return f"骑手 {dst.id} 当前是「忙碌」，不接单"
    if dst.active_count() >= dst.max_orders:
        return f"骑手 {dst.id} 已达接单上限（{dst.max_orders} 单）"

    metric = world.metric

    # 从原骑手路线上摘掉这一单
    before = 0 if src is None else len(src.route)
    if src is not None:
        src.route[:] = [s for s in src.route if s.order_id != order_id]
        if src.wait_order_id == order_id:
            src.wait_order_id = None
            src.wait_until = 0
            src.motion = Motion.IDLE if not src.route else Motion.MOVING
        if not src.route and src.motion is not Motion.WAITING:
            src.motion = Motion.IDLE
        src.invalidate_route()

    min_gap = 1 if dst.motion is Motion.WAITING else 0
    ins = rp.best_insertion(metric, dst, order, dst.max_orders, min_gap)
    if ins is None:
        # 装不下就回滚，别把单弄丢了
        if src is not None and len(src.route) != before:
            src.route.clear()
            back = rp.best_insertion(metric, src, order, src.max_orders, 0)
            if back is not None:
                src.route[:] = back.route
            else:
                src.route[:] = [
                    Stop(order.id, StopType.PICKUP, order.merchant_pt),
                    Stop(order.id, StopType.DELIVERY, order.dest_pt),
                ]
            src.motion = Motion.MOVING
            src.invalidate_route()
        return f"骑手 {dst.id} 装不下这一单，调单已取消"

    direct_m = metric.metres(order.merchant_pt, order.dest_pt)
    on_route = dst.has_orders() and rp.is_on_route(ins.extra_m, direct_m, world.cfg)
    tier = 2 if not dst.has_orders() else (1 if on_route else 3)

    old = order.rider_id if src is None else f"{src.id} {src.name}"
    order.reassign_count += 1
    _apply(world, order, Choice(dst, ins, tier, 0.0, f"调单：从 {old} 转来"),
           AssignMode.REASSIGN)
    order.event(world.now(), "调单", f"由 {old} 转派给 {dst.id} {dst.name}")
    world.add_log("调单", f"{order.id} 从 {old} 转派给 {dst.id} {dst.name}")
    return None


# ------------------------------------------------------------ 骑手增删（运营）

# 备用名字池，添加骑手时按序号取
_EXTRA_RIDER_NAMES = [
    ("赵敏", "13900000011"), ("钱峰", "13900000012"), ("孙鹏", "13900000013"),
    ("李娜", "13900000014"), ("周洋", "13900000015"), ("吴强", "13900000016"),
    ("郑爽", "13900000017"), ("王芳", "13900000018"), ("冯磊", "13900000019"),
    ("陈静", "13900000020"), ("褚勇", "13900000021"), ("卫东", "13900000022"),
]


def next_rider_id(world) -> str:
    """下一个可用的骑手号：找现有 R 编号里最大的那个 +1，避免重用。"""
    used = set()
    for rid in world.riders:
        if rid.startswith("R") and rid[1:].isdigit():
            used.add(int(rid[1:]))
    n = 1
    while n in used:
        n += 1
    return f"R{n}"


def add_rider(world, name: Optional[str] = None, phone: Optional[str] = None,
              x: Optional[float] = None, y: Optional[float] = None,
              max_orders: Optional[int] = None):
    """加一个骑手。返回 (rider, None) 或 (None, 错误说明)。

    位置默认落在路网/城区里一个分散的位置 —— 和开局布点用同一套逻辑，
    免得新骑手全挤在一个角落。
    """
    from .road_metric import RoadMetric

    rid = next_rider_id(world)
    seq = len(world.riders)
    fallback_name, fallback_phone = _EXTRA_RIDER_NAMES[seq % len(_EXTRA_RIDER_NAMES)]

    if x is not None and y is not None:
        pos = Pt(x, y)
    elif isinstance(world.metric, RoadMetric):
        pos = world.metric.spread_nodes(1 + seq, world.rng)[-1]
    else:
        # 抽象城市：在城里随机挑一个离现有骑手尽可能远的位置
        best, best_d = None, -1.0
        for _ in range(30):
            p = Pt(world.rng.uniform(200, world.width_m - 200),
                   world.rng.uniform(200, world.height_m - 200))
            d = min((p.straight(r.pos) for r in world.riders.values()), default=1e9)
            if d > best_d:
                best_d, best = d, p
        pos = best or Pt(world.width_m / 2, world.height_m / 2)

    rider = Rider(rid, name or fallback_name, phone or fallback_phone, pos,
                  max_orders or world.cfg.default_max_orders)
    # 速度和现有骑手同分布，免得新来的特别快或特别慢
    rider.speed_mpm = 280 + world.rng.randrange(5) * 20
    world.riders[rid] = rider

    world.add_log("骑手", f"新增骑手 {rid} {rider.name}，接单上限 {rider.max_orders} 单")
    return rider, None


def remove_rider(world, rider_id: str):
    """移除一个骑手。返回 (结果说明, None) 或 (None, 错误说明)。

    规则（和现实一致）：
      * 手上**还没取餐**的订单 → 退回订单池，等下一轮重新派给别人；
      * 手上**已经取餐**的订单 → 拒绝移除。餐已经在车上，
        把人从系统里删掉没有意义，得先让他送完或调单给别人。
    """
    rider = world.riders.get(rider_id)
    if rider is None:
        return None, f"骑手不存在：{rider_id}"

    active = [world.orders[oid] for oid in rider.active_order_ids()
              if oid in world.orders]
    picked_up = [o for o in active if o.picked_at > 0]
    if picked_up:
        ids = "、".join(o.id for o in picked_up[:3])
        more = "…" if len(picked_up) > 3 else ""
        return None, (f"骑手 {rider.id} {rider.name} 手上有 {len(picked_up)} 单已取餐"
                      f"（{ids}{more}），餐已经在车上，不能直接移除。"
                      f"请先把这些单送完，或用「调单」转给别的骑手。")

    returned = 0
    for o in active:
        # 退回订单池：清掉派单痕迹，让它重新参与派单
        o.status = OrderStatus.POOLED
        o.rider_id = None
        o.mode = None
        o.tier = None
        o.detour_m = 0.0
        o.dispatched_at = 0
        o.arrived_store_at = 0
        o.event(world.now(), "退回订单池",
                f"骑手 {rider.id} 被移除，订单退回订单池等待重新派单")
        if not world.in_pool(o.id):
            world.pool.append(o.id)
        returned += 1

    del world.riders[rider_id]
    world.add_log("骑手", f"移除骑手 {rider.id} {rider.name}"
                          + (f"，{returned} 单退回订单池" if returned else ""))
    return {"riderId": rider_id, "name": rider.name, "returnedOrders": returned}, None


# ------------------------------------------------------------ 骑手设置

def set_rider_status(world, rider_id: str, status: str) -> Optional[str]:
    """骑手自己控制：上线 / 忙碌。"""
    rider = world.riders.get(rider_id)
    if rider is None:
        return f"骑手不存在：{rider_id}"
    try:
        rider.status = RiderStatus(status.upper())
    except ValueError:
        return "状态只能是 ONLINE（上线）或 BUSY（忙碌）"
    world.add_log("骑手", f"{rider.id} {rider.name} 切换为「"
                         f"{'上线' if rider.status is RiderStatus.ONLINE else '忙碌'}」")
    return None


def set_rider_cap(world, rider_id: str, cap: int) -> Optional[str]:
    """骑手自己控制：接单上限。"""
    rider = world.riders.get(rider_id)
    if rider is None:
        return f"骑手不存在：{rider_id}"
    if cap < 1 or cap > 20:
        return "接单上限定为 1~20 单"
    if cap < rider.active_count():
        return f"骑手 {rider.id} 手上已有 {rider.active_count()} 单，上限不能低于这个数"
    rider.max_orders = cap
    world.add_log("骑手", f"{rider.id} {rider.name} 接单上限设为 {cap} 单")
    return None


def _status_text(order: Order) -> str:
    return {
        OrderStatus.POOLED: "在订单池",
        OrderStatus.ASSIGNED: "已派单",
        OrderStatus.PICKED_UP: "已取餐",
        OrderStatus.DELIVERED: "已送达",
    }[order.status]
