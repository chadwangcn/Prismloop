#!/usr/bin/env python3
"""三视频自动轮播演示:按各自完整时长依次播放并循环。

用法: python3 scripts/playback_cycle.py [--rounds N] [--host 127.0.0.1:18080]
每个视频播完一遍(按帧数/fps 计算时长)后切换到下一个,无断流热切换。
"""
import argparse
import json
import time
import urllib.request

FIXTURES = [
    "/data/local/tmp/prismloop/fixtures/f43-1/video-a",
    "/data/local/tmp/prismloop/fixtures/f43-2/video-a",
    "/data/local/tmp/prismloop/fixtures/f43-3/video-a",
]


def post(host, path, body):
    req = urllib.request.Request(
        f"http://{host}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get(host, path):
    with urllib.request.urlopen(f"http://{host}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1:18080")
    ap.add_argument("--rounds", type=int, default=3, help="轮播圈数")
    args = ap.parse_args()

    for rnd in range(1, args.rounds + 1):
        for d in FIXTURES:
            # 从状态里读当前 feeder 帧数,推算播放一遍的时长
            # 帧数在切换后由 manifest 提供,改用固定:24fps,各视频 241/289/145 帧
            frames = {"f43-1": 241, "f43-2": 289, "f43-3": 145}[d.split("/")[-2]]
            duration = frames / 24 + 1  # 播完一遍 + 1s 缓冲
            r = post(args.host, "/camera/sequence", {"dir": d, "fps": 24})
            print(f"[round {rnd}] {d} ({duration:.0f}s) -> {r}", flush=True)
            time.sleep(duration)
    print("done", flush=True)


if __name__ == "__main__":
    main()
