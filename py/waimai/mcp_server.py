"""MCP 服务：把派单系统暴露成工具，供智能体调用（stdio + JSON-RPC 2.0）。

    python -m waimai mcp                 # 进程内沙箱（默认）
    python -m waimai mcp --url http://127.0.0.1:8787/   # 接管一个正在跑的实例

零第三方依赖：MCP 的 stdio 传输就是"一行一个 JSON-RPC 消息"，
用标准库的 json + sys.stdin/stdout 就能实现，不需要装 mcp 那个包。

**两种模式，用途不同：**

· **进程内沙箱（默认）** —— 自己持有一个 World，**不起线程、不跑实时时钟**。
  时间只在你调用 `sim_advance` 时前进，`run_experiment` 用固定步长推进。
  好处是完全确定：同种子同参数必然复现，所以"改一个参数、跑两次、比指标"
  这种对照实验是可信的。agent 的大多数工作应该在这里做。

· **接管在跑的实例（`--url`）** —— 通过 HTTP 驱动 GUI 那个进程，
  用来观察/干预真实运行中的系统（下单、改参数、看实时指标）。
  它读的是别人的状态，所以复现性由那个进程决定，不由这里保证。

**stdout 只能出现协议消息。** 这一点必须靠代码保证而不是靠自觉：
所有工具执行期间 `sys.stdout` 被换成 stderr，真正的通道留给协议本身。
否则任何一个 `print`（比如加载路网时的进度输出）都会插进 JSON-RPC 流里，
对面就会看到一条解析不了的"消息"，表现为莫名其妙的协议错误。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from waimai import dispatcher, learn, obs, osm_loader, paths, security, service
    from waimai.cli import Client
    from waimai.simulator import advance as sim_advance
    from waimai.world import NetworkMode, World
else:
    from . import dispatcher, learn, obs, osm_loader, paths, security, service
    from .cli import Client
    from .simulator import advance as sim_advance
    from .world import NetworkMode, World

# 我们支持的协议版本。客户端报的版本不在这个列表里时，回我们自己的版本，
# 由客户端决定是否继续（规范允许这样协商）。
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

SERVER_NAME = "waimai-dispatch"
SERVER_VERSION = "1.0.0"


# ------------------------------------------------------------ 工具定义

def _schema(props: Dict[str, dict], required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}


TOOLS: List[dict] = [
    {
        "name": "sim_status",
        "description": "看当前世界的时钟、规模、进单状态。只读，是了解现状的第一步。",
        "inputSchema": _schema({}),
    },
    {
        "name": "get_stats",
        "description": "取关键指标：准时率、顺路/无单/兜底占比、平均时长、骑手负载、推迟次数。"
                       "不带订单明细，通常比 get_state 更合适。",
        "inputSchema": _schema({}),
    },
    {
        "name": "get_state",
        "description": "取完整快照（含商家、骑手、订单、日志）。数据量大，"
                       "orderLimit 可以限制返回的订单条数。",
        "inputSchema": _schema({"orderLimit": INT, "logLimit": INT}),
    },
    {
        "name": "get_zones",
        "description": "区域研判：按网格给出各区供需与建议（需要增派多少人、谁可以轮休）。",
        "inputSchema": _schema({"windowMin": INT, "cells": INT}),
    },
    {
        "name": "list_riders",
        "description": "列骑手：状态、手上单数、上限、位置、位置里程。",
        "inputSchema": _schema({}),
    },
    {
        "name": "list_orders",
        "description": "列订单，可按状态过滤（POOLED/ASSIGNED/PICKED_UP/DELIVERED）。",
        "inputSchema": _schema({"status": STR, "limit": INT}),
    },
    {
        "name": "explain_dispatch",
        "description": "解释一笔订单为什么派给了那个骑手：档位（顺路/无单/兜底）、"
                       "多绕多少米、是否被推迟过、以及完整时间线。",
        "inputSchema": _schema({"orderId": STR}, ["orderId"]),
    },
    {
        "name": "sim_advance",
        "description": "推进模拟时间（分钟），固定步长、可复现。推进后自动返回最新指标。",
        "inputSchema": _schema({"minutes": NUM}, ["minutes"]),
    },
    {
        "name": "place_order",
        "description": "下一笔订单（进订单池等待派单）。不给 merchantId 时随机选一家店。",
        "inputSchema": _schema({"name": STR, "phone": STR, "address": STR,
                                "note": STR, "merchantId": STR, "dx": NUM, "dy": NUM},
                               ["name", "phone", "address"]),
    },
    {
        "name": "place_orders",
        "description": "批量随机下单，返回实际下了几笔（可能因停单/到达上限而少下）。",
        "inputSchema": _schema({"count": INT}, ["count"]),
    },
    {
        "name": "dispatch_now",
        "description": "立刻跑一轮派单（不等派单节拍），返回本轮派出几单。",
        "inputSchema": _schema({}),
    },
    {
        "name": "add_rider",
        "description": "加一个骑手。不指定位置时自动选一个分散的位置。",
        "inputSchema": _schema({"name": STR, "maxOrders": INT, "x": NUM, "y": NUM}),
    },
    {
        "name": "remove_rider",
        "description": "移除一个骑手。手上有已取餐订单时会被拒绝（餐在人家手上）。",
        "inputSchema": _schema({"riderId": STR}, ["riderId"]),
    },
    {
        "name": "set_rider",
        "description": "改骑手状态（ONLINE/BUSY）或接单上限（1..20）。",
        "inputSchema": _schema({"riderId": STR, "status": STR, "maxOrders": INT},
                               ["riderId"]),
    },
    {
        "name": "assign_order",
        "description": "指派单：把订单池里的一笔单直接指定给某个骑手，算法不参与。",
        "inputSchema": _schema({"orderId": STR, "riderId": STR},
                               ["orderId", "riderId"]),
    },
    {
        "name": "reassign_order",
        "description": "调单：把已派出的单换给另一个骑手。已取餐的会被拒绝。",
        "inputSchema": _schema({"orderId": STR, "riderId": STR},
                               ["orderId", "riderId"]),
    },
    {
        "name": "get_config",
        "description": "取全部可调参数（派单节拍、顺路阈值、停单阈值、推迟开关、SLA 口径等）。",
        "inputSchema": _schema({}),
    },
    {
        "name": "set_config",
        "description": "改参数。键名用 get_config 里的名字；越界值会被夹到合法区间，"
                       "不认识的键会原样报回来（不会静默忽略）。",
        "inputSchema": _schema({"set": {"type": "object"}}, ["set"]),
    },
    {
        "name": "set_intake",
        "description": "停单/恢复进单。'on' 收单，'off' 停单，'auto' 交回自动判定。",
        "inputSchema": _schema({"action": {**STR, "enum": ["on", "off", "auto"]}},
                               ["action"]),
    },
    {
        "name": "reset",
        "description": "重置模拟（清空订单/骑手/日志并重新布点）。**参数会被保留**。"
                       "必须显式传 confirm=true，避免误触把状态清掉。",
        "inputSchema": _schema({"confirm": BOOL}, ["confirm"]),
    },
    {
        "name": "run_experiment",
        "description": "在独立沙箱里跑一个完整场景并返回 KPI（不动当前世界）。"
                       "同种子同参数必然复现，适合做对照实验。"
                       "ordersPerMin 控制来单速率，minutes 控制时长，riders 控制车队规模。",
        "inputSchema": _schema({
            "minutes": NUM, "seed": INT, "network": STR, "ordersPerMin": NUM,
            "riders": INT, "set": {"type": "object"},
        }),
    },
    {
        "name": "compare_configs",
        "description": "同一个种子、同一个场景，跑两组参数并给出逐项对比"
                       "（准时率、顺路占比、兜底占比、平均时长、送达量）。"
                       "判断某个改动到底有没有用，用这个而不是靠感觉。",
        "inputSchema": _schema({
            "base": {"type": "object"}, "variant": {"type": "object"},
            "minutes": NUM, "seed": INT, "network": STR, "ordersPerMin": NUM,
            "riders": INT, "labelBase": STR, "labelVariant": STR,
        }, ["base", "variant"]),
    },
    {
        "name": "learn_status",
        "description": "数据飞轮现状：攒了多少样本、学到了什么、有没有真的用上、模型版本。"
                       "想了解系统有没有随着使用变好，就问这个。",
        "inputSchema": _schema({}),
    },
    {
        "name": "learn_run",
        "description": "跑一次学习：从落盘样本里拟合模型。预测类在留出集上更优才自动应用；"
                       "策略类只出建议（看返回里的 requiresConfirmation）。"
                       "evaluateOnly=true 可以只看结果、不应用。",
        "inputSchema": _schema({"evaluateOnly": BOOL}),
    },
    {
        "name": "learn_rollback",
        "description": "把学习到的模型回滚到上一版。学习环能撤销，才敢让它自动应用。",
        "inputSchema": _schema({"steps": INT}),
    },
    {
        "name": "get_eta",
        "description": "查在途订单的预计送达时间（分钟）以及哪些有超时风险。"
                       "ETA 里的排队部分来自学习到的等派单模型 —— 这是模型真正被用上的地方。",
        "inputSchema": _schema({"limit": INT}),
    },
]


# ------------------------------------------------------------ 世界持有

class Lab:
    """进程内沙箱：自己的 World，时间只在被要求时前进。

    刻意**不起模拟线程**：agent 的实验需要的是"推进 N 分钟然后看结果"，
    而不是"等真实时间流逝"。不起线程也就没有竞态、没有锁等待、完全确定。
    """

    def __init__(self, seed: int = 20260927, network: Optional[str] = None):
        self.world = World.seeded(seed)
        if network:
            res = osm_loader.load(network, None, verbose=False)
            # NetworkMode 必须来自**文件顶部**的导入。以前这里写的是函数内
            # `from .world import NetworkMode`，而 `python py\waimai\mcp_server.py`
            # 是以**脚本**方式运行的（__package__ 为空）—— 相对导入直接
            # ImportError: attempted relative import with no known parent package，
            # 于是"带 --network 启动 MCP"一启动就崩。
            # 这个分支没被自检覆盖到（测试里没传 --network），
            # 而 PyCharm 的运行配置恰好带 `--network realistic`，一点就炸。
            mode = NetworkMode.SYNTHETIC if res.synthetic else NetworkMode.ROAD
            self.world.load_network(res.graph, res.pois, mode, network)

    def advance(self, seconds: float) -> None:
        sim_advance(self.world, seconds)


class Remote:
    """代理：把工具调用转成对在跑实例的 HTTP 请求。"""

    def __init__(self, url: str):
        self.client = Client(url)
        self.world = None

    def get(self, path: str) -> dict:
        return self.client.req(path)

    def post(self, path: str, body: dict) -> dict:
        return self.client.req(path, "POST", body)


# ------------------------------------------------------------ 工具实现

def _tool_impls(target) -> Dict[str, Callable[[dict], Any]]:
    """把工具名映射到实现。target 是 Lab 或 Remote。

    每个实现都返回一个 JSON 可序列化的 dict。凡是**改状态**的工具，
    返回里都带上改完之后的指标或相关状态，这样 agent 一次调用就能拿到反馈，
    不用马上再问一轮。
    """
    if isinstance(target, Lab):
        w = target.world

        def sim_status(_a):
            return service.stats_json(w)

        def get_stats(_a):
            return service.stats_json(w)

        def get_state(a):
            return service.state_json(w, max_orders=int(a.get("orderLimit") or 50),
                                      log_limit=int(a.get("logLimit") or 30))

        def get_zones(a):
            return service.zones_json(w, int(a.get("windowMin") or 60),
                                      int(a.get("cells") or 8))

        def list_riders(_a):
            d = service.stats_json(w)
            return {"ok": True, "riders": [service.rider_json(w, r)
                                           for r in w.riders.values()],
                    "stats": d["stats"]}

        def list_orders(a):
            status = (a.get("status") or "").upper()
            limit = int(a.get("limit") or 30)
            items = [o for o in w.orders.values() if not status or o.status.name == status]
            items.sort(key=lambda o: o.created_at, reverse=True)
            return {"ok": True, "count": len(items),
                    "orders": [service.order_json(w, o) for o in items[:limit]]}

        def explain_dispatch(a):
            return service.explain_order(w, a.get("orderId", ""))

        def sim_advance_tool(a):
            minutes = float(a.get("minutes") or 0)
            if minutes <= 0:
                return {"ok": False, "error": "minutes 必须大于 0"}
            t0 = time.perf_counter()
            target.advance(minutes * 60.0)
            d = service.stats_json(w)
            d["advancedMinutes"] = minutes
            d["wallSeconds"] = round(time.perf_counter() - t0, 3)
            return d

        def place_order(a):
            return service.place_order(w, a)

        def place_orders(a):
            return service.auto_orders(w, {"count": a.get("count", 1)})

        def dispatch_now(_a):
            return service.dispatch_now(w)

        def add_rider(a):
            return service.rider_add(w, a)

        def remove_rider(a):
            return service.rider_remove(w, a)

        def set_rider(a):
            return service.rider_settings(w, a)

        def assign_order(a):
            return service.assign(w, a)

        def reassign_order(a):
            return service.reassign(w, a)

        def get_config(_a):
            return {"ok": True, "config": service.config_json(w)}

        def set_config(a):
            payload = a.get("set") or {}
            if not isinstance(payload, dict):
                return {"ok": False, "error": "set 要是一个对象，比如 {\"slaMinutes\": 60}"}
            return service.apply_control(w, payload)

        def set_intake(a):
            action = (a.get("action") or "").lower()
            if action == "on":
                return service.apply_control(w, {"acceptOrders": True})
            if action == "off":
                return service.apply_control(w, {"acceptOrders": False})
            if action == "auto":
                return service.apply_control(w, {"acceptOrders": True,
                                                 "stopAcceptPoolRatio": 0.5})
            return {"ok": False, "error": "action 只能是 on / off / auto"}

        def reset(a):
            if not a.get("confirm"):
                return {"ok": False,
                        "error": "reset 会清空订单/骑手/日志。确认要重置就传 confirm=true。"}
            return service.reset(w)

        def run_experiment(a):
            return _experiment(a)

        def compare_configs(a):
            return _compare(a)

        def learn_status(_a):
            return learn.status()

        def learn_run(a):
            return learn.run(apply_low_risk=not bool(a.get("evaluateOnly")))

        def learn_rollback(a):
            return learn.rollback(int(a.get("steps") or 1))

        def get_eta(a):
            limit = int(a.get("limit") or 20)
            active = [o for o in w.orders.values()
                      if o.status.name in ("POOLED", "ASSIGNED", "PICKED_UP")]
            active.sort(key=lambda o: o.created_at)
            rows = []
            for o in active[:limit]:
                sec = learn.eta_seconds(w, o)
                rows.append({
                    "orderId": o.id, "status": o.status.name,
                    "etaMinutes": None if sec is None else round(sec / 60.0, 1),
                    "atRisk": (sec is not None
                               and sec > w.cfg.sla_minutes * 60),
                    "waitedMin": round((w.now() - o.created_at) / 60.0, 1),
                })
            return {"ok": True, "count": len(rows), "orders": rows,
                    "slaMinutes": w.cfg.sla_minutes,
                    "note": "etaMinutes 为 null 表示还没有等派单模型可用，不编数字。"}

        impls = locals()
        # 工具名和函数名不一定一样：`sim_advance` 的实现叫 sim_advance_tool
        # （避免和从 simulator 导入的 advance 别名打架）。不补这一行，
        # 工具清单里有 sim_advance，但 dispatch 时找不到实现 —— 报"没有这个工具"。
        impls["sim_advance"] = sim_advance_tool
        return impls

    # ---- Remote（接管在跑实例）----
    def sim_status(_a):
        return target.get("stats")

    def get_stats(_a):
        return target.get("stats")

    def get_state(a):
        d = target.get("state")
        limit = int(a.get("orderLimit") or 50)
        d["orders"] = d.get("orders", [])[:limit]
        d["log"] = d.get("log", [])[:int(a.get("logLimit") or 30)]
        return d

    def get_zones(a):
        return target.get(f"zones?window={int(a.get('windowMin') or 60)}"
                          f"&cells={int(a.get('cells') or 8)}")

    def list_riders(_a):
        return {"ok": True, "riders": target.get("state").get("riders", [])}

    def list_orders(a):
        status = (a.get("status") or "").upper()
        limit = int(a.get("limit") or 30)
        items = [o for o in target.get("state").get("orders", [])
                 if not status or o.get("status") == status]
        return {"ok": True, "count": len(items), "orders": items[:limit]}

    def explain_dispatch(a):
        return target.get(f"explain?orderId={a.get('orderId', '')}")

    def sim_advance_tool(a):
        return {"ok": False,
                "error": "接管模式下时间由那个进程自己走，不能手动推进。"
                         "要做可控实验请用进程内沙箱（不带 --url 启动）。"}

    def place_order(a):
        return target.post("order", a)

    def place_orders(a):
        return target.post("order/auto", {"count": a.get("count", 1)})

    def dispatch_now(_a):
        return target.post("dispatch", {})

    def add_rider(a):
        return target.post("rider/add", a)

    def remove_rider(a):
        return target.post("rider/remove", a)

    def set_rider(a):
        return target.post("rider", a)

    def assign_order(a):
        return target.post("assign", a)

    def reassign_order(a):
        return target.post("reassign", a)

    def get_config(_a):
        return {"ok": True, "config": target.get("state").get("cfg", {})}

    def set_config(a):
        payload = a.get("set") or {}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "set 要是一个对象"}
        return target.post("control", payload)

    def set_intake(a):
        action = (a.get("action") or "").lower()
        if action == "on":
            return target.post("control", {"acceptOrders": True})
        if action == "off":
            return target.post("control", {"acceptOrders": False})
        if action == "auto":
            return target.post("control", {"acceptOrders": True, "stopAcceptPoolRatio": 0.5})
        return {"ok": False, "error": "action 只能是 on / off / auto"}

    def reset(a):
        if not a.get("confirm"):
            return {"ok": False, "error": "reset 会清空状态。确认请传 confirm=true。"}
        return target.post("reset", {})

    # 实验类工具天然是"另开一个沙箱"，接管模式下也能用（不影响被接管的进程）
    def run_experiment(a):
        return _experiment(a)

    def compare_configs(a):
        return _compare(a)

    impls = locals()
    impls["sim_advance"] = sim_advance_tool
    return impls


# ------------------------------------------------------------ 实验工具

# 实验里允许透传的标量参数（对象参数 base/variant/set 另行展开）
_SCALARS = ("minutes", "seed", "network", "ordersPerMin", "riders")


def _sim_kwargs(a: dict, overrides: dict) -> dict:
    kw = {}
    if a.get("minutes") is not None:
        kw["minutes"] = float(a["minutes"])
    if a.get("seed") is not None:
        kw["seed"] = int(a["seed"])
    if a.get("network"):
        kw["network"] = str(a["network"])
    if a.get("ordersPerMin") is not None:
        kw["orders_per_min"] = float(a["ordersPerMin"])
    if a.get("riders") is not None:
        kw["riders"] = int(a["riders"])
    # 对象里的键直接作为配置覆盖传下去；不认识的名字会被 service 报出来
    kw.update(overrides or {})
    kw.setdefault("minutes", 90.0)
    return kw


def _experiment(a: dict) -> dict:
    kw = _sim_kwargs(a, a.get("set") or {})
    d = service.simulate(**kw)
    d.pop("perf", None)          # 机器相关的数字不进实验结论
    return d


# 对比里关心的指标。挑的都是"能说明问题"的：效率（时长/里程）、
# 质量（准时率）、调度结构（顺路/兜底占比）、规模（送达量）。
COMPARE_KEYS = [
    ("onTimeRate", "准时率 %", "up"),
    ("onRouteShare", "顺路占比 %", "up"),
    ("tier3Fallback", "兜底单数", "down"),
    ("avgTotalMin", "平均总时长 分", "down"),
    ("avgWaitDispatchMin", "平均等派单 分", "down"),
    ("delivered", "送达单数", "up"),
    ("postponedOrders", "被推迟单数", "flat"),
    ("riderTotalKm", "骑手里程 km", "down"),
]


def _compare(a: dict) -> dict:
    base_kw = _sim_kwargs(a, a.get("base") or {})
    var_kw = _sim_kwargs(a, a.get("variant") or {})
    # 两组必须同种子同场景，否则比的不是参数而是随机性或场景
    var_kw["seed"] = base_kw.get("seed", var_kw.get("seed"))
    for k in ("minutes", "network", "orders_per_min", "riders"):
        if k not in (a.get("variant") or {}):
            var_kw[k] = base_kw.get(k, var_kw.get(k))

    b = service.simulate(**base_kw)
    v = service.simulate(**var_kw)
    bs, vs = b["stats"], v["stats"]

    rows = []
    for key, label, direction in COMPARE_KEYS:
        bv, vv = bs.get(key), vs.get(key)
        delta = None if (bv is None or vv is None) else round(vv - bv, 2)
        better = None
        if delta is not None and direction != "flat":
            better = (delta > 0) if direction == "up" else (delta < 0)
            if abs(delta) < 1e-9:
                better = None                      # 没区别就不说谁更好
        rows.append({"metric": key, "label": label, "base": bv, "variant": vv,
                     "delta": delta, "better": better})

    return {
        "ok": bool(b.get("ok") and v.get("ok")),
        "labelBase": a.get("labelBase") or "base",
        "labelVariant": a.get("labelVariant") or "variant",
        "paramsBase": b["params"], "paramsVariant": v["params"],
        "comparison": rows,
        "verdict": _verdict(rows, a.get("labelBase") or "base",
                            a.get("labelVariant") or "variant"),
        "ignoredParams": list(b.get("ignoredParams") or []) + list(v.get("ignoredParams") or []),
        "note": "同一随机种子、同一场景，只有参数不同 —— 所以差异来自参数本身。",
    }


def _verdict(rows: list, label_base: str, label_variant: str) -> str:
    better = [r for r in rows if r["better"] is True]
    worse = [r for r in rows if r["better"] is False]
    if not better and not worse:
        return f"{label_variant} 与 {label_base} 在关心的指标上没有可测差别。"
    parts = []
    if better:
        parts.append("变好：" + "、".join(r["label"] for r in better))
    if worse:
        parts.append("变差：" + "、".join(r["label"] for r in worse))
    return "；".join(parts) + "。"


# ------------------------------------------------------------ JSON-RPC

class Server:
    def __init__(self, target, out):
        self.target = target
        self.impls = _tool_impls(target)
        self.out = out                 # 真正的协议通道（原始 stdout）
        self.initialized = False

    # ---- 收发 ----

    def send(self, msg: dict) -> None:
        self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.out.flush()

    def reply(self, req_id, result) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def error(self, req_id, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": code, "message": message}})

    # ---- 方法 ----

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            client_ver = (params.get("protocolVersion") or "")
            ver = client_ver if client_ver in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
            self.initialized = True
            self.reply(req_id, {
                "protocolVersion": ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "外卖派单系统的沙箱。默认模式下时间只在 sim_advance 时前进，"
                    "所以实验是可复现的：先用 run_experiment 或 compare_configs 做判断，"
                    "再用 sim_advance / place_order 观察具体行为，"
                    "用 explain_dispatch 查「这单为什么派给了他」。"),
            })
            return

        if is_notification:
            # 通知不需要回复（initialized / cancelled 之类）
            return

        if method == "ping":
            self.reply(req_id, {})
            return

        if method == "tools/list":
            self.reply(req_id, {"tools": TOOLS})
            return

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            impl = self.impls.get(name)
            if impl is None:
                self.error(req_id, -32602, f"没有这个工具：{name}")
                return
            if not isinstance(args, dict):
                self.error(req_id, -32602, "arguments 必须是一个对象")
                return
            try:
                # 工具执行期间把 stdout 收到 stderr，任何 print 都不会插进协议流。
                # 这一点靠代码保证：路网加载、日志初始化之类的地方都有 print。
                real_stdout = sys.stdout
                sys.stdout = sys.stderr
                try:
                    result = impl(args)
                finally:
                    sys.stdout = real_stdout
                text = json.dumps(result, ensure_ascii=False, indent=1)
                is_err = isinstance(result, dict) and result.get("ok") is False
                self.reply(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": bool(is_err),
                })
            except Exception as e:                  # noqa: BLE001
                # 工具里的异常不该把整个服务打死：MCP 允许用 isError 回报
                self.reply(req_id, {
                    "content": [{"type": "text",
                                 "text": f"执行失败（{type(e).__name__}）：{e}"}],
                    "isError": True,
                })
            return

        self.error(req_id, -32601, f"不支持的方法：{method}")

    def serve_stdio(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 协议流里出现非 JSON 时不能猜，只能忽略这一行并继续
                obs.counter("mcp_bad_lines")
                continue
            if isinstance(msg, list):
                for m in msg:                       # 规范允许批量
                    if isinstance(m, dict):
                        self.handle(m)
            elif isinstance(msg, dict):
                self.handle(msg)
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="waimai mcp", description="MCP 服务（stdio）")
    p.add_argument("--url", default=None,
                   help="接管一个正在跑的实例（不给则在自己的沙箱里跑）")
    p.add_argument("--seed", type=int, default=20260927, help="沙箱随机种子")
    p.add_argument("--network", default=None, help="沙箱用哪块路网（realistic/synthetic）")
    p.add_argument("--no-log", action="store_true", help="不写日志文件")
    p.add_argument("--log-level", default="error",
                   choices=("debug", "info", "warn", "error"))
    args = p.parse_args(argv)

    # stdout 是协议通道：先把它单独拿住，再把 sys.stdout 指向 stderr。
    # 这样即便有代码漏了 print，也只会跑到 stderr，不会破坏协议流。
    protocol_out = sys.stdout
    sys.stdout = sys.stderr

    obs.configure(log_dir=None if args.no_log else paths.under_data("logs"),
                  console_level=args.log_level)

    if args.url:
        target = Remote(args.url)
        obs.event("info", "mcp", "MCP 启动（接管模式）", url=args.url)
    else:
        target = Lab(seed=args.seed, network=args.network)
        obs.event("info", "mcp", "MCP 启动（进程内沙箱）",
                  seed=args.seed, network=args.network or "abstract")

    return Server(target, protocol_out).serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
