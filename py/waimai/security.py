"""安全原语：认证 token、静态文件路径校验、请求体限制、LLM 出口地址校验。

单独成模块，是因为这些恰恰是最该被钉死的逻辑，而塞在 HTTP 处理函数里就只能
靠"发个请求试试"来测。放在这里可以被自检直接调用，覆盖各种畸形输入。

**这里修的是三个真实漏洞（不是理论风险）：**

1. 任意文件读取。原实现挡路径穿越只判断 `".." in rel`，而 `Path(web_dir) / "C:/x"`
   因为 pathlib 的盘符规则会把 `web_dir` 整个丢掉，于是
   `GET /C:/Users/.../data/llm.properties` 能直接读到明文 API Key。
   现在改成：先按段解析、逐段拒绝（`..`、反斜杠、冒号、NUL），再 resolve 并确认
   结果仍在 web/ 目录内 —— 用白名单式的"必须在里面"，而不是黑名单式的"看起来不像"。

2. 通配 CORS。`Access-Control-Allow-Origin: *` 让任意网页都能驱动本机接口
   （包括改 LLM 的 baseUrl，再把 Key 发到攻击者那里）。现在只在同源时回 CORS 头。

3. SSRF。`/api/llm/config` 不校验 baseUrl，随后 analyze 会把
   `Authorization: Bearer <key>` 发过去。现在有出口地址校验，并在真正发请求前再查一次。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit

from . import paths

# ------------------------------------------------------------ 请求体限制

# 这个系统的请求体最大就是"下一批订单"级别的 JSON，1 MiB 绰绰有余。
# 没有上限时，一个巨大的 Content-Length 就能把线程和内存吊住。
MAX_BODY_BYTES = 1 * 1024 * 1024


class RequestTooLarge(ValueError):
    """请求体超过上限（对应 HTTP 413）。"""


class BadRequestLen(ValueError):
    """Content-Length 本身不可信（对应 HTTP 400）。"""


def parse_content_length(headers) -> int:
    """从请求头解析出可信的请求体长度。

    拒绝的情况（每一种都对应过真实的挂起/串包问题）：
      · `Transfer-Encoding: chunked` —— 手写实现不支持，读长度会得到 0，
        剩下的分块字节留在 socket 里被当成下一个请求的起始行（请求串包）
      · 重复的 Content-Length —— 拿其中一个去读会和对端不一致
      · 非数字 / 负数 —— `int()` 接受 `-5`，`length > 0` 为假就不读，
        于是残留字节同样会串包
      · 超过上限 —— 直接 413，不要去读那 100 GB
    """
    if headers.get("Transfer-Encoding"):
        raise BadRequestLen("不支持 Transfer-Encoding: chunked 的请求")

    raw_values = headers.get_all("Content-Length") or []
    if len(raw_values) > 1:
        raise BadRequestLen("Content-Length 出现了多次")
    raw = (raw_values[0] if raw_values else "").strip()
    if not raw:
        return 0
    if not re.fullmatch(r"[0-9]+", raw):
        raise BadRequestLen(f"Content-Length 不是非负整数：{raw[:32]!r}")

    length = int(raw)
    if length > MAX_BODY_BYTES:
        raise RequestTooLarge(
            f"请求体太大（{length} 字节，上限 {MAX_BODY_BYTES}）")
    return length


def parse_json_body(raw: bytes) -> dict:
    """把请求体解析成 dict。

    `parse_constant` 用来拒绝 `NaN` / `Infinity` / `-Infinity`：
    Python 的 json 默认接受它们，但 JSON 标准不允许，而且
    `int(float('inf'))` 会抛 OverflowError 变成 500 —— 更糟的是 NaN 一旦
    混进坐标或参数里，会静默污染几何计算（NaN 参与比较恒为假）。
    """
    if not raw:
        return {}

    def _reject(token: str):
        raise BadRequestLen(f"JSON 里不允许 {token}（标准 JSON 没有这个字面量）")

    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject)
    except BadRequestLen:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BadRequestLen(f"请求体不是合法 JSON：{e}") from e
    return parsed if isinstance(parsed, dict) else {}


# ------------------------------------------------------------ 认证 token

TOKEN_FILE = "data/api-token"          # 相对项目根（见 paths.py），不是相对 CWD
TOKEN_ENV = "WAIMAI_TOKEN"
TOKEN_HEADER = "X-Auth-Token"


def token_path(path: Optional[Path] = None) -> Path:
    """token 文件位置。默认从**包的位置**推，和当前工作目录无关 ——
    否则从 py/ 目录跑 CLI 会全新生成一个 token，然后 401 到底。"""
    return Path(path) if path is not None else paths.under_data("api-token")


def load_or_create_token(path: Optional[Path] = None,
                         env: Optional[dict] = None) -> Tuple[str, str]:
    """拿到本次进程要用的 token，返回 (token, 来源)。

    来源分三种，便于启动日志里说清"token 是从哪来的"（排查 401 时这一步最省事）：
      · `env`  —— 环境变量 WAIMAI_TOKEN（容器/CI 里用，不用挂文件）
      · `file` —— data/api-token（首次启动自动生成并落盘）
      · `new`  —— 刚生成、还没写进文件

    落盘用 0600：同机器上的其他用户不该读到它。Windows 上 chmod 只影响只读位，
    真正的隔离靠用户目录的 ACL，这里尽力而为。
    """
    env = os.environ if env is None else env
    from_env = (env.get(TOKEN_ENV) or "").strip()
    if from_env:
        return from_env, "env"

    p = token_path(path)
    try:
        existing = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        existing = ""
    if existing:
        return existing, "file"

    token = secrets.token_urlsafe(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass                       # 某些文件系统不支持，不影响功能
        return token, "new"
    except OSError:
        return token, "new"            # 写不了就只用内存里的，下次重启会变


def token_matches(expected: str, given: Optional[str]) -> bool:
    """定长比较，避免按字符比较泄漏前缀（时序侧信道）。"""
    if not expected or not given:
        return False
    return hmac.compare_digest(str(expected), str(given))


def is_loopback_host(host: str) -> bool:
    """判断绑定地址是不是"只有本机能访问"。"""
    h = (host or "").strip().strip("[]").lower()
    return h in ("127.0.0.1", "localhost", "::1", "127.0.0.0/8") or h.startswith("127.")


def origin_allowed(origin: Optional[str], host: str, port: int) -> bool:
    """CORS 判定：只允许同源。

    原来是 `Access-Control-Allow-Origin: *`，等于允许任意网页驱动本机接口
    （浏览器会把响应交给那个网页）。改成只在本机自己这个源上回 CORS 头，
    跨站请求拿不到响应，写操作也就无从下手（CSRF 一起挡掉了）。
    """
    if not origin:
        return False
    try:
        p = urlsplit(origin)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    o_host = (p.hostname or "").lower()
    o_port = p.port or (443 if p.scheme == "https" else 80)

    base = (host or "").lower()
    if base in ("0.0.0.0", "::", ""):
        # 绑了所有网卡时，"同源"就等于"任意一个能访问到我的地址"，
        # 此时任何 Origin 都可能是合法的页面来源 —— 那就一个都不放行，
        # 让浏览器按同源策略拦住跨站请求。局域网用法见 README（用 token 链接）。
        return False
    allowed_hosts = {base}
    if is_loopback_host(base):
        allowed_hosts |= {"127.0.0.1", "localhost", "::1"}
    return o_host in allowed_hosts and o_port == port


# ------------------------------------------------------------ 静态文件

# Windows 保留设备名。写到这些名字上会打到设备而不是文件，
# 在一个只该发静态文件的路径上不该出现。
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class UnsafePath(ValueError):
    """URL 路径里出现了不该出现的东西。"""


def split_url_path(url_path: str) -> list:
    """把一个 URL 路径切成安全的相对段。

    逐段白名单：空段和 `.` 丢掉，其余只允许普通名字。
    拒绝的每一类都对应一种真实的越界手法：
      · `..`            经典目录穿越
      · 反斜杠          在 Windows 上也是分隔符，`..\\` 和 `../` 等价
      · 冒号            盘符（`C:`）、UNC（`\\\\host\\share`）和 NTFS 数据流（`file.txt:stream`）
      · NUL             C 层字符串截断，让校验看到的和实际打开的不是同一个路径
      · Windows 设备名   `CON` / `NUL` / `COM1` 之类
    """
    decoded = unquote(url_path or "", errors="strict")
    if "\x00" in decoded:
        raise UnsafePath("路径里含 NUL 字节")
    if "\\" in decoded:
        raise UnsafePath("路径里含反斜杠")
    if ":" in decoded:
        raise UnsafePath("路径里含冒号（盘符 / UNC / 数据流）")

    parts = []
    for seg in decoded.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise UnsafePath("路径里含 ..")
        if seg.lower().split(".")[0] in _WIN_RESERVED:
            raise UnsafePath(f"路径里含 Windows 设备名：{seg}")
        parts.append(seg)
    return parts


def resolve_web_file(web_dir: Optional[Path], url_path: str) -> Optional[Path]:
    """把 URL 路径映射到 web/ 目录里的真实文件；不安全或不存在都返回 None。

    最后那步 `is_relative_to` 是兜底：即便上面的逐段校验漏了什么手法，
    只要 resolve 之后跑出了 web/（符号链接也算），一律拒绝。
    这是"必须在里面"的白名单，比"看着不像坏的"黑名单可靠得多。
    """
    if web_dir is None:
        return None
    try:
        parts = split_url_path(url_path)
    except UnsafePath:
        return None
    if not parts:                       # "/" → 首页
        parts = ["index.html"]

    base = web_dir.resolve()
    try:
        candidate = (base / Path(*parts)).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(base):
        return None
    return candidate if candidate.is_file() else None


# ------------------------------------------------------------ LLM 出口地址

class BlockedTarget(ValueError):
    """出口地址被安全策略拒绝。"""


def _addresses(host: str, port: int):
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedTarget(f"域名解析失败：{host}") from e
    return [info[4][0] for info in infos]


def check_llm_base_url(url: str, allow_private: bool = False) -> str:
    """校验大模型服务的 baseUrl，返回规范化后的地址；不合规抛 BlockedTarget。

    为什么必须有这个：`/api/llm/analyze` 会把 `Authorization: Bearer <你的Key>`
    发到配置里的 baseUrl。baseUrl 只要能随便改，Key 就能被发到任意主机。

    规则（默认档）：
      · 只允许 https —— 明文 http 会把 Key 暴露在链路上
      · 回环地址放行 —— 本地跑 Ollama / vLLM 是正当用法，而且把 Key 发给自己不算泄漏
      · 其他私网 / 链路本地 / 保留地址一律拒绝 —— 那是"往局域网里别人的机器发 Key"
        （攻击者用 DNS 把域名解析到内网地址的手法也在这里被拦住）
      · `allow_private=True` 才放开（局域网自建推理服务用）

    诚实说明：域名在这里解析一次、发请求时还会再解析一次，两次之间理论上存在
    DNS rebinding 的窗口。真正的第一道防线是 token 认证（改不了配置就利用不了），
    这一层挡的是"贴错地址"和"内网横向"。
    """
    raw = (url or "").strip()
    if not raw:
        raise BlockedTarget("baseUrl 不能为空")
    try:
        p = urlsplit(raw)
    except ValueError as e:
        raise BlockedTarget(f"baseUrl 解析失败：{e}") from e

    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").strip()
    if scheme not in ("http", "https"):
        raise BlockedTarget(f"baseUrl 只支持 http/https，收到 {scheme or '(空)'!r}")
    if not host:
        raise BlockedTarget("baseUrl 里没有主机名")

    loopback = host.lower() in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")
    if not allow_private:
        if scheme != "https" and not loopback:
            raise BlockedTarget(
                "baseUrl 必须用 https（明文 http 会把 API Key 暴露在链路上）；"
                "本机地址除外")
        if not loopback:
            port = p.port or (443 if scheme == "https" else 80)
            for addr in _addresses(host, port):
                try:
                    ip = ipaddress.ip_address(addr)
                except ValueError:
                    continue
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                    raise BlockedTarget(
                        f"baseUrl 指向内网地址 {ip}（拒绝把 API Key 发到内网）；"
                        "确实要连自建服务就用 --allow-private-llm")

    return raw.rstrip("/")
