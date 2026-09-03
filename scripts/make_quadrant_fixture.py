#!/usr/bin/env python3
"""生成四象限测试 fixture(单帧 I420):左上红/右上绿/左下蓝/右下白。"""

import json
import os
import sys

W, H = 360, 640
Y_SIZE = W * H


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/quadrant-fixture/video-a"
    y = bytearray(Y_SIZE)
    u = bytearray(Y_SIZE // 4)
    v = bytearray(Y_SIZE // 4)

    def set_px(x: int, row: int, r: int, g: int, b: int) -> None:
        yv = int(0.299 * r + 0.587 * g + 0.114 * b)
        uv = int(-0.169 * r - 0.331 * g + 0.5 * b) + 128
        vv = int(0.5 * r - 0.419 * g - 0.081 * b) + 128
        y[row * W + x] = max(0, min(255, yv))
        idx = (row // 2) * (W // 2) + (x // 2)
        u[idx] = max(0, min(255, uv))
        v[idx] = max(0, min(255, vv))

    for row in range(H):
        for col in range(W):
            if row < H // 2 and col < W // 2:
                set_px(col, row, 255, 0, 0)
            elif row < H // 2:
                set_px(col, row, 0, 255, 0)
            elif col < W // 2:
                set_px(col, row, 0, 0, 255)
            else:
                set_px(col, row, 255, 255, 255)

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "video.bin"), "wb") as fh:
        fh.write(y)
        fh.write(u)
        fh.write(v)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"width": W, "height": H, "format": "i420", "fps": 24, "file": "video.bin", "frames": 1},
            fh,
        )
    print(f"quadrant fixture ready: {out}")


if __name__ == "__main__":
    main()
