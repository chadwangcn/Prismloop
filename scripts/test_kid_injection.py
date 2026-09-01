#!/usr/bin/env python3
"""kid-explore fixture 注入测试:模拟儿童持设备拍摄(摄像头+麦克风),取证输出。

前置:fixture 由 prepare_media_fixture.py 生成于 /tmp/prismloop-fixture-kid
  video-a/ (I420 820 帧 720x1280@24) + audio-a/ (PCM 48kHz 2ch)

步骤:
1. 连接 Pod(复用 ADB),安装/启动 injector 与 media-probe
2. push fixture 到 Pod
3. 视频注入(camera/sequence)+ 音频注入(audio/file)
4. 播放中多次截图取证(验证画面随时间推进)
5. 汇总 injector status + logcat + 截图到 reports/
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.config import load_config, create_acep_client  # noqa: E402

FIXTURE_DIR = Path("/tmp/prismloop-fixture-kid")
REMOTE_ROOT = "/data/local/tmp/prismloop/fixtures/kid"
INJECTOR_PKG = "cn.prismloop.mediainjector"
PROBE_PKG = "cn.prismloop.mediaprobe"
INJECTOR_APK = REPO / "media-injector/app/build/outputs/apk/debug/app-debug.apk"
PROBE_APK = REPO / "media-probe/build-artifacts/prismloop-media-probe-debug.apk"


def adb(adb_addr: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "-s", adb_addr, *args], capture_output=True, text=True, timeout=timeout)


def http(method: str, url: str, body: dict | None = None, timeout: int = 20) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    config = load_config()
    acep = create_acep_client(config)
    pod_id = next(iter(config.pods)).pod_id if hasattr(next(iter(config.pods)), "pod_id") else None
    pods = {p.pod_id: p for p in acep.list_pod()}
    pod = pods.get(pod_id) or next(iter(pods.values()))
    pod_id = pod.pod_id
    print(f"[1] Pod: {pod_id} ({pod.name}, online={pod.online}, spec={pod.spec_code})")

    report_dir = REPO / f"reports/kid-inject-{int(time.time())}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # ADB
    addr = acep.pod_adb_enable(pod_id)
    adb_addr = addr.address
    subprocess.run(["adb", "connect", adb_addr], capture_output=True, text=True)
    print(f"[2] ADB: {adb_addr}")

    # 安装(幂等)
    print("[3] 安装 injector + media-probe...")
    for pkg, apk in ((INJECTOR_PKG, INJECTOR_APK), (PROBE_PKG, PROBE_APK)):
        r = adb(adb_addr, "shell", f"pm list packages {pkg}")
        installed = pkg in r.stdout
        if not installed:
            r = adb(adb_addr, "install", "-t", str(apk), timeout=180)
            ok = r.returncode == 0
            print(f"    {apk.name}: {'installed' if ok else r.stdout + r.stderr}")
            if not ok:
                return 1
        else:
            print(f"    {pkg}: already installed")

    # fixture push(存在性检查,幂等)
    print("[4] push fixture...")
    r = adb(adb_addr, "shell", f"ls {REMOTE_ROOT}/video-a/manifest.json 2>/dev/null")
    if "manifest.json" not in r.stdout:
        adb(adb_addr, "shell", f"rm -rf {REMOTE_ROOT}")
        adb(adb_addr, "shell", f"mkdir -p {REMOTE_ROOT}")
        for name in ("video-a", "audio-a"):
            r = adb(adb_addr, "push", str(FIXTURE_DIR / name), f"{REMOTE_ROOT}/", timeout=1200)
            print(f"    push {name}: {'OK' if r.returncode == 0 else r.stderr[:200]}")
            if r.returncode != 0:
                return 1
    else:
        print("    fixture 已存在")

    # 启动 injector
    print("[5] 启动 injector...")
    adb(adb_addr, "shell", "am force-stop " + INJECTOR_PKG)
    adb(adb_addr, "shell", "am start -n " + INJECTOR_PKG + "/.MainActivity")
    time.sleep(3)
    adb(adb_addr, "forward", "tcp:18080", "tcp:18080")
    time.sleep(2)
    base = "http://127.0.0.1:18080"
    st = http("GET", f"{base}/status")
    print(f"    injector ready: camera={st['camera']} audio={st['audio']}")

    # 注入
    print("[6] 注入: camera sequence + audio file...")
    r1 = http("POST", f"{base}/camera/sequence", {"dir": f"{REMOTE_ROOT}/video-a", "fps": 24})
    print(f"    camera: {r1}")
    r2 = http("POST", f"{base}/audio/file", {"path": f"{REMOTE_ROOT}/audio-a/tone.pcm", "sampleRate": 48000, "channels": 2})
    print(f"    audio: {r2}")

    # media-probe 前台消费
    print("[7] 启动 media-probe...")
    adb(adb_addr, "shell", "am start -n " + PROBE_PKG + "/.MainActivity")
    time.sleep(6)

    # 播放中多次截图(34s 视频,取 3 个时间点覆盖三段画面)
    print("[8] 截图取证(3 个时间点)...")
    for i, wait in enumerate((5, 12, 22), start=1):
        time.sleep(wait if i == 1 else 7)
        shot = report_dir / f"kid-frame-{i}.png"
        adb(adb_addr, "shell", f"screencap -p /sdcard/kid-frame-{i}.png")
        adb(adb_addr, "pull", f"/sdcard/kid-frame-{i}.png", str(shot))
        st = http("GET", f"{base}/status")
        feeder = st.get("feeder") or {}
        if isinstance(feeder, str):
            feeder = json.loads(feeder)
        print(f"    t+{wait}s: frames_pushed={st.get('video_frames_pushed')} feeder_pushed={feeder.get('pushed')} err={feeder.get('lastError', '')[:60]}")

    # 终态取证
    st = http("GET", f"{base}/status")
    (report_dir / "injector-status.json").write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    r = adb(adb_addr, "shell", "logcat -d -s PodProxy InjectorService FrameFeeder MediaProbe | tail -50")
    (report_dir / "logcat.txt").write_text(r.stdout, encoding="utf-8")
    print(f"\n[OK] 证据: {report_dir}")
    print(json.dumps(st, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
