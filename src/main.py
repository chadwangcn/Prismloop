"""APP 自动测试系统主入口

用法:
    python -m src.orchestrator --task functional
    python -m src.orchestrator --case GAPP-DUAL-001
    python -m src.orchestrator --list-cases
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .case_loader import CaseLoader, CasePriority
from .config import (
    create_adb_client,
    create_mua_client,
    create_pod_pool,
    create_sdk_client,
    load_config,
)
from .orchestrator import Orchestrator
from .reporter import Reporter


def setup_logging(verbose: bool = False) -> None:
    """配置日志"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def cmd_list_cases(args: argparse.Namespace) -> int:
    """列出所有用例"""
    loader = CaseLoader(args.cases_dir)
    cases = loader.load_all()
    print(f"共 {len(cases)} 个用例(目录: {args.cases_dir}):")
    for c in cases:
        print(
            f"  [{c.priority.value}] [{c.type.value}] {c.case_id}: {c.title}"
            f"  ({len(c.steps)} 步)"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """执行用例"""
    config = load_config(args.env)
    mua = create_mua_client(config)
    adb = create_adb_client(config)
    sdk = create_sdk_client(config, adb_client=adb)
    pool = create_pod_pool(config)
    reporter = Reporter(args.reports_dir)
    orchestrator = Orchestrator(
        mua_client=mua,
        sdk_client=sdk,
        adb_client=adb,
        pod_pool=pool,
        reporter=reporter,
        tos_config=config.tos,
    )

    loader = CaseLoader(args.cases_dir)
    all_cases = loader.load_all()

    # 筛选用例
    if args.case:
        selected = [c for c in all_cases if c.case_id in args.case]
        if not selected:
            print(f"未找到用例: {args.case}")
            return 1
    elif args.priority:
        priorities = {CasePriority(p) for p in args.priority}
        selected = [c for c in all_cases if c.priority in priorities]
    elif args.category:
        selected = [c for c in all_cases if c.category in args.category]
    else:
        selected = all_cases

    if not selected:
        print("无匹配用例")
        return 1

    print(f"将执行 {len(selected)} 个用例")
    results = orchestrator.execute_cases(selected, stop_on_fail=args.stop_on_fail)

    # 输出汇总
    passed = sum(1 for r in results if r.status == "passed")
    failed = sum(1 for r in results if r.status == "failed")
    errored = sum(1 for r in results if r.status == "error")
    print("\n========== 执行汇总 ==========")
    print(f"总计: {len(results)}")
    print(f"通过: {passed}")
    print(f"失败: {failed}")
    print(f"错误: {errored}")
    for r in results:
        print(f"  [{r.status.upper():5}] {r.case.case_id}: {r.case.title}")

    return 0 if (failed == 0 and errored == 0) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prismloop",
        description="Prismloop - Agent 驱动的云手机 APP 自动测试与 harness 闭环系统",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    parser.add_argument("--env", default="config/env.local.json", help="环境配置文件")
    parser.add_argument("--cases-dir", default="cases", help="用例目录")
    parser.add_argument("--reports-dir", default="reports", help="报告目录")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list 命令
    list_parser = subparsers.add_parser("list", help="列出用例")
    list_parser.set_defaults(func=cmd_list_cases)

    # run 命令
    run_parser = subparsers.add_parser("run", help="执行用例")
    run_parser.add_argument("--case", nargs="*", help="指定用例 ID")
    run_parser.add_argument(
        "--priority", nargs="*",
        choices=["P0", "P1", "P2", "P3"],
        help="按优先级筛选",
    )
    run_parser.add_argument("--category", nargs="*", help="按分类筛选")
    run_parser.add_argument("--stop-on-fail", action="store_true", help="首次失败后停止")
    # 便捷别名
    run_parser.add_argument("--task", help="任务别名(smoke/functional/pressure)")
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    # 处理 task 别名
    if getattr(args, "task", None) and not getattr(args, "priority", None):
        if args.task == "smoke":
            args.priority = ["P0"]
        elif args.task == "functional":
            args.priority = ["P0", "P1"]
        elif args.task == "pressure":
            args.category = ["pressure"]

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
