#!/usr/bin/env python3
"""通过 ACEP 启用 ADB,然后本地 adb install 安装 APK 到云手机

使用方式:
    python scripts/install_apk_via_adb.py <pod_id> <apk_path> [--package <name>] [--launch]

流程:
1. 从 Keychain 读取凭证
2. ACEP detail_pod 查询 Pod 状态
3. 若 Pod 不在线,power_on
4. ACEP add_adb_key(若未添加)+ bind_adb_key_pods + pod_adb(enable=True)
5. 本地 adb connect <address>
6. adb install -r <apk>
7. (可选)adb shell pm list packages | grep <package>
8. (可选)adb shell monkey -p <package> -c android.intent.category.LAUNCHER 1
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Optional

# 添加项目根目录到 path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.config import load_config, create_acep_client
from src.acep_client import ACEPClient, ACEPError, ADBKeyNotConfigured, DEFAULT_POD_K1


POD_ID = "<K1_POD_ID>"
APK_PATH = "/Users/hydramr/Documents/App-Dev/Lumi-App-Android/k1-v1.2.40-debug.apk"
PACKAGE_NAME = "com.lumi.osdemo"


def run(cmd: list[str], check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """运行命令并打印"""
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print("STDERR:", result.stderr, end="", file=sys.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败(returncode={result.returncode}): {' '.join(cmd)}")
    return result


def main():
    parser = argparse.ArgumentParser(description="通过 ACEP+ADB 安装 APK 到云手机")
    parser.add_argument("pod_id", nargs="?", default=POD_ID, help="目标 Pod ID")
    parser.add_argument("apk_path", nargs="?", default=APK_PATH, help="本地 APK 路径")
    parser.add_argument("--package", default=PACKAGE_NAME, help="包名(用于验证和启动)")
    parser.add_argument("--launch", action="store_true", help="安装后启动应用")
    args = parser.parse_args()

    if not os.path.exists(args.apk_path):
        print(f"错误:APK 文件不存在: {args.apk_path}", file=sys.stderr)
        sys.exit(1)

    print(f"=== 安装 APK 到云手机 ===")
    print(f"Pod ID:    {args.pod_id}")
    print(f"APK:       {args.apk_path}")
    print(f"包名:      {args.package}")
    print(f"APK 大小:  {os.path.getsize(args.apk_path) / 1024 / 1024:.2f} MB")
    print()

    # 1. 加载配置和凭证
    print("[1/7] 加载 ACEP 客户端...")
    config = load_config()
    acep = create_acep_client(config)
    print(f"      Region: {acep.region}, ProductId: {acep.product_id}")
    print()

    # 2. 查询 Pod 状态
    print("[2/7] 查询 Pod 状态...")
    try:
        status = acep.detail_pod(args.pod_id)
        print(f"      online={status.online}, ip={status.intranet_ip}, eip={status.eip_address}")
    except ACEPError as e:
        print(f"      查询失败: {e}", file=sys.stderr)
        sys.exit(2)

    # 3. 开机(若未在线)
    if not status.online:
        print("[3/7] Pod 未在线,开机中...")
        acep.power_on([args.pod_id])
        # 轮询等待开机
        for i in range(24):  # 最多 120 秒
            time.sleep(5)
            try:
                status = acep.detail_pod(args.pod_id)
                if status.online:
                    print(f"      已开机(等待 {(i+1)*5}s)")
                    break
            except ACEPError:
                pass
        else:
            print("      开机超时", file=sys.stderr)
            sys.exit(3)
    else:
        print("[3/7] Pod 已在线,跳过开机")
    print()

    # 4. 添加 ADB Key(若未添加)
    print("[4/7] 配置 ADB Key...")
    adb_key_path = os.path.expanduser("~/.android/adbkey.pub")
    if not os.path.exists(adb_key_path):
        # 生成 ADB 密钥对
        print(f"      ADB 公钥不存在: {adb_key_path},生成中...")
        os.makedirs(os.path.dirname(adb_key_path), exist_ok=True)
        run(["adb", "start-server"], check=False, timeout=10)

    try:
        key_id = acep.add_adb_key(
            key_name="agent-adb-key-pod-level",
            public_key_path=adb_key_path,
            auth_type=1,  # root
            effect_type=2,  # 实例维度(pod-level),bind_adb_key_pods 要求
            annotation="lumi-test agent (pod-level)",
        )
        print(f"      ADB Key 已添加: key_id={key_id}")
    except ACEPError as e:
        if "already" in str(e).lower() or "duplicate" in str(e).lower():
            # 已存在,查询获取
            print(f"      ADB Key 已存在,查询...")
            keys = acep.list_adb_key()
            if not keys:
                print(f"      但 list_adb_key 返回空", file=sys.stderr)
                sys.exit(4)
            key_id = keys[0]["key_id"]
            print(f"      复用 key_id={key_id}")
        else:
            print(f"      添加 ADB Key 失败: {e}", file=sys.stderr)
            sys.exit(4)

    # 绑定 Key 到 Pod(若未绑定)
    try:
        acep.bind_adb_key_pods([args.pod_id], key_id=key_id)
        print(f"      ADB Key 已绑定到 Pod")
    except ACEPError as e:
        # 已绑定会报错,忽略
        print(f"      绑定结果(可能已绑定): {e}")
    print()

    # 5. 启用 ADB 获取连接地址
    print("[5/7] 启用 ADB 获取连接地址...")
    try:
        adb_addr = acep.pod_adb_enable(args.pod_id)
        print(f"      ADB 地址: {adb_addr.address}")
    except ACEPError as e:
        print(f"      启用 ADB 失败: {e}", file=sys.stderr)
        sys.exit(5)
    print()

    # 6. 本地 adb connect + install
    print("[6/7] 本地 ADB 连接并安装 APK...")
    # adb connect
    connect_result = run(["adb", "connect", adb_addr.address], check=False, timeout=15)
    if "connected" not in (connect_result.stdout + connect_result.stderr).lower():
        # 可能已连接
        print(f"      连接结果(可能已连接)")

    # 等待设备出现
    for i in range(10):
        devices_result = run(["adb", "devices"], check=False, timeout=5)
        if adb_addr.address in devices_result.stdout and "device" in devices_result.stdout:
            break
        time.sleep(1)
    else:
        print(f"      adb devices 未发现 {adb_addr.address}", file=sys.stderr)
        sys.exit(6)

    # adb install -r(覆盖安装,保留数据)
    print(f"      安装 APK(覆盖安装,-r)...")
    install_result = run(
        ["adb", "-s", adb_addr.address, "install", "-r", "-g", args.apk_path],
        check=False,
        timeout=300,  # 25MB APK 可能需要较长时间
    )
    install_output = (install_result.stdout + install_result.stderr).lower()
    if "success" in install_output:
        print(f"      安装成功")
    else:
        print(f"      安装失败,完整输出:")
        print(install_result.stdout)
        print(install_result.stderr, file=sys.stderr)
        sys.exit(7)
    print()

    # 7. 验证安装
    print("[7/7] 验证安装结果...")
    verify_result = run(
        ["adb", "-s", adb_addr.address, "shell", "pm", "list", "packages"],
        check=False,
        timeout=15,
    )
    if args.package in verify_result.stdout:
        print(f"      ✓ 包 {args.package} 已安装")

        # 查询版本
        version_result = run(
            ["adb", "-s", adb_addr.address, "shell", "dumpsys", "package", args.package],
            check=False,
            timeout=15,
        )
        for line in version_result.stdout.splitlines():
            line = line.strip()
            if line.startswith("versionName=") or line.startswith("versionCode="):
                print(f"      {line}")
                break
    else:
        print(f"      ✗ 包 {args.package} 未找到", file=sys.stderr)
        sys.exit(8)

    # 可选:启动应用
    if args.launch:
        print()
        print("启动应用...")
        run(
            ["adb", "-s", adb_addr.address, "shell",
             "monkey", "-p", args.package,
             "-c", "android.intent.category.LAUNCHER", "1"],
            check=False,
            timeout=15,
        )
        time.sleep(2)
        # 截图验证
        screenshot_path = f"/tmp/{args.pod_id}-after-launch.png"
        run(
            ["adb", "-s", adb_addr.address, "shell",
             "screencap", "-p", "/sdcard/launch-verify.png"],
            check=False, timeout=10,
        )
        run(
            ["adb", "-s", adb_addr.address, "pull",
             "/sdcard/launch-verify.png", screenshot_path],
            check=False, timeout=15,
        )
        print(f"      启动后截图: {screenshot_path}")

    print()
    print("=== 安装完成 ===")


if __name__ == "__main__":
    main()
