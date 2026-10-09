"""MCP 集成测试：真的起一个子进程，走完整的 JSON-RPC 往返。

不做 mock —— mock 掉的正是最该验证的东西（stdio 传输、协议握手、
stdout 有没有被日志污染）。每一条都对应一种真实的失效方式。
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "py" / "waimai" / "mcp_server.py"

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


class Mcp:
    """一个 MCP 客户端：按行发 JSON-RPC，按行读回复。"""

    def __init__(self, *extra):
        self.p = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SCRIPT), *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0

    def send(self, method, params=None, notify=False):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            msg["id"] = self._id
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("服务端没有回复就退出了")
        return json.loads(line)

    def call(self, name, args=None):
        r = self.send("tools/call", {"name": name, "arguments": args or {}})
        if "result" not in r:
            return {"_rpc_error": r.get("error")}
        text = r["result"]["content"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text, "isError": r["result"].get("isError")}

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:                                       # noqa: BLE001
            self.p.kill()
        err = self.p.stderr.read()
        return err


print("== MCP 集成测试 ==")
print("  ... 握手与清单")
m = Mcp()
try:
    r = m.send("initialize", {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": "test", "version": "0"}})
    check("initialize 有回应", "result" in r, str(r)[:160])
    res = r.get("result", {})
    check("回应里有协议版本", res.get("protocolVersion") in
          ("2025-06-18", "2025-03-26", "2024-11-05"), str(res.get("protocolVersion")))
    check("回应里有 serverInfo", res.get("serverInfo", {}).get("name") == "waimai-dispatch")
    check("声明了 tools 能力", "tools" in (res.get("capabilities") or {}))

    # 客户端报一个我们不认识的版本 → 回我们自己的（规范允许的协商）
    r = m.send("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    check("不认识的协议版本回退到我们支持的",
          r.get("result", {}).get("protocolVersion") in
          ("2025-06-18", "2025-03-26", "2024-11-05"))

    m.send("notifications/initialized", notify=True)         # 通知：不应有回复
    r = m.send("ping")
    check("ping 有回应", "result" in r)

    r = m.send("tools/list")
    tools = r.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    check("工具清单非空", len(tools) >= 15, f"count={len(tools)}")
    for t in tools:
        if not (t.get("name") and t.get("description") and t.get("inputSchema")):
            check(f"工具 {t.get('name')} 三要素齐全", False)
            break
    else:
        check("每个工具都有 name/description/inputSchema", True)
    required = {"sim_status", "get_stats", "get_state", "sim_advance", "place_order",
                "dispatch_now", "list_riders", "explain_dispatch", "get_config",
                "set_config", "set_intake", "reset", "run_experiment", "compare_configs",
                "add_rider", "remove_rider", "set_rider", "assign_order", "reassign_order",
                "list_orders", "place_orders", "get_zones"}
    missing = required - names
    check("关键工具都在", not missing, f"缺 {missing}")

    print("  ... 只读工具")
    st = m.call("sim_status")
    check("sim_status 返回时钟", st.get("sim", {}).get("clock"), str(st)[:120])
    check("sim_status 带指标", "stats" in st)
    st = m.call("get_stats")
    check("get_stats 有统计", "stats" in st and "onTimeRate" in st["stats"])
    st = m.call("list_riders")
    check("list_riders 有 10 个骑手", len(st.get("riders", [])) == 10, str(len(st.get("riders", []))))
    cfg = m.call("get_config")
    check("get_config 有参数", "slaMinutes" in (cfg.get("config") or {}))
    z = m.call("get_zones")
    check("get_zones 有网格", "zones" in z or "cellsX" in z, str(z)[:120])

    print("  ... 改状态")
    r = m.call("place_order", {"name": "测试", "phone": "13900000000", "address": "测试路1号"})
    check("place_order 成功", r.get("ok") is True, str(r)[:160])
    oid = r.get("orderId")
    check("拿到订单号", bool(oid))
    r = m.call("dispatch_now")
    check("dispatch_now 派出了单", r.get("assigned", 0) >= 1, str(r)[:120])
    r = m.call("explain_dispatch", {"orderId": oid})
    check("explain_dispatch 能解释", r.get("ok") is True and r.get("why"), str(r)[:160])
    check("解释里有档位与时间线", r.get("tierLabel") and r.get("timeline"))
    r = m.call("add_rider", {"name": "测试骑手"})
    check("add_rider 成功", r.get("ok") is True, str(r)[:160])
    new_id = r.get("riderId")
    r = m.call("set_rider", {"riderId": new_id, "maxOrders": 3})
    check("set_rider 成功", r.get("ok") is True, str(r)[:160])
    r = m.call("remove_rider", {"riderId": new_id})
    check("remove_rider 成功", r.get("ok") is True, str(r)[:160])
    r = m.call("set_config", {"set": {"slaMinutes": 33}})
    check("set_config 成功", r.get("ok") is True, str(r)[:160])
    check("参数真的改了", m.call("get_config")["config"]["slaMinutes"] == 33)
    r = m.call("set_config", {"set": {"nosuchKey": 1}})
    check("不认识的参数名被报出来（不是静默忽略）",
          "nosuchKey" in json.dumps(r, ensure_ascii=False), str(r)[:160])
    r = m.call("set_intake", {"action": "off"})
    check("set_intake off 成功", r.get("ok") is True, str(r)[:160])
    r = m.call("place_order", {"name": "x", "phone": "1", "address": "y"})
    check("停单后下单被拒", r.get("ok") is False, str(r)[:160])
    m.call("set_intake", {"action": "auto"})

    print("  ... 时间推进与实验")
    r = m.call("sim_advance", {"minutes": 20})
    check("sim_advance 推进了时间", r.get("advancedMinutes") == 20, str(r)[:120])
    check("推进后时钟前进了", r.get("sim", {}).get("clock"))
    r = m.call("sim_advance", {"minutes": 0})
    check("minutes=0 被拒", r.get("ok") is False)
    r = m.call("run_experiment", {"minutes": 30, "ordersPerMin": 1})
    check("run_experiment 出 KPI", r.get("ok") is True and "stats" in r, str(r)[:160])
    check("实验不带机器相关的 perf", "perf" not in r)
    r2 = m.call("run_experiment", {"minutes": 30, "ordersPerMin": 1})
    check("同参数实验可复现（逐字节一致）",
          json.dumps(r, sort_keys=True) == json.dumps(r2, sort_keys=True))
    r = m.call("run_experiment", {"minutes": 10, "set": {"typoKey": 1}})
    check("实验里拼错的参数会被报出来", r.get("ignoredParams"), str(r)[:160])

    print("  ... 对照实验")
    c = m.call("compare_configs", {
        "minutes": 60, "seed": 20260927,
        "base": {"postponePoorAssignments": False},
        "variant": {"postponePoorAssignments": True},
        "labelBase": "不推迟", "labelVariant": "推迟",
    })
    check("compare_configs 有对比表", len(c.get("comparison", [])) >= 5, str(c)[:160])
    check("对比里有结论", bool(c.get("verdict")), str(c)[:200])
    check("对比声明了同种子同场景", "同一随机种子" in (c.get("note") or ""))
    route = next((r for r in c["comparison"] if r["metric"] == "onRouteShare"), None)
    check("对比包含顺路占比", route is not None)
    if route:
        check("推迟让顺路占比变好（实测结论）",
              route["variant"] > route["base"],
              f"base={route['base']} variant={route['variant']}")

    print("  ... 安全与容错")
    r = m.call("reset", {})
    check("reset 不带 confirm 被拒", r.get("ok") is False, str(r)[:120])
    r = m.call("reset", {"confirm": True})
    check("reset 带 confirm 通过", r.get("ok") is True, str(r)[:120])
    r = m.send("tools/call", {"name": "no_such_tool", "arguments": {}})
    check("不存在的工具返回 JSON-RPC 错误", "error" in r, str(r)[:120])
    r = m.send("no/such/method", {})
    check("不存在的方法返回错误", "error" in r, str(r)[:120])
    r = m.call("explain_dispatch", {"orderId": "WM99999"})
    check("查不存在的订单是业务错误而不是崩溃", r.get("ok") is False, str(r)[:120])
    r = m.call("get_state", {"orderLimit": 3})
    check("get_state 的 orderLimit 生效", len(r.get("orders", [])) <= 3)

    print("  ... stdout 未被污染")
    r = m.call("sim_status")
    check("大量工具调用之后协议仍然正常", "sim" in r, str(r)[:120])
finally:
    err = m.close()
    check("服务端没有往 stderr 打 traceback", "Traceback" not in err,
          err[-300:] if "Traceback" in err else "")

# 带 --network 启动（PyCharm 的 MCP 运行配置就是这么带的）。
#
# 这个分支以前**没被测过**，而它恰好是用户点绿三角会走的路径：
# `--network` 里有一句函数级的 `from .world import NetworkMode`，
# 以脚本方式运行时 __package__ 为空 → ImportError，一启动就崩。
# 教训是"参数分支必须有测试走到"，否则写的时候自测一遍就以为好了。
print("  ... 带 --network 启动（脚本方式，和 PyCharm 运行配置一致）")
m_net = Mcp("--network", "realistic")
try:
    r = m_net.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    check("--network 模式能完成握手", "result" in r, str(r)[:200])
    r = m_net.call("sim_status")
    check("--network 模式用的是那块路网",
          (r.get("network") or {}).get("name") == "模拟城区", str(r.get("network"))[:120])
    check("--network 模式下世界规模正确",
          (r.get("network") or {}).get("nodes", 0) > 100, str(r.get("network"))[:120])
finally:
    err = m_net.close()
    check("--network 模式也没有 traceback", "Traceback" not in err,
          err[-300:] if "Traceback" in err else "")

print()
print("  ... 接管模式（--url）")
live = None
try:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    live = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "py" / "waimai" / "main.py"),
         "--port", str(port), "--speed", "20"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            break
        except Exception:                                       # noqa: BLE001
            time.sleep(0.5)

    m2 = Mcp("--url", f"http://127.0.0.1:{port}")
    try:
        m2.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        r = m2.call("sim_status")
        check("接管模式能读到那个实例的状态", "sim" in r, str(r)[:160])
        r = m2.call("place_order", {"name": "接管测试", "phone": "1", "address": "路1"})
        check("接管模式能往那个实例下单", r.get("ok") is True, str(r)[:160])
        r = m2.call("sim_advance", {"minutes": 5})
        check("接管模式拒绝手动推进时间（并说明原因）",
              r.get("ok") is False and "沙箱" in json.dumps(r, ensure_ascii=False),
              str(r)[:200])
        r = m2.call("run_experiment", {"minutes": 15})
        check("接管模式下实验仍然可用（另开沙箱）", r.get("ok") is True, str(r)[:160])
    finally:
        m2.close()
finally:
    if live:
        live.terminate()
        try:
            live.wait(timeout=10)
        except Exception:                                       # noqa: BLE001
            live.kill()

print()
if failed:
    print(f"{passed} 项通过，{failed} 项失败")
    raise SystemExit(1)
print(f"全部 {passed} 项通过")
