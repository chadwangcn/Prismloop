"""src/screenshot_collector.py 单元测试

覆盖:
- 数据模型 ScreenShot / FlowRecording / CollectionResult
- ADBNavigator 行为
- ACEPCapturer 截图/录屏
- ScreenshotCollector.collect_all / record_flows
- 失败路径(导航失败、截图失败、录屏失败)
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List
from unittest import mock

import pytest

from src.screenshot_collector import (
    ADBNavigator,
    ACEPCapturer,
    CollectionResult,
    FlowRecording,
    Navigator,
    ScreenShot,
    ScreenshotCollector,
    ScreenshotCollectorError,
)


# ============================================================================
# Fake 实现
# ============================================================================


class FakeNavigator:
    """可编程的 Navigator fake"""

    def __init__(
        self,
        navigate_ok: bool = True,
        trigger_ok: bool = True,
        edge_ok: bool = True,
    ):
        self.navigate_ok = navigate_ok
        self.trigger_ok = trigger_ok
        self.edge_ok = edge_ok
        self.navigate_calls: List[tuple] = []
        self.trigger_calls: List[tuple] = []
        self.edge_calls: List[tuple] = []
        self.run_command_calls: List[tuple] = []
        self.run_command_ok = True

    def navigate_to(self, pod_id: str, route: str) -> bool:
        self.navigate_calls.append((pod_id, route))
        return self.navigate_ok

    def trigger_state(self, pod_id: str, trigger: str) -> bool:
        self.trigger_calls.append((pod_id, trigger))
        return self.trigger_ok

    def trigger_edge(self, pod_id: str, trigger: str) -> bool:
        self.edge_calls.append((pod_id, trigger))
        return self.edge_ok

    def run_command(self, pod_id: str, command: str) -> bool:
        self.run_command_calls.append((pod_id, command))
        return self.run_command_ok


class FakeCapturer:
    """可编程的 Capturer fake"""

    def __init__(
        self,
        screenshot_ok: bool = True,
        recording_ok: bool = True,
    ):
        self.screenshot_ok = screenshot_ok
        self.recording_ok = recording_ok
        self.screenshot_calls: List[tuple] = []
        self.start_recording_calls: List[str] = []
        self.stop_recording_calls: List[tuple] = []
        self._recording_started = False

    def screenshot(self, pod_id: str, save_path: str) -> bytes:
        self.screenshot_calls.append((pod_id, save_path))
        if not self.screenshot_ok:
            raise RuntimeError("screenshot failed")
        # 写一个空文件以验证路径
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(b"fake-png")
        return b"fake-png"

    def start_recording(self, pod_id: str) -> None:
        if not self.recording_ok:
            raise RuntimeError("start_recording failed")
        self.start_recording_calls.append(pod_id)
        self._recording_started = True

    def stop_recording(self, pod_id: str, save_path: str) -> bytes:
        if not self.recording_ok:
            raise RuntimeError("stop_recording failed")
        self.stop_recording_calls.append((pod_id, save_path))
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(b"fake-mp4")
        self._recording_started = False
        return b"fake-mp4"


# ============================================================================
# 设计合约 fixture
# ============================================================================


@pytest.fixture
def simple_contract() -> Dict[str, Any]:
    """简单设计合约:1 个 screen,2 个 state(default + loaded)"""
    return {
        "screens": [
            {
                "id": "entry",
                "name": "入口页",
                "route": "/entry",
                "states": [
                    {"id": "loaded", "trigger": "数据返回"},
                ],
            }
        ],
        "flows": [],
    }


@pytest.fixture
def multi_screen_contract() -> Dict[str, Any]:
    """多 screen + adb_command 的设计合约"""
    return {
        "screens": [
            {
                "id": "entry",
                "route": "/entry",
                "adb_launch_package": "com.lumi.osdemo",
                "states": [
                    {
                        "id": "clicked",
                        "trigger": "点击进入",
                        "adb_command": "input tap 320 240",
                    },
                ],
            },
            {
                "id": "home",
                "route": "/home",
                "adb_launch_package": "com.lumi.osdemo",
                "adb_launch_activity": ".HomeActivity",
                "states": [],
            },
        ],
        "flows": [
            {
                "id": "login-flow",
                "edges": [
                    {"from": "entry", "to": "home", "trigger": "点击登录",
                     "adb_command": "input tap 540 960"},
                ],
            },
        ],
    }


# ============================================================================
# 数据模型测试
# ============================================================================


class TestScreenShot:
    def test_default_fields(self):
        s = ScreenShot(
            screen_id="entry", state_id="default",
            screenshot_id="guardian-entry-default", reached=True,
        )
        assert s.local_path == ""
        assert s.error == ""
        assert s.captured_at == 0.0

    def test_failed_screenshot(self):
        s = ScreenShot(
            screen_id="entry", state_id="loaded",
            screenshot_id="id-1", reached=False,
            error="timeout",
        )
        assert s.reached is False
        assert s.error == "timeout"


class TestCollectionResult:
    def test_reached_screens_dedup(self):
        result = CollectionResult(pod_id="p1", role="guardian")
        result.screenshots = [
            ScreenShot("entry", "default", "id1", True),
            ScreenShot("entry", "loaded", "id2", True),
            ScreenShot("home", "default", "id3", False),
            ScreenShot("profile", "default", "id4", True),
        ]
        assert result.reached_screens() == ["entry", "profile"]

    def test_to_output_schema(self):
        result = CollectionResult(pod_id="p1", role="guardian")
        result.screenshots = [
            ScreenShot("entry", "default", "id1", True),
            ScreenShot("entry", "loaded", "id2", False),
        ]
        result.flows = [
            FlowRecording("login", reached=True, edges_total=1, edges_completed=1),
        ]
        schema = result.to_output_schema()
        assert len(schema["screens"]) == 2
        assert schema["screens"][0]["screen_id"] == "entry"
        assert schema["screens"][0]["reached"] is True
        assert schema["screens"][1]["reached"] is False
        assert len(schema["flows_recorded"]) == 1
        assert schema["flows_recorded"][0]["flow_id"] == "login"


# ============================================================================
# ADBNavigator 测试
# ============================================================================


class TestADBNavigator:
    def test_navigate_to_success(self):
        adb = mock.MagicMock()
        nav = ADBNavigator(adb)
        assert nav.navigate_to("pod-1", "/entry") is True
        adb.ensure_connected.assert_called_once_with("pod-1")

    def test_navigate_to_failure(self):
        adb = mock.MagicMock()
        adb.ensure_connected.side_effect = RuntimeError("connect failed")
        nav = ADBNavigator(adb)
        assert nav.navigate_to("pod-1", "/entry") is False

    def test_run_command_success(self):
        adb = mock.MagicMock()
        nav = ADBNavigator(adb)
        assert nav.run_command("pod-1", "input tap 100 200") is True
        adb.shell.assert_called_once_with("pod-1", "input tap 100 200")

    def test_run_command_failure(self):
        adb = mock.MagicMock()
        adb.shell.side_effect = RuntimeError("shell error")
        nav = ADBNavigator(adb)
        assert nav.run_command("pod-1", "input tap 100 200") is False


# ============================================================================
# ACEPCapturer 测试
# ============================================================================


class TestACEPCapturer:
    def test_screenshot_success(self, tmp_path):
        acep = mock.MagicMock()
        acep.batch_screen_shot.return_value = [
            {"url": "https://example.com/ss.png"},
        ]
        capturer = ACEPCapturer(acep)
        save_path = str(tmp_path / "ss.png")

        with mock.patch("urllib.request.urlretrieve") as urlretrieve:
            # 写一个假文件模拟下载
            urlretrieve.side_effect = lambda url, path: open(path, "wb").write(b"png")
            data = capturer.screenshot("pod-1", save_path)

        assert data == b"png"
        acep.batch_screen_shot.assert_called_once_with(["pod-1"], is_saved_on_pod=True)

    def test_screenshot_empty_response(self, tmp_path):
        acep = mock.MagicMock()
        acep.batch_screen_shot.return_value = []
        capturer = ACEPCapturer(acep)
        with pytest.raises(ScreenshotCollectorError, match="返回空"):
            capturer.screenshot("pod-1", str(tmp_path / "ss.png"))

    def test_screenshot_no_url(self, tmp_path):
        acep = mock.MagicMock()
        acep.batch_screen_shot.return_value = [{"path": "/data/ss.png"}]
        capturer = ACEPCapturer(acep)
        with pytest.raises(ScreenshotCollectorError, match="未返回有效 URL"):
            capturer.screenshot("pod-1", str(tmp_path / "ss.png"))

    def test_start_recording_not_implemented(self):
        acep = mock.MagicMock()
        capturer = ACEPCapturer(acep)
        with pytest.raises(NotImplementedError):
            capturer.start_recording("pod-1")


# ============================================================================
# ScreenshotCollector 测试
# ============================================================================


class TestScreenshotCollectorCollectAll:
    def setup_method(self):
        self.capturer = FakeCapturer()
        self.navigator = FakeNavigator()
        self.output_dir = "/tmp/test-screenshot-collector"
        self.collector = ScreenshotCollector(
            capturer=self.capturer,
            navigator=self.navigator,
            output_dir=self.output_dir,
            state_stable_sec=0,  # 测试不等待
        )

    def test_collect_all_single_screen_with_state(self, simple_contract):
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=simple_contract,
        )
        assert result.pod_id == "pod-1"
        assert result.role == "guardian"
        # default + loaded = 2 张截图
        assert len(result.screenshots) == 2
        assert result.screenshots[0].screen_id == "entry"
        assert result.screenshots[0].state_id == "default"
        assert result.screenshots[0].reached is True
        assert result.screenshots[1].state_id == "loaded"
        assert result.screenshots[1].reached is True
        assert result.reached_screens() == ["entry"]

    def test_collect_all_navigate_failure(self, simple_contract):
        self.navigator.navigate_ok = False
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=simple_contract,
        )
        # 导航失败,只有 1 条记录(失败)
        assert len(result.screenshots) == 1
        assert result.screenshots[0].reached is False
        assert "导航失败" in result.screenshots[0].error
        assert result.reached_screens() == []

    def test_collect_all_screenshot_failure(self, simple_contract):
        self.capturer.screenshot_ok = False
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=simple_contract,
        )
        # default 截图失败 + loaded 状态截图失败
        assert len(result.screenshots) == 2
        assert all(not s.reached for s in result.screenshots)
        assert all("screenshot failed" in s.error for s in result.screenshots)

    def test_collect_all_trigger_state_failure(self, simple_contract):
        self.navigator.trigger_ok = False
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=simple_contract,
        )
        # default 成功 + loaded 失败
        assert len(result.screenshots) == 2
        assert result.screenshots[0].reached is True  # default
        assert result.screenshots[1].reached is False  # loaded 触发失败
        assert "触发状态失败" in result.screenshots[1].error

    def test_collect_all_uses_adb_command(self, multi_screen_contract):
        """设计合约中的 adb_command 优先于 trigger"""
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        # entry 的 state "clicked" 应通过 adb_command 调用
        assert len(self.navigator.run_command_calls) >= 1
        # home screen 通过 adb_launch_activity 启动
        cmds = [c[1] for c in self.navigator.run_command_calls]
        assert any("input tap 320 240" in c for c in cmds)
        assert any("am start -n com.lumi.osdemo/.HomeActivity" in c for c in cmds)

    def test_collect_all_empty_contract(self):
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract={"screens": [], "flows": []},
        )
        assert result.screenshots == []
        assert result.flows == []

    def test_collect_all_skip_screen_without_id(self, simple_contract):
        simple_contract["screens"].append({"id": "", "route": "/x"})
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=simple_contract,
        )
        # 仅 entry 的 2 张,跳过空 id screen
        assert len(result.screenshots) == 2

    def test_collect_all_screenshot_id_format(self, simple_contract):
        result = self.collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=simple_contract,
        )
        assert result.screenshots[0].screenshot_id == "guardian-entry-default"
        assert result.screenshots[1].screenshot_id == "guardian-entry-loaded"


class TestScreenshotCollectorRecordFlows:
    def setup_method(self):
        self.capturer = FakeCapturer()
        self.navigator = FakeNavigator()
        self.output_dir = "/tmp/test-screenshot-collector-flows"
        self.collector = ScreenshotCollector(
            capturer=self.capturer,
            navigator=self.navigator,
            output_dir=self.output_dir,
            edge_wait_sec=0,
        )

    def test_record_flows_success(self, multi_screen_contract):
        recordings = self.collector.record_flows(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        assert len(recordings) == 1
        rec = recordings[0]
        assert rec.flow_id == "login-flow"
        assert rec.reached is True
        assert rec.edges_total == 1
        assert rec.edges_completed == 1
        assert rec.local_path.endswith("login-flow.mp4")
        assert len(self.capturer.start_recording_calls) == 1
        assert len(self.capturer.stop_recording_calls) == 1

    def test_record_flows_edge_failure(self, multi_screen_contract):
        self.navigator.run_command_ok = False
        recordings = self.collector.record_flows(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        assert len(recordings) == 1
        rec = recordings[0]
        assert rec.reached is False
        assert rec.edges_completed == 0
        assert rec.edges_total == 1

    def test_record_flows_start_recording_failure(self, multi_screen_contract):
        self.capturer.recording_ok = False
        recordings = self.collector.record_flows(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        assert len(recordings) == 1
        assert recordings[0].reached is False
        assert "start_recording" in recordings[0].error

    def test_record_flows_empty(self):
        recordings = self.collector.record_flows(
            pod_id="pod-1", role="guardian",
            design_contract={"flows": []},
        )
        assert recordings == []

    def test_record_flows_skips_flow_without_id(self, multi_screen_contract):
        multi_screen_contract["flows"].append({"edges": []})  # 无 id
        recordings = self.collector.record_flows(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        assert len(recordings) == 1  # 仅 login-flow


# ============================================================================
# 集成测试:collect_all + record_flows 完整流程
# ============================================================================


class TestScreenshotCollectorIntegration:
    def test_full_collection(self, multi_screen_contract, tmp_path):
        capturer = FakeCapturer()
        navigator = FakeNavigator()
        collector = ScreenshotCollector(
            capturer=capturer,
            navigator=navigator,
            output_dir=str(tmp_path),
            state_stable_sec=0,
            edge_wait_sec=0,
        )

        # 1. 采集截图
        result = collector.collect_all(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        # entry: default + clicked = 2
        # home: default = 1
        assert len(result.screenshots) == 3
        assert result.reached_screens() == ["entry", "home"]

        # 2. 录制 flows
        recordings = collector.record_flows(
            pod_id="pod-1", role="guardian",
            design_contract=multi_screen_contract,
        )
        assert len(recordings) == 1
        assert recordings[0].reached is True

        # 3. 输出 schema 可被消费
        schema = result.to_output_schema()
        assert len(schema["screens"]) == 3
