"""
打包脚本：用 PyInstaller 生成单个 Windows 可执行文件 AgentMonitor.exe

运行：  .venv\Scripts\python.exe build.py
产物：  dist/AgentMonitor.exe
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

from src.icon_render import render_tray_icon
from src.models import AccountStatus


def ensure_icon() -> Path:
    asset_dir = Path(__file__).parent / "assets"
    asset_dir.mkdir(exist_ok=True)
    ico_path = asset_dir / "icon.ico"
    if ico_path.exists():
        return ico_path

    img = render_tray_icon(AccountStatus.ACTIVE, 75.0, size=256)
    # 生成多尺寸 ico
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    for s in sizes:
        images.append(img.resize((s, s), Image.Resampling.LANCZOS))
    images[0].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes])
    return ico_path


def main():
    root = Path(__file__).parent
    ico = ensure_icon()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "AgentMonitor",
        "--onefile",
        "--windowed",
        "--noconsole",
        "--icon", str(ico),
        "--add-data", f"assets{os.pathsep}assets",
        "--paths", str(root),
        "--paths", str(root / "src"),
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "PIL.ImageFont",
        "--hidden-import", "requests",
        "--hidden-import", "apscheduler",
        "--hidden-import", "keyring",
        "--hidden-import", "cryptography",
        "main.py",
    ]

    print("Running:\n  " + " ".join(cmd))
    subprocess.check_call(cmd)
    print(f"\n✅ Build done: {root / 'dist' / 'AgentMonitor.exe'}")


if __name__ == "__main__":
    main()
