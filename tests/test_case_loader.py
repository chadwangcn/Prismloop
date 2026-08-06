"""Case Loader 单元测试"""

from __future__ import annotations

import os
import tempfile
from textwrap import dedent

import pytest

from src.case_loader import (
    CaseLoader,
    CasePriority,
    CaseType,
    StepMethod,
)


# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
def sample_case_yaml() -> str:
    return dedent("""
    case_id: GAPP-DUAL-001
    title: 家长端绑定 K1 设备,验证双端绑定状态一致
    priority: P0
    type: dual
    category: binding
    tags: [binding, smoke, dual-end]
    topology:
      guardian_pod: pod-guardian-01
      k1_pod: pod-k1-01
    preconditions:
      - APP 已安装
      - K1 已激活
    steps:
      - pod: pod-k1-01
        method: mua
        action: 运行 K1 APP,完成设备激活流程
        assert:
          - APP 显示激活成功
        screenshot: true
      - pod: pod-guardian-01
        method: mua
        action: 扫描 K1 二维码完成绑定
        assert:
          - type: visual
            description: Guardian APP 显示绑定成功
        screenshot: true
      - pod: pod-k1-01
        method: poll_ui
        action: 等待 K1 显示绑定状态
        check:
          type: visual
          description: 截图检测已绑定提示
        timeout_sec: 30
        interval_sec: 2
    dual_assertions:
      - type: dual_screenshot
        description: 双端截图绑定状态
      - type: state_consistency
        fields: [device_sn, family_id]
    expected:
      binding_success: true
    """)


@pytest.fixture
def case_dir(sample_case_yaml, tmp_path):
    case_dir = tmp_path / "cases"
    functional = case_dir / "functional" / "P0-smoke"
    functional.mkdir(parents=True)
    (functional / "GAPP-DUAL-001.yaml").write_text(sample_case_yaml, encoding="utf-8")
    return case_dir


# ============================================================================
# 加载与解析
# ============================================================================


def test_load_file(sample_case_yaml, tmp_path):
    path = tmp_path / "case.yaml"
    path.write_text(sample_case_yaml, encoding="utf-8")

    loader = CaseLoader(str(tmp_path))
    case = loader.load_file(str(path))

    assert case.case_id == "GAPP-DUAL-001"
    assert case.priority == CasePriority.P0
    assert case.type == CaseType.DUAL
    assert case.category == "binding"
    assert "binding" in case.tags
    assert len(case.steps) == 3
    assert len(case.dual_assertions) == 2


def test_load_all(case_dir):
    loader = CaseLoader(str(case_dir))
    cases = loader.load_all()
    assert len(cases) == 1
    assert cases[0].case_id == "GAPP-DUAL-001"


def test_load_dir(case_dir):
    loader = CaseLoader(str(case_dir))
    cases = loader.load_dir("functional/P0-smoke")
    assert len(cases) == 1


def test_step_methods_parsed(case_dir):
    loader = CaseLoader(str(case_dir))
    cases = loader.load_all()
    case = cases[0]

    assert case.steps[0].method == StepMethod.MUA
    assert case.steps[1].method == StepMethod.MUA
    assert case.steps[2].method == StepMethod.POLL_UI


def test_step_assertion_parsing(case_dir):
    loader = CaseLoader(str(case_dir))
    case = loader.load_all()[0]

    # 简化文本断言
    step1 = case.steps[0]
    assert len(step1.assert_list) == 1
    assert step1.assert_list[0].description == "APP 显示激活成功"

    # 带 type 的断言
    step2 = case.steps[1]
    assert step2.assert_list[0].type == "visual"
    assert step2.assert_list[0].description == "Guardian APP 显示绑定成功"


def test_topology_resolution(case_dir):
    loader = CaseLoader(str(case_dir))
    case = loader.load_all()[0]

    assert case.topology["guardian_pod"] == "pod-guardian-01"
    assert case.topology["k1_pod"] == "pod-k1-01"

    # 别名解析
    assert case.resolve_pod("guardian_pod") == "pod-guardian-01"
    assert case.resolve_pod("k1_pod") == "pod-k1-01"
    # 真实 ID 直接返回
    assert case.resolve_pod("pod-guardian-01") == "pod-guardian-01"


def test_dual_assertions_parsed(case_dir):
    loader = CaseLoader(str(case_dir))
    case = loader.load_all()[0]

    assert len(case.dual_assertions) == 2
    assert case.dual_assertions[0].type == "dual_screenshot"
    assert case.dual_assertions[1].type == "state_consistency"
    assert "device_sn" in case.dual_assertions[1].fields


# ============================================================================
# 错误处理
# ============================================================================


def test_missing_case_id_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("title: no id\nsteps: []\n", encoding="utf-8")
    loader = CaseLoader(str(tmp_path))
    with pytest.raises(Exception):
        loader.load_file(str(path))


def test_missing_steps_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "case_id: C1\ntitle: t\npriority: P0\ntype: single\ncategory: c\n",
        encoding="utf-8",
    )
    loader = CaseLoader(str(tmp_path))
    with pytest.raises(Exception):
        loader.load_file(str(path))


def test_invalid_priority_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "case_id: C1\ntitle: t\npriority: P9\ntype: single\ncategory: c\nsteps:\n  - pod: x\n    method: mua\n    action: y\n",
        encoding="utf-8",
    )
    loader = CaseLoader(str(tmp_path))
    with pytest.raises(Exception):
        loader.load_file(str(path))


def test_invalid_method_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "case_id: C1\ntitle: t\npriority: P0\ntype: single\ncategory: c\nsteps:\n  - pod: x\n    method: unknown\n    action: y\n",
        encoding="utf-8",
    )
    loader = CaseLoader(str(tmp_path))
    with pytest.raises(Exception):
        loader.load_file(str(path))


# ============================================================================
# 过滤
# ============================================================================


def test_filter_by_priority(case_dir):
    loader = CaseLoader(str(case_dir))
    cases = loader.load_all()
    p0 = CaseLoader.filter_by_priority(cases, CasePriority.P0)
    p1 = CaseLoader.filter_by_priority(cases, CasePriority.P1)
    assert len(p0) == 1
    assert len(p1) == 0


def test_filter_by_type(case_dir):
    loader = CaseLoader(str(case_dir))
    cases = loader.load_all()
    dual = CaseLoader.filter_by_type(cases, CaseType.DUAL)
    single = CaseLoader.filter_by_type(cases, CaseType.SINGLE)
    assert len(dual) == 1
    assert len(single) == 0


def test_filter_by_tag(case_dir):
    loader = CaseLoader(str(case_dir))
    cases = loader.load_all()
    smoke = CaseLoader.filter_by_tag(cases, "smoke")
    assert len(smoke) == 1


# ============================================================================
# 空目录与不存在
# ============================================================================


def test_load_all_nonexistent_dir(tmp_path):
    loader = CaseLoader(str(tmp_path / "nonexistent"))
    cases = loader.load_all()
    assert cases == []
