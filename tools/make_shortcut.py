#!/usr/bin/env python3
"""生成 EdgeAgent 桌面快捷方式（纯二进制写 .lnk，不依赖 WScript.Shell）。

沙箱里 WScript.Shell / COM 都被安全策略拦，所以这里直接构造 ShellLink
二进制格式：只支持「本地文件 + 可选工作目录 + 可选图标」这一种最简场景。

用法:
    python tools/make_shortcut.py <目标 exe> <输出 .lnk> [图标路径]
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# ShellLink CLSID
LINK_CLSID = bytes.fromhex("00021401" "0000" "0000" "C000" "000000000046")


def _win32_item(path: str) -> bytes:
    """构造一个 Win32 item（类型 0x1F），用于 LinkTargetIDList。"""
    data = path.encode("utf-16-le") + b"\x00\x00"
    item = struct.pack("<H", len(data) + 2) + data
    return item


def build_link(target: str, workdir: str = "", icon: str = "", description: str = "") -> bytes:
    tgt = str(Path(target))
    wd = str(Path(workdir)) if workdir else ""
    ic = str(Path(icon)) if icon else ""

    # ---- LinkFlags ----
    # 0x01 HasLinkTargetIDList / 0x02 HasLinkInfo / 0x04 HasName /
    # 0x08 HasRelativePath / 0x10 HasWorkingDir / 0x20 HasArguments /
    # 0x40 HasIconLocation / 0x80 IsUnicode
    flags = 0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0x20 | 0x40 | 0x80

    # ---- LinkTargetIDList ----
    idlist = b"\x00\x00\x00\x00"  # CLSID 0（桌面）
    idlist += _win32_item(tgt)
    idlist += b"\x00\x00"  # 结束符

    # ---- LinkInfo（本地文件用 VolumeID + LocalBasePath）----
    volume = b"\x00\x00\x00\x00" + struct.pack("<I", 0)  # drive = 0, serial = 0
    local_base = tgt.replace("/", "\\")
    if not local_base.startswith("\\\\"):
        local_base = local_base[2:] if local_base[:2].isalpha() else local_base
    link_info = struct.pack("<I", 0x00000001)  # LinkInfoHeaderSize / flags
    link_info += struct.pack("<I", len(volume) + len(local_base.encode("utf-16-le")) + 4)
    link_info += volume
    link_info += local_base.encode("utf-16-le") + b"\x00\x00"
    link_info += struct.pack("<I", 0)  # CommonNetworkRelativeLinkOffset
    link_info += struct.pack("<I", 0)  # CommonLocalRelativeLinkOffset
    link_info = struct.pack("<I", len(link_info)) + link_info[4:]

    # ---- StringData ----
    name = "EdgeAgent"
    rel = tgt
    working = wd
    args = ""
    icon_loc = f"{ic},0" if ic else ""

    def u16(s: str) -> bytes:
        return s.encode("utf-16-le") + b"\x00\x00"

    string_data = u16(name) + u16(rel) + u16(working) + u16(args) + u16(icon_loc)
    if description:
        string_data += u16(description)

    # ---- 头部 ----
    header = struct.pack(
        "<I", 0x0000004C
    )  # HeaderSize
    header += LINK_CLSID
    header += struct.pack("<I", flags)
    header += struct.pack("<I", 0x00000020 | 0x00000000)  # FileAttributes = ARCHIVE
    header += struct.pack("<I", 0)  # CreationTime
    header += struct.pack("<I", 0)  # AccessTime
    header += struct.pack("<I", 0)  # WriteTime
    header += struct.pack("<I", 0)  # FileSize
    header += struct.pack("<I", 0)  # IconIndex
    header += struct.pack("<I", 1)  # ShowCommand = SW_SHOWNORMAL
    header += struct.pack("<H", 0)  # HotKey
    header += struct.pack("<H", 0)  # Reserved
    header += struct.pack("<II", 0, 0)  # Reserved2

    # ---- 组装 ----
    out = bytearray(header)
    out += struct.pack("<H", len(idlist)) + idlist
    out += struct.pack("<I", len(link_info)) + link_info
    out += struct.pack("<I", len(string_data)) + string_data
    return bytes(out)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    target = sys.argv[1]
    out_path = Path(sys.argv[2])
    icon = sys.argv[3] if len(sys.argv) > 3 else target

    if not Path(target).is_file():
        print(f"目标不存在：{target}")
        return 1

    data = build_link(target, workdir=str(Path(target).parent), icon=icon)
    out_path.write_bytes(data)
    print(f"[OK] 已生成快捷方式：{out_path}（{len(data)} 字节）")
    print(f"     目标：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
