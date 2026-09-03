#!/usr/bin/env python3
"""精确方向验证:从 video.bin 抽出 injector 当前帧,与截图预览区做 4 方向比对。

用法: python3 scripts/check_rotation_exact.py <screenshot> <video.bin> <frame_index>
"""

import sys

from PIL import Image

WIDTH, HEIGHT = 360, 640
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2  # I420


def extract_i420_frame(path: str, index: int) -> Image.Image:
    y_size = WIDTH * HEIGHT
    with open(path, "rb") as fh:
        fh.seek(index * FRAME_BYTES)
        y = fh.read(y_size)
        u = fh.read(y_size // 4)
        v = fh.read(y_size // 4)
    # 快速上色:Y + (U,V) 最近邻
    pixels = bytearray(y_size * 3)
    for row in range(HEIGHT):
        for col in range(WIDTH):
            yy = y[row * WIDTH + col]
            uv_index = (row // 2) * (WIDTH // 2) + (col // 2)
            uu = u[uv_index] - 128
            vv = v[uv_index] - 128
            base = (row * WIDTH + col) * 3
            pixels[base] = max(0, min(255, int(yy + 1.402 * vv)))
            pixels[base + 1] = max(0, min(255, int(yy - 0.344 * uu - 0.714 * vv)))
            pixels[base + 2] = max(0, min(255, int(yy + 1.772 * uu)))
    return Image.frombytes("RGB", (WIDTH, HEIGHT), bytes(pixels))


def main() -> None:
    screenshot_path, video_path, frame_index = (
        sys.argv[1], sys.argv[2], int(sys.argv[3]),
    )
    shot = Image.open(screenshot_path).convert("RGB")
    W, H = shot.size
    # 预览 TextureView 弹性区域中心
    region = shot.crop(((W - 720) // 2, 420, (W + 720) // 2, 1500)).convert("L")

    frame = extract_i420_frame(video_path, frame_index).convert("L")

    results = {}
    for rotation in (0, 90, 180, 270):
        rotated = frame.rotate(rotation, expand=True).resize(region.size)
        # 拉普拉斯粗略差
        diff = sum(
            abs(a - b) for a, b in zip(region.getdata(), rotated.getdata())
        ) / (region.size[0] * region.size[1])
        results[rotation] = diff

    best = min(results, key=results.get)
    print("帧 #" + str(frame_index) + " 平均绝对差(越小越匹配):")
    for rotation, value in results.items():
        marker = " <-- 显示方向与原帧的相对旋转" if rotation == best else ""
        print(f"  旋转 {rotation}°: {value:.2f}{marker}")


if __name__ == "__main__":
    main()
