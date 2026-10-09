"""`python -m waimai <命令>` 的入口。

单独一个文件是为了让"包"有个明确的入口，同时保留 `python py/waimai/main.py`
和 `python py/waimai/cli.py` 两种跑法。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
