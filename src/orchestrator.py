"""双端协同测试编排器

依据架构文档:
- `architecture/00-总体架构设计.md`:系统拓扑、双端协同模型
- `architecture/04-测试用例矩阵规范.md`:步骤执行方式
- `architecture/07-双端协同测试设计.md`:编排器设计(第四章)

职责:
- 解析用例 topology + steps
- 借还 Pod 配对(并发安全)
- 按 method 调度 MUA / SDK / ADB / poll_ui
- 跨端轮询等待(纯用户视角)
- 协同断言(dual_screenshot / state_consistency)
- 证据聚合
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .adb_client import ADBClient
from .assertor import AssertionResult, Assertor
from .case_loader import CasePriority, CaseType, Step, StepMethod, TestCase
from .mua_client import MUAClient
from .pod_pool import Pod, PodPair, PodPool, PodRole
from .reporter import CaseRunResult, Reporter, StepResult, generate_run_id, now_iso
from .sdk_client import SDKClient

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class OrchestratorError(Exception):
    """编排器异常"""


# ============================================================================
# Orchestrator
# ============================================================================


class Orchestrator:
    """双端协同测试编排器

    架构文档 07-第四章:编排器核心类
    """

    def __init__(
        self,
        mua_client: MUAClient,
        sdk_client: SDKClient,
        adb_client: ADBClient,
        pod_pool: PodPool,
        assertor: Optional[Assertor] = None,
        reporter: Optional[Reporter] = None,
        tos_config: Optional[Dict[str, str]] = None,
        screenshots_dir: str = "reports/screenshots",
    ):
        self.mua = mua_client
        self.sdk = sdk_client
        self.adb = adb_client
        self.pods = pod_pool
        self.assertor = assertor or Assertor(adb_client=adb_client, mua_client=mua_client)
        self.reporter = reporter or Reporter()
        self.tos_config = tos_config or {}
        self.screenshots_dir = screenshots_dir

    # ----------------------------------------------------------------------
    # 公共入口
    # ----------------------------------------------------------------------

    def execute_case(
        self,
        case: TestCase,
        run_id: Optional[str] = None,
        stop_on_fail: bool = False,
    ) -> CaseRunResult:
        """执行单个用例

        架构文档 07-第 4.2 节 execute_case。
        """
        run_id = run_id or generate_run_id(f"run-{case.case_id}")
        logger.info("开始执行用例: %s (run_id=%s)", case.case_id, run_id)

        result = CaseRunResult(
            run_id=run_id,
            case=case,
            start_time=now_iso(),
        )

        # 1. 借用 Pod
        pair: Optional[PodPair] = None
        single_pod: Optional[Pod] = None
        try:
            if case.type == CaseType.DUAL:
                pair = self.pods.acquire_pair(purpose=case.case_id)
            else:
                role = PodRole.GUARDIAN if "guardian" in case.topology else PodRole.K1
                single_pod = self.pods.acquire_pod(role, purpose=case.case_id)
        except Exception as e:  # noqa: BLE001
            result.status = "error"
            result.error = f"借用 Pod 失败: {e}"
            result.end_time = now_iso()
            logger.exception("借用 Pod 失败")
            return result

        try:
            # 2. 解析 topology 中的真实 Pod ID
            pod_map = self._resolve_topology(case, pair, single_pod)

            # 3. 注入音视频资源(架构文档 04 第二章 inject_assets)
            self._inject_assets(case, pod_map)

            # 4. 执行步骤
            for idx, step in enumerate(case.steps, start=1):
                step_result = self._execute_step(
                    step, idx, pod_map, run_id
                )
                result.steps.append(step_result)

                if step_result.status == "failed" and stop_on_fail:
                    logger.warning("步骤 %d 失败,停止执行(stop_on_fail)", idx)
                    break

            # 5. 执行协同断言(若用例是双端)
            if case.type == CaseType.DUAL:
                result.dual_assertions = self._execute_dual_assertions(
                    case, pod_map, run_id
                )

            # 6. 汇总状态
            result.status = self._compute_status(result)

        except Exception as e:  # noqa: BLE001
            result.status = "error"
            result.error = str(e)
            logger.exception("用例执行异常")
        finally:
            # 7. 归还 Pod
            try:
                if pair:
                    self.pods.release_pair(pair.pair_id)
                elif single_pod:
                    self.pods.release_pod(single_pod.pod_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("归还 Pod 失败: %s", e)

            result.end_time = now_iso()
            result.duration_sec = _parse_duration(result.start_time, result.end_time)

        logger.info(
            "用例完成: %s (status=%s, %d 步, 耗时 %.1fs)",
            case.case_id, result.status, len(result.steps), result.duration_sec,
        )
        return result

    def execute_cases(
        self,
        cases: List[TestCase],
        stop_on_fail: bool = False,
        generate_report: bool = True,
    ) -> List[CaseRunResult]:
        """批量执行用例"""
        results: List[CaseRunResult] = []
        for case in cases:
            result = self.execute_case(case, stop_on_fail=stop_on_fail)
            results.append(result)
            if generate_report:
                self.reporter.generate(result, include_html=True)
        return results

    # ----------------------------------------------------------------------
    # 内部实现
    # ----------------------------------------------------------------------

    def _resolve_topology(
        self,
        case: TestCase,
        pair: Optional[PodPair],
        single_pod: Optional[Pod],
    ) -> Dict[str, str]:
        """解析 topology 别名为真实 Pod ID

        若用例 topology 中写了别名(如 pod-guardian-01),
        而我们实际借到的 Pod ID 不同,需要建立映射。

        架构文档 04 第二章:
            topology:
              guardian_pod: pod-guardian-01
              k1_pod: pod-k1-01

        本实现优先用 topology 中的字面值,若不存在则用借到的 Pod。
        """
        pod_map: Dict[str, str] = {}

        if case.type == CaseType.DUAL and pair:
            # 配对场景:用 topology 中的 key 指向借到的 Pod
            for alias in ("guardian_pod", "k1_pod"):
                if alias in case.topology:
                    # 若 topology 写的是真实 Pod ID,直接用
                    pod_map[alias] = case.topology[alias]
                    pod_map[case.topology[alias]] = case.topology[alias]
            # 同时记录实际借到的 Pod
            pod_map["guardian_pod_id"] = pair.guardian_pod_id
            pod_map["k1_pod_id"] = pair.k1_pod_id
            pod_map[pair.guardian_pod_id] = pair.guardian_pod_id
            pod_map[pair.k1_pod_id] = pair.k1_pod_id
        elif single_pod:
            pod_map["pod_id"] = single_pod.pod_id
            for alias, val in case.topology.items():
                pod_map[alias] = single_pod.pod_id
                pod_map[val] = single_pod.pod_id

        return pod_map

    def _inject_assets(self, case: TestCase, pod_map: Dict[str, str]) -> None:
        """注入音视频资源

        架构文档 02 第二章 + 04 第二章 inject_assets
        """
        for asset in case.inject_assets:
            pod_ref = pod_map.get(asset.pod) or asset.pod
            file_path = asset.file
            if not os.path.exists(file_path):
                logger.warning("注入资源文件不存在: %s", file_path)
                continue

            try:
                if asset.type == "video":
                    logger.info("注入视频到 Pod %s: %s", pod_ref, file_path)
                    self.sdk.push_and_inject_video(pod_ref, file_path)
                elif asset.type == "audio":
                    logger.info("注入音频到 Pod %s: %s", pod_ref, file_path)
                    self.sdk.push_and_inject_audio(pod_ref, file_path)
                else:
                    logger.warning("未知注入资源类型: %s", asset.type)
            except Exception as e:  # noqa: BLE001
                logger.exception("注入资源失败: %s", e)

    def _execute_step(
        self,
        step: Step,
        step_index: int,
        pod_map: Dict[str, str],
        run_id: str,
    ) -> StepResult:
        """执行单个步骤

        架构文档 04 第三章 + 07 第 4.2 节 _execute_step。
        """
        pod_id = pod_map.get(step.pod) or step.pod
        pod_role = self._get_pod_role(pod_id, pod_map)

        result = StepResult(
            step_index=step_index,
            pod_id=pod_id,
            pod_role=pod_role,
            method=step.method.value,
            action=step.action,
            start_time=now_iso(),
        )

        screenshot_dir = os.path.join(
            self.screenshots_dir, run_id, pod_role
        )
        os.makedirs(screenshot_dir, exist_ok=True)

        try:
            if step.method == StepMethod.MUA:
                self._execute_mua_step(step, pod_id, result, run_id, screenshot_dir)
            elif step.method == StepMethod.SDK:
                self._execute_sdk_step(step, pod_id, result, screenshot_dir)
            elif step.method == StepMethod.ADB:
                self._execute_adb_step(step, pod_id, result, screenshot_dir)
            elif step.method == StepMethod.POLL_UI:
                self._execute_poll_ui_step(step, pod_id, result, screenshot_dir)

            # 评估断言
            context = self._build_assertion_context(step, result, pod_id, pod_role)
            assertion_results = self.assertor.evaluate_all(step.assert_list, context)
            result.assertions = assertion_results

            # 计算步骤状态
            if result.error:
                result.status = "error"
            elif all(a.passed for a in assertion_results) if assertion_results else True:
                result.status = "passed"
            else:
                result.status = "failed"

        except Exception as e:  # noqa: BLE001
            result.status = "error"
            result.error = str(e)
            logger.exception("步骤 %d 执行异常", step_index)

        result.end_time = now_iso()
        result.duration_sec = _parse_duration(result.start_time, result.end_time)
        return result

    def _execute_mua_step(
        self,
        step: Step,
        pod_id: str,
        result: StepResult,
        run_id: str,
        screenshot_dir: str,
    ) -> None:
        """执行 MUA 步骤"""
        logger.info("MUA 步骤: %s (pod=%s)", step.action[:80], pod_id)
        run_name = f"{run_id}-step{result.step_index}-{pod_id}"

        mua_result = self.mua.execute_test_task(
            pod_id=pod_id,
            user_prompt=step.action,
            run_name=run_name,
            system_prompt=step.system_prompt,
            output_schema=step.output_schema,
            tos_config=self.tos_config,
            max_step=step.max_step,
            timeout=step.timeout_sec,
        )

        result.mua_run_id = mua_result.get("run_id")
        result.mua_usage = mua_result.get("usage", {})
        result.raw_response = mua_result

        # 持久化截图(MUA 返回的 ScreenShots 是 URL,此处仅记录 URL)
        screenshots = mua_result.get("screenshots", {})
        if isinstance(screenshots, dict):
            for ss_id, ss_info in screenshots.items():
                ss_url = ss_info.get("screenshot") or ss_info.get("original_screenshot")
                if ss_url:
                    result.screenshots.append(ss_url)

        # MUA 任务失败则记录错误
        if not mua_result.get("is_success"):
            result.error = mua_result.get("content") or "MUA 任务失败"

    def _execute_sdk_step(
        self,
        step: Step,
        pod_id: str,
        result: StepResult,
        screenshot_dir: str,
    ) -> None:
        """执行 SDK 步骤"""
        call = step.sdk_call or {}
        interface = call.get("interface")
        if not interface:
            raise OrchestratorError("SDK 步骤缺少 sdk_call.interface")

        logger.info("SDK 步骤: %s.%s (pod=%s)", interface, call.get("action", ""), pod_id)

        if interface == "sendKeyCode":
            keycode = int(call.get("keycode", 0))
            action = call.get("action", "down_up")
            duration = int(call.get("duration_ms", 100))
            self.sdk.send_key_code(pod_id, keycode, action, duration)
        elif interface == "sendMouseKey":
            x = int(call.get("x", 0))
            y = int(call.get("y", 0))
            action = call.get("action", "down_up")
            self.sdk.send_mouse_key(pod_id, x, y, action)
        elif interface == "sendMulitTouch":
            events = call.get("events", [])
            self.sdk.send_multi_touch(pod_id, events)
        elif interface == "sendImeComposition":
            text = call.get("text", "")
            self.sdk.send_ime_composition(pod_id, text)
        elif interface == "sendEditTextInput":
            text = call.get("text", "")
            self.sdk.send_edit_text_input(pod_id, text)
        elif interface == "screenShot":
            pass  # 由下方统一截图逻辑处理
        else:
            raise OrchestratorError(f"未知 SDK 接口: {interface}")

        # 截图(screenshot 或 screenshot_after_ms)
        if step.screenshot or step.screenshot_after_ms > 0:
            if step.screenshot_after_ms > 0:
                time.sleep(step.screenshot_after_ms / 1000.0)
            shot_path = os.path.join(
                screenshot_dir,
                f"step-{result.step_index:03d}-{int(time.time())}.png",
            )
            try:
                self.sdk.screenshot(pod_id, save_path=shot_path)
                result.screenshots.append(shot_path)
            except Exception as e:  # noqa: BLE001
                logger.warning("截图失败: %s", e)

    def _execute_adb_step(
        self,
        step: Step,
        pod_id: str,
        result: StepResult,
        screenshot_dir: str,
    ) -> None:
        """执行 ADB 步骤"""
        command = step.adb_command
        if not command:
            raise OrchestratorError("ADB 步骤缺少 adb_command")

        logger.info("ADB 步骤: %s (pod=%s)", command, pod_id)
        output = self.adb.shell(pod_id, command)
        result.raw_response = {"output": output[:2000]}

        if step.screenshot or step.screenshot_after_ms > 0:
            if step.screenshot_after_ms > 0:
                time.sleep(step.screenshot_after_ms / 1000.0)
            shot_path = os.path.join(
                screenshot_dir,
                f"step-{result.step_index:03d}-{int(time.time())}.png",
            )
            try:
                self.adb.screenshot(pod_id, shot_path)
                result.screenshots.append(shot_path)
            except Exception as e:  # noqa: BLE001
                logger.warning("截图失败: %s", e)

    def _execute_poll_ui_step(
        self,
        step: Step,
        pod_id: str,
        result: StepResult,
        screenshot_dir: str,
    ) -> None:
        """执行 poll_ui 步骤(轮询等待 UI 状态)

        架构文档 07-第 4.2 节 _execute_poll_step
        """
        check = step.check or {}
        description = check.get("description", step.action)

        logger.info(
            "POLL_UI 步骤: %s (pod=%s, timeout=%ds)",
            description, pod_id, step.timeout_sec,
        )

        start = time.time()
        matched = False
        last_screenshot = ""
        round_idx = 0

        while time.time() - start < step.timeout_sec:
            round_idx += 1
            shot_path = os.path.join(
                screenshot_dir,
                f"step-{result.step_index:03d}-poll-{round_idx:03d}.png",
            )
            try:
                self.sdk.screenshot(pod_id, save_path=shot_path)
                last_screenshot = shot_path
            except Exception as e:  # noqa: BLE001
                logger.warning("轮询截图失败(round %d): %s", round_idx, e)
                time.sleep(step.interval_sec)
                continue

            # MUA 视觉检测
            if self._mua_visual_check(pod_id, description, shot_path):
                matched = True
                result.screenshots.append(shot_path)
                break

            time.sleep(step.interval_sec)

        if not matched and last_screenshot:
            result.screenshots.append(last_screenshot)

        if not matched:
            result.error = f"轮询超时({step.timeout_sec}s): {description}"

    def _mua_visual_check(
        self,
        pod_id: str,
        expected_description: str,
        screenshot_path: str,
    ) -> bool:
        """MUA 视觉检测

        架构文档 07-第 3.2 节 mua_visual_check。
        实际场景应使用专用 vision-check Pod 分析截图,
        本实现简化为直接在目标 Pod 上跑一个轻量 MUA 任务判断。
        """
        try:
            prompt = (
                f"请观察当前屏幕,判断是否满足以下描述。"
                f"只回答 JSON: {{\"matched\": true/false}}。"
                f"描述:{expected_description}"
            )
            result = self.mua.run_task_one_step(
                run_name=f"vision-check-{int(time.time())}",
                pod_id=pod_id,
                user_prompt=prompt,
                max_step=1,
                timeout=15,
                use_base64_screenshot=True,
                is_screen_record=False,
            )
            content = str(result).lower()
            return "true" in content
        except Exception as e:  # noqa: BLE001
            logger.warning("MUA 视觉检测异常: %s", e)
            return False

    def _execute_dual_assertions(
        self,
        case: TestCase,
        pod_map: Dict[str, str],
        run_id: str,
    ) -> List[AssertionResult]:
        """执行协同断言

        架构文档 07 第六章:双端证据采集
        """
        results: List[AssertionResult] = []
        guardian_pod = pod_map.get("guardian_pod_id", "")
        k1_pod = pod_map.get("k1_pod_id", "")

        # 双端截图
        guardian_shots = [
            s for r in self._last_results_guardian for s in r.screenshots
        ] if hasattr(self, "_last_results_guardian") else []
        k1_shots = [
            s for r in self._last_results_k1 for s in r.screenshots
        ] if hasattr(self, "_last_results_k1") else []

        # 收集本用例的双端截图
        for da in case.dual_assertions:
            context = {
                "pod_id": guardian_pod,
                "guardian_screenshots": guardian_shots,
                "k1_screenshots": k1_shots,
            }
            results.append(self.assertor.evaluate(da, context))

        return results

    def _build_assertion_context(
        self,
        step: Step,
        result: StepResult,
        pod_id: str,
        pod_role: str,
    ) -> Dict[str, Any]:
        """构造断言上下文"""
        raw = result.raw_response or {}
        return {
            "pod_id": pod_id,
            "pod_role": pod_role,
            "content": raw.get("content", ""),
            "struct_output": raw.get("struct_output"),
            "screenshots": result.screenshots,
            "mua_result": raw,
        }

    def _get_pod_role(self, pod_id: str, pod_map: Dict[str, str]) -> str:
        """根据 Pod ID 推断角色"""
        if pod_id == pod_map.get("guardian_pod_id"):
            return "guardian"
        if pod_id == pod_map.get("k1_pod_id"):
            return "k1"
        # 从 PodPool 查询
        try:
            pod = self.pods.get_pod(pod_id)
            return pod.role.value
        except Exception:  # noqa: BLE001
            return "unknown"

    def _compute_status(self, result: CaseRunResult) -> str:
        """计算用例整体状态"""
        if result.error:
            return "error"
        if any(s.status == "error" for s in result.steps):
            return "error"
        if any(s.status == "failed" for s in result.steps):
            return "failed"
        if result.dual_assertions and any(not a.passed for a in result.dual_assertions):
            return "failed"
        return "passed"


# ============================================================================
# 工具函数
# ============================================================================


def _parse_duration(start_iso: Optional[str], end_iso: Optional[str]) -> float:
    """计算 ISO 时间差(秒)"""
    if not start_iso or not end_iso:
        return 0.0
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(start_iso, fmt).replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_iso, fmt).replace(tzinfo=timezone.utc)
        return (end - start).total_seconds()
    except (ValueError, TypeError):
        return 0.0
