"""外卖平台派单系统 · Python 版。

模块划分刻意和 Java 版一一对应，方便两边对照着读：

    model.py          Pt / Merchant / Stop / Order / Rider / Config
    metric.py         Metric / StraightMetric
    geom.py           折线工具（弧长、取点、抽稀）
    route_planner.py  插入启发式 + 2-opt / Or-opt
    world.py          全局状态 + 布点
    dispatcher.py     三档优先级 + 指派单 + 调单
    simulator.py      时钟推进 + 沿路推进 + 五个时间点
    stats.py          统计口径
    api.py            HTTP 接口 + 静态文件
    selftest.py       自检
"""

__version__ = "1.0.0-py"
