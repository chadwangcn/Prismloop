#!/usr/bin/env python3
"""验证截图预览区的画面方向:对比原始视频帧的 4 个旋转假设。

预览区在 1080x1920 截图上部(TextureView 占 1:1 权重区域)。
取截图上半部分中心区,与原视频某帧(缩放到相近尺寸)的 0/90/180/270
旋转版本做平均绝对差,取最小者即当前显示方向。
"""

import sys

from PIL import Image

SCREENSHOT = sys.argv[1] if len(sys.argv) > 1 else "reports/rot-check.png"
VIDEO_FRAME = sys.argv[2] if len(sys.argv) > 2 else "/tmp/kid-frame-ref.png"


def load_region(img: Image.Image, box: tuple) -> Image.Image:
    return img.crop(box).convert("L").resize((120, 160))


def score(a: Image.Image, b: Image.Image) -> float:
    pa, pb = a.load(), b.load()
    total = 0
    for y in range(160):
        for x in range(120):
            total += abs(pa[x, y] - pb[x, y])
    return total / (120 * 160)


def main() -> None:
    shot = Image.open(SCREENSHOT).convert("RGB")
    W, H = shot.size  # 1080x1920
    # 预览 TextureView:标题(约 300px)之下,诊断区之上的弹性区域,取其中心 720x1080。
    center_box = ((W - 720) // 2, 420, (W + 720) // 2, 1500)
    region = load_region(shot, center_box)

    ref = Image.open(VIDEO_FRAME).convert("RGB")
    ref_region = ref.resize((720, 1080))  # 原视频竖屏帧
    ref_gray = ref_region.convert("L").resize((120, 160))

    results = {}
    for rotation in (0, 90, 180, 270):
        rotated = ref_gray.rotate(-rotation, expand=True).resize((120, 160))
        results[rotation] = score(region, rotated)

    best = min(results, key=results.get)
    print("平均绝对差(越小越匹配):")
    for rotation, value in results.items():
        marker = " <-- 当前显示方向" if rotation == best else ""
        print(f"  旋转 {rotation}°: {value:.2f}{marker}")


if __name__ == "__main__":
    main()
