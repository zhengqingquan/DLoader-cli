"""串口交互：AT 命令、软触发进下载模式、等待端口出现。

注意：所有面向用户的输出一律使用英文，避免控制台编码问题。
"""

from __future__ import annotations

import time
from typing import Any

import serial

from .discovery import get_at_port, get_download_port, get_sprd_devices


def send_at(
    com: str,
    command: str,
    cfg: dict[str, Any],
    wait_ms: int | None = None,
    baud_rate: int | None = None,
    retries: int = 6,
) -> str:
    """向 AT 口发一条命令，返回压平换行后的响应。

    设备刚枚举出来时端口可能还打不开（Windows 报 OSError 22
    "The device does not exist"），所以打个短重试，最多 retries 次。
    """
    wait_ms = cfg["AtProbeWaitMs"] if wait_ms is None else wait_ms
    baud_rate = cfg["BaudRate"] if baud_rate is None else baud_rate
    timeout = max(cfg["AtProbeWaitMs"], 1000) / 1000.0

    last_exc: Exception | None = None
    raw = b""
    total = max(retries, 1)
    for attempt in range(1, total + 1):
        try:
            with serial.Serial(com, baud_rate, timeout=timeout, write_timeout=timeout) as ser:
                time.sleep(0.25)
                ser.reset_input_buffer()
                ser.write((command + "\r\n").encode("ascii", errors="ignore"))
                time.sleep(wait_ms / 1000.0)
                raw = ser.read(ser.in_waiting or 0)
            last_exc = None
            break
        except OSError as exc:  # SerialException 也是 OSError 的子类
            last_exc = exc
            if attempt < total:
                # 端口刚枚举出来时 Windows 还会短暂报 "device does not exist"，
                # 把重试过程打印出来，免得看起来像卡住了
                print(f"[INFO] {com} not ready yet (attempt {attempt}/{total}), retrying...")
            time.sleep(0.5)

    if last_exc is not None:
        raise RuntimeError(
            f"Serial port {com} error after {total} attempts: {last_exc}"
        ) from last_exc

    return raw.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ").strip()


def invoke_autoloader(com: str, cfg: dict[str, Any], wait_ms: int = 500) -> None:
    """软触发：让设备从正常模式切进下载模式。

    设备收到命令后会立刻断开 USB 重新枚举成下载口，
    所以这里只负责把命令发出去，不读响应，并吞掉断连异常。
    """
    if not com:
        raise ValueError("AT port is not specified, cannot soft trigger")

    timeout = max(cfg["AtProbeWaitMs"], 1000) / 1000.0
    try:
        ser = serial.Serial(com, cfg["BaudRate"], timeout=timeout, write_timeout=timeout)
    except Exception as exc:
        # 端口压根打不开（口被占用 / 设备不在正常模式）——这不是"设备切走"，
        # 必须立刻失败，否则上层仍会打印「已触发」
        raise RuntimeError(
            f"cannot open {com} to send the trigger command: {exc}"
        ) from exc

    try:
        time.sleep(0.25)
        ser.reset_input_buffer()
        ser.write((cfg["AutoLoaderCommand"] + "\r\n").encode("ascii", errors="ignore"))
        time.sleep(wait_ms / 1000.0)
    except Exception:
        # 命令已发出，设备立刻断开 USB 重新枚举成下载口，
        # 这里的写入/关闭报错属于预期行为，静默即可
        pass
    finally:
        try:
            ser.close()
        except Exception:
            pass


def wait_sprd_port(
    cfg: dict[str, Any],
    kind: str,
    timeout_sec: int | None = None,
    poll_ms: int | None = None,
    quiet: bool = False,
) -> str | None:
    """轮询等待某类端口出现。kind: download | at

    quiet=True 用于调用方自己已经打印了步骤提示的场景，避免重复刷屏。
    """
    if timeout_sec is None:
        timeout_sec = cfg["EnterDownloadSec"] if kind == "download" else cfg["BootWaitSec"]
    poll_ms = cfg["PollMs"] if poll_ms is None else poll_ms

    label = "download port" if kind == "download" else "AT port"
    if not quiet:
        print(f"[WAIT] Looking for {label} (up to {timeout_sec}s)...")

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        com = get_download_port(cfg) if kind == "download" else get_at_port(cfg)
        if com:
            return com
        time.sleep(poll_ms / 1000.0)

    if not quiet:
        print(f"[WAIT] {label} did not appear within {timeout_sec}s")
    return None


_KIND_TEXT = {
    "download": "download",
    "at": "normal-AT",
    "normal": "normal",
}


def show_ports(cfg: dict[str, Any]) -> None:
    devices = get_sprd_devices(cfg)
    if not devices:
        print(
            f"No device detected ({cfg['DownloadVidPid']} / {cfg['NormalVidPid']})"
        )
        return

    print()
    print(f"{'Kind':<12} {'Port':<10} Device")
    print(f"{'----':<12} {'----':<10} ------")
    for d in devices:
        print(f"{_KIND_TEXT.get(d['kind'], 'other'):<12} {d['com']:<10} {d['description']}")
    print()


def show_detect(
    cfg: dict[str, Any],
    tool_dir: str | None = None,
    pac: str | None = None,
    scan_roots: list[str] | None = None,
) -> None:
    from .config import config_path
    from .discovery import DiscoveryError, describe_tool, find_cmddloader, find_pac

    print()
    print("======== Environment probe ========")

    try:
        exe = find_cmddloader(cfg, tool_dir=tool_dir, scan_roots=scan_roots)
        print(f"[TOOL] {describe_tool(exe)}")
    except DiscoveryError as exc:
        print(f"[TOOL] not found -- {exc}")

    try:
        p = find_pac(cfg, pac=pac)
        st = p.stat()
        print(f"[PAC ] {p}")
        print(f"       {st.st_size:,} bytes")
    except DiscoveryError as exc:
        print(f"[PAC ] not found -- {exc}")

    print("[PORTS]")
    devices = get_sprd_devices(cfg)
    if not devices:
        print("       No device detected")
    else:
        for d in devices:
            print(
                f"       {_KIND_TEXT.get(d['kind'], 'other'):<12} "
                f"{d['com']:<8} {d['description']}"
            )

    cp = config_path()
    state = "exists" if cp.is_file() else "not created yet"
    print(f"[CONFIG] {cp} ({state})")
    print()
