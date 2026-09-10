"""
智论助手 - 应用图标生成脚本
生成 assets/icon.ico（多尺寸）+ assets/icon.png
用法：python make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).parent / "assets"
ASSETS.mkdir(exist_ok=True)

BRAND = (37, 99, 235)       # 品牌蓝
BRAND_DARK = (23, 61, 153)
WHITE = (255, 255, 255)
ACCENT = (250, 204, 21)     # 强调黄


def _rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def make_icon(size: int) -> Image.Image:
    """画一个：圆角蓝底 + 柱状图 + 上升趋势线 的图标。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    s = size / 256.0  # 以 256 为基准缩放

    # 圆角背景 + 轻微渐变（用两层叠加近似）
    _rounded(d, (0, 0, size - 1, size - 1), int(56 * s), BRAND)
    _rounded(d, (0, int(size * 0.45), size - 1, size - 1), int(56 * s), BRAND_DARK)

    # 三根柱子
    bar_w = int(30 * s)
    gap = int(20 * s)
    base_y = int(196 * s)
    bars = [(int(60 * s), int(120 * s)), (int(60 * s + (bar_w + gap)), int(90 * s)),
            (int(60 * s + 2 * (bar_w + gap)), int(150 * s))]
    for x, h in bars:
        _rounded(d, (x, base_y - h, x + bar_w, base_y), int(8 * s), WHITE)

    # 上升趋势线 + 顶点圆点
    pts = [bars[0], bars[1], bars[2]]
    line = []
    for (x, h) in pts:
        line.append((x + bar_w / 2, base_y - h - int(14 * s)))
    d.line(line, fill=ACCENT, width=max(2, int(8 * s)), joint="curve")
    for (px, py) in line:
        r = max(2, int(9 * s))
        d.ellipse((px - r, py - r, px + r, py + r), fill=ACCENT)

    return img


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [make_icon(n) for n in sizes]

    # 主 PNG
    imgs[-1].save(ASSETS / "icon.png")
    print(f"已生成 {ASSETS / 'icon.png'}")

    # ICO（PIL 直接支持多尺寸）
    imgs[-1].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(n, n) for n in sizes],
    )
    print(f"已生成 {ASSETS / 'icon.ico'}（{', '.join(f'{n}x{n}' for n in sizes)}）")


if __name__ == "__main__":
    main()
