"""工具、PAC、串口设备的自动发现。

设备枚举完全基于 pyserial 的 list_ports，不依赖 pywin32，
也不需要调用 Get-PnpDevice。

注意：所有面向用户的输出一律使用英文，避免控制台编码问题。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from serial.tools import list_ports

from .config import _warn as warn, parse_vid_pid


class DiscoveryError(RuntimeError):
    """找不到工具 / PAC / 设备。"""


# ---------------------------------------------------------------------------
# 兜底扫描根：当前目录及祖先 + Program Files 下的厂商目录
# 不含任何写死的本机路径
# ---------------------------------------------------------------------------
_VENDOR_RE = re.compile(r"Unisoc|SPRD|Spreadtrum|ResearchDownload", re.IGNORECASE)


def fallback_scan_roots() -> list[Path]:
    roots: list[Path] = []

    cur = Path.cwd().resolve()
    for _ in range(6):
        roots.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent

    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if not base:
            continue
        b = Path(base)
        if not b.is_dir():
            continue
        try:
            for child in b.iterdir():
                if child.is_dir() and _VENDOR_RE.search(child.name):
                    roots.append(child)
        except OSError:
            continue

    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# CmdDloader.exe 定位
# 优先级：参数 > 配置 > 扫描根 > 兜底
# ---------------------------------------------------------------------------
def _locate_exe(base: str | Path) -> Path | None:
    """在给定路径下定位 CmdDloader.exe。

    宽容接受三种写法，省得用户去猜该指到哪一层：
      <...>\\Bin\\CmdDloader.exe   直接给 exe
      <...>\\Bin                   exe 所在目录
      <...>\\ResearchDownload      工具根目录（会自动往下找 Bin）
    """
    p = Path(base)
    if not p.exists():
        return None
    if p.is_file():
        return p.resolve() if p.name.lower() == "cmddloader.exe" else None

    direct = p / "CmdDloader.exe"
    if direct.is_file():
        return direct.resolve()

    nested = p / "Bin" / "CmdDloader.exe"
    if nested.is_file():
        return nested.resolve()

    try:
        hits = list(p.rglob("CmdDloader.exe"))
    except OSError:
        hits = []
    if hits:
        # 多个版本共存时取最新的那个
        return max(hits, key=lambda x: x.stat().st_mtime).resolve()
    return None


def find_cmddloader(
    cfg: dict[str, Any],
    tool_dir: str | Path | None = None,
    scan_roots: list[str] | None = None,
) -> Path:
    # 1) 显式参数
    if tool_dir:
        exe = _locate_exe(tool_dir)
        if exe:
            return exe
        raise DiscoveryError(
            f"CmdDloader.exe not found under: {tool_dir}"
        )

    # 2) 配置 / 环境变量
    if cfg.get("ToolDir"):
        exe = _locate_exe(cfg["ToolDir"])
        if exe:
            return exe

    # 3) 扫描根
    roots: list[Path] = []
    if scan_roots:
        roots = [Path(r) for r in scan_roots]
    elif cfg.get("ScanRoots"):
        roots = [Path(r) for r in cfg["ScanRoots"]]
    else:
        roots = fallback_scan_roots()

    hits: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            hits.extend(root.rglob("CmdDloader.exe"))
        except OSError:
            continue

    if not hits:
        raise DiscoveryError(
            "CmdDloader.exe not found. Use --tool-dir, or run: "
            "dloader config --tool-dir <dir>"
        )

    best = max(hits, key=lambda p: p.stat().st_mtime)
    if len(hits) > 1:
        print(
            f"[DISCOVERY] {len(hits)} CmdDloader found, "
            f"using newest: {best.parent.parent}"
        )
        print("        Pin it with 'config --tool-dir' to skip scanning next time")
    return best.resolve()


def get_exe_version(exe: str | Path) -> str | None:
    """读取 CmdDloader.exe 的文件版本号，读不到返回 None。

    用 ctypes 调 version.dll 的 VerQueryValueW 取 VS_FIXEDFILEINFO。
    非 Windows 环境下 ctypes.windll 不存在，直接降级返回 None。
    """
    try:
        import ctypes
        from ctypes import wintypes

        path = str(Path(exe).resolve())
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None

        class _VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
            ]

        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None

        info = ctypes.cast(ptr, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        return (
            f"{info.dwFileVersionMS >> 16}.{info.dwFileVersionMS & 0xFFFF}."
            f"{info.dwFileVersionLS >> 16}.{info.dwFileVersionLS & 0xFFFF}"
        )
    except Exception:
        return None


def describe_tool(exe: str | Path) -> str:
    """返回 '路径 (vX.Y.Z.W)' 形式的描述，读不到版本时只给路径。"""
    ver = get_exe_version(exe)
    return f"{exe}  (v{ver})" if ver else str(exe)


# ---------------------------------------------------------------------------
# PAC 定位
# 优先级：参数 > 配置 > 当前目录 *.pac > 当前目录 build\*\img\*.pac
# ---------------------------------------------------------------------------
def find_pac(cfg: dict[str, Any], pac: str | Path | None = None) -> Path:
    if pac:
        p = Path(pac)
        if not p.is_file():
            raise DiscoveryError(f"PAC file does not exist: {pac}")
        return p.resolve()

    if cfg.get("Pac"):
        configured = Path(cfg["Pac"])
        if configured.is_file():
            return configured.resolve()
        warn(
            "configured Pac does not exist, falling back to auto-discovery: "
            f"{configured}"
        )

    here = Path.cwd()
    cands = list(here.glob("*.pac"))

    # Unisoc SDK 标准产物结构：build/<target>_builddir/img/*.pac
    for img_dir in here.glob("build/*/img"):
        if img_dir.is_dir():
            cands.extend(img_dir.glob("*.pac"))

    if not cands:
        raise DiscoveryError(
            "PAC file not found. Use --pac, or run: "
            "dloader config --pac <file>"
        )

    best = max(cands, key=lambda p: p.stat().st_mtime)
    if len(cands) > 1:
        warn(f"{len(cands)} PAC files found, using the newest one")
    print(f"[DISCOVERY] Using PAC: {best}")
    return best.resolve()


# ---------------------------------------------------------------------------
# PAC 内嵌分区表（用于校验 --skip-file-id 的拼写）
# ---------------------------------------------------------------------------
_FILE_ID_RE = re.compile(rb"<ID>([A-Za-z0-9_]+)</ID>")


def list_pac_file_ids(pac: str | Path) -> list[str] | None:
    """从 PAC 内嵌的 BMAConfig XML 里读出 FileID 列表。

    PAC 外层是私有容器，但里面的 XML 是明文 ASCII，可以直接扫。
    读不出来时返回 None，调用方据此跳过校验（而不是误报）。
    """
    try:
        raw = Path(pac).read_bytes()
    except OSError:
        return None
    if b"<SchemeList>" not in raw:
        return None

    ids: list[str] = []
    for m in _FILE_ID_RE.finditer(raw):
        fid = m.group(1).decode("ascii", errors="ignore")
        if fid and fid not in ids:
            ids.append(fid)
    return ids or None


def validate_skip_file_ids(spec: str, pac: str | Path) -> list[str]:
    """校验 --skip-file-id 的内容，返回问题列表（空列表 = 没问题）。

    只警告不阻断：跳过一个不存在的 ID 本身无害，但拼错了却静默成功
    会让人以为分区被跳过了，所以必须提示。
    """
    problems: list[str] = []
    ids = [x.strip() for x in (spec or "").split(",") if x.strip()]
    if not ids:
        return ["--skip-file-id is empty"]

    seen: set[str] = set()
    for fid in ids:
        if fid.lower() in seen:
            problems.append(f"duplicate FileID '{fid}' in --skip-file-id")
        seen.add(fid.lower())

    known = list_pac_file_ids(pac)
    if known is None:
        warn("cannot read the partition list from PAC; skip IDs are not validated")
        return problems

    known_lower = {k.lower() for k in known}
    for fid in ids:
        if fid.lower() not in known_lower:
            problems.append(
                f"unknown FileID '{fid}' (not in this PAC, check spelling)"
            )
    return problems


# ---------------------------------------------------------------------------
# SPRD 设备枚举（pyserial，含 VID/PID）
# ---------------------------------------------------------------------------
def get_sprd_devices(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 [{"kind","com","description","vid","pid"}, ...]。

    kind: download | at | normal
    """
    dl = parse_vid_pid(cfg["DownloadVidPid"])
    nm = parse_vid_pid(cfg["NormalVidPid"])
    at_pat = cfg.get("AtNamePattern") or ""

    result: list[dict[str, Any]] = []
    for p in list_ports.comports():
        if p.vid is None or p.pid is None:
            continue
        pair = (p.vid, p.pid)
        if pair == dl:
            kind = "download"
        elif pair == nm:
            kind = "at" if at_pat and at_pat in (p.description or "") else "normal"
        else:
            continue
        result.append(
            {
                "kind": kind,
                "com": p.device,
                "description": p.description or "",
                "vid": p.vid,
                "pid": p.pid,
            }
        )
    return result


def get_download_port(cfg: dict[str, Any]) -> str | None:
    for d in get_sprd_devices(cfg):
        if d["kind"] == "download":
            return d["com"]
    return None


def get_at_port(cfg: dict[str, Any]) -> str | None:
    for d in get_sprd_devices(cfg):
        if d["kind"] == "at":
            return d["com"]
    return None
