"""断言引擎单元测试"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.assertor import Assertor, summarize, AssertionSummary
from src.case_loader import Assertion


# ============================================================================
# text 断言
# ============================================================================


def test_text_assertion_passes():
    assertor = Assertor()
    assertion = Assertion(type="text", description="显示绑定成功")
    result = assertor.evaluate(assertion, {"content": "Guardian APP 显示绑定成功"})
    assert result.passed is True


def test_text_assertion_fails():
    assertor = Assertor()
    assertion = Assertion(type="text", description="显示绑定成功")
    result = assertor.evaluate(assertion, {"content": "页面空白"})
    assert result.passed is False
    assert "显示绑定成功" in result.message


def test_text_assertion_quoted():
    assertor = Assertor()
    assertion = Assertion(type="text", description='验证 "登录成功" 文本')
    result = assertor.evaluate(assertion, {"content": "登录成功,欢迎"})
    assert result.passed is True


# ============================================================================
# json_path 断言
# ============================================================================


def test_json_path_simple():
    assertor = Assertor()
    assertion = Assertion(type="json_path", path="$.binding_success", expected=True)
    context = {"struct_output": {"binding_success": True, "device_sn": "K1-001"}}
    result = assertor.evaluate(assertion, context)
    assert result.passed is True
    assert result.actual is True


def test_json_path_nested():
    assertor = Assertor()
    assertion = Assertion(type="json_path", path="$.test_results[0].passed", expected=True)
    context = {
        "struct_output": {
            "test_results": [{"passed": True, "step": "激活"}]
        }
    }
    result = assertor.evaluate(assertion, context)
    assert result.passed is True


def test_json_path_mismatch():
    assertor = Assertor()
    assertion = Assertion(type="json_path", path="$.passed", expected=True)
    context = {"struct_output": {"passed": False}}
    result = assertor.evaluate(assertion, context)
    assert result.passed is False


def test_json_path_missing_key():
    assertor = Assertor()
    assertion = Assertion(type="json_path", path="$.missing", expected="x")
    result = assertor.evaluate(assertion, {"struct_output": {}})
    assert result.passed is False
    assert result.error is not None


# ============================================================================
# state 断言
# ============================================================================


def test_state_assertion_with_adb():
    adb = MagicMock()
    adb.shell.return_value = " mCurrentFocus=com.lumi.guardian/.VoiceAssistantActivity"
    assertor = Assertor(adb_client=adb)

    assertion = Assertion(
        type="state",
        check="adb shell dumpsys activity grep mCurrentFocus",
        expected="com.lumi.guardian/.VoiceAssistantActivity",
    )
    result = assertor.evaluate(assertion, {"pod_id": "pod-1"})
    assert result.passed is True
    adb.shell.assert_called_once_with("pod-1", "dumpsys activity grep mCurrentFocus")


def test_state_assertion_no_adb():
    assertor = Assertor(adb_client=None)
    assertion = Assertion(type="state", check="dumpsys", expected="x")
    result = assertor.evaluate(assertion, {"pod_id": "pod-1"})
    assert result.passed is False


def test_state_assertion_strips_adb_prefix():
    adb = MagicMock()
    adb.shell.return_value = "mCurrentFocus=LoginActivity"
    assertor = Assertor(adb_client=adb)
    assertion = Assertion(
        type="state",
        check="adb shell dumpsys activity mCurrentFocus",
        expected="LoginActivity",
    )
    assertor.evaluate(assertion, {"pod_id": "pod-1"})
    adb.shell.assert_called_once_with("pod-1", "dumpsys activity mCurrentFocus")


# ============================================================================
# visual 断言
# ============================================================================


def test_visual_uses_struct_output_passed():
    assertor = Assertor()
    assertion = Assertion(type="visual", description="登录按钮可见")
    result = assertor.evaluate(assertion, {
        "struct_output": {"passed": True},
        "screenshots": ["/tmp/shot.png"],
    })
    assert result.passed is True
    assert "/tmp/shot.png" in result.evidence_paths


def test_visual_no_evidence_fails():
    assertor = Assertor()
    assertion = Assertion(type="visual", description="登录按钮可见")
    result = assertor.evaluate(assertion, {"struct_output": {}, "screenshots": []})
    assert result.passed is False


def test_visual_screenshots_only():
    assertor = Assertor()
    assertion = Assertion(type="visual", description="页面已加载")
    result = assertor.evaluate(assertion, {"screenshots": ["/tmp/1.png", "/tmp/2.png"]})
    assert result.passed is True
    assert len(result.evidence_paths) == 2


# ============================================================================
# dual_screenshot 断言
# ============================================================================


def test_dual_screenshot_passes():
    assertor = Assertor()
    assertion = Assertion(type="dual_screenshot", description="双端截图绑定状态")
    result = assertor.evaluate(assertion, {
        "guardian_screenshots": ["/tmp/g.png"],
        "k1_screenshots": ["/tmp/k.png"],
    })
    assert result.passed is True


def test_dual_screenshot_fails_when_missing_k1():
    assertor = Assertor()
    assertion = Assertion(type="dual_screenshot", description="双端截图")
    result = assertor.evaluate(assertion, {
        "guardian_screenshots": ["/tmp/g.png"],
        "k1_screenshots": [],
    })
    assert result.passed is False


# ============================================================================
# state_consistency 断言
# ============================================================================


def test_state_consistency_passes():
    assertor = Assertor()
    assertion = Assertion(
        type="state_consistency",
        fields=["device_sn", "family_id"],
    )
    result = assertor.evaluate(assertion, {
        "guardian_state": {"device_sn": "K1-001", "family_id": "FAM-001"},
        "k1_state": {"device_sn": "K1-001", "family_id": "FAM-001"},
    })
    assert result.passed is True


def test_state_consistency_fails_on_mismatch():
    assertor = Assertor()
    assertion = Assertion(
        type="state_consistency",
        fields=["device_sn"],
    )
    result = assertor.evaluate(assertion, {
        "guardian_state": {"device_sn": "K1-001"},
        "k1_state": {"device_sn": "K1-002"},
    })
    assert result.passed is False


def test_state_consistency_no_fields():
    assertor = Assertor()
    assertion = Assertion(type="state_consistency", fields=[])
    result = assertor.evaluate(assertion, {})
    assert result.passed is False


# ============================================================================
# 未知类型与异常处理
# ============================================================================


def test_unknown_assertion_type():
    assertor = Assertor()
    assertion = Assertion(type="unknown_type")
    result = assertor.evaluate(assertion, {})
    assert result.passed is False
    assert "未知断言类型" in result.message


# ============================================================================
# summarize
# ============================================================================


def test_summarize_all_passed():
    results = [
        _make_result(True),
        _make_result(True),
    ]
    summary = summarize(results)
    assert summary.total == 2
    assert summary.passed == 2
    assert summary.failed == 0
    assert summary.all_passed is True


def test_summarize_with_failures():
    results = [_make_result(True), _make_result(False)]
    summary = summarize(results)
    assert summary.failed == 1
    assert summary.all_passed is False


def test_summarize_empty():
    summary = summarize([])
    assert summary.total == 0
    assert summary.all_passed is False  # 没有断言不算通过


# ============================================================================
# 辅助
# ============================================================================


def _make_result(passed: bool):
    assertion = Assertion(type="text", description="test")
    from src.assertor import AssertionResult
    return AssertionResult(assertion=assertion, passed=passed)
