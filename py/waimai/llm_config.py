"""大模型接入配置。对应 Java 版 LlmConfig。

走 OpenAI 兼容的 /chat/completions 协议 —— DeepSeek、月之暗面、通义千问、
以及本地的 Ollama / vLLM 都支持它，所以只要换 base_url 和 model 就能换供应商。

Key 的读取顺序：
  1. 环境变量（DEEPSEEK_API_KEY 等，见 PROVIDERS）
  2. data/llm.properties（在界面上填了 Key 之后会写到这里）

不会把 Key 写进源代码，也不会写进任何会被前端读到的字段。
public_view() 只暴露「有没有配 Key」和 Key 的后四位，完整 Key 永不出后端。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import paths


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: str
    model: str
    env_key: str


# 预设的供应商。base_url 都是 OpenAI 兼容端点。
PROVIDERS: List[Provider] = [
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-chat",
             "DEEPSEEK_API_KEY"),
    Provider("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1",
             "moonshot-v1-8k", "MOONSHOT_API_KEY"),
    Provider("qwen", "通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "qwen-plus", "DASHSCOPE_API_KEY"),
    Provider("ollama", "本地 Ollama", "http://127.0.0.1:11434/v1", "qwen2.5", ""),
    Provider("custom", "自定义（OpenAI 兼容）", "", "", ""),
]

# 配置文件位置从**包的位置**推（见 paths.py），不是相对当前工作目录 ——
# 否则从 py/ 目录启动会读到另一个（空的）配置文件，表现为"Key 明明配了却提示未配置"。
CONFIG_FILE = paths.under_data("llm.properties")


def provider_of(provider_id: str) -> Provider:
    for p in PROVIDERS:
        if p.id == provider_id:
            return p
    return PROVIDERS[0]


@dataclass
class LlmConfig:
    provider_id: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""
    timeout_sec: int = 90
    temperature: float = 0.3
    auto_interval_sec: int = 0     # 自动分析的间隔（真实秒）；0 = 只手动触发

    # ------------------------------------------------------------ 读写

    @staticmethod
    def load() -> "LlmConfig":
        c = LlmConfig()
        if CONFIG_FILE.is_file():
            try:
                props = read_properties(CONFIG_FILE)
                c.provider_id = props.get("provider", c.provider_id)
                c.base_url = props.get("baseUrl", c.base_url)
                c.model = props.get("model", c.model)
                c.api_key = props.get("apiKey", "")
                c.timeout_sec = _int(props.get("timeoutSec"), c.timeout_sec)
                c.temperature = _float(props.get("temperature"), c.temperature)
                c.auto_interval_sec = _int(props.get("autoIntervalSec"), 0)
            except OSError as e:
                print(f"  读取 {CONFIG_FILE} 失败，用默认配置：{e}")

        # 环境变量优先级更高 —— 部署时不想把 Key 落到磁盘上
        env = provider_of(c.provider_id).env_key
        if env:
            v = os.environ.get(env)
            if v and v.strip():
                c.api_key = v.strip()
        if not c.api_key and not c.base_url:
            p = provider_of(c.provider_id)
            c.base_url = p.base_url
            c.model = p.model
        return c

    def save(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_properties(CONFIG_FILE, {
            "provider": self.provider_id,
            "baseUrl": self.base_url or "",
            "model": self.model or "",
            "apiKey": self.api_key or "",
            "timeoutSec": str(self.timeout_sec),
            "temperature": str(self.temperature),
            "autoIntervalSec": str(self.auto_interval_sec),
        })

    # ------------------------------------------------------------ 视图

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip()
                    and self.base_url and self.base_url.strip()
                    and self.model and self.model.strip())

    def _key_source(self) -> str:
        env = provider_of(self.provider_id).env_key
        if env and os.environ.get(env, "").strip():
            return f"env:{env}"
        return "file" if self.api_key and self.api_key.strip() else "none"

    def public_view(self) -> dict:
        """给前端看的视图：**不含完整 Key**。"""
        return {
            "provider": self.provider_id,
            "baseUrl": self.base_url,
            "model": self.model,
            "timeoutSec": self.timeout_sec,
            "temperature": self.temperature,
            "autoIntervalSec": self.auto_interval_sec,
            "configured": self.configured,
            "keySource": self._key_source(),
            "keyHint": _mask_key(self.api_key),
            "providers": [
                {"id": p.id, "label": p.label, "baseUrl": p.base_url,
                 "model": p.model, "envKey": p.env_key}
                for p in PROVIDERS
            ],
            "configFile": str(CONFIG_FILE.absolute()),
        }


def _mask_key(k: Optional[str]) -> str:
    """脱敏：只留前 3 位和后 4 位。"""
    if not k or not k.strip():
        return ""
    if len(k) <= 8:
        return "****"
    return k[:3] + "****" + k[-4:]


# ------------------------------------------------------------ 配置文件读写
#
# 刻意用 java.util.Properties 那种「key=value、无 section」的格式，
# **不用 configparser** —— 因为 Java 版写出来的就是这个格式（Properties.store），
# 而 configparser 读不了没有 [section] 的文件。两边共用一个 data/llm.properties，
# 用户才能在两个实现之间来回切换而不必重填 Key。
#
# 读取时容忍 `[section]` 头和 `#` / `!` 注释，所以手写的带 section 的文件也能读。


def read_properties(path: Path) -> Dict[str, str]:
    props: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            if line.startswith("[") and line.endswith("]"):
                continue                       # 容忍 INI 风格的 section 头
            key, sep, value = line.partition("=")
            if not sep:
                continue
            props[key.strip()] = value.strip()
    return props


def write_properties(path: Path, props: Dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# waimai-dispatch LLM settings (local only, do not commit)\n")
        for k, v in props.items():
            f.write(f"{k}={v}\n")


def _int(s, default: int) -> int:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def _float(s, default: float) -> float:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return default
