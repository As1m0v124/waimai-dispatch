"""路网的发现与加载。对应 Java 版 OsmLoader。

约定：把 OpenStreetMap XML 放进 data/osm/ 目录（.osm 或 .osm.xml），
程序启动时会自动发现它。首次加载解析完会写一个 .graph 二进制缓存，
之后启动就是毫秒级。

**只认 XML，不认 .osm.pbf。** PBF 要 protobuf 加 deflate，和本项目
零第三方依赖的原则冲突。Geofabrik 的 .osm.bz2 解压后就是 XML。

没有 .osm 文件时，用内置的合成路网 —— 有主干道、环路、单行道，离线也能演示路网模式。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from . import graph_cache, osm_parser, paths, realistic_net, synthetic_net
from .osm_parser import Bbox, Result

# 数据目录（相对工作目录）
DATA_DIR = paths.under_data("osm")


class Network:
    """一个可选的路网。"""

    def __init__(self, id_: str, label: str, file: Optional[Path] = None,
                 synthetic: bool = False):
        self.id = id_
        self.label = label
        self.file = file
        self.synthetic = synthetic


def discover() -> List[Network]:
    """列出可用路网：两个内置的 + data/osm 目录下的每个 .osm/.osm.xml。"""
    out = [
        Network("realistic", "模拟城区（环放射 + 河流瓶颈，推荐）", synthetic=True),
        Network("synthetic", "规则方格（均匀网格 + 少量对角线）", synthetic=True),
    ]
    if DATA_DIR.is_dir():
        for p in sorted(DATA_DIR.iterdir()):
            if not p.is_file():
                continue
            n = p.name.lower()
            if n.endswith(".osm") or n.endswith(".osm.xml"):
                out.append(Network(p.name, p.name, file=p))
    return out


def load(network_id: str, bbox: Optional[Bbox] = None,
         verbose: bool = True) -> Result:
    """加载一个路网。优先读 .graph 缓存，没有再解析原始 .osm 并写缓存。"""
    if network_id == "synthetic":
        g = synthetic_net.build(6000.0, 20260927)
        r = Result()
        r.graph = g
        r.nodes_in_bbox = g.node_count
        r.edges_built = g.edge_count
        r.synthetic = True
        return r

    if network_id == "realistic":
        g = realistic_net.build(6000.0, 20260927)
        r = Result()
        r.graph = g
        r.nodes_in_bbox = g.node_count
        r.edges_built = g.edge_count
        r.synthetic = True
        if verbose:
            st = realistic_net.stats(g, samples=120, sources=8)
            print(f"  已生成模拟城区：{st['nodes']} 节点 / {st['edges']} 有向边 / "
                  f"{st['segments']} 条无向线段")
            print(f"    断头路 {st['deadEnds']} 条，单行道 {st['onewayPct']}%，"
                  f"连通分量 {st['components']} 个")
            print(f"    绕路比（沿路 ÷ 直线）：中位 {st['detourP50']}，"
                  f"p95 {st['detourP95']}，最大 {st['detourMax']}")
        return r

    net = next((n for n in discover() if n.id == network_id), None)
    if net is None or net.file is None:
        raise FileNotFoundError(
            f"找不到路网 {network_id}。把 .osm 文件放进 {DATA_DIR.absolute()} 即可。")

    osm = net.file
    cache = graph_cache.cache_for(osm)

    # 只有不裁剪时才能用缓存（裁剪结果和全量不是同一张图）
    if bbox is None:
        t0 = time.monotonic()
        cached = graph_cache.load(cache)
        if cached is not None:
            if verbose:
                print(f"  路网缓存命中 {cache.name}（{(time.monotonic() - t0) * 1000:.0f} ms）")
            return cached

    t0 = time.monotonic()
    if verbose:
        print(f"  解析 {osm.name} …")
    res = osm_parser.parse_file(osm, bbox)
    ms = (time.monotonic() - t0) * 1000

    if verbose:
        print(f"  解析完成（{ms:.0f} ms）：读入 {res.nodes_read} 个节点、"
              f"{res.ways_read} 条 way；保留 {res.graph.node_count} 个节点、"
              f"{res.edges_built} 条有向边；剔除不可骑行 way {res.ways_dropped_by_class} 条")
        if res.component_dropped:
            print(f"  丢弃不在最大连通分量里的节点 {res.component_dropped} 个")
        if res.poi_found:
            print(f"  找到 {res.poi_found} 个餐饮 POI（会用作真实商家名）")

    if res.graph.node_count == 0:
        raise ValueError("这个 .osm 里没有可骑行的道路（highway 全是人行道之类？）。"
                         "确认文件是 OSM XML 而不是 .pbf，也可以换个区域的 .osm 再试。")

    if bbox is None:
        try:
            graph_cache.save(cache, res)
            if verbose:
                print(f"  已写入缓存 {cache.name}，下次启动会快很多")
        except OSError as e:
            if verbose:
                print(f"  缓存写入失败（不影响运行）：{e}")
    return res


def parse_bbox(s: Optional[str]) -> Optional[Bbox]:
    """解析 "30.24,120.14,30.29,120.19" 这样的裁剪参数。"""
    if not s or not s.strip():
        return None
    parts = s.split(",")
    if len(parts) != 4:
        raise ValueError("bbox 需要 4 个数字：minLat,minLon,maxLat,maxLon")
    try:
        vals = [float(p.strip()) for p in parts]
    except ValueError as e:
        raise ValueError(f"bbox 里的数字解析失败：{s}") from e
    return Bbox(*vals)


def print_available() -> None:
    """打印可用路网清单。"""
    print("  可用路网：")
    for n in discover():
        if n.synthetic:
            print(f"    {n.id}  → {n.label}")
        else:
            size_kb = n.file.stat().st_size // 1024 if n.file else -1
            cached = "，已有缓存" if n.file and graph_cache.cache_for(n.file).is_file() else ""
            print(f"    {n.id}  → {n.label}（{size_kb} KB{cached}）")
