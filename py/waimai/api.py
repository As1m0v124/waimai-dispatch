"""HTTP 接口 + 前端静态文件。对应 Java 版 Api。

只用标准库的 http.server —— Java 那边用 com.sun.net.httpserver 也是同样的思路：
不引入任何第三方依赖。

**这一层只做 HTTP**：方法校验、查询串、状态码、静态文件、认证、限流。
业务规则（校验、夹区间、进单判定、序列化）全在 `service.py`，
因为 CLI 和 MCP 两个入口也要用同一套规则 —— 复制一份迟早会不一致。
"""

from __future__ import annotations

import json
import math
import mimetypes
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

from . import learn, obs, security, service
from .llm_advisor import LlmAdvisor
from .llm_config import LlmConfig, provider_of
from .security import TOKEN_HEADER

# 单次下发给前端渲染的路段上限
MAX_RENDER_SEGS = 80_000

# 连接上没有任何动静就断开。慢速发送的请求（slowloris）靠这个兜住：
# 不设超时的话，一个只发一半 header 的连接可以永久占住一个线程。
SOCKET_TIMEOUT_SEC = 30

# 同时在处理的连接上限。超了直接回 503，而不是继续起线程。
MAX_CONCURRENT_REQUESTS = 64

# 注入到首页里的 token 标签：本机访问时服务端把 token 填进去，前端不用自己找。
# 绑非本机地址时不注入（否则等于把 token 发给整个网段），见 _static。
TOKEN_META = '<meta name="waimai-token" content="{token}">'


class BoundedHTTPServer(ThreadingHTTPServer):
    """有并发上限的 HTTP 服务。

    `ThreadingHTTPServer` 每个连接起一个线程、且没有上限：本机任意一个进程
    开几千个连接就能把线程数推上去（就算每个连接最终会超时，峰值也已经吃掉了）。
    这里限一下，超出直接 503 —— 一个本地服务没有理由被这样打。

    计数用"已接纳的 socket 对象集合"来做，而不是信号量：`shutdown_request`
    在拒绝路径上也会被调用，用信号量会多释放一次。集合用 discard 天然幂等。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._inflight = set()
        self._admit_lock = threading.Lock()

    def _admit(self, request) -> bool:
        with self._admit_lock:
            if len(self._inflight) >= MAX_CONCURRENT_REQUESTS:
                return False
            self._inflight.add(request)
            return True

    def _release(self, request) -> None:
        with self._admit_lock:
            self._inflight.discard(request)

    def process_request(self, request, client_address):
        if not self._admit(request):
            obs.counter("http_rejected_overload")
            self._reject_overload(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    @staticmethod
    def _reject_overload(request) -> None:
        """回 503 并**先读掉客户端已发的字节再关**。

        不读就关的话，Windows 上"带着未读的入站数据关闭"会发 RST，
        把刚写出去的 503 一起丢掉 —— 客户端看到的是"连接被重置"，
        而不是一个说清楚原因的 503（我第一次就是这么写的）。
        """
        try:
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                            b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        try:
            request.settimeout(0.5)
            drained = 0
            while drained < 64 * 1024:          # 有界，别被"一直发的客户端"拖住
                chunk = request.recv(4096)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass                                  # 超时或已断开都正常
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._release(request)

    def handle_error(self, request, client_address):
        """客户端断开不算故障，别打 traceback。

        `_read_body` 之前的位置（读请求行、读 header）出的连接类异常够不到
        `_handle` 里的 try，会冒到这里由 socketserver 默认实现打一整段调用栈。
        而"刷新页面/关标签页/扫描器探测"每天都有一堆 —— 日志里全是狼来了，
        真正的错误反而被埋掉。所以这里分类：连接类静默计数，其他照旧抛出。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            obs.counter("http_client_aborts")
            return
        try:
            super().handle_error(request, client_address)
        except Exception:                            # noqa: BLE001
            pass


class Api:
    def __init__(self, world, web_dir: Optional[Path],
                 advisor: Optional[LlmAdvisor] = None,
                 host: str = "127.0.0.1",
                 token: str = "",
                 token_source: str = "",
                 allow_private_llm: bool = False):
        self.world = world
        self.web_dir = web_dir
        self.advisor = advisor if advisor is not None else LlmAdvisor(world, LlmConfig.load())
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.host = host
        self.token = token
        # 只有绑在本机时才把 token 注入页面。绑到 0.0.0.0 还注入，
        # 等于把 token 发给整个局域网，认证就形同虚设了。
        self.inject_token = bool(token) and security.is_loopback_host(host)
        self.token_source = token_source
        self.allow_private_llm = allow_private_llm
        self.started_at = time.time()

    def start(self, port: int) -> ThreadingHTTPServer:
        api = self
        self.port = port

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = SOCKET_TIMEOUT_SEC

            def log_message(self, fmt, *args):        # 访问日志用我们自己的格式，见 _access
                pass

            def _read_body(self):
                """**无论什么请求都要把请求体读干净，并且要先校验长度。**

                这是手写 HTTP 最容易踩的坑：HTTP/1.1 是长连接，
                如果某个分支（比如方法不允许、参数校验失败）直接返回而没读完 body，
                剩下的字节会留在 socket 里，被当成下一个请求的起始行 ——
                于是下一个请求会以 "Unsupported method ('{}POST')" 这种莫名其妙的方式挂掉。

                所以统一在分发之前读掉；而"读多少"必须来自可信的长度，
                否则一个 `Content-Length: -5` 就能让残留字节再次串包。
                """
                try:
                    length = security.parse_content_length(self.headers)
                except security.RequestTooLarge as e:
                    self._raw = b""
                    self._len_error = (413, str(e))
                    return
                except security.BadRequestLen as e:
                    self._raw = b""
                    self._len_error = (400, str(e))
                    return
                self._raw = self.rfile.read(length) if length > 0 else b""

            # ---- GET ----
            def do_GET(self):
                self._read_body()
                api._handle(self, "GET")

            # ---- POST ----
            def do_POST(self):
                self._read_body()
                api._handle(self, "POST")

            def do_OPTIONS(self):
                self._read_body()
                api._send_cors_preflight(self)

        # 默认只绑本机。绑 0.0.0.0 意味着同一个 Wi-Fi 下任何人都能改你的调度参数、
        # 读你的配置 —— 那不该是默认行为。
        self.httpd = BoundedHTTPServer((self.host, port), Handler)
        threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True).start()
        return self.httpd

    # ------------------------------------------------------------ 分发

    def _handle(self, h, method: str):
        t0 = time.perf_counter()
        path = h.path.split("?", 1)[0]
        query = h.path.split("?", 1)[1] if "?" in h.path else ""
        status = 0

        try:
            len_error = getattr(h, "_len_error", None)
            if len_error is not None:
                status = len_error[0]
                self._json(h, status, {"ok": False, "error": len_error[1]})
                return

            # 健康检查不要求认证：它是给探针用的，且只回状态不回数据。
            # 其余 /api/* 一律要 token —— 包括只读接口，因为状态里有客户地址。
            if path == "/api/health":
                status = 200
                self._json(h, 200, self.health())
                return

            if path == "/api/metrics":
                # 指标既是运营信息也是敏感信息（能看出负载与调用量），要 token。
                if not self._authorized(h):
                    status = 401
                    self._json(h, 401, {"ok": False, "error": "缺少或错误的认证 token"})
                    return
                status = 200
                if query.find("format=prometheus") >= 0:
                    data = obs.prometheus_text().encode("utf-8")
                    h.send_response(200)
                    h.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    h.send_header("Cache-Control", "no-store")
                    h.send_header("Content-Length", str(len(data)))
                    h.end_headers()
                    h.wfile.write(data)
                else:
                    self._json(h, 200, {"ok": True, **obs.snapshot(),
                                        "road": self._road_metrics(),
                                        "health": self.health()})
                return

            if path.startswith("/api/"):
                if not self._authorized(h):
                    status = 401
                    self._json(h, 401, {
                        "ok": False,
                        "error": "缺少或错误的认证 token（请求头 "
                                 f"{TOKEN_HEADER}）。token 在 data/api-token，"
                                 "或用环境变量 WAIMAI_TOKEN 指定。"})
                    return
                with self.world.lock:
                    self._api(h, path, method, query)
                status = 200
                return
            if path == "/favicon.ico":
                status = 204
                h.send_response(204)
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            if method != "GET":
                status = 405
                self._json(h, 405, {"ok": False, "error": "不支持的方法"})
                return
            status = self._static(h, path)
        except ValueError as e:
            status = 400
            self._json(h, 400, {"ok": False, "error": str(e)})
        except (ConnectionError, BrokenPipeError, TimeoutError):
            # 客户端中途断开（刷新页面、关标签页、超时）是**正常流量**，不是故障。
            # 以前它会冒到 socketserver 那里打一整段 traceback，日志里全是狼来了，
            # 真正的错误反而被埋掉。这里安静地记一条，客户端已经走了不用再回响应。
            status = 499                                   # 约定：客户端主动断开
            obs.counter("http_client_aborts")
        except Exception as e:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            status = 500
            obs.counter(f"http_errors_5xx:{type(e).__name__}")
            # 不回显异常原文（可能带绝对路径等本机信息），只给一个可供排查的短标记
            self._json(h, 500, {"ok": False,
                                "error": f"服务端错误：{type(e).__name__}"})
        finally:
            self._access(h, method, path, status, (time.perf_counter() - t0) * 1000.0)

    def _authorized(self, h) -> bool:
        given = h.headers.get(TOKEN_HEADER)
        if not given:
            # 也接受 Bearer 形式，方便 curl / 通用客户端
            auth = h.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
        return security.token_matches(self.token, given)

    def _access(self, h, method: str, path: str, status: int, ms: float) -> None:
        """一条紧凑的访问日志。原来是完全禁用的，问题排查起来两眼一抹黑。"""
        try:
            from . import obs
            obs.access(method, path, status, ms, h.client_address[0] if h.client_address else "")
        except Exception:                            # noqa: BLE001
            pass                                     # 日志绝不能反过来把请求搞挂

    def health(self) -> dict:
        """存活 + 就绪。给探针用，不回任何业务数据。"""
        w = self.world
        return {
            "ok": True,
            "uptimeSec": round(time.time() - self.started_at, 1),
            "simClock": w.clock(),
            "simSeconds": w.now(),
            "paused": w.paused,
            "network": w.network_name,
        }

    def _road_metrics(self) -> dict:
        """最短路调用次数。

        `RoadMetric` 早就在自己数 dijkstra_runs / astar_runs 了，但**从来没人调用**
        过那个 stats() —— 于是这些数字白算了。接出来，性能问题才有地方看。
        """
        metric = self.world.metric
        try:
            return dict(metric.stats())
        except Exception:                            # noqa: BLE001
            return {}

    def _api(self, h, path: str, method: str, query: str):
        """HTTP 路由。每个分支都是一句"解析请求 → 调服务层 → 写响应"。

        业务规则一律不在这里 —— 它们住 `service.py`，CLI 和 MCP 调的是同一批函数。
        这一层只负责 HTTP 特有的东西：方法校验、查询串、状态码、响应格式。
        """
        w = self.world

        if path == "/api/state":
            if not self._get(h, method):
                return
            self._json(h, 200, service.state_json(w, self.advisor))

        elif path == "/api/stats":
            # 只要指标、不要订单明细（agent 和监控通常只要这一份）
            if not self._get(h, method):
                return
            self._json(h, 200, service.stats_json(w))

        elif path == "/api/order":
            if not self._post(h, method):
                return
            self._json(h, 200, service.place_order(w, self._body(h)))

        elif path == "/api/order/random":
            if not self._get(h, method):
                return
            self._json(h, 200, service.random_order_sample(w))

        elif path == "/api/order/auto":
            if not self._post(h, method):
                return
            self._json(h, 200, service.auto_orders(w, self._body(h)))

        elif path == "/api/dispatch":
            if not self._post(h, method):
                return
            self._json(h, 200, service.dispatch_now(w))

        elif path == "/api/assign":
            if not self._post(h, method):
                return
            self._json(h, 200, service.assign(w, self._body(h)))

        elif path == "/api/reassign":
            if not self._post(h, method):
                return
            self._json(h, 200, service.reassign(w, self._body(h)))

        elif path == "/api/explain":
            # 这笔单为什么派给了他
            if not self._get(h, method):
                return
            self._json(h, 200, service.explain_order(w, _query_str(query, "orderId", "")))

        elif path == "/api/rider":
            if not self._post(h, method):
                return
            self._json(h, 200, service.rider_settings(w, self._body(h)))

        elif path == "/api/rider/add":
            if not self._post(h, method):
                return
            self._json(h, 200, service.rider_add(w, self._body(h)))

        elif path == "/api/rider/remove":
            if not self._post(h, method):
                return
            self._json(h, 200, service.rider_remove(w, self._body(h)))

        elif path == "/api/control":
            if not self._post(h, method):
                return
            self._json(h, 200, service.apply_control(w, self._body(h)))

        elif path == "/api/reset":
            if not self._post(h, method):
                return
            self._json(h, 200, service.reset(w))

        elif path == "/api/roads":
            if not self._get(h, method):
                return
            self._json(h, 200, self._roads())

        elif path == "/api/networks":
            if not self._get(h, method):
                return
            self._json(h, 200, service.networks_json(w))

        elif path == "/api/network":
            if not self._post(h, method):
                return
            b = self._body(h)
            try:
                # 解析在锁外（服务层保证），这里只处理错误
                self._json(h, 200, service.switch_network(w, service.trim(b.get("id")) or ""))
            except Exception as e:                   # noqa: BLE001
                # 不回显异常原文（OSError 之类会带绝对路径）
                self._json(h, 200, {"ok": False,
                                    "error": f"切换失败（{type(e).__name__}）：{e}"})

        elif path == "/api/learn/status":
            if not self._get(h, method):
                return
            self._json(h, 200, learn.status())

        elif path == "/api/learn/run":
            if not self._post(h, method):
                return
            b = self._body(h)
            evaluate_only = bool(b.get("evaluateOnly"))
            self._json(h, 200, learn.run(apply_low_risk=not evaluate_only))

        elif path == "/api/learn/rollback":
            if not self._post(h, method):
                return
            b = self._body(h)
            self._json(h, 200, learn.rollback(service.opt_int(b.get("steps"), 1, 1, 10)))

        elif path == "/api/zones":
            if not self._get(h, method):
                return
            win = _int_query(query, "window", w.cfg.zone_window_min)
            cells = _int_query(query, "cells", w.cfg.heat_cells)
            self._json(h, 200, service.zones_json(w, win, cells))

        elif path == "/api/llm/status":
            if not self._get(h, method):
                return
            self._json(h, 200, {"ok": True,
                                "status": self.advisor.status_json(),
                                "config": self.advisor.config.public_view()})

        elif path == "/api/llm/analyze":
            if not self._post(h, method):
                return
            b = self._body(h)
            # `windowMin: null` 以前会 Int(None) → TypeError → 500。
            # 缺省、null、非数字一律退回配置值，并把区间夹住（0 表示全部历史）。
            win = service.opt_int(b.get("windowMin"), w.cfg.zone_window_min, 0, 1440)
            cells = service.opt_int(b.get("cells"), w.cfg.heat_cells, 3, 24)
            started = self.advisor.analyze_async(win, cells)
            self._json(h, 200, {"ok": True, "message": "已开始分析…", "state": "RUNNING"}
                       if started else {"ok": False, "error": "上一次分析还在进行中"})

        elif path == "/api/llm/config":
            if not self._post(h, method):
                return
            self._llm_config(h)

        elif path == "/api/llm/clear":
            if not self._post(h, method):
                return
            self.advisor.clear()
            self._json(h, 200, {"ok": True, "message": "已清空分析结果"})

        else:
            self._json(h, 404, {"ok": False, "error": f"未知接口：{path}"})

    # ------------------------------------------------------------ 写操作

    def _static(self, h, path: str) -> int:
        """发一个静态文件。返回给访问日志用的状态码。"""
        p = security.resolve_web_file(self.web_dir, path)
        if p is None:
            self._not_found(h)
            return 404

        try:
            data = p.read_bytes()
        except OSError:
            # 不回显异常原文（里面会带绝对路径）
            self._not_found(h)
            return 404

        rel = "/" + p.name
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"

        # 只有绑在本机时，才把 token 塞进页面，让前端能直接调接口。
        # 绑到 0.0.0.0 时注入 = 把 token 发给整个局域网，所以那种情况不注入，
        # 前端改用 URL 上的 #token= 片段（见 web/app.js）。
        if rel == "/index.html" and self.inject_token:
            html = data.decode("utf-8", errors="replace")
            if "waimai-token" not in html:
                meta = TOKEN_META.format(token=self.token)
                if "<head>" in html:
                    html = html.replace("<head>", "<head>\n  " + meta, 1)
                else:
                    html = meta + "\n" + html
                data = html.encode("utf-8")

        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return 200

    # ------------------------------------------------------------ 小工具

    def _body(self, h) -> dict:
        """解析请求体。原始字节已经在 Handler._read_body 里读掉了。"""
        return security.parse_json_body(getattr(h, "_raw", b""))

    def _cors(self, h) -> None:
        """只在同源时回 CORS 头。

        原来是 `Access-Control-Allow-Origin: *`：任意网页都能驱动本机接口
        （浏览器允许它读响应），配合"baseUrl 可改"就能把 API Key 骗走。
        现在跨站拿不到 CORS 头，浏览器会拦住响应，CSRF 也一并挡掉。
        """
        if security.origin_allowed(h.headers.get("Origin"), self.host, getattr(self, "port", 0)):
            h.send_header("Access-Control-Allow-Origin", h.headers.get("Origin"))
            h.send_header("Vary", "Origin")

    def _json(self, h, code: int, body) -> bytes:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        self._cors(h)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return data

    def _send_cors_preflight(self, h):
        h.send_response(204)
        self._cors(h)
        h.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        h.send_header("Access-Control-Allow-Headers",
                      f"Content-Type, {TOKEN_HEADER}, Authorization")
        h.send_header("Content-Length", "0")
        h.end_headers()

    def _not_found(self, h):
        data = "404 Not Found".encode("utf-8")
        h.send_response(404)
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _llm_config(self, h):
        """保存大模型配置。**Key 只进不出** —— 返回值里只有脱敏提示。"""
        cfg = self.advisor.config
        b = self._body(h)

        provider = service.trim(b.get("provider"))
        if provider:
            p = provider_of(provider)
            cfg.provider_id = p.id
            # 换供应商时，如果没同时指定 baseUrl/model，就用该供应商的默认值
            if "baseUrl" not in b:
                cfg.base_url = p.base_url
            if "model" not in b:
                cfg.model = p.model
        base_url = service.trim(b.get("baseUrl"))
        if base_url is not None:
            # 出口地址校验：analyze 会把 `Authorization: Bearer <Key>` 发到 baseUrl，
            # 不校验就等于"谁都能把 Key 指到自己的服务器"。详见 security 模块。
            #
            # 注意 `_trim` 把空串当成"没传"，所以清空输入框 = 保持原值不变 ——
            # 和 apiKey 的约定一致（留空 = 不改动已存的 Key）。
            # 这是刻意的：清出一条空的 baseUrl 只会让下次请求以难以理解的方式失败。
            try:
                cfg.base_url = security.check_llm_base_url(
                    base_url, allow_private=self.allow_private_llm)
            except security.BlockedTarget as e:
                self._json(h, 200, {"ok": False, "error": str(e)})
                return
        model = service.trim(b.get("model"))
        if model is not None:
            cfg.model = model
        # 只有明确传了 apiKey 才覆盖；传空字符串表示「清空」
        if "apiKey" in b:
            cfg.api_key = (b.get("apiKey") or "").strip()
        if isinstance(b.get("timeoutSec"), (int, float)):
            cfg.timeout_sec = max(10, min(600, int(b["timeoutSec"])))
        if isinstance(b.get("temperature"), (int, float)):
            cfg.temperature = max(0.0, min(2.0, float(b["temperature"])))
        if isinstance(b.get("autoIntervalSec"), (int, float)):
            cfg.auto_interval_sec = max(0, min(3600, int(b["autoIntervalSec"])))

        warning = None
        try:
            cfg.save()
        except OSError as e:
            # 不回显异常原文：里面会带本机的绝对路径
            warning = f"配置已在内存中生效，但写入配置文件失败：{type(e).__name__}"
            self._json(h, 200, {"ok": True, "message": warning,
                                "config": cfg.public_view()})
            return

        with self.world.lock:
            self.world.add_log("系统", f"大模型配置已更新（{cfg.provider_id} / {cfg.model}）")
        self._json(h, 200, {
            "ok": True,
            "message": "大模型配置已保存",
            "config": cfg.public_view(),
        })

    def _roads(self) -> dict:
        """路网几何。给前端一次性取走、画到离屏画布上。

        格式压到最紧：坐标以**分米**为单位的整数，按 [x1,y1,x2,y2, ...] 平铺。
        用 JSON 数组而不是二进制，是为了能用浏览器直接看一眼、调试方便；
        一条线段 4 个整数，几万条线段也就几百 KB，一次性传输完全够用。

        线段太多时只保留最长的一批 —— 这自然会把小巷子滤掉、留下主干道，
        也正是真实地图在低缩放级别下的做法。
        """
        w = self.world
        g = w.graph
        out = {"mode": w.network_mode, "name": w.network_name,
               "w": w.width_m, "h": w.height_m}
        if g is None:
            out.update({"count": 0, "total": 0, "truncated": False, "seg": []})
            return out

        total = g.seg_count
        keep = min(total, MAX_RENDER_SEGS)
        order = list(range(total))
        if keep < total:
            lengths = [
                math.hypot(g.x[g.seg_b[i]] - g.x[g.seg_a[i]],
                           g.y[g.seg_b[i]] - g.y[g.seg_a[i]])
                for i in range(total)
            ]
            order = sorted(range(total), key=lambda i: -lengths[i])[:keep]
            order.sort()

        seg: List[int] = []
        for i in order:
            a, b = g.seg_a[i], g.seg_b[i]
            seg.append(int(round(g.x[a] * 10)))
            seg.append(int(round(g.y[a] * 10)))
            seg.append(int(round(g.x[b] * 10)))
            seg.append(int(round(g.y[b] * 10)))

        out.update({"count": keep, "total": total, "truncated": keep < total, "seg": seg})
        return out

    def _get(self, h, method) -> bool:
        if method == "GET":
            return True
        self._json(h, 405, {"ok": False, "error": "请用 GET"})
        return False

    def _post(self, h, method) -> bool:
        if method == "POST":
            return True
        self._json(h, 405, {"ok": False, "error": "请用 POST"})
        return False


# ------------------------------------------------------------ 模块级小工具

def _int_query(query: str, key: str, default: int) -> int:
    """从 URL 查询串里取一个整数参数；取不到或格式不对就回退默认值。"""
    for part in query.split("&"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k != key:
            continue
        try:
            return int(v)
        except ValueError:
            return default
    return default


def _query_str(query: str, key: str, default: str) -> str:
    """从 URL 查询串里取一个字符串参数（不做百分号解码：订单号是 ASCII）。"""
    for part in query.split("&"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k == key:
            return v
    return default
