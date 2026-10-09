"""项目里的固定路径。

为什么要有这个模块：`data/` 下的东西原来都是**相对当前工作目录**找的
（`Path("data", "api-token")` 之类）。于是从不同目录启动会拿到不同的文件：

    cd waimai-dispatch && python -m waimai ...     # 用 waimai-dispatch/data/
    cd waimai-dispatch/py && python -m waimai ...  # 用 waimai-dispatch/py/data/

最坑的一次：CLI 从 `py/` 目录跑，找不到 `data/api-token`，于是**新建了一个 token**，
然后拿它去请求服务端 —— 服务端当然不认，所有命令都返回 401。
更糟的是它还在 `py/data/` 下留了一个多余的 token 文件。

所以所有固定路径一律从**包的位置**推出来，和当前工作目录无关。
这个包在 `<root>/py/waimai/`，所以 root 是上两级。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# <root>/py/waimai/paths.py → <root>
ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """data/ 目录（不存在就创建）。"""
    p = ROOT / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def under_data(*parts: str) -> Path:
    """data/ 下的某个路径（不创建父目录，交给写入方决定）。"""
    return ROOT.joinpath("data", *parts)


def to_root_relative(p: Path) -> Optional[str]:
    """转成相对 root 的字符串，便于日志/接口里显示而不泄露绝对路径。"""
    try:
        return str(p.resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return None
