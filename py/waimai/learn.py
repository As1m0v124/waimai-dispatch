"""学习器：从落盘的样本里学出可用的东西，并**只在真的更好时才应用**。

三个学习器，都是统计学方法，没有黑箱，也没有假装是"AI"：

| 学什么 | 输入 | 用在哪 | 风险 | 自动应用? |
| --- | --- | --- | --- | --- |
| 等派单时长 | 决策记录里的负载上下文 + 实际等待 | 预计送达时间（ETA）、超时预警 | 低（只影响预测） | 是 |
| 骑行速度 | 骑行观测（距离, 时长, 类型） | ETA 里的在路上部分 | 低（只影响预测） | 是 |
| 推迟策略 | 带探索标记的决策 + 结果 | 建议每档负载下要不要推迟 | **高**（会改变调度行为） | **否，只出建议** |

**为什么学的不是"骑行速度"本身**：每辆车的速度在系统里本来就是已知的
（`rider.speed_mpm`，280~360 米/分）。真正不知道的是**排队要等多久** ——
实测同一套参数下，闲时平均等 1.5 分钟，忙时能到 40 分钟。
ETA 的主要误差就来自这一项，所以等派单模型才是值得学的那个。
骑行速度仍然统计，但它的作用是**校验假设**（对不上说明模型有问题），
而不是"学一个新参数"。

**"只在更好时才应用"不是口号**：每个自动应用的模型都要在**留出集**上
和当前值比，不更优就不应用，并在报告里说清"没应用，因为没好"。
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import obs, paths, telemetry

MODEL_FILE = "model.json"
# 保留多少个历史版本，用于回滚。每次应用都追加一版。
HISTORY_KEEP = 10

# 应用门禁：留出集上至少要相对改善这么多才算"更好"。
# 设成 0 会让噪声也被当成进步（来回抖）；太大则永远学不动。
MIN_RELATIVE_GAIN = 0.02

# 负载分档的边界（负载率）。分三档够了：
# 前面实测过"低负载推迟是负优化、中负载推迟明显有用"，
# 分太细会让每档样本不足，学出来的东西全是噪声。
LOAD_BUCKETS = ((0.0, 0.45, "低"), (0.45, 0.8, "中"), (0.8, 1.01, "高"))

# 样本不足时的态度：**报"数据不足"而不是编一个数出来**
MIN_SAMPLES = 30
MIN_BUCKET_SAMPLES = 8


# ------------------------------------------------------------ 模型存取

def _model_path():
    return paths.under_data("learn", MODEL_FILE)


# 模型缓存。
#
# 为什么必须有：`order_json` 里每笔订单都要算 ETA，而 ETA 要读模型 ——
# 不加缓存就是**每笔订单一次磁盘读 + json 解析**。`/api/state` 每 500ms 发一次、
# 每次带几百笔订单，于是每秒几千次文件读取。
# 实测后果很隐蔽：跑 5 个模拟小时，准时率从 100% 慢慢掉到 87.6%，
# 而骑手利用率只有 66%（说明不是运力不够）—— 慢在解析上，不在算法上。
# 用 (路径, mtime, 大小) 做缓存键：改了就重读，没改就用内存里的。
_model_cache: Optional[dict] = None
_model_key: Optional[tuple] = None


def _cache_key():
    p = _model_path()
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), 0, 0)


def invalidate_cache() -> None:
    global _model_cache, _model_key
    _model_cache = None
    _model_key = None


def load_model() -> dict:
    global _model_cache, _model_key
    key = _cache_key()
    if _model_cache is not None and key == _model_key:
        return _model_cache
    p = _model_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"version": 0, "models": {}, "history": []}
    _model_cache, _model_key = data, key
    return data


def save_model(model: dict, reason: str) -> None:
    """写模型文件，并把上一版压进 history（可回滚）。"""
    model = dict(model)
    model["version"] = int(model.get("version", 0)) + 1
    model["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    model["reason"] = reason
    history = list(model.get("history") or [])
    history.insert(0, {"version": model["version"], "at": model["updatedAt"],
                       "reason": reason, "models": model.get("models", {})})
    model["history"] = history[:HISTORY_KEEP]
    p = _model_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
        invalidate_cache()          # 写完之后缓存必须失效，否则读到的还是旧的
    except OSError as e:
        obs.event("warn", "learn", f"模型写入失败：{type(e).__name__}")


def _policy_reason(has_enough: bool, insufficient: List[str], policy: dict) -> str:
    if not has_enough:
        return (f"探索样本不足（{policy['exploredSamples']} 条；"
                f"每档每个动作至少要 {MIN_BUCKET_SAMPLES} 条）。"
                f"策略学习需要随机探索产生的对照样本：把 exploreEpsilon 调大一点"
                f"（比如 0.3）再跑几段 —— 注意探索比例越高，"
                f"当前的服务水准受影响越大。")
    reason = "部分档位已有足够样本，建议见 suggestion"
    if insufficient:
        reason += "；这些档位还不够：" + "、".join(insufficient)
    # 必须说清这一点：策略类改动不会自动生效
    reason += "。策略类改动**不会自动生效**，需要人确认。"
    return reason


def _advantage_text(row: dict) -> str:
    """把两条臂的对比说成人话。

    注意措辞方向：`improvement` 是"推迟相对立刻派"的时长变化，
    负数表示推迟更慢。第一版直接写成"改善 -34.2%"，读起来像"改善了 34%"，
    实际是"更差了 34%" —— 符号和词必须同时对。
    """
    imp = row.get("improvement") or 0.0
    p, a = row["medianPostponeSec"], row["medianAssignSec"]
    who = "推迟" if imp > 0 else "立刻派"
    return (f"推迟中位 {p:.0f}s vs 立刻派 {a:.0f}s，"
            f"{who}更快（{abs(imp) * 100:.0f}%），"
            f"n={row['nPostpone']}/{row['nAssign']}")


def rollback(steps: int = 1) -> dict:
    """回滚到上一版。学习环必须能撤销 —— 否则没人敢让它自动应用。"""
    m = load_model()
    hist = list(m.get("history") or [])
    idx = max(0, min(len(hist) - 1, steps - 1))
    if not hist:
        return {"ok": False, "error": "没有可回滚的历史版本"}
    target = hist[idx]
    restored = dict(m)
    restored["models"] = target.get("models", {})
    restored["history"] = hist[idx + 1:]
    restored["rolledBackTo"] = target.get("version")
    save_model(restored, f"回滚到第 {target.get('version')} 版")
    return {"ok": True, "rolledBackTo": target.get("version"),
            "reason": target.get("reason")}


# ------------------------------------------------------------ 1. 等派单时长

def _load_bucket(load: float) -> str:
    for lo, hi, name in LOAD_BUCKETS:
        if lo <= load < hi:
            return name
    return LOAD_BUCKETS[-1][2]


def _split(ds: List[dict], train_ratio: float) -> Tuple[List[dict], List[dict]]:
    """按订单号做确定性划分：同一批数据每次划分结果一致，便于复现比较。

    用 zlib.crc32 而不是内置 hash() —— 后者受 PYTHONHASHSEED 影响，
    同一个模型在不同进程里会被分到不同的留出集上，指标就不可比了。
    """
    import zlib
    train, hold = [], []
    for d in ds:
        key = f"{d.get('runId')}/{d.get('orderId')}"
        h = zlib.crc32(key.encode("utf-8")) % 100
        (train if h < train_ratio * 100 else hold).append(d)
    return train, hold


def fit_wait_model(ds: List[dict]) -> dict:
    """按 (负载档, 池深档) 估计等派单时长（秒）。

    用的是分档中位数而不是线性回归：样本量不大、关系明显非线性
    （池子一超过空闲运力就雪崩），分档中位数更稳也更好解释 ——
    出问题能直接说"高负载+池深 5 单时中位数是 8 分钟"。
    """
    buckets: Dict[str, List[float]] = {}
    for d in ds:
        wait = d.get("waitSec")
        ctx = d.get("ctx") or {}
        if wait is None or wait < 0:
            continue
        key = f"{_load_bucket(float(ctx.get('load') or 0))}|{min(5, int(ctx.get('pool') or 0))}"
        buckets.setdefault(key, []).append(float(wait))

    table = {}
    for k, vals in buckets.items():
        if len(vals) >= 3:                      # 少于 3 个样本的中位数没有意义
            table[k] = {"medianSec": round(statistics.median(vals), 1),
                        "n": len(vals)}
    return {"table": table, "samples": sum(len(v) for v in buckets.values())}


def evaluate_wait_model(ds: List[dict], model: dict,
                        baseline: Optional[dict] = None) -> dict:
    """在留出集上比 MAE：新模型 vs 基线（基线 = 全局中位数，也就是"不看上下文"）。"""
    hold = [d for d in ds if (d.get("waitSec") or 0) >= 0]
    if len(hold) < MIN_SAMPLES:
        return {"ok": False, "reason": f"留出样本不足（{len(hold)} < {MIN_SAMPLES}）"}

    all_waits = [float(d["waitSec"]) for d in hold]
    base_pred = statistics.median(all_waits)
    table = (model or {}).get("table", {})
    base_table = (baseline or {}).get("table", {})

    def pred_for(d, tbl, fallback):
        ctx = d.get("ctx") or {}
        key = f"{_load_bucket(float(ctx.get('load') or 0))}|{min(5, int(ctx.get('pool') or 0))}"
        row = tbl.get(key)
        return float(row["medianSec"]) if row else fallback

    err_new = [abs(pred_for(d, table, base_pred) - float(d["waitSec"])) for d in hold]
    err_old = ([abs(pred_for(d, base_table, base_pred) - float(d["waitSec"])) for d in hold]
               if base_table else [abs(base_pred - float(d["waitSec"])) for d in hold])

    mae_new = sum(err_new) / len(err_new)
    mae_old = sum(err_old) / len(err_old)
    gain = (mae_old - mae_new) / mae_old if mae_old > 0 else 0.0
    return {"ok": True, "n": len(hold), "maeNew": round(mae_new, 1),
            "maeOld": round(mae_old, 1), "gain": round(gain, 4),
            "better": gain >= MIN_RELATIVE_GAIN,
            "baseline": "全局中位数" if not base_table else "上一版模型"}


# ------------------------------------------------------------ 2. 骑行速度校验

def fit_speed(ds: List[dict]) -> dict:
    """从骑行观测里估每类的实际速度（米/秒）。按距离加权 ——
    长距离的样本更能代表"巡航速度"，短腿里上下楼、找店的时间占比大。"""
    out = {}
    for kind in ("to_store", "to_customer"):
        rows = [r for r in ds if r.get("kind") == kind and (r.get("durationSec") or 0) > 0]
        if not rows:
            continue
        dist = sum(float(r["distanceM"]) for r in rows)
        dur = sum(float(r["durationSec"]) for r in rows)
        if dur <= 0:
            continue
        mps = dist / dur
        speeds = [float(r["mps"]) for r in rows if r.get("mps")]
        out[kind] = {
            "mps": round(mps, 3),
            "mpm": round(mps * 60, 1),
            "n": len(rows),
            "medianMps": round(statistics.median(speeds), 3) if speeds else None,
            "avgDistanceM": round(dist / len(rows), 1),
        }
    return out


# ------------------------------------------------------------ 3. 推迟策略

def _context_key(d: dict) -> Optional[str]:
    ctx = d.get("ctx") or {}
    if ctx.get("load") is None:
        return None
    return _load_bucket(float(ctx["load"]))


def fit_postpone_policy() -> dict:
    """学"每档负载下该不该推迟"。

    **只看被探索过的样本**，而且要看**整笔订单的决策历史**，不是只看最后一条。

    为什么必须这样（第一版两个坑都踩了）：
      · 正常运行时策略固定，被推迟的单和没被推迟的单不是同一批单
        （本来就难派的才被推迟）。直接比较会把选择偏差当成"推迟有害"。
        随机探索让"推迟/不推迟"在相同上下文中随机分配，对照才无偏。
      · 一笔单会被评估很多轮（推迟过的尤其如此），所以同一个 orderId 会有
        多条决策记录。只看最后一条的话，"探索出来的那次推迟"根本没被记进去 ——
        实测探索了 68 次却只看到 18 条样本，就是因为记录被后一轮覆盖了。
        这里改成按订单聚合：发生过"探索导致的推迟"就算推迟臂，
        发生过"探索导致的立刻派"就算派单臂。
    """
    # 订单 → 这条单在决策历史里出现过哪些探索动作
    arms: Dict[tuple, str] = {}
    for d in telemetry.records(telemetry.KIND_DECISION):
        if not d.get("explored"):
            continue
        key = (d.get("runId"), d.get("orderId"))
        action = "postpone" if d.get("postponed") else "assign"
        # 两种都出现过（先被探索推迟、后来被探索派出）就不算干净样本，丢掉
        prev = arms.get(key)
        if prev is None:
            arms[key] = action
        elif prev != action:
            arms[key] = "mixed"

    per_ctx: Dict[str, Dict[str, List[float]]] = {}
    explored = len(arms)
    for o in telemetry.records(telemetry.KIND_OUTCOME):
        key = (o.get("runId"), o.get("orderId"))
        action = arms.get(key)
        if action not in ("postpone", "assign"):
            continue
        total = o.get("totalSec")
        if total is None:
            continue
        d = _first_decision(key)
        ctx = _context_key(d) if d else None
        if ctx is None:
            continue
        per_ctx.setdefault(ctx, {"postpone": [], "assign": []})[action].append(float(total))

    table = {}
    for ctx, acts in per_ctx.items():
        p, a = acts["postpone"], acts["assign"]
        if len(p) < MIN_BUCKET_SAMPLES or len(a) < MIN_BUCKET_SAMPLES:
            table[ctx] = {"insufficient": True, "nPostpone": len(p), "nAssign": len(a)}
            continue
        mp, ma = statistics.median(p), statistics.median(a)
        # 总时长越短越好（顾客等得少）；差异小于 5% 视为没有实际差别
        rel = (ma - mp) / ma if ma > 0 else 0.0
        best = "postpone" if rel > 0.05 else ("assign" if rel < -0.05 else "either")
        table[ctx] = {"insufficient": False, "nPostpone": len(p), "nAssign": len(a),
                      "medianPostponeSec": round(mp, 1),
                      "medianAssignSec": round(ma, 1),
                      "improvement": round(rel, 4), "best": best}
    return {"table": table, "exploredSamples": explored,
            "samples": sum(len(v["postpone"]) + len(v["assign"]) for v in per_ctx.values())}


_DECISION_INDEX: Optional[Dict[tuple, dict]] = None


def _first_decision(key: tuple) -> Optional[dict]:
    """某笔单的第一条决策记录（上下文以"当时的处境"为准，不用后来的）。"""
    global _DECISION_INDEX
    if _DECISION_INDEX is None:
        idx: Dict[tuple, dict] = {}
        for d in telemetry.records(telemetry.KIND_DECISION):
            k = (d.get("runId"), d.get("orderId"))
            prev = idx.get(k)
            if prev is None or (d.get("t") or 0) < (prev.get("t") or 0):
                idx[k] = d
        _DECISION_INDEX = idx
    return _DECISION_INDEX.get(key)


def _reset_index() -> None:
    """样本变化后要重建索引（学习器每次 run 之前调）。"""
    global _DECISION_INDEX
    _DECISION_INDEX = None


# ------------------------------------------------------------ 一次完整学习

def run(apply_low_risk: bool = True, directory=None) -> dict:
    """跑一次学习：拟合 → 留出评估 → 按风险决定是否应用 → 写模型与报告。

    返回的字典就是给人/agent 看的报告：每个模型**学没学会、有没有应用、为什么**。
    """
    if directory is not None:
        telemetry.configure(directory)

    _reset_index()                     # 样本可能刚变过，索引要重建
    ds_joined = telemetry.joined_samples()
    ds_legs = telemetry.records(telemetry.KIND_LEG)
    ds_decisions = telemetry.records(telemetry.KIND_DECISION)

    model = load_model()
    models = dict(model.get("models") or {})
    report: Dict[str, dict] = {}

    # ---- 等派单时长（低风险，可自动应用）----
    if len(ds_joined) < MIN_SAMPLES:
        report["wait"] = {"ok": False,
                          "reason": f"可训练样本不足（{len(ds_joined)} < {MIN_SAMPLES}），"
                                    f"先去跑几段模拟攒样本",
                          "applied": False}
    else:
        # 按订单号做**确定性随机划分**，而不是按时间前后切。
        #
        # 按时间切是错的：样本来自好几段不同负载的模拟，时间靠后的那段
        # 负载和前面完全不同，于是"留出集"其实换了个工况 —— 模型再好也会
        # 显得更差（我第一版就是这么切的，结果 MAE 384 → 386 秒，
        # 差点据此认为这套模型没用）。随机划分让两边覆盖同样的工况，
        # 比的才是"这个模型预测得准不准"。
        train, hold = _split(ds_joined, 0.7)
        fitted = fit_wait_model(train)
        ev = evaluate_wait_model(hold, fitted, models.get("wait"))
        applied = False
        if ev.get("ok") and ev.get("better") and apply_low_risk:
            models["wait"] = fitted
            applied = True
        report["wait"] = {
            "ok": bool(ev.get("ok")),
            "trainSamples": len(train), "holdoutSamples": len(hold),
            "evaluation": ev, "applied": applied,
            "buckets": len(fitted["table"]),
            "reason": ("留出集上更准，已应用" if applied else
                       ("留出集上没有更好，保留原模型" if ev.get("ok")
                        else ev.get("reason"))),
        }

    # ---- 骑行速度（低风险：只做校验与 ETA 用）----
    speed = fit_speed(ds_legs)
    if speed:
        prev = models.get("speed") or {}
        moved = any(
            k in prev and prev[k].get("mps")
            and abs(speed[k]["mps"] - prev[k]["mps"]) / prev[k]["mps"] > 0.05
            for k in speed if k in prev
        )
        report["speed"] = {
            "ok": True, "fit": speed, "applied": bool(apply_low_risk),
            "changed": moved,
            "reason": "统计实际速度，用于 ETA 与校验假设（发现变化会标 changed）",
        }
        if apply_low_risk:
            models["speed"] = speed
    else:
        report["speed"] = {"ok": False, "reason": "没有骑行观测样本", "applied": False}

    # ---- 推迟策略（高风险：只出建议）----
    policy = fit_postpone_policy()
    insufficient = [k for k, v in policy["table"].items() if v.get("insufficient")]
    has_enough = any(not v.get("insufficient") for v in policy["table"].values())
    suggestion = None
    if has_enough:
        suggestion = {ctx: v["best"] for ctx, v in policy["table"].items()
                      if not v.get("insufficient")}
    report["postponePolicy"] = {
        "ok": bool(has_enough),
        "applied": False,
        "requiresConfirmation": True,
        "suggestion": suggestion,
        "table": policy["table"],
        "exploredSamples": policy["exploredSamples"],
        "reason": _policy_reason(has_enough, insufficient, policy),
    }

    if apply_low_risk:
        save_model({**model, "models": models}, "学习环自动应用低风险模型")
    else:
        save_model({**model, "models": models},
                   "学习环运行（未应用，仅评估）")

    return {
        "ok": True,
        "applied": {k: v.get("applied") for k, v in report.items()},
        "report": report,
        "modelVersion": load_model().get("version"),
        "data": {"joinedSamples": len(ds_joined), "legs": len(ds_legs),
                 "decisions": len(ds_decisions)},
        "note": "预测类模型在留出集上更优才自动应用；策略类只出建议，需确认。",
    }


# ------------------------------------------------------------ 消费模型

def get_speed_mps(kind: str, default: float = 5.3) -> float:
    """ETA 用的速度（米/秒）。没有模型时用默认值（约 320 米/分）。"""
    m = load_model().get("models", {}).get("speed", {})
    row = m.get(kind)
    if row and row.get("mps"):
        try:
            v = float(row["mps"])
            # 明显不合理的值不用：样本太少时可能拟合出荒唐的速度
            return v if 1.0 <= v <= 30.0 else default
        except (TypeError, ValueError):
            return default
    return default


def predict_wait_sec(world) -> Optional[float]:
    """按当前上下文预测"新单要等多久才被派出去"。没有模型就返回 None。"""
    m = load_model().get("models", {}).get("wait", {})
    table = m.get("table") or {}
    if not table:
        return None
    ctx = telemetry.load_context(world)
    key = f"{_load_bucket(float(ctx['load']))}|{min(5, int(ctx['pool']))}"
    row = table.get(key)
    return float(row["medianSec"]) if row else None


def eta_seconds(world, order) -> Optional[float]:
    """预计这单还要多久送到（秒）：排队 + 去商家 + 出餐等待 + 去顾客。

    这就是"学到的模型被真正用上"的地方 —— 它的精度直接决定
    `/api/state` 里 `etaMinutes` 和超时预警准不准。
    """
    if order.status.name == "DELIVERED":
        return 0.0
    metric = world.metric
    rider = world.riders.get(order.rider_id) if order.rider_id else None

    wait = 0.0
    if order.dispatched_at == 0:
        p = predict_wait_sec(world)
        if p is None:
            return None                      # 没有模型就不编一个数出来
        wait = p

    speed = get_speed_mps("to_store")
    to_store = 0.0
    if order.arrived_store_at == 0:
        origin = rider.pos if rider is not None else order.merchant_pt
        to_store = metric.metres(origin, order.merchant_pt) / speed

    prep = max(0.0, order.ready_at - world.now()) if order.picked_at == 0 else 0.0

    to_customer = 0.0
    if order.delivered_at == 0:
        origin = order.merchant_pt
        if rider is not None and order.picked_at > 0:
            origin = rider.pos
        to_customer = metric.metres(origin, order.dest_pt) / get_speed_mps("to_customer")

    return wait + to_store + prep + to_customer


def status() -> dict:
    """学习环现在什么样：有多少样本、学到什么、最后什么时候更新过。"""
    m = load_model()
    t = telemetry.stats()
    models = m.get("models") or {}
    return {
        "ok": True,
        "modelVersion": m.get("version", 0),
        "updatedAt": m.get("updatedAt"),
        "reason": m.get("reason"),
        "historyVersions": [h.get("version") for h in (m.get("history") or [])],
        "learned": {
            "wait": {"buckets": len((models.get("wait") or {}).get("table") or {}),
                     "applied": bool(models.get("wait"))},
            "speed": {k: v.get("mpm") for k, v in (models.get("speed") or {}).items()},
        },
        "data": t["counts"],
        "joinedSamples": t["samples"],
        "readyToLearn": t["samples"] >= MIN_SAMPLES,
        "minSamples": MIN_SAMPLES,
        "privacy": "样本只含坐标/订单号/时间/决策特征/结果，不含姓名、电话、地址。",
    }
