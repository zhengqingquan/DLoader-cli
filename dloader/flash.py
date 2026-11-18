"""调用 CmdDloader.exe 完成一次烧写，并解析结果。

注意：CmdDloader.exe 的进程退出码恒为 0，成功与否只能靠解析 stdout。
所有面向用户的输出一律使用英文，避免控制台编码问题。

占口诊断见 ``port_diag``；此处再导出常用符号，兼容旧 import 路径。
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import get_exe_version
from .port_diag import (  # noqa: F401 — re-export public helpers
    find_processes_locking_com,
    find_suspect_serial_processes,
    format_open_port_hint,
    guess_port_from_output,
    is_open_port_failure,
    normalize_com_port,
    show_flash_failure,
)


@dataclass
class FlashResult:
    success: bool
    output: str
    elapsed_sec: float | None = None
    port: str | None = None
    imei: str | None = None
    exit_code: int = -1


def _decode(raw: bytes) -> str:
    """CmdDloader 在不同语言环境下可能输出 UTF-8 / GBK，逐级尝试。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_flash_success(text: str, cfg: dict[str, Any]) -> bool:
    for pattern in cfg.get("PassPatterns") or []:
        if pattern and re.search(pattern, text):
            return True
    return False


def probe_skip_file_id(
    exe: str | Path, timeout_sec: float = 20.0
) -> tuple[bool, str]:
    """探测这个 CmdDloader 是否真正支持 -SkipFileID。

    实测差异：
      v22.x 的 usage 只有 -pac / -port / -ReadNV，命令行传 -SkipFileID
            会被静默忽略（照样烧全部分区），不报错也不生效。
      v27.x 的 usage 明确列出 -SkipFileID，trace 里能看到 SkipFile(...)。

    做法：故意传 -SkipFileID 让工具打印 usage，再看参数表里有没有它。
    返回 (是否支持, 原始 usage 文本)。
    """
    try:
        proc = subprocess.run(
            [str(exe), "-SkipFileID"],
            cwd=str(Path(exe).parent),
            capture_output=True,
            timeout=timeout_sec,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"<probe failed: {exc}>"

    text = _decode(proc.stdout) + "\n" + _decode(proc.stderr)
    return ("skipfileid" in text.lower()), text


def unsupported_skip_file_id_message(exe: str | Path, usage: str) -> str:
    """版本不支持 -SkipFileID 时给用户的提示。"""
    head = usage.strip().splitlines()
    usage_line = next(
        (ln.strip() for ln in head if "cmddloader" in ln.lower()), "(usage unavailable)"
    )
    return (
        "This CmdDloader build does not support -SkipFileID.\n"
        f"  Tool   : {exe}\n"
        f"  Usage  : {usage_line}\n"
        "  Version 22.x accepts the flag but silently ignores it, so a\n"
        "  'skip' would still flash the whole package. -SkipFileID works\n"
        "  from v27.x (e.g. Download_R27.26.2304).\n"
        "  Fix    : point --tool-dir at a v26+/v27+ installation, or drop\n"
        "           --skip-file-id to flash everything."
    )


def invoke_flash_run(
    exe: str | Path,
    pac: str | Path,
    port: int,
    cfg: dict[str, Any],
    reset: bool = False,
    power_off: bool = False,
    write_sn1: str | None = None,
    write_sn2: str | None = None,
    ezmode: bool = False,
    skip_file_ids: str | None = None,
) -> FlashResult:
    if reset and power_off:
        raise ValueError("--reset and --power-off are mutually exclusive")

    args = [
        str(exe),
        "-pac",
        str(pac),
        "-port",
        str(port),
        "-timeout",
        str(cfg["TimeoutMs"]),
    ]
    if power_off:
        args.append("-poweroff")
    elif reset:
        args.append("-reset")
    if write_sn1:
        args += ["-writesn1", str(write_sn1)]
    if write_sn2:
        args += ["-writesn2", str(write_sn2)]
    if skip_file_ids:
        args += ["-SkipFileID", str(skip_file_ids)]
    if ezmode:
        args.append("-ezmode")

    print()
    ver = get_exe_version(exe)
    if ver:
        print(f"    Tool  : v{ver}  ({Path(exe).name})")
    print(">>> " + " ".join(args))

    start_ts = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=str(Path(exe).parent),
            capture_output=True,
            timeout=cfg["FlashRunTimeoutSec"],
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Flash timed out (>{cfg['FlashRunTimeoutSec']}s), process killed"
        ) from exc
    wall_sec = time.monotonic() - start_ts

    out = _decode(proc.stdout) + "\n" + _decode(proc.stderr)

    # 优先用输出里自带的耗时。老版本 CmdDloader(v22) 会输出
    # "Elapsed Times = 56s"，新版本(v27)不再输出该字段，
    # 这时回落到本地计时，保证两个版本都能显示真实耗时。
    elapsed: float | None = None
    if cfg.get("ElapsedPattern"):
        m = re.search(cfg["ElapsedPattern"], out)
        if m:
            elapsed = float(m.group(1))
    if elapsed is None:
        elapsed = round(wall_sec, 1)

    port_val: str | None = None
    if cfg.get("PortPattern"):
        m = re.search(cfg["PortPattern"], out)
        if m:
            port_val = "COM" + m.group(1)

    imei_val: str | None = None
    if cfg.get("ImeiPattern"):
        m = re.search(cfg["ImeiPattern"], out)
        if m:
            imei_val = m.group(1)

    return FlashResult(
        success=is_flash_success(out, cfg),
        output=out,
        elapsed_sec=elapsed,
        port=port_val,
        imei=imei_val,
        exit_code=proc.returncode,
    )
