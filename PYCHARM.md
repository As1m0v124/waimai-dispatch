# 在 PyCharm 里跑这个项目

**零第三方依赖，不需要 `pip install` 任何东西** —— 只用 Python 标准库。
用 PyCharm 直接打开这个目录就能跑。

---

## 1. 打开（**这一步选错文件夹，后面全都不对**）

`File → Open` → 选中 **`waimai-dispatch` 这个文件夹** → 信任项目。

> ⚠️ **注意选里层那个 `waimai-dispatch`，不是解压出来的外层目录。**
>
> 解压 `waimai-dispatch-pycharm.zip` 之后，Windows 会建两层：
>
> ```
> waimai-dispatch-pycharm\        ← 不要打开这一层（它只是解压出来的外壳）
> └─ waimai-dispatch\             ← ★ 打开这一层
>    ├─ .idea\runConfigurations\  ← 6 个运行配置住在这里
>    ├─ py\  web\  data\  tools\  ...
>    └─ PYCHARM.md  README.md
> ```
>
> PyCharm 只在**项目根目录**的 `.idea\runConfigurations\` 里找运行配置。
> 打开外层目录的话，那 6 个配置一个都不会出现在右上角的下拉框里，
> 你就只能看到一个叫「当前文件」的配置 —— 而它对 `.ps1`、`.md` 文件都会说
> 「编辑器中的文件不可运行」。**这不是项目坏了，是打开错了层级。**
>
> 已经开错了也没关系：`File → Open` 重新选里层那个文件夹，
> 选「New Window」或「Attach」都行。解释器可以直接复用上层那个 `venv`。

## 2. 选解释器（这一步最容易踩坑，先看）

`File → Settings → Project → Python Interpreter → Add Interpreter → Add Local Interpreter
→ System Interpreter`，然后选你的 Python 3.10+ 解释器。

**先找出它在哪** —— 在 PyCharm 底部的 Terminal 里跑（`py -0` 最省事）：

```powershell
py -0                                                          # 列出所有已安装的版本
py -0p                                                         # 连路径一起列出来
Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory    # 直接看安装目录
where.exe python                                                # 看 PATH 上的都是什么
```

理想情况是选到类似这样一个路径（版本号可能不同）：

```
C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe
```

> **不要选 PATH 里那个 `python.exe`。**
> 它在 `...\AppData\Local\Microsoft\WindowsApps\python.exe`，是 **Microsoft Store 的占位程序**：
> 在命令行里跑会直接以退出码 9009 失败，在 PyCharm 里则会表现成一堆莫名其妙的错误
> （模块找不到、进程秒退、没有输出）。真正能用的解释器在 `%LOCALAPPDATA%\Programs\Python\` 下面。
>
> 如果 `py -0` 什么都不输出、`%LOCALAPPDATA%\Programs\Python` 目录也不存在，
> 说明这台机器还没装 Python。装一个（不需要装任何第三方包）：
>
> ```powershell
> winget install --id Python.Python.3.13 --exact --scope user
> ```
>
> 装完**重开 PyCharm**（它只在启动时扫解释器列表），再回到这一步。

**不需要建虚拟环境、不需要 pip install。** 这个项目只用标准库，
选 System Interpreter 就行 —— 少一层 venv，路径问题也少一个。
（想用 venv 也行，不会有任何冲突，只是白搭一层。）

## 3. 直接点绿三角

项目里已经带了 6 个运行配置，右上角的下拉框里能直接看到：

| 运行配置 | 干什么 |
| --- | --- |
| **启动服务（8787 · 抽象城市）** | 启动服务，然后浏览器打开 http://127.0.0.1:8787/ |
| **启动服务（模拟城区路网 · 8788）** | 启动并加载「模拟城区」路网（环放射骨架 + 河流瓶颈 + 断头路 + 单行道） |
| **运行自检（算法）** | 跑算法自检（400+ 项）；全绿说明代码没问题 |
| **CLI · 看状态** | 用命令行看运行中的实例（时钟/指标/进单） |
| **CLI · 跑基准（KPI 门禁）** | 无头跑 90 模拟分钟并比对准时率/顺路占比/兜底占比门禁 |
| **MCP 服务（stdio）** | 以 MCP 服务运行，给智能体调用。**注意它是被 MCP 客户端启动的**，点绿三角之后它会安静地等 stdin 输入（不会自己退出），手工跑只能一行行喂 JSON 试；平时不用管它 |

六个配置都带上了 `-X utf8`（否则 Windows 控制台会把中文输出打成乱码）和
`PYTHONUNBUFFERED=1`（否则启动横幅会卡在缓冲区里不显示）。

> 如果下拉框提示「未知模块 / module not found」：那是 PyCharm 还没把配置里的模块名对上。
> 点 `Edit Configurations…`，把 **Interpreter** 重新选一遍、**Working directory** 设成项目根目录即可
> —— **命令行参数（`--port` / `--speed` / `--osm`）不用改**。

## 4. 手动配置（等价写法，不想用现成配置时）

`Run → Edit Configurations → + → Python`，然后：

| 字段 | 值 |
| --- | --- |
| Script path | `<项目根>\py\waimai\main.py` |
| Parameters | `--port 8787 --speed 20` |
| Working directory | `<项目根>` |
| Interpreter options | `-X utf8` |

自检那个配置把 Script path 换成 `<项目根>\py\waimai\selftest.py`，参数留空。

命令行等价写法（PyCharm 的 Terminal 里也能直接用）：

```powershell
cd py

# Web 服务
python -X utf8 -m waimai.main --port 8787 --speed 20            # 抽象城市
python -X utf8 -m waimai.main --port 8788 --osm realistic       # 模拟城区路网

# 命令行（不开浏览器，详见 README 的「命令行」一节）
python -X utf8 -m waimai state                                  # 看状态
python -X utf8 -m waimai simulate --minutes 90 --orders-per-min 1
python -X utf8 -m waimai kpi                                    # 跑基准 + 比对门禁
python -X utf8 -m waimai bench --local                          # 派单耗时基线
python -X utf8 -m waimai learn status                           # 数据飞轮现状
python -X utf8 -m waimai explain WM00012                        # 这单为什么派给了他

# MCP 服务（给智能体调用；stdio，不会自己退出，Ctrl+C 停）
python -X utf8 -m waimai mcp --network realistic

# 自检
python -X utf8 py\waimai\selftest.py
python -X utf8 py\mcp_selftest.py
```

> **关于认证**：服务默认只绑 `127.0.0.1` 且所有 `/api/*` 需要 token。
> token 在 `data/api-token`（首次启动自动生成），CLI/MCP 会自动读取，浏览器访问时
> 服务端会把 token 注入页面，所以你**不需要手工传**。
> 用 curl / Postman 手工调接口时才需要带上 `X-Auth-Token` 头。

## 5. 几个会遇到的点

- **PyCharm 提示「找到支持 \*.ps1 文件的插件 / 安装 PowerShell 插件」→ 点「忽略扩展」。**
  不需要装：那些 `.ps1` 只是命令行的便捷封装，PyCharm 里用运行配置或
  `python -X utf8 -m waimai ...` 就够了，而且**绕开了 PowerShell 的执行策略**
  （`.ps1` 默认被禁止运行，得要 `-ExecutionPolicy Bypass`）——
  装了插件只多一个语法高亮，真要跑还是得处理执行策略，解决不了问题。
  另外注意 `run.ps1` / `build.ps1` / `selftest.ps1` 是 **Java 版**的脚本（对照参考），
  Python 版对应的是带 `-py` 后缀的那几个。
- **改完代码要重启服务**：`main.py` 是常驻进程，没有热重载，改完按红方块停掉再启动。
- **端口被占**：默认 8787 已经在跑（比如命令行那个还开着），换一个，例如 `--port 8788`。
- **浏览器缓存**：前端 `web/` 是静态文件，改了之后用 `Ctrl + F5` 强刷，别用普通刷新。
- **控制台中文乱码**：运行配置里已经带了 `-X utf8`。还乱的话看
  `Settings → Editor → File Encodings`，把三处都设成 UTF-8。
- **`.ps1` 脚本不用管**：`run-py.ps1` / `selftest-py.ps1` / `verify-api.ps1` 是给命令行用的。
  PyCharm 里优先用运行配置或 `python -X utf8 -m waimai ...`，
  这样绕开了 PowerShell 的执行策略（`.ps1` 默认被禁止运行，得要 `-ExecutionPolicy Bypass`）。
  `verify-api.ps1` 是 HTTP 端到端验收（306 项），需要服务已经在跑 ——
  想要的话在 PyCharm 的 Terminal 里执行
  `powershell -ExecutionPolicy Bypass -File verify-api.ps1`。
- **数据文件会落在项目里**：`data/api-token`（首次启动自动生成）、`data/logs/`（运行日志）、
  `data/learn/`（学习样本）。它们都在 `.gitignore` 里，不需要管；删掉也会自动重建。

## 6. 关于 `data/llm.properties`（大模型 Key）

这份导出里**没有**带这个文件 —— 它里面有 API Key，导出包可能会离开这台机器，所以不打包。
不影响运行：区域研判和订单热力图都是纯本地计算，照样能用；只有侧边栏的「AI 解读」需要 Key。

要启用 AI 解读，两种方式都行：

1. 界面「运营分析」侧边栏里填 Key 并保存（会自动写回 `data/llm.properties`）；
2. 自己新建 `data/llm.properties`：

```properties
provider=deepseek
baseUrl=https://api.deepseek.com
model=deepseek-chat
apiKey=你的Key
timeoutSec=20
temperature=0.3
autoIntervalSec=300
```

格式刻意用 **`key=value`**（`java.util.Properties` 那种，没有 `[section]`），
这样 Java 版和 Python 版能共用同一个配置文件。

## 7. 目录速查

```
py/waimai/           ← Python 实现，全部代码在这里
  main.py            入口（就是上面要跑的那个脚本）
  dispatcher.py      派单算法：三档优先级 + 推迟 + regret 排序
  route_planner.py   插入启发式 + 2-opt / Or-opt
  world.py           全局状态 + 停单判定
  model.py           数据结构 + Config（所有可调参数）
  selftest.py        326 项自检
web/                 前端（HTML + 原生 JS + Canvas）
src/waimai/          Java 版（对照参考，冻结在移植那一刻，不参与 Python 开发）
data/osm/            放你自己的 .osm 路网文件
```
