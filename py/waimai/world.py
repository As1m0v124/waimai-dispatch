"""全局状态：城市、商家、骑手、订单、订单池、模拟时钟、派单配置。对应 Java 版 World。"""

from __future__ import annotations

import math
import random
import threading
import zlib
from collections import deque
from typing import Deque, Dict, List

from . import obs
from .metric import Metric, StraightMetric
from .model import Config, Merchant, Order, OrderStatus, Pt, Rider

# 抽象城市模式下的默认边长（米）
CITY = 10_000.0

# 模拟时钟的起点：10:00:00。
# 刻意不从 0 开始 —— 时间戳字段用 0 表示「还没发生」，
# 如果时钟也从 0 起步，这两个含义就撞在一起了。
START_SECONDS = 10 * 3600


class OrderLimitReached(RuntimeError):
    """累计订单达到 cfg.max_total_orders，不再接单。

    单独定义异常类型，是为了让上层能把它和"参数写错了"区分开：
    调用方该回一个"到上限了"的业务错误，而不是 500 或者 400。
    """


def merchant_prep_sec(name: str) -> int:
    """按店名推一个稳定的出餐时间（5~10 分钟）。

    出餐时间名义上是店家属性，OSM 里没有，只能从店名推。关键是**必须稳定**：

    这里**不能**用内置 `hash()` —— 字符串 hash 受 `PYTHONHASHSEED` 影响，
    每个进程都不一样。于是同一个种子、同一块路网，换个进程跑出来的备餐时间就变了，
    实验不可复现（agent 做对照实验必须"两次运行只差一个变量"）。
    `crc32` 是固定算法，跨进程、跨机器都一致。自检里把这个等式钉死了。
    """
    return 300 + (zlib.crc32(name.encode("utf-8")) % 7) * 60


class NetworkMode:
    """路网来源。用普通类而不是 Enum，因为要带上中文标签。"""
    ABSTRACT = "ABSTRACT"
    ROAD = "ROAD"
    SYNTHETIC = "SYNTHETIC"

    LABELS = {ABSTRACT: "抽象城市", ROAD: "真实路网", SYNTHETIC: "合成路网"}

    @staticmethod
    def label(mode: str) -> str:
        return NetworkMode.LABELS.get(mode, mode)


def format_clock(sec: int) -> str:
    """模拟秒 → 时钟字符串。"""
    t = max(0, int(sec))
    return f"{(t // 3600) % 24:02d}:{(t // 60) % 60:02d}:{t % 60:02d}"


class World:
    """全局状态。"""

    SURNAMES = "张王李赵陈刘杨黄周吴徐孙马朱胡郭何林"
    GIVEN = ["伟", "静", "强", "敏", "浩", "涛", "悦", "迪", "磊", "洋",
             "丽", "娜", "军", "勇", "艳", "杰"]
    DISTRICTS = ["阳光", "锦绣", "和平", "长虹", "望江", "青枫", "金桂", "银河", "紫竹", "春江"]
    BUILDINGS = ["小区", "花园", "公寓", "大厦", "家园", "苑"]
    NOTES = ["", "", "", "不要辣", "多放辣", "少放盐", "多加一份米饭",
             "不要葱", "餐具两份", "放门口，敲门", "请电话联系", "到了放前台", "微辣即可"]

    def __init__(self, seed: int = 20260927):
        # 记住种子：reset() 要能用它把随机流也一起复原，
        # 否则"重置"两次得到的不是同一个世界（第二次用的是已经跑过的随机流）。
        self.seed = seed
        self.rng = random.Random(seed)

        # 模拟线程和 HTTP 线程都要改状态，用一把大锁互斥 —— 和 Java 版
        # synchronized (world) 的作用一样。好处是永远读到一致快照，
        # 代价是请求处理期间会阻塞模拟，所以持有它的时候绝不能做慢操作
        # （比如调用大模型）。
        self.lock = threading.RLock()

        self.merchants: Dict[str, Merchant] = {}
        self.orders: Dict[str, Order] = {}
        self.riders: Dict[str, Rider] = {}

        # 订单池：已下单、还没派出去的订单号，先进先出
        self.pool: Deque[str] = deque()

        # 派单日志：(模拟秒, 类型, 文本)
        self.log: Deque[tuple] = deque(maxlen=300)

        self.cfg = Config()

        # ---- 世界范围（米）。抽象模式是正方形；路网模式是路网 bbox 的实际宽高 ----
        self.width_m = CITY
        self.height_m = CITY

        self.network_mode = NetworkMode.ABSTRACT
        self.network_id = "abstract"          # 当前加载的是哪一块路网（供界面标记"当前"）
        self.network_name = "抽象城市 10km × 10km"
        self.network_nodes = 0
        self.network_edges = 0

        # 距离/路径的算法实现。换成路网就是图上的最短路。
        self.metric: Metric = StraightMetric()
        # 路网模式下生效的图；抽象模式为 None
        self.graph = None
        self.pois: list = []

        # ---- 时钟 ----
        self.sim_seconds: float = float(START_SECONDS)
        self.paused = False
        self.speed_factor = 20.0
        self.last_dispatch_at = START_SECONDS
        self.dispatch_rounds = 0
        self.next_auto_order_at = START_SECONDS

        # 停单只记一次日志，免得刷屏
        self.intake_logged = False

        # 每次重置换一个 run_id。订单号会从头开始，落盘的训练数据要靠
        # (run_id, order_id) 才能唯一定位一笔订单（否则两次运行的 WM00001 会撞车）。
        self._run_seq = 0
        self.run_id = "r0"

        self._order_seq = 0

    # ------------------------------------------------------------ 时钟

    def now(self) -> int:
        return int(self.sim_seconds)

    def clock(self) -> str:
        return format_clock(self.now())

    def next_dispatch_in(self) -> int:
        nxt = self.last_dispatch_at + self.cfg.dispatch_interval_sec
        return max(0, nxt - self.now())

    # ------------------------------------------------------------ 停单 / 进单

    def current_order_count(self) -> int:
        """当前还在系统里流转的订单数（待派单 + 配送中），不含已送达。"""
        return sum(1 for o in self.orders.values()
                   if o.status is not OrderStatus.DELIVERED)

    def intake_status(self) -> dict:
        """当前能不能进单，以及为什么。

        判定只在这一个地方做，模拟器的自动下单和 API 的顾客下单都调它 ——
        两处各写一遍迟早会出现「自动下单停了但手动还能下」这种不一致。
        """
        cfg = self.cfg
        # 待派单数按**状态**数，不按订单池队列的长度。
        # 派单器实际遍历的就是「状态为 POOLED 的单」，所以状态才是口径；
        # 队列只是它的存储。两者万一不一致（比如中途改了状态），
        # 按队列数会算出一个和实际派单情况对不上的占比。
        pooled = sum(1 for o in self.orders.values()
                     if o.status is OrderStatus.POOLED)
        inflight = sum(1 for o in self.orders.values()
                       if o.status in (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP))
        current = pooled + inflight
        ratio = (pooled / current) if current > 0 else 0.0

        status = {
            "open": True,
            "manualOff": not cfg.accept_orders,
            "autoStopped": False,
            "reason": "",
            "pooled": pooled,
            "poolQueue": len(self.pool),
            "inFlight": inflight,
            "current": current,
            "poolRatio": round(ratio, 4),
            "thresholdRatio": cfg.stop_accept_pool_ratio,
            "minOrders": cfg.stop_accept_min_orders,
            "totalOrders": len(self.orders),
            "maxTotalOrders": cfg.max_total_orders,
            "atOrderCap": len(self.orders) >= cfg.max_total_orders,
        }

        if not cfg.accept_orders:
            status["open"] = False
            status["reason"] = "管理平台已手动关闭进单"
            return status

        # 有一单都没派出去过的时候，不能就认定「池子在积压」。
        # 刚重置完、或者离第一次派单还没到点，池子里全是待派单、在途为 0，
        # 占比必然是 100% —— 但这只是派单还没跑，不是处理不过来。
        #
        # 唯一的例外是「一个有余量的骑手都没有」：那时候积压是真的，
        # 等下去也好不了，就该停单。
        riders_free = sum(1 for r in self.riders.values() if r.can_take())
        status["ridersFree"] = riders_free
        backlog_evidence = inflight > 0 or riders_free == 0

        # 低于最小单量不触发 —— 否则开局 1 单待派、0 单在途，占比 100% 直接锁死
        if (current >= cfg.stop_accept_min_orders and ratio >= cfg.stop_accept_pool_ratio
                and backlog_evidence):
            status["open"] = False
            status["autoStopped"] = True
            status["reason"] = (
                f"待派单占比 {ratio * 100:.0f}% 已达停单阈值 "
                f"{cfg.stop_accept_pool_ratio * 100:.0f}%"
                f"（待派 {pooled} / 在系统 {current}），系统自动停单")
            return status

        return status

    def can_accept_order(self) -> bool:
        return self.intake_status()["open"]

    # ------------------------------------------------------------ 订单

    def next_order_id(self) -> str:
        self._order_seq += 1
        return f"WM{self._order_seq:05d}"

    def place_order(self, name: str, phone: str, address: str, note: str,
                    merchant: Merchant, dest: Pt) -> Order:
        """下单：生成订单号 → 进订单池。订单总量受 cfg.max_total_orders 约束。"""
        if len(self.orders) >= self.cfg.max_total_orders:
            # 自动下单一直在守这个上限，手动下单却没有 —— 于是反复调
            # /api/order 就能把内存和 /api/state 的响应撑到任意大。
            # 上限属于系统级保护，两条入口都得认。
            raise OrderLimitReached(
                f"累计订单已达上限 {self.cfg.max_total_orders} 单，"
                "请先重置或调大 maxTotalOrders")
        order = Order.create(self.next_order_id(), name, phone, address, note,
                             merchant, dest, self.now())
        self.orders[order.id] = order
        self.pool.append(order.id)
        order.event(self.now(), "下单", "顾客下单，进入订单池")
        return order

    def in_pool(self, order_id: str) -> bool:
        return order_id in self.pool

    def remove_from_pool(self, order_id: str) -> None:
        try:
            self.pool.remove(order_id)
        except ValueError:
            pass

    def add_log(self, kind: str, text: str) -> None:
        self.log.appendleft((self.now(), kind, text))
        # 同时落盘。deque(maxlen=300) 只该决定界面展示多少条，
        # 不该决定历史是否留得下来 —— 跑长跑时"早期日志被挤掉"直接导致过错误结论
        # （一次 90 分钟实验里我据此读出了"高峰 0 次推迟"）。
        # obs 内部保证不抛异常：它在模拟主循环里被调用，抛出来就等于打断仿真。
        obs.sim_log(kind, text, self.now())

    def active_orders(self) -> List[Order]:
        """正在配送中（已派单未送达）的订单，按派单时间排序。"""
        out = [o for o in self.orders.values()
               if o.status in (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP)]
        out.sort(key=lambda o: o.dispatched_at)
        return out

    def pooled_orders(self) -> List[Order]:
        """订单池里的订单，按顾客下单先后排队。"""
        out = []
        for oid in self.pool:
            o = self.orders.get(oid)
            if o is not None and o.status is OrderStatus.POOLED:
                out.append(o)
        out.sort(key=lambda o: o.created_at)
        return out

    def recent_orders(self, limit: int) -> List[Order]:
        ordered = sorted(self.orders.values(), key=lambda o: o.created_at, reverse=True)
        return ordered[:limit]

    # ------------------------------------------------------------ 初始化

    @staticmethod
    def seeded(seed: int = 20260927) -> "World":
        w = World(seed)
        w.seed_entities()
        return w

    def reset(self) -> None:
        """清空一切，回到初始状态。

        重置后应当与"同种子的新进程"完全一致，所以随机流也一起复原 ——
        不复原的话第二次 seed_entities() 用的是已经消耗过的随机流，
        同一个种子两次重置会得到不同的世界，实验就没法复现了。

        **不重置 speed_factor**：它是"怎么跑"而不是"跑什么"。
        以前这里硬写回 20.0，结果是 `run-py.ps1 -Speed 200` 之后再点一次重置，
        倍速就悄悄变回 20 —— loadcheck.ps1 正是"先设 speed 再 reset"的顺序，
        所以它的 -Speed 参数一直是失效的（20 倍速跑完了整轮压测）。

        每次重置换一个 run_id：订单号会从 WM00001 重新开始，
        而落盘的数据要靠 (run_id, order_id) 才能唯一定位一笔订单。
        """
        self.merchants.clear()
        self.orders.clear()
        self.riders.clear()
        self.pool.clear()
        self.log.clear()
        self.rng = random.Random(self.seed)
        # 先自增再赋值：初始世界是 r0，第一次重置必须是 r1。
        # 反过来写（先赋值后自增）的话第一次重置会拿到 r0，
        # 和初始世界撞号，落盘的数据就没法区分这两段了。
        self._run_seq += 1
        self.run_id = f"r{self._run_seq}"
        self._order_seq = 0
        self.sim_seconds = float(START_SECONDS)
        self.paused = False
        self.last_dispatch_at = START_SECONDS
        self.dispatch_rounds = 0
        self.next_auto_order_at = START_SECONDS
        self.intake_logged = False
        self.seed_entities()

    def seed_entities(self) -> None:
        if self.network_mode != NetworkMode.ABSTRACT and self.graph is not None:
            self._seed_on_network()
        else:
            self._seed_abstract_city()

    # ------------------------------------------------------------ 切换路网

    def load_network(self, graph, pois, mode: str, network_id: str = "") -> None:
        """切换到一块路网：重建整个世界（商家、骑手、订单池）。

        商家优先用 OSM 里真实的餐饮店名，用最远点采样挑尽量分散的若干家；
        没有 POI 数据就退回在路网上均匀撒点、用合成店名。
        """
        from .road_metric import RoadMetric

        self.graph = graph
        self.pois = pois or []
        self.metric = RoadMetric(graph)
        self.network_mode = mode
        self.network_id = network_id or graph.name
        self.network_name = graph.name
        self.network_nodes = graph.node_count
        self.network_edges = graph.edge_count
        self.width_m = max(200.0, graph.width_m)
        self.height_m = max(200.0, graph.height_m)
        self.reset()

    def load_abstract(self) -> None:
        """回到抽象城市模式。"""
        self.graph = None
        self.pois = []
        self.metric = StraightMetric()
        self.network_mode = NetworkMode.ABSTRACT
        self.network_id = "abstract"
        self.network_name = "抽象城市 10km × 10km"
        self.network_nodes = 0
        self.network_edges = 0
        self.width_m = CITY
        self.height_m = CITY
        self.reset()

    def _seed_on_network(self) -> None:
        """在一张真实/合成路网上布商家和骑手。"""
        rng = self.rng
        metric = self.metric
        merchant_count = 10
        rider_count = 10

        # 商家：优先用 OSM 里真实的餐饮店名
        usable = [p for p in self.pois if p.pt is not None and p.pt.x >= 0 and p.pt.y >= 0]
        if len(usable) >= merchant_count:
            # 最远点采样，挑尽量分散的若干家，别都挤在一条街上
            picked = [usable[rng.randrange(len(usable))]]
            while len(picked) < merchant_count:
                best, best_d = None, -1.0
                for p in usable:
                    d = min(p.pt.straight(q.pt) for q in picked)
                    if d > best_d:
                        best_d, best = d, p
                if best is None or best in picked:
                    break
                picked.append(best)

            for i, p in enumerate(picked, start=1):
                mid = f"M{i}"
                self.merchants[mid] = Merchant(mid, p.name, p.pt, merchant_prep_sec(p.name))

        # POI 不够（或没有）就在路网上均匀撒点，用合成店名
        if not self.merchants:
            spots = metric.spread_nodes(merchant_count, rng)
            names = [
                ("湘味小炒", 600), ("兰州拉面", 420), ("沙县小吃", 360),
                ("蜜雪冰城", 300), ("肯德基", 480), ("老乡鸡", 540),
                ("华莱士", 420), ("瑞幸咖啡", 300), ("杨国福麻辣烫", 420),
                ("真功夫", 480),
            ]
            for i, spot in enumerate(spots):
                name, prep = names[i % len(names)]
                mid = f"M{i + 1}"
                self.merchants[mid] = Merchant(mid, name, spot, prep)

        # 骑手：在路网上分散布点
        rider_names = [
            ("R1", "王磊", "13800000001"), ("R2", "李静", "13800000002"),
            ("R3", "张强", "13800000003"), ("R4", "赵敏", "13800000004"),
            ("R5", "陈浩", "13800000005"), ("R6", "周涛", "13800000006"),
            ("R7", "孙悦", "13800000007"), ("R8", "吴迪", "13800000008"),
            ("R9", "郑凯", "13800000009"), ("R10", "冯雪", "13800000010"),
        ]
        spots = metric.spread_nodes(rider_count, rng)
        for i, spot in enumerate(spots):
            if i >= len(rider_names):
                break
            rid, name, phone = rider_names[i]
            rider = Rider(rid, name, phone, spot, self.cfg.default_max_orders)
            rider.speed_mpm = 280 + rng.randrange(5) * 20
            self.riders[rid] = rider

        self.next_auto_order_at = START_SECONDS
        for _ in range(self.cfg.warmup_orders):
            self.auto_place_order()

    def _seed_abstract_city(self) -> None:
        """抽象正方形城市：固定的商家和骑手坐标。

        用字面量而不是随机生成，是为了让自检里的期望值（1400m / 1680m 那种）
        可以手算验证。
        """
        rng = self.rng

        merchants = [
            ("M1", "湘味小炒", 4600, 5000, 600),
            ("M2", "兰州拉面", 5100, 4600, 420),
            ("M3", "沙县小吃", 4200, 5500, 360),
            ("M4", "蜜雪冰城", 5600, 5200, 300),
            ("M5", "肯德基", 3600, 4200, 480),
            ("M6", "老乡鸡", 6000, 4400, 540),
            ("M7", "华莱士", 4900, 6200, 420),
            ("M8", "瑞幸咖啡", 6300, 5800, 300),
        ]
        for mid, name, x, y, prep in merchants:
            self.merchants[mid] = Merchant(mid, name, Pt(x, y), prep)

        riders = [
            ("R1", "王磊", "13800000001", 4600, 5300),
            ("R2", "李静", "13800000002", 5200, 4700),
            ("R3", "张强", "13800000003", 4000, 5000),
            ("R4", "赵敏", "13800000004", 5800, 5600),
            ("R5", "陈浩", "13800000005", 3500, 4000),
            ("R6", "周涛", "13800000006", 6200, 4600),
            ("R7", "孙悦", "13800000007", 4800, 6300),
            ("R8", "吴迪", "13800000008", 3000, 5800),
            ("R9", "郑凯", "13800000009", 5400, 6200),
            ("R10", "冯雪", "13800000010", 3800, 3600),
        ]
        for rid, name, phone, x, y in riders:
            rider = Rider(rid, name, phone, Pt(x, y), self.cfg.default_max_orders)
            rider.speed_mpm = 280 + rng.randrange(5) * 20
            self.riders[rid] = rider

        self.next_auto_order_at = START_SECONDS
        for _ in range(self.cfg.warmup_orders):
            self.auto_place_order()

    # ------------------------------------------------------------ 顾客信息

    def random_customer(self) -> tuple:
        rng = self.rng
        name = rng.choice(self.SURNAMES) + rng.choice(self.GIVEN)
        phone = "13" + str(100000000 + rng.randrange(900000000))
        addr = (rng.choice(self.DISTRICTS) + rng.choice(self.BUILDINGS)
                + str(rng.randrange(20) + 1) + "栋"
                + str(rng.randrange(30) + 1) + "0" + str(rng.randrange(9) + 1))
        return name, phone, addr, rng.choice(self.NOTES)

    def random_dest_near(self, merchant_pt: Pt, radius: float) -> Pt:
        """顾客地址：在商家周围一定范围内随机撒点。"""
        # 路网模式：必须落在路网节点上。否则吸附出来的连接段会是一条脱离道路的直线，
        # 而且「沿路走」的里程也会把那段凭空多出来的距离算进去。
        from .road_metric import RoadMetric
        if isinstance(self.metric, RoadMetric):
            return self.metric.random_node_near(merchant_pt, radius, self.rng)

        rng = self.rng
        inset = 200.0
        max_x = max(inset * 2, self.width_m - inset)
        max_y = max(inset * 2, self.height_m - inset)
        for _ in range(40):
            ang = rng.random() * 2 * math.pi
            dist = radius * (0.25 + rng.random() * 0.75)
            x = merchant_pt.x + math.cos(ang) * dist
            y = merchant_pt.y + math.sin(ang) * dist
            if inset <= x <= max_x and inset <= y <= max_y:
                return Pt(x, y)
        return Pt(
            max(inset, min(max_x, merchant_pt.x + rng.gauss(0, 1) * radius * 0.5)),
            max(inset, min(max_y, merchant_pt.y + rng.gauss(0, 1) * radius * 0.5)),
        )

    def auto_place_order(self) -> Order:
        """造一笔自动订单。"""
        merchant = self.rng.choice(list(self.merchants.values()))
        name, phone, addr, note = self.random_customer()
        return self.place_order(name, phone, addr, note,
                                merchant, self.random_dest_near(merchant.pt, 2600))
