"""读 OpenStreetMap XML（.osm / .osm.xml）。对应 Java 版 OsmParser。

**只支持 XML，不支持 .osm.pbf。** PBF 要 protobuf + deflate，跟本项目
「零第三方依赖」的原则冲突。XML 版本到处都有：Geofabrik 的 .osm.bz2 解压后就是，
Overpass 也可以直接返回 [out:xml]。

用标准库的 xml.etree.ElementTree.iterparse 做流式解析 —— 它是**边读边丢**的，
所以几百 MB 的省级文件也能只占很小内存。当然更省事的做法是用 bbox 先裁一块出来。

解析规则：
  * 只保留可骑行的 highway；人行道、台阶、自行车道、步行街、施工中路段一律丢掉 ——
    电动车不能走，也不该走。
  * 处理 oneway（含 -1 反向）与 junction=roundabout；
    motorway 按 OSM 惯例默认单行，除非显式 oneway=no。
  * 只保留最大弱连通分量，避免图里出现一大片互相不可达的碎片。
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .model import Pt
from .road_graph import RawEdge, RoadGraph

# 能骑的 highway 值
RIDABLE: Set[str] = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "service", "living_street", "road",
    "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
}

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


@dataclass
class Poi:
    """一个餐饮兴趣点。"""

    name: str
    lat: float
    lon: float
    pt: Optional[Pt] = None


@dataclass
class Bbox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon


@dataclass
class Result:
    graph: Optional[RoadGraph] = None
    nodes_read: int = 0
    ways_read: int = 0
    ways_dropped_by_class: int = 0
    ways_clipped: int = 0
    nodes_in_bbox: int = 0
    edges_built: int = 0
    component_dropped: int = 0
    poi_found: int = 0
    pois: List[Poi] = field(default_factory=list)
    # 这块图是「内置生成出来的」还是「从 .osm 文件读出来的」。
    # 由 loader 负责填 —— 只有它知道图是从哪来的。调用方别再自己拿名字猜：
    # 那份硬编码名单一旦漏了新的内置路网，生成路网就会被标成「真实路网」，
    # 界面上的标签就开始骗人。
    synthetic: bool = False


# ------------------------------------------------------------ 入口

def parse_file(path: Path, clip: Optional[Bbox] = None) -> Result:
    """从文件解析并建图。"""
    with open(path, "rb") as f:
        return parse(f, clip, path.stem, f"文件 {path.name}")


def parse(stream, clip: Optional[Bbox], name: str, source: str) -> Result:
    """从任意二进制流解析（自检里用它喂内存里的 XML，不需要落盘）。"""
    res = Result()
    node_lat_lon: List[Optional[Tuple[float, float]]] = []
    id_to_idx: Dict[int, int] = {}
    edges: List[RawEdge] = []

    _read_xml(stream, clip, res, node_lat_lon, id_to_idx, edges)

    # 只保留最大弱连通分量
    n = len(node_lat_lon)
    keep = _largest_component(n, edges)
    remap = [-1] * n
    kept_lat_lon: List[Optional[Tuple[float, float]]] = []
    for i in range(n):
        if keep[i]:
            remap[i] = len(kept_lat_lon)
            kept_lat_lon.append(node_lat_lon[i])
        else:
            remap[i] = -1
            res.component_dropped += 1

    kept_edges: List[RawEdge] = []
    for e in edges:
        a, b = remap[e.frm], remap[e.to]
        if a < 0 or b < 0:
            continue
        kept_edges.append(RawEdge(a, b))
    res.edges_built = len(kept_edges)

    graph = RoadGraph.build(name, source, kept_lat_lon, kept_edges)
    res.graph = graph

    # POI 的投影坐标要等 graph 建好（投影原点那时才确定）
    for p in res.pois:
        p.pt = graph.proj.to_metres(p.lat, p.lon)
    res.poi_found = len(res.pois)
    return res


# ------------------------------------------------------------ XML

def _read_xml(stream, clip, res: Result, node_lat_lon, id_to_idx, edges) -> None:
    # 安全性：ElementTree 不解析外部实体，也不支持 DTD 实体展开，
    # 所以喂不可信文件不会造成 XXE 或本地文件读取。
    in_node = False
    in_way = False
    node_id = node_lat = node_lon = None
    node_tags: Dict[str, str] = {}
    way_nodes: List[int] = []
    way_tags: Dict[str, str] = {}

    for event, elem in ET.iterparse(stream, events=("start", "end")):
        tag = elem.tag
        if event == "start":
            if tag == "node":
                in_node = True
                node_id = _int_attr(elem, "id")
                node_lat = _float_attr(elem, "lat")
                node_lon = _float_attr(elem, "lon")
                node_tags = {}
            elif tag == "way":
                in_way = True
                way_nodes = []
                way_tags = {}
            elif tag == "nd" and in_way:
                ref = elem.get("ref")
                if ref is not None:
                    way_nodes.append(int(ref))
                else:
                    # Overpass 的 `out geom` 会把坐标内联在 <nd> 上，没有独立 <node>。
                    # 这种文件里用坐标合成一个 id —— 取负数，保证不会和真实 OSM id（正数）撞。
                    lat = _float_attr(elem, "lat")
                    lon = _float_attr(elem, "lon")
                    synth = -(int(round(lat * 1e7)) * 200_000_003 + int(round(lon * 1e7)))
                    if clip is None or clip.contains(lat, lon):
                        if synth not in id_to_idx:
                            id_to_idx[synth] = len(node_lat_lon)
                            node_lat_lon.append((lat, lon))
                    way_nodes.append(synth)
            elif tag == "tag":
                k, v = elem.get("k"), elem.get("v")
                if k is not None:
                    if in_way:
                        way_tags[k] = v or ""
                    elif in_node:
                        node_tags[k] = v or ""

        else:  # end
            if tag == "node" and in_node:
                in_node = False
                res.nodes_read += 1
                if clip is None or clip.contains(node_lat, node_lon):
                    id_to_idx[node_id] = len(node_lat_lon)
                    node_lat_lon.append((node_lat, node_lon))
                    res.nodes_in_bbox += 1
                    if _is_food(node_tags):
                        nm = node_tags.get("name")
                        res.pois.append(Poi(
                            nm.strip() if nm and nm.strip() else "无名餐馆",
                            node_lat, node_lon))
                node_tags = {}
                elem.clear()
            elif tag == "way" and in_way:
                in_way = False
                res.ways_read += 1
                if not _is_ridable(way_tags):
                    res.ways_dropped_by_class += 1
                else:
                    _emit_edges(way_nodes, id_to_idx, edges, way_tags, res)
                way_tags = {}
                elem.clear()


def _emit_edges(way_nodes: List[int], id_to_idx: Dict[int, int],
                out: List[RawEdge], tags: Dict[str, str], res: Result) -> None:
    """把一条 way 的节点序列变成有向边。

    落在 bbox 外的节点会把 way 切断：只在「连续落在 bbox 内」的片段之间连边，
    这样裁剪不会在边界上凭空造出一条穿过区外的近路。
    """
    direction = oneway_sign(tags)         # 1 正向单行，-1 反向单行，0 双向
    run: List[int] = []
    clipped = False

    for i in range(len(way_nodes) + 1):
        idx = id_to_idx.get(way_nodes[i]) if i < len(way_nodes) else None
        if idx is None:
            if i < len(way_nodes):
                clipped = True
            if len(run) >= 2:
                _add_run(run, direction, out)
            run = []
        else:
            run.append(idx)

    if clipped:
        res.ways_clipped += 1


def _add_run(run: List[int], direction: int, out: List[RawEdge]) -> None:
    for i in range(len(run) - 1):
        a, b = run[i], run[i + 1]
        if a == b:
            continue
        if direction >= 0:
            out.append(RawEdge(a, b))
        if direction <= 0:
            out.append(RawEdge(b, a))


# ------------------------------------------------------------ 最大弱连通分量

def _largest_component(n: int, edges: List[RawEdge]) -> List[bool]:
    """找出最大的弱连通分量（忽略方向看待连通性）。"""
    if n == 0:
        return []

    deg = [0] * n
    for e in edges:
        deg[e.frm] += 1
        deg[e.to] += 1
    start = [0] * (n + 1)
    for i in range(n):
        start[i + 1] = start[i] + deg[i]
    adj = [0] * start[n]
    cursor = start[:n]
    for e in edges:
        adj[cursor[e.frm]] = e.to
        cursor[e.frm] += 1
        adj[cursor[e.to]] = e.frm
        cursor[e.to] += 1

    comp = [-1] * n
    comp_size: List[int] = []
    for s in range(n):
        if comp[s] >= 0:
            continue
        cid = len(comp_size)
        size = 0
        comp[s] = cid
        queue = deque([s])
        while queue:
            u = queue.popleft()
            size += 1
            for a in range(start[u], start[u + 1]):
                v = adj[a]
                if comp[v] < 0:
                    comp[v] = cid
                    queue.append(v)
        comp_size.append(size)

    best = max(range(len(comp_size)), key=lambda i: comp_size[i]) if comp_size else -1
    return [c == best for c in comp]


# ------------------------------------------------------------ 小工具

def _is_food(tags: Dict[str, str]) -> bool:
    a = tags.get("amenity")
    return a == "restaurant" or a == "fast_food"


def is_ridable(tags: Dict[str, str]) -> bool:
    return _is_ridable(tags)


def _is_ridable(tags: Dict[str, str]) -> bool:
    hw = tags.get("highway")
    if hw is None or hw not in RIDABLE:
        return False
    if "construction" in tags or "proposed" in tags:
        return False
    access = tags.get("access")
    if access in ("private", "no", "agricultural", "forestry", "military", "customers"):
        return False
    return True


def oneway_sign(tags: Dict[str, str]) -> int:
    """oneway=yes/true/1/no/false/0/-1；-1 表示沿节点顺序的**反向**。"""
    hw = tags.get("highway", "")
    ow = tags.get("oneway")
    if ow is not None:
        if ow in ("yes", "true", "1"):
            return 1
        if ow in ("-1", "reverse"):
            return -1
        if ow in ("no", "false", "0"):
            return 0
    if tags.get("junction") == "roundabout":
        return 1
    if hw.startswith("motorway"):     # OSM 惯例：高速默认单行
        return 1
    return 0


def _int_attr(elem, name: str) -> int:
    try:
        return int(elem.get(name))
    except (TypeError, ValueError):
        return -1


def _float_attr(elem, name: str) -> float:
    try:
        return float(elem.get(name))
    except (TypeError, ValueError):
        return 0.0
