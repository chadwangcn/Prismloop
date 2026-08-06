"""harness 闭环 E2E 验证脚本

完整链路:
1. 加载 ACEP 客户端(Keychain 凭证)
2. 确保 Pod 已开机并启用 ADB
3. 启动 K1 APP (com.lumi.osdemo)
4. 构造 K1 设计合约(screen + state + adb_command)
5. 用 ScreenshotCollector + ADBNavigator + ACEPCapturer 采集
6. 用 VisualDiffer 对比实际截图与基线(首次运行自动建立基线)
7. 输出 result.json + manifest.json + diff-report.json(符合架构文档 06)

使用方式:
    python scripts/e2e_harness_screenshot.py [--pod-id <id>] [--package <name>] [--baseline <dir>]

输出:
    reports/<run-id>/
    ├── result.json              # 采集结果
    ├── manifest.json            # 证据清单
    ├── diff-report.json         # 视觉 diff 报告
    ├── screenshots/k1/          # 实际截图
    └── diffs/k1/                # diff 图(红色高亮差异)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.config import load_config, create_acep_client
from src.acep_client import DEFAULT_POD_K1
from src.adb_client import ADBClient
from src.screenshot_collector import (
    ADBNavigator,
    ACEPCapturer,
    ScreenshotCollector,
)
from src.visual_differ import VisualDiffer


# ============================================================================
# 常量
# ============================================================================


POD_ID = DEFAULT_POD_K1
PACKAGE = "com.lumi.osdemo"
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")


# ============================================================================
# K1 设计合约(用于 E2E 验证)
# ============================================================================


def build_k1_design_contract(package: str, baseline_dir: str = "") -> dict:
    """构造 K1 APP 的设计合约

    基于 explore_k1_app.py 探索结果:
    - 单 screen(MainActivity 同时是 HOME Activity)
    - 屏幕 640×480
    - UI 为纯图形界面(uiautomator dump 无文本)
    - 中间区域 bounds=[120,43][520,436] 可点击

    实际项目应从 FigmaParser 获取,此处用预构造合约验证 ScreenshotCollector。

    若 baseline_dir 已存在基线文件(如 entry-default.png),自动填充 baseline_image 字段。
    """
    contract = {
        "design_version": "k1-v1.2.40-debug",
        "screens": [
            {
                "id": "entry",
                "name": "K1 主页",
                "route": "/entry",
                "adb_launch_package": package,
                # MainActivity 同时是 HOME Activity,monkey 启动即可
                "states": [
                    {
                        "id": "tapped",
                        "trigger": "点击屏幕中间区域",
                        "adb_command": "input tap 320 240",
                    },
                ],
            },
        ],
        "flows": [],
        "tokens": {},
    }
    # 若 baseline 目录已存在基线文件,自动填充 baseline_image 字段
    if baseline_dir and os.path.isdir(baseline_dir):
        for screen in contract["screens"]:
            sid = screen["id"]
            default_baseline = os.path.join(baseline_dir, f"{sid}-default.png")
            if os.path.exists(default_baseline):
                screen["baseline_image"] = f"{sid}-default.png"
            for state in screen.get("states", []):
                stid = state["id"]
                state_baseline = os.path.join(baseline_dir, f"{sid}-{stid}.png")
                if os.path.exists(state_baseline):
                    state["baseline_image"] = f"{sid}-{stid}.png"
    return contract


# ============================================================================
# 主流程
# ============================================================================


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    parser = argparse.ArgumentParser(description="harness 闭环 E2E 验证")
    parser.add_argument("--pod-id", default=POD_ID, help="K1 Pod ID")
    parser.add_argument("--package", default=PACKAGE, help="APP 包名")
    parser.add_argument(
        "--reports-dir", default=REPORTS_DIR,
        help="报告输出根目录",
    )
    parser.add_argument(
        "--baseline-dir", default=None,
        help="设计基线图根目录(不传则用 reports/baseline/k1,首次运行自动建立)",
    )
    parser.add_argument(
        "--establish-baseline", action="store_true",
        help="首次运行:把本次采集的截图作为基线(后续运行不再覆盖)",
    )
    args = parser.parse_args()

    run_id = f"e2e-{int(time.time())}"
    run_dir = os.path.join(args.reports_dir, run_id)
    screenshots_dir = os.path.join(run_dir, "screenshots", "k1")
    diffs_dir = os.path.join(run_dir, "diffs", "k1")
    baseline_dir = args.baseline_dir or os.path.join(args.reports_dir, "baseline", "k1")
    os.makedirs(screenshots_dir, exist_ok=True)

    print("=" * 60)
    print(f"Prismloop - harness 闭环 E2E 验证")
    print(f"Run ID:    {run_id}")
    print(f"Pod ID:    {args.pod_id}")
    print(f"Package:   {args.package}")
    print(f"输出目录:   {run_dir}")
    print("=" * 60)
    print()

    # ----- 1. 加载 ACEP 客户端 -----
    print("[1/7] 加载 ACEP 客户端...")
    config = load_config()
    acep = create_acep_client(config)
    print(f"      Region: {acep.region}, ProductId: {acep.product_id}")
    print()

    # ----- 2. 确保 Pod 已开机并启用 ADB -----
    print("[2/7] 确保 Pod 已开机并启用 ADB...")
    acep.ensure_pods_powered_on([args.pod_id])
    adb_addr_obj = acep.get_adb_address(args.pod_id)
    adb_address = adb_addr_obj.address
    print(f"      ADB 地址: {adb_address}")
    print()

    # ----- 3. 构造 ADBClient + ADBNavigator + ACEPCapturer -----
    print("[3/7] 构造采集组件...")
    adb_client = ADBClient()
    # register_pod 需要 eip+port,从 adb_address 拆分
    if ":" in adb_address:
        host, port_str = adb_address.rsplit(":", 1)
        adb_client.register_pod(args.pod_id, host, int(port_str))
    else:
        adb_client.register_pod(args.pod_id, adb_address, 5555)

    # 确保 ADB 已连接
    target = adb_client.ensure_connected(args.pod_id)
    print(f"      ADB 已连接: {target}")

    navigator = ADBNavigator(adb_client)
    capturer = ACEPCapturer(acep)
    collector = ScreenshotCollector(
        capturer=capturer,
        navigator=navigator,
        output_dir=screenshots_dir,
        state_stable_sec=2.0,  # K1 APP 反应稍慢,等待 2 秒
    )
    print()

    # ----- 4. 启动 K1 APP(导航器在 navigate 时自动 launch) -----
    print("[4/7] 启动 K1 APP...")
    design_contract = build_k1_design_contract(args.package, baseline_dir=baseline_dir)
    # ScreenshotCollector 会根据 adb_launch_package 自动启动
    print(f"      设计合约: {design_contract['design_version']}")
    print(f"      screens: {[s['id'] for s in design_contract['screens']]}")
    print()

    # ----- 5. 用 ScreenshotCollector 采集 -----
    print("[5/7] 采集截图...")
    start_time = now_iso()
    result = collector.collect_all(
        pod_id=args.pod_id,
        role="k1",
        design_contract=design_contract,
    )
    end_time = now_iso()
    print(f"      截图数: {len(result.screenshots)}")
    print(f"      到达 screens: {result.reached_screens()}")
    for s in result.screenshots:
        status = "✓" if s.reached else "✗"
        path = os.path.relpath(s.local_path, run_dir) if s.local_path else ""
        print(f"      {status} {s.screen_id}/{s.state_id} -> {path}")
    print()

    # ----- 6. (可选)建立基线 / 执行 VisualDiffer -----
    # 如果指定 --establish-baseline,把本次截图复制到 baseline_dir 作为基线
    if args.establish_baseline:
        print("[6/7] 建立基线(本次采集作为后续 diff 基线)...")
        os.makedirs(baseline_dir, exist_ok=True)
        import shutil
        for s in result.screenshots:
            if s.reached and s.local_path and os.path.exists(s.local_path):
                basename = os.path.basename(s.local_path)
                shutil.copy2(s.local_path, os.path.join(baseline_dir, basename))
        print(f"      基线已保存到: {baseline_dir}")
        # 同时更新设计合约中的 baseline_image 字段
        for screen in design_contract["screens"]:
            sid = screen["id"]
            screen["baseline_image"] = f"{sid}-default.png"
            for state in screen.get("states", []):
                stid = state["id"]
                state["baseline_image"] = f"{sid}-{stid}.png"
        print()

    # 执行 VisualDiffer
    print("[6/7] 执行视觉 diff...")
    differ = VisualDiffer(
        baseline_dir=baseline_dir,
        actual_dir=screenshots_dir,
        diff_dir=diffs_dir,
        design_version=design_contract["design_version"],
        actual_version=run_id,
    )
    actual_screenshots_data = [
        {
            "screen_id": s.screen_id,
            "state_id": s.state_id,
            "local_path": os.path.basename(s.local_path) if s.local_path else "",
            "reached": s.reached,
            "error": s.error,
        }
        for s in result.screenshots
    ]
    diff_report = differ.diff(actual_screenshots_data, design_contract)
    diff_report_path = os.path.join(run_dir, "diff-report.json")
    diff_report.save(diff_report_path)
    print(f"      diff 报告: {diff_report_path}")
    print(f"      偏差总数: {diff_report.summary.get('total_diffs', 0)}")
    print(f"      Critical: {diff_report.summary.get('critical', 0)}")
    print(f"      Major: {diff_report.summary.get('major', 0)}")
    print(f"      Minor: {diff_report.summary.get('minor', 0)}")
    print(f"      通过: {diff_report.passed}")
    for d in diff_report.diffs:
        status = "CRIT" if d.severity == "Critical" else ("MAJ" if d.severity == "Major" else "MIN")
        print(f"      [{status}] {d.screen_id}/{d.state_id} ({d.type}): {d.description}")
    print()

    # ----- 7. 输出 result.json + manifest.json -----
    print("[7/7] 输出结果文件...")
    result_json = {
        "run_id": run_id,
        "case_id": "E2E-HARNESS-001",
        "title": "Prismloop harness 闭环 E2E 验证 - K1 APP 截图采集与视觉 diff",
        "priority": "P0",
        "type": "single",
        "status": "passed" if (result.reached_screens() and diff_report.passed) else "failed",
        "topology": {"pod_id": args.pod_id},
        "start_time": start_time,
        "end_time": end_time,
        "pod_id": args.pod_id,
        "role": "k1",
        "design_version": design_contract["design_version"],
        "package": args.package,
        "screenshots": [
            {
                "screen_id": s.screen_id,
                "state_id": s.state_id,
                "screenshot_id": s.screenshot_id,
                "reached": s.reached,
                "local_path": os.path.relpath(s.local_path, run_dir) if s.local_path else "",
                "error": s.error,
            }
            for s in result.screenshots
        ],
        "diff_report": {
            "passed": diff_report.passed,
            "summary": diff_report.summary,
            "diffs_count": len(diff_report.diffs),
        },
        "summary": {
            "total_screens": len(design_contract["screens"]),
            "reached_screens": len(result.reached_screens()),
            "total_screenshots": len(result.screenshots),
            "diff_passed": diff_report.passed,
            "passed": result.reached_screens() and diff_report.passed,
        },
    }
    result_path = os.path.join(run_dir, "result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
    print(f"      result.json: {result_path}")

    # manifest.json(对齐架构文档 06-2.2 证据清单)
    manifest = {
        "run_id": run_id,
        "case_id": "E2E-HARNESS-001",
        "start_time": start_time,
        "end_time": end_time,
        "evidence": {
            "screenshots": {
                "k1": [
                    {
                        "screenshot_id": s.screenshot_id,
                        "screen_id": s.screen_id,
                        "state_id": s.state_id,
                        "reached": s.reached,
                        "local_path": os.path.relpath(s.local_path, run_dir) if s.local_path else "",
                    }
                    for s in result.screenshots
                ],
                "guardian": [],
            },
            "recordings": {"k1": [], "guardian": []},
        },
    }
    manifest_path = os.path.join(run_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"      manifest.json: {manifest_path}")
    print()

    # ----- 汇总 -----
    print("=" * 60)
    print("Prismloop E2E 验证完成")
    print(f"  状态: {result_json['status'].upper()}")
    print(f"  截图: {result_json['summary']['total_screenshots']} 张")
    print(f"  到达 screens: {result_json['summary']['reached_screens']}/{result_json['summary']['total_screens']}")
    print(f"  Visual diff: {'PASSED' if diff_report.passed else 'FAILED'} (critical={diff_report.summary.get('critical', 0)}, major={diff_report.summary.get('major', 0)}, minor={diff_report.summary.get('minor', 0)})")
    print(f"  输出目录: {run_dir}")
    print("=" * 60)

    return 0 if result_json["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
