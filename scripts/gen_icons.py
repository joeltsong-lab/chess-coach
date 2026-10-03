# -*- coding: utf-8 -*-
"""生成 PWA 图标: static/mobile/icon-192.png 与 icon-512.png。

用法:
    pip install Pillow
    python scripts/gen_icons.py

样式: 背景 #1a1a2e, 中间一枚象棋棋子(米色圆盘 + 红边), 盘内一个红色「帅」字。
依赖 Pillow(仅生成图标时需要, 运行服务不需要)。
"""

import os
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # 没装 Pillow 时给一句人话
    sys.exit("缺少 Pillow, 请先执行:  pip install Pillow")

# 配色
BG = (26, 26, 46)          # #1a1a2e 深蓝背景
DISC = (243, 220, 178)     # #f3dcb2 棋子底色
DISC_HI = (255, 253, 247)  # #fffdf7 高光
RING = (176, 137, 78)      # #b08b4e 棋子描边
RED = (192, 57, 43)        # #c0392b 「帅」字

SIZES = (192, 512)
OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "mobile"

# 找一个能画中文的字体; 找不到就退回 Pillow 自带位图字体
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",     # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def load_font(px):
    """按优先级找字体, 都不行就用默认位图字体(中文会变方块, 但不会崩)"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_icon(size):
    """画一张 size×size 的图标"""
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # 棋子圆盘(留出边距, 视觉更像棋子而不是贴边圆)
    pad = size * 0.13
    box = (pad, pad, size - pad, size - pad)
    d.ellipse(box, fill=DISC, outline=RING, width=max(2, round(size * 0.022)))

    # 盘内一圈细描边, 增加层次
    inner = size * 0.29
    d.ellipse((size / 2 - inner, size / 2 - inner, size / 2 + inner, size / 2 + inner),
              outline=RING, width=max(1, round(size * 0.008)))

    # 中间的红「帅」
    font = load_font(round(size * 0.44))
    text = "帅"
    bbox = d.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    d.text((size / 2 - w / 2 - bbox[0], size / 2 - h / 2 - bbox[1]),
           text, font=font, fill=RED)
    return img


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        out = OUT_DIR / f"icon-{size}.png"
        draw_icon(size).save(out, "PNG")
        print(f"已生成 {out}  ({out.stat().st_size} 字节, {size}x{size})")
    print("图标生成完毕。")


if __name__ == "__main__":
    main()
