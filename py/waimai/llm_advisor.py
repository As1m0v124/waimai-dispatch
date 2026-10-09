"""把区域研判交给大模型做解读，异步执行。对应 Java 版 LlmAdvisor。

**为什么必须异步。** 一次大模型调用要几秒到几十秒。如果同步跑，
而调用又发生在持有 World 锁的时候，整个模拟会被冻住那么久 ——
骑手不动、时钟不走，界面上看起来就是卡死了。
所以这里分两步：先在锁内**只取一份聚合快照**（微秒级），把锁放掉，
再到后台线程里去发请求。状态用普通字段暴露（读写都是原子的），前端轮询取结果。

**发给模型的东西里没有任何个人信息。** 只有按格子聚合过的统计量
（单量、准时率、平均时长、骑手数、缺口），不含顾客姓名、电话、地址、订单号。
这是默认行为，不是可选项 —— 把顾客手机号发给第三方 API 是不能接受的。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import llm_client, zone_analytics
from .llm_client import LlmError
from .llm_config import LlmConfig


class LlmAdvisor:
    """一次分析的产物与状态。"""

    def __init__(self, world, config: Optional[LlmConfig] = None):
        self.world = world
        self.config = config if config is not None else LlmConfig.load()

        self.state = "IDLE"        # IDLE / RUNNING / DONE / ERROR
        self.text = ""
        self.error = ""
        self.elapsed_ms = 0
        self.model = ""
        self.finished_at_sim = 0
        self.window_min = 0
        self.cells = 0

        self._in_flight = threading.Lock()
        self._busy = False

    # ------------------------------------------------------------ 状态

    def running(self) -> bool:
        return self.state == "RUNNING"

    def status_json(self) -> dict:
        return {
            "state": self.state,
            "text": self.text,
            "error": self.error,
            "elapsedMs": self.elapsed_ms,
            "model": self.model,
            "finishedAtSim": self.finished_at_sim,
            "windowMin": self.window_min,
            "cells": self.cells,
            "configured": self.config.configured,
        }

    def clear(self) -> None:
        self.state = "IDLE"
        self.text = ""
        self.error = ""

    # ------------------------------------------------------------ 触发

    def analyze_async(self, window_min: int, cells: int) -> bool:
        """触发一次分析。立刻返回；真正的调用在后台线程里跑。

        返回 False 表示已经有一次在跑了。
        """
        with self._in_flight:
            if self._busy:
                return False
            self._busy = True

        self.state = "RUNNING"
        self.error = ""
        self.text = ""
        self.window_min = window_min
        self.cells = cells
        self.model = self.config.model

        # 关键：只在锁内取快照，拿到就放锁。提示词拼装也放在锁内做，
        # 因为它读的是 World 里的集合，但很快。
        with self.world.lock:
            report = zone_analytics.analyze(self.world, window_min, cells)
            prompt = build_prompt(self.world, report)
            now_sim = self.world.now()

        t0 = time.monotonic()

        def _run():
            try:
                out = llm_client.chat(self.config, SYSTEM_PROMPT, prompt)
                self.text = (out or "").strip()
                self.error = ""
                self.state = "DONE"
            except LlmError as e:
                self.error = str(e)
                self.state = "ERROR"
            except Exception as e:                      # noqa: BLE001
                self.error = f"调用出错：{e}"
                self.state = "ERROR"
            finally:
                self.elapsed_ms = int((time.monotonic() - t0) * 1000)
                self.finished_at_sim = now_sim
                with self._in_flight:
                    self._busy = False

        threading.Thread(target=_run, name="llm-advisor", daemon=True).start()
        return True


# ------------------------------------------------------------ 提示词

SYSTEM_PROMPT = """你是一名外卖平台的运力调度分析师。你会收到一份按网格聚合的实时运营数据。
你的任务是帮助平台管理判断：哪些区域需要增加骑手运力，哪些区域单量偏低、
可以安排骑手轮休，从而在不影响准时率的前提下降低站点运营成本。

要求：
1. 先给一句总体结论（当前运力是偏紧、平衡还是过剩）。
2. 用「需要增派」「可以轮休」两组列出具体区域。每个区域要说清楚：
   格子位置（第几行第几列或坐标范围）、依据的数据（单量/准时率/压力值）、
   建议的人数和理由。不要只复述数字，要给出判断。
3. 指出风险与例外：比如准时率不达标但单量不大的格子、压力值高但没有骑手的格子。
4. 最后给 2~4 条可执行的调度建议。
5. 在数据不足的地方要明确说「数据不足」，不要编造结论。
6. 输出用中文，Markdown 格式，控制在 600 字以内，不要寒暄。
"""


def build_prompt(world, report: zone_analytics.Report) -> str:
    """把研判报告拼成一段紧凑的文本。**只含聚合数据，不含任何顾客信息。**"""
    from . import stats as stats_mod

    g = stats_mod.of(world)
    lines = []

    lines.append("【运行概况】")
    lines.append(f"距离模型：{world.metric.name}")
    lines.append(f"模拟时钟：{world.clock()}（已运行 {report.hours_elapsed:.1f} 小时）")
    window = f"最近 {report.window_min} 分钟" if report.window_min > 0 else "本次模拟全部历史"
    lines.append(f"统计窗口：{window}")
    lines.append(f"订单总数：{g['totalOrders']}，已送达 {g['delivered']}，"
                 f"在途 {g['inFlight']}，订单池待派 {g['pooled']}")
    lines.append(f"准时率：{g['onTimeRate']}%（口径 {g['slaMinutes']} 分钟）")
    lines.append(f"平均总时长：{g['avgTotalMin']} 分钟，平均等派单 {g['avgWaitDispatchMin']} 分钟，"
                 f"平均配送 {g['avgOnRoadMin']} 分钟")
    lines.append(f"骑手：在线 {report.riders_online} 人，负载 {g['riderLoad']}/{g['riderCapacity']}"
                 f"（{g['riderUtilization']}%），总里程 {g['riderTotalKm']} km")
    lines.append(f"派单档位：顺路 {g['tier1OnRoute']} / 无单 {g['tier2Idle']} / "
                 f"兜底 {g['tier3Fallback']}（顺路占比 {g['onRouteShare']}%）")

    lines.append("")
    lines.append("【运力模型】")
    if report.throughput_per_rider_hour is not None:
        if report.throughput_method == "little":
            how = "Little 法则 —— 同时在手上的单量 ÷ 每单服务时长（派单到送达，不含订单池排队），不受开局爬坡影响"
        else:
            how = "窗口内实际送达量 ÷ 骑手数 ÷ 小时数，开局阶段会偏低"
        lines.append(f"观测到的单骑手产能：{report.throughput_per_rider_hour} 单/小时（估法：{how}）")
    else:
        lines.append("观测到的单骑手产能：数据不足（在途和已送达样本都太少，无法估算）")
    lines.append(f"全网需求：{report.total_demand_per_hour} 单/小时，"
                 f"据此需要骑手约 {report.global_need_riders} 人")
    lines.append(f"合计建议：增派 {report.suggest_add} 人，可轮休 {report.suggest_rest} 人")

    lines.append("")
    lines.append("【分区域数据】")
    lines.append(f"网格 {report.cells_x} 列 × {report.cells_y} 行，每格约 {report.cell_m} 米。"
                 f"坐标为「第几列,第几行」，原点在西南角。")
    lines.append("字段：格子 | 窗口内单量 | 在途 | 已送达 | 准时率 | 平均总时长 | "
                 "需求单/时 | 投入骑手 | 缺口 | 判定")
    for z in report.zones:
        otr = "—" if z.on_time_rate is None else f"{_fmt(z.on_time_rate)}%"
        avg = "—" if z.delivered == 0 else f"{_fmt(z.avg_total_min)}分"
        lines.append(f"({z.col},{z.row}) | {z.orders} | {z.active} | {z.delivered} | {otr} | "
                     f"{avg} | {_fmt(z.demand_per_hour)} | {z.serving_riders} | "
                     f"{z.delta:+d} | {zone_analytics.verdict_label(z.verdict)}")

    lines.append("")
    lines.append("重要：判定分两类，处方完全不同 ——")
    lines.append("· 「需要增派」= 需求超过运力，或本区有单却没人服务 → 应该加人。")
    lines.append("· 「准时率偏低」= 运力其实够（压力不高甚至建议撤人），但准时率不达标，")
    lines.append("  多半是等派单排队太久或商家出餐慢 → 应该查调度/商家，不要往这里加骑手。")
    lines.append("· 「缺口」为正表示建议增派人数，为负表示可撤下的人数。")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "—"
    return str(int(v)) if float(v) == int(v) else str(round(float(v), 1))
