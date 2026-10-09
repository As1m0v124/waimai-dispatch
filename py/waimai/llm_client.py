"""OpenAI 兼容的对话补全客户端，只用标准库的 urllib.request。

对应 Java 版 LlmClient（那边用 java.net.http）。

之所以打成「OpenAI 兼容」而不是「DeepSeek 专用」：DeepSeek、月之暗面、通义千问、
本地的 Ollama 都实现了同一个 /chat/completions 协议，换个 base_url 就能换供应商。

错误处理刻意做得具体 —— 这类外呼失败的原因很好区分，也很有必要区分：
401 是 Key 不对、429 是限流、超时是网络或模型太慢、连不上是被墙或 base_url 写错。
全都糊成一句「调用失败」的话，用的人根本不知道该改什么。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import List, Optional

from .llm_config import LlmConfig, provider_of


class LlmError(Exception):
    """调用失败，带上人话的原因。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def chat(cfg: LlmConfig, system_prompt: str, user_prompt: str) -> str:
    """发一次对话补全请求，返回模型返回的正文。"""
    if not cfg.api_key or not cfg.api_key.strip():
        raise LlmError(
            f"还没有配置 API Key。在侧边栏「AI 分析」里填一个，"
            f"或者设置环境变量 {provider_of(cfg.provider_id).env_key}。")

    base = (cfg.base_url or "").strip()
    if not base:
        raise LlmError("还没有配置 base_url。")
    base = base.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"

    messages: List[dict] = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {cfg.api_key}")
    req.add_header("Accept", "application/json")

    timeout = max(10, cfg.timeout_sec)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:                              # noqa: BLE001
            pass
        raise LlmError(_explain_http(e.code, text), e.code) from e
    except socket.timeout as e:
        raise LlmError(f"调用超时（{timeout} 秒）。可以把超时调大一点，"
                       f"或者换一个更快的模型。") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            raise LlmError(f"调用超时（{timeout} 秒）。可以把超时调大一点，"
                           f"或者换一个更快的模型。") from e
        raise LlmError(f"连不上 {url}（{reason}）。"
                       f"检查网络、代理，以及 base_url 是否写对。") from e
    except OSError as e:
        raise LlmError(f"连不上 {url}（{e}）。检查网络、代理，以及 base_url 是否写对。") from e

    return extract_content(text)


def _explain_http(code: int, body: str) -> str:
    """把 HTTP 状态码翻译成「你该改什么」。"""
    if code in (401, 403):
        return (f"API Key 被拒绝（HTTP {code}）。"
                f"检查 Key 是否正确、是否已过期、余额是否够。")
    if code == 429:
        return f"被限流了（HTTP 429）。等一会儿再试，或降低自动分析的频率。"
    return f"接口返回 HTTP {code}：{_truncate(body, 300)}"


def extract_content(text: str) -> str:
    """从 choices[0].message.content 里取出正文。"""
    try:
        root = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise LlmError(f"返回的不是合法 JSON：{_truncate(text, 300)}") from e

    if not isinstance(root, dict):
        raise LlmError(f"返回的 JSON 结构不是对象：{_truncate(text, 200)}")

    err = root.get("error")
    if isinstance(err, dict):
        raise LlmError(f"接口报错：{err.get('message', err)}")

    choices = root.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError(f"返回里没有 choices：{_truncate(text, 300)}")

    first = choices[0]
    if not isinstance(first, dict):
        raise LlmError("choices[0] 不是对象。")

    message = first.get("message")
    if isinstance(message, dict) and message.get("content") is not None:
        return str(message["content"])
    # 有些实现会把正文放在 text 字段
    if first.get("text") is not None:
        return str(first["text"])
    raise LlmError("choices[0] 里没有 message.content。")


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + "…"
