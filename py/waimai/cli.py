"""命令行入口：让智能体（和你自己）不开浏览器也能驱动整套系统。

    python -m waimai <命令> [选项]
    python py/waimai/cli.py <命令> [选项]

**两种模式，刻意分开**（这是设计决定，不是方便的产物）：

· **驱动在跑的服务**（`--url`，默认 http://127.0.0.1:8787）
  凡是"改一个正在运行的系统"的命令都走这里：下单、派单、调单、改参数、看状态。
  这些命令在本地模式下没有意义 —— CLI 每次调用都是一个新进程，
  在一份马上要消失的世界里下单一笔单，等于什么都没做。

· **本地无头**（`--local`，`simulate` / `bench` / `learn` 默认走这里）
  进程内建世界、固定步长推进、跑完出指标，不依赖任何长驻服务。
  这是给 agent 做实验用的：**同种子同参数必然复现**，
  所以"改一个参数、跑两次、比指标"的结论是可信的。

所有命令都支持 `--json`（机器读）和有意义退出码：0 成功 / 1 业务失败 / 2 用法或连接问题。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from waimai import dispatcher, security, service, simulator, telemetry
    from waimai import learn as learn_mod
    from waimai import main as main_mod
    from waimai.world import World
else:
    from . import dispatcher, security, service, simulator, telemetry
    from . import learn as learn_mod
    from . import main as main_mod
    from .world import World
# 注意：这里**不能**在顶层导入 mcp_server —— 它反过来要 `from .cli import Client`，
# 两边都在顶层互相导入就成了循环，报 `cannot import name 'Client' from 'waimai.cli'`。
# mcp_server 在 cmd_mcp 里按需导入（见那里的注释）。

DEFAULT_URL = "http://127.0.0.1:8787"

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ------------------------------------------------------------ 输出

def emit(payload, as_json: bool, human: str = "") -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif human:
        print(human)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def err(msg: str, as_json: bool, code: int = EXIT_FAIL) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    else:
        print(f"错误：{msg}", file=sys.stderr)
    return code


# ------------------------------------------------------------ HTTP 客户端

class Client:
    """把 CLI 命令翻成 HTTP 调用。token 自动从 data/api-token 或环境变量取。"""

    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token or security.load_or_create_token()[0]

    def req(self, path: str, method: str = "GET", body: Optional[dict] = None):
        url = f"{self.base}/api/{path}"
        data = None
        headers = {"X-Auth-Token": self.token}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError(
                    "认证失败（401）。服务端要的 token 在 data/api-token，"
                    "或设环境变量 WAIMAI_TOKEN。") from e
            raise RuntimeError(f"HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"连不上 {self.base}（{e.reason}）。服务没在跑就用 "
                f"`python -m waimai serve` 起一个。") from e


# ------------------------------------------------------------ 人读格式

def human_stats(d: dict) -> str:
    s = d["stats"]
    lines = [
        f"时钟 {d['sim']['clock']}   运行 {d.get('runId', '-')}   "
        f"路网 {d['network']['name']}",
        f"订单 总 {s['totalOrders']}  已送达 {s['delivered']}  配送中 {s['inFlight']}  待派 {s['pooled']}",
    ]
    if s.get("onTimeRate") is not None:
        lines.append(f"准时率 {s['onTimeRate']}%   平均总时长 {s['avgTotalMin']} 分   "
                     f"平均等派单 {s['avgWaitDispatchMin']} 分")
    if s.get("onRouteShare") is not None:
        lines.append(f"顺路占比 {s['onRouteShare']}%   "
                     f"（顺路 {s['tier1OnRoute']} / 无单 {s['tier2Idle']} / 兜底 {s['tier3Fallback']}）"
                     f"   推迟 {s['postponedOrders']} 单")
    lines.append(f"骑手 在线 {s['riderOnline']}/{s['riderCount']}   负载 {s['riderLoad']}/{s['riderCapacity']}"
                 f"（{s['riderUtilization']}%）   里程 {s['riderTotalKm']} km")
    it = d.get("intake") or {}
    if it:
        state = "进单中" if it.get("open") else "已停单"
        lines.append(f"进单 {state}   待派占比 {round(it.get('poolRatio', 0) * 100)}%"
                     f"（阈值 {round(it.get('thresholdRatio', 0) * 100)}%）")
    return "\n".join(lines)


def human_rider(r: dict) -> str:
    return (f"{r['id']:4s} {r['name']:4s} {r['status']:6s} {r['motion']:8s} "
            f"手持 {r['activeOrders']}/{r['maxOrders']}  已送 {r['delivered']}  {r['km']}km")


def human_order(o: dict) -> str:
    tier = {1: "顺路", 2: "无单", 3: "兜底"}.get(o.get("tier"), "—")
    rider = o.get("riderId") or "待派"
    return (f"{o['id']:9s} {o['status']:9s} {tier:4s} {rider:4s} "
            f"{o['merchantName']} → {o['address']}")


# ------------------------------------------------------------ 命令实现

def cmd_serve(args) -> int:
    argv = ["--port", str(args.port), "--speed", str(args.speed), "--host", args.host]
    if args.osm:
        argv += ["--osm", args.osm]
    if args.no_auth:
        argv += ["--no-auth"]
    return main_mod.main(argv)


def cmd_state(args) -> int:
    d = Client(args.url, args.token).req("state")
    emit(d, args.json, human_stats(d))
    return EXIT_OK


def cmd_stats(args) -> int:
    d = Client(args.url, args.token).req("stats")
    if args.json:
        emit(d, True)
    else:
        print(human_stats({**d, "stats": d["stats"]}))
    return EXIT_OK


def cmd_riders(args) -> int:
    d = Client(args.url, args.token).req("state")
    riders = d["riders"]
    if args.json:
        emit(riders, True)
    else:
        for r in riders:
            print(human_rider(r))
    return EXIT_OK


def cmd_orders(args) -> int:
    d = Client(args.url, args.token).req("state")
    orders = d["orders"]
    if args.status:
        orders = [o for o in orders if o["status"] == args.status.upper()]
    if args.json:
        emit(orders, True)
    else:
        for o in orders[:args.limit]:
            print(human_order(o))
    return EXIT_OK


def cmd_order_add(args) -> int:
    body = {"name": args.name, "phone": args.phone, "address": args.address,
            "note": args.note or ""}
    if args.merchant:
        body["merchantId"] = args.merchant
    if args.dx is not None and args.dy is not None:
        body["dx"], body["dy"] = args.dx, args.dy
    r = Client(args.url, args.token).req("order", "POST", body)
    emit(r, args.json, r.get("message") or r.get("error", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_order_random(args) -> int:
    c = Client(args.url, args.token)
    if args.count:
        r = c.req("order/auto", "POST", {"count": args.count})
    else:
        r = c.req("order/random")
    emit(r, args.json, r.get("message") or json.dumps(r.get("customer", {}), ensure_ascii=False))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_dispatch(args) -> int:
    r = Client(args.url, args.token).req("dispatch", "POST", {})
    emit(r, args.json, r.get("message", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_assign(args) -> int:
    r = Client(args.url, args.token).req(args.cmd_kind, "POST",
                                        {"orderId": args.order, "riderId": args.rider})
    emit(r, args.json, r.get("message") or r.get("error", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_rider_add(args) -> int:
    body = {}
    if args.name:
        body["name"] = args.name
    if args.max_orders:
        body["maxOrders"] = args.max_orders
    r = Client(args.url, args.token).req("rider/add", "POST", body)
    emit(r, args.json, r.get("message") or r.get("error", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_rider_remove(args) -> int:
    r = Client(args.url, args.token).req("rider/remove", "POST", {"riderId": args.rider})
    emit(r, args.json, r.get("message") or r.get("error", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_rider_set(args) -> int:
    body = {"riderId": args.rider}
    if args.status:
        body["status"] = args.status
    if args.cap:
        body["maxOrders"] = args.cap
    r = Client(args.url, args.token).req("rider", "POST", body)
    emit(r, args.json, r.get("message") or r.get("error", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_config(args) -> int:
    c = Client(args.url, args.token)
    if args.set:
        body = {}
        for pair in args.set:
            if "=" not in pair:
                return err(f"参数要写成 key=value：{pair}", args.json, EXIT_USAGE)
            k, _, v = pair.partition("=")
            try:
                body[k] = json.loads(v)
            except json.JSONDecodeError:
                body[k] = v                       # 字符串值（比如 status）
        r = c.req("control", "POST", body)
        emit(r, args.json, r.get("message") or r.get("error", ""))
        return EXIT_OK if r.get("ok") else EXIT_FAIL
    d = c.req("state")
    cfg = d["cfg"]
    if args.json:
        emit(cfg, True)
    else:
        for k, v in cfg.items():
            print(f"{k:26s} {v}")
    return EXIT_OK


def cmd_intake(args) -> int:
    c = Client(args.url, args.token)
    if args.action == "on":
        r = c.req("control", "POST", {"acceptOrders": True})
    elif args.action == "off":
        r = c.req("control", "POST", {"acceptOrders": False})
    else:
        d = c.req("state")
        it = d["intake"]
        emit(it, args.json,
             f"进单：{'开' if it['open'] else '停'}（{it.get('reason') or '正常'}）\n"
             f"待派 {it['pooled']} / 在系统 {it['current']}，占比 {round(it['poolRatio'] * 100)}%"
             f"，阈值 {round(it['thresholdRatio'] * 100)}%")
        return EXIT_OK
    emit(r, args.json, r.get("message") or r.get("error", ""))
    return EXIT_OK if r.get("ok") else EXIT_FAIL


def cmd_network(args) -> int:
    c = Client(args.url, args.token)
    if args.action == "use":
        r = c.req("network", "POST", {"id": args.id})
        emit(r, args.json, r.get("message") or r.get("error", ""))
        return EXIT_OK if r.get("ok") else EXIT_FAIL
    d = c.req("networks")
    if args.json:
        emit(d, True)
    else:
        for n in d["list"]:
            mark = "←当前" if n["current"] else ""
            print(f"{n['id']:28s} {n['label']} {mark}")
    return EXIT_OK


def cmd_reset(args) -> int:
    r = Client(args.url, args.token).req("reset", "POST", {})
    emit(r, args.json, r.get("message", ""))
    return EXIT_OK


def cmd_zones(args) -> int:
    d = Client(args.url, args.token).req(f"zones?window={args.window}&cells={args.cells}")
    if args.json:
        emit(d, True)
        return EXIT_OK
    print(f"窗口 {d['windowMin']} 分钟，{d['cellsX']}×{d['cellsY']} 网格，"
          f"单骑手产能 {d['throughputPerRiderHour']} 单/小时（{d['throughputMethodLabel']}）")
    for z in d["zones"]:
        if z["orders"] or z["verdict"] != "IDLE":
            print(f"  行{z['row']} 列{z['col']}  单 {z['orders']:3d}  "
                  f"{z['verdictLabel']:10s} 压力 {z['pressure']}  {z['cause']}")
    print(f"\n建议增派 {d['suggestAdd']} 人，建议轮休 {d['suggestRest']} 人")
    return EXIT_OK


def cmd_explain(args) -> int:
    d = Client(args.url, args.token).req(f"explain?orderId={args.order}")
    if not d.get("ok"):
        return err(d.get("error", "查不到这笔订单"), args.json)
    if args.json:
        emit(d, True)
        return EXIT_OK
    print(f"订单 {d['orderId']}（{d['status']}）")
    print(f"派给 {d['riderId']} {d['riderName'] or ''}　档位 {d['tierLabel']}"
          f"（多绕 {d['detourM']:.0f} 米）")
    print("理由：")
    for w in d["why"]:
        print(f"  · {w}")
    print("时间线：")
    for e in d["timeline"]:
        print(f"  {e['clock']}  {e['type']}  {e['detail']}")
    return EXIT_OK


# ------------------------------------------------------------ 本地无头

def cmd_simulate(args) -> int:
    """进程内跑一段模拟，出 KPI。不开服务、不起线程、同种子可复现。"""
    d = service.simulate(
        minutes=args.minutes, seed=args.seed, network=args.network,
        orders_per_min=args.orders_per_min, riders=args.riders,
        **(_cfg_overrides(args)))
    if args.deterministic:
        # 只保留可复现的部分：同种子同参数两次跑必须逐字节一致，
        # 这样 agent 才能用"比 JSON"判断某个改动到底有没有影响。
        d.pop("perf", None)
    if args.json:
        emit(d, True)
        return EXIT_OK if d.get("ok") else EXIT_FAIL
    # 参数名拼错时必须报错退出 —— 静默按默认值跑完，拿到的是一份
    # 看起来正常的报告，而你以为自己改的那个参数根本没生效。
    if d.get("ignoredParams"):
        print(f"错误：不认识的参数名 {', '.join(d['ignoredParams'])}"
              f"（用 `waimai config` 看全部可用名字）", file=sys.stderr)
        return EXIT_FAIL
    print(f"跑了 {args.minutes} 模拟分钟"
          f"（真实耗时 {d.get('perf', {}).get('wallSeconds')}s；"
          f"这个数取决于机器，不是仿真结果）")
    print(human_stats(d))
    return EXIT_OK


def _cfg_overrides(args) -> dict:
    out = {}
    for pair in (args.set or []):
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


# KPI 门禁：把"有效性/高效性"变成可判定的数字，而不是形容词。
KPI_GATES = [
    ("onTimeRate", ">=", 95.0, "准时率"),
    ("onRouteShare", ">=", 35.0, "顺路占比"),
]


def _tier3_share(s: dict) -> Optional[float]:
    auto = s["tier1OnRoute"] + s["tier2Idle"] + s["tier3Fallback"]
    return None if auto == 0 else s["tier3Fallback"] * 100.0 / auto


def cmd_kpi(args) -> int:
    """跑一轮基准场景，把关键指标和门禁比对，不达标就非零退出。

    基准场景刻意选在**有负载**的一档（默认 90 分钟 / 1 单每分钟 / 10 骑手，
    实测负载约 50%）。原因：低负载时"顺路占比"天然就低 —— 空车骑手就在旁边，
    让他直接去取餐比硬塞给已有路线的骑手更省钱，那是**正确答案**而不是缺陷
    （README 里那张负载对照表就是这个意思）。拿低负载的占比去卡门禁，
    只会逼着实现把单子硬塞给顺路骑手，把指标做好看、把配送做差。
    """
    d = service.simulate(minutes=args.minutes, seed=args.seed,
                         network=args.network, orders_per_min=args.orders_per_min,
                         riders=args.riders, **_cfg_overrides(args))
    s = d["stats"]
    rows = []
    for key, op, threshold, label in KPI_GATES:
        val = s.get(key)
        ok = val is not None and val >= threshold
        rows.append({"metric": key, "label": label, "value": val,
                     "threshold": f">= {threshold}", "pass": bool(ok)})
    fallback = _tier3_share(s)
    rows.append({"metric": "tier3Share", "label": "兜底占比", "value": round(fallback, 1)
                 if fallback is not None else None, "threshold": "<= 20",
                 "pass": fallback is not None and fallback <= 20.0})

    failed = [r for r in rows if not r["pass"]]
    if args.json:
        emit({"ok": not failed, "params": d["params"], "gates": rows,
              "load": s.get("riderUtilization"), "stats": s}, True)
        return EXIT_FAIL if failed else EXIT_OK
    print(f"KPI 门禁（{args.minutes} 模拟分钟，种子 {args.seed}，"
          f"{d['params']['riders']} 骑手，{args.orders_per_min} 单/分钟，"
          f"负载 {s.get('riderUtilization')}%）")
    for r in rows:
        mark = "✓" if r["pass"] else "✗"
        val = "—" if r["value"] is None else r["value"]
        print(f"  {mark} {r['label']:10s} {val:>7}   门禁 {r['threshold']}")
    print()
    if failed:
        print(f"{len(failed)} 项没达标 —— 这些数字是实测，不是目标值。")
        return EXIT_FAIL
    print("全部门禁达标。")
    return EXIT_OK


def cmd_bench(args) -> int:
    """延迟与体积基线：把"高效性"量出来，而不是"感觉挺快"。"""
    if args.local:
        return _bench_local(args)
    return _bench_http(args)


def _bench_http(args) -> int:
    c = Client(args.url, args.token)
    lat, sizes = [], []
    for i in range(args.iterations):
        t0 = time.perf_counter()
        try:
            d = c.req("state")
        except RuntimeError as e:
            return err(str(e), args.json, EXIT_USAGE)
        lat.append((time.perf_counter() - t0) * 1000.0)
        sizes.append(len(json.dumps(d, ensure_ascii=False)))
    lat.sort()

    def pct(q):
        return round(lat[min(len(lat) - 1, int(q * (len(lat) - 1)))], 1)

    s = d["stats"]
    out = {"ok": True, "mode": "http", "url": args.url,
           "iterations": args.iterations,
           "stateLatencyMs": {"p50": pct(0.50), "p95": pct(0.95), "max": round(lat[-1], 1)},
           "stateBytes": {"last": sizes[-1], "max": max(sizes)},
           "ordersInState": s["totalOrders"], "retainedOrders": s["totalOrders"]}
    gate = out["stateLatencyMs"]["p95"] <= args.max_p95_ms
    out["gateP95Ms"] = args.max_p95_ms
    out["ok"] = bool(gate)
    if args.json:
        emit(out, True)
        return EXIT_OK if gate else EXIT_FAIL
    print(f"/api/state 延迟（{args.iterations} 次）：p50 {out['stateLatencyMs']['p50']} ms，"
          f"p95 {out['stateLatencyMs']['p95']} ms，max {out['stateLatencyMs']['max']} ms")
    print(f"响应体积：{sizes[-1] / 1024:.1f} KB（{s['totalOrders']} 笔订单在内存里）")
    if not gate:
        print(f"p95 超过门禁 {args.max_p95_ms} ms")
        return EXIT_FAIL
    print(f"p95 在门禁 {args.max_p95_ms} ms 之内。")
    return EXIT_OK


def _bench_local(args) -> int:
    """进程内基准：派单轮次耗时、推进速度。不测 HTTP。"""
    world = World.seeded(args.seed)
    world.cfg.auto_order = True
    world.cfg.auto_order_every_sec = 10
    simulator.advance(world, 600)                       # 先跑热，让池子里有单
    rounds, times = 0, []
    for _ in range(args.iterations):
        if not world.pool:
            simulator.advance(world, 60)
        t0 = time.perf_counter()
        dispatcher.round_(world)
        times.append((time.perf_counter() - t0) * 1000.0)
        rounds += 1
    times.sort()

    def pct(q):
        return round(times[min(len(times) - 1, int(q * (len(times) - 1)))], 2)

    t0 = time.perf_counter()
    simulator.advance(world, 3600)
    wall = time.perf_counter() - t0
    out = {"ok": True, "mode": "local", "iterations": rounds,
           "dispatchRoundMs": {"p50": pct(0.50), "p95": pct(0.95), "max": round(times[-1], 2)},
           "simSpeedup": round(3600 / wall, 1),
           "ordersInWorld": len(world.orders)}
    gate = out["dispatchRoundMs"]["p95"] <= args.max_p95_ms
    out["gateP95Ms"] = args.max_p95_ms
    out["ok"] = bool(gate)
    if args.json:
        emit(out, True)
        return EXIT_OK if gate else EXIT_FAIL
    print(f"派单轮次耗时（{rounds} 轮）：p50 {out['dispatchRoundMs']['p50']} ms，"
          f"p95 {out['dispatchRoundMs']['p95']} ms，max {out['dispatchRoundMs']['max']} ms")
    print(f"模拟推进：1 小时模拟时间用 {wall:.2f}s 真实时间（{out['simSpeedup']}× 实时）")
    if not gate:
        print(f"p95 超过门禁 {args.max_p95_ms} ms")
        return EXIT_FAIL
    print(f"p95 在门禁 {args.max_p95_ms} ms 之内。")
    return EXIT_OK


def cmd_learn(args) -> int:
    """数据飞轮：看样本、跑学习、看报告、回滚。"""

    if args.action == "status":
        d = learn_mod.status()
        if args.json:
            emit(d, True)
            return EXIT_OK
        print(f"模型版本 {d['modelVersion']}"
              + (f"（{d['updatedAt']}，{d['reason']}）" if d.get("updatedAt")
                 else "（还没学过）"))
        print(f"样本：决策 {d['data']['decisions']}，结果 {d['data']['outcomes']}，"
              f"骑行 {d['data']['legs']}，可训练样本 {d['joinedSamples']}")
        print(f"学到的：等派单模型 {d['learned']['wait']['buckets']} 档"
              f"（{'已应用' if d['learned']['wait']['applied'] else '未应用'}），"
              f"骑行速度 {d['learned']['speed'] or '无'}")
        ready = "是" if d["readyToLearn"] else f"样本还不够（需 {d['minSamples']} 条）"
        print(f"现在能学：{ready}")
        print(d["privacy"])
        return EXIT_OK

    if args.action == "run":
        d = learn_mod.run(apply_low_risk=not args.evaluate_only)
        if args.json:
            emit(d, True)
            return EXIT_OK
        print(f"学习完成（模型版本 {d['modelVersion']}）")
        for name, rep in d["report"].items():
            mark = "已应用" if rep.get("applied") else "未应用"
            print(f"  {name:16s} {mark}  {rep.get('reason', '')}")
            ev = rep.get("evaluation") or {}
            if ev.get("ok"):
                print(f"      留出集 MAE：{ev['maeOld']} → {ev['maeNew']} 秒"
                      f"（{'改善' if ev['gain'] > 0 else '变差'} "
                      f"{abs(ev['gain']) * 100:.1f}%），基准：{ev['baseline']}")
        print(f"  数据：{d['data']}")
        return EXIT_OK

    if args.action == "rollback":
        r = learn_mod.rollback(args.steps)
        if r.get("ok"):
            emit(r, args.json, f"已回滚到第 {r['rolledBackTo']} 版（{r['reason']}）")
            return EXIT_OK
        return err(r.get("error", "回滚失败"), args.json)

    d = telemetry.stats()
    if args.json:
        emit(d, True)
        return EXIT_OK
    print(f"样本目录 {d['dir']}（{'已启用' if d['enabled'] else '未启用'}）")
    print(f"  决策 {d['counts']['decisions']}  结果 {d['counts']['outcomes']}  "
          f"骑行 {d['counts']['legs']}  可训练样本 {d['samples']}")
    for f in d["files"]:
        print(f"  {f['name']:34s} {f['bytes'] / 1024:8.1f} KB")
    print(f"  {d['note']}")
    return EXIT_OK


def cmd_mcp(args) -> int:
    # 按需导入：mcp_server 反向依赖 cli.Client，顶层互相导入会成循环。
    # 这里用**绝对**导入（waimai.mcp_server），因为脚本方式运行时
    # 顶部的 sys.path.insert 已经把 py/ 放进搜索路径了，两种跑法都能用。
    from waimai import mcp_server

    argv = []
    if args.url and args.url != DEFAULT_URL:
        argv += ["--url", args.url]
    if getattr(args, "seed", None) is not None:
        argv += ["--seed", str(args.seed)]
    if getattr(args, "network", None):
        argv += ["--network", args.network]
    return mcp_server.main(argv)


# ------------------------------------------------------------ 参数

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="waimai", description="外卖平台派单系统 · 命令行（GUI / CLI / MCP 三个入口共用同一套业务规则）")
    # 公共选项挂在 parent 上，这样 `waimai --json state` 和 `waimai state --json`
    # 两种写法都能用 —— agent 拼命令时很容易把 --json 放后面，
    # 只定义在顶层会直接报"unrecognized arguments"。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="以 JSON 输出（给机器/agent 读）")
    common.add_argument("--url", default=DEFAULT_URL,
                        help=f"目标服务地址（默认 {DEFAULT_URL}）")
    common.add_argument("--token", default="", help="认证 token（默认读 data/api-token）")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--url", default=DEFAULT_URL, help=argparse.SUPPRESS)
    p.add_argument("--token", default="", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", parents=[common], help="启动 Web 服务（= run-py.ps1）")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--speed", type=float, default=20)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--osm", default=None)
    s.add_argument("--no-auth", action="store_true")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("state", parents=[common],
                   help="看整体状态（时钟/指标/进单）").set_defaults(func=cmd_state)
    sub.add_parser("stats", parents=[common],
                   help="只看指标，不带订单明细").set_defaults(func=cmd_stats)

    s = sub.add_parser("riders", parents=[common], help="列骑手")
    s.set_defaults(func=cmd_riders)
    s = sub.add_parser("orders", parents=[common], help="列订单")
    s.add_argument("--status", default=None, help="只看某个状态：POOLED/ASSIGNED/PICKED_UP/DELIVERED")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("order", help="下单相关")
    osub = s.add_subparsers(dest="action", required=True)
    a = osub.add_parser("add", parents=[common], help="下一笔指定内容的单")
    a.add_argument("--name", required=True)
    a.add_argument("--phone", required=True)
    a.add_argument("--address", required=True)
    a.add_argument("--note", default="")
    a.add_argument("--merchant", default=None)
    a.add_argument("--dx", type=float, default=None)
    a.add_argument("--dy", type=float, default=None)
    a.set_defaults(func=cmd_order_add)
    a = osub.add_parser("random", parents=[common], help="随机取一份（--count N 则直接下 N 单）")
    a.add_argument("--count", type=int, default=0)
    a.set_defaults(func=cmd_order_random)

    sub.add_parser("dispatch", parents=[common],
                   help="立刻跑一轮派单").set_defaults(func=cmd_dispatch)

    s = sub.add_parser("assign", parents=[common], help="指派单：把订单池里的单指定给某骑手")
    s.add_argument("order")
    s.add_argument("rider")
    s.set_defaults(func=cmd_assign, cmd_kind="assign")
    s = sub.add_parser("reassign", parents=[common], help="调单：把已派出的单换给另一个骑手")
    s.add_argument("order")
    s.add_argument("rider")
    s.set_defaults(func=cmd_assign, cmd_kind="reassign")

    s = sub.add_parser("rider", help="骑手管理")
    rsub = s.add_subparsers(dest="action", required=True)
    a = rsub.add_parser("add", parents=[common])
    a.add_argument("--name", default=None)
    a.add_argument("--max-orders", type=int, default=None)
    a.set_defaults(func=cmd_rider_add)
    a = rsub.add_parser("remove", parents=[common])
    a.add_argument("rider")
    a.set_defaults(func=cmd_rider_remove)
    a = rsub.add_parser("set", parents=[common])
    a.add_argument("rider")
    a.add_argument("--status", default=None, choices=[None, "ONLINE", "BUSY"])
    a.add_argument("--cap", type=int, default=None)
    a.set_defaults(func=cmd_rider_set)

    s = sub.add_parser("config", parents=[common], help="看或改参数")
    s.add_argument("--set", action="append", metavar="key=value",
                   help="改参数，可重复：--set slaMinutes=60 --set postponeMaxWaitMin=3")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("intake", parents=[common], help="停单/进单")
    s.add_argument("action", nargs="?", default="status", choices=["status", "on", "off"])
    s.set_defaults(func=cmd_intake)

    s = sub.add_parser("network", parents=[common], help="路网")
    s.add_argument("action", nargs="?", default="list", choices=["list", "use"])
    s.add_argument("id", nargs="?", default="")
    s.set_defaults(func=cmd_network)

    sub.add_parser("reset", parents=[common],
                   help="重置模拟（参数保留）").set_defaults(func=cmd_reset)

    s = sub.add_parser("zones", parents=[common], help="区域研判")
    s.add_argument("--window", type=int, default=60)
    s.add_argument("--cells", type=int, default=8)
    s.set_defaults(func=cmd_zones)

    s = sub.add_parser("explain", parents=[common], help="这笔单为什么派给了他")
    s.add_argument("order")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("simulate", parents=[common],
                       help="进程内无头跑一段模拟，出 KPI（可复现）")
    _add_sim_args(s)
    s.add_argument("--deterministic", action="store_true",
                   help="--json 时去掉机器相关的数字，让同种子两次输出逐字节一致（便于 diff）")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("kpi", parents=[common],
                       help="跑基准场景并与 KPI 门禁比对，不达标非零退出")
    _add_sim_args(s)
    s.set_defaults(func=cmd_kpi)

    s = sub.add_parser("bench", parents=[common], help="延迟/体积基线")
    s.add_argument("--local", action="store_true", help="进程内基准（默认打 HTTP）")
    s.add_argument("--iterations", type=int, default=30)
    s.add_argument("--max-p95-ms", type=float, default=50.0)
    s.add_argument("--seed", type=int, default=20260927)
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("mcp", parents=[common], help="以 MCP 服务运行（stdio，给智能体调用）")
    s.add_argument("--seed", type=int, default=None, help="沙箱种子（默认 20260927）")
    s.add_argument("--network", default=None, help="沙箱路网（realistic/synthetic）")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("learn", parents=[common],
                       help="数据飞轮：样本概况 / 跑学习 / 回滚")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "data", "run", "rollback"])
    s.add_argument("--evaluate-only", action="store_true",
                   help="只评估不应用（看看学出来会怎样）")
    s.add_argument("--steps", type=int, default=1, help="回滚几版")
    s.set_defaults(func=cmd_learn)

    return p


def _add_sim_args(s) -> None:
    s.add_argument("--minutes", type=float, default=90.0,
                   help="跑多少模拟分钟（默认 90：够长才稳，30 分钟还在爬坡）")
    s.add_argument("--seed", type=int, default=20260927)
    s.add_argument("--network", default=None, help="realistic / synthetic / abstract")
    s.add_argument("--orders-per-min", type=float, default=1.0, dest="orders_per_min")
    s.add_argument("--riders", type=int, default=None)
    s.add_argument("--set", action="append", metavar="key=value", help="覆盖配置")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        return err(str(e), getattr(args, "json", False), EXIT_USAGE)
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
