"""Open-port / COM holder diagnostics (Windows-oriented).

Separated from flash.py so CmdDloader invocation stays free of Win32 handle
scanning. All user-facing strings are English for console code-page safety.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

# Best-effort budget between handle-scan loop iterations (a single
# NtQueryObject / DuplicateHandle may still block longer on Windows).
HANDLE_SCAN_TIMEOUT_SEC = 3.0

_FAILURE_KEYS = re.compile(
    r"Result|Error|Fail|error|Timeout|Connect|Open port|PS\d+"
)

_OPEN_PORT_FAIL = re.compile(
    r"Open port fail|PS2264|access is denied|Access denied",
    re.IGNORECASE,
)

_SUSPECT_EXACT = {
    "dloader.exe",
    "cmddloader.exe",
    "logel.exe",
    "researchdownload.exe",
    "upgradedownload.exe",
}
_PYTHON_DLOADER_CMD = re.compile(
    r"(?:-m\s+dloader\b)|(?:[\\/]dloader[\\/](?:__main__|cli)\.py\b)",
    re.IGNORECASE,
)

_SystemExtendedHandleInformation = 64
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_ObjectNameInformation = 1
_PROCESS_DUP_HANDLE = 0x0040
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DUPLICATE_SAME_ACCESS = 0x00000002


class _SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t),
        ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", wintypes.ULONG),
        ("CreatorBackTraceIndex", wintypes.USHORT),
        ("ObjectTypeIndex", wintypes.USHORT),
        ("HandleAttributes", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
    ]


class _SYSTEM_HANDLE_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("NumberOfHandles", ctypes.c_size_t),
        ("Reserved", ctypes.c_size_t),
        ("Handles", _SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX * 1),
    ]


def is_open_port_failure(output: str) -> bool:
    """输出是否像「下载口打不开」（占口 / 幽灵口）。"""
    return bool(_OPEN_PORT_FAIL.search(output or ""))


def normalize_com_port(port: str | int | None) -> str | None:
    """把 17 / '17' / 'COM17' 归一成 'COM17'。"""
    if port is None:
        return None
    s = str(port).strip().upper()
    if not s:
        return None
    if s.isdigit():
        return f"COM{s}"
    m = re.fullmatch(r"COM\s*(\d+)", s)
    if m:
        return f"COM{m.group(1)}"
    return None


def guess_port_from_output(output: str) -> str | None:
    """从 CmdDloader 输出里尽量抠出 COM 口。"""
    if not output:
        return None
    m = re.search(r"PORT\s*=\s*COM(\d+)", output, re.IGNORECASE)
    if m:
        return f"COM{m.group(1)}"
    m = re.search(r"\bCOM(\d+)\b", output, re.IGNORECASE)
    if m:
        return f"COM{m.group(1)}"
    return None


def _com_device_path(com: str) -> str | None:
    """COM17 -> \\Device\\...（QueryDosDevice）。"""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.QueryDosDeviceW(com, buf, 1024)
    if not n:
        return None
    return buf.value.split("\x00", 1)[0] or None


def _com_token_bounded(name: str, com_l: str) -> bool:
    """``com17`` 作为完整口名出现，避免 ``com1`` 误匹配 ``com17``。"""
    if not com_l:
        return False
    return bool(re.search(rf"(?<![0-9a-z]){re.escape(com_l)}(?![0-9])", name))


def _path_components(path: str) -> list[str]:
    return [c for c in path.replace("/", "\\").split("\\") if c]


def _components_equal_or_suffix(longer: list[str], shorter: list[str]) -> bool:
    """路径分量完全相等，或 ``longer`` 以 ``shorter`` 整段分量序列结尾。"""
    if not shorter:
        return False
    if longer == shorter:
        return True
    if len(longer) < len(shorter):
        return False
    return longer[-len(shorter) :] == shorter


def _device_name_matches(handle_name: str, device_path: str, com: str) -> bool:
    """句柄对象名是否对应目标 COM（精确路径分量 / COM 号边界）。

    Windows 上同一 COM 可能表现为：
    ``\\Device\\USBSER000``、``\\Device\\Serial1``、``\\??\\COM17`` 等。
    不用裸 ``endswith``，避免 ``Serial1`` 误匹配 ``Serial10``、
    ``USBSER00`` 误匹配 ``USBSER000``。
    """
    if not handle_name:
        return False
    n = handle_name.strip().lower().rstrip("\\")
    d = (device_path or "").strip().lower().rstrip("\\")
    com_l = com.strip().lower()

    if d:
        nc, dc = _path_components(n), _path_components(d)
        if _components_equal_or_suffix(nc, dc) or _components_equal_or_suffix(dc, nc):
            return True

    if _com_token_bounded(n, com_l):
        return True

    for alias in (
        f"\\??\\{com_l}",
        f"\\\\.\\{com_l}",
        f"\\device\\{com_l}",
    ):
        # 别名已含完整 COM 号，endswith 不会把 com1 认成 com17
        if n == alias or n.endswith(alias):
            return True
    return False


def _enable_debug_privilege() -> None:
    """尽量启用 SeDebugPrivilege，便于 DuplicateHandle 其它进程句柄。"""
    advapi = ctypes.windll.advapi32
    k32 = ctypes.windll.kernel32

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [
            ("PrivilegeCount", wintypes.DWORD),
            ("Privileges", LUID_AND_ATTRIBUTES * 1),
        ]

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        k32.GetCurrentProcess(),
        0x0020 | 0x0008,  # TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY
        ctypes.byref(token),
    ):
        return
    try:
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(
            None, "SeDebugPrivilege", ctypes.byref(luid)
        ):
            return
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
        advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
    finally:
        k32.CloseHandle(token)


def _process_image_name(pid: int) -> str:
    k32 = ctypes.windll.kernel32
    access = _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_DUP_HANDLE
    h = k32.OpenProcess(access, False, pid)
    if not h:
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return f"pid={pid}"
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            path = buf.value
            return path.rsplit("\\", 1)[-1] or path
    finally:
        k32.CloseHandle(h)
    return f"pid={pid}"


def _object_name_from_handle(handle: int) -> str | None:
    ntdll = ctypes.windll.ntdll
    h = wintypes.HANDLE(handle)
    needed = wintypes.ULONG(0)
    ntdll.NtQueryObject(h, _ObjectNameInformation, None, 0, ctypes.byref(needed))
    if not needed.value:
        return None
    buf = ctypes.create_string_buffer(needed.value + 8)
    if ntdll.NtQueryObject(h, _ObjectNameInformation, buf, needed, ctypes.byref(needed)) != 0:
        return None
    length = int.from_bytes(buf[0:2], "little")
    if length <= 0:
        return None
    ptr_size = ctypes.sizeof(ctypes.c_void_p)
    ptr = int.from_bytes(buf[8:16] if ptr_size == 8 else buf[4:8], "little")
    if not ptr:
        return None
    try:
        return ctypes.wstring_at(ptr, length // 2)
    except OSError:
        return None


def find_processes_locking_com(
    port: str | int,
    exclude_pids: set[int] | None = None,
    timeout_sec: float = HANDLE_SCAN_TIMEOUT_SEC,
) -> list[tuple[int, str]]:
    """查出正在占用指定 COM 口的进程（扫系统句柄，按设备名匹配）。

    返回 [(pid, 进程名), ...]，已排除本进程。非 Windows 或查不到返回 []。
    ``timeout_sec`` 限制句柄扫描时长，超时则返回已找到的结果。
    """
    if sys.platform != "win32":
        return []

    com = normalize_com_port(port)
    if not com:
        return []

    device = _com_device_path(com)
    if not device:
        return []
    device_l = device.lower()

    skip = set(exclude_pids or ())
    skip.add(os.getpid())

    ntdll = ctypes.windll.ntdll
    k32 = ctypes.windll.kernel32
    my_pid = os.getpid()
    _enable_debug_privilege()
    deadline = time.monotonic() + max(0.1, float(timeout_sec))

    import msvcrt

    marker = None
    marker_handle = 0
    status = -1
    info = None
    base = 0
    entry_size = 0
    file_type_index: int | None = None

    try:
        marker = open(sys.executable, "rb")
        marker_handle = int(msvcrt.get_osfhandle(marker.fileno()))
    except OSError:
        marker = None
        marker_handle = 0

    try:
        size = 0x100000
        status = _STATUS_INFO_LENGTH_MISMATCH
        buf = ctypes.create_string_buffer(size)
        while (status & 0xFFFFFFFF) == _STATUS_INFO_LENGTH_MISMATCH:
            if time.monotonic() >= deadline:
                return []
            ret_len = wintypes.ULONG(0)
            status = ntdll.NtQuerySystemInformation(
                _SystemExtendedHandleInformation,
                buf,
                size,
                ctypes.byref(ret_len),
            )
            if (status & 0xFFFFFFFF) == _STATUS_INFO_LENGTH_MISMATCH:
                size = max(size * 2, int(ret_len.value) + 0x10000)
                if size > 512 * 1024 * 1024:
                    return []
                buf = ctypes.create_string_buffer(size)

        if status != 0:
            return []

        info = ctypes.cast(buf, ctypes.POINTER(_SYSTEM_HANDLE_INFORMATION_EX)).contents
        base = ctypes.addressof(info) + _SYSTEM_HANDLE_INFORMATION_EX.Handles.offset
        entry_size = ctypes.sizeof(_SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX)

        if marker_handle:
            for i in range(info.NumberOfHandles):
                if time.monotonic() >= deadline:
                    break
                entry = _SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX.from_address(
                    base + i * entry_size
                )
                if entry.UniqueProcessId != my_pid:
                    continue
                if int(entry.HandleValue) == marker_handle:
                    file_type_index = int(entry.ObjectTypeIndex)
                    break
    finally:
        if marker is not None:
            marker.close()

    if status != 0 or info is None:
        return []

    holders: dict[int, str] = {}
    access = _PROCESS_DUP_HANDLE | _PROCESS_QUERY_LIMITED_INFORMATION
    open_cache: dict[int, int] = {}

    def _proc_handle(pid: int) -> int:
        if pid in open_cache:
            return open_cache[pid]
        h = k32.OpenProcess(access, False, pid)
        open_cache[pid] = int(h) if h else 0
        return open_cache[pid]

    try:
        for i in range(info.NumberOfHandles):
            if time.monotonic() >= deadline:
                break
            entry = _SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX.from_address(
                base + i * entry_size
            )
            pid = int(entry.UniqueProcessId)
            if not pid or pid in skip or pid in holders:
                continue
            if file_type_index is not None and entry.ObjectTypeIndex != file_type_index:
                continue

            name: str | None = None
            if pid == my_pid:
                name = _object_name_from_handle(int(entry.HandleValue))
            else:
                hproc = _proc_handle(pid)
                if not hproc:
                    continue
                dup = wintypes.HANDLE()
                ok = k32.DuplicateHandle(
                    hproc,
                    wintypes.HANDLE(entry.HandleValue),
                    k32.GetCurrentProcess(),
                    ctypes.byref(dup),
                    0,
                    False,
                    _DUPLICATE_SAME_ACCESS,
                )
                if not ok:
                    st = ntdll.NtDuplicateObject(
                        hproc,
                        ctypes.c_void_p(entry.HandleValue),
                        k32.GetCurrentProcess(),
                        ctypes.byref(dup),
                        0,
                        0,
                        _DUPLICATE_SAME_ACCESS,
                    )
                    if (st & 0xFFFFFFFF) != 0:
                        continue
                try:
                    name = _object_name_from_handle(int(dup.value))
                finally:
                    k32.CloseHandle(dup)

            if name and _device_name_matches(name, device_l, com):
                holders[pid] = _process_image_name(pid)
    finally:
        for h in open_cache.values():
            if h:
                k32.CloseHandle(h)

    found = sorted(holders.items(), key=lambda x: (x[1].lower(), x[0]))
    return found


def _com_port_busy(com: str) -> bool | None:
    """试开 \\\\.\\COMx：True=被占，False=可开，None=不确定。"""
    k32 = ctypes.windll.kernel32
    path = f"\\\\.\\{com}"
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID or handle == -1 or handle == 0xFFFFFFFF:
        err = k32.GetLastError()
        if err in (5, 32):
            return True
        return None
    k32.CloseHandle(handle)
    return False


def _suspect_process_label(name: str, cmdline: str | None) -> str | None:
    """进程是否属于常见 SPRD 占口嫌疑；返回展示标签。"""
    base = (name or "").strip().lower()
    if base in _SUSPECT_EXACT:
        return name.strip() or base
    if base in {"python.exe", "pythonw.exe"} and cmdline and _PYTHON_DLOADER_CMD.search(
        cmdline
    ):
        return f"{name.strip()} (-m dloader)"
    return None


def find_suspect_serial_processes(
    exclude_pids: set[int] | None = None,
) -> list[tuple[int, str]]:
    """列出常见可能占 SPRD 口的进程（按名称/命令行，不含本进程）。"""
    if sys.platform != "win32":
        return []

    skip = set(exclude_pids or ())
    skip.add(os.getpid())

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    rows = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    found: list[tuple[int, str]] = []
    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or pid in skip:
            continue
        label = _suspect_process_label(
            str(row.get("Name") or ""),
            row.get("CommandLine"),
        )
        if label:
            found.append((pid, label))

    found.sort(key=lambda x: (x[1].lower(), x[0]))
    return found


def _format_pid_lines(rows: list[tuple[int, str]]) -> list[str]:
    return [f"  PID {pid}  {label}" for pid, label in rows]


def format_open_port_hint(
    port: str | int | None = None,
    holders: list[tuple[int, str]] | None = None,
    suspects: list[tuple[int, str]] | None = None,
) -> str:
    """Open port fail 提示：精确占口进程优先，再补充常见嫌疑进程。"""
    com = normalize_com_port(port)
    if holders is None and com:
        holders = find_processes_locking_com(com)
    if suspects is None:
        suspects = find_suspect_serial_processes()

    holder_pids = {pid for pid, _ in (holders or [])}
    extra_suspects = [
        (pid, label) for pid, label in (suspects or []) if pid not in holder_pids
    ]

    lines: list[str] = []
    if holders:
        target = com or "the download port"
        lines.append(f"Hint: these processes are using {target}:")
        lines.extend(_format_pid_lines(holders))
    elif com:
        busy = _com_port_busy(com)
        if busy is True:
            lines.append(
                f"Hint: {com} is in use, but the exact holder could not be identified"
            )
            lines.append("  (listing PID may need elevation / SeDebugPrivilege).")
        else:
            lines.append(
                f"Hint: {com} may be a stale ghost COM (visible but not usable)."
            )
    else:
        lines.append("Hint: download port may be busy or a stale ghost COM.")

    if extra_suspects:
        if holders:
            lines.append("Also running (may hold SPRD ports):")
        else:
            lines.append("Suspect processes currently running:")
        lines.extend(_format_pid_lines(extra_suspects))
        lines.append("  Close them if applicable, then retry;")
    else:
        lines.append("  Close any program using that serial port, then retry;")

    lines.append("  if it still fails, hard-enter download mode again.")
    return "\n".join(lines)


def show_flash_failure(
    output: str,
    port: str | int | None = None,
) -> None:
    lines = [ln.strip() for ln in output.splitlines() if _FAILURE_KEYS.search(ln)]
    if lines:
        print("  Failure output excerpt:")
        for ln in lines[-10:]:
            print("    " + ln)
    if is_open_port_failure(output):
        resolved = normalize_com_port(port) or guess_port_from_output(output)
        print(format_open_port_hint(port=resolved))
