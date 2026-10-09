"""经纬度 → 本地平面米坐标的投影。对应 Java 版 Projection。

用等距圆柱投影（equirectangular），以给定的西南角为原点：

    x = (lon − lon0) · 111320 · cos(lat0)
    y = (lat − lat0) · 110540

在一个城市尺度（十几公里）上，它的误差远小于 0.1%，而好处很大：
「米」是项目里其它所有代码（路径规划、阈值配置、Canvas 渲染）已经在用的单位，
于是换成真实路网以后，那些代码一行都不用改。

原点刻意取包围盒的**西南角**而不是中心，这样所有坐标都落在第一象限
（x ≥ 0、y ≥ 0），世界的范围就是包围盒的宽高，跟抽象模式的正方形是同一套语义。
"""

from __future__ import annotations

import math

from .model import Pt

M_PER_DEG_LAT = 110540.0            # 一个纬度对应的米数（全球近似平均值）
M_PER_DEG_LON_EQUATOR = 111320.0    # 赤道上一个经度对应的米数


class Projection:
    def __init__(self, lat0: float, lon0: float):
        self.lat0 = lat0
        self.lon0 = lon0
        self.cos_lat0 = math.cos(math.radians(lat0))

    def to_metres(self, lat: float, lon: float) -> Pt:
        x = (lon - self.lon0) * M_PER_DEG_LON_EQUATOR * self.cos_lat0
        y = (lat - self.lat0) * M_PER_DEG_LAT
        return Pt(x, y)

    def to_lat_lon(self, x: float, y: float) -> tuple:
        """本地米坐标 → (lat, lon)。"""
        lat = self.lat0 + y / M_PER_DEG_LAT
        lon = self.lon0 + x / (M_PER_DEG_LON_EQUATOR * self.cos_lat0)
        return lat, lon

    def describe(self) -> str:
        return f"原点 {self.lat0:.5f}, {self.lon0:.5f}（等距圆柱投影）"
