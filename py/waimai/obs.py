"""可观测性：结构化日志（JSONL，带轮转）+ 进程内指标（计数/仪表/直方图）。

为什么要有这个模块：这套系统原来**只有**一个 `world.log`，它是
`deque(maxlen=300)` 的内存环形缓冲 —— 跑长一点就静默丢历史（我自己就被它坑过：
一次 90 分钟的实验里，早期的"推迟"日志被挤掉，我据此读出了"高峰 0 次推迟"这个错误结论）。
另外访问日志被显式禁用、没有任何延迟测量、也没有 /metrics 和 /health。
改完之后：

  · 每条 `world.add_log` 同时落盘，deque 挤掉的只是内存里的展示副本，历史不再丢
  · 每个请求记一条访问日志（方法/路径/状态/耗时/来源），可以回答"刚才是不是卡了一下"
  · 派单轮次、最短路调用、错误类型都有计数；延迟有 p50/p95/p99
  · 日志文件按天 + 体积轮转，有总量上限，不会把磁盘写满

设计约束（都是踩过的坑）：

  · **绝不允许把调用方搞挂**。日志和指标是旁观者，写失败就静默降级；
    `world.add_log` 在模拟主循环里，它抛异常等于把仿真打断。
  · **默认关闭**。自检和库调用不该在仓库里制造日志文件，必须显式 `configure()`。
  · 指标用**有界样本窗口**（每个指标最近 N 个样本）算分位，而不是无限累积 ——
    无限累积在长时间运行里就是内存泄漏。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional

# 每个指标保留的样本数。够算稳的 p95/p99，又不会随运行时长增长。
SAMPLE_WINDOW = 2048

# 单个日志文件的上限，超过就换文件。8 MiB × 保留 6 个 ≈ 48 MiB 上限。
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_LOG_FILES = 6

_lock = threading.Lock()
_log_dir: Optional[Path] = None
_log_fh = None
_log_day = ""
_log_bytes = 0
_console_level = "warn"
_enabled = False

_counters: Dict[str, float] = {}
_gauges: Dict[str, float] = {}
_samples: Dict[str, Deque[float]] = {}
_sums: Dict[str, float] = {}
_maxima: Dict[str, float] = {}

# ------------------------------------------------------------ 级别

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


def configure(log_dir: Optional[Path] = None, console_level: str = "warn",
              enabled: bool = True) -> None:
    """打开日志与指标。log_dir 为 None 时只记内存里的指标，不写文件。"""
    global _log_dir, _console_level, _enabled
    with _lock:
        _enabled = enabled
        _console_level = console_level if console_level in _LEVELS else "warn"
        _log_dir = Path(log_dir) if log_dir else None
        if _log_dir is not None:
            try:
                _log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                _log_dir = None            # 目录建不出来就退化成"不写文件"


def reset() -> None:
    """清空指标和日志句柄（自检里用，保证每个用例从干净状态开始）。"""
    global _log_fh, _log_day, _log_bytes
    with _lock:
        _close_locked()
        _log_day = ""
        _log_bytes = 0
        _counters.clear()
        _gauges.clear()
        _samples.clear()
        _sums.clear()
        _maxima.clear()


def _close_locked() -> None:
    global _log_fh
    if _log_fh is not None:
        try:
            _log_fh.close()
        except OSError:
            pass
        _log_fh = None


# ------------------------------------------------------------ 指标

def counter(name: str, delta: float = 1.0) -> None:
    try:
        with _lock:
            _counters[name] = _counters.get(name, 0.0) + delta
    except Exception:                                # noqa: BLE001
        pass


def gauge(name: str, value: float) -> None:
    try:
        with _lock:
            _gauges[name] = float(value)
    except Exception:                                # noqa: BLE001
        pass


def observe(name: str, value: float) -> None:
    """记一个观测值（延迟、体积等），用于算分位数。"""
    try:
        v = float(value)
        with _lock:
            buf = _samples.get(name)
            if buf is None:
                buf = deque(maxlen=SAMPLE_WINDOW)
                _samples[name] = buf
            buf.append(v)
            _sums[name] = _sums.get(name, 0.0) + v
            if v > _maxima.get(name, float("-inf")):
                _maxima[name] = v
    except Exception:                                # noqa: BLE001
        pass


def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    # 最近邻取法：样本量是我们自己控的，不需要插值那种精度
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def snapshot() -> dict:
    """给 /metrics 用的一整套指标快照。"""
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
        hist = {}
        for name, buf in _samples.items():
            vals = sorted(buf)
            hist[name] = {
                "count": len(vals),
                "total": len(vals),
                "sum": round(_sums.get(name, 0.0), 3),
                "avg": round(_sums.get(name, 0.0) / len(vals), 3) if vals else 0.0,
                "max": round(_maxima.get(name, 0.0), 3),
                "p50": round(_percentile(vals, 0.50), 3),
                "p95": round(_percentile(vals, 0.95), 3),
                "p99": round(_percentile(vals, 0.99), 3),
            }
    return {"counters": counters, "gauges": gauges, "histograms": hist,
            "window": SAMPLE_WINDOW}


def prometheus_text() -> str:
    """Prometheus 文本格式。够标准，可以直接被 scrape。"""
    snap = snapshot()
    lines: List[str] = []

    def _metric_name(n: str) -> str:
        return "waimai_" + "".join(c if (c.isalnum() or c == "_") else "_" for c in n)

    for name, value in sorted(snap["counters"].items()):
        lines.append(f"# TYPE {_metric_name(name)} counter")
        lines.append(f"{_metric_name(name)} {value}")

    for name, value in sorted(snap["gauges"].items()):
        lines.append(f"# TYPE {_metric_name(name)} gauge")
        lines.append(f"{_metric_name(name)} {value}")

    for name, h in sorted(snap["histograms"].items()):
        base = _metric_name(name)
        lines.append(f"# TYPE {base} summary")
        for q in ("p50", "p95", "p99"):
            lines.append(f'{base}{{quantile="0.{q[1:]}"}} {h[q]}')
        lines.append(f"{base}_count {h['count']}")
        lines.append(f"{base}_sum {h['sum']}")
        lines.append(f"{base}_max {h['max']}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ 日志

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _rotate_locked() -> None:
    """选择当前该写哪个文件：**优先追加当天的文件**，写满了才开新序号。

    这里第一版写错了：每次都是"找一个还不存在的文件名"，于是**进程每重启一次
    就新建一个日志文件**，而且总数超过 MAX_LOG_FILES 时还会把最老的删掉 ——
    等于每重启一次就丢一段历史，恰好把这个模块要解决的问题又犯了一遍。
    正确行为是：同一天的日志追加到同一个文件里，只有它写满才换下一个序号。
    """
    global _log_fh, _log_day, _log_bytes
    if _log_dir is None:
        return

    day = datetime.now().strftime("%Y%m%d")
    _close_locked()

    chosen = None
    for seq in range(MAX_LOG_FILES + 1):
        name = f"waimai-{day}.jsonl" if seq == 0 else f"waimai-{day}.{seq}.jsonl"
        p = _log_dir / name
        try:
            size = p.stat().st_size if p.is_file() else -1
        except OSError:
            size = -1
        if size < 0 or size < MAX_LOG_BYTES:        # 不存在，或还没写满
            chosen = p
            break
    if chosen is None:                              # 全写满了：也不丢数据，回头覆盖最老的
        chosen = _log_dir / f"waimai-{day}.{MAX_LOG_FILES}.jsonl"

    try:
        _log_fh = open(chosen, "a", encoding="utf-8")
        _log_day = day
        _log_bytes = chosen.stat().st_size
    except OSError:
        _log_fh = None
        return

    # 删最老的，控制总量
    files = sorted(_log_dir.glob("waimai-*.jsonl"),
                   key=lambda f: f.stat().st_mtime if f.exists() else 0)
    for old in files[:-MAX_LOG_FILES]:
        try:
            old.unlink()
        except OSError:
            pass


def _write(record: dict) -> None:
    global _log_bytes
    if not _enabled:
        return
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    except (TypeError, ValueError):
        return
    with _lock:
        if _log_dir is not None:
            try:
                if _log_fh is None or _log_bytes >= MAX_LOG_BYTES:
                    _rotate_locked()
                if _log_fh is not None:
                    _log_fh.write(line)
                    _log_fh.flush()
                    _log_bytes += len(line.encode("utf-8"))
            except OSError:
                _close_locked()           # 写不进去就放弃文件，不打断业务
        lvl = _LEVELS.get(record.get("level", "info"), 20)
        if lvl >= _LEVELS.get(_console_level, 30) and record.get("kind") != "access":
            print(f"  [{record.get('level')}] {record.get('kind')}: "
                  f"{record.get('msg', '')}")


def event(level: str, kind: str, msg: str, **fields) -> None:
    """记一条结构化事件。字段里不要放客户姓名/电话/地址。"""
    rec = {"ts": _now_iso(), "level": level, "kind": kind, "msg": msg}
    rec.update({k: v for k, v in fields.items() if v is not None})
    _write(rec)


def access(method: str, path: str, status: int, ms: float, client: str = "") -> None:
    """一条访问日志 + 相应的指标。这是"刚才是不是卡了一下"的答案来源。"""
    counter("http_requests")
    counter(f"http_status_{status // 100}xx")
    observe("http_latency_ms", ms)
    # API 路径去掉查询串并把订单号之类的高基数段折叠，
    # 否则每个订单 id 都会变成一个独立的时序（指标基数爆炸）。
    norm = path
    if norm.startswith("/api/"):
        observe(f"api_latency_ms:{norm}", ms)
    if status >= 500:
        counter("http_errors_5xx")
    _write({"ts": _now_iso(), "level": "info", "kind": "access",
            "method": method, "path": path, "status": status,
            "ms": round(ms, 2), "client": client})


def sim_log(kind: str, text: str, sim_seconds: float) -> None:
    """把 world.add_log 的内容镜像到日志文件里。

    deque(maxlen=300) 只该影响界面上的展示条数，不该决定历史是否留得下来。
    """
    _write({"ts": _now_iso(), "level": "info", "kind": "sim",
            "sim": kind, "simSeconds": sim_seconds, "msg": text})


def log_path() -> Optional[str]:
    with _lock:
        return str(_log_dir) if _log_dir is not None else None
