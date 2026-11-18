"""配置管理：默认值 <- dloader.config.json <- 环境变量。

所有可调项集中在这里，业务代码不出现魔法数字或本机路径。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 默认配置。键名与旧 PowerShell 版保持一致，旧配置文件可直接复用。
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    # 路径（空 = 自动发现）
    "ToolDir": "",
    "Pac": "",
    "AtPort": 0,
    # 设备识别（换芯片平台只改这三项）
    "DownloadVidPid": "VID_0525&PID_A4A7",
    "NormalVidPid": "VID_1782&PID_3D03",
    "AtNamePattern": "SPRD AT",
    # 串口与触发
    "BaudRate": 115200,
    "AutoLoaderCommand": 'AT+SPREF="AUTODLOADER"',
    # 烧写成功判据（正则列表，兼容不同版本 CmdDloader）
    "PassPatterns": [
        "DownLoad Passed",
        r"Download Result\s*=\s*Passed",
    ],
    # 输出解析正则
    "ElapsedPattern": r"Elapsed Times\s*=\s*([\d\.]+)\s*s",
    "PortPattern": r"PORT\s*=\s*COM(\d+)",
    "ImeiPattern": r"IMEI\s*=\s*(\S+)",
    # 超时与等待
    "TimeoutMs": 30000,
    "EnterDownloadSec": 30,
    "BootWaitSec": 70,
    "FlashRunTimeoutSec": 600,
    "AtProbeWaitMs": 800,
    "PollMs": 400,
    # 循环
    "Rounds": 3,
    "IntervalSec": 5,
    # 扫描根（空 = 用兜底逻辑）
    "ScanRoots": [],
    # 配置文件
    "ConfigFileName": "dloader.config.json",
}

ENV_MAP = {
    "DLOADER_TOOL": ("ToolDir", str),
    "DLOADER_BAUD": ("BaudRate", int),
    "DLOADER_TIMEOUT_MS": ("TimeoutMs", int),
}


def config_path() -> Path:
    """配置文件位置：DLOADER_CONFIG 优先；frozen 时用 exe 旁，否则包根目录。"""
    env = os.environ.get("DLOADER_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # PyInstaller 单文件：__file__ 指向临时解压目录，配置必须落在 exe 旁边
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    return (base / DEFAULTS["ConfigFileName"]).resolve()


def _warn(msg: str) -> None:
    """关键路径告警：配置有问题时明确说出来，不静默降级。

    历史上这里吞掉过异常，导致"配置明明写了却不生效"排查很久，
    所以配置层的任何降级都必须是可见的。
    """
    print(f"[WARN] {msg}")


def load_config() -> dict[str, Any]:
    """合并默认值、配置文件、环境变量。"""
    cfg = dict(DEFAULTS)

    p = config_path()
    if p.is_file():
        data: Any = None
        try:
            # utf-8-sig 兼容 Windows PowerShell 写出的带 BOM 文件
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            _warn(f"config is not valid JSON, using defaults: {p} ({exc})")
        except OSError as exc:
            _warn(f"config cannot be read, using defaults: {p} ({exc})")

        if data is not None:
            if not isinstance(data, dict):
                _warn(f"config must be a JSON object, ignored: {p}")
            else:
                for k, v in data.items():
                    if k in cfg:
                        cfg[k] = v
                    else:
                        _warn(f"unknown config key '{k}' ignored (typo?)")

    for env_key, (cfg_key, caster) in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            try:
                cfg[cfg_key] = caster(val)
            except ValueError:
                _warn(f"environment variable {env_key}={val!r} is invalid, ignored")

    roots = os.environ.get("DLOADER_SCAN_ROOTS")
    if roots:
        cfg["ScanRoots"] = [r for r in roots.split(";") if r]

    return cfg


def save_config(updates: dict[str, Any]) -> Path:
    """增量保存：只写显式设置过的键，默认值仍由代码管理。"""
    p = config_path()
    existing: dict[str, Any] = {}
    if p.is_file():
        try:
            existing = json.loads(p.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            _warn(f"existing config is unreadable and will be replaced: {p} ({exc})")
            existing = {}
        if not isinstance(existing, dict):
            _warn(f"existing config is not a JSON object and will be replaced: {p}")
            existing = {}

    existing.update(updates)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(existing, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# "VID_0525&PID_A4A7" -> (0x0525, 0xA4A7)
# ---------------------------------------------------------------------------
_VIDPID_RE = re.compile(
    r"VID_([0-9A-Fa-f]{4})\s*&\s*PID_([0-9A-Fa-f]{4})", re.IGNORECASE
)


def parse_vid_pid(spec: str) -> tuple[int, int]:
    """把 VID/PID 字符串解析成整数元组，格式非法时抛 ValueError。"""
    m = _VIDPID_RE.search(spec or "")
    if not m:
        raise ValueError(f"VID/PID 格式无法解析: {spec!r}，期望形如 VID_0525&PID_A4A7")
    return int(m.group(1), 16), int(m.group(2), 16)
