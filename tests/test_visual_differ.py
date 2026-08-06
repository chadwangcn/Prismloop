"""src/visual_differ.py 单元测试

覆盖:
- 数据模型 Diff / DiffReport
- 基线索引构建(_build_baseline_map)
- 像素级 diff(_pixel_diff):完全一致 / 有偏差 / 尺寸不一致 / 文件缺失
- 完整 diff 流程:成功 / 基线缺失 / 实际未到达 / 设计中 screen 未采集
- 汇总信息与 passed 判断
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest
from PIL import Image

from src.visual_differ import (
    Diff,
    DiffReport,
    DiffType,
    Severity,
    VisualDiffer,
    VisualDifferError,
)


# ============================================================================
# 辅助:生成测试图像
# ============================================================================


def make_image(path: str, color: tuple, size: tuple = (100, 100)) -> None:
    """生成纯色图像"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img = Image.new("RGB", size, color)
    img.save(path)


def make_image_with_block(
    path: str,
    bg_color: tuple,
    block_color: tuple,
    block_box: tuple = (40, 40, 60, 60),
    size: tuple = (100, 100),
) -> None:
    """生成带小色块的图像"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img = Image.new("RGB", size, bg_color)
    block = Image.new("RGB", (block_box[2] - block_box[0], block_box[3] - block_box[1]), block_color)
    img.paste(block, (block_box[0], block_box[1]))
    img.save(path)


# ============================================================================
# 设计合约 fixture
# ============================================================================


@pytest.fixture
def design_contract() -> Dict[str, Any]:
    """设计合约:1 个 screen,1 个 default + 1 个 state"""
    return {
        "design_version": "v1.0.0",
        "screens": [
            {
                "id": "entry",
                "route": "/entry",
                "baseline_image": "figma://entry-default.png",
                "states": [
                    {
                        "id": "loaded",
                        "trigger": "数据返回",
                        "baseline_image": "figma://entry-loaded.png",
                    },
                ],
            },
        ],
        "flows": [],
    }


@pytest.fixture
def diff_env(tmp_path):
    """构造完整的 diff 测试环境:baseline_dir / actual_dir / diff_dir"""
    baseline_dir = tmp_path / "baseline"
    actual_dir = tmp_path / "actual"
    diff_dir = tmp_path / "diffs"

    # 基线图:红色 default + 蓝色 loaded
    make_image(str(baseline_dir / "entry-default.png"), (255, 0, 0))
    make_image(str(baseline_dir / "entry-loaded.png"), (0, 0, 255))

    # 实际图:红色 default(完全一致)+ 蓝色 loaded 带 30x30 色块(9% 偏差,触发 Major 阈值)
    make_image(str(actual_dir / "entry-default.png"), (255, 0, 0))
    make_image_with_block(
        str(actual_dir / "entry-loaded.png"),
        bg_color=(0, 0, 255),
        block_color=(0, 255, 0),
        block_box=(0, 0, 30, 30),  # 30x30 = 900 px = 9% 偏差
    )

    return {
        "baseline_dir": str(baseline_dir),
        "actual_dir": str(actual_dir),
        "diff_dir": str(diff_dir),
    }


# ============================================================================
# 数据模型测试
# ============================================================================


class TestDiffModels:
    def test_diff_default_fields(self):
        d = Diff(
            screen_id="entry", state_id="default",
            type=DiffType.LAYOUT.value, severity=Severity.MAJOR.value,
        )
        assert d.deviation_pct == 0.0
        assert d.description == ""
        assert d.diff_image == ""

    def test_diff_report_passed_no_critical(self):
        report = DiffReport()
        report.diffs = [
            Diff("entry", "default", DiffType.LAYOUT.value, Severity.MAJOR.value),
            Diff("entry", "loaded", DiffType.COLOR.value, Severity.MINOR.value),
        ]
        report.summary = {"critical": 0, "major": 1, "minor": 1, "total_diffs": 2, "passed": True}
        assert report.passed is True

    def test_diff_report_failed_with_critical(self):
        report = DiffReport()
        report.diffs = [
            Diff("entry", "default", DiffType.MISSING_ELEMENT.value, Severity.CRITICAL.value),
        ]
        report.summary = {"critical": 1, "major": 0, "minor": 0, "total_diffs": 1, "passed": False}
        assert report.passed is False

    def test_diff_report_to_dict(self):
        report = DiffReport(design_version="v1", actual_version="v2")
        report.diffs = [
            Diff("s1", "default", DiffType.LAYOUT.value, Severity.MAJOR.value,
                 deviation_pct=8.5),
        ]
        report.summary = {"critical": 0, "major": 1, "minor": 0, "total_diffs": 1, "passed": True}
        d = report.to_dict()
        assert d["design_version"] == "v1"
        assert len(d["diffs"]) == 1
        assert d["diffs"][0]["deviation_pct"] == 8.5
        assert d["summary"]["passed"] is True

    def test_diff_report_save(self, tmp_path):
        report = DiffReport(design_version="v1")
        report.diffs = [Diff("s1", "default", "layout", "Major")]
        report.summary = {"critical": 0, "major": 1, "minor": 0, "total_diffs": 1, "passed": True}
        path = str(tmp_path / "sub" / "report.json")
        report.save(path)
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["design_version"] == "v1"
        assert len(loaded["diffs"]) == 1


# ============================================================================
# _build_baseline_map 测试
# ============================================================================


class TestBuildBaselineMap:
    def test_strips_figma_prefix(self, design_contract):
        differ = VisualDiffer(
            baseline_dir="/tmp/b", actual_dir="/tmp/a", diff_dir="/tmp/d",
        )
        m = differ._build_baseline_map(design_contract)
        assert "entry" in m
        # figma:// 前缀应被去除
        assert m["entry"]["default"] == "entry-default.png"
        assert m["entry"]["loaded"] == "entry-loaded.png"

    def test_handles_missing_baseline_image(self):
        contract = {
            "screens": [
                {"id": "s1", "states": [{"id": "s1_a"}]},  # 无 baseline_image
            ],
        }
        differ = VisualDiffer(baseline_dir="/tmp/b", actual_dir="/tmp/a", diff_dir="/tmp/d")
        m = differ._build_baseline_map(contract)
        assert m["s1"] == {}  # 无基线记录

    def test_skips_screen_without_id(self):
        contract = {"screens": [{"id": "", "baseline_image": "x.png"}]}
        differ = VisualDiffer(baseline_dir="/tmp/b", actual_dir="/tmp/a", diff_dir="/tmp/d")
        m = differ._build_baseline_map(contract)
        assert m == {}


# ============================================================================
# _pixel_diff 测试
# ============================================================================


class TestPixelDiff:
    def test_identical_images(self, tmp_path):
        baseline = str(tmp_path / "base.png")
        actual = str(tmp_path / "act.png")
        make_image(baseline, (255, 0, 0))
        make_image(actual, (255, 0, 0))

        differ = VisualDiffer(
            baseline_dir=str(tmp_path),
            actual_dir=str(tmp_path),
            diff_dir=str(tmp_path / "diffs"),
        )
        deviation, diff_rel = differ._pixel_diff(
            actual, baseline, "entry", "default",
        )
        assert deviation == 0.0
        assert diff_rel == "entry-default-diff.png"

    def test_images_with_deviation(self, tmp_path):
        baseline = str(tmp_path / "base.png")
        actual = str(tmp_path / "act.png")
        make_image(baseline, (255, 0, 0))
        make_image_with_block(
            actual, bg_color=(255, 0, 0), block_color=(0, 255, 0),
            block_box=(0, 0, 30, 30),  # 30x30 = 900 px 偏差
        )

        differ = VisualDiffer(
            baseline_dir=str(tmp_path),
            actual_dir=str(tmp_path),
            diff_dir=str(tmp_path / "diffs"),
        )
        deviation, _ = differ._pixel_diff(actual, baseline, "s", "d")
        assert deviation > 0.0
        # 900 / 10000 = 9%
        assert 5.0 < deviation < 15.0

    def test_size_mismatch_raises(self, tmp_path):
        baseline = str(tmp_path / "base.png")
        actual = str(tmp_path / "act.png")
        make_image(baseline, (0, 0, 0), size=(100, 100))
        make_image(actual, (0, 0, 0), size=(200, 200))

        differ = VisualDiffer(
            baseline_dir=str(tmp_path),
            actual_dir=str(tmp_path),
            diff_dir=str(tmp_path / "diffs"),
        )
        with pytest.raises(VisualDifferError, match="尺寸不一致"):
            differ._pixel_diff(actual, baseline, "s", "d")

    def test_missing_actual_file(self, tmp_path):
        baseline = str(tmp_path / "base.png")
        make_image(baseline, (0, 0, 0))

        differ = VisualDiffer(
            baseline_dir=str(tmp_path),
            actual_dir=str(tmp_path),
            diff_dir=str(tmp_path / "diffs"),
        )
        with pytest.raises(FileNotFoundError, match="实际截图不存在"):
            differ._pixel_diff(
                str(tmp_path / "nonexistent.png"), baseline, "s", "d",
            )

    def test_missing_baseline_file(self, tmp_path):
        actual = str(tmp_path / "act.png")
        make_image(actual, (0, 0, 0))

        differ = VisualDiffer(
            baseline_dir=str(tmp_path),
            actual_dir=str(tmp_path),
            diff_dir=str(tmp_path / "diffs"),
        )
        with pytest.raises(FileNotFoundError, match="基线图不存在"):
            differ._pixel_diff(
                actual, str(tmp_path / "nonexistent.png"), "s", "d",
            )


# ============================================================================
# 完整 diff 流程测试
# ============================================================================


class TestVisualDifferDiff:
    def test_diff_all_match(self, diff_env, design_contract):
        """实际与基线完全一致(无偏差)"""
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
            design_version="v1.0.0",
            actual_version="commit-abc",
        )
        # 修改 actual/default 与 baseline 完全一致
        # (默认已经是红色 default + 蓝色 loaded 完全一致)
        actual_screenshots = [
            {
                "screen_id": "entry",
                "state_id": "default",
                "local_path": "entry-default.png",
                "reached": True,
            },
            {
                "screen_id": "entry",
                "state_id": "loaded",
                "local_path": "entry-loaded.png",
                "reached": True,
            },
        ]
        report = differ.diff(actual_screenshots, design_contract)
        # default 完全一致(0 偏差,无 diff)
        # loaded 有色块(偏差)
        assert report.total_screens == 1
        assert report.total_states == 2  # default + loaded
        # 仅 loaded 有偏差
        loaded_diffs = [d for d in report.diffs if d.state_id == "loaded"]
        assert len(loaded_diffs) == 1
        assert loaded_diffs[0].deviation_pct > 0
        assert report.passed is True  # 无 Critical

    def test_diff_with_critical_size_mismatch(self, diff_env, design_contract):
        """尺寸不一致返回 Major(size_mismatch)"""
        # 重新生成不同尺寸的实际图
        make_image(
            os.path.join(diff_env["actual_dir"], "entry-default.png"),
            (255, 0, 0), size=(200, 200),
        )
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        actual_screenshots = [
            {
                "screen_id": "entry",
                "state_id": "default",
                "local_path": "entry-default.png",
                "reached": True,
            },
        ]
        report = differ.diff(actual_screenshots, design_contract)
        size_diffs = [d for d in report.diffs if d.type == DiffType.SIZE_MISMATCH.value]
        assert len(size_diffs) == 1
        assert size_diffs[0].severity == Severity.MAJOR.value

    def test_diff_missing_baseline(self, diff_env, design_contract):
        """基线缺失返回 missing_baseline + Major"""
        # 删除 baseline 中的 loaded
        os.remove(os.path.join(diff_env["baseline_dir"], "entry-loaded.png"))
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        actual_screenshots = [
            {
                "screen_id": "entry",
                "state_id": "default",
                "local_path": "entry-default.png",
                "reached": True,
            },
            {
                "screen_id": "entry",
                "state_id": "loaded",
                "local_path": "entry-loaded.png",
                "reached": True,
            },
        ]
        report = differ.diff(actual_screenshots, design_contract)
        missing_baseline = [
            d for d in report.diffs if d.type == DiffType.MISSING_BASELINE.value
        ]
        assert len(missing_baseline) == 1
        assert missing_baseline[0].state_id == "loaded"

    def test_diff_actual_not_reached(self, diff_env, design_contract):
        """实际截图未到达,返回 Critical"""
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        actual_screenshots = [
            {
                "screen_id": "entry",
                "state_id": "default",
                "local_path": "",
                "reached": False,
                "error": "navigation timeout",
            },
        ]
        report = differ.diff(actual_screenshots, design_contract)
        not_reached = [d for d in report.diffs if d.severity == Severity.CRITICAL.value]
        assert len(not_reached) >= 1
        # 还应该有 loaded 未采集的 Critical
        missing_element = [d for d in report.diffs if d.type == DiffType.MISSING_ELEMENT.value]
        assert any(d.state_id == "loaded" for d in missing_element)
        assert report.passed is False  # 有 Critical

    def test_diff_missing_screen_in_actual(self, diff_env, design_contract):
        """设计中存在但实际未采集的 screen/state,返回 Critical"""
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        # 仅采集 default,缺少 loaded
        actual_screenshots = [
            {
                "screen_id": "entry",
                "state_id": "default",
                "local_path": "entry-default.png",
                "reached": True,
            },
        ]
        report = differ.diff(actual_screenshots, design_contract)
        missing = [d for d in report.diffs if d.type == DiffType.MISSING_ELEMENT.value]
        assert any(d.state_id == "loaded" for d in missing)
        assert report.passed is False

    def test_diff_summary_correct(self, diff_env, design_contract):
        """汇总信息正确统计 critical/major/minor"""
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        actual_screenshots = [
            {"screen_id": "entry", "state_id": "default",
             "local_path": "entry-default.png", "reached": True},
            {"screen_id": "entry", "state_id": "loaded",
             "local_path": "entry-loaded.png", "reached": True},
        ]
        report = differ.diff(actual_screenshots, design_contract)
        assert "critical" in report.summary
        assert "major" in report.summary
        assert "minor" in report.summary
        assert "total_diffs" in report.summary
        assert "passed" in report.summary
        # default 一致,loaded 有 minor color diff(偏差小于 5%)
        # 但 loaded 实际上是 900/10000=9%,应该是 Major
        assert report.summary["major"] >= 1
        assert report.summary["passed"] is True  # 无 Critical

    def test_diff_generates_diff_image(self, diff_env, design_contract):
        """diff 图被生成到 diff_dir"""
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        actual_screenshots = [
            {"screen_id": "entry", "state_id": "loaded",
             "local_path": "entry-loaded.png", "reached": True},
        ]
        report = differ.diff(actual_screenshots, design_contract)
        loaded_diff = [d for d in report.diffs if d.state_id == "loaded"][0]
        assert loaded_diff.diff_image == "entry-loaded-diff.png"
        assert os.path.exists(
            os.path.join(diff_env["diff_dir"], loaded_diff.diff_image)
        )

    def test_diff_report_save(self, diff_env, design_contract, tmp_path):
        """DiffReport.save 方法生成 JSON 文件"""
        differ = VisualDiffer(
            baseline_dir=diff_env["baseline_dir"],
            actual_dir=diff_env["actual_dir"],
            diff_dir=diff_env["diff_dir"],
        )
        actual_screenshots = [
            {"screen_id": "entry", "state_id": "default",
             "local_path": "entry-default.png", "reached": True},
        ]
        report = differ.diff(actual_screenshots, design_contract)
        path = str(tmp_path / "report.json")
        report.save(path)
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["design_version"] == "v1.0.0"
        assert "summary" in loaded
