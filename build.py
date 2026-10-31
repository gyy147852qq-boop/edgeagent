#!/usr/bin/env python3
"""EdgeAgent 打包脚本（PyInstaller）。

用法：
    python build.py                 # 单文件 exe（默认，输出 dist/EdgeAgent.exe）
    python build.py --onedir        # 目录模式（启动更快，便于排障）
    python build.py --install       # 缺少 PyInstaller 时自动安装
    python build.py --icon-only     # 只生成图标
    python build.py --no-windowed   # 保留控制台（调试用）

产物：
    dist/EdgeAgent.exe              # --onefile
    dist/EdgeAgent/EdgeAgent.exe    # --onedir

打包要点：
- ``web/``、``config.example.yaml``、``.env.example``、``requirements.txt`` 作为数据文件带入；
- 图标由本脚本用纯 Python 生成 ICO（不依赖 Pillow）；
- ``--windowed`` 隐藏控制台，运行日志写 ``logs/app.log``（launcher 负责重定向）。
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
ICON_PATH = BUILD_DIR / "edgeagent.ico"
APP_NAME = "EdgeAgent"

#: 需要随包携带的只读资源（相对路径 → 包内目录）
DATA_FILES: tuple[tuple[str, str], ...] = (
    ("web", "web"),
    ("config.example.yaml", "."),
    (".env.example", "."),
    ("requirements.txt", "."),
)

#: uvicorn 的动态导入（PyInstaller 静态分析看不到，必须显式声明）
HIDDEN_IMPORTS: tuple[str, ...] = (
    "websockets",
    "websockets.asyncio.client",
    "websockets.legacy.client",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
)

EXCLUDES: tuple[str, ...] = (
    "pytest",
    "_pytest",
    "tests",
    # 网页版走 CDP（websockets + 系统 Edge），不再依赖 Playwright 的 node 内核。
    # 万一环境里装过 Playwright，排除掉以免把上百 MB 的 driver 拖进 exe。
    "playwright",
    "greenlet",
)


# ---------------------------------------------------------------------------
# 图标：纯 Python 生成 64x64 ICO（不依赖 Pillow）
# ---------------------------------------------------------------------------
def _rounded_rect_alpha(x: float, y: float, size: int, radius: float) -> bool:
    """判断像素是否落在圆角矩形内（四角用圆判定）。"""
    r = radius
    if r <= x < size - r or r <= y < size - r:
        return 0 <= x < size and 0 <= y < size
    cx = r if x < r else size - r
    cy = r if y < r else size - r
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_triangle(px: float, py: float, points: list[tuple[float, float]]) -> bool:
    """点是否在三角形内（重心坐标法）。"""
    (x1, y1), (x2, y2), (x3, y3) = points
    denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if denominator == 0:
        return False
    a = ((y2 - y3) * (px - x3) + (x3 - x2) * (py - y3)) / denominator
    b = ((y3 - y1) * (px - x3) + (x1 - x3) * (py - y3)) / denominator
    c = 1 - a - b
    return a >= 0 and b >= 0 and c >= 0


def make_icon(path: Path = ICON_PATH, size: int = 64) -> Path:
    """生成一个蓝底白色“播放/终端”图标的 ICO 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)

    bg = (47, 109, 246, 255)      # #2f6df6
    fg = (255, 255, 255, 255)
    triangle = [(24.0, 18.0), (46.0, 32.0), (24.0, 46.0)]

    # ICO 内嵌 32bpp BMP：像素行自下而上，BGRA
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            if _rounded_rect_alpha(x + 0.5, y + 0.5, size, 12.0):
                color = fg if _in_triangle(x + 0.5, y + 0.5, triangle) else bg
            else:
                color = (0, 0, 0, 0)
            b, g, r, a = color
            pixels += bytes((b, g, r, a))

    # AND 掩码：32bpp 已用 alpha，置 0 即可（每行按 4 字节对齐）
    mask_row = ((size + 31) // 32) * 4
    mask = bytes(mask_row * size)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,           # biSize
        size,         # biWidth
        size * 2,     # biHeight = 图像 + 掩码
        1,            # biPlanes
        32,           # biBitCount
        0,            # biCompression = BI_RGB
        len(pixels),  # biSizeImage
        0, 0, 0, 0,
    )
    image = header + bytes(pixels) + mask

    icon_dir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 22)
    path.write_bytes(icon_dir + entry + image)
    return path


# ---------------------------------------------------------------------------
# PyInstaller
# ---------------------------------------------------------------------------
def ensure_pyinstaller(auto_install: bool) -> bool:
    try:
        import PyInstaller  # noqa: F401

        return True
    except Exception:
        if not auto_install:
            print("[ERROR] 未安装 PyInstaller。执行：pip install pyinstaller（或加 --install 自动安装）")
            return False
        print("[BUILD] 正在安装 PyInstaller ...")
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "pyinstaller"],
            check=False,
        )
        if completed.returncode != 0:
            print("[ERROR] PyInstaller 安装失败，请检查网络或手动安装")
            return False
        return True


def build(onefile: bool, windowed: bool, clean: bool) -> int:
    if not ensure_pyinstaller(auto_install=True):
        return 1

    icon = make_icon()
    print(f"[BUILD] 图标：{icon}")

    args: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        APP_NAME,
        "--icon",
        str(icon),
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
    ]
    args.append("--onefile" if onefile else "--onedir")
    args.append("--windowed" if windowed else "--console")
    if clean:
        args.append("--clean")

    for src, dest in DATA_FILES:
        source = ROOT / src
        if not source.exists():
            print(f"[WARN] 缺少资源：{src}（已跳过）")
            continue
        args += ["--add-data", f"{source}{';' if sys.platform == 'win32' else ':'}{dest}"]

    for name in HIDDEN_IMPORTS:
        args += ["--hidden-import", name]
    for name in EXCLUDES:
        args += ["--exclude-module", name]

    args.append(str(ROOT / "launcher.py"))

    print("[BUILD] " + " ".join(args[2:]))
    completed = subprocess.run(args, cwd=str(ROOT), check=False)
    if completed.returncode != 0:
        print("[ERROR] 打包失败，请查看上方 PyInstaller 输出")
        return completed.returncode

    exe = DIST_DIR / f"{APP_NAME}.exe" if onefile else DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    if exe.is_file():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"[OK] 产物：{exe}（{size_mb:.1f} MB）")
        print("[OK] 双击即可运行；首次启动会在同级目录生成 config.yaml / .env / logs/")
        return 0

    print("[ERROR] 未找到预期产物，请检查 PyInstaller 输出")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EdgeAgent 打包脚本")
    parser.add_argument("--onedir", action="store_true", help="目录模式（默认单文件）")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（调试用）")
    parser.add_argument("--no-clean", action="store_true", help="不清理上次构建缓存")
    parser.add_argument("--icon-only", action="store_true", help="只生成图标文件")
    parser.add_argument("--install", action="store_true", help="缺少 PyInstaller 时自动安装（默认即自动）")
    args = parser.parse_args(argv)

    if args.icon_only:
        path = make_icon()
        print(f"[OK] 图标已生成：{path}")
        return 0

    if not (ROOT / "launcher.py").is_file():
        print("[ERROR] 未找到 launcher.py，请在项目根目录运行本脚本")
        return 1

    return build(onefile=not args.onedir, windowed=not args.console, clean=not args.no_clean)


if __name__ == "__main__":
    raise SystemExit(main())
