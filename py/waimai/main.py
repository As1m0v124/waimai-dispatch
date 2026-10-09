"""外卖平台派单系统 · Python 版（零第三方依赖）。

和 Java 版共用同一套 REST 接口和前端，所以 web/ 和 verify-api.ps1 都能原样复用。

运行：
    python -m waimai.main
    python -m waimai.main --port 8787 --speed 20 --seed 20260927
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许 `python py/waimai/main.py` 和 `python -m waimai.main` 两种跑法
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from waimai import obs, osm_loader, paths, security
    from waimai.api import Api
    from waimai.simulator import Simulator
    from waimai.world import NetworkMode, World
else:
    from . import obs, osm_loader, paths, security
    from .api import Api
    from .simulator import Simulator
    from .world import NetworkMode, World


def locate_web() -> Path | None:
    """找前端目录：优先仓库里的 web/，其次上一级的 web/。"""
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent.parent / "web",      # py/waimai/ → 仓库根
                      Path.cwd() / "web",
                      Path.cwd().parent / "web"):
        if candidate.is_dir():
            return candidate
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="waimai", description="外卖平台派单系统 · Python 版（零第三方依赖）")
    parser.add_argument("--port", "-p", type=int, default=8787, help="监听端口（默认 8787）")
    parser.add_argument("--speed", type=float, default=20.0,
                        help="模拟倍速：1 真实秒 = N 模拟秒（默认 20）")
    parser.add_argument("--seed", type=int, default=20260927, help="随机种子")
    parser.add_argument("--osm", default=None,
                        help="启用路网：synthetic 用内置合成路网，或 data/osm/ 下的文件名")
    parser.add_argument("--bbox", default=None,
                        help="裁剪范围 minLat,minLon,maxLat,maxLon（对省级大文件很有用）")
    parser.add_argument("--list-networks", action="store_true", help="列出可用路网后退出")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1，只有本机能访问）。"
                             "填 0.0.0.0 会暴露给整个局域网，需要自己承担风险")
    parser.add_argument("--no-auth", action="store_true",
                        help="关闭 token 认证。**只在明确知道自己在做什么时用**："
                             "绑非本机地址时会强行保留认证")
    parser.add_argument("--no-log", action="store_true", help="不写日志文件（只输出到控制台）")
    parser.add_argument("--log-level", default="warn",
                    choices=("debug", "info", "warn", "error"),
                    help="控制台日志级别（默认 warn；日志文件始终记录全部）")
    parser.add_argument("--allow-private-llm", action="store_true",
                        help="允许大模型 baseUrl 指向内网/明文 http（默认只允许 https 和本机地址）")
    args = parser.parse_args(argv)

    if args.list_networks:
        osm_loader.print_available()
        return 0

    loopback = security.is_loopback_host(args.host)

    # 日志：默认写 data/logs/。结构化的 JSONL，按天+体积轮转，有总量上限。
    log_dir = None if args.no_log else paths.under_data("logs")
    obs.configure(log_dir=log_dir, console_level=args.log_level)

    token, token_source = security.load_or_create_token()
    auth_on = not args.no_auth
    if args.no_auth and not loopback:
        # 绑到非本机地址还关认证 = 把改调度参数、读配置的能力交给整个网段。
        # 这种组合不允许，宁可跟使用者的显式要求不一致，也不能默默开出一个洞。
        print("  !! --no-auth 与 --host 非本机地址不能同时使用："
              "那会把接口暴露给整个网络。已忽略 --no-auth。", file=sys.stderr)
        auth_on = True

    web_dir = locate_web()
    world = World.seeded(args.seed)
    world.speed_factor = args.speed

    if args.osm:
        # 明确要了路网却没加载成功时**必须停下来**，不能悄悄退回抽象城市接着跑。
        # 这里原来是把异常吞掉、只在 stderr 打一行就继续，结果 `--osm realistic`
        # 因为一个 AttributeError 一直是失效的，而界面看起来「启动正常」——
        # 使用者只会以为路网功能根本没做。要抽象城市就别传 --osm。
        try:
            bbox = osm_loader.parse_bbox(args.bbox)
            res = osm_loader.load(args.osm, bbox)
            # 生成出来的内置路网（realistic / synthetic）和真的读 .osm 文件
            # 在界面上要区分开，这个判断由 loader 给出（res.synthetic），
            # 不要在这里再硬编码一份路网名单。
            mode = (NetworkMode.SYNTHETIC if res.synthetic else NetworkMode.ROAD)
            world.load_network(res.graph, res.pois, mode, args.osm)
            print(f"  已启用路网：{res.graph.describe()}")
        except Exception as e:                          # noqa: BLE001
            print(f"\n  !! 路网加载失败：{e}", file=sys.stderr)
            print(f"  !! 已终止启动 —— 你传了 --osm {args.osm}，"
                  f"但没能用上这块路网。", file=sys.stderr)
            print(f"  !! 查看可用路网： python py/waimai/main.py --list-networks",
                  file=sys.stderr)
            print(f"  !! 只是想用抽象城市：去掉 --osm 参数即可。\n", file=sys.stderr)
            return 2

    simulator = Simulator(world, tick_ms=100)
    simulator.start()

    api = Api(world, web_dir, host=args.host, token=token if auth_on else "",
              token_source=token_source, allow_private_llm=args.allow_private_llm)
    api.start(args.port)

    llm_cfg = api.advisor.config
    if llm_cfg.configured:
        llm_line = f"{llm_cfg.provider_id} / {llm_cfg.model}（已配置）"
    else:
        llm_line = "未配置 API Key —— 区域研判和热力图照常可用，AI 解读需要填一个 Key"

    reachable = "127.0.0.1" if loopback else args.host
    print()
    print("  外卖平台派单系统 · Python 版")
    print("  --------------------------------------------------")
    print(f"  访问地址  http://{reachable}:{args.port}/")
    print(f"  距离模型  {world.metric.name}")
    if world.network_nodes > 0:
        print(f"            {world.network_nodes} 个节点 / {world.network_edges} 条有向边，范围 "
              f"{world.width_m / 1000:.1f} × {world.height_m / 1000:.1f} km")
    print(f"  大模型    {llm_line}")
    print(f"  前端目录  {web_dir if web_dir else '（没找到 web/，静态文件会 404）'}")
    print(f"  派单节拍  每 {world.cfg.dispatch_interval_sec} 模拟秒（默认 2 分钟）")
    print(f"  模拟倍速  {int(args.speed)}x（1 真实秒 = {int(args.speed)} 模拟秒）")

    if auth_on:
        src = {"env": "环境变量 WAIMAI_TOKEN", "file": "data/api-token",
               "new": "刚生成并写入 data/api-token"}.get(token_source, token_source)
        print(f"  认证      X-Auth-Token（来源：{src}），/api/* 全部需要")
    else:
        print("  认证      已关闭（--no-auth）")

    log_dir = obs.log_path()
    print(f"  日志      {log_dir if log_dir else '未写文件（--no-log）'}")
    print(f"  可观测    http://{reachable}:{args.port}/api/health（免认证）"
          f" · /api/metrics（需认证）")

    if not loopback:
        print()
        print("  ⚠ 绑定在非本机地址，局域网内可访问。")
        if auth_on:
            # 只有绑本机时才把 token 注入页面，所以局域网访问要手动带上
            print("    页面不会自动带上 token，用这个链接：")
            print(f"      http://{args.host}:{args.port}/#token={token}")
        else:
            print("    ⚠⚠ 认证是关闭状态，任何能访问该端口的人都能改调度参数。")
    print()
    print("  停止服务  Ctrl + C")
    print()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        # 优雅关闭：先停模拟线程再收尾，避免关到一半还在改状态。
        # （Simulator.stop() 一直存在，但从来没人调用过。）
        simulator.stop()
        obs.event("info", "shutdown", "收到中断信号，正在关闭",
                  simSeconds=world.now(), orders=len(world.orders))
        print("\n  已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
