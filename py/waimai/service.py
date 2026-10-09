"""服务层：所有业务操作的**唯一**实现。

为什么要有这一层：这些逻辑原来都长在 `api.py` 的 HTTP 处理函数里
（参数校验、区间夹取、进单判定、序列化）。一旦要再加 CLI 和 MCP 两个入口，
它们只有两个选择 —— 复制一份（必然越走越不一致），或者直接调这里的函数。

所以：**HTTP / CLI / MCP 三个入口都只是适配器**，业务规则只在这一个文件里。
每个函数收一个 `world`（加上需要的东西）和一个普通 dict，返回一个普通 dict，
形状就是 HTTP 接口原来的响应形状。这样三边天然一致，改规则也只改一处。

顺带解决了原来的一处重复：`/api/order/auto` 和模拟器的自动下单各自写了一遍
"能不能进单"的循环判断（api.py 和 simulator.py），现在都走
`can_accept_order()`。
"""

from __future__ import annotations

import math
import time
from typing import List, Optional, Tuple

from . import dispatcher, obs, osm_loader, security, stats, zone_analytics
from .model import Motion, OrderStatus, Pt
from .world import CITY, NetworkMode, OrderLimitReached

# 一次状态快照里最多带多少笔订单（按时间倒序取最近的）
MAX_ORDER_JSON = 400
# 快照里最多带多少条派单日志
MAX_LOG_JSON = 120


# ------------------------------------------------------------ 小工具

def trim(s) -> Optional[str]:
    if not isinstance(s, str):
        return None
    t = s.strip()
    return t or None


def num(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(f) or math.isinf(f)) else f


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def opt_int(v, default: int, lo: int, hi: int) -> int:
    """请求里的可选整数：null / 缺失 / 非数字 / 布尔 / 畸形浮点都退回默认值，再夹区间。

    `int(None)` 抛 TypeError、`int("NaN")` 抛 ValueError，两者原来都会变成 500，
    而调用方只是想给个默认值。布尔要单独排除 —— Python 里 `True` 是 `int` 子类，
    `int(True) == 1` 会把"开关"当成数字收下。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return clamp_int(n, lo, hi)


def clock_str(seconds: float) -> str:
    s = int(seconds)
    return f"{(s // 3600) % 24:02d}:{(s // 60) % 60:02d}:{s % 60:02d}"


def nz(t: float) -> Optional[float]:
    """0 表示「这个时间点还没发生」，对外一律给 null，别让调用方去猜 0 的含义。"""
    return t if t else None


def can_accept_order(world) -> Tuple[bool, str]:
    """现在还能不能进新单？(可以?, 原因)

    **唯一**的进单判定。停单闸门和累计上限都在这里，
    自动下单、批量下单、手动下单三条路径共用，不再各写一份。
    """
    intake = world.intake_status()
    if not intake["open"]:
        return False, intake["reason"]
    if len(world.orders) >= world.cfg.max_total_orders:
        return False, f"累计订单已达上限 {world.cfg.max_total_orders} 单"
    return True, ""


# ------------------------------------------------------------ 参数名映射
#
# 对外（HTTP / CLI / MCP）用的是 camelCase 的 API 名，内部的 Config 字段是
# snake_case。这张表是**唯一**的对应关系 —— 之前 apply_control 里一份、
# simulate 里靠 hasattr 猜一份，于是 `--set postponePoorAssignments=false`
# 静默失效（键名对不上，hasattr 返回 False，什么都不改），
# 跑出来的对照组和实验组一模一样，很容易据此得出"推迟没用"的错误结论。
INT_FIELDS = (
    ("dispatchIntervalSec", "dispatch_interval_sec", 10, 3600),
    ("slaMinutes", "sla_minutes", 1, 600),
    ("candidateRadiusM", "candidate_radius_m", 100, 50000),
    ("autoOrderEverySec", "auto_order_every_sec", 3, 3600),
    ("warmupOrders", "warmup_orders", 0, 50),
    ("heatCells", "heat_cells", 3, 24),
    ("stopAcceptMinOrders", "stop_accept_min_orders", 0, 500),
    ("postponeMaxWaitMin", "postpone_max_wait_min", 0, 60),
    ("postponeMaxRounds", "postpone_max_rounds", 0, 20),
    ("maxTotalOrders", "max_total_orders", 10, 100000),
)
FLOAT_FIELDS = (
    ("onRouteMaxDetourM", "on_route_max_detour_m", 0, 20000),
    ("onRouteMaxDetourRatio", "on_route_max_detour_ratio", 0, 10),
    ("loadPenaltyPerOrderM", "load_penalty_per_order_m", 0, 10000),
    ("stopAcceptPoolRatio", "stop_accept_pool_ratio", 0.0, 1.0),
    ("postponeMaxPoolFactor", "postpone_max_pool_factor", 0.0, 3.0),
    # 探索比例上限给到 0.5：再高就不是"探索"而是把策略随机化一半了，
    # 那会明显伤害当前服务的水准，不该有人能一键做出来。
    ("exploreEpsilon", "explore_epsilon", 0.0, 0.5),
)
BOOL_FIELDS = (
    ("autoOrder", "auto_order"),
    ("avoidWaiting", "avoid_waiting"),
    ("acceptOrders", "accept_orders"),
    ("postponePoorAssignments", "postpone_poor_assignments"),
)
# API 名和字段名都能收：写 `slaMinutes` 或 `sla_minutes` 都认。
#
# **两套名字都要收**，不能只收 camelCase。只收一套的后果是踩过的：
# `--set explore_epsilon=0.4`（字段名）不在表里，于是被当成"不认识的键"——
# 而调用方如果不看 ignoredParams，就会以为参数生效了，
# 拿着一个其实没变的场景做实验，还得出"这个参数没用"的结论。
CFG_ALIASES = {}
for _api, _field, _lo, _hi in INT_FIELDS:
    CFG_ALIASES[_api] = _field
    CFG_ALIASES[_field] = _field
for _api, _field, _lo, _hi in FLOAT_FIELDS:
    CFG_ALIASES[_api] = _field
    CFG_ALIASES[_field] = _field
for _api, _field in BOOL_FIELDS:
    CFG_ALIASES[_api] = _field
    CFG_ALIASES[_field] = _field
CFG_ALIASES["zoneWindowMin"] = "zone_window_min"
CFG_ALIASES["zone_window_min"] = "zone_window_min"


def coerce_cfg(field: str, value):
    """按字段的类型把值转成对的样子（int / float / bool）。"""
    for api, f, lo, hi in INT_FIELDS:
        if f == field:
            return clamp_int(int(value), lo, hi)
    for api, f, lo, hi in FLOAT_FIELDS:
        if f == field:
            return clamp(num(value, lo), lo, hi)
    for api, f in BOOL_FIELDS:
        if f == field:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开")
            return bool(value)
    if field == "zone_window_min":
        v = int(value)
        return 0 if v <= 0 else clamp_int(v, 5, 1440)
    return value


def apply_cfg_overrides(world, overrides: dict) -> Tuple[dict, List[str]]:
    """按字段名或 API 名改配置。返回 (改了什么, 不认识的键)。

    **不认识的键要报出来**，不能静默忽略 —— `--set typoWhatever=1` 悄悄不生效
    比报错危险得多：你会拿着两个其实没区别的场景做对照，然后得出错误结论。
    """
    applied, unknown = {}, []
    for k, v in (overrides or {}).items():
        key = k if k in CFG_ALIASES else CFG_ALIASES.get(k)
        field = CFG_ALIASES.get(k)
        if field is None:
            unknown.append(k)
            continue
        try:
            val = coerce_cfg(field, v)
        except (TypeError, ValueError):
            unknown.append(k)
            continue
        setattr(world.cfg, field, val)
        applied[key or field] = val
    return applied, unknown


# ------------------------------------------------------------ 序列化

def config_json(world) -> dict:
    c = world.cfg
    return {
        "dispatchIntervalSec": c.dispatch_interval_sec,
        "onRouteMaxDetourM": c.on_route_max_detour_m,
        "onRouteMaxDetourRatio": c.on_route_max_detour_ratio,
        "loadPenaltyPerOrderM": c.load_penalty_per_order_m,
        "defaultMaxOrders": c.default_max_orders,
        "candidateRadiusM": c.candidate_radius_m,
        "slaMinutes": c.sla_minutes,
        "avoidWaiting": c.avoid_waiting,
        "autoOrder": c.auto_order,
        "autoOrderEverySec": c.auto_order_every_sec,
        "warmupOrders": c.warmup_orders,
        "zoneWindowMin": c.zone_window_min,
        "heatCells": c.heat_cells,
        "acceptOrders": c.accept_orders,
        "stopAcceptPoolRatio": c.stop_accept_pool_ratio,
        "stopAcceptMinOrders": c.stop_accept_min_orders,
        "maxTotalOrders": c.max_total_orders,
        "postponePoorAssignments": c.postpone_poor_assignments,
        "postponeMaxPoolFactor": c.postpone_max_pool_factor,
        "postponeMaxWaitMin": c.postpone_max_wait_min,
        "postponeMaxRounds": c.postpone_max_rounds,
    }


def order_json(world, o) -> dict:
    merchant = world.merchants.get(o.merchant_id)
    rider = world.riders.get(o.rider_id) if o.rider_id else None
    # ETA 是"学到的模型真正被用上"的地方：其中的排队时间来自
    # learn.py 从历史样本里学出的等派单模型，速度来自骑行观测。
    # 没有模型时返回 None —— 宁可显示"暂无预计"，也不编一个数给顾客。
    eta = None
    try:
        from . import learn
        sec = learn.eta_seconds(world, o)
        if sec is not None and o.status is not OrderStatus.DELIVERED:
            eta = round(sec / 60.0, 1)
    except Exception:                                # noqa: BLE001
        eta = None                                    # 预测失败不影响主流程
    return {
        "id": o.id,
        "customerName": o.customer_name,
        "phone": o.phone,
        "address": o.address,
        "note": o.note,
        "merchantId": o.merchant_id,
        "merchantName": merchant.name if merchant else o.merchant_id,
        "mx": o.merchant_pt.x, "my": o.merchant_pt.y,
        "dx": o.dest_pt.x, "dy": o.dest_pt.y,
        "readyAt": o.ready_at,
        "status": o.status.name,
        "riderId": o.rider_id,
        "riderName": rider.name if rider else None,
        "mode": o.mode.name if o.mode else None,
        "tier": o.tier,
        "detourM": o.detour_m,
        "reassignCount": o.reassign_count,
        "etaMinutes": eta,
        "atRisk": (eta is not None and eta > world.cfg.sla_minutes
                   and o.status is not OrderStatus.DELIVERED),
        "t": {
            "created": o.created_at,
            "dispatched": nz(o.dispatched_at),
            "arrivedStore": nz(o.arrived_store_at),
            "picked": nz(o.picked_at),
            "delivered": nz(o.delivered_at),
        },
        "d": {
            "waitDispatch": o.wait_dispatch_sec,
            "toStore": o.to_store_sec,
            "prepWait": o.prep_wait_sec,
            "onRoad": o.on_road_sec,
            "total": o.total_sec,
        },
        "events": [
            {"t": e.t, "clock": clock_str(e.t), "type": e.type, "detail": e.detail}
            for e in o.events
        ],
    }


def rider_json(world, r) -> dict:
    from . import geom                       # 折线抽稀
    route = []
    for s in r.route:
        o = world.orders.get(s.order_id)
        item = {
            "orderId": s.order_id,
            "type": s.type.name,
            "x": round(s.pt.x, 1), "y": round(s.pt.y, 1),
        }
        if o is not None:
            m = world.merchants.get(o.merchant_id)
            item.update({
                "customerName": o.customer_name, "phone": o.phone,
                "address": o.address, "note": o.note,
                "merchantName": m.name if m else o.merchant_id,
                "status": o.status.name,
                "picked": o.picked_at > 0,
            })
        route.append(item)

    tail = r.tail_path(world.metric, 4)
    return {
        "id": r.id, "name": r.name, "phone": r.phone,
        "x": round(r.pos.x, 1), "y": round(r.pos.y, 1),
        "status": r.status.name,
        "motion": r.motion.name,
        "maxOrders": r.max_orders,
        "speedMpm": r.speed_mpm,
        "activeOrders": r.active_count(),
        "delivered": r.delivered_count,
        "km": round(r.distance_m / 1000.0, 1),
        "waitUntil": r.wait_until if r.motion is Motion.WAITING else None,
        "activeOrderIds": sorted(r.active_order_ids()),
        "leg": _polyline(r.leg),
        "tail": _polyline(tail),
        "route": route,
    }


def _polyline(pts) -> list:
    """折线压成一维整数数组（分米），省一半 JSON 体积。"""
    if not pts:
        return []
    out = []
    for p in pts:
        out.append(int(round(p.x * 10)))
        out.append(int(round(p.y * 10)))
    return out


def state_json(world, advisor=None, max_orders: int = MAX_ORDER_JSON,
               log_limit: int = MAX_LOG_JSON) -> dict:
    body = {
        "sim": {
            "seconds": world.now(),
            "clock": world.clock(),
            "paused": world.paused,
            "speed": world.speed_factor,
            "nextDispatchIn": world.next_dispatch_in(),
            "dispatchRounds": world.dispatch_rounds,
        },
        "cfg": config_json(world),
        "city": CITY,
        "worldW": world.width_m,
        "worldH": world.height_m,
        "network": {
            "mode": world.network_mode,
            "label": NetworkMode.label(world.network_mode),
            "name": world.network_name,
            "nodes": world.network_nodes,
            "edges": world.network_edges,
        },
        "stats": stats.of(world),
        "intake": world.intake_status(),
        "merchants": [
            {"id": m.id, "name": m.name, "x": m.pt.x, "y": m.pt.y, "prepSec": m.prep_sec}
            for m in world.merchants.values()
        ],
        "riders": [rider_json(world, r) for r in world.riders.values()],
        "orders": [order_json(world, o) for o in world.recent_orders(max_orders)],
        "pool": list(world.pool),
        "log": [
            {"t": t, "clock": clock_str(t), "kind": k, "text": txt}
            for (t, k, txt) in list(world.log)[:log_limit]
        ],
        "runId": world.run_id,
    }
    if advisor is not None:
        body["llm"] = {"status": advisor.status_json(),
                       "config": advisor.config.public_view()}
    return body


def stats_json(world) -> dict:
    """只给指标，不带订单明细 —— agent 和监控通常只要这一份。"""
    return {
        "ok": True,
        "runId": world.run_id,
        "sim": {"clock": world.clock(), "seconds": world.now(),
                "paused": world.paused, "speed": world.speed_factor},
        "network": {"mode": world.network_mode, "name": world.network_name,
                    "nodes": world.network_nodes, "edges": world.network_edges},
        "stats": stats.of(world),
        "intake": world.intake_status(),
    }


def zones_json(world, window_min: int, cells: int) -> dict:
    report = zone_analytics.analyze(world, window_min, cells)
    body = zone_analytics.to_json(report)
    body.update({"ok": True, "windowMin": window_min, "cells": cells,
                 "windowOptions": [15, 30, 60, 120, 0]})
    return body


def networks_json(world) -> dict:
    """可用路网清单：抽象城市 + 两个内置的 + data/osm 下的每个 .osm。

    「当前是哪一个」按 network_id 判断，不能按 mode —— 两个内置路网的 mode
    都是 SYNTHETIC，用 mode 比会让它们同时显示成「当前」。
    """
    items = [{"id": "abstract", "label": "抽象城市（1.4 × 直线距离）",
              "synthetic": False, "current": world.network_id == "abstract"}]
    for n in osm_loader.discover():
        items.append({"id": n.id, "label": n.label,
                      "synthetic": n.synthetic,
                      "current": n.id == world.network_id})
    return {"ok": True, "list": items,
            "dataDir": str(osm_loader.DATA_DIR.absolute())}


# ------------------------------------------------------------ 操作

def place_order(world, payload: dict) -> dict:
    """顾客下单：校验 → 进单判定 → 入池。"""
    name = trim(payload.get("name"))
    phone = trim(payload.get("phone"))
    address = trim(payload.get("address"))
    note = trim(payload.get("note")) or ""
    if not name:
        return {"ok": False, "error": "请填写姓名"}
    if not phone:
        return {"ok": False, "error": "请填写电话"}
    if not address:
        return {"ok": False, "error": "请填写地址"}

    allowed, reason = can_accept_order(world)
    if not allowed:
        return {"ok": False, "error": reason, "intake": world.intake_status()}

    mid = trim(payload.get("merchantId"))
    merchant = world.merchants.get(mid) if mid else None
    if merchant is None:
        merchant = world.rng.choice(list(world.merchants.values()))

    if "dx" in payload and "dy" in payload:
        dx = clamp(num(payload.get("dx"), 0.0), 200.0, max(400.0, world.width_m - 200.0))
        dy = clamp(num(payload.get("dy"), 0.0), 200.0, max(400.0, world.height_m - 200.0))
        dest = Pt(dx, dy)
    else:
        dest = world.random_dest_near(merchant.pt, 2600)

    try:
        order = world.place_order(name, phone, address, note, merchant, dest)
    except OrderLimitReached as e:
        return {"ok": False, "error": str(e)}
    world.add_log("下单", f"顾客 {order.customer_name} 在 {merchant.name} 下单 {order.id}")
    return {"ok": True, "orderId": order.id,
            "message": f"下单成功，订单号 {order.id}，已放入订单池等待派单"}


def random_order_sample(world) -> dict:
    """给「随机填写」用：返回一份随机顾客 + 商家 + 送达点，但**不下单**。"""
    name, phone, addr, note = world.random_customer()
    merchant = world.rng.choice(list(world.merchants.values()))
    dest = world.random_dest_near(merchant.pt, 2600)
    return {"ok": True,
            "customer": {"name": name, "phone": phone, "address": addr, "note": note},
            "merchantId": merchant.id, "merchantName": merchant.name,
            "dest": {"x": dest.x, "y": dest.y},
            "intake": world.intake_status()}


def auto_orders(world, payload: dict) -> dict:
    """一键随机生成 N 笔订单。停单期间会拒绝，并把原因说清楚。"""
    count = opt_int(payload.get("count"), 1, 1, 50)
    placed: List[str] = []
    reason = ""
    for _ in range(count):
        allowed, why = can_accept_order(world)
        if not allowed:
            reason = why
            break
        order = world.auto_place_order()
        placed.append(order.id)
        world.add_log("下单", f"顾客 {order.customer_name} 在 "
                              f"{world.merchants[order.merchant_id].name} 下单 {order.id}")

    if placed:
        msg = f"已随机生成 {len(placed)} 笔订单（{placed[0]}…{placed[-1]}）"
        if len(placed) < count:
            msg += f"；剩余 {count - len(placed)} 笔被拒：{reason}"
    else:
        msg = ""
    return {"ok": bool(placed), "placed": len(placed), "orderIds": placed,
            "error": "" if placed else (reason or "没有生成订单"),
            "message": msg, "intake": world.intake_status()}


def dispatch_now(world) -> dict:
    """立刻跑一轮派单。"""
    t0 = time.perf_counter()
    n = dispatcher.round_(world)
    obs.observe("dispatch_round_ms", (time.perf_counter() - t0) * 1000.0)
    obs.counter("dispatch_rounds")
    return {"ok": True, "assigned": n, "message": f"本轮派出 {n} 单"}


def assign(world, payload: dict) -> dict:
    """指派单：管理者手动指定骑手，算法不参与。"""
    order_id = trim(payload.get("orderId"))
    rider_id = trim(payload.get("riderId"))
    err = dispatcher.manual_assign(world, order_id, rider_id)
    return {"ok": False, "error": err} if err else {"ok": True, "message": "指派成功"}


def reassign(world, payload: dict) -> dict:
    """调单：把已派出的单转给另一个骑手。已取餐的会被拒绝。"""
    order_id = trim(payload.get("orderId"))
    rider_id = trim(payload.get("riderId"))
    err = dispatcher.reassign(world, order_id, rider_id)
    return {"ok": False, "error": err} if err else {"ok": True, "message": "调单成功"}


def rider_add(world, payload: dict) -> dict:
    x = payload.get("x")
    y = payload.get("y")
    # 接单上限夹到和 set_rider_cap 同一个区间（1..20）。
    # 不夹的话，新增的骑手可以一上来就背 10000 单 —— 和"改上限"走两套规则说不通。
    max_orders = None
    if isinstance(payload.get("maxOrders"), (int, float)):
        max_orders = clamp_int(int(payload["maxOrders"]), 1, 20)
    rider, err = dispatcher.add_rider(
        world,
        name=trim(payload.get("name")),
        phone=trim(payload.get("phone")),
        x=num(x, 0.0) if isinstance(x, (int, float)) else None,
        y=num(y, 0.0) if isinstance(y, (int, float)) else None,
        max_orders=max_orders,
    )
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "riderId": rider.id,
            "message": f"已新增骑手 {rider.id} {rider.name}（上限 {rider.max_orders} 单）"}


def rider_remove(world, payload: dict) -> dict:
    rider_id = trim(payload.get("riderId"))
    if not rider_id:
        return {"ok": False, "error": "缺少 riderId"}
    info, err = dispatcher.remove_rider(world, rider_id)
    if err:
        return {"ok": False, "error": err}
    returned = int(info.get("returnedOrders", 0) or 0) if isinstance(info, dict) else 0
    return {"ok": True, "returnedOrders": returned,
            "message": f"已移除骑手 {rider_id}"
                       + (f"，{returned} 笔未取餐订单退回订单池" if returned else "")}


def rider_settings(world, payload: dict) -> dict:
    rider_id = payload.get("riderId")
    err = None
    status = trim(payload.get("status"))
    if status:
        err = dispatcher.set_rider_status(world, rider_id, status)
    if err is None and isinstance(payload.get("maxOrders"), (int, float)):
        err = dispatcher.set_rider_cap(world, rider_id, int(payload["maxOrders"]))
    return {"ok": False, "error": err} if err else {"ok": True, "message": "已更新骑手设置"}


def apply_control(world, payload: dict) -> dict:
    """改参数/暂停。每项都有明确区间，越界一律夹住而不是照单全收。

    字段名映射只用 `INT_FIELDS` / `FLOAT_FIELDS` / `BOOL_FIELDS` 三张表
    （和 `--set` 覆盖、MCP 的 set_config 共用一份），不再各写一遍。
    """
    c = world.cfg
    b = payload

    if "paused" in b:
        world.paused = bool(b["paused"])
        world.add_log("系统", "模拟已暂停" if world.paused else "模拟继续")
    if isinstance(b.get("speed"), (int, float)):
        world.speed_factor = clamp(num(b["speed"], 20.0), 0.1, 2000.0)

    unknown = []
    for k, v in b.items():
        if k == "paused" or k == "speed":
            continue
        if k not in CFG_ALIASES:
            unknown.append(k)
            continue
        field = CFG_ALIASES[k]
        try:
            setattr(c, field, coerce_cfg(field, v))
        except (TypeError, ValueError):
            unknown.append(k)

    # 打开自动下单时立刻排下一拍，否则要等一个间隔才来单
    if c.auto_order and payload.get("autoOrder"):
        world.next_auto_order_at = world.now()
    # 停单相关的改动要允许重新记一次自动停单的日志
    if any(k in b for k in ("acceptOrders", "stopAcceptPoolRatio", "stopAcceptMinOrders")):
        world.intake_logged = False
    if "acceptOrders" in b:
        world.add_log("停单", f"管理平台手动{'开启' if c.accept_orders else '关闭'}进单")

    out = {"ok": True, "message": "参数已更新", "intake": world.intake_status()}
    if unknown:
        # 报出来而不是吞掉：静默忽略会让人拿着没生效的参数做对照实验
        out["ignored"] = unknown
        out["message"] = f"参数已更新（未识别：{', '.join(unknown)}）"
    return out


def reset(world) -> dict:
    world.reset()
    world.add_log("系统", "模拟已重置")
    return {"ok": True, "message": "模拟已重置", "runId": world.run_id}


def switch_network(world, network_id: str) -> dict:
    """切换路网。等价于一次重置：商家、骑手、订单池全部重建。

    **解析在锁外做**：OSM 解析是秒级的慢操作，而 world 锁同时挡着模拟线程
    和其他请求 —— 在锁里解析的话，一个请求就能让整个服务卡几秒，还能反复触发。
    """
    network_id = trim(network_id) or ""
    if not network_id:
        return {"ok": False, "error": "缺少 id"}

    if network_id == "abstract":
        with world.lock:
            world.load_abstract()
            world.add_log("系统", "已切回抽象城市模式")
        return {"ok": True, "message": "已切回抽象城市模式"}

    t0 = time.monotonic()
    res = osm_loader.load(network_id, None)
    parse_sec = time.monotonic() - t0
    # 内置路网（realistic / synthetic）和真的读 .osm 文件在界面上要区分开。
    # 判断依据由 loader 给出（res.synthetic），不再硬编码一份路网名单。
    mode = NetworkMode.SYNTHETIC if res.synthetic else NetworkMode.ROAD
    with world.lock:
        world.load_network(res.graph, res.pois, mode, network_id)
        world.add_log("系统", f"已切换到{NetworkMode.label(world.network_mode)}："
                              f"{world.network_nodes} 节点 / {world.network_edges} 边")
        nodes, edges, name = world.network_nodes, world.network_edges, world.network_name
    return {"ok": True, "nodes": nodes, "edges": edges,
            "message": f"已切换到 {name}：{nodes} 个节点、{edges} 条有向边，"
                       f"耗时 {parse_sec:.1f} 秒"}


# ------------------------------------------------------------ 无头模拟

def simulate(minutes: float, seed: int = 20260927, network: Optional[str] = None,
             orders_per_min: Optional[float] = None, riders: Optional[int] = None,
             steps_per_sec: bool = True, **cfg_over) -> dict:
    """**进程内无头跑一段模拟**，返回 KPI。不开 HTTP、不起线程、不用浏览器。

    这是给 agent 用的核心能力：跑实验不该依赖浏览器和一个长驻服务。
    而且它是**确定性**的 —— 固定 1 模拟秒一步，同种子同参数必然复现，
    所以"改一个参数、跑两次、比指标"是可信的（见 selftest 里的复现性测试）。

    返回的是指标，不是订单明细 —— agent 需要的是"好还是不好"。
    """
    from . import simulator as sim
    from .world import World

    world = World.seeded(seed)
    applied, unknown = apply_cfg_overrides(world, cfg_over)
    if network:
        res = osm_loader.load(network, None)
        mode = NetworkMode.SYNTHETIC if res.synthetic else NetworkMode.ROAD
        world.load_network(res.graph, res.pois, mode, network)
    if riders is not None and riders > 0:
        while len(world.riders) < riders:
            dispatcher.add_rider(world)
        while len(world.riders) > riders:
            victim = list(world.riders.values())[-1]
            if dispatcher.remove_rider(world, victim.id)[1]:
                break
    if orders_per_min is not None and orders_per_min > 0:
        world.cfg.auto_order = True
        # 必须是整数：模拟器里用 randrange() 抽抖动，浮点会抛 TypeError，
        # 而这个异常在模拟线程里会把整条仿真打断（不是只跳过这一拍）。
        world.cfg.auto_order_every_sec = max(1, int(round(60.0 / orders_per_min)))
        world.cfg.auto_order_jitter_sec = max(1, world.cfg.auto_order_every_sec // 4)

    world.cfg.auto_order = bool(world.cfg.auto_order)
    total = int(minutes * 60)
    step = 1.0 if steps_per_sec else 5.0
    t0 = time.perf_counter()
    done = 0.0
    while done < total:
        dt = min(step, total - done)
        sim.advance(world, dt)
        done += dt
    wall = time.perf_counter() - t0

    body = stats_json(world)
    body["ok"] = True
    body["sim"]["simSecondsElapsed"] = total
    body["params"] = {"minutes": minutes, "seed": seed, "network": network or "abstract",
                      "ordersPerMin": orders_per_min, "riders": len(world.riders),
                      "cfg": config_json(world)}
    if unknown:
        # 不认识的参数名要说出来。静默忽略会让人拿着两个其实一样的场景做对照
        # （`--set 拼错的键名=false` 什么都不改，看起来就像"这个开关没用"）。
        body["ok"] = False
        body["ignoredParams"] = unknown
        body["error"] = f"不认识的参数名：{', '.join(unknown)}"
    # 机器相关的数字单独放一块：它们取决于这台机器的快慢，**不是仿真结果的一部分**。
    # 混在一起会让"同种子两次跑出来的 JSON 不一样"这种误判发生
    # （stats 是完全确定的，只有这两个数会变）。
    body["perf"] = {"wallSeconds": round(wall, 2),
                    "realtimeFactor": round(total / wall, 1) if wall > 0 else None}
    return body


# ------------------------------------------------------------ 解释一笔派单

def explain_order(world, order_id: str) -> dict:
    """这笔单为什么派给了他？—— 把决策当时的依据摆出来。

    数据来源是订单自己的事件链（下单/派给谁/是否被推迟过/有没有被调单），
    不额外依赖落盘的训练数据，所以对内存里任何一笔单都能回答。
    """
    order_id = trim(order_id) or ""
    o = world.orders.get(order_id)
    if o is None:
        return {"ok": False, "error": f"找不到订单 {order_id}"}
    rider = world.riders.get(o.rider_id) if o.rider_id else None
    tier_label = {1: "顺路", 2: "无单", 3: "兜底"}.get(o.tier, "—")
    why = []
    if o.tier == 1:
        why.append(f"塞进已有路线只多绕 {o.detour_m:.0f} 米，在顺路阈值内")
    elif o.tier == 2:
        why.append(f"当时的空车骑手最近（空驶 {o.detour_m:.0f} 米）")
    elif o.tier == 3:
        why.append(f"没有更合适的骑手，只能是兜底（多绕 {o.detour_m:.0f} 米）")
    if o.postpone_count:
        why.append(f"曾被推迟 {o.postpone_count} 轮，等有没有更顺路的骑手")
    if o.reassign_count:
        why.append(f"被人工调单 {o.reassign_count} 次")
    return {
        "ok": True,
        "orderId": o.id,
        "status": o.status.name,
        "tier": o.tier,
        "tierLabel": tier_label,
        "detourM": o.detour_m,
        "postponedRounds": o.postpone_count,
        "reassignCount": o.reassign_count,
        "mode": o.mode.name if o.mode else None,
        "riderId": o.rider_id,
        "riderName": rider.name if rider else None,
        "why": why,
        "timeline": [
            {"clock": clock_str(e.t), "type": e.type, "detail": e.detail}
            for e in o.events
        ],
    }
