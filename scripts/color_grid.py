#!/usr/bin/env python3
"""输出截图的低分辨率颜色网格,用于定位预览区与象限布局。"""

import sys

from PIL import Image


def classify(r: int, g: int, b: int) -> str:
    if r > 150 and g < 100 and b < 100:
        return "R"
    if g > 150 and r < 100 and b < 100:
        return "G"
    if b > 150 and r < 100 and g < 100:
        return "B"
    if r > 200 and g > 200 and b > 200:
        return "W"
    if r < 60 and g < 60 and b < 60:
        return "."
    return "?"


def main() -> None:
    path = sys.argv[1]
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    shot = Image.open(path).convert("RGB")
    W, H = shot.size
    rows = int(cols * H / W / 2)  # 字符高宽比补偿
    small = shot.resize((cols, rows), Image.LANCZOS)
    px = small.load()
    print(f"{path} ({W}x{H}) 颜色网格 {cols}x{rows}:")
    for row in range(rows):
        print("".join(classify(*px[col, row]) for col in range(cols)))


if __name__ == "__main__":
    main()
