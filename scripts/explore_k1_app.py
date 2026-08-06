"""探索 K1 APP:启动 → 截图 → dump UI → 尝试操作摄像头/麦/扬声器

使用方式:
    python scripts/explore_k1_app.py [--pod-id <id>]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.config import load_config, create_acep_client
from src.acep_client import DEFAULT_POD_K1

POD_ID = DEFAULT_POD_K1
PACKAGE = "com.lumi.osdemo"
OUTPUT_DIR = "/tmp/k1-app-explore"


def run(cmd: list[str], check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.stdout:
        print(result.stdout[:2000], end="" if len(result.stdout) <= 2000 else "...\n")
    if result.stderr and result.returncode != 0:
        print("STDERR:", result.stderr[:500], file=sys.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}")
    return result


def adb(adb_addr: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return run(["adb", "-s", adb_addr] + args, timeout=timeout)


def screenshot(adb_addr: str, name: str) -> str:
    """截图并拉取到本地"""
    path = f"/tmp/{name}.png"
    remote = f"/sdcard/{name}.png"
    adb(adb_addr, ["shell", "screencap", "-p", remote])
    adb(adb_addr, ["pull", remote, path])
    adb(adb_addr, ["shell", "rm", remote])
    print(f"   截图: {path}")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pod-id", default=POD_ID)
    parser.add_argument("--package", default=PACKAGE)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=== 1. 加载 ACEP 客户端,获取 ADB 地址 ===")
    config = load_config()
    acep = create_acep_client(config)
    # 先确保 pod 已开机
    acep.ensure_pods_powered_on([args.pod_id])
    # 获取 ADB 地址(已启用则用缓存)
    adb_addr_obj = acep.get_adb_address(args.pod_id)
    adb_addr = adb_addr_obj.address
    print(f"   ADB 地址: {adb_addr}")

    # adb connect
    run(["adb", "connect", adb_addr], check=False, timeout=15)
    time.sleep(1)

    print()
    print("=== 2. 启动应用 ===")
    # 用 monkey 启动 LAUNCHER
    adb(adb_addr, ["shell", "monkey", "-p", args.package,
                   "-c", "android.intent.category.LAUNCHER", "1"])
    time.sleep(3)  # 等待启动

    print()
    print("=== 3. 首页截图 ===")
    screenshot(adb_addr, "01-home")

    print()
    print("=== 4. dump UI 结构 ===")
    ui_result = adb(adb_addr, ["shell", "uiautomator", "dump", "--compressed", "/sdcard/ui.xml"])
    if "dumped" in (ui_result.stdout + ui_result.stderr):
        adb(adb_addr, ["pull", "/sdcard/ui.xml", f"{OUTPUT_DIR}/ui-home.xml"])
        adb(adb_addr, ["shell", "rm", "/sdcard/ui.xml"])
        print(f"   UI 结构: {OUTPUT_DIR}/ui-home.xml")
        # 简单提取 text 和 content-desc
        with open(f"{OUTPUT_DIR}/ui-home.xml", "r") as f:
            ui_xml = f.read()
        import re
        texts = re.findall(r'(?:text|content-desc)="([^"]+)"', ui_xml)
        texts = [t for t in texts if t]
        print(f"   可见文本({len(texts)} 项):")
        for t in texts[:30]:
            print(f"     - {t}")

    print()
    print("=== 5. 查询当前 Activity ===")
    adb(adb_addr, ["shell", "dumpsys", "activity", "activities"],
        timeout=10).stdout
    # 简化:用 dumpsys window
    focus_result = adb(adb_addr, ["shell", "dumpsys", "window", "|", "grep", "-E", "mCurrentFocus|mFocusedApp"])
    print(focus_result.stdout)

    print()
    print("=== 6. 尝试查找摄像头/麦克风/扬声器入口 ===")
    # 查找可能包含 camera/mic/speaker/audio 相关的 Activity
    print("--- 查询 APK 的 Activity 列表 ---")
    adb(adb_addr, ["shell", "dumpsys", "package", args.package],
        timeout=15)

    print()
    print("=== 7. 探索应用权限(摄像头/录音) ===")
    adb(adb_addr, ["shell", "pm", "list", "permissions", args.package])
    # 检查运行时权限授予情况
    adb(adb_addr, ["shell", "dumpsys", "package", args.package, "|", "grep", "-A1", "permission"])

    print()
    print("=== 完成 ===")
    print(f"截图保存在 /tmp/01-home.png")
    print(f"UI 结构保存在 {OUTPUT_DIR}/ui-home.xml")


if __name__ == "__main__":
    main()
