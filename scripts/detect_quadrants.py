#!/usr/bin/env python3
"""精确象限采样:先定位预览彩色区域包围盒,再取四象限中心颜色。"""

import sys

from PIL import Image


def classify(r: int, g: int, b: int) -> str:
    if r > 150 and g < 100 and b < 100:
        return "红"
    if g > 150 and r < 100 and b < 100:
        return "绿"
    if b > 150 and r < 100 and g < 100:
        return "蓝"
    if r > 200 and g > 200 and b > 200:
        return "白"
    return f"({r},{g},{b})"


def main() -> None:
    path = sys.argv[1]
    shot = Image.open(path).convert("RGB")
    W, H = shot.size
    px = shot.load()

    def is_quadrant_color(x: int, y: int) -> bool:
        r, g, b = px[x, y]
        return (r > 150 and g < 100 and b < 100) or (g > 150 and r < 100 and b < 100) \
            or (b > 150 and r < 100 and g < 100)

    # 扫描彩色像素的包围盒(步长 8)
    min_x, min_y, max_x, max_y = W, H, 0, 0
    for y in range(0, H, 8):
        for x in range(0, W, 8):
            if is_quadrant_color(x, y):
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
    if min_x >= max_x:
        print("未找到彩色区域")
        return
    print(f"彩色区域包围盒: x[{min_x},{max_x}] y[{min_y},{max_y}]")

    bw, bh = max_x - min_x, max_y - min_y
    samples = {
        "区域左上": (min_x + bw // 4, min_y + bh // 4),
        "区域右上": (max_x - bw // 4, min_y + bh // 4),
        "区域左下": (min_x + bw // 4, max_y - bh // 4),
        "区域右下": (max_x - bw // 4, max_y - bh // 4),
    }
    observed = {}
    for name, (x, y) in samples.items():
        color = classify(*px[x, y])
        observed[name.replace("区域", "")] = color
        print(f"  {name}: {color} @{x},{y}")

    order = ["左上", "右上", "右下", "左下"]
    src = {"左上": "红", "右上": "绿", "右下": "白", "左下": "蓝"}
    for rotation in (0, 90, 180, 270):
        steps = rotation // 90
        expected = {pos: src[order[(i - steps) % 4]] for i, pos in enumerate(order)}
        if expected == observed:
            print(f"结论:显示内容相对源图案顺时针旋转 {rotation}°")
            return
    print("结论:非整图旋转(疑似缩放裁切/镜像),观测=" + str(observed))


if __name__ == "__main__":
    main()
