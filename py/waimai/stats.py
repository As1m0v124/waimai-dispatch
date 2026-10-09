"""统计口径：用五个时间点算出每一步的耗时，衡量派单和配送的质量。对应 Java 版 Stats。"""

from __future__ import annotations

from typing import Optional

from .model import OrderStatus, RiderStatus


def _avg_minutes(orders, getter) -> float:
    """平均耗时（分钟）。"""
    total = 0
    n = 0
    for o in orders:
        v = getter(o)
        if v is not None:
            total += v
            n += 1
    return 0.0 if n == 0 else total / 60.0 / n


def _round1(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 1)


def of(world) -> dict:
    done = []
    pooled = assigned = picked_up = 0
    for o in world.orders.values():
        if o.status is OrderStatus.POOLED:
            pooled += 1
        elif o.status is OrderStatus.ASSIGNED:
            assigned += 1
        elif o.status is OrderStatus.PICKED_UP:
            picked_up += 1
        else:
            done.append(o)

    stats = {
        "totalOrders": len(world.orders),
        "delivered": len(done),
        "pooled": pooled,
        "assigned": assigned,
        "pickedUp": picked_up,
        "inFlight": assigned + picked_up,
    }

    # ---- 已完成订单的平均耗时（分钟）----
    stats["avgWaitDispatchMin"] = _round1(_avg_minutes(done, lambda o: o.wait_dispatch_sec))
    stats["avgToStoreMin"] = _round1(_avg_minutes(done, lambda o: o.to_store_sec))
    stats["avgPrepWaitMin"] = _round1(_avg_minutes(done, lambda o: o.prep_wait_sec))
    stats["avgOnRoadMin"] = _round1(_avg_minutes(done, lambda o: o.on_road_sec))
    stats["avgTotalMin"] = _round1(_avg_minutes(done, lambda o: o.total_sec))

    # ---- 准时率 ----
    sla_sec = world.cfg.sla_minutes * 60
    on_time = late = 0
    worst = 0
    for o in done:
        t = o.total_sec
        if t is None:
            continue
        if t <= sla_sec:
            on_time += 1
        else:
            late += 1
        worst = max(worst, t)
    stats["onTime"] = on_time
    stats["late"] = late
    stats["slaMinutes"] = world.cfg.sla_minutes
    stats["onTimeRate"] = None if not done else _round1(on_time * 100.0 / len(done))
    stats["worstTotalMin"] = None if not done else _round1(worst / 60.0)

    # ---- 派单档位分布：最直观地体现「顺路优先」的效果 ----
    t1 = t2 = t3 = manual = 0
    for o in world.orders.values():
        if o.dispatched_at == 0:
            continue
        if o.mode is not None and o.mode.name == "MANUAL_ASSIGN":
            manual += 1
            continue
        if o.tier is None:
            continue
        if o.tier == 1:
            t1 += 1
        elif o.tier == 2:
            t2 += 1
        else:
            t3 += 1
    stats["tier1OnRoute"] = t1
    stats["tier2Idle"] = t2
    stats["tier3Fallback"] = t3
    stats["tierManual"] = manual
    auto = t1 + t2 + t3
    stats["onRouteShare"] = None if auto == 0 else _round1(t1 * 100.0 / auto)

    # ---- 推迟派单：池子里有多少单是「故意在等」，不是积压 ----
    # 开了推迟之后，池子非空是设计的一部分，所以要把这部分单独数出来，
    # 否则「池子里有几单」这个数字会被读成「处理不过来了」。
    postponed_ever = 0
    pooled_postponed = 0
    postponed_rounds = 0
    for o in world.orders.values():
        if o.postpone_count:
            postponed_ever += 1
            postponed_rounds += o.postpone_count
            if o.status is OrderStatus.POOLED:
                pooled_postponed += 1
    stats["postponedOrders"] = postponed_ever
    stats["postponedRounds"] = postponed_rounds
    stats["pooledPostponed"] = pooled_postponed

    # ---- 骑手运力 ----
    dist = 0.0
    cap = load = online = 0
    for r in world.riders.values():
        dist += r.distance_m
        cap += r.max_orders
        load += r.active_count()
        if r.status is RiderStatus.ONLINE:
            online += 1
    stats["riderTotalKm"] = _round1(dist / 1000.0)
    stats["riderCapacity"] = cap
    stats["riderLoad"] = load
    stats["riderOnline"] = online
    stats["riderCount"] = len(world.riders)
    stats["riderUtilization"] = None if cap == 0 else _round1(load * 100.0 / cap)
    stats["avgKmPerOrder"] = None if not done else _round1(dist / 1000.0 / len(done))

    # ---- 派单节拍 ----
    stats["dispatchRounds"] = world.dispatch_rounds
    stats["dispatchIntervalSec"] = world.cfg.dispatch_interval_sec
    stats["nextDispatchInSec"] = world.next_dispatch_in()
    stats["simClock"] = world.clock()
    stats["simSeconds"] = world.now()
    stats["poolSize"] = len(world.pool)
    return stats
