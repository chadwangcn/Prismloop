#!/usr/bin/env python3
"""Pod 裸数据注入 PoC:部署 media-injector + media-probe 到 Pod,验证注入闭环。

步骤:
1. ACEP 启用 ADB 并连接
2. 安装 injector APK(含 proxysdk)与 media-probe APK(观察点)
3. push fixture(I420 帧流 + PCM)
4. 启动 injector 服务,adb forward 18080
5. POST /camera/sequence 开始注入帧序列
6. POST /audio/file 开始 PCM 循环注入
7. ACEP 截图取证(media-probe 前台,应显示注入画面)
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

INJECTOR_APK = REPO / "media-injector/app/build/outputs/apk/debug/app-debug.apk"
PROBE_APK = REPO / "media-probe/build-artifacts/prismloop-media-probe-debug.apk"
FIXTURE_DIR = Path("/tmp/prismloop-fixture")  # prepare_media_fixture.py 的输出

ADB_TARGET = "127.0.0.1:18080-local"  # 仅打印用;实际用 -s <addr>
INJECTOR_PKG = "cn.prismloop.mediainjector"
PROBE_PKG = "cn.prismloop.mediaprobe"


def adb(adb_addr: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["adb", "-s", adb_addr, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def http_post(url: str, body: bytes | None = None, timeout: int = 20) -> str:
    data = body if body is not None else b"{}"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def http_get(url: str, timeout: int = 10) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def main() -> int:
    config = load_config()
    acep = create_acep_client(config)
    pod_id = config.pods.get("k1", {}).get("pod_id", "")
    # prismloop-poc 是当前旗舰型实例
    pods = {p.pod_id: p for p in acep.list_pod()}
    pod = pods.get(pod_id) or next(iter(pods.values()))
    pod_id = pod.pod_id
    print(f"[1] Pod: {pod_id} ({pod.name}, online={pod.online}, spec={pod.spec_code})")
    if not pod.online:
        print("    Pod 离线,先开机...")
        acep.power_on([pod_id])
        for _ in range(24):
            time.sleep(5)
            if acep.detail_pod(pod_id).online:
                break
        else:
            print("    开机超时")
            return 1

    # ADB
    print("[2] 启用 ADB...")
    addr = acep.pod_adb_enable(pod_id)
    adb_addr = addr.address
    print(f"    ADB 地址: {adb_addr}")
    r = subprocess.run(["adb", "connect", adb_addr], capture_output=True, text=True)
    print(f"    adb connect: {r.stdout.strip() or r.stderr.strip()}")

    # 安装(先卸载避免签名冲突)
    print("[3] 安装 injector + media-probe...")
    for pkg, apk in ((INJECTOR_PKG, INJECTOR_APK), (PROBE_PKG, PROBE_APK)):
        adb(adb_addr, "uninstall", pkg)
        r = adb(adb_addr, "install", "-t", str(apk), timeout=180)
        ok = "Success" in r.stdout or r.returncode == 0
        print(f"    {apk.name}: {'OK' if ok else r.stdout + r.stderr}")
        if not ok:
            return 1

    # fixture
    print("[4] 推送 fixture...")
    remote_root = "/data/local/tmp/prismloop/fixtures"
    adb(adb_addr, "shell", f"mkdir -p {remote_root}")
    r = adb(adb_addr, "shell", f"ls {remote_root}/video-a 2>/dev/null | head -3")
    if "manifest.json" not in r.stdout:
        for name in ("video-a", "audio-a"):
            src = FIXTURE_DIR / name
            if src.is_dir():
                r = adb(adb_addr, "push", str(src), f"{remote_root}/", timeout=600)
                print(f"    push {name}: {'OK' if r.returncode == 0 else r.stderr[:200]}")
    else:
        print("    fixture 已存在,跳过 push")

    # 启动 injector 服务
    print("[5] 启动 injector 服务...")
    adb(adb_addr, "shell", "am force-stop " + INJECTOR_PKG)
    adb(adb_addr, "shell", "am start -n " + INJECTOR_PKG + "/.MainActivity")
    time.sleep(3)
    r = adb(adb_addr, "forward", "tcp:18080", "tcp:18080")
    print(f"    adb forward: rc={r.returncode}")
    time.sleep(2)

    # 检查服务
    try:
        st = http_get("http://127.0.0.1:18080/status")
        print(f"[6] injector /status: {st[:300]}")
    except Exception as e:
        print(f"[6] injector 服务未就绪: {e}")
        r = adb(adb_addr, "shell", "logcat -d -s InjectorService PodProxy HttpApi | tail -30")
        print(r.stdout)
        return 1

    # 视频注入
    print("[7] 开始视频注入(I420 帧序列)...")
    body = json.dumps({"dir": f"{remote_root}/video-a", "fps": 30}).encode()
    print("    " + http_post("http://127.0.0.1:18080/camera/sequence", body))

    # 等帧生效
    time.sleep(5)
    st = json.loads(http_get("http://127.0.0.1:18080/status"))
    feeder = st["feeder"] if isinstance(st.get("feeder"), dict) else json.loads(st.get("feeder", "{}"))
    print(f"[8] 推帧状态: pushed={feeder['pushed']} err={feeder['lastError'][:100]}")
    print(f"    camera session open={st['camera']}")

    # 音频注入
    print("[9] 开始音频注入(PCM 循环)...")
    body = json.dumps({
        "path": f"{remote_root}/audio-a/tone.pcm",
        "sampleRate": 48000, "channels": 2,
    }).encode()
    print("    " + http_post("http://127.0.0.1:18080/audio/file", body))

    time.sleep(3)

    # media-probe 前台观察
    print("[10] 启动 media-probe 观察注入效果...")
    adb(adb_addr, "shell", "am start -n " + PROBE_PKG + "/.MainActivity")
    time.sleep(5)

    # 截图取证
    print("[11] ACEP 截图取证...")
    shot_dir = REPO / f"reports/inject-poc-{int(time.time())}"
    shot_dir.mkdir(parents=True, exist_ok=True)
    result = acep.batch_screen_shot([pod_id])
    # 下载
    from src.acep_client import ACEPError  # noqa: E402
    shots = getattr(result, "shots", None) or getattr(result, "screen_shots", None) or []
    downloaded = []
    for i, s in enumerate(shots):
        url = getattr(s, "url", "") or getattr(s, "pre_signed_url", "")
        if not url:
            continue
        out = shot_dir / f"inject-{i}.png"
        try:
            urllib.request.urlretrieve(url, out)
            downloaded.append(out)
            print(f"    截图: {out}")
        except Exception as e:
            print(f"    下载失败: {e}")

    # logcat 取证
    print("[12] logcat 关键日志...")
    r = adb(adb_addr, "shell", "logcat -d -s PodProxy InjectorService FrameFeeder MediaProbe | tail -40")
    (shot_dir / "logcat.txt").write_text(r.stdout, encoding="utf-8")
    print("    已保存 logcat.txt")

    # 最终状态
    st = http_get("http://127.0.0.1:18080/status")
    (shot_dir / "injector-status.json").write_text(
        json.dumps(json.loads(st), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[OK] 证据目录: {shot_dir}")
    print(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
