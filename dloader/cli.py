"""命令行入口：dloader <command> [options]

注意：所有面向用户的输出（含 argparse 的帮助文本）一律使用英文，
避免 Windows 控制台代码页不一致导致的编码错误。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import config_path, load_config, save_config
from .device import (
    invoke_autoloader,
    send_at,
    show_detect,
    show_ports,
    wait_sprd_port,
)
from .discovery import (
    DiscoveryError,
    find_cmddloader,
    find_pac,
    get_at_port,
    get_download_port,
    get_sprd_devices,
    validate_skip_file_ids,
)
from .flash import (
    invoke_flash_run,
    probe_skip_file_id,
    show_flash_failure,
    unsupported_skip_file_id_message,
)


# ---------------------------------------------------------------------------
# 参数定义
# ---------------------------------------------------------------------------
def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pac", help="PAC file path (auto-discover by default)")
    p.add_argument("--tool-dir", help="directory containing CmdDloader.exe (auto-discover by default)")
    p.add_argument("--port", type=int, default=0, help="download port COM number (auto-discover by default)")
    p.add_argument("--at-port", help="AT port COM number (auto-discover by default)")
    p.add_argument("--timeout-ms", type=int, default=0, help="timeout for entering download mode")
    p.add_argument(
        "--scan-root",
        action="append",
        dest="scan_roots",
        metavar="DIR",
        help="root directory to search for CmdDloader, repeatable",
    )


def _add_flash_opts(
    p: argparse.ArgumentParser, *, allow_power_off: bool = True
) -> None:
    if allow_power_off:
        g = p.add_mutually_exclusive_group()
        g.add_argument(
            "--reset",
            action="store_true",
            help="reset to normal mode after flashing (implicit default unless --power-off)",
        )
        g.add_argument(
            "--power-off",
            action="store_true",
            help="power off after flashing (flash only; not valid with loop)",
        )
    else:
        # loop 始终 reset；保留 --reset 以免旧脚本/显式写法报错
        p.add_argument(
            "--reset",
            action="store_true",
            help="reset to normal mode after flashing (always on for loop)",
        )
    p.add_argument("--write-sn1", help="write IMEI1 / SN1")
    p.add_argument("--write-sn2", help="write IMEI2 / SN2")
    p.add_argument(
        "--skip-file-id",
        metavar="ID[,ID...]",
        help=(
            "comma separated FileIDs to skip, e.g. 'BOOTLOADER,CP,TFBOOT'. "
            "Requires CmdDloader v27+ (v22 accepts the flag but ignores it)"
        ),
    )
    p.add_argument("--ez-mode", action="store_true", help="compact output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dloader",
        description="Unisoc UIS8910 universal flashing CLI (Python)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  dloader detect\n"
            "  dloader ports\n"
            "  dloader flash --pac <PAC file>\n"
            "  dloader loop --rounds 5\n"
            "  dloader trigger\n"
            "  dloader config --tool-dir <Bin dir>\n"
            "\n"
            "On Open port fail / PS2264 / Access denied, flash and loop print an\n"
            "English Hint naming the COM holder (Windows) and common suspects\n"
            "(Logel / ResearchDownload / other dloader), then suggest hard-enter.\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"dloader {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    f = sub.add_parser("flash", help="flash once")
    _add_common(f)
    _add_flash_opts(f, allow_power_off=True)

    loop = sub.add_parser(
        "loop",
        help="fully automatic repeated flashing (AT soft trigger, no USB replug)",
    )
    _add_common(loop)
    # loop 依赖烧完自动开机回 AT，不能 --power-off
    _add_flash_opts(loop, allow_power_off=False)
    loop.add_argument("--rounds", type=int, default=0, help="number of rounds (default from config)")
    loop.add_argument(
        "--interval", type=int, default=-1, dest="interval_sec",
        help="seconds between rounds (default from config, 0 = no wait)",
    )

    t = sub.add_parser("trigger", help="only switch device into download mode, no flashing")
    t.add_argument("--at-port", help="AT port COM number (auto-discover by default)")

    sub.add_parser("ports", help="list current devices and serial ports")

    d = sub.add_parser("detect", help="probe tool / PAC / ports")
    d.add_argument("--pac", help="PAC file path")
    d.add_argument("--tool-dir", help="directory containing CmdDloader.exe")
    d.add_argument("--scan-root", action="append", dest="scan_roots", metavar="DIR")

    c = sub.add_parser("config", help="show or save configuration")
    c.add_argument("--tool-dir", help="pin CmdDloader directory")
    c.add_argument("--pac", help="pin PAC file")
    c.add_argument("--at-port", help="pin AT port COM number")
    c.add_argument("--timeout-ms", type=int, default=0)
    c.add_argument("--rounds", type=int, default=0)
    c.add_argument("--scan-root", action="append", dest="scan_roots", metavar="DIR")
    c.add_argument("--save", action="store_true", help="kept for compatibility; settings auto-save")

    ch = sub.add_parser("check", help="check project compatibility from source")
    ch.add_argument("--project", default=".", help="project source root (default: .)")

    pi = sub.add_parser("pacinfo", help="analyze a PAC package (no source needed)")
    pi.add_argument("--pac", help="PAC file path (default: auto-discover)")

    sub.add_parser("help", help="show help")

    return parser


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def resolve_at_port(cfg: dict[str, Any], given: str | None) -> str | None:
    if given:
        return given if not given.isdigit() else f"COM{given}"
    if cfg.get("AtPort") and int(cfg["AtPort"]) > 0:
        com = f"COM{cfg['AtPort']}"
        if any(d["com"] == com for d in get_sprd_devices(cfg)):
            return com
    return get_at_port(cfg)


def ensure_skip_file_id_supported(exe: str | Path, skip_ids: str | None) -> None:
    """使用了 --skip-file-id 时，先确认这个 CmdDloader 版本真的支持。

    v22.x 接受该参数但静默忽略，会让人误以为"只烧了某几个分区"，
    实际整包都烧了 —— 所以宁可提前报错，也不静默降级。
    """
    if not skip_ids:
        return
    ok, usage = probe_skip_file_id(exe)
    if not ok:
        raise RuntimeError(unsupported_skip_file_id_message(exe, usage))


def validate_inputs(args: argparse.Namespace, pac: str | Path) -> None:
    """校验用户输入，有问题就明确提示（警告不阻断，非法值才阻断）。"""
    if args.port < 0:
        raise ValueError(f"--port must be >= 0 (0 means auto-detect), got {args.port}")
    if args.skip_file_id:
        for problem in validate_skip_file_ids(args.skip_file_id, pac):
            print(f"[WARN] {problem}")


# ---------------------------------------------------------------------------
# 各命令
# ---------------------------------------------------------------------------
def cmd_flash(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    exe = find_cmddloader(cfg, args.tool_dir, args.scan_roots)
    ensure_skip_file_id_supported(exe, args.skip_file_id)
    pac = find_pac(cfg, args.pac)
    validate_inputs(args, pac)

    com = f"COM{args.port}" if args.port > 0 else get_download_port(cfg)
    if not com:
        print("[INFO] No download port found, trying AT soft trigger...")
        at = resolve_at_port(cfg, args.at_port)
        if at:
            invoke_autoloader(at, cfg)
            com = wait_sprd_port(cfg, "download")
    if not com:
        raise RuntimeError(
            "Device is not in download mode. Run 'dloader trigger' first, "
            "or power-cycle the device"
        )

    port_num = int(str(com).replace("COM", ""))
    do_reset = not args.power_off

    result = invoke_flash_run(
        exe,
        pac,
        port_num,
        cfg,
        reset=do_reset,
        power_off=args.power_off,
        write_sn1=args.write_sn1,
        write_sn2=args.write_sn2,
        ezmode=args.ez_mode,
        skip_file_ids=args.skip_file_id,
    )

    if result.success:
        elapsed = f"{result.elapsed_sec:g}s" if result.elapsed_sec else "?"
        print()
        print(f"[OK] Flash completed in {elapsed}")
        if result.port:
            print(f"       Port: {result.port}")
        if result.imei:
            print(f"       IMEI: {result.imei}")
        return 0

    print()
    print("[FAIL] Flash did not pass")
    show_flash_failure(result.output, port=port_num)
    print(f"       Exit code: {result.exit_code}")
    return 1


def cmd_loop(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    if getattr(args, "power_off", False):
        raise RuntimeError(
            "loop cannot use --power-off: closed-loop needs auto-boot back to AT. "
            "Use 'dloader flash --power-off' for a single power-off flash."
        )

    exe = find_cmddloader(cfg, args.tool_dir, args.scan_roots)
    ensure_skip_file_id_supported(exe, args.skip_file_id)
    pac = find_pac(cfg, args.pac)
    validate_inputs(args, pac)

    rounds = args.rounds if args.rounds > 0 else int(cfg["Rounds"])
    interval = args.interval_sec if args.interval_sec >= 0 else int(cfg["IntervalSec"])

    failed_at: int | None = None

    for i in range(1, rounds + 1):
        print()
        print(f"================ Round {i}/{rounds} ================")

        # 1) 等正常模式 AT 口
        at = resolve_at_port(cfg, args.at_port)
        if not at:
            print("[1] Waiting for AT port...")
            at = wait_sprd_port(cfg, "at", quiet=True)
        if not at:
            print("[1] FAIL: AT port not found")
            failed_at = i
            break
        print(f"[1] AT port = {at}")

        # 2) 确认在线
        try:
            resp = send_at(at, "AT", cfg)
            if "OK" not in resp:
                raise RuntimeError(f"AT no response: {resp}")
            print("[2] AT -> OK")
        except RuntimeError as exc:
            print(f"[2] FAIL: {exc}")
            failed_at = i
            break

        # 3) 软触发
        try:
            invoke_autoloader(at, cfg)
        except RuntimeError as exc:
            print(f"[3] FAIL: {exc}")
            failed_at = i
            break
        print(f"[3] Triggered {cfg['AutoLoaderCommand']}")

        # 4) 等下载口
        print("[4] Waiting for download port...")
        dl = wait_sprd_port(cfg, "download", quiet=True)
        if not dl:
            print("[4] FAIL: did not enter download mode")
            failed_at = i
            break
        port_num = int(str(dl).replace("COM", ""))
        print(f"[4] Download port = {dl}")

        # 5) 烧写（闭环必须 reset，不能 power-off）
        result = invoke_flash_run(
            exe,
            pac,
            port_num,
            cfg,
            reset=True,
            power_off=False,
            write_sn1=args.write_sn1,
            write_sn2=args.write_sn2,
            ezmode=args.ez_mode,
            skip_file_ids=args.skip_file_id,
        )
        if not result.success:
            print("[5] FAIL: flash did not pass")
            show_flash_failure(result.output, port=port_num)
            failed_at = i
            break
        if result.elapsed_sec:
            print(f"[5] Flash passed ({result.elapsed_sec:g}s)")
        else:
            print("[5] Flash passed")

        # 6) 等自动开机
        print("[6] Waiting for auto-boot...")
        at2 = wait_sprd_port(cfg, "at", quiet=True)
        if not at2:
            print("[6] FAIL: device did not auto-boot")
            failed_at = i
            break

        # 7) 确认退出下载模式
        if get_download_port(cfg):
            print("[7] FAIL: still in download mode")
            failed_at = i
            break
        print("[7] Auto-booted, download mode exited")

        # 8) AT 验证
        try:
            v = send_at(at2, "AT", cfg)
            if "OK" not in v:
                raise RuntimeError(f"AT verification failed: {v}")
            print(f"[8] AT({at2}) -> OK")
        except RuntimeError as exc:
            print(f"[8] FAIL: {exc}")
            failed_at = i
            break

        print(f"Round {i} closed-loop OK")
        if i < rounds and interval > 0:
            time.sleep(interval)

    print()
    if failed_at is None:
        unit = "round" if rounds == 1 else "rounds"
        print(f"Result: all passed ({rounds} {unit}, no manual intervention)")
        return 0
    print(f"Result: failed (round {failed_at})")
    return 1


def cmd_trigger(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    at = resolve_at_port(cfg, getattr(args, "at_port", None))
    if not at:
        raise RuntimeError(
            "AT port not found, cannot soft trigger. "
            "Make sure the device is booted normally"
        )

    print(f"[TRIGGER] Sending {cfg['AutoLoaderCommand']} to {at}")
    invoke_autoloader(at, cfg)
    dl = wait_sprd_port(cfg, "download")
    if dl:
        print(f"[OK] Entered download mode: {dl}")
        return 0

    print(f"[FAIL] Not in download mode within {cfg['EnterDownloadSec']}s")
    return 1


def cmd_ports(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    show_ports(cfg)
    return 0


def cmd_detect(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    show_detect(cfg, args.tool_dir, args.pac, args.scan_roots)
    return 0


def cmd_check(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from .check import print_report, run_checks

    return print_report(run_checks(args.project))


def cmd_pacinfo(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    from .discovery import find_pac
    from .pacinfo import parse_pac, print_pac_info

    pac = find_pac(cfg, args.pac)
    return print_pac_info(parse_pac(pac))


def cmd_config(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    updates: dict[str, Any] = {}
    if args.tool_dir:
        updates["ToolDir"] = args.tool_dir
    if args.pac:
        updates["Pac"] = str(Path(args.pac).resolve())
    if args.at_port:
        if str(args.at_port).isdigit():
            updates["AtPort"] = int(args.at_port)
        elif str(args.at_port).upper().startswith("COM"):
            updates["AtPort"] = int(str(args.at_port)[3:])
    if args.scan_roots:
        updates["ScanRoots"] = args.scan_roots
    if args.timeout_ms and args.timeout_ms > 0:
        updates["TimeoutMs"] = args.timeout_ms
    if args.rounds and args.rounds > 0:
        updates["Rounds"] = args.rounds

    if updates:
        path = save_config(updates)
        print(f"[CONFIG] Saved to {path}")
        cfg = load_config()

    print()
    print("Effective configuration:")
    print(f"  File: {config_path()}")
    print()
    print("  Paths:")
    print(f"    ToolDir : {cfg['ToolDir'] or '(unset, auto-discover)'}")
    print(f"    Pac     : {cfg['Pac'] or '(unset, auto-discover)'}")
    print(f"    AtPort  : {('COM' + str(cfg['AtPort'])) if cfg['AtPort'] else '(unset, auto-discover)'}")
    print()
    print("  Device identification:")
    print(f"    DownloadVidPid : {cfg['DownloadVidPid']}")
    print(f"    NormalVidPid   : {cfg['NormalVidPid']}")
    print(f"    AtNamePattern  : {cfg['AtNamePattern']}")
    print()
    print("  Serial / trigger:")
    print(f"    BaudRate          : {cfg['BaudRate']}")
    print(f"    AutoLoaderCommand : {cfg['AutoLoaderCommand']}")
    print()
    print("  Timeouts / waits:")
    for key in (
        "TimeoutMs", "EnterDownloadSec", "BootWaitSec",
        "FlashRunTimeoutSec", "AtProbeWaitMs", "PollMs",
    ):
        print(f"    {key:<19}: {cfg[key]}")
    print()
    print("  Loop:")
    print(f"    Rounds      : {cfg['Rounds']}")
    print(f"    IntervalSec : {cfg['IntervalSec']}")
    print()
    print("  Flash success patterns:")
    for pat in cfg["PassPatterns"]:
        print(f"    {pat}")
    print()
    print("  Scan roots:")
    if cfg.get("ScanRoots"):
        for r in cfg["ScanRoots"]:
            print(f"    {r}")
    else:
        print("    (unset, fallback: cwd + ancestors + Program Files vendor dirs)")
    print()
    return 0


def cmd_help(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    build_parser().print_help()
    return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
_HANDLERS = {
    "flash": cmd_flash,
    "loop": cmd_loop,
    "trigger": cmd_trigger,
    "ports": cmd_ports,
    "detect": cmd_detect,
    "check": cmd_check,
    "pacinfo": cmd_pacinfo,
    "config": cmd_config,
    "help": cmd_help,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    cfg = load_config()

    try:
        return _HANDLERS[args.command](args, cfg)
    except (RuntimeError, ValueError, DiscoveryError) as exc:
        print()
        print(f"[ERROR] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
