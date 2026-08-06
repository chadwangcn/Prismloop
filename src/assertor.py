"""断言引擎

依据架构文档 `architecture/04-测试用例矩阵规范.md` 第四章:
- 三层断言:MUA 内置 / OutputSchema 结构 / 截图视觉
- 断言类型:visual / state / text / json_path / dual_screenshot / state_consistency

依据架构文档 `architecture/06-证据链与报告规范.md`:
- 断言结果附带证据(截图路径、实际值)
- 失败时记录详细上下文
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .case_loader import Assertion

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class AssertionError(Exception):  # noqa: A001 - 与架构文档语义一致
    """断言失败"""


# ============================================================================
# 断言结果
# ============================================================================


@dataclass
class AssertionResult:
    """单个断言的执行结果"""

    assertion: Assertion
    passed: bool
    actual: Any = None
    expected: Any = None
    message: str = ""
    evidence_paths: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.assertion.type,
            "description": self.assertion.description,
            "passed": self.passed,
            "actual": self.actual,
            "expected": self.expected,
            "message": self.message,
            "evidence_paths": self.evidence_paths,
            "error": self.error,
        }


# ============================================================================
# Assertor
# ============================================================================


class Assertor:
    """断言引擎

    支持的断言类型(架构文档 04-第 4.2 节):
    - visual:截图视觉检测(委托 MUA 视觉判断,本类只调度)
    - state:APP 状态检测(ADB shell dumpsys)
    - text:文本检测(在内容中查找文本)
    - json_path:JSONPath 断言
    - dual_screenshot:双端截图(只验证证据存在)
    - state_consistency:双端状态一致性
    """

    def __init__(
        self,
        adb_client: Optional[Any] = None,
        mua_client: Optional[Any] = None,
    ):
        self.adb_client = adb_client
        self.mua_client = mua_client

    # ----------------------------------------------------------------------
    # 执行单个断言
    # ----------------------------------------------------------------------

    def evaluate(
        self,
        assertion: Assertion,
        context: Dict[str, Any],
    ) -> AssertionResult:
        """评估一个断言

        Args:
            assertion:断言条件
            context:执行上下文,包含:
                - pod_id: 当前 Pod ID
                - content: MUA 返回的文本内容
                - struct_output: MUA 结构化输出
                - screenshots: 截图路径列表
                - guardian_state / k1_state: 双端状态(用于 consistency)
        """
        atype = assertion.type
        try:
            if atype == "visual":
                return self._eval_visual(assertion, context)
            if atype == "state":
                return self._eval_state(assertion, context)
            if atype == "text":
                return self._eval_text(assertion, context)
            if atype == "json_path":
                return self._eval_json_path(assertion, context)
            if atype == "dual_screenshot":
                return self._eval_dual_screenshot(assertion, context)
            if atype == "state_consistency":
                return self._eval_state_consistency(assertion, context)
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message=f"未知断言类型: {atype}",
            )
        except Exception as e:  # noqa: BLE001
            return AssertionResult(
                assertion=assertion,
                passed=False,
                error=str(e),
                message=f"断言执行异常: {e}",
            )

    def evaluate_all(
        self,
        assertions: List[Assertion],
        context: Dict[str, Any],
    ) -> List[AssertionResult]:
        """批量评估断言"""
        return [self.evaluate(a, context) for a in assertions]

    # ----------------------------------------------------------------------
    # 各类型断言实现
    # ----------------------------------------------------------------------

    def _eval_visual(
        self, assertion: Assertion, context: Dict[str, Any]
    ) -> AssertionResult:
        """视觉断言:依赖 MUA 视觉判断或截图证据

        架构文档 04-第 4.1 节:MUA 内置断言 + 截图视觉断言
        本实现简化为:若 context["visual_passed"] 已由 MUA 返回,直接使用;
        否则需要调用方提供截图证据路径。
        """
        # 若 MUA StructOutput 中已有 passed 字段,优先使用
        struct_output = context.get("struct_output") or {}
        if "passed" in struct_output:
            passed = bool(struct_output["passed"])
            return AssertionResult(
                assertion=assertion,
                passed=passed,
                actual=struct_output.get("passed"),
                expected=True,
                message="MUA 结构化输出 passed 字段",
                evidence_paths=context.get("screenshots", []),
            )

        # 否则仅验证截图证据存在
        screenshots = context.get("screenshots", [])
        if not screenshots:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message="无截图证据可供视觉断言",
            )

        return AssertionResult(
            assertion=assertion,
            passed=True,
            actual=f"{len(screenshots)} 张截图",
            evidence_paths=screenshots,
            message="已采集截图证据,详细视觉对比需人工或独立视觉 diff 模块",
        )

    def _eval_state(
        self, assertion: Assertion, context: Dict[str, Any]
    ) -> AssertionResult:
        """状态断言:执行 ADB shell 命令检查 APP 状态

        架构文档 04-第 4.2 节示例:
            check: "adb shell dumpsys activity grep mCurrentFocus"
            expected: "com.lumi.guardian/.VoiceAssistantActivity"
        """
        if not self.adb_client:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message="state 断言需要 ADB Client",
            )

        pod_id = context.get("pod_id")
        if not pod_id:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message="context 缺少 pod_id",
            )

        check_cmd = assertion.check or ""
        if not check_cmd:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message="state 断言缺少 check 命令",
            )

        # 去掉 "adb shell " 前缀(若存在)
        if check_cmd.startswith("adb shell "):
            check_cmd = check_cmd[len("adb shell "):]

        actual = self.adb_client.shell(pod_id, check_cmd)
        expected = assertion.expected

        if expected is None:
            return AssertionResult(
                assertion=assertion,
                passed=True,
                actual=actual,
                message="无 expected,只要命令执行成功即通过",
            )

        passed = str(expected) in actual
        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual=actual[:500],
            expected=expected,
            message="包含匹配" if passed else f"实际值不包含期望: {expected}",
        )

    def _eval_text(
        self, assertion: Assertion, context: Dict[str, Any]
    ) -> AssertionResult:
        """文本断言:在内容中查找指定文本"""
        content = context.get("content") or ""
        expected_text = assertion.description or assertion.expected

        if not expected_text:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message="text 断言缺少期望文本",
            )

        # 支持描述中带引号:验证 "显示绑定成功" 文本
        match = re.search(r'[\"\'](.+?)[\"\']', expected_text)
        target = match.group(1) if match else expected_text

        passed = target in content
        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual=content[:500],
            expected=target,
            message="内容包含期望文本" if passed else f"内容中未找到: {target}",
        )

    def _eval_json_path(
        self, assertion: Assertion, context: Dict[str, Any]
    ) -> AssertionResult:
        """JSONPath 断言:在 StructOutput 中按路径取值

        支持简化语法:$.field1.field2(只支持点分路径)
        """
        struct_output = context.get("struct_output") or {}
        path = assertion.path or "$"

        try:
            actual = self._resolve_json_path(struct_output, path)
        except (KeyError, IndexError) as e:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                error=str(e),
                message=f"JSONPath {path} 解析失败",
            )

        expected = assertion.expected
        passed = actual == expected

        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual=actual,
            expected=expected,
            message="值匹配" if passed else f"值不匹配: actual={actual}, expected={expected}",
        )

    def _eval_dual_screenshot(
        self, assertion: Assertion, context: Dict[str, Any]
    ) -> AssertionResult:
        """双端截图断言:验证双端截图证据均已采集"""
        guardian_shots = context.get("guardian_screenshots", [])
        k1_shots = context.get("k1_screenshots", [])
        all_shots = guardian_shots + k1_shots

        if not guardian_shots or not k1_shots:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual={
                    "guardian": len(guardian_shots),
                    "k1": len(k1_shots),
                },
                message="双端截图证据不完整",
            )

        return AssertionResult(
            assertion=assertion,
            passed=True,
            actual={
                "guardian": len(guardian_shots),
                "k1": len(k1_shots),
            },
            evidence_paths=all_shots,
            message="双端截图证据已采集",
        )

    def _eval_state_consistency(
        self, assertion: Assertion, context: Dict[str, Any]
    ) -> AssertionResult:
        """状态一致性断言:双端字段值一致

        架构文档 04-第 4.2 节:fields: [device_sn, family_id, child_id]
        要求 context 中提供 guardian_state / k1_state 两个 dict
        """
        fields = assertion.fields or []
        if not fields:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                message="state_consistency 断言缺少 fields",
            )

        guardian_state = context.get("guardian_state") or {}
        k1_state = context.get("k1_state") or {}

        mismatches: List[Dict[str, Any]] = []
        for f in fields:
            g_val = guardian_state.get(f)
            k_val = k1_state.get(f)
            if g_val != k_val:
                mismatches.append({
                    "field": f,
                    "guardian": g_val,
                    "k1": k_val,
                })

        return AssertionResult(
            assertion=assertion,
            passed=len(mismatches) == 0,
            actual={
                "guardian_state": {f: guardian_state.get(f) for f in fields},
                "k1_state": {f: k1_state.get(f) for f in fields},
            },
            expected={f: "双端一致" for f in fields},
            message=(
                "双端状态一致" if not mismatches
                else f"状态不一致: {mismatches}"
            ),
        )

    # ----------------------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------------------

    @staticmethod
    def _resolve_json_path(data: Any, path: str) -> Any:
        """解析简化 JSONPath($.a.b[0].c)

        支持:
        - $.field
        - $.field.subfield
        - $.field[0]
        """
        if not path.startswith("$"):
            return data

        # 去掉 $.
        expr = path[1:].lstrip(".")

        # 分词:按 . 和 [] 分割
        tokens = re.findall(r"\[(-?\d+)\]|\.?([^\.\[\]]+)", expr)
        current = data
        for idx_str, key in tokens:
            if idx_str:
                idx = int(idx_str)
                if not isinstance(current, list):
                    raise KeyError(f"无法索引 {idx}:当前值非列表")
                current = current[idx]
            elif key:
                if not isinstance(current, dict):
                    raise KeyError(f"无法取字段 {key}:当前值非 dict")
                current = current[key]
        return current


# ============================================================================
# 断言结果聚合
# ============================================================================


@dataclass
class AssertionSummary:
    """断言结果汇总"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    results: List[AssertionResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.total > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "all_passed": self.all_passed,
            "results": [r.to_dict() for r in self.results],
        }


def summarize(results: List[AssertionResult]) -> AssertionSummary:
    """汇总断言结果"""
    summary = AssertionSummary(
        total=len(results),
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed),
        results=results,
    )
    return summary
