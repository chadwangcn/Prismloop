"""测试用例加载器

依据架构文档 `architecture/04-测试用例矩阵规范.md`:
- 用例格式:YAML(第二章)
- 用例分层:P0/P1/P2/P3(第一章)
- 用例组织:目录结构(第八章)
- 步骤方法:mua / sdk / adb / poll_ui(第三章)
- 断言类型:visual/state/text/json_path/dual_screenshot/state_consistency(第四章)

提供:
- TestCase / Step / Assertion 数据模型
- YAML 加载与校验
- 按目录/优先级/标签过滤
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class CaseLoadError(Exception):
    """用例加载异常"""


# ============================================================================
# 枚举
# ============================================================================


class CasePriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class CaseType(str, Enum):
    SINGLE = "single"
    DUAL = "dual"


class StepMethod(str, Enum):
    MUA = "mua"
    SDK = "sdk"
    ADB = "adb"
    POLL_UI = "poll_ui"


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class Assertion:
    """断言条件

    架构文档 04-第四章:三层断言
    """

    type: str = "visual"  # visual/state/text/json_path/dual_screenshot/state_consistency
    description: str = ""
    expected: Any = None
    path: Optional[str] = None  # json_path 专用
    expected_element: Optional[str] = None
    check: Optional[str] = None  # state 检查命令
    screenshot_after_ms: int = 0
    fields: List[str] = field(default_factory=list)  # state_consistency 字段

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Assertion":
        if isinstance(data, str):
            # 简化写法:assert: ["显示绑定成功"]
            return cls(description=data)
        return cls(
            type=data.get("type", "visual"),
            description=data.get("description", ""),
            expected=data.get("expected"),
            path=data.get("path"),
            expected_element=data.get("expected_element"),
            check=data.get("check"),
            screenshot_after_ms=data.get("screenshot_after_ms", 0),
            fields=data.get("fields", []),
        )


@dataclass
class InjectAsset:
    """音视频注入资源"""

    type: str  # video / audio
    file: str
    pod: str  # 引用 topology 的 key(guardian_pod / k1_pod)
    inject_method: str = "adb_broadcast"
    remote_path: Optional[str] = None


@dataclass
class Step:
    """测试步骤

    架构文档 04-第三章:步骤执行方式
    """

    pod: str  # Pod ID 或引用 topology 中的别名
    method: StepMethod
    action: str
    assert_list: List[Assertion] = field(default_factory=list)
    screenshot: bool = False
    screenshot_after_ms: int = 0
    timeout_sec: int = 120
    interval_sec: float = 2.0
    max_step: int = 100
    system_prompt: Optional[str] = None
    prereq: Optional[str] = None  # 前置步骤依赖
    sdk_call: Optional[Dict[str, Any]] = None  # SDK 调用参数
    adb_command: Optional[str] = None  # ADB 命令
    check: Optional[Dict[str, Any]] = None  # poll_ui 检查条件
    output_schema: Optional[Dict[str, Any]] = None  # MUA OutputSchema

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        method_raw = data.get("method", "mua")
        try:
            method = StepMethod(method_raw)
        except ValueError:
            raise CaseLoadError(f"未知 step method: {method_raw}")

        asserts_raw = data.get("assert") or data.get("assertions") or []
        if isinstance(asserts_raw, str):
            asserts_raw = [asserts_raw]

        return cls(
            pod=data.get("pod", ""),
            method=method,
            action=data.get("action", ""),
            assert_list=[Assertion.from_dict(a) for a in asserts_raw],
            screenshot=data.get("screenshot", False),
            screenshot_after_ms=data.get("screenshot_after_ms", 0),
            timeout_sec=data.get("timeout_sec", data.get("timeout", 120)),
            interval_sec=data.get("interval_sec", 2.0),
            max_step=data.get("max_step", 100),
            system_prompt=data.get("system_prompt"),
            prereq=data.get("prereq"),
            sdk_call=data.get("sdk_call"),
            adb_command=data.get("adb_command"),
            check=data.get("check"),
            output_schema=data.get("output_schema"),
        )


@dataclass
class TestCase:
    """测试用例

    架构文档 04-第二章:用例格式规范
    """

    case_id: str
    title: str
    priority: CasePriority
    type: CaseType
    category: str
    file_path: str = ""
    tags: List[str] = field(default_factory=list)
    topology: Dict[str, str] = field(default_factory=dict)
    preconditions: List[str] = field(default_factory=list)
    inject_assets: List[InjectAsset] = field(default_factory=list)
    steps: List[Step] = field(default_factory=list)
    dual_assertions: List[Assertion] = field(default_factory=list)
    output_schema: Optional[Dict[str, Any]] = None
    expected: Dict[str, Any] = field(default_factory=dict)
    teardown: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], file_path: str = "") -> "TestCase":
        case_id = data.get("case_id")
        if not case_id:
            raise CaseLoadError(f"用例缺少 case_id(文件: {file_path})")

        try:
            priority = CasePriority(data.get("priority", "P1"))
        except ValueError:
            raise CaseLoadError(f"未知 priority: {data.get('priority')} (case: {case_id})")

        try:
            ctype = CaseType(data.get("type", "single"))
        except ValueError:
            raise CaseLoadError(f"未知 type: {data.get('type')} (case: {case_id})")

        steps_raw = data.get("steps", [])
        if not steps_raw:
            raise CaseLoadError(f"用例 {case_id} 缺少 steps")

        return cls(
            case_id=case_id,
            title=data.get("title", ""),
            priority=priority,
            type=ctype,
            category=data.get("category", ""),
            file_path=file_path,
            tags=data.get("tags", []),
            topology=data.get("topology", {}),
            preconditions=data.get("preconditions", []),
            inject_assets=[
                InjectAsset(
                    type=a.get("type", ""),
                    file=a.get("file", ""),
                    pod=a.get("pod", ""),
                    inject_method=a.get("inject_method", "adb_broadcast"),
                    remote_path=a.get("remote_path"),
                )
                for a in data.get("inject_assets", [])
            ],
            steps=[Step.from_dict(s) for s in steps_raw],
            dual_assertions=[
                Assertion.from_dict(a) for a in data.get("dual_assertions", [])
            ],
            output_schema=data.get("output_schema"),
            expected=data.get("expected", {}),
            teardown=data.get("teardown", []),
            raw=data,
        )

    def resolve_pod(self, pod_ref: str, fallback_pair: Optional[Dict[str, str]] = None) -> str:
        """解析步骤中的 pod 引用

        步骤 pod 可以是:
        - 真实 Pod ID(如 pod-guardian-01)
        - topology 别名(如 guardian_pod / k1_pod)
        - pair 配对中的别名(注入 Pod 引用时)
        """
        if pod_ref in self.topology:
            return self.topology[pod_ref]
        if fallback_pair and pod_ref in fallback_pair:
            return fallback_pair[pod_ref]
        return pod_ref


# ============================================================================
# Case Loader
# ============================================================================


class CaseLoader:
    """测试用例加载器

    架构文档 04-第八章:用例组织目录
    """

    def __init__(self, cases_dir: str = "cases"):
        self.cases_dir = Path(cases_dir)

    def load_all(self) -> List[TestCase]:
        """加载目录下所有用例"""
        cases: List[TestCase] = []
        if not self.cases_dir.exists():
            logger.warning("用例目录不存在: %s", self.cases_dir)
            return cases

        for yaml_path in sorted(self.cases_dir.rglob("*.yaml")):
            try:
                case = self.load_file(str(yaml_path))
                cases.append(case)
            except CaseLoadError as e:
                logger.error("加载用例失败 %s: %s", yaml_path, e)
            except Exception as e:  # noqa: BLE001
                logger.exception("加载用例异常 %s: %s", yaml_path, e)
        logger.info("共加载 %d 个用例(目录: %s)", len(cases), self.cases_dir)
        return cases

    def load_file(self, file_path: str) -> TestCase:
        """加载单个 YAML 用例文件"""
        path = Path(file_path)
        if not path.exists():
            raise CaseLoadError(f"用例文件不存在: {file_path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise CaseLoadError(f"用例文件格式错误(应为 dict): {file_path}")
        return TestCase.from_dict(data, file_path=str(path))

    def load_dir(self, subdir: str) -> List[TestCase]:
        """加载指定子目录的用例(如 functional/P0-smoke)"""
        target = self.cases_dir / subdir
        cases: List[TestCase] = []
        if not target.exists():
            return cases
        for yaml_path in sorted(target.rglob("*.yaml")):
            try:
                cases.append(self.load_file(str(yaml_path)))
            except CaseLoadError as e:
                logger.error("加载用例失败 %s: %s", yaml_path, e)
        return cases

    # ----------------------------------------------------------------------
    # 过滤
    # ----------------------------------------------------------------------

    @staticmethod
    def filter_by_priority(
        cases: List[TestCase], priority: CasePriority
    ) -> List[TestCase]:
        return [c for c in cases if c.priority == priority]

    @staticmethod
    def filter_by_type(cases: List[TestCase], case_type: CaseType) -> List[TestCase]:
        return [c for c in cases if c.type == case_type]

    @staticmethod
    def filter_by_category(
        cases: List[TestCase], category: str
    ) -> List[TestCase]:
        return [c for c in cases if c.category == category]

    @staticmethod
    def filter_by_tag(cases: List[TestCase], tag: str) -> List[TestCase]:
        return [c for c in cases if tag in c.tags]

    @staticmethod
    def filter_by_ids(cases: List[TestCase], case_ids: List[str]) -> List[TestCase]:
        id_set = set(case_ids)
        return [c for c in cases if c.case_id in id_set]
