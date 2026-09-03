#!/usr/bin/env python3
"""扫描帧窗口的方向验证:对每个旋转假设取 ±window 帧内最优匹配,消除时间偏移。

用法: python3 scripts/check_rotation_scan.py <screenshot> <video.bin> <frame_index>
"""

import sys

from PIL import Image

WIDTH, HEIGHT = 360, 640
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2


def extract_i420_gray(path: str, index: int) -> Image.Image:
    y_size = WIDTH * HEIGHT
    with open(path, "rb") as fh:
        fh.seek(index * FRAME_BYTES)
        y = fh.read(y_size)
    return Image.frombytes("L", (WIDTH, HEIGHT), y)


def diff(a: Image.Image, b: Image.Image) -> float:
    pa, pb = a.tobytes(), b.tobytes()
    return sum(abs(x - z) for x, z in zip(pa, pb)) / len(pa)


def main() -> None:
    screenshot_path, video_path, frame_index = (
        sys.argv[1], sys.argv[2], int(sys.argv[3]),
    )
    window = int(sys.argv[4]) if len(sys.argv) > 4 else 60

    shot = Image.open(screenshot_path).convert("RGB")
    W, H = shot.size
    region = shot.crop(((W - 720) // 2, 420, (W + 720) // 2, 1500)).convert("L")

    results = {}
    for rotation in (0, 90, 180, 270):
        best = None
        best_frame = None
        for offset in range(-window, window + 1, 2):
            idx = (frame_index + offset) % 820
            gray = extract_i420_gray(video_path, idx)
            rotated = gray.rotate(rotation, expand=True).resize(region.size)
            value = diff(region, rotated)
            if best is None or value < best:
                best, best_frame = value, idx
        results[rotation] = (best, best_frame)

    best_rotation = min(results, key=lambda r: results[r][0])
    print(f"基准帧 {frame_index},窗口 ±{window}:")
    for rotation, (value, frame) in results.items():
        marker = " <-- 显示相对原帧的旋转" if rotation == best_rotation else ""
        print(f"  旋转 {rotation}°: 最小差 {value:.2f} (帧 #{frame}){marker}")


if __name__ == "__main__":
    main()
