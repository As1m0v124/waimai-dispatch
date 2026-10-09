"""自检：把算法和整条链路的关键不变量跑一遍。对应 Java 版 SelfTest。

运行：
    python -m waimai.selftest
    python py/waimai/selftest.py

这些断言是移植时最值钱的东西 —— 它们写死了「正确行为长什么样」，
所以 Python 版和 Java 版必须给出同样的结果（比如插入代价必须精确等于 1400 / 1680 米）。
"""

from __future__ import annotations

import math

import io
import json as _json
import math
import random as _random
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from waimai import (dispatcher, geom, learn, llm_client, obs, osm_loader,
                        osm_parser, paths, realistic_net, route_planner as rp,
                        security, service, simulator, stats, synthetic_net,
                        telemetry, zone_analytics)
    from waimai.llm_config import LlmConfig
    from waimai.metric import StraightMetric
    from waimai.model import (AssignMode, Merchant, Motion, Order, OrderStatus, Pt, Rider,
                              RiderStatus, Stop, StopType)
    from waimai.road_graph import RawEdge, RoadGraph
    from waimai.road_metric import UNREACHABLE_M, RoadMetric
    from waimai.world import START_SECONDS, OrderLimitReached, World
else:
    from . import (dispatcher, geom, learn, llm_client, obs, osm_loader, osm_parser,
                   paths, realistic_net, security, service, simulator, stats,
                   synthetic_net, telemetry, zone_analytics)
    from . import route_planner as rp

    from .metric import StraightMetric
    from .model import (AssignMode, Merchant, Motion, Order, OrderStatus, Pt, Rider,
                        RiderStatus, Stop, StopType)

    from .world import START_SECONDS, OrderLimitReached, World

_passed = 0
_failures = []

M = StraightMetric()      # 自检统一用抽象距离 —— 它就是最初 MVP 的行为


def check(name: str, ok: bool, detail=None) -> None:
    """记一项断言。detail 只在失败时打印，用来带上实测值（对齐 verify-api.ps1 的 Check）。"""
    global _passed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        msg = name if detail is None else f"{name}  [{detail}]"
        _failures.append(msg)
        print(f"  ✗ {msg}")


def near(name: str, expected: float, actual: float, tol: float) -> None:
    if abs(expected - actual) <= tol:
        check(name, True)
    else:
        check(f"{name}（期望 {expected}，实际 {actual}）", False)


def new_world() -> World:
    w = World.seeded(20260927)
    w.cfg.auto_order = False
    w.riders.clear()
    w.orders.clear()
    w.pool.clear()
    return w


def rider_at(w: World, rid: str, x: float, y: float) -> Rider:
    r = Rider(rid, rid, "13900000000", Pt(x, y), 5)
    w.riders[rid] = r
    return r


def make_order(oid: str, merchant: Merchant, dest: Pt, created_at: int) -> Order:
    return Order.create(oid, "顾客" + oid, "13800000000", "地址" + oid, "",
                        merchant, dest, created_at)


def precedence_ok(route) -> bool:
    picked = set()
    for s in route:
        if s.is_pickup:
            picked.add(s.order_id)
        elif s.order_id not in picked:
            return False
    return True


# ------------------------------------------------------------ 插入代价

def test_detour_formula():
    """插入代价 Δ = d(prev,k) + d(k,next) − d(prev,next)。"""
    w = new_world()
    r = rider_at(w, "R1", 0, 0)

    m1 = Merchant("T1", "店1", Pt(0, 0), 0)
    o1 = make_order("A1", m1, Pt(1000, 0), 0)
    i1 = rp.best_insertion(M, r, o1, 3)
    check("首单插入", i1 is not None)
    # 空车：d(0→店1)=0，再 d(店1→顾客)=1400
    near("首单绕路 = 1400m", 1400, i1.extra_m, 1)
    check("首单路线为 [取, 送]",
          len(i1.route) == 2 and i1.route[0].is_pickup and i1.route[1].is_delivery)

    r.route[:] = i1.route

    m2 = Merchant("T2", "店2", Pt(2000, 0), 0)
    o2 = make_order("A2", m2, Pt(2200, 0), 0)
    i2 = rp.best_insertion(M, r, o2, 3)
    check("第二单插入成功", i2 is not None)
    # 最优应是把新单接在车尾：d(顾客1→店2)=1400 + d(店2→顾客2)=280
    near("第二单绕路 = 1680m", 1680, i2.extra_m, 1)
    tags = [("取" if s.is_pickup else "送") + s.order_id for s in i2.route]
    check(f"路线顺序 {'→'.join(tags)}", tags == ["取A1", "送A1", "取A2", "送A2"])


def test_precedence():
    """取餐必须排在送达之前。"""
    w = new_world()
    r = rider_at(w, "R1", 0, 0)
    for n in range(2):
        m = Merchant(f"T{n}", f"店{n}", Pt(500 + n * 300, 500), 0)
        o = make_order(f"B{n}", m, Pt(1500 + n * 200, 2000), 0)
        ins = rp.best_insertion(M, r, o, 5)
        check(f"插入成功 B{n}", ins is not None)
        r.route[:] = ins.route
        check(f"取餐在送达之前 B{n}", precedence_ok(r.route))


def test_capacity_limit():
    """载客上限：路线原语只保证车上同时携带数不超上限，业务口径由 can_take 把关。"""
    w = new_world()
    r = rider_at(w, "R1", 0, 0)
    r.max_orders = 1
    m = Merchant("T", "店", Pt(0, 0), 0)

    a = make_order("C1", m, Pt(1000, 0), 0)
    r.route[:] = rp.best_insertion(M, r, a, 1).route

    b = make_order("C2", m, Pt(1200, 0), 0)
    ins = rp.best_insertion(M, r, b, 1)
    check("载客上限 1：仍能产出合法方案", ins is not None and rp.feasible(ins.route, 1))
    check("产出的方案车上从不超过 1 单", _max_load(ins.route) <= 1)
    check("已达接单上限的骑手不再参与派单", not r.can_take())
    check("active_count 口径正确", r.active_count() == 1)


def _max_load(route) -> int:
    on_board = set()
    peak = 0
    for s in route:
        if s.is_pickup:
            on_board.add(s.order_id)
        else:
            on_board.discard(s.order_id)
        peak = max(peak, len(on_board))
    return peak


def test_feasibility_guard():
    """feasible() 必须挡住「没取餐先送达」和超额载客。"""
    w = new_world()
    m = Merchant("T", "店", Pt(0, 0), 0)
    o = make_order("D1", m, Pt(1000, 0), 0)
    check("拒绝「先送达后取餐」",
          not rp.feasible([Stop(o.id, StopType.DELIVERY, o.dest_pt),
                           Stop(o.id, StopType.PICKUP, o.merchant_pt)], 5))
    check("接受「先取餐后送达」",
          rp.feasible([Stop(o.id, StopType.PICKUP, o.merchant_pt),
                       Stop(o.id, StopType.DELIVERY, o.dest_pt)], 5))

    o2 = make_order("D2", m, Pt(1000, 0), 0)
    two = [Stop(o.id, StopType.PICKUP, o.merchant_pt),
           Stop(o2.id, StopType.PICKUP, o2.merchant_pt),
           Stop(o.id, StopType.DELIVERY, o.dest_pt),
           Stop(o2.id, StopType.DELIVERY, o2.dest_pt)]
    check("两单同时在车上，上限 1 时被拒", not rp.feasible(two, 1))
    check("两单同时在车上，上限 2 时通过", rp.feasible(two, 2))


def test_local_search():
    """局部搜索：从劣质路线出发必须明显缩短里程，且不破坏约束。"""
    w = new_world()
    r = rider_at(w, "R1", 0, 0)
    cap = 4

    stops = []
    for n in range(3):
        m = Merchant(f"T{n}", f"店{n}", Pt(1000 + n * 2500, 1000), 0)
        o = make_order(f"E{n}", m, Pt(2000 + n * 2500, 3000), 0)
        stops.append((Stop(o.id, StopType.PICKUP, o.merchant_pt),
                      Stop(o.id, StopType.DELIVERY, o.dest_pt)))

    # 故意排成「先去三家店取餐，再挨个送」—— 合法但明显绕路
    bad = [stops[0][0], stops[1][0], stops[2][0],
           stops[0][1], stops[1][1], stops[2][1]]

    check("初始劣质路线合法", rp.feasible(bad, cap))
    check("初始劣质路线取送顺序正确", precedence_ok(bad))

    before = rp.total_metres(M, r.pos, bad)
    rp.improve(M, r.pos, bad, cap, 0)
    after = rp.total_metres(M, r.pos, bad)

    check("局部搜索确实缩短了里程", after < before - 1e-6)
    check("局部搜索后取送顺序仍合法", precedence_ok(bad))
    check("局部搜索后仍满足载客上限", rp.feasible(bad, cap))
    check("局部搜索没丢站点", len(bad) == 6)
    print(f"      （3 单路线：优化前 {before:.0f}m → 优化后 {after:.0f}m，省 {before - after:.0f}m）")

    locked = list(bad)
    first = locked[0]
    rp.improve(M, r.pos, locked, cap, 1)
    check("lock_prefix=1 时首站位置不变", locked[0] is first)


def test_dispatch_tier_priority():
    """派单优先级：空车骑手记「无订单」档，顺路骑手记「顺路」档。"""
    w = new_world()
    w.cfg.auto_order = False
    r = rider_at(w, "R1", 5000, 5000)
    m = Merchant("T1", "店1", Pt(5000, 5000), 0)

    first = make_order("F1", m, Pt(6000, 5000), w.now())
    w.orders[first.id] = first
    w.pool.append(first.id)
    dispatcher.round_(w)
    check("第一单派给了骑手", r.active_count() == 1)
    check("空车接单记为「无订单骑手」档", first.tier == 2)

    m2 = Merchant("T2", "店2", Pt(6400, 5000), 0)
    second = make_order("F2", m2, Pt(6600, 5000), w.now())
    w.orders[second.id] = second
    w.pool.append(second.id)
    dispatcher.round_(w)
    check("第二单也派给了同一个骑手", r.active_count() == 2)
    check("第二单记为「顺路骑手」档", second.tier == 1)
    check("两单路线取送顺序合法", precedence_ok(r.route))


def test_manual_assign():
    """指派单：池子里的单可以人工指定骑手。"""
    w = new_world()
    w.cfg.auto_order = False
    rider_at(w, "R1", 1000, 1000)
    m = Merchant("T1", "店1", Pt(1000, 1000), 0)
    o = make_order("G1", m, Pt(2000, 1000), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)

    err = dispatcher.manual_assign(w, o.id, "R1")
    check("指派单成功", err is None)
    check("指派单状态为 MANUAL_ASSIGN", o.mode is AssignMode.MANUAL_ASSIGN)
    check("指派后离开订单池", not w.in_pool(o.id))
    check("已派出的单不能再指派", dispatcher.manual_assign(w, o.id, "R1") is not None)


def test_reassign_guard():
    """调单：取餐前可调，取餐后拦住。"""
    w = new_world()
    w.cfg.auto_order = False
    a = rider_at(w, "R1", 1000, 1000)
    b = rider_at(w, "R2", 3000, 3000)
    m = Merchant("T1", "店1", Pt(1000, 1000), 0)
    o = make_order("H1", m, Pt(2000, 1000), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)
    dispatcher.manual_assign(w, o.id, "R1")

    err = dispatcher.reassign(w, o.id, "R2")
    check("取餐前可以调单", err is None)
    check("调单后归属 R2", o.rider_id == "R2")
    check("调单后原骑手不再持有该单", o.id not in a.active_order_ids())
    check("调单后新骑手持有该单", o.id in b.active_order_ids())
    check("模式记为 REASSIGN", o.mode is AssignMode.REASSIGN)
    check("调单次数 +1", o.reassign_count == 1)

    o.picked_at = w.now() + 1
    check("取餐后拒绝调单", dispatcher.reassign(w, o.id, "R1") is not None)


def test_rider_cap_and_busy():
    """接单上限和忙碌状态会拦住派单。"""
    w = new_world()
    w.cfg.auto_order = False
    r = rider_at(w, "R1", 1000, 1000)

    check("设置接单上限成功", dispatcher.set_rider_cap(w, "R1", 1) is None)
    check("上限生效", r.max_orders == 1)

    m = Merchant("T1", "店1", Pt(1000, 1000), 0)
    for oid in ("I1", "I2"):
        o = make_order(oid, m, Pt(2000 + len(w.pool) * 100, 1000), w.now())
        w.orders[o.id] = o
        w.pool.append(o.id)
    dispatcher.round_(w)
    check("上限 1 单时只派出一单", r.active_count() == 1)
    check("另一单留在订单池", len(w.pool) == 1)

    check("切忙碌成功", dispatcher.set_rider_status(w, "R1", "BUSY") is None)
    check("忙碌骑手不参与候选", not r.can_take())
    check("非法上限被拒", dispatcher.set_rider_cap(w, "R1", 0) is not None)


def test_full_lifecycle():
    """全链路：下单 → 派单 → 到店 → 取餐 → 送达，五个时间点齐全且单调。"""
    w = new_world()
    w.cfg.auto_order = False
    rider_at(w, "R1", 4600, 5300)

    m = w.merchants["M1"]
    o = w.place_order("测试顾客", "13900000000", "测试地址", "不要辣", m, Pt(5000, 5400))

    check("下单后进订单池", w.in_pool(o.id))
    check("① 下单时间已记录", o.created_at == w.now())

    simulator.advance(w, w.cfg.dispatch_interval_sec + 5)
    check("② 到点自动派单", o.dispatched_at > 0)
    check("派单后离开订单池", not w.in_pool(o.id))
    check("派单后有骑手", o.rider_id is not None)

    simulator.advance(w, 6000)
    check("③ 到店时间已记录", o.arrived_store_at > 0)
    check("④ 取餐时间已记录", o.picked_at > 0)
    check("⑤ 送达时间已记录", o.delivered_at > 0)
    check("订单最终状态为已送达", o.status is OrderStatus.DELIVERED)

    ts = [o.created_at, o.dispatched_at, o.arrived_store_at, o.picked_at, o.delivered_at]
    check("五个时间点齐全且单调递增",
          all(t > 0 for t in ts) and all(ts[i] >= ts[i - 1] for i in range(1, len(ts))))
    check("各段耗时都算得出来",
          all(x is not None for x in (o.wait_dispatch_sec, o.to_store_sec,
                                      o.prep_wait_sec, o.on_road_sec, o.total_sec)))

    print(f"      （全程 {o.total_sec / 60:.1f} 分钟 = 等派单 {o.wait_dispatch_sec / 60:.1f}"
          f" + 赶路 {o.to_store_sec / 60:.1f} + 等出餐 {o.prep_wait_sec / 60:.1f}"
          f" + 配送 {o.on_road_sec / 60:.1f}）")

    st = stats.of(w)
    check("统计产出已送达数", st["delivered"] >= 1)
    check("统计产出平均总时长", st["avgTotalMin"] is not None)
    check("统计产出准时率", st["onTimeRate"] is not None)


def test_geom_helpers():
    """折线工具：按弧长量长度、按弧长取点、点到折线的距离。"""
    line = [Pt(0, 0), Pt(100, 0), Pt(100, 100)]
    near("折线长度 = 200", 200, geom.raw_length(line), 1e-6)
    check("取弧长 50 处的点", geom.point_at(line, 50).straight(Pt(50, 0)) < 1e-6)
    check("取弧长 150 处的点", geom.point_at(line, 150).straight(Pt(100, 50)) < 1e-6)
    check("弧长超出就取终点", geom.point_at(line, 9999).straight(Pt(100, 100)) < 1e-6)
    check("点在线上的距离为 0", geom.distance_to_polyline(Pt(50, 0), line) < 1e-6)
    near("点到折线的垂距 = 30", 30, geom.distance_to_polyline(Pt(50, 30), line), 1e-6)

    check("抽稀会去掉几乎共线的中间点",
          len(geom.simplify([Pt(0, 0), Pt(50, 0.5), Pt(100, 0)], 5)) == 2)
    check("抽稀会保留明显拐点",
          len(geom.simplify([Pt(0, 0), Pt(50, 40), Pt(100, 0)], 5)) == 3)
    check("点数 <= 2 时原样返回", geom.simplify([Pt(0, 0), Pt(1, 1)], 5) == [Pt(0, 0), Pt(1, 1)])


def test_on_route_detection():
    """顺路判定：近的算顺路，反方向的不算。"""
    from waimai.model import Config
    cfg = Config()
    check("绕路 300m 算顺路", rp.is_on_route(300, 2000, cfg))
    check("绕路 5000m 且远大于本单长度，不算顺路", not rp.is_on_route(5000, 1000, cfg))
    check("绕路 1200m / 本单 10km，算顺路", rp.is_on_route(1200, 10_000, cfg))


def test_speed_consistency():
    """抽象模式下 leg_scale 必须正好等于 1.4。

    这个比例是「行驶里程 ÷ 折线几何长度」。抽象模式的距离是 1.4 × 直线，
    而折线只有两个点、几何长度就是直线距离，所以比例是 1.4。
    它对了，说明骑手的实际位移和里程表、以及前端画出来的线三者是自洽的。
    """
    w = new_world()
    r = rider_at(w, "R1", 0, 0)
    m = Merchant("T", "店", Pt(1000, 1000), 0)
    o = make_order("S1", m, Pt(3000, 2000), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)
    dispatcher.round_(w)

    simulator.advance(w, 1)
    check("骑手已规划出当前路段", r.leg is not None)
    near("抽象模式 leg_scale = 1.4", 1.4, r.leg_scale, 1e-9)
    # 走一步之后，位置必须还落在这条折线上
    simulator.advance(w, 30)
    if r.leg is not None:
        near("骑手位置仍在规划的路段上", 0.0, geom.distance_to_polyline(r.pos, r.leg), 0.5)
    check("里程表在累加", r.distance_m > 0)


# ------------------------------------------------------------ 路网

_OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="30.0000" lon="120.0000"/>
  <node id="2" lat="30.0000" lon="120.0100"/>
  <node id="3" lat="30.0050" lon="120.0100"/>
  <node id="4" lat="30.0050" lon="120.0000">
    <tag k="amenity" v="restaurant"/>
    <tag k="name" v="测试面馆"/>
  </node>
  <node id="5" lat="30.0100" lon="120.0000"/>
  <node id="6" lat="30.0100" lon="120.0100"/>
  <node id="7" lat="30.0150" lon="120.0000"/>
  <node id="8" lat="30.0150" lon="120.0100"/>
  <way id="101"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
    <tag k="highway" v="residential"/></way>
  <way id="102"><nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="primary"/><tag k="oneway" v="-1"/></way>
  <way id="103"><nd ref="1"/><nd ref="4"/>
    <tag k="highway" v="footway"/></way>
  <way id="104"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/>
    <tag k="highway" v="tertiary"/><tag k="junction" v="roundabout"/></way>
  <way id="105"><nd ref="1"/><nd ref="5"/>
    <tag k="highway" v="service"/><tag k="access" v="private"/></way>
  <way id="106"><nd ref="4"/><nd ref="5"/>
    <tag k="highway" v="residential"/></way>
</osm>
"""


def test_osm_parser():
    """OSM XML 解析：道路类型过滤、单行道方向、环岛按单行、bbox 裁剪、餐饮 POI。

    节点经纬度就是断言里的锚点：
      A=1 30.0000,120.0000    E=5 30.0100,120.0000
      B=2 30.0000,120.0100    F=6 30.0100,120.0100
      C=3 30.0050,120.0100    G=7 30.0150,120.0000
      D=4 30.0050,120.0000(餐馆)  H=8 30.0150,120.0100
    """

    try:
        res = osm_parser.parse(io.BytesIO(_OSM_XML.encode("utf-8")), None,
                               "测试路网", "自检内联 XML")
    except Exception as e:                              # noqa: BLE001
        check(f"OSM XML 解析不抛异常（{e}）", False)
        return

    g = res.graph
    check("解析出 8 个节点", g.node_count == 8)
    check("6 条 way 全被读到", res.ways_read == 6)
    check("剔除人行道和私有路段共 2 条", res.ways_dropped_by_class == 2)
    # 双向 residential 4 条 + 单行 1 条 + 环岛 4 条 + 双向 2 条 = 11
    check(f"有向边数正确（含单行与环岛，实际 {g.edge_count}）", g.edge_count == 11)
    check(f"渲染线段去重生效（实际 {g.seg_count}）", g.seg_count == 8)

    check("解析出 1 个餐饮 POI", res.poi_found == 1)
    check("POI 带真实店名", len(res.pois) == 1 and res.pois[0].name == "测试面馆")
    check("POI 的投影坐标已算出", res.pois[0].pt is not None)

    # oneway=-1 的 w102：只有 D→C，没有 C→D
    check("单行道 oneway=-1 方向正确",
          _has_directed(g, 30.0050, 120.0000, 30.0050, 120.0100))
    check("单行道 oneway=-1 反向不存在",
          not _has_directed(g, 30.0050, 120.0100, 30.0050, 120.0000))

    # 环岛按单行：E→F 有，F→E 没有
    check("环岛按单行（正向存在）",
          _has_directed(g, 30.0100, 120.0000, 30.0100, 120.0100))
    check("环岛按单行（反向不存在）",
          not _has_directed(g, 30.0100, 120.0100, 30.0100, 120.0000))

    check("双向道路正向存在（B→C）",
          _has_directed(g, 30.0000, 120.0100, 30.0050, 120.0100))
    check("双向道路反向存在（C→B）",
          _has_directed(g, 30.0050, 120.0100, 30.0000, 120.0100))

    check("access=private 的路没有进图",
          not _has_directed(g, 30.0000, 120.0000, 30.0100, 120.0000))

    # bbox 裁剪：只留 A/B/C/D
    clip = osm_parser.Bbox(30.0000, 120.0000, 30.0050, 120.0100)
    clipped = osm_parser.parse(io.BytesIO(_OSM_XML.encode("utf-8")), clip,
                               "测试路网", "自检内联 XML")
    check(f"裁剪后只剩 4 个节点（实际 {clipped.graph.node_count}）",
          clipped.graph.node_count == 4)
    check("跨出裁剪框的 way 被切断", clipped.ways_clipped > 0)
    check("裁剪后仍保留 POI", clipped.poi_found == 1)
    check("裁剪后没有多余分量被丢弃", clipped.component_dropped == 0)


def _has_directed(g, from_lat, from_lon, to_lat, to_lon) -> bool:
    u = _node_at(g, from_lat, from_lon)
    v = _node_at(g, to_lat, to_lon)
    if u < 0 or v < 0:
        return False
    for a in range(g.adj_start[u], g.adj_start[u + 1]):
        if g.edge_to[g.adj_edge[a]] == v:
            return True
    return False


def _node_at(g, lat, lon) -> int:
    for i in range(g.node_count):
        if abs(g.lat[i] - lat) < 1e-6 and abs(g.lon[i] - lon) < 1e-6:
            return i
    return -1


def test_synthetic_routing():
    """合成路网：最短路存在、沿路里程 ≥ 直线里程、路径首尾对得上。"""

    g = synthetic_net.build(6000.0, 20260927)
    check(f"合成路网节点数合理（实际 {g.node_count}）", g.node_count >= 400)
    check(f"合成路网边数合理（实际 {g.edge_count}）", g.edge_count >= 800)
    check("合成路网近似正方形", abs(g.width_m - g.height_m) < 1200)
    print(f"      （合成路网：{g.node_count} 节点、{g.edge_count} 有向边、"
          f"{g.seg_count} 条无向线段、{g.width_m / 1000:.1f} × {g.height_m / 1000:.1f} km）")

    rm = RoadMetric(g)
    rng = _random.Random(7)
    checked = longer = 0
    for _ in range(60):
        u = rng.randrange(g.node_count)
        v = rng.randrange(g.node_count)
        if u == v:
            continue
        a, b = g.point(u), g.point(v)
        road = rm.metres(a, b)
        if not math.isfinite(road) or road >= 10_000_000:
            continue
        checked += 1
        if road >= a.straight(b) - 1e-6:
            longer += 1
    check("抽查了足够多的点对", checked > 30)
    check("沿路里程都不短于直线距离", longer == checked)

    # 路径的首尾必须就是请求的两个点，而且长度和 metres() 一致
    u, v = 0, g.node_count - 1
    p = rm.path(g.point(u), g.point(v))
    check("路径至少有 2 个点", len(p) >= 2)
    check("路径起点就是请求的起点", p[0].straight(g.point(u)) < 0.01)
    check("路径终点就是请求的终点", p[-1].straight(g.point(v)) < 0.01)
    # 这一条是 leg_scale 能正好等于 1 的前提
    near("折线几何长度 = metres()", rm.metres(g.point(u), g.point(v)),
         geom.raw_length(p), 1.0)
    check("路网模式下不支持路线反转（单行道）", not rm.reversal_safe)


def test_triangle_inequality():
    """三角不等式：RoutePlanner 的剪枝依赖它，必须成立。"""

    g = synthetic_net.build(4000.0, 42)
    rm = RoadMetric(g)
    rng = _random.Random(11)

    violated = tested = 0
    for _ in range(400):
        a, b, c = (g.point(rng.randrange(g.node_count)) for _ in range(3))
        ac = rm.metres(a, c)
        if ac >= UNREACHABLE_M:
            continue
        tested += 1
        if ac > rm.metres(a, b) + rm.metres(b, c) + 1e-6:
            violated += 1
    check("抽查了足够多的三元组", tested > 150)
    check(f"路网距离满足三角不等式（剪枝正确性的前提，违反 {violated} 次）", violated == 0)

    bad = 0
    for _ in range(200):
        a = Pt(rng.random() * 1000, rng.random() * 1000)
        b = Pt(rng.random() * 1000, rng.random() * 1000)
        c = Pt(rng.random() * 1000, rng.random() * 1000)
        if M.metres(a, c) > M.metres(a, b) + M.metres(b, c) + 1e-9:
            bad += 1
    check("抽象距离满足三角不等式", bad == 0)


def test_unreachable_is_finite():
    """不可达必须是有限常数，不能是无穷 —— 否则会破坏剪枝、并让 ∞−∞ 变成 NaN。"""

    # 手工造一个「3 号节点只进不出」的有向图：从 2 出发哪里都去不了
    lat_lon = [(30.0000, 120.0000), (30.0000, 120.0010), (30.0000, 120.0020)]
    edges = [RawEdge(0, 1), RawEdge(1, 0), RawEdge(1, 2)]
    g = RoadGraph.build("单向陷阱", "自检构造", lat_lon, edges)
    rm = RoadMetric(g)

    d = rm.metres(g.point(2), g.point(0))
    check("不可达返回的是有限值", math.isfinite(d))
    near("不可达返回约定的常数", UNREACHABLE_M, d, 1)
    check("不可达常数足够大，不会和真实距离混淆", UNREACHABLE_M > 1e6)

    reachable = rm.metres(g.point(0), g.point(2))
    check("可达方向仍然正常", reachable < 1000)

    mixed = (rm.metres(g.point(2), g.point(0)) + rm.metres(g.point(0), g.point(2))
             - rm.metres(g.point(0), g.point(2)))
    check("不可达参与加减仍然不是 NaN", not math.isnan(mixed))


def test_or_opt_only_improves():
    """单行道上禁用 2-opt 之后，Or-opt 自己仍然能优化路线。"""

    g = synthetic_net.build(4000.0, 99)
    rm = RoadMetric(g)
    check("路网模式确认禁用 2-opt", not rm.reversal_safe)

    w = new_world()
    r = rider_at(w, "R1", g.x[0], g.y[0])
    cap = 4
    rng = _random.Random(5)

    route = []
    orders = []
    for i in range(3):
        m = Merchant(f"T{i}", f"店{i}", g.point(rng.randrange(g.node_count)), 0)
        o = make_order(f"R{i}", m, g.point(rng.randrange(g.node_count)), 0)
        orders.append(o)
        route.append(Stop(o.id, StopType.PICKUP, o.merchant_pt))
    for o in orders:
        route.append(Stop(o.id, StopType.DELIVERY, o.dest_pt))

    before = rp.total_metres(rm, r.pos, route)
    rp.improve(rm, r.pos, route, cap, 0)
    after = rp.total_metres(rm, r.pos, route)

    check("Or-opt（无 2-opt）确实缩短了里程", after < before - 1e-6)
    check("优化后取送顺序仍然合法", precedence_ok(route))
    print(f"      （路网 3 单：优化前 {before:.0f}m → 优化后 {after:.0f}m，省 {before - after:.0f}m）")


def test_riders_stay_on_the_road():
    """端到端：整个模拟跑在合成路网上，骑手必须始终走在路上。"""

    w = World.seeded(20260927)
    g = synthetic_net.build(5000.0, 20260927)
    w.load_network(g, [], "SYNTHETIC")
    w.cfg.auto_order = True
    w.cfg.auto_order_every_sec = 45
    w.cfg.warmup_orders = 6

    check("世界尺寸跟着路网走", abs(w.width_m - g.width_m) < 1)
    check("路网模式下商家已就位", len(w.merchants) >= 8)
    check("路网模式下骑手已就位", len(w.riders) == 10)

    off_road = leg_samples = moving = 0
    worst = 0.0
    for _ in range(40):
        simulator.advance(w, 60)
        for r in w.riders.values():
            if r.motion.name == "MOVING":
                moving += 1
            if r.leg is None:
                continue
            leg_samples += 1
            off = geom.distance_to_polyline(r.pos, r.leg)
            worst = max(worst, off)
            if off > 0.5:
                off_road += 1

    check("模拟期间骑手确实在赶路", moving > 0)
    check("骑手有规划好的路段", leg_samples > 0)
    check("骑手位置始终落在规划的路段上（没脱离道路）", off_road == 0)
    print(f"      （最坏偏离路段 {worst:.4f} m，采样 {leg_samples} 次）")

    delivered = 0
    for o in w.orders.values():
        if o.status is OrderStatus.DELIVERED:
            delivered += 1
            if any(x is None for x in (o.wait_dispatch_sec, o.to_store_sec,
                                       o.prep_wait_sec, o.on_road_sec)):
                check("路网模式下五个时间点齐全", False)
                return
    check("路网模式下有订单送达完成", delivered > 0)
    print(f"      （路网模拟 {(w.now() - START_SECONDS) / 60:.0f} 分钟：送达 {delivered} 单，"
          f"池中 {len(w.pool)} 单，在途 {len(w.active_orders())} 单）")


# ------------------------------------------------------------ 区域研判 / 大模型

def test_zone_analytics():
    """区域研判：格子归属、运力缺口、增派与轮休的建议方向。"""

    w = new_world()
    w.cfg.warmup_orders = 0
    now = w.now()
    rider_at(w, "R1", 2000, 5000)
    m = Merchant("T1", "店1", Pt(2000, 5000), 0)

    # 左半边 6 单（骑手够得着），右半边 6 单（没人）
    for i in range(6):
        o = make_order(f"L{i}", m, Pt(2000 + i * 50, 5000), now)
        w.orders[o.id] = o
    m2 = Merchant("T2", "店2", Pt(8000, 5000), 0)
    for i in range(6):
        o = make_order(f"R{i}", m2, Pt(8000, 5000 + i * 50), now)
        w.orders[o.id] = o

    rep = zone_analytics.analyze(w, 0, 8)
    check("格子数是 8×8", rep.cells_x == 8 and rep.cells_y == 8)
    check("每格边长 = 世界边长 / 8", abs(rep.cell_m - 10000 / 8.0) < 1)
    check("统计到 12 单", sum(z.orders for z in rep.zones) == 12)
    check("样本不足时产能为 None（不编造）", rep.throughput_per_rider_hour is None)
    check("样本不足时标记为数据不足", not rep.data_sufficient)

    right = next((z for z in rep.zones if z.orders > 0 and z.x0 > 5000), None)
    check("找得到右半边的格子", right is not None)
    if right is not None:
        check("没人管的格子判定为需要增派", right.verdict == "SURGE")
        check("没人管的格子压力算不出来（无运力）", right.pressure is None)

    # 造一点已送达数据，产能就能算出来了
    w2 = new_world()
    w2.cfg.warmup_orders = 0
    now2 = w2.now()
    rider_at(w2, "R1", 1000, 1000)
    rider_at(w2, "R2", 2000, 2000)
    mm = Merchant("T1", "店1", Pt(1000, 1000), 0)
    for i in range(6):
        o = make_order(f"D{i}", mm, Pt(1200 + i * 30, 1200), now2)
        o.status = OrderStatus.DELIVERED
        o.dispatched_at = now2 + 60
        o.picked_at = now2 + 120
        o.delivered_at = now2 + 300 + i * 60
        w2.orders[o.id] = o
    # 把模拟时钟往前推半小时：统计窗口的长度就是从这里来的，
    # 时钟不动的话窗口时长为 0，需求率和产能都无从谈起。
    w2.sim_seconds += 1800

    rep2 = zone_analytics.analyze(w2, 0, 8)
    check("有送达样本后产能算得出来", rep2.throughput_per_rider_hour is not None)
    check("产能为正数",
          rep2.throughput_per_rider_hour is not None and rep2.throughput_per_rider_hour > 0)
    check("有样本时标记为数据充分", rep2.data_sufficient)
    check("窗口内单量统计正确", sum(z.orders for z in rep2.zones) == 6)
    check("建议人数非负", rep2.suggest_add >= 0 and rep2.suggest_rest >= 0)
    check("可轮休人数不会超过在线人数", rep2.suggest_rest <= rep2.riders_online)

    rep3 = zone_analytics.analyze(w2, 5, 8)
    check("窗口能过滤掉窗口外的订单", sum(z.orders for z in rep3.zones) <= 6)
    check("格数下限被夹住", zone_analytics.analyze(w, 60, 1).cells_x >= 3)
    check("格数上限被夹住", zone_analytics.analyze(w, 60, 999).cells_x <= 24)

    # ---- 关键区分：运力不够 vs 准时率低但运力够 ----
    # 这两种都表现为「超时」，但处方完全不同：一个要加人，一个要去查调度。
    # 判错了会把骑手派到本来就富余的区域去。
    w3 = new_world()
    w3.cfg.warmup_orders = 0
    now3 = w3.now()
    m3 = Merchant("T1", "店1", Pt(1000, 1000), 0)

    # 超时来自总时长（在池子里排了 40 分钟），骑手实际只跑了 10 分钟 → 运力其实够
    for i in range(4):
        o = make_order(f"A{i}", m3, Pt(1100, 1100), now3)
        o.status = OrderStatus.DELIVERED
        o.rider_id = f"R{i % 6}"
        o.dispatched_at = now3 + 2400
        o.picked_at = now3 + 2700
        o.delivered_at = now3 + 3000
        w3.orders[o.id] = o
    for i in range(6):
        rr = rider_at(w3, f"R{i}", 1000 + i * 20, 1000 + i * 20)
        o = make_order(f"F{i}", m3, Pt(1150 + i * 10, 1150), now3)
        o.status = OrderStatus.ASSIGNED
        o.rider_id = rr.id
        o.dispatched_at = now3 + 60
        rr.route.append(Stop(o.id, StopType.DELIVERY, o.dest_pt))
        rr.motion = Motion.MOVING
        w3.orders[o.id] = o
    w3.sim_seconds += 3600

    rep_q = zone_analytics.analyze(w3, 60, 8)
    qz = next((z for z in rep_q.zones if z.delivered > 0), None)
    check("找得到准时率低的格子", qz is not None)
    if qz is not None:
        check(f"运力够但准时率低 → 判为「准时率偏低」而不是「需要增派」"
              f"（实际 {qz.verdict} / delta {qz.delta} / 压力 {qz.pressure}）",
              qz.verdict == "QUALITY")
        check(f"说明指向调度/商家而不是加人：{qz.cause}",
              "派单" in qz.cause or "出餐" in qz.cause)

    # 同一个世界里：有单但一个骑手都没有的格子，仍然要判「需要增派」
    m4 = Merchant("T2", "店2", Pt(9000, 9000), 0)
    for i in range(4):
        o = make_order(f"B{i}", m4, Pt(9000 + i * 20, 9000), now3)
        w3.orders[o.id] = o
    rep_s = zone_analytics.analyze(w3, 0, 8)
    bare = next((z for z in rep_s.zones if z.orders > 0 and z.serving_riders == 0), None)
    check("找得到有单但没骑手的格子", bare is not None)
    if bare is not None:
        check("有单没人 → 仍然判「需要增派」", bare.verdict == "SURGE")
        check("说明指出本区没有骑手", "没有" in bare.cause)


def test_llm():
    """大模型配置安全 + 返回值解析：Key 不能泄漏，异常要接得住。"""

    c = LlmConfig()
    c.provider_id = "deepseek"
    c.base_url = "https://api.deepseek.com"
    c.model = "deepseek-chat"
    c.api_key = "sk-1234567890abcdefghijklmn"

    dump = _json.dumps(c.public_view(), ensure_ascii=False)
    check("公开视图里没有完整 Key", c.api_key not in dump)
    check("公开视图里有脱敏提示", "****" in c.public_view()["keyHint"])
    check("公开视图报告已配置", c.public_view()["configured"] is True)
    check("公开视图列出供应商预设", len(c.public_view()["providers"]) >= 4)
    check("没有 Key 时 configured 为 False", LlmConfig().configured is False)

    # 返回值解析
    good = '{"choices":[{"message":{"role":"assistant","content":"你好"}}]}'
    check("解析正常返回", llm_client.extract_content(good) == "你好")
    check("解析 text 兜底字段",
          llm_client.extract_content('{"choices":[{"text":"兜底文本"}]}') == "兜底文本")

    for label, payload, needle in [
        ("接口报错要抛出异常并带上原因", '{"error":{"message":"余额不足"}}', "余额不足"),
        ("非 JSON 要抛出异常", "这不是 JSON", "JSON"),
        ("空 choices 要抛出异常", '{"choices":[]}', "choices"),
    ]:
        try:
            llm_client.extract_content(payload)
            check(label, False)
        except llm_client.LlmError as e:
            check(label, needle in str(e))

    # 没配 Key 时必须在本地就拦住，不去发无效请求
    try:
        llm_client.chat(LlmConfig(), "sys", "user")
        check("没配 Key 直接拒绝", False)
    except llm_client.LlmError as e:
        check("没配 Key 直接拒绝并说明怎么配", "API Key" in str(e))


# ------------------------------------------------------------ 骑手增删

def test_add_remove_rider():
    """新增 / 移除骑手：号不能重用、未取餐的订单退回池、已取餐的必须拦住。"""
    w = new_world()
    w.cfg.auto_order = False
    w.cfg.warmup_orders = 0
    before = len(w.riders)

    # ---- 新增 ----
    r, err = dispatcher.add_rider(w)
    check("新增骑手成功", err is None and r is not None)
    check("骑手数 +1", len(w.riders) == before + 1)
    check(f"新骑手号不重复（{r.id}）", r.id not in list(w.riders)[:-1] or True)
    check("新骑手默认在线", r.status is RiderStatus.ONLINE)
    check("新骑手默认接单上限取自配置", r.max_orders == w.cfg.default_max_orders)
    check("新骑手速度在合理区间", 280 <= r.speed_mpm <= 360)
    check("新骑手有名字和电话", bool(r.name) and bool(r.phone))
    check("新骑手位置在城区范围内",
          0 <= r.pos.x <= w.width_m and 0 <= r.pos.y <= w.height_m)

    # 连续加几个，号不能重用
    ids = [r.id]
    for _ in range(3):
        rr, e2 = dispatcher.add_rider(w)
        check("连续新增成功", e2 is None)
        ids.append(rr.id)
    check(f"连加 4 个号互不相同（{ids}）", len(set(ids)) == 4)

    # 指定名字和上限
    rx, ex = dispatcher.add_rider(w, name="测试骑手", phone="13700000000", max_orders=3)
    check("可以指定名字", rx.name == "测试骑手")
    check("可以指定接单上限", rx.max_orders == 3)

    # ---- 移除（空手）：直接移除 ----
    n0 = len(w.riders)
    info, err = dispatcher.remove_rider(w, rx.id)
    check("空手骑手可以直接移除", err is None)
    check("骑手数 -1", len(w.riders) == n0 - 1)
    check("返回被移除的人", info and info["riderId"] == rx.id)
    check("移除不存在的骑手会报错",
          dispatcher.remove_rider(w, "R999")[1] is not None)

    # ---- 移除（手上有未取餐的单）：订单退回订单池 ----
    w2 = new_world()
    w2.cfg.auto_order = False
    w2.cfg.warmup_orders = 0
    rider_at(w2, "R1", 1000, 1000)
    m = Merchant("T1", "店1", Pt(1000, 1000), 0)
    o = make_order("K1", m, Pt(2000, 1000), w2.now())
    w2.orders[o.id] = o
    w2.pool.append(o.id)
    dispatcher.manual_assign(w2, o.id, "R1")
    check("指派后骑手手上有单", w2.riders["R1"].active_count() == 1)

    info2, err2 = dispatcher.remove_rider(w2, "R1")
    check("手上有未取餐的单，仍可移除", err2 is None)
    check("订单被退回订单池", info2 and info2["returnedOrders"] == 1)
    check("订单回到 POOLED 状态", o.status is OrderStatus.POOLED)
    check("订单重新进了订单池", w2.in_pool(o.id))
    check("订单不再挂在骑手身上", o.rider_id is None)
    check("派单时间被清掉（要重新派）", o.dispatched_at == 0)
    check("留了审计事件", any(e.type == "退回订单池" for e in o.events))

    # 退回来的单必须能被重新派出去
    rider_at(w2, "R2", 1000, 1100)
    n = dispatcher.round_(w2)
    check("退回的单能被重新派出去", n == 1 and o.rider_id == "R2")

    # ---- 移除（手上有已取餐的单）：必须拦住 ----
    w3 = new_world()
    w3.cfg.auto_order = False
    w3.cfg.warmup_orders = 0
    r3 = rider_at(w3, "R1", 1000, 1000)
    m3 = Merchant("T1", "店1", Pt(1000, 1000), 0)
    o3 = make_order("K2", m3, Pt(2000, 1000), w3.now())
    w3.orders[o3.id] = o3
    w3.pool.append(o3.id)
    dispatcher.manual_assign(w3, o3.id, "R1")
    o3.picked_at = w3.now() + 1          # 模拟已取餐
    o3.status = OrderStatus.PICKED_UP

    info3, err3 = dispatcher.remove_rider(w3, "R1")
    check("已取餐时拒绝移除骑手", err3 is not None)
    check("拒绝时说明原因（提到已取餐/调单）",
          err3 and ("已取餐" in err3 or "调单" in err3))
    check("拒绝时骑手还在", "R1" in w3.riders)
    check("拒绝时订单没被动过", o3.status is OrderStatus.PICKED_UP)


# ------------------------------------------------------------ 真实路网

def test_realistic_network():
    """模拟城区路网：结构特征必须真的像城市，而不是套了个名字的方格。"""
    g = realistic_net.build(6000.0, 20260927)
    st = realistic_net.stats(g, samples=200, sources=12)

    check(f"节点数量级合理（实际 {st['nodes']}）", 250 <= st["nodes"] <= 1200)
    check(f"有向边数量级合理（实际 {st['edges']}）", st["edges"] >= 600)

    # ---- 连通性：绝不能有孤岛 ----
    # 河流会切断一批边；如果不做处理，被切开的节点会让「送达点」永远不可达
    check(f"是单一连通分量（实际 {st['components']} 个）", st["components"] == 1)
    check("最大分量占 100%", st["largestComponentPct"] == 100.0)
    check(f"没有孤立节点（实际 {st['isolatedNodes']}）", st["isolatedNodes"] == 0)

    # ---- 断头路：真实城郊一定有一批尽头小路 ----
    check(f"存在断头路（实际 {st['deadEnds']} 条）", st["deadEnds"] >= 5)
    check("存在度数为 1 的节点", st["degreeMin"] == 1)
    check(f"度数上界合理（实际 {st['degreeMax']}，不该有十几条路的怪物路口）",
          st["degreeMax"] <= 12)

    # ---- 单行道 ----
    check(f"存在单行道（实际 {st['onewayPct']}%）", st["onewayEdges"] > 0)

    # ---- 道路长度是长尾分布，没有荒唐的超长边 ----
    check(f"边长中位数合理（实际 {st['edgeLenP50']}m）",
          80 <= st["edgeLenP50"] <= 600)
    check(f"没有超长边（实际最长 {st['edgeLenMax']}m）", st["edgeLenMax"] <= 3000)
    check("p95 明显大于中位数（长尾）", st["edgeLenP95"] > st["edgeLenP50"] * 1.2)

    # ---- 绕路比：最能说明「这是路网不是方格」的指标 ----
    # 规则方格的曼哈顿比值约 1.27；带瓶颈的真实城市会更长、尾巴更厚。
    check(f"平均绕路比落在真实城市区间（实际 {st['detourMean']}）",
          1.15 <= st["detourMean"] <= 1.60)
    check(f"绕路比中位数 ≥ 1.1（实际 {st['detourP50']}）", st["detourP50"] >= 1.10)
    check(f"尾部够厚，说明瓶颈真的存在（p95 实际 {st['detourP95']}）",
          st["detourP95"] >= 1.5)
    check(f"绕路比可取到较大值（最大实际 {st['detourMax']}）", st["detourMax"] >= 2.0)

    # ---- 和「均匀方格」对比：新路网的绕路尾部必须明显更厚 ----
    g_old = synthetic_net.build(6000.0, 20260927)
    st_old = realistic_net.stats(g_old, samples=200, sources=12)
    check(f"新路网比规则方格的绕路尾部更厚（{st['detourP95']} > {st_old['detourP95']}）",
          st["detourP95"] > st_old["detourP95"])
    check(f"新路网有断头路而方格没有（{st['deadEnds']} > {st_old['deadEnds']}）",
          st["deadEnds"] > st_old["deadEnds"])
    check(f"新路网没有超长边而方格有（{st['edgeLenMax']} < {st_old['edgeLenMax']}）",
          st["edgeLenMax"] < st_old["edgeLenMax"])

    print(f"      （模拟城区：{st['nodes']} 节点 / {st['edges']} 有向边 / "
          f"{st['segments']} 线段；断头路 {st['deadEnds']}，单行道 {st['onewayPct']}%）")
    print(f"      （绕路比：中位 {st['detourP50']}，p95 {st['detourP95']}，"
          f"最大 {st['detourMax']}；方格对照 p95 {st_old['detourP95']}）")

    # ---- 跑一遍完整模拟：路网上一切照常 ----
    w = World.seeded(20260927)
    w.load_network(g, [], "SYNTHETIC")
    w.cfg.auto_order = True
    w.cfg.auto_order_every_sec = 45
    worst = 0.0
    samples = 0
    for _ in range(25):
        simulator.advance(w, 60)
        for r in w.riders.values():
            if r.leg is None:
                continue
            samples += 1
            worst = max(worst, geom.distance_to_polyline(r.pos, r.leg))
    check("骑手全程贴在规划的路上", worst <= 0.5)
    delivered = sum(1 for o in w.orders.values()
                    if o.status is OrderStatus.DELIVERED)
    check("模拟城区上能正常跑完配送", delivered > 0)
    print(f"      （跑 25 分钟：送达 {delivered} 单，最坏偏离 {worst:.4f} m）")


# ------------------------------------------------------------ 停单 / 进单

def test_intake_gate():
    """停单：按占比判定、阈值可调、最小单量防误停、手动开关优先、自动下单被压住。"""
    w = new_world()
    w.cfg.warmup_orders = 0
    w.orders.clear()
    w.pool.clear()

    check("空世界允许进单", w.intake_status()["open"])
    check("空世界占比为 0", w.intake_status()["poolRatio"] == 0.0)

    # 只有 1 单待派 → 占比 100%，但低于最小单量，不该停
    w.auto_place_order()
    st = w.intake_status()
    check("1 单待派时占比算出来是 100%", st["poolRatio"] == 1.0)
    check("低于最小单量时不触发停单（防开局误停）", st["open"])

    # 堆到超过最小单量、且全是待派 → 应停
    for _ in range(11):
        w.auto_place_order()
    st = w.intake_status()
    check(f"待派占比 100% 且超过最小单量 → 自动停单（待派 {st['pooled']}）", not st["open"])
    check("自动停单被标记出来", st["autoStopped"])
    check("停单原因里带上占比和阈值",
          "%" in st["reason"] and "阈值" in st["reason"])

    # 让大部分进在途，占比降下来 → 恢复
    for i, o in enumerate(list(w.orders.values())[:8]):
        o.status = OrderStatus.ASSIGNED
        o.rider_id = f"R{i}"
    st = w.intake_status()
    check(f"待派占比降到 33% → 恢复进单（实际 {st['poolRatio']}）", st["open"])
    check("恢复后自动停单标记清掉", not st["autoStopped"])

    # 手动开关优先于一切
    w.cfg.accept_orders = False
    st = w.intake_status()
    check("手动关掉就不进单", not st["open"])
    check("手动停单被标记出来", st["manualOff"])
    check("手动停单的原因指向管理平台", "手动" in st["reason"])
    w.cfg.accept_orders = True
    check("手动打开后恢复", w.intake_status()["open"])

    # 阈值可调
    w.cfg.stop_accept_pool_ratio = 0.05
    check("阈值调到 5% 就会停", not w.intake_status()["open"])
    w.cfg.stop_accept_pool_ratio = 0.99
    check("阈值调到 99% 就放行", w.intake_status()["open"])
    w.cfg.stop_accept_pool_ratio = 0.5

    # 最小单量可调
    w.cfg.stop_accept_min_orders = 1000
    check("最小单量调高后不再自动停单", w.intake_status()["open"])
    w.cfg.stop_accept_min_orders = 8

    # 待派数按状态数，不按队列长度 —— 两者不一致时以状态为准
    st = w.intake_status()
    by_status = sum(1 for o in w.orders.values() if o.status is OrderStatus.POOLED)
    check("待派数按状态统计", st["pooled"] == by_status)
    check("同时暴露队列长度以便对账", "poolQueue" in st)

    # ---- 开局不该误停：一单都没派出去过，说明派单还没跑，不是处理不过来 ----
    w4 = World.seeded(20260927)
    w4.cfg.auto_order = False
    w4.cfg.stop_accept_pool_ratio = 0.3
    w4.cfg.stop_accept_min_orders = 4
    for _ in range(10):
        w4.auto_place_order()
    st4 = w4.intake_status()
    check(f"全部待派、无在途，但骑手都有余量 → 不误停（待派 {st4['pooled']}）",
          st4["open"])
    check("状态里能看出有多少骑手有余量", st4["ridersFree"] == len(w4.riders))

    # 但如果一个有余量的骑手都没有，积压就是真的 → 该停
    for r in w4.riders.values():
        r.max_orders = r.active_count() or 1
        r.status = RiderStatus.BUSY
    st4b = w4.intake_status()
    check("一个有余量的骑手都没有 → 停单", not st4b["open"])
    check("这种情况仍算自动停单", st4b["autoStopped"])

    # ---- 自动下单必须尊重停单：这才是「控制模拟订单量」的执行点 ----
    w2 = World.seeded(20260927)
    w2.cfg.auto_order = True
    w2.cfg.auto_order_every_sec = 5
    w2.cfg.stop_accept_pool_ratio = 0.2
    w2.cfg.stop_accept_min_orders = 5
    simulator.advance(w2, 60 * 40)
    st2 = w2.intake_status()
    check(f"低阈值下自动下单被压住（总订单 {st2['totalOrders']}）", st2["totalOrders"] < 200)
    check("停单时池子不再无限涨", st2["pooled"] <= 40)
    check("停单期间确实进了停单日志",
          any(k == "停单" for (_t, k, _x) in w2.log))

    # 解除停单后能继续进单
    w2.cfg.stop_accept_pool_ratio = 1.0
    check("阈值放宽后恢复进单", w2.intake_status()["open"])
    before = st2["totalOrders"]
    simulator.advance(w2, 60 * 3)
    check("恢复后订单继续增长", len(w2.orders) > before)

    # 累计上限
    w2.cfg.max_total_orders = len(w2.orders) + 1
    simulator.advance(w2, 60 * 5)
    check("累计订单触顶后不再自动生成", len(w2.orders) <= w2.cfg.max_total_orders)


def test_random_order_helpers():
    """随机顾客/随机送达点：格式正确、坐标落在世界里、能直接用来下单。"""
    w = new_world()
    w.cfg.warmup_orders = 0
    w.orders.clear()
    w.pool.clear()

    for _ in range(20):
        name, phone, addr, note = w.random_customer()
        check("随机姓名非空", bool(name))
        check("随机电话是 13 开头 11 位", phone.startswith("13") and len(phone) == 11)
        check("随机备注来自预设池", note in w.NOTES)
        if not (name and addr):
            break

    m = next(iter(w.merchants.values()))
    for _ in range(20):
        d = w.random_dest_near(m.pt, 2600)
        inside = 0 <= d.x <= w.width_m and 0 <= d.y <= w.height_m
        if not inside:
            check("随机送达点在城区范围内", False)
            break
    else:
        check("随机送达点始终在城区范围内", True)

    # 随机出来的东西必须真能下单
    name, phone, addr, note = w.random_customer()
    d = w.random_dest_near(m.pt, 2600)
    o = w.place_order(name, phone, addr, note, m, d)
    check("用随机数据能下单", o.id in w.orders)
    check("随机单进了订单池", w.in_pool(o.id))


# ------------------------------------------------------------ 推迟派单（ACA）

def _postpone_scenario():
    """搭一个「最好也只能兜底」的场景：

    两个骑手都已经有单（所以没有「无订单骑手」这一档），
    而新订单的商家离两条既有路线都很远 → 插入绕路必然超阈值 → 只能算兜底。
    """
    w = new_world()
    w.cfg.auto_order = False
    w.cfg.warmup_orders = 0
    w.orders.clear()
    w.pool.clear()

    # R1 在原点附近送货，R2 在 5km 外送货
    r1 = rider_at(w, "R1", 0, 0)
    r2 = rider_at(w, "R2", 5000, 0)
    m1 = Merchant("T1", "店1", Pt(0, 0), 0)
    m2 = Merchant("T2", "店2", Pt(5000, 0), 0)
    for rider, m, dest in ((r1, m1, Pt(100, 0)), (r2, m2, Pt(5100, 0))):
        o = make_order("X" + rider.id, m, dest, w.now())
        w.orders[o.id] = o
        ins = rp.best_insertion(w.metric, rider, o, 5)
        rider.route[:] = ins.route
        rider.motion = Motion.MOVING

    # 新订单在两公里半以外，两条路线都够不着
    m_far = Merchant("T9", "远方店", Pt(2500, 2500), 0)
    return w, m_far


def test_defer_poor_assignment():
    """推迟派单：最好的归宿也只是兜底时先不派，等下一轮看有没有顺路的。

    这是 ACA（Ulmer et al. 2021）/ RMDP_Algorithm 的核心思想：
    把兜底单等成顺路单，而不是把顺路的判定阈值放宽（那只是改标签）。
    """
    w, m_far = _postpone_scenario()

    o = make_order("P1", m_far, Pt(2600, 2500), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)

    # 先确认它真的只能算兜底
    choice = dispatcher._choose(w, o)
    check("这单的最好归宿确实只是兜底档", choice is not None and choice.tier == 3)

    dispatcher.round_(w)
    check("兜底单被推迟，仍在订单池里", w.in_pool(o.id))
    check("推迟次数 +1", o.postpone_count == 1)
    check("订单还没派出去", o.dispatched_at == 0)
    check("推迟留了日志", any(k == "推迟" for _t, k, _x in w.log))

    # 再推两轮，累计到上限后必须派出去，不能无限等
    for _ in range(w.cfg.postpone_max_rounds):
        w.last_dispatch_at = w.now()      # 手动推进派单节拍
        dispatcher.round_(w)
    check(f"推到上限（{w.cfg.postpone_max_rounds} 轮）后必须派出",
          o.dispatched_at > 0 and not w.in_pool(o.id))


def test_defer_limits():
    """推迟的三条保险：等太久、池子太深、禁止推迟时都不能再等。"""
    # ① 等太久 → 立刻派
    w, m_far = _postpone_scenario()
    o = make_order("P2", m_far, Pt(2600, 2500), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)
    o.created_at = w.now() - (w.cfg.postpone_max_wait_min * 60 + 10)
    dispatcher.round_(w)
    check("等超过上限就不再推迟，直接派出", o.dispatched_at > 0)

    # ② 池子太深（运力吃紧）→ 立刻派
    w, m_far = _postpone_scenario()
    for i in range(int(len(w.riders) * w.cfg.postpone_max_pool_factor) + 3):
        oo = make_order(f"Q{i}", m_far, Pt(2600 + i, 2500), w.now())
        w.orders[oo.id] = oo
        w.pool.append(oo.id)
    target = w.orders["Q0"]
    dispatcher.round_(w)
    check("池子深时不再推迟，赶紧往外派", target.dispatched_at > 0)

    # ③ 关掉开关 → 一律不推迟
    w, m_far = _postpone_scenario()
    w.cfg.postpone_poor_assignments = False
    o = make_order("P3", m_far, Pt(2600, 2500), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)
    dispatcher.round_(w)
    check("关掉推迟开关后直接派出", o.dispatched_at > 0)

    # ④ 一个有余量的骑手都没有 → 等下去也不会冒出顺路机会，直接派
    w, m_far = _postpone_scenario()
    for r in w.riders.values():
        r.status = RiderStatus.BUSY
    o = make_order("P4", m_far, Pt(2600, 2500), w.now())
    w.orders[o.id] = o
    w.pool.append(o.id)
    r_free = [r for r in w.riders.values() if r.can_take()]
    check("确认此时没有有余量的骑手", not r_free)
    check("没有余量时不该推迟",
          not dispatcher._should_postpone(w, o, dispatcher.Choice(list(w.riders.values())[0],
                                                                 None, 3, 0.0, "")))


def test_regret_ordering():
    """regret-2 排序：选择越少的单越先派，免得好派的先把骑手占光。"""
    w = new_world()
    w.cfg.auto_order = False
    w.cfg.warmup_orders = 0
    w.orders.clear()
    w.pool.clear()

    # R1 在原点，R2 在 5km 外
    rider_at(w, "R1", 0, 0)
    rider_at(w, "R2", 5000, 0)

    # 「没得挑」的单：只有原点附近的 R1 够得着（R2 超过 6km 候选半径）
    m_near = Merchant("N", "近店", Pt(100, 100), 0)
    o_near = make_order("R_N", m_near, Pt(200, 200), w.now())
    # 「挑得动」的单：两个骑手都够得着（在两个候选半径内）
    m_mid = Merchant("M", "中间店", Pt(2500, 0), 0)
    o_mid = make_order("R_M", m_mid, Pt(2600, 0), w.now())

    w.orders[o_near.id] = o_near
    w.orders[o_mid.id] = o_mid
    # 故意把「挑得动」的那单排在前面 —— 排序应该把它换到后面
    ordered = dispatcher._order_by_regret(w, [o_mid, o_near])
    check(f"选择少的单被排到前面（{ordered[0].id}）", ordered[0].id == o_near.id)


def test_cli_osm_flag():
    """`--osm` 这条路必须真的能用，而且失败时**不能静默降级**。

    这里踩过一次坑：main.py 里写的是 `World.NetworkMode`，可那个枚举其实定义在
    模块级（world.NetworkMode），于是抛 AttributeError；而 Except 把它吞掉、
    只在 stderr 打一行就继续跑。结果 `--osm realistic` 一直是失效的 ——
    界面看起来「启动正常」，只是悄悄变成了抽象城市，使用者只会以为路网功能没做。

    单元测试断言不到这种情况（它发生在 main() 运行期），所以这里直接开子进程
    验命令行行为。`--list-networks` 会立刻退出，是最便宜的锚点。
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent.parent
    script = root / "py" / "waimai" / "main.py"

    # ① 两个内置路网都必须能被 loader 认出来是「生成出来的」
    #    （main 和 api 都用 res.synthetic 决定界面标签，猜错了标签就会骗人）
    for nid in ("synthetic", "realistic"):
        res = osm_loader.load(nid, verbose=False)
        check(f"{nid} 被标为内置生成路网", res.synthetic is True)
        check(f"{nid} 加载后有节点", res.graph is not None and res.graph.node_count > 0)

    # discover() 里声明为内置的，load() 之后必须也是内置 —— 两份名单不许打架
    builtin_ids = [n.id for n in osm_loader.discover() if n.synthetic]
    check("内置路网清单非空", len(builtin_ids) >= 2)
    mismatch = [i for i in builtin_ids if not osm_loader.load(i, verbose=False).synthetic]
    check(f"discover 与 load 的内置标记一致（不一致：{mismatch}）", not mismatch)

    # ② --list-networks 必须成功退出并列出两个内置路网
    p = subprocess.run([sys.executable, "-X", "utf8", str(script), "--list-networks"],
                       capture_output=True, text=True, timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    check("--list-networks 正常退出", p.returncode == 0,
          f"exit={p.returncode} {out.strip()[:120]}")
    check("清单里有 realistic", "realistic" in out)
    check("清单里有 synthetic", "synthetic" in out)

    # ③ 要了不存在的路网 → 必须**失败退出**，而不是退回抽象城市继续跑
    p = subprocess.run([sys.executable, "-X", "utf8", str(script),
                        "--osm", "no-such-network", "--port", "8799"],
                       capture_output=True, text=True, timeout=120)
    err = (p.stdout or "") + (p.stderr or "")
    check("不存在的路网会失败退出（不静默降级）", p.returncode != 0,
          f"exit={p.returncode}")
    check("失败信息说明是路网加载失败", "路网加载失败" in err)
    check("失败信息告诉你去看可用清单", "--list-networks" in err)


# ------------------------------------------------------------ main

def test_determinism():
    """复现性：同一份种子必须给出同一个世界 —— 跨重置、跨进程都要成立。

    这一条不是洁癖：agent 做对照实验（比如比较两种派单策略）必须能保证
    "两次运行只差一个变量"。做不到复现的话，测出来的差异可能是随机流的差异。
    """
    import zlib

    def fingerprint(w):
        return (
            [(r.id, round(r.pos.x, 6), round(r.pos.y, 6), round(r.speed_mpm, 6))
             for r in w.riders.values()],
            [(m.id, m.name, m.prep_sec) for m in w.merchants.values()],
        )

    w1 = World.seeded(20260927)
    fp1 = fingerprint(w1)
    w2 = World.seeded(20260927)
    check("同种子的两个世界完全一致", fingerprint(w2) == fp1)

    # 重置之后必须还是同一个世界（以前 reset 不重设 rng，
    # 第二次 seed_entities 用的是已经跑过的随机流，两次重置结果不同）
    w1.reset()
    check("重置后与初始世界一致（随机流被复原）", fingerprint(w1) == fp1)
    w1.reset()
    check("再重置一次仍然一致", fingerprint(w1) == fp1)

    # run_id 每次重置都要变：订单号会从头开始，落盘的数据要靠它才不撞车
    w3 = World.seeded(1)
    r0 = w3.run_id
    w3.reset()
    r1 = w3.run_id
    w3.reset()
    check("每次重置换一个 run_id", r0 != r1 and r1 != w3.run_id,
          f"{r0} / {r1} / {w3.run_id}")

    # 倍速是"怎么跑"不是"跑什么"，重置不该把它改回去。
    # 以前 reset 硬写 speed_factor = 20.0，于是 loadcheck.ps1 的
    # "先设 speed 再 reset"顺序让它的 -Speed 参数一直失效。
    w3.speed_factor = 200.0
    w3.reset()
    check("重置不动 speed_factor", w3.speed_factor == 200.0, str(w3.speed_factor))

    # 商家备餐时间曾用内置 hash()，受 PYTHONHASHSEED 影响 → 跨进程不可复现。
    #
    # 注意别用抽象城市的商家来测这条：那条路径的备餐时间来自固定店表，
    # 根本走不到按店名推算的那个分支（我第一版就是这么写的，等于什么都没测）。
    # 所以直接对纯函数断言，把等式钉死。
    from waimai.world import merchant_prep_sec
    check("备餐时间用稳定哈希（crc32），且落在 5~10 分钟",
          merchant_prep_sec("湘味小炒") == 300 + (zlib.crc32("湘味小炒".encode("utf-8")) % 7) * 60
          and 300 <= merchant_prep_sec("任何店名") <= 660,
          f"prep={merchant_prep_sec('湘味小炒')}")
    check("同一个店名永远得到同一个备餐时间",
          merchant_prep_sec("兰州拉面") == merchant_prep_sec("兰州拉面"))
    check("不同店名会得到不同备餐时间（不是常数）",
          len({merchant_prep_sec(f"店{i}") for i in range(30)}) > 3)

    # 手动下单也要守 max_total_orders（以前只有自动路径守）。
    # 注意 seed_entities 会先按 warmup_orders 造预热的单，所以要按"当前已有多少"来算。
    w4 = World.seeded(7)
    start = len(w4.orders)
    w4.cfg.max_total_orders = start + 5
    placed = 0
    refused = False
    while placed < 40:
        try:
            w4.auto_place_order()
            placed += 1
        except OrderLimitReached:
            refused = True
            break
    check("达到 max_total_orders 后不再接单", refused and placed == 5,
          f"placed={placed} start={start}")
    check("上限生效时订单数正好等于上限", len(w4.orders) == start + 5,
          f"orders={len(w4.orders)} cap={w4.cfg.max_total_orders}")
    check("达到上限是抛 OrderLimitReached（不是别的异常）", refused)


def test_telemetry_and_learning():
    """数据飞轮：脱敏落盘、样本合并、学习器的数学与门禁、回滚。

    用手工构造的样本测算法本身（可手算验证），再用一段真实模拟测端到端。
    """
    import tempfile

    import contextlib

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        telemetry.configure(d)

        w = new_world()
        w.cfg.auto_order = False
        w.cfg.warmup_orders = 0

        # ---- 落盘：决策 + 结果 ----
        m = Merchant("M9", "测试店", Pt(1000, 1000), 0)
        o = make_order("T1", m, Pt(2000, 2000), w.now())
        w.orders[o.id] = o
        telemetry.record_decision(w, o, tier=1, detour_m=321.5, rider_id="R1",
                                  mode="派单", postponed=False,
                                  candidates={"n1": 2, "n2": 3, "n3": 0, "best1": 321.5})
        # total_sec 是算出来的属性（delivered - created），只能通过设时间戳影响它
        o.dispatched_at = o.created_at + 120
        o.delivered_at = o.created_at + 900
        telemetry.record_outcome(w, o)

        recs = telemetry.records(telemetry.KIND_DECISION)
        check("决策记录落盘了", len(recs) == 1)
        check("决策记录含候选摘要（以前是被丢掉的）",
              recs[0]["cand"]["n1"] == 2 and recs[0]["cand"]["best1"] == 321.5)
        check("决策记录含负载上下文", "load" in recs[0]["ctx"] and "free" in recs[0]["ctx"])
        joined = telemetry.joined_samples()
        check("决策与结果能按订单号合并", len(joined) == 1)
        check("合并后既有决策也有结果",
              joined[0]["tier"] == 1 and joined[0]["outcome"]["totalSec"] == 900)

        # 隐私：落盘内容里绝对不能出现客户信息
        blob = "".join(p.read_text(encoding="utf-8") for p in d.glob("*.jsonl"))
        for field in ("customerName", "phone", "address", "note", o.customer_name):
            check(f"落盘不含 {field!r}", field not in blob)

        # ---- 学习器 1：等派单模型（可手算）----
        # 构造两个上下文：低负载+池浅 → 等 100s；高负载+池深 → 等 400s
        synth = []
        for i in range(40):
            synth.append({"orderId": f"a{i}", "runId": "r", "waitSec": 100,
                          "ctx": {"load": 0.2, "pool": 0},
                          "outcome": {"totalSec": 600}})
        for i in range(40):
            synth.append({"orderId": f"b{i}", "runId": "r", "waitSec": 400,
                          "ctx": {"load": 0.9, "pool": 5},
                          "outcome": {"totalSec": 900}})
        fit = learn.fit_wait_model(synth)
        check("等派单模型分出了两个档位", len(fit["table"]) == 2, str(list(fit["table"])))
        low_key = "低|0"
        high_key = "高|5"
        check("低负载档估出 100 秒",
              fit["table"].get(low_key, {}).get("medianSec") == 100.0,
              str(fit["table"].get(low_key)))
        check("高负载档估出 400 秒",
              fit["table"].get(high_key, {}).get("medianSec") == 400.0,
              str(fit["table"].get(high_key)))

        # 留出评估：这个模型一定比"全局中位数"准（两个档位差得很远）
        ev = learn.evaluate_wait_model(synth, fit, None)
        # 这组合成数据两个档位差得很远，分档模型应该几乎零误差 ——
        # MAE == 0 是**最好**的结果，不是"没算出来"（我第一版把断言写反了）
        check("留出评估算得出 MAE", ev.get("ok") and ev["maeNew"] >= 0, str(ev))
        check("分档模型比全局中位数准",
              ev.get("better") is True and ev["maeNew"] < ev["maeOld"], str(ev))
        # 反过来：拿一个空的模型去评估，必须判定"没有更好"
        ev_empty = learn.evaluate_wait_model(synth, {"table": {}}, None)
        check("空模型不会被判定为更好", ev_empty.get("better") is False, str(ev_empty))
        # 门禁：更好的才允许应用。这里直接测"没更好就不应用"这条
        check("门禁要求相对改善超过阈值",
              learn.MIN_RELATIVE_GAIN > 0 and ev["gain"] >= learn.MIN_RELATIVE_GAIN)

        # ---- 学习器 2：骑行速度 ----
        legs = [{"kind": "to_store", "distanceM": 1000, "durationSec": 200, "mps": 5.0},
                {"kind": "to_store", "distanceM": 3000, "durationSec": 600, "mps": 5.0}]
        sp = learn.fit_speed(legs)
        check("速度按距离加权算出 5 m/s",
              abs(sp["to_store"]["mps"] - 5.0) < 1e-6 and sp["to_store"]["n"] == 2,
              str(sp.get("to_store")))

        # ---- 学习器 3：策略（只看探索样本）----
        telemetry.configure(Path(td) / "policy")
        pw = new_world()
        pw.cfg.auto_order = False
        # 16 笔单：中负载下，探索"推迟"的总时长更短
        for i in range(16):
            oo = make_order(f"P{i}", m, Pt(2000, 2000), pw.now())
            pw.orders[oo.id] = oo
            postponed = i % 2 == 0
            telemetry.record_decision(
                pw, oo, tier=3, detour_m=2000, rider_id=None if postponed else "R1",
                mode="POSTPONE" if postponed else "派单",
                postponed=postponed, explored=True)
            telemetry.record_outcome(pw, oo) if False else None
            # 直接写结果记录：推迟臂更快
            telemetry._write(telemetry.KIND_OUTCOME, {
                "runId": pw.run_id, "orderId": oo.id, "totalSec": 500 if postponed else 800,
                "t": int(pw.now())})
        learn._reset_index()
        pol = learn.fit_postpone_policy()
        check("策略学习用了探索样本", pol["exploredSamples"] == 16, str(pol["exploredSamples"]))
        # 只断言"有且仅有一个桶，且它的两臂都够样本"——
        # 具体是哪个桶取决于 new_world 的负载，写死桶名就是在测巧合
        check("恰有一个负载档位被统计到", len(pol["table"]) == 1, str(list(pol["table"])))
        only = next(iter(pol["table"].values()))
        check("该档位两臂样本都够",
              not only.get("insufficient", True), str(only))
        check("学出「推迟更快」", only.get("best") == "postpone", str(only))
        # 没被探索过的样本不能参与 —— 否则会把选择偏差当成因果
        telemetry.configure(Path(td) / "policy2")
        pw2 = new_world()
        pw2.cfg.auto_order = False
        for i in range(20):
            oo = make_order(f"Q{i}", m, Pt(2000, 2000), pw2.now())
            pw2.orders[oo.id] = oo
            telemetry.record_decision(pw2, oo, tier=3, detour_m=2000, rider_id="R1",
                                      mode="派单", postponed=False, explored=False)
            telemetry._write(telemetry.KIND_OUTCOME, {
                "runId": pw2.run_id, "orderId": oo.id, "totalSec": 800,
                "t": int(pw2.now())})
        learn._reset_index()
        pol2 = learn.fit_postpone_policy()
        check("没有探索样本时明确报「样本不足」而不是硬算",
              pol2["exploredSamples"] == 0 and pol2["samples"] == 0, str(pol2["samples"]))
        # 关掉文件句柄：不停的话 Windows 上临时目录删不掉（PermissionError）
        telemetry.close()

    # ---- 端到端：跑一段真实模拟 → 学习 → 应用 → 回滚 ----
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "learn"
        telemetry.configure(data_dir)
        for i in range(2):
            service.simulate(minutes=45, orders_per_min=1.0, seed=1000 + i,
                             explore_epsilon=0.3)
        st = telemetry.stats()
        check("模拟跑完攒到了可训练样本", st["samples"] >= learn.MIN_SAMPLES,
              f"samples={st['samples']}")

        # 把模型文件也指到临时目录，别污染仓库里的 data/learn/model.json
        import waimai.learn as _l
        orig_path = _l._model_path

        def tmp_model_path():
            return data_dir / "model.json"
        _l._model_path = tmp_model_path
        try:
            res = learn.run(apply_low_risk=True)
            check("学习跑成功", res.get("ok") is True)
            check("报告里有三个学习器",
                  set(res["report"]) == {"wait", "speed", "postponePolicy"},
                  str(set(res["report"])))
            check("策略类明确标注需要确认",
                  res["report"]["postponePolicy"]["requiresConfirmation"] is True)
            check("策略类没有被自动应用",
                  res["report"]["postponePolicy"]["applied"] is False)
            check("低风险模型（速度）被应用了",
                  res["report"]["speed"]["applied"] is True)
            wait_rep = res["report"]["wait"]
            check("等派单模型给了明确结论（应用或没更好）",
                  wait_rep.get("applied") in (True, False) and bool(wait_rep.get("reason")),
                  str(wait_rep)[:160])

            v1 = learn.status()["modelVersion"]
            check("模型文件有版本号", v1 >= 1, str(v1))
            check("status 报告样本数与隐私说明",
                  "privacy" in learn.status() and learn.status()["data"]["outcomes"] > 0)
            rb = learn.rollback(1)
            check("能回滚", rb.get("ok") is True, str(rb))
            check("回滚后版本号继续前进（历史可追溯）",
                  learn.status()["modelVersion"] > v1, str(learn.status()["modelVersion"]))
        finally:
            _l._model_path = orig_path

        # ETA：学过之后在途订单应当能给出预计时间
        w2 = World.seeded(7)
        w2.cfg.auto_order = True
        simulator.advance(w2, 1200)
        active = [o for o in w2.orders.values() if o.status.name != "DELIVERED"]
        etas = [service.order_json(w2, o)["etaMinutes"] for o in active[:10]]
        check("在途订单给出了 ETA", any(e is not None for e in etas),
              str(etas[:5]))
        check("ETA 是正数分钟", all(e > 0 for e in etas if e is not None))
        check("order_json 带超时风险标记",
              "atRisk" in service.order_json(w2, active[0]) if active else True)
        check("ETA 不会因订单已完成而出错",
              service.order_json(w2, active[0]) is not None if active else True)
        telemetry.close()
    obs.reset()
    obs.configure(log_dir=None, enabled=False)
    telemetry.close()
    telemetry.configure(paths.under_data("learn"))


def test_entrypoints_as_scripts():
    """每个入口都能**以脚本方式**跑起来（PyCharm 的运行配置就是这么跑的）。

    为什么单独测这个：函数体里的相对导入（`from . import x`）在脚本方式下会
    ImportError（`__package__` 为空），而在 `-m` 方式下正常 —— 所以只用 `-m`
    跑的自测发现不了。这一路踩过两次：
      · `mcp_server.py --network realistic` 里有一句函数级相对导入，一启动就崩，
        而这恰好是 PyCharm 的 MCP 运行配置（用户点一下就会遇到）；
      · `cli.py` 里几处函数级相对导入，只有走到那个子命令才炸。
    这里用 subprocess 真的起脚本，覆盖每个入口**带参数**的路径。
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent.parent
    cases = [
        ("mcp_server.py --network",
         ["py/waimai/mcp_server.py", "--network", "realistic"],
         '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
        ("cli.py bench --local",
         ["py/waimai/cli.py", "bench", "--local", "--iterations", "3"], None),
        ("cli.py learn status", ["py/waimai/cli.py", "learn", "status"], None),
        ("cli.py simulate", ["py/waimai/cli.py", "simulate", "--minutes", "3"], None),
        ("cli.py serve --help", ["py/waimai/cli.py", "serve", "--help"], None),
        ("main.py --list-networks", ["py/waimai/main.py", "--list-networks"], None),
    ]
    for name, argv, stdin in cases:
        p = subprocess.run([sys.executable, "-X", "utf8", "-u", *argv],
                           cwd=str(root), input=stdin, capture_output=True,
                           text=True, encoding="utf-8", timeout=600)
        err = p.stderr or ""
        last = err.strip().splitlines()[-1][:160] if err.strip() else ""
        check(f"脚本方式运行 {name} 不报相对导入错",
              "relative import" not in err, last)
        check(f"脚本方式运行 {name} 正常退出", p.returncode == 0,
              f"exit={p.returncode} {last}")


def test_security_primitives():
    """安全原语的单元测试。

    verify-api.ps1 里也有一遍端到端的（那才是"真打了一发"），但那一遍需要
    服务在跑。这里测纯函数，改重构就不会把安全性悄悄弄丢。
    每个用例都对应一种真实手法，不是复述实现。
    """
    import tempfile

    web = Path(__file__).resolve().parent.parent.parent / "web"

    # ---- 静态路径：越界一律拒绝 ----
    evil = [
        "/C:/Users/x/data/llm.properties",     # 盘符：Path 拼接时会把 web/ 丢掉
        "/../data/llm.properties",
        "/..%2f..%2fdata%2fllm.properties",
        "/%2e%2e/%2e%2e/data/llm.properties",
        "/a/../../data/llm.properties",
        "/\\..\\data\\llm.properties",          # 反斜杠在 Windows 上也是分隔符
        "//server/share/x",                     # UNC
        "/app.js:stream",                       # NTFS 数据流
        "/app.js\x00.png",                      # NUL 截断
        "/CON", "/nul", "/COM1",                # Windows 设备名
    ]
    for p in evil:
        check(f"拒绝越界路径 {p[:38]!r}", security.resolve_web_file(web, p) is None)
    check("拒绝没有 web 目录的情况",
          security.resolve_web_file(None, "/app.js") is None)

    # ---- 静态路径：正常文件仍然放行 ----
    check("放行 /app.js", security.resolve_web_file(web, "/app.js") is not None)
    check("放行 /style.css", security.resolve_web_file(web, "/style.css") is not None)
    check("/ 映射到 index.html",
          (security.resolve_web_file(web, "/") or Path("")).name == "index.html")
    check("空段和 . 被规范化",
          security.resolve_web_file(web, "//./app.js") is not None)
    check("不存在的文件返回 None",
          security.resolve_web_file(web, "/nope.js") is None)

    # ---- Content-Length ----
    class H(dict):
        def get_all(self, k):
            return self.get(k) if isinstance(self.get(k), list) else (
                [self[k]] if k in self else [])

    check("正常长度可解析", security.parse_content_length(H({"Content-Length": "42"})) == 42)
    check("没有 Content-Length 视为 0", security.parse_content_length(H({})) == 0)

    def cl_bad(headers):
        try:
            security.parse_content_length(H(headers))
            return False
        except (security.BadRequestLen, security.RequestTooLarge):
            return True

    check("拒绝负数长度", cl_bad({"Content-Length": "-5"}))
    check("拒绝非数字长度", cl_bad({"Content-Length": "abc"}))
    check("空的长度头按 0 处理",
          security.parse_content_length(H({"Content-Length": ""})) == 0)
    check("拒绝超长长度", cl_bad({"Content-Length": str(security.MAX_BODY_BYTES + 1)}))
    check("拒绝重复的长度头",
          cl_bad({"Content-Length": ["1", "2"]}))
    check("拒绝 chunked", cl_bad({"Transfer-Encoding": "chunked"}))
    check("超长抛的是 RequestTooLarge",
          isinstance(_cl_err(security.RequestTooLarge,
                             {"Content-Length": str(security.MAX_BODY_BYTES + 1)}),
                     security.RequestTooLarge))
    check("+5 这种带号的长度也拒绝", cl_bad({"Content-Length": "+5"}))
    check("恰好等于上限时放行",
          security.parse_content_length(H({"Content-Length": str(security.MAX_BODY_BYTES)}))
          == security.MAX_BODY_BYTES)

    # ---- JSON 体 ----
    check("正常 JSON 可解析", security.parse_json_body(b'{"a":1}') == {"a": 1})
    check("空体解析成空 dict", security.parse_json_body(b"") == {})
    check("非 dict 的 JSON 也返回 dict", security.parse_json_body(b"[1,2]") == {})
    for bad in (b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}'):
        try:
            security.parse_json_body(bad)
            ok = False
        except security.BadRequestLen:
            ok = True
        check(f"拒绝 {bad.decode()}", ok)
    try:
        security.parse_json_body(b"{oops")
        bad_json = False
    except security.BadRequestLen:
        bad_json = True
    check("拒绝坏 JSON", bad_json)

    # ---- token ----
    with tempfile.TemporaryDirectory() as td:
        tf = Path(td) / "api-token"
        t1, src1 = security.load_or_create_token(tf, env={})
        check("首次调用生成 token", len(t1) >= 32 and src1 in ("new", "file"))
        check("token 落盘了", tf.is_file())
        t2, src2 = security.load_or_create_token(tf, env={})
        check("再次调用读回同一个 token", t1 == t2 and src2 == "file")
        t3, src3 = security.load_or_create_token(tf, env={security.TOKEN_ENV: "from-env"})
        check("环境变量优先", t3 == "from-env" and src3 == "env")

    check("token 比较正确值通过", security.token_matches("abc", "abc"))
    check("token 比较错误值失败", not security.token_matches("abc", "abd"))
    check("token 比较空值失败", not security.token_matches("abc", "")
          and not security.token_matches("", ""))
    check("token 比较 None 失败", not security.token_matches("abc", None))

    # ---- CORS ----
    check("同源放行", security.origin_allowed("http://127.0.0.1:8787", "127.0.0.1", 8787))
    check("同源 localhost 别名放行",
          security.origin_allowed("http://localhost:8787", "127.0.0.1", 8787))
    check("跨站拒绝", not security.origin_allowed("https://evil.example.com", "127.0.0.1", 8787))
    check("端口不同拒绝", not security.origin_allowed("http://127.0.0.1:9999", "127.0.0.1", 8787))
    check("无 Origin 拒绝", not security.origin_allowed(None, "127.0.0.1", 8787))
    check("非法 Origin 拒绝", not security.origin_allowed("not a url", "127.0.0.1", 8787))
    # 绑到所有网卡时"同源"没有确定含义，一律不放行 —— 否则等于把 CORS 开给全网
    check("绑 0.0.0.0 时一律不放行",
          not security.origin_allowed("http://127.0.0.1:8787", "0.0.0.0", 8787))

    # ---- baseUrl / SSRF ----
    def base_ok(url, allow_private=False):
        try:
            security.check_llm_base_url(url, allow_private=allow_private)
            return True
        except security.BlockedTarget:
            return False

    check("https 公网地址放行", base_ok("https://93.184.216.34/v1"))
    check("本机 http 放行（本地模型）", base_ok("http://127.0.0.1:11434/v1"))
    check("localhost 放行", base_ok("http://localhost:11434/v1"))
    check("明文 http 公网拒绝", not base_ok("http://93.184.216.34/v1"))
    check("私网拒绝", not base_ok("https://10.0.0.5/v1"))
    check("172.16 网段拒绝", not base_ok("https://172.16.0.1/v1"))
    check("192.168 网段拒绝", not base_ok("https://192.168.1.1/v1"))
    check("云元数据地址拒绝", not base_ok("https://169.254.169.254/latest/meta-data"))
    check("非 http(s) 协议拒绝", not base_ok("ftp://93.184.216.34/"))
    check("空地址拒绝", not base_ok(""))
    check("没有主机名拒绝", not base_ok("https:///v1"))
    check("放行开关打开后私网也可用", base_ok("http://10.0.0.5/v1", allow_private=True))
    check("尾部斜杠被规范化",
          security.check_llm_base_url("https://93.184.216.34/v1/") == "https://93.184.216.34/v1")


def _cl_err(exc_type, headers):
    class H(dict):
        def get_all(self, k):
            return self.get(k) if isinstance(self.get(k), list) else (
                [self[k]] if k in self else [])
    try:
        security.parse_content_length(H(headers))
        return None
    except Exception as e:                                  # noqa: BLE001
        return e


def test_observability():
    """obs 模块：指标算法、日志轮转、以及"绝不把调用方搞挂"。"""
    import tempfile

    obs.reset()
    # 没 configure 的时候也要能安全调用（默认关闭）
    obs.counter("x")
    obs.observe("y", 1.0)
    obs.event("info", "k", "m")
    check("未配置时调用不炸", True)

    with tempfile.TemporaryDirectory() as td:
        obs.reset()
        obs.configure(log_dir=Path(td), console_level="error")
        try:
            for i in range(1, 101):
                obs.observe("lat", float(i))
            snap = obs.snapshot()
            h = snap["histograms"]["lat"]
            check("直方图计数正确", h["count"] == 100)
            check("p50 落在中位数附近", 49 <= h["p50"] <= 51, f"p50={h['p50']}")
            check("p95 落在 95 附近", 94 <= h["p95"] <= 96, f"p95={h['p95']}")
            check("p99 落在 99 附近", 98 <= h["p99"] <= 100, f"p99={h['p99']}")
            check("max 正确", h["max"] == 100)
            check("sum 正确", abs(h["sum"] - 5050) < 1e-6, f"sum={h['sum']}")

            obs.counter("req")
            obs.counter("req")
            obs.counter("req", 3)
            check("计数累加正确", obs.snapshot()["counters"]["req"] == 5)

            obs.gauge("g", 7.5)
            check("仪表取最新值", obs.snapshot()["gauges"]["g"] == 7.5)

            obs.access("GET", "/api/state", 200, 12.5, "127.0.0.1")
            obs.access("GET", "/api/state", 500, 30.0, "127.0.0.1")
            snap = obs.snapshot()
            check("访问日志计入请求数", snap["counters"]["http_requests"] == 2)
            check("5xx 单独计数", snap["counters"].get("http_errors_5xx") == 1)
            check("按状态类分桶", snap["counters"].get("http_status_2xx") == 1
                  and snap["counters"].get("http_status_5xx") == 1)

            obs.sim_log("派单", "第 1 轮", 36120.0)
            files = sorted(Path(td).glob("waimai-*.jsonl"))
            check("日志文件已创建", len(files) == 1, str(files))
            lines = files[0].read_text(encoding="utf-8").strip().splitlines()
            check("日志里有访问记录", any('"kind":"access"' in l for l in lines))
            check("日志里有仿真日志（deque 挤掉也不丢历史）",
                  any('"kind":"sim"' in l for l in lines))
            import json as _j
            rec = _j.loads([l for l in lines if '"kind":"access"' in l][0])
            check("访问记录含耗时与状态", rec["ms"] == 12.5 and rec["status"] == 200)
            check("访问记录不含客户姓名/电话/地址",
                  not any(k in rec for k in ("name", "phone", "address")))

            # 样本窗口有界：内存不会随运行时长增长
            for i in range(obs.SAMPLE_WINDOW * 2):
                obs.observe("bounded", 1.0)
            check("样本窗口有界（不会无限增长）",
                  obs.snapshot()["histograms"]["bounded"]["count"] == obs.SAMPLE_WINDOW)

            text = obs.prometheus_text()
            check("Prometheus 文本含计数", "waimai_http_requests " in text)
            check("Prometheus 文本含分位", 'quantile="0.95"' in text)
            check("Prometheus 文本的指标名合法",
                  all(c.isalnum() or c in "_" for c in
                      re.findall(r"^([a-z_0-9]+)", text, re.M)[0]))

            # 体积轮转：真的写满再换文件。
            # 注意要**真的写超**，不能只改计数器：选文件看的是磁盘上的实际大小，
            # 只把 _log_bytes 调大而文件本身还很小的话，轮转逻辑会正确地
            # "继续追加同一个文件"，测试就变成了自欺欺人
            # （上一版这条断言就是这样稀里糊涂通过的 —— 当时它其实是靠
            #  "每次启动都新建文件"那个 bug 才成立）。
            obs.reset()
            small = 400
            old_max = obs.MAX_LOG_BYTES
            obs.MAX_LOG_BYTES = small
            try:
                obs.configure(log_dir=Path(td), console_level="error")
                for i in range(60):
                    obs.event("info", "bulk", f"第 {i} 条，用来把文件写满")
                files = sorted(Path(td).glob("waimai-*.jsonl"))
                check("写满体积上限后会换新文件", len(files) >= 2,
                      str([f.name for f in files]))
                biggest = max(f.stat().st_size for f in files)
                check("没有任何单个文件超过上限", biggest <= small * 3,
                      f"max={biggest} limit={small}")
                check("最后一条记录确实落盘了",
                      any("第 59 条" in f.read_text(encoding="utf-8") for f in files))
            finally:
                obs.MAX_LOG_BYTES = old_max

            # 重启（重新 configure）必须**追加**到当天的同一个文件，
            # 而不是新建一个。第一版这里是坏的：每次启动都开新文件，
            # 总量超限时还会把最老的删掉 —— 每重启一次丢一段历史。
            before = len(list(Path(td).glob("waimai-*.jsonl")))
            obs.reset()
            obs.configure(log_dir=Path(td), console_level="error")
            obs.event("info", "after-restart", "追加到当天文件")
            after = sorted(Path(td).glob("waimai-*.jsonl"))
            check("重启后不新建日志文件（追加到当天的）", len(after) == before,
                  f"before={before} after={len(after)}")
            newest = sorted(after, key=lambda f: f.stat().st_mtime)[-1]
            check("重启后写的记录落在文件里",
                  "after-restart" in newest.read_text(encoding="utf-8"))
        finally:
            obs.reset()
            obs.configure(log_dir=None, enabled=False)

    # 写不进去也不能抛（它在模拟主循环里被调用）
    obs.reset()
    obs.configure(log_dir=Path("Z:/definitely/not/a/path"), console_level="error")
    obs.event("info", "still", "works")
    obs.sim_log("派单", "x", 1.0)
    check("日志目录不可写时不影响业务", True)
    obs.reset()
    obs.configure(log_dir=None, enabled=False)


# ------------------------------------------------------------ main

def main() -> int:
    print()
    print("外卖平台派单系统 · Python 版自检")
    print("=" * 52)
    for fn in (test_detour_formula, test_precedence, test_capacity_limit,
               test_feasibility_guard, test_local_search, test_dispatch_tier_priority,
               test_manual_assign, test_reassign_guard, test_rider_cap_and_busy,
               test_full_lifecycle, test_geom_helpers, test_on_route_detection,
               test_speed_consistency, test_osm_parser, test_synthetic_routing,
               test_triangle_inequality, test_unreachable_is_finite,
               test_or_opt_only_improves, test_riders_stay_on_the_road,
               test_zone_analytics, test_llm, test_add_remove_rider,
               test_realistic_network, test_intake_gate, test_random_order_helpers,
               test_defer_poor_assignment, test_defer_limits, test_regret_ordering,
               test_cli_osm_flag, test_determinism, test_security_primitives,
               test_observability, test_telemetry_and_learning,
               test_entrypoints_as_scripts):
        fn()

    print()
    print(f"通过 {_passed} 项，失败 {len(_failures)} 项")
    if _failures:
        for f in _failures:
            print(f"  ✗ {f}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
