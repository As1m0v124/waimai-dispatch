"""路网的二进制缓存。对应 Java 版 GraphCache。

解析一个几 MB 的 .osm 要好几秒，每次都重来很浪费；而把图缓存成二进制之后
启动是毫秒级的。缓存里只存**原始输入**（节点的经纬度、边表、POI），
不存派生出来的坐标和边长 —— 那些由 RoadGraph.build 按同一套逻辑重新算出来，
保证「从 .osm 直接建」和「从缓存建」得到的图完全一致。

格式带 magic 和版本号，格式一变旧缓存会被自动忽略并重新生成，而不是读出垃圾。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

from .model import Pt
from .osm_parser import Poi, Result
from .road_graph import RawEdge, RoadGraph

MAGIC = 0x574D4150        # "WMAP"
VERSION = 1


def cache_for(osm_file: Path) -> Path:
    """缓存文件名：xxx.osm → xxx.graph。"""
    return osm_file.with_suffix(".graph")


def save(path: Path, res: Result) -> None:
    g = res.graph
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<ii", MAGIC, VERSION))
        _write_str(f, g.name)
        _write_str(f, g.source)
        f.write(struct.pack("<dd", g.proj.lat0, g.proj.lon0))

        f.write(struct.pack("<i", g.node_count))
        f.write(struct.pack(f"<{g.node_count * 2}d", *[
            v for i in range(g.node_count) for v in (g.lat[i], g.lon[i])]))

        f.write(struct.pack("<i", g.edge_count))
        if g.edge_count:
            f.write(struct.pack(f"<{g.edge_count * 2}i", *[
                v for i in range(g.edge_count) for v in (g.edge_from[i], g.edge_to[i])]))

        f.write(struct.pack("<i", len(res.pois)))
        for p in res.pois:
            _write_str(f, p.name)
            f.write(struct.pack("<dd", p.lat, p.lon))


def load(path: Path) -> Optional[Result]:
    """读缓存；文件不存在、magic/版本不匹配、或内容损坏都返回 None。"""
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            magic, version = struct.unpack("<ii", f.read(8))
            if magic != MAGIC or version != VERSION:
                return None
            name = _read_str(f)
            source = _read_str(f)
            f.read(16)                                    # lat0/lon0 由 build 重推

            node_count = struct.unpack("<i", f.read(4))[0]
            if node_count <= 0 or node_count > 20_000_000:
                return None
            flat = struct.unpack(f"<{node_count * 2}d", f.read(node_count * 16))
            lat_lon = [(flat[i * 2], flat[i * 2 + 1]) for i in range(node_count)]

            edge_count = struct.unpack("<i", f.read(4))[0]
            if edge_count < 0 or edge_count > 200_000_000:
                return None
            if edge_count:
                eflat = struct.unpack(f"<{edge_count * 2}i", f.read(edge_count * 8))
            else:
                eflat = ()
            edges = [RawEdge(eflat[i * 2], eflat[i * 2 + 1]) for i in range(edge_count)]

            res = Result()
            res.nodes_read = node_count
            res.nodes_in_bbox = node_count
            res.edges_built = edge_count
            graph = RoadGraph.build(name, source, lat_lon, edges)
            res.graph = graph

            poi_count = struct.unpack("<i", f.read(4))[0]
            if poi_count < 0 or poi_count > 5_000_000:
                return None
            for _ in range(poi_count):
                pname = _read_str(f)
                lat, lon = struct.unpack("<dd", f.read(16))
                p = Poi(pname, lat, lon)
                p.pt = graph.proj.to_metres(lat, lon)
                res.pois.append(p)
            res.poi_found = len(res.pois)
            return res
    except (OSError, struct.error, ValueError, MemoryError):
        return None          # 坏缓存不是错误，重新生成即可


def _write_str(f, s: str) -> None:
    data = s.encode("utf-8")
    f.write(struct.pack("<i", len(data)))
    f.write(data)


def _read_str(f) -> str:
    n = struct.unpack("<i", f.read(4))[0]
    if n < 0 or n > 1_000_000:
        raise ValueError("字符串长度异常")
    return f.read(n).decode("utf-8")
