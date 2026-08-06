"""测试报告生成器

依据架构文档 `architecture/06-证据链与报告规范.md`:
- 本地报告目录结构(第三章)
- result.json 结构(第 3.2 节)
- HTML 报告(第四章)
- 脱敏规则(第五章)
- 证据清单 manifest.json(第 2.2 节)
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .assertor import AssertionResult, AssertionSummary, summarize
from .case_loader import TestCase
from .pod_pool import PodRole

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class ReporterError(Exception):
    """报告生成异常"""


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class StepResult:
    """单个测试步骤执行结果"""

    step_index: int
    pod_id: str
    pod_role: str  # guardian / k1
    method: str
    action: str
    status: str = "passed"  # passed / failed / skipped / error
    duration_sec: float = 0.0
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)
    assertions: List[AssertionResult] = field(default_factory=list)
    mua_run_id: Optional[str] = None
    mua_usage: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step_index,
            "pod": self.pod_id,
            "pod_role": self.pod_role,
            "method": self.method,
            "action": self.action,
            "status": self.status,
            "duration_sec": round(self.duration_sec, 2),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "screenshot": self.screenshots[0] if self.screenshots else None,
            "screenshots": self.screenshots,
            "assertions": [a.to_dict() for a in self.assertions],
            "mua_run_id": self.mua_run_id,
            "mua_usage": self.mua_usage,
            "error": self.error,
        }


@dataclass
class CaseRunResult:
    """单个用例运行结果"""

    run_id: str
    case: TestCase
    status: str = "passed"  # passed / failed / error
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_sec: float = 0.0
    steps: List[StepResult] = field(default_factory=list)
    dual_assertions: List[AssertionResult] = field(default_factory=list)
    mua_usage: Dict[str, Dict[str, int]] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def passed_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "passed")

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "failed")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "case_id": self.case.case_id,
            "title": self.case.title,
            "priority": self.case.priority.value,
            "type": self.case.type.value,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_sec": round(self.duration_sec, 2),
            "topology": self.case.topology,
            "steps": [s.to_dict() for s in self.steps],
            "dual_assertions": [a.to_dict() for a in self.dual_assertions],
            "mua_usage": self.mua_usage,
            "summary": {
                "total_steps": len(self.steps),
                "passed_steps": self.passed_count,
                "failed_steps": self.failed_count,
            },
            "error": self.error,
        }


# ============================================================================
# 证据脱敏
# ============================================================================


class EvidenceSanitizer:
    """证据脱敏器

    架构文档 06-第五章:脱敏规则
    """

    PATTERNS = {
        # Token / Session / Authorization
        "token": (r"(?i)(token|session|authorization|api[_-]?key)['\"\s]*[:=]\s*['\"]?[A-Za-z0-9_\-.]+", "***"),
        # 邮箱
        "email": (r"[\w.-]+@[\w.-]+\.\w+", "t***@lumi.com"),
        # 手机号
        "phone": (r"1[3-9]\d{9}", "138****8888"),
        # 设备 SN(保留前 4 位)
        "device_sn": (r"(K1-[A-Z0-9]{4})-[A-Z0-9]+", r"\1-***"),
    }

    def sanitize_text(self, text: str) -> str:
        """脱敏文本"""
        if not text:
            return text
        for key, (pattern, repl) in self.PATTERNS.items():
            text = re.sub(pattern, repl, text)
        return text

    def sanitize_dict(self, data: Any) -> Any:
        """递归脱敏 dict/list/str"""
        if isinstance(data, str):
            return self.sanitize_text(data)
        if isinstance(data, dict):
            return {k: self.sanitize_dict(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.sanitize_dict(x) for x in data]
        return data


# ============================================================================
# Reporter
# ============================================================================


class Reporter:
    """测试报告生成器

    架构文档 06-第三章:本地报告目录结构
    """

    def __init__(self, reports_dir: str = "reports", sanitize: bool = True):
        self.reports_dir = reports_dir
        self.sanitizer = EvidenceSanitizer() if sanitize else None

    def generate(
        self,
        result: CaseRunResult,
        include_html: bool = True,
    ) -> Dict[str, str]:
        """生成测试报告

        Returns:
            生成的文件路径 dict:{"result_json": ..., "report_html": ..., "manifest": ...}
        """
        run_dir = os.path.join(self.reports_dir, result.run_id)
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(os.path.join(run_dir, "screenshots", "guardian"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "screenshots", "k1"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "recordings"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)

        paths: Dict[str, str] = {}

        # 1. result.json
        result_data = result.to_dict()
        if self.sanitizer:
            result_data = self.sanitizer.sanitize_dict(result_data)
        result_path = os.path.join(run_dir, "result.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)
        paths["result_json"] = result_path

        # 2. manifest.json(证据清单)
        manifest = self._build_manifest(result)
        if self.sanitizer:
            manifest = self.sanitizer.sanitize_dict(manifest)
        manifest_path = os.path.join(run_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        paths["manifest"] = manifest_path

        # 3. report.html
        if include_html:
            html_path = os.path.join(run_dir, "report.html")
            self._generate_html(result_data, html_path)
            paths["report_html"] = html_path

        logger.info("报告已生成: %s (status=%s)", run_dir, result.status)
        return paths

    def _build_manifest(self, result: CaseRunResult) -> Dict[str, Any]:
        """构造证据清单

        架构文档 06-第 2.2 节 manifest.json
        """
        screenshots: Dict[str, List[Dict[str, Any]]] = {"guardian": [], "k1": []}
        recordings: Dict[str, Any] = {}

        for step in result.steps:
            role = step.pod_role
            if role not in screenshots:
                screenshots[role] = []
            for idx, shot in enumerate(step.screenshots):
                screenshots[role].append({
                    "step": step.step_index,
                    "timestamp": step.end_time,
                    "url": None,  # TOS URL 由调用方填充
                    "local_path": shot,
                })

        return {
            "run_id": result.run_id,
            "case_id": result.case.case_id,
            "start_time": result.start_time,
            "end_time": result.end_time,
            "evidence": {
                "screenshots": screenshots,
                "recordings": recordings,
                "audio_analysis": {},
            },
        }

    def _generate_html(self, result: Dict[str, Any], output_path: str) -> None:
        """生成 HTML 报告(自包含,无外部依赖)"""
        case_id = html_lib.escape(str(result.get("case_id", "")))
        title = html_lib.escape(str(result.get("title", "")))
        status = result.get("status", "unknown")
        duration = result.get("duration_sec", 0)
        start_time = result.get("start_time", "")
        run_id = html_lib.escape(str(result.get("run_id", "")))

        status_class = "passed" if status == "passed" else "failed"

        # 步骤 HTML
        steps_html = []
        for step in result.get("steps", []):
            step_status = step.get("status", "unknown")
            step_class = "passed" if step_status == "passed" else "failed"
            step_num = step.get("step", "?")
            pod = html_lib.escape(str(step.get("pod", "")))
            action = html_lib.escape(str(step.get("action", "")))
            method = html_lib.escape(str(step.get("method", "")))
            step_dur = step.get("duration_sec", 0)

            assertions_html = []
            for a in step.get("assertions", []):
                a_passed = a.get("passed", False)
                a_class = "passed" if a_passed else "failed"
                a_desc = html_lib.escape(str(a.get("description", "")))
                a_actual = html_lib.escape(str(a.get("actual", "")))[:200]
                assertions_html.append(
                    f'<div class="assertion {a_class}">'
                    f'<span class="badge {a_class}">{a_class.upper()}</span>'
                    f'<span class="desc">{a_desc}</span>'
                    f'<div class="actual">实际值:{a_actual}</div>'
                    f"</div>"
                )

            screenshot_html = ""
            if step.get("screenshots"):
                shot_path = step["screenshots"][0]
                # 转为相对路径(便于浏览器打开)
                if os.path.isabs(shot_path):
                    rel = os.path.relpath(
                        shot_path, os.path.dirname(output_path)
                    )
                else:
                    rel = shot_path
                screenshot_html = (
                    f'<img class="screenshot" src="{html_lib.escape(rel)}" '
                    f'alt="step-{step_num}" />'
                )

            steps_html.append(f"""
            <div class="step {step_class}">
                <h3>步骤 {step_num} - <span class="badge {step_class}">{step_status.upper()}</span></h3>
                <div class="meta">
                    <span><b>Pod:</b> {pod}</span>
                    <span><b>方法:</b> {method}</span>
                    <span><b>耗时:</b> {step_dur}s</span>
                </div>
                <p><b>操作:</b> {action}</p>
                {screenshot_html}
                <div class="assertions">{''.join(assertions_html)}</div>
            </div>
            """)

        # 协同断言 HTML
        dual_html = []
        for da in result.get("dual_assertions", []):
            da_passed = da.get("passed", False)
            da_class = "passed" if da_passed else "failed"
            da_type = html_lib.escape(str(da.get("type", "")))
            da_desc = html_lib.escape(str(da.get("description", "")))
            dual_html.append(
                f'<div class="dual-assertion {da_class}">'
                f'<span class="badge {da_class}">{da_class.upper()}</span>'
                f'<b>{da_type}</b>: {da_desc}'
                f"</div>"
            )

        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>测试报告 - {case_id}</title>
    <style>
        body {{ font-family: -apple-system, "Helvetica Neue", sans-serif; margin: 20px; color: #333; }}
        h1 {{ border-bottom: 2px solid #4a90e2; padding-bottom: 10px; }}
        .summary {{ background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
        .summary table {{ border-collapse: collapse; }}
        .summary td {{ padding: 4px 12px; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; color: white; font-size: 12px; font-weight: bold; }}
        .badge.passed {{ background: #28a745; }}
        .badge.failed {{ background: #dc3545; }}
        .passed {{ color: #28a745; }}
        .failed {{ color: #dc3545; }}
        .step {{ border-left: 3px solid #ccc; padding: 10px 15px; margin: 15px 0; background: #fafafa; }}
        .step.passed {{ border-left-color: #28a745; }}
        .step.failed {{ border-left-color: #dc3545; }}
        .meta span {{ margin-right: 20px; color: #666; }}
        .screenshot {{ max-width: 300px; border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
        .assertions {{ margin-top: 10px; }}
        .assertion {{ padding: 6px 0; border-bottom: 1px solid #eee; }}
        .assertion .desc {{ margin-left: 8px; }}
        .assertion .actual {{ margin-left: 24px; color: #666; font-size: 12px; }}
        .dual-assertion {{ padding: 8px; margin: 8px 0; background: #f0f0f0; border-radius: 4px; }}
    </style>
</head>
<body>
    <h1>测试报告</h1>
    <div class="summary">
        <table>
            <tr><td><b>用例 ID</b></td><td>{case_id}</td></tr>
            <tr><td><b>标题</b></td><td>{title}</td></tr>
            <tr><td><b>运行 ID</b></td><td>{run_id}</td></tr>
            <tr><td><b>状态</b></td><td class="{status_class}"><b>{status.upper()}</b></td></tr>
            <tr><td><b>开始时间</b></td><td>{html_lib.escape(start_time)}</td></tr>
            <tr><td><b>耗时</b></td><td>{duration} 秒</td></tr>
        </table>
    </div>

    <h2>测试步骤</h2>
    {''.join(steps_html) if steps_html else '<p>无步骤数据</p>'}

    <h2>协同断言</h2>
    {''.join(dual_html) if dual_html else '<p>无协同断言</p>'}
</body>
</html>
"""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)


# ============================================================================
# 工具函数
# ============================================================================


def now_iso() -> str:
    """当前 UTC 时间 ISO 格式"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_run_id(prefix: str = "run") -> str:
    """生成运行 ID"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}"
