"""PAC 包结构分析（不依赖源码）。

能回答的问题：
- 这个 PAC 是给哪个产品/Flash 的（Product / FlashTypeID）
- 版本号、大小
- 烧录时哪些 NV 项会备份（IMEI 等会不会保留）
- 内嵌了哪些分区 / FileID

不能回答的（需要源码或实机验证）：
- 固件行为，例如 auto_power_on 是否生效、USB 是否复合枚举
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PacInfo:
    path: Path
    size: int
    version: str = ""
    product: str = ""
    scheme: str = ""
    flash_type: str = ""
    mode: str = ""
    nv_items: list[tuple[str, str, str]] = field(default_factory=list)  # name, id, backup
    file_ids: list[str] = field(default_factory=list)

    @property
    def keeps_imei(self) -> bool:
        return any("imei" in name.lower() and backup == "1" for name, _id, backup in self.nv_items)


def _u16_z(raw: bytes, start: int, maxlen: int = 64) -> str:
    end = start
    limit = min(len(raw), start + maxlen)
    while end + 1 < limit and not (raw[end] == 0 and raw[end + 1] == 0):
        end += 2
    try:
        return raw[start:end].decode("utf-16-le", "ignore").strip("\x00")
    except Exception:
        return ""


def parse_pac(path: str | Path) -> PacInfo:
    p = Path(path).resolve()
    raw = p.read_bytes()
    info = PacInfo(path=p, size=len(raw))

    # 头：版本串（UTF-16LE，如 BP_R1.0.0）
    info.version = _u16_z(raw, 0, 32)

    # 内嵌明文 XML
    i = raw.find(b"<BMAConfig")
    if i < 0:
        return info
    j = raw.find(b"</BMAConfig>", i)
    xml = raw[i : (j + 12 if j >= 0 else len(raw))].decode("ascii", "replace")

    m = re.search(r'<Product name="([^"]+)"', xml)
    if m:
        info.product = m.group(1)
    m = re.search(r"<SchemeName>([^<]+)</SchemeName>", xml)
    if m:
        info.scheme = m.group(1)
    m = re.search(r"<FlashTypeID>([^<]+)</FlashTypeID>", xml)
    if m:
        info.flash_type = m.group(1)
    m = re.search(r"<Mode>([^<]+)</Mode>", xml)
    if m:
        info.mode = m.group(1)

    # NVItem name / ID / backup
    for name, attrs, body in re.findall(r'<NVItem name="([^"]+)"([^>]*)>(.*?)</NVItem>', xml, re.S):
        idm = re.search(r"<ID>([^<]+)</ID>", body)
        bk = "1" if 'backup="1"' in attrs else "0"
        info.nv_items.append((name, idm.group(1) if idm else "", bk))

    # 分区 FileID（字母名，排除 0x 开头的 NV ID）
    seen: set[str] = set()
    for fid in re.findall(r"<ID>([A-Za-z0-9_]+)</ID>", xml):
        if fid.lower().startswith("0x") or fid in seen:
            continue
        seen.add(fid)
        info.file_ids.append(fid)

    return info


def print_pac_info(info: PacInfo) -> int:
    print()
    print("======== PAC info ========")
    print(f"File      : {info.path}")
    print(f"Size      : {info.size:,} bytes")
    print(f"Version   : {info.version or '(unknown)'}")
    print(f"Product   : {info.product or '(unknown)'}")
    if info.scheme:
        print(f"Scheme    : {info.scheme}")
    if info.flash_type:
        print(f"FlashType : {info.flash_type}")
    if info.mode:
        print(f"Mode      : {info.mode}")

    print()
    if info.file_ids:
        print(f"Partitions ({len(info.file_ids)}):")
        print("  " + ", ".join(info.file_ids))
    else:
        print("Partitions: (not found)")

    print()
    if info.nv_items:
        print(f"NV backup items ({len(info.nv_items)}):")
        for name, nid, bk in info.nv_items:
            print(f"  {'keep ' if bk == '1' else 'reset'} {name:<22} {nid}")
        print()
        print("IMEI kept on flash : " + ("YES" if info.keeps_imei else "NO"))
    else:
        print("NV backup items: (not found)")

    print()
    print("Note: this only covers PAC structure/NV policy. Firmware behaviour")
    print("      (auto_power_on, USB enumeration) needs source or a real flash.")
    return 0
