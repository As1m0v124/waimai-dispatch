这里放你的 OpenStreetMap 数据文件。

支持 **OSM XML**：扩展名 `.osm` 或 `.osm.xml`。

  ✗ 不支持 `.osm.pbf` —— PBF 是 protobuf + deflate 的二进制格式，
    解析它需要引入第三方库，和本项目「零第三方依赖」的原则冲突。
  ✓ XML 版本很容易拿到：
      - Geofabrik 下载页里选 `.osm.bz2`，解压后就是 XML
      - Overpass 查询用 `[out:xml]` 输出
      - 也可以用 JOSM 框选一块区域「另存为 .osm」

放进来之后重启程序，或者直接在界面「距离模型 / 路网」卡片的下拉框里选它
（下拉框来自 `GET /api/networks`，会实时扫描这个目录）。

如果文件很大（比如一个省），建议用 `--bbox` 裁一块出来：

  java -cp out waimai.Main --osm 你的文件.osm --bbox 30.24,120.14,30.29,120.19

首次加载会解析并写一个同名的 `.graph` 二进制缓存，之后启动就是毫秒级。
想强制重新解析，把 `.graph` 删掉即可。

不想找数据也没关系：`--osm synthetic` 会用内置的合成路网
（有主干道、环路、单行道、断头路），离线也能看路网模式的效果。
