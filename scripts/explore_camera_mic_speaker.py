"""K1 APP 摄像头/麦克风/扬声器探索

1. 截图查看当前界面
2. 查询摄像头客户端状态
3. 查询音频路由和录制状态
4. 尝试 ACEP 屏幕串流 URL(实时观看)
5. 启动 ACEP 录屏(含音频)验证扬声器/麦克风
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.config import load_config, create_acep_client
from src.acep_client import DEFAULT_POD_K1

POD_ID = DEFAULT_POD_K1
ADB_ADDR = "122.228.93.250:10000"  # 已连接


def run(cmd, timeout=30):
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    # 截断输出
    if len(out) > 3000:
        out = out[:3000] + "\n... [截断]"
    print(out)
    if r.returncode != 0 and r.stderr:
        print("STDERR:", r.stderr[:500])
    return r


def adb(args, timeout=30):
    return run(["adb", "-s", ADB_ADDR] + args, timeout=timeout)


def screenshot(name):
    remote = f"/sdcard/{name}.png"
    local = f"/tmp/k1-{name}.png"
    adb(["shell", "screencap", "-p", remote])
    adb(["pull", remote, local])
    adb(["shell", "rm", remote])
    print(f"   截图保存: {local}")


def main():
    print("=== 1. ACEP 屏幕串流 URL(实时观看)===")
    config = load_config()
    acep = create_acep_client(config)
    try:
        url = acep.get_pre_signed_edge_url(POD_ID, ttl=3600)
        print(f"   串流 URL: {url}")
        print(f"   (浏览器打开此 URL 即可实时观看云手机画面)")
    except Exception as e:
        print(f"   获取失败: {e}")

    print()
    print("=== 2. 当前界面截图 ===")
    screenshot("02-current")

    print()
    print("=== 3. 摄像头状态 ===")
    adb(["shell", "dumpsys", "media.camera"])

    print()
    print("=== 4. 摄像头 HAL 设备列表 ===")
    adb(["shell", "ls", "-la", "/dev/video*"])

    print()
    print("=== 5. 音频路由详情 ===")
    adb(["shell", "dumpsys", "audio"])

    print()
    print("=== 6. 麦克风录音测试(3 秒)===")
    # 录制到 Pod 本地
    adb(["shell", "mediarecorder", "audiorecord", "/sdcard/mic-test.3gp", "3"],
        timeout=10)
    # 拉取
    adb(["pull", "/sdcard/mic-test.3gp", "/tmp/k1-mic-test.3gp"])
    adb(["shell", "rm", "/sdcard/mic-test.3gp"])

    print()
    print("=== 7. ACEP 录屏(5 秒,含音频)===")
    try:
        acep.start_recording(POD_ID, duration_limit=5)
        print("   录屏已启动,等待 5 秒...")
        time.sleep(6)
        result = acep.stop_recording(POD_ID)
        print(f"   录屏结果: {result}")
    except Exception as e:
        print(f"   录屏失败: {e}")

    print()
    print("=== 8. 尝试点击屏幕中心(进入/退出摄像头)===")
    # 640x480 中心点
    adb(["shell", "input", "tap", "320", "240"])
    time.sleep(2)
    screenshot("03-after-tap")

    print()
    print("=== 完成 ===")
    print("截图:")
    print("  /tmp/k1-02-current.png  - 当前界面")
    print("  /tmp/k1-03-after-tap.png - 点击后")
    print("  /tmp/k1-mic-test.3gp    - 麦克风录音")


if __name__ == "__main__":
    main()
