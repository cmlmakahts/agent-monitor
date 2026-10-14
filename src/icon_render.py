"""
托盘图标生成：纯代码绘制 16x16 图标，分层显示状态色 + 平台缩写
"""
from __future__ import annotations
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .models import AccountStatus, PlatformId


_LABEL = {
    PlatformId.CHATGPT: "G",
    PlatformId.VOLCENGINE: "V",
    PlatformId.QWEN: "Q",
}

# 中国市场习惯：红涨/绿跌——但本场景剩余度，绿 = 健康，红 = 危险；保持通行直觉
_STATUS_COLOR = {
    AccountStatus.ACTIVE: (76, 175, 80),     # #4CAF50
    AccountStatus.WARNING: (255, 193, 7),    # #FFC107
    AccountStatus.CRITICAL: (244, 67, 54),   # #F44336
    AccountStatus.EXPIRED: (158, 158, 158),  # #9E9E9E
    AccountStatus.UNKNOWN: (96, 125, 139),   # #607D8B
}


def _font(size: int = 12) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_tray_icon(
    overall_status: AccountStatus,
    overall_remaining: Optional[float],
    size: int = 64,
) -> Image.Image:
    """主托盘图标：圆点颜色 + 中心百分比文字"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(2, size // 12)
    # 外圈深色边
    d.ellipse([pad, pad, size - pad, size - pad], fill=(33, 33, 33, 255))
    # 内圈状态色
    inner = pad * 2
    color = _STATUS_COLOR.get(overall_status, _STATUS_COLOR[AccountStatus.UNKNOWN])
    d.ellipse([inner, inner, size - inner, size - inner], fill=color + (255,))

    # 文字（剩余 %）
    if overall_remaining is None:
        text = "—"
        font = _font(int(size * 0.42))
    else:
        text = f"{int(round(overall_remaining))}"
        font = _font(int(size * 0.50))

    # 居中
    try:
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        w = r - l
        h = b - t
    except AttributeError:
        w, h = d.textsize(text, font=font)
    d.text(
        ((size - w) / 2 - ({True: 0, False: 0}[isinstance(font, ImageFont.FreeTypeFont)]),
         (size - h) / 2 - 2),
        text,
        fill=(255, 255, 255, 255),
        font=font,
    )
    return img


def render_status_dot(status: AccountStatus, size: int = 16) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _STATUS_COLOR.get(status, _STATUS_COLOR[AccountStatus.UNKNOWN])
    pad = 1
    d.ellipse([pad, pad, size - pad, size - pad], fill=c + (255,))
    return img


def save_default_icon(path: Path):
    icon = render_tray_icon(AccountStatus.UNKNOWN, None, size=64)
    icon.save(path)
