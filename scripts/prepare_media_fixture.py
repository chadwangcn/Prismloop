#!/usr/bin/env python3
"""准备 Pod 注入 fixture:视频 → I420 裸帧流,音频 → PCM。

产物布局(media-injector 的 FrameFeeder 消费):
  <out>/video-a/
    manifest.json  {"width":W,"height":H,"format":"i420","fps":F,"file":"video.bin","frames":N}
    video.bin      连续 I420 帧(w*h*3/2 字节/帧)
  <out>/audio-a/
    tone.pcm       48kHz 立体声 16bit PCM

用法:
  python3 scripts/prepare_media_fixture.py \
      --video assets/videos/test-pattern-640x480.mp4 \
      --audio assets/audios/test-tone-440hz.m4a \
      --out /tmp/prismloop-fixture
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def probe_int(video: Path, key: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={key}", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # 对 fps 等可能返回 "30/1" 形式
    return int(out.split("/")[0])


def build_video_fixture(video: Path, out_dir: Path, max_frames: int = 0,
                        rotate_180: bool = True) -> Path:
    """视频 → I420 裸帧流 + manifest.json。

    rotate_180: 注入链路补偿。PodRawFrame 链路(Proxy SDK putVideoFrame → 虚拟摄像头 →
    消费端按 sensorOrientation=90 旋转显示)整体会给推送帧叠加 180° 顺时针旋转
    (经四象限图案三次实验验证,与流尺寸无关)。预旋转 180°(hflip+vflip)使画面正立。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "video.bin"

    width = probe_int(video, "width")
    height = probe_int(video, "height")
    fps = probe_int(video, "avg_frame_rate") or probe_int(video, "r_frame_rate")

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    if max_frames > 0:
        cmd += ["-frames:v", str(max_frames)]
    if rotate_180:
        cmd += ["-vf", "hflip,vflip"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "yuv420p", str(raw)]
    subprocess.run(cmd, check=True)

    frame_bytes = width * height * 3 // 2
    frames = raw.stat().st_size // frame_bytes
    if raw.stat().st_size % frame_bytes != 0:
        raise SystemExit(f"raw size {raw.stat().st_size} not multiple of frame bytes {frame_bytes}")

    manifest = {
        "width": width,
        "height": height,
        "format": "i420",
        "fps": fps,
        "file": raw.name,
        "frames": frames,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[video] {width}x{height}@{fps} frames={frames} → {raw} ({raw.stat().st_size} bytes)")
    return out_dir


def build_audio_fixture(audio: Path, out_dir: Path) -> Path:
    """音频 → 48kHz 立体声 16bit PCM"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pcm = out_dir / "tone.pcm"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(audio),
         "-ar", "48000", "-ac", "2", "-f", "s16le", "-acodec", "pcm_s16le", str(pcm)],
        check=True,
    )
    print(f"[audio] 48kHz/2ch/s16le → {pcm} ({pcm.stat().st_size} bytes)")
    return pcm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", type=Path, default=REPO_ROOT / "assets/videos/test-pattern-640x480.mp4")
    ap.add_argument("--audio", type=Path, default=REPO_ROOT / "assets/audios/test-tone-440hz.m4a")
    ap.add_argument("--out", type=Path, default=Path("/tmp/prismloop-fixture"))
    ap.add_argument("--max-frames", type=int, default=0, help="限制解码帧数(调试)")
    ap.add_argument("--no-rotate-180", action="store_true",
                    help="禁用注入链路 180° 预旋转补偿(默认开启)")
    args = ap.parse_args()

    if args.video.exists():
        build_video_fixture(args.video, args.out / "video-a", args.max_frames,
                            rotate_180=not args.no_rotate_180)
    else:
        print(f"[video] skip, not found: {args.video}", file=sys.stderr)

    if args.audio.exists():
        build_audio_fixture(args.audio, args.out / "audio-a")
    else:
        print(f"[audio] skip, not found: {args.audio}", file=sys.stderr)

    print(f"\nfixture ready at {args.out}")
    print("push 到 Pod:")
    print(f"  adb -s <target> push {args.out}/video-a /data/local/tmp/prismloop/fixtures/")
    print(f"  adb -s <target> push {args.out}/audio-a /data/local/tmp/prismloop/fixtures/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
