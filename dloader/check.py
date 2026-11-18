"""Project compatibility check: layout + key firmware changes.

Exposes the root causes of "device won't boot after flashing" before they
are misdiagnosed as a flashing-tool failure. Everything is based on the
project source root, not the current working directory.

All user-facing text is English on purpose: Windows console code pages vary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    key: str
    title: str
    status: str          # PASS / FAIL / SKIP
    detail: str
    hint: str = ""


def _read(path: Path) -> str | None:
    """Read a source file as text.

    Returns None when the file cannot be read OR when it looks like DLP
    ciphertext: enterprise transparent-encryption agents (e.g. Esafenet)
    encrypt on read for processes outside their allow-list, so a packaged
    .exe sees binary garbage while python.exe sees plaintext.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    head = raw[:256]
    if b"Esafenet" in head or b"\x00" in raw[:64]:
        return None
    return raw.decode("utf-8", "ignore")


def _unreadable(f: Path) -> CheckResult:
    return CheckResult(
        "source", "source file", "SKIP",
        f"{f} not readable as text (DLP-encrypted?)",
        "run 'check' from a shell that can read plaintext (python/whitelisted)",
    )


def _find_first(root: Path, name: str) -> Path | None:
    for p in root.rglob(name):
        if p.is_file():
            return p
    return None


def check_pac(root: Path) -> CheckResult:
    hits = list(root.glob("build/*/img/*.pac")) or list(root.glob("*.pac"))
    if hits:
        return CheckResult("pac", "PAC", "PASS", str(hits[0]))
    return CheckResult(
        "pac", "PAC", "FAIL",
        "no PAC under build/*/img/",
        "build the project first, or pass --pac",
    )


def check_tool(root: Path) -> CheckResult:
    hits = list(root.rglob("CmdDloader.exe"))
    if hits:
        return CheckResult("tool", "CmdDloader", "PASS", str(hits[0]))
    return CheckResult(
        "tool", "CmdDloader", "FAIL",
        "CmdDloader.exe not found in source tree",
        "put ResearchDownload under tools/, or pass --tool-dir",
    )


def check_auto_power_on(root: Path) -> CheckResult:
    f = _find_first(root, "ref_param.c")
    if not f:
        return CheckResult("auto_power", "auto_power_on", "SKIP", "ref_param.c not found")
    txt = _read(f)
    if txt is None:
        return _unreadable(f)
    if re.search(r"auto_power_on\s*=\s*1", txt):
        return CheckResult("auto_power", "auto_power_on", "PASS", f"{f} forces =1")
    return CheckResult(
        "auto_power", "auto_power_on", "FAIL",
        f"{f} does not force auto_power_on = 1",
        "device stops at charging screen after flashing",
    )


def check_usb_enum(root: Path) -> CheckResult:
    f = _find_first(root, "usbservice_isr.c")
    if not f:
        return CheckResult(
            "usb_isr", "USB plug enumeration", "SKIP", "usbservice_isr.c not found"
        )
    txt = _read(f)
    if txt is None:
        return _unreadable(f)
    if "#if 0" in txt and "USB_StartService(USB_SERVICE_LOG)" in txt:
        return CheckResult(
            "usb_isr", "USB plug enumeration", "PASS", f"{f} uses #if 0 branch"
        )
    return CheckResult(
        "usb_isr", "USB plug enumeration", "FAIL",
        f"{f} may still use the LOG branch",
        "AT port may not appear after cable plug-in",
    )


def _usb_boot_log_path_ok(txt: str) -> bool:
    """Start LOG 附近是否在等 ``g_USBService_table[USB_SERVICE_LOG]``。

    仅出现 ``UCOM_init`` 不够（其它路径也可能调用）。
    """
    marker = "g_USBService_table[USB_SERVICE_LOG]"
    for m in re.finditer(r"USB_StartService\s*\(\s*USB_SERVICE_LOG\s*\)", txt):
        window = txt[max(0, m.start() - 900) : m.end() + 120]
        if marker in window:
            return True
    return False


def check_usb_boot(root: Path) -> CheckResult:
    """开机路径是否在 CHARGE/SOFTWARE/充电连接时起 USB_SERVICE_LOG。

    无条件 UDC_HWInit + NULL/UCOM 会导致主机看到 VID_0000。
    启发式：取树中第一个 ``usbservice_main.c``；注释里的符号也可能干扰判定。
    """
    f = _find_first(root, "usbservice_main.c")
    if not f:
        return CheckResult(
            "usb_boot", "USB boot enumeration", "SKIP", "usbservice_main.c not found"
        )
    txt = _read(f)
    if txt is None:
        return _unreadable(f)

    has_software = "RESTART_BY_SOFTWARE" in txt
    has_charge = "RESTART_BY_CHARGE" in txt
    has_start_log = bool(
        re.search(r"USB_StartService\s*\(\s*USB_SERVICE_LOG\s*\)", txt)
    )
    has_wait_log = _usb_boot_log_path_ok(txt)
    # 反模式：拉起 D+ 却不按 LOG 表起服务
    bad_null = "UDC_HWInit" in txt and "USB_SERVICE_NULL" in txt and not has_start_log
    bad_blind_ucom = (
        "UDC_HWInit" in txt
        and re.search(r"USB_StartService\s*\(\s*USB_SERVICE_UCOM\s*\)", txt)
        and not has_start_log
    )

    if bad_null or bad_blind_ucom:
        why = "UDC_HWInit + NULL/UCOM without Start LOG"
        return CheckResult(
            "usb_boot", "USB boot enumeration", "FAIL",
            f"{f} looks like {why}",
            "host may see VID_0000 / Device Descriptor Request Failed",
        )

    if has_software and has_start_log and has_wait_log:
        detail = f"{f} starts LOG on SOFTWARE"
        if has_charge:
            detail += "/CHARGE"
        return CheckResult("usb_boot", "USB boot enumeration", "PASS", detail)

    missing = []
    if not has_software:
        missing.append("RESTART_BY_SOFTWARE")
    if not has_start_log:
        missing.append("USB_StartService(USB_SERVICE_LOG)")
    if not has_wait_log:
        missing.append("g_USBService_table[LOG] near Start LOG")
    return CheckResult(
        "usb_boot", "USB boot enumeration", "FAIL",
        f"{f} missing {', '.join(missing)}",
        "after flash, PC may get VID_0000; wait for LOG table then Start LOG",
    )


def check_at_trigger(root: Path) -> CheckResult:
    """Look for the AutoDloader AT-trigger plumbing.

    The SDK spells it "AutoDloader" (config descriptor) rather than
    "AUTODLOADER", so match case-insensitively. Known files are checked
    first: they sit deep in the tree, and a capped scan can miss them.
    """
    known = ("ucom_config.c", "ucom_drv.c", "ref_outport.c", "auto_download.c")
    known_read = 0
    for name in known:
        f = _find_first(root, name)
        if f is None:
            continue
        txt = _read(f)
        if txt is None:
            continue
        known_read += 1
        if re.search(r"autodloader", txt, re.IGNORECASE):
            return CheckResult("at", "AT soft trigger", "PASS", f"{f} has AutoDloader plumbing")

    if known_read == 0:
        return CheckResult(
            "at", "AT soft trigger", "SKIP",
            "source files not readable as text (DLP-encrypted?)",
            "run 'check' from a shell that can read plaintext",
        )

    n = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".c", ".h"):
            continue
        n += 1
        if n > 2000:
            break
        txt = _read(p)
        if txt and re.search(r"autodloader", txt, re.IGNORECASE):
            return CheckResult("at", "AT soft trigger", "PASS", f"{p} has AutoDloader plumbing")

    return CheckResult(
        "at", "AT soft trigger", "SKIP",
        "AutoDloader not found in known files or first 2000 .c/.h",
        "verify at runtime with 'dloader trigger'",
    )


def check_fdl_reset(root: Path) -> CheckResult:
    f = _find_first(root, "watchdog_phy_v5.c")
    if not f:
        return CheckResult("fdl", "FDL reset", "SKIP", "watchdog_phy_v5.c not found")
    txt = _read(f)
    if txt is None:
        return _unreadable(f)
    if "CHIP_SOFT_ResetNormal" in txt:
        return CheckResult("fdl", "FDL reset", "PASS", f"{f} has CHIP_SOFT_ResetNormal")
    return CheckResult(
        "fdl", "FDL reset", "FAIL",
        f"{f} lacks CHIP_SOFT_ResetNormal",
        "device may stay in download mode after flashing",
    )


def run_checks(root: str | Path) -> list[CheckResult]:
    root = Path(root).resolve()
    return [
        check_pac(root),
        check_tool(root),
        check_auto_power_on(root),
        check_usb_enum(root),
        check_usb_boot(root),
        check_at_trigger(root),
        check_fdl_reset(root),
    ]


def print_report(results: list[CheckResult]) -> int:
    icon = {"PASS": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}
    print()
    print("======== Project compatibility check ========")
    fails = 0
    skips = 0
    for r in results:
        print(f"{icon.get(r.status, '[????]')} {r.title}: {r.detail}")
        if r.hint:
            print(f"         -> {r.hint}")
        if r.status == "FAIL":
            fails += 1
        elif r.status == "SKIP":
            skips += 1
    print()
    if fails:
        print(f"{fails} check(s) failed -- see hints above.")
    elif skips:
        print(
            f"No blocking FAIL. {skips} item(s) SKIPPED "
            "-- still verify manually / on device."
        )
    else:
        print("No blocking issues.")
    return 1 if fails else 0
