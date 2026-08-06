"""Reporter 单元测试"""

from __future__ import annotations

import json
import os

import pytest

from src.case_loader import CasePriority, CaseType, TestCase
from src.reporter import (
    CaseRunResult,
    EvidenceSanitizer,
    Reporter,
    StepResult,
    generate_run_id,
    now_iso,
)
from src.assertor import AssertionResult
from src.case_loader import Assertion


# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
def sample_case() -> TestCase:
    return TestCase(
        case_id="GAPP-DUAL-001",
        title="家长端绑定 K1 设备",
        priority=CasePriority.P0,
        type=CaseType.DUAL,
        category="binding",
        topology={"guardian_pod": "pod-g-01", "k1_pod": "pod-k-01"},
    )


@pytest.fixture
def sample_result(sample_case) -> CaseRunResult:
    result = CaseRunResult(
        run_id="run-test-001",
        case=sample_case,
        status="passed",
        start_time="2026-08-06T14:00:00Z",
        end_time="2026-08-06T14:05:00Z",
        duration_sec=300,
    )
    result.steps.append(StepResult(
        step_index=1,
        pod_id="pod-k-01",
        pod_role="k1",
        method="mua",
        action="运行 K1 APP",
        status="passed",
        duration_sec=30,
        screenshots=["reports/run-test-001/screenshots/k1/step-001.png"],
        assertions=[
            AssertionResult(
                assertion=Assertion(type="text", description="APP 显示激活成功"),
                passed=True,
                actual="APP 显示激活成功",
            )
        ],
    ))
    result.steps.append(StepResult(
        step_index=2,
        pod_id="pod-g-01",
        pod_role="guardian",
        method="mua",
        action="扫描 K1 二维码",
        status="failed",
        duration_sec=45,
        assertions=[
            AssertionResult(
                assertion=Assertion(type="text", description="绑定成功"),
                passed=False,
                actual="页面显示错误",
                message="未找到文本",
            )
        ],
    ))
    result.status = "failed"
    return result


# ============================================================================
# 时间工具
# ============================================================================


def test_now_iso_format():
    ts = now_iso()
    assert ts.endswith("Z")
    assert "T" in ts


def test_generate_run_id_has_prefix():
    rid = generate_run_id("run")
    assert rid.startswith("run-")


# ============================================================================
# 脱敏器
# ============================================================================


class TestSanitizer:
    def test_sanitize_email(self):
        s = EvidenceSanitizer()
        result = s.sanitize_text("联系 test@lumi.com 获取支持")
        assert "test@lumi.com" not in result
        assert "t***@lumi.com" in result

    def test_sanitize_phone(self):
        s = EvidenceSanitizer()
        result = s.sanitize_text("电话 13812345678")
        assert "13812345678" not in result
        assert "138****8888" in result or "***" in result

    def test_sanitize_token(self):
        s = EvidenceSanitizer()
        text = "token: abc123def456"
        result = s.sanitize_text(text)
        assert "abc123def456" not in result

    def test_sanitize_device_sn(self):
        s = EvidenceSanitizer()
        result = s.sanitize_text("设备 SN: K1-AB12-XYZ999")
        assert "K1-AB12-XYZ999" not in result

    def test_sanitize_dict_recursive(self):
        s = EvidenceSanitizer()
        data = {
            "user": "test@lumi.com",
            "nested": {"phone": "13812345678"},
            "list": ["token: secret123"],
        }
        result = s.sanitize_dict(data)
        assert "test@lumi.com" not in str(result)
        assert "13812345678" not in str(result)
        assert "secret123" not in str(result)

    def test_sanitize_preserves_non_sensitive(self):
        s = EvidenceSanitizer()
        text = "这是一个普通文本,无敏感信息"
        assert s.sanitize_text(text) == text


# ============================================================================
# 报告生成
# ============================================================================


class TestReporter:
    def test_generate_creates_files(self, sample_result, tmp_path):
        reporter = Reporter(reports_dir=str(tmp_path), sanitize=False)
        paths = reporter.generate(sample_result, include_html=True)

        assert os.path.exists(paths["result_json"])
        assert os.path.exists(paths["manifest"])
        assert os.path.exists(paths["report_html"])

    def test_result_json_structure(self, sample_result, tmp_path):
        reporter = Reporter(reports_dir=str(tmp_path), sanitize=False)
        paths = reporter.generate(sample_result, include_html=False)

        with open(paths["result_json"], "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["run_id"] == "run-test-001"
        assert data["case_id"] == "GAPP-DUAL-001"
        assert data["status"] == "failed"
        assert len(data["steps"]) == 2
        assert data["steps"][0]["step"] == 1
        assert data["steps"][0]["assertions"][0]["passed"] is True

    def test_manifest_structure(self, sample_result, tmp_path):
        reporter = Reporter(reports_dir=str(tmp_path), sanitize=False)
        paths = reporter.generate(sample_result, include_html=False)

        with open(paths["manifest"], "r", encoding="utf-8") as f:
            manifest = json.load(f)

        assert manifest["run_id"] == "run-test-001"
        assert "evidence" in manifest
        assert "screenshots" in manifest["evidence"]

    def test_html_contains_case_info(self, sample_result, tmp_path):
        reporter = Reporter(reports_dir=str(tmp_path), sanitize=False)
        paths = reporter.generate(sample_result, include_html=True)

        with open(paths["report_html"], "r", encoding="utf-8") as f:
            html = f.read()

        assert "GAPP-DUAL-001" in html
        assert "家长端绑定 K1 设备" in html
        assert "FAILED" in html  # 用例失败
        assert "步骤 1" in html
        assert "步骤 2" in html

    def test_sanitizer_applied(self, sample_result, tmp_path):
        """开启脱敏后,结果中不应包含敏感信息"""
        # 修改实际值含邮箱
        sample_result.steps[0].assertions[0].actual = "联系 test@example.com"
        reporter = Reporter(reports_dir=str(tmp_path), sanitize=True)
        paths = reporter.generate(sample_result, include_html=False)

        with open(paths["result_json"], "r", encoding="utf-8") as f:
            content = f.read()

        assert "test@example.com" not in content

    def test_creates_screenshot_dirs(self, sample_result, tmp_path):
        reporter = Reporter(reports_dir=str(tmp_path), sanitize=False)
        reporter.generate(sample_result, include_html=False)

        run_dir = tmp_path / "run-test-001"
        assert (run_dir / "screenshots" / "guardian").exists()
        assert (run_dir / "screenshots" / "k1").exists()
        assert (run_dir / "recordings").exists()
        assert (run_dir / "logs").exists()


# ============================================================================
# CaseRunResult.to_dict
# ============================================================================


def test_case_result_to_dict(sample_result):
    data = sample_result.to_dict()
    assert data["case_id"] == "GAPP-DUAL-001"
    assert data["status"] == "failed"
    assert data["summary"]["total_steps"] == 2
    assert data["summary"]["passed_steps"] == 1
    assert data["summary"]["failed_steps"] == 1


def test_step_result_to_dict(sample_result):
    step = sample_result.steps[0]
    data = step.to_dict()
    assert data["step"] == 1
    assert data["pod"] == "pod-k-01"
    assert data["pod_role"] == "k1"
    assert data["method"] == "mua"
    assert len(data["assertions"]) == 1
