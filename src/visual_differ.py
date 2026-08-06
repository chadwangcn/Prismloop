"""harness 闭环视觉 diff 工具

依据架构文档 `architecture/09-APP开发验证闭环.md` 第五章:
- 像素级对比实际截图与设计基线
- 输出偏差类型:layout / color / font / missing_element / extra_element / motion / interaction
- 输出偏差严重度:Critical / Major / Minor
- 输出 diff 报告(JSON + diff 图)

设计要点:
- 不依赖 MUA,纯图像对比
- 支持缺失基线(返回 missing_baseline 偏差)
- 支持图像尺寸不一致(自动 resize 后对比)
- diff 图高亮差异区域(红色叠加)
- 阈值:偏差 > 5% 为 Major(架构文档 09-5.2)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class VisualDifferError(Exception):
    """视觉 diff 异常"""


# ============================================================================
# 数据模型
# ============================================================================


class DiffType(str, Enum):
    """偏差类型(架构文档 09-5.1)"""

    LAYOUT = "layout"               # 布局偏差:元素位置/尺寸不一致
    COLOR = "color"                 # 颜色偏差:颜色值不一致
    FONT = "font"                   # 字体偏差:字体大小/粗细不一致
    MISSING_ELEMENT = "missing_element"  # 缺失元素:设计有但实际没有
    EXTRA_ELEMENT = "extra_element"       # 多余元素:设计没有但实际有
    MOTION = "motion"               # 动效偏差:过渡动画不一致
    INTERACTION = "interaction"     # 交互偏差:flow 跳转不正确
    MISSING_BASELINE = "missing_baseline"  # 基线缺失
    SIZE_MISMATCH = "size_mismatch"       # 尺寸不一致


class Severity(str, Enum):
    """严重度(架构文档 09-5.1 + 6.1)"""

    CRITICAL = "Critical"  # 必须修复,阻塞
    MAJOR = "Major"        # 应该修复,不阻塞
    MINOR = "Minor"        # 可选修复


@dataclass
class Diff:
    """单个偏差

    对齐架构文档 09-5.3 diff 报告中的 diffs 项。
    """

    screen_id: str
    state_id: str
    type: str              # DiffType 值
    severity: str         # Severity 值
    deviation_pct: float = 0.0  # 偏差百分比
    description: str = ""
    diff_image: str = ""        # diff 图相对路径
    actual_image: str = ""      # 实际截图相对路径
    baseline_image: str = ""    # 基线图相对路径


@dataclass
class DiffReport:
    """diff 报告

    对齐架构文档 09-5.3 diff 报告结构。
    """

    design_version: str = ""
    actual_version: str = ""
    total_screens: int = 0
    total_states: int = 0
    diffs: List[Diff] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "design_version": self.design_version,
            "actual_version": self.actual_version,
            "total_screens": self.total_screens,
            "total_states": self.total_states,
            "diffs": [asdict(d) for d in self.diffs],
            "summary": self.summary,
        }

    def save(self, path: str) -> None:
        """保存为 JSON 文件"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info("diff 报告已保存: %s", path)

    @property
    def passed(self) -> bool:
        """是否通过(无 Critical)"""
        return self.summary.get("critical", 0) == 0


# ============================================================================
# VisualDiffer 主类
# ============================================================================


class VisualDiffer:
    """视觉 diff 工具

    架构文档 09-第 5.2 节 VisualDiffer 的实现。

    用法:
        differ = VisualDiffer(
            baseline_dir=".local-acceptance/visual/v1.2.0/baseline/k1",
            actual_dir="reports/run-001/screenshots/k1",
            diff_dir="reports/run-001/diffs/k1",
        )
        report = differ.diff(
            actual_screenshots=[
                {"screen_id": "entry", "state_id": "default",
                 "local_path": "entry-default.png", "reached": True},
            ],
            design_contract={"screens": [...]},
        )
        print(report.passed, report.summary)
    """

    # 偏差阈值(架构文档 09-5.2:偏差 > 5% 为 Major)
    DEVIATION_MAJOR_THRESHOLD = 5.0
    DEVIATION_CRITICAL_THRESHOLD = 20.0

    def __init__(
        self,
        baseline_dir: str,
        actual_dir: str,
        diff_dir: str,
        design_version: str = "",
        actual_version: str = "",
    ):
        """
        Args:
            baseline_dir: 设计基线图根目录
            actual_dir: 实际截图根目录
            diff_dir: diff 图输出根目录
            design_version: 设计版本号
            actual_version: 实际版本号(如 commit-sha)
        """
        self.baseline_dir = baseline_dir
        self.actual_dir = actual_dir
        self.diff_dir = diff_dir
        self.design_version = design_version
        self.actual_version = actual_version

    # ----------------------------------------------------------------------
    # 公共入口
    # ----------------------------------------------------------------------

    def diff(
        self,
        actual_screenshots: List[Dict[str, Any]],
        design_contract: Dict[str, Any],
    ) -> DiffReport:
        """对比实际截图与设计基线

        架构文档 09-第 5.2 节 diff 方法。

        Args:
            actual_screenshots: 实际截图列表,每项含
                screen_id / state_id / local_path / reached
            design_contract: 设计合约(含 screens,每个 screen 含
                id / states / baseline_image)

        Returns:
            DiffReport
        """
        report = DiffReport(
            design_version=self.design_version or design_contract.get("design_version", ""),
            actual_version=self.actual_version,
        )

        # 索引基线:screen_id -> state_id -> baseline_path
        baseline_map = self._build_baseline_map(design_contract)
        report.total_screens = len(design_contract.get("screens", []))
        report.total_states = sum(
            len(s.get("states", [])) + 1  # +1 for default
            for s in design_contract.get("screens", [])
        )

        os.makedirs(self.diff_dir, exist_ok=True)

        # 遍历实际截图,与基线对比
        for shot in actual_screenshots:
            screen_id = shot.get("screen_id", "")
            state_id = shot.get("state_id", "default")
            actual_path = shot.get("local_path", "")
            reached = shot.get("reached", False)

            # 1. 实际截图未到达
            if not reached:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.INTERACTION.value,
                    severity=Severity.CRITICAL.value,
                    description=f"截图未到达:{shot.get('error', 'unknown')}",
                    actual_image=actual_path,
                ))
                continue

            # 2. 基线缺失(设计合约未指定,或指定但文件不存在)
            baseline_rel = baseline_map.get(screen_id, {}).get(state_id)
            if not baseline_rel:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.MISSING_BASELINE.value,
                    severity=Severity.MAJOR.value,
                    description=f"基线缺失:screen={screen_id}, state={state_id}(设计合约未指定)",
                    actual_image=actual_path,
                ))
                continue

            baseline_abs = os.path.join(self.baseline_dir, baseline_rel)
            if not os.path.exists(baseline_abs):
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.MISSING_BASELINE.value,
                    severity=Severity.MAJOR.value,
                    description=f"基线文件不存在:screen={screen_id}, state={state_id}, path={baseline_rel}",
                    actual_image=actual_path,
                    baseline_image=baseline_rel,
                ))
                continue

            actual_abs = os.path.join(self.actual_dir, actual_path)

            # 3. 执行像素级 diff
            try:
                deviation, diff_rel = self._pixel_diff(
                    actual_abs, baseline_abs,
                    screen_id, state_id,
                )
            except FileNotFoundError as e:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.MISSING_ELEMENT.value,
                    severity=Severity.CRITICAL.value,
                    description=f"文件不存在: {e}",
                    actual_image=actual_path,
                    baseline_image=baseline_rel,
                ))
                continue
            except VisualDifferError as e:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.SIZE_MISMATCH.value,
                    severity=Severity.MAJOR.value,
                    description=str(e),
                    actual_image=actual_path,
                    baseline_image=baseline_rel,
                ))
                continue

            # 4. 根据偏差阈值分类
            if deviation >= self.DEVIATION_CRITICAL_THRESHOLD:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.LAYOUT.value,
                    severity=Severity.CRITICAL.value,
                    deviation_pct=round(deviation, 2),
                    description=f"布局偏差 {deviation:.2f}%(>= {self.DEVIATION_CRITICAL_THRESHOLD}%)",
                    diff_image=diff_rel,
                    actual_image=actual_path,
                    baseline_image=baseline_rel,
                ))
            elif deviation >= self.DEVIATION_MAJOR_THRESHOLD:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.LAYOUT.value,
                    severity=Severity.MAJOR.value,
                    deviation_pct=round(deviation, 2),
                    description=f"布局偏差 {deviation:.2f}%(>= {self.DEVIATION_MAJOR_THRESHOLD}%)",
                    diff_image=diff_rel,
                    actual_image=actual_path,
                    baseline_image=baseline_rel,
                ))
            elif deviation > 0:
                report.diffs.append(Diff(
                    screen_id=screen_id,
                    state_id=state_id,
                    type=DiffType.COLOR.value,
                    severity=Severity.MINOR.value,
                    deviation_pct=round(deviation, 2),
                    description=f"颜色偏差 {deviation:.2f}%",
                    diff_image=diff_rel,
                    actual_image=actual_path,
                    baseline_image=baseline_rel,
                ))

        # 5. 检查设计中存在但实际未采集的 screen/state(missing_element)
        actual_keys = {
            (s.get("screen_id"), s.get("state_id"))
            for s in actual_screenshots
        }
        for screen in design_contract.get("screens", []):
            sid = screen.get("id", "")
            # default
            if (sid, "default") not in actual_keys:
                report.diffs.append(Diff(
                    screen_id=sid,
                    state_id="default",
                    type=DiffType.MISSING_ELEMENT.value,
                    severity=Severity.CRITICAL.value,
                    description=f"设计中 screen={sid} state=default 未采集",
                ))
            for state in screen.get("states", []):
                stid = state.get("id", "")
                if (sid, stid) not in actual_keys:
                    report.diffs.append(Diff(
                        screen_id=sid,
                        state_id=stid,
                        type=DiffType.MISSING_ELEMENT.value,
                        severity=Severity.CRITICAL.value,
                        description=f"设计中 screen={sid} state={stid} 未采集",
                    ))

        # 6. 汇总
        report.summary = self._build_summary(report.diffs)
        logger.info(
            "diff 完成:total=%d, critical=%d, major=%d, minor=%d, passed=%s",
            len(report.diffs),
            report.summary["critical"],
            report.summary["major"],
            report.summary["minor"],
            report.passed,
        )
        return report

    # ----------------------------------------------------------------------
    # 内部实现
    # ----------------------------------------------------------------------

    def _build_baseline_map(
        self, design_contract: Dict[str, Any],
    ) -> Dict[str, Dict[str, str]]:
        """从设计合约构建基线索引

        Returns:
            {screen_id: {state_id: baseline_rel_path}}
        """
        result: Dict[str, Dict[str, str]] = {}
        for screen in design_contract.get("screens", []):
            sid = screen.get("id", "")
            if not sid:
                continue
            result[sid] = {}
            # default 状态基线
            default_baseline = screen.get("baseline_image", "")
            if default_baseline:
                result[sid]["default"] = self._strip_figma_prefix(default_baseline)
            # 其他 state 基线
            for state in screen.get("states", []):
                stid = state.get("id", "")
                st_baseline = state.get("baseline_image", "")
                if stid and st_baseline:
                    result[sid][stid] = self._strip_figma_prefix(st_baseline)
        return result

    @staticmethod
    def _strip_figma_prefix(path: str) -> str:
        """去除 figma:// 前缀,返回相对路径"""
        if path.startswith("figma://"):
            return path[len("figma://"):]
        return path

    def _pixel_diff(
        self,
        actual_path: str,
        baseline_path: str,
        screen_id: str,
        state_id: str,
    ) -> Tuple[float, str]:
        """像素级 diff

        Returns:
            (deviation_pct, diff_image_relpath)
            deviation_pct: 偏差百分比(0-100)
            diff_image_relpath: diff 图相对路径(相对于 self.diff_dir)
        """
        try:
            from PIL import Image, ImageChops
        except ImportError as e:
            raise VisualDifferError(
                "需要 Pillow 库: pip install Pillow"
            ) from e

        if not os.path.exists(actual_path):
            raise FileNotFoundError(f"实际截图不存在: {actual_path}")
        if not os.path.exists(baseline_path):
            raise FileNotFoundError(f"基线图不存在: {baseline_path}")

        actual_img = Image.open(actual_path).convert("RGB")
        baseline_img = Image.open(baseline_path).convert("RGB")

        # 尺寸不一致:resize 到相同尺寸后对比(并记录 size_mismatch)
        size_mismatch_desc = ""
        if actual_img.size != baseline_img.size:
            size_mismatch_desc = (
                f"尺寸不一致:actual={actual_img.size}, "
                f"baseline={baseline_img.size},已 resize 后对比"
            )
            # 以 baseline 尺寸为准
            actual_img = actual_img.resize(baseline_img.size)

        # 像素 diff
        diff = ImageChops.difference(actual_img, baseline_img)
        bbox = diff.getbbox()

        # 计算偏差百分比
        if bbox is None:
            # 完全一致
            deviation = 0.0
        else:
            # 统计差异像素数(用 tobytes 避免 getdata deprecation)
            diff_gray = diff.convert("L")
            total_pixels = diff_gray.width * diff_gray.height
            diff_pixels = sum(1 for p in diff_gray.tobytes() if p > 30)  # 阈值 30
            deviation = (diff_pixels / total_pixels) * 100.0

        # 生成 diff 图(高亮差异区域)
        diff_filename = f"{screen_id}-{state_id}-diff.png"
        diff_relpath = diff_filename
        diff_path = os.path.join(self.diff_dir, diff_filename)

        if bbox is None:
            # 完全一致:保存基线副本并标注 "IDENTICAL"
            marked = baseline_img.copy()
        else:
            # 差异区域用红色叠加
            marked = actual_img.copy()
            overlay = Image.new("RGB", marked.size, (255, 0, 0))
            mask = diff.convert("L").point(lambda p: 128 if p > 30 else 0)
            marked = Image.composite(overlay, marked, mask)

        # 确保 diff_dir 存在
        os.makedirs(os.path.dirname(os.path.abspath(diff_path)), exist_ok=True)
        marked.save(diff_path)
        logger.info(
            "diff 图已保存: %s (deviation=%.2f%%)",
            diff_path, deviation,
        )

        if size_mismatch_desc:
            raise VisualDifferError(size_mismatch_desc)

        return deviation, diff_relpath

    @staticmethod
    def _build_summary(diffs: List[Diff]) -> Dict[str, Any]:
        """构建汇总信息"""
        critical = sum(1 for d in diffs if d.severity == Severity.CRITICAL.value)
        major = sum(1 for d in diffs if d.severity == Severity.MAJOR.value)
        minor = sum(1 for d in diffs if d.severity == Severity.MINOR.value)
        return {
            "critical": critical,
            "major": major,
            "minor": minor,
            "total_diffs": len(diffs),
            "passed": critical == 0,
        }
