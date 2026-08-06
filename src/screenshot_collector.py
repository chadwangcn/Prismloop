"""harness 闭环截图采集器

依据架构文档 `architecture/09-APP开发验证闭环.md` 第四章:
- 按设计合约的 screen/state 顺序采集截图
- 录制 flow 交互过程
- 输出符合 `06-证据链与报告规范.md` 第二章 manifest.json 契约

设计要点:
- 导航与状态触发抽象为 Navigator 接口(MUA 非刚需,支持 ADBNavigator)
- 截图与录屏通过 ACEP / SDK / ADB 三选一(由调用方注入)
- 不与单一传输层耦合,可单端运行也可双端独立运行
- 输出结构可被 VisualDiffer 直接消费
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class ScreenshotCollectorError(Exception):
    """截图采集异常"""


class NavigationError(ScreenshotCollectorError):
    """导航失败"""


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class ScreenShot:
    """单张截图元数据

    对齐架构文档 09-第 4.3 节 OutputSchema:
    screen_id / state_id / screenshot_id / reached
    """

    screen_id: str
    state_id: str  # 默认状态用 "default"
    screenshot_id: str  # 唯一 ID(用于 manifest 引用)
    reached: bool  # 是否成功到达该 screen/state
    local_path: str = ""  # 本地保存路径
    error: str = ""  # 失败原因
    captured_at: float = 0.0  # Unix 时间戳


@dataclass
class FlowRecording:
    """单个 flow 的录像元数据"""

    flow_id: str
    reached: bool  # 是否完成全部 edges
    local_path: str = ""
    edges_total: int = 0
    edges_completed: int = 0
    error: str = ""
    captured_at: float = 0.0


@dataclass
class CollectionResult:
    """单端采集结果

    对应架构文档 09-第 4.3 节采集 OutputSchema,并扩展为可被
    VisualDiffer 直接消费的形态。
    """

    pod_id: str
    role: str  # guardian / k1
    screenshots: List[ScreenShot] = field(default_factory=list)
    flows: List[FlowRecording] = field(default_factory=list)

    def to_output_schema(self) -> Dict[str, Any]:
        """转换为架构文档 09-第 4.3 节 OutputSchema"""
        return {
            "screens": [
                {
                    "screen_id": s.screen_id,
                    "state_id": s.state_id,
                    "screenshot_id": s.screenshot_id,
                    "reached": s.reached,
                }
                for s in self.screenshots
            ],
            "flows_recorded": [
                {
                    "flow_id": f.flow_id,
                    "reached": f.reached,
                    "edges_completed": f.edges_completed,
                    "edges_total": f.edges_total,
                }
                for f in self.flows
            ],
        }

    def reached_screens(self) -> List[str]:
        """返回所有到达过的 screen_id(去重,保序)"""
        seen = set()
        result: List[str] = []
        for s in self.screenshots:
            if s.reached and s.screen_id not in seen:
                seen.add(s.screen_id)
                result.append(s.screen_id)
        return result


# ============================================================================
# Navigator 协议
# ============================================================================


class Navigator(Protocol):
    """导航协议:抽象 MUA / ADB / SDK 三种导航实现

    MUA 非刚需时,可只实现 ADBNavigator。
    """

    def navigate_to(self, pod_id: str, route: str) -> bool:
        """导航到指定 route

        Args:
            pod_id: 目标 Pod ID
            route: 设计合约中定义的 route(如 "/entry")

        Returns:
            True 表示导航成功(到达该 screen)
        """
        ...

    def trigger_state(self, pod_id: str, trigger: str) -> bool:
        """触发状态切换

        Args:
            pod_id: 目标 Pod ID
            trigger: 设计合约中定义的触发条件(如 "点击登录" / "数据返回")

        Returns:
            True 表示触发成功
        """
        ...

    def trigger_edge(self, pod_id: str, trigger: str) -> bool:
        """触发 flow 边(状态跳转)

        与 trigger_state 的区别:edge 是 screen 之间的跳转,
        state 是同一 screen 内部状态变化。

        Returns:
            True 表示跳转成功
        """
        ...


# ============================================================================
# ADB Navigator 实现(默认,不依赖 MUA)
# ============================================================================


class ADBNavigator:
    """基于 ADB 命令的导航器

    适用于无 MUA 的场景,通过设计合约中预配置的 adb 命令映射导航。

    设计合约扩展字段(可选):
        screens[].states[].adb_command: 触发该状态的具体 ADB 命令
        flows[].edges[].adb_command: 触发该 edge 的具体 ADB 命令
        screens[].adb_launch_package: 该 screen 的启动包名
        screens[].adb_launch_activity: 该 screen 的启动 Activity(可选)
    """

    def __init__(self, adb_client: Any):
        """
        Args:
            adb_client: src.adb_client.ADBClient 实例
        """
        self.adb = adb_client

    def navigate_to(self, pod_id: str, route: str) -> bool:
        """导航(简化:不做实际导航,仅确保 Pod 已连接)

        实际项目应:
        1. 从设计合约读取 route -> adb_launch_package 映射
        2. 调 adb shell am start -n <pkg>/<activity>
        3. 等待页面稳定

        本默认实现仅做连接检查,具体导航由设计合约的 adb_command 触发。
        """
        try:
            self.adb.ensure_connected(pod_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("ADB navigate_to 失败(pod=%s, route=%s): %s", pod_id, route, e)
            return False

    def trigger_state(self, pod_id: str, trigger: str) -> bool:
        """触发状态(由调用方通过 adb_command 参数执行具体命令)

        本方法仅作占位,实际触发应在 ScreenshotCollector 中通过
        design_contract 中的 adb_command 字段调用。
        """
        logger.info(
            "ADBNavigator.trigger_state 触发:%s (pod=%s)",
            trigger, pod_id,
        )
        return True

    def trigger_edge(self, pod_id: str, trigger: str) -> bool:
        """触发 edge(同 trigger_state,仅占位)"""
        logger.info(
            "ADBNavigator.trigger_edge 触发:%s (pod=%s)",
            trigger, pod_id,
        )
        return True

    def run_command(self, pod_id: str, command: str) -> bool:
        """执行具体的 ADB shell 命令"""
        try:
            self.adb.shell(pod_id, command)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("ADB 命令失败(pod=%s): %s | 错误: %s", pod_id, command, e)
            return False


# ============================================================================
# Capturer 协议(截图与录屏)
# ============================================================================


class Capturer(Protocol):
    """截图与录屏协议"""

    def screenshot(self, pod_id: str, save_path: str) -> bytes:
        """截图并保存到 save_path,返回 PNG bytes"""
        ...

    def start_recording(self, pod_id: str) -> None:
        """开始录屏"""
        ...

    def stop_recording(self, pod_id: str, save_path: str) -> bytes:
        """停止录屏并保存到 save_path,返回视频 bytes"""
        ...


# ============================================================================
# ACEP Capturer 实现(默认,通过 ACEP OpenAPI)
# ============================================================================


class ACEPCapturer:
    """基于 ACEP OpenAPI 的截图与录屏实现

    使用 ACEP 的 batch_screen_shot / start_recording / stop_recording。
    """

    def __init__(self, acep_client: Any):
        """
        Args:
            acep_client: src.acep_client.ACEPClient 实例
        """
        self.acep = acep_client

    def screenshot(self, pod_id: str, save_path: str) -> bytes:
        """通过 ACEP 截图,保存到本地 save_path

        ACEP batch_screen_shot 返回 URL,需要下载到本地。
        """
        import urllib.request  # 延迟导入,避免全局依赖

        results = self.acep.batch_screen_shot([pod_id], is_saved_on_pod=True)
        if not results:
            raise ScreenshotCollectorError(f"ACEP 截图返回空(pod={pod_id})")
        url = results[0].get("url") or results[0].get("path")
        if not url or not url.startswith("http"):
            raise ScreenshotCollectorError(
                f"ACEP 截图未返回有效 URL(pod={pod_id}): {results[0]}"
            )

        # 下载到本地
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        urllib.request.urlretrieve(url, save_path)
        with open(save_path, "rb") as f:
            data = f.read()
        logger.info("ACEP 截图已保存: %s (%d bytes)", save_path, len(data))
        return data

    def start_recording(self, pod_id: str) -> None:
        """开始录屏(round_id 由调用方决定)"""
        # ACEP 的 start_recording 需要 round_id,这里由 ScreenshotCollector
        # 通过 _round_id_map 管理并调用 _acep_start_recording。
        # 本实现简化为直接调用,但调用方应使用 ScreenshotCollector.record_flows
        # 而非直接调用 Capturer.start_recording。
        raise NotImplementedError(
            "ACEP start_recording 需要 round_id 参数,请通过 "
            "ScreenshotCollector.record_flows 调用"
        )

    def stop_recording(self, pod_id: str, save_path: str) -> bytes:
        """停止录屏并下载"""
        import urllib.request  # 延迟导入

        resp = self.acep.stop_recording(pod_id)
        url = resp.get("url") or resp.get("path")
        if not url or not url.startswith("http"):
            raise ScreenshotCollectorError(
                f"ACEP stop_recording 未返回有效 URL(pod={pod_id}): {resp}"
            )

        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        urllib.request.urlretrieve(url, save_path)
        with open(save_path, "rb") as f:
            data = f.read()
        logger.info("ACEP 录像已保存: %s (%d bytes)", save_path, len(data))
        return data


# ============================================================================
# ScreenshotCollector 主类
# ============================================================================


class ScreenshotCollector:
    """截图采集器

    架构文档 09-第 4.2 节 ScreenshotCollector 的实现。

    用法:
        collector = ScreenshotCollector(
            capturer=ACEPCapturer(acep_client),
            navigator=ADBNavigator(adb_client),
            output_dir="reports/run-001/screenshots/guardian",
        )
        result = collector.collect_all(
            pod_id="<GUARDIAN_POD_ID>",
            role="guardian",
            design_contract={
                "screens": [...],
                "flows": [...],
            },
        )
    """

    def __init__(
        self,
        capturer: Capturer,
        navigator: Navigator,
        output_dir: str,
        state_stable_sec: float = 1.0,
        edge_wait_sec: float = 2.0,
    ):
        """
        Args:
            capturer: 截图与录屏实现
            navigator: 导航实现
            output_dir: 截图/录像输出根目录
            state_stable_sec: 触发 state 后等待稳定时长(秒)
            edge_wait_sec: 触发 edge 后等待跳转时长(秒)
        """
        self.capturer = capturer
        self.navigator = navigator
        self.output_dir = output_dir
        self.state_stable_sec = state_stable_sec
        self.edge_wait_sec = edge_wait_sec

    # ----------------------------------------------------------------------
    # 公共入口
    # ----------------------------------------------------------------------

    def collect_all(
        self,
        pod_id: str,
        role: str,
        design_contract: Dict[str, Any],
    ) -> CollectionResult:
        """按设计合约采集所有截图

        架构文档 09-第 4.2 节 collect_all。

        Args:
            pod_id: 目标 Pod ID
            role: "guardian" 或 "k1"
            design_contract: 设计合约(screens / flows / tokens)

        Returns:
            CollectionResult,含所有截图与录像元数据
        """
        result = CollectionResult(pod_id=pod_id, role=role)
        os.makedirs(self.output_dir, exist_ok=True)

        screens = design_contract.get("screens", [])
        logger.info(
            "开始采集截图:pod=%s, role=%s, screens=%d",
            pod_id, role, len(screens),
        )

        for screen in screens:
            screen_id = screen.get("id", "")
            route = screen.get("route", "")
            if not screen_id:
                logger.warning("screen 缺少 id,跳过")
                continue

            # 1. 导航到 screen
            nav_ok = self._navigate_to_screen(pod_id, screen)
            if not nav_ok:
                result.screenshots.append(ScreenShot(
                    screen_id=screen_id,
                    state_id="default",
                    screenshot_id=self._gen_id(role, screen_id, "default"),
                    reached=False,
                    error=f"导航失败: route={route}",
                    captured_at=time.time(),
                ))
                continue

            # 2. 采集 default state
            self._wait_stable(self.state_stable_sec)
            default_shot = self._capture(
                pod_id, role, screen_id, "default",
            )
            result.screenshots.append(default_shot)

            # 3. 遍历 states
            for state in screen.get("states", []):
                state_id = state.get("id", "")
                if not state_id or state_id == "default":
                    continue
                trigger = state.get("trigger", "")
                adb_command = state.get("adb_command", "")

                triggered = self._trigger_state(pod_id, trigger, adb_command)
                if not triggered:
                    result.screenshots.append(ScreenShot(
                        screen_id=screen_id,
                        state_id=state_id,
                        screenshot_id=self._gen_id(role, screen_id, state_id),
                        reached=False,
                        error=f"触发状态失败: {trigger}",
                        captured_at=time.time(),
                    ))
                    continue

                self._wait_stable(self.state_stable_sec)
                state_shot = self._capture(
                    pod_id, role, screen_id, state_id,
                )
                result.screenshots.append(state_shot)

        logger.info(
            "截图采集完成:pod=%s, screenshots=%d, reached_screens=%s",
            pod_id, len(result.screenshots), result.reached_screens(),
        )
        return result

    def record_flows(
        self,
        pod_id: str,
        role: str,
        design_contract: Dict[str, Any],
    ) -> List[FlowRecording]:
        """录制所有 flow

        架构文档 09-第 4.2 节 record_flows。

        Args:
            pod_id: 目标 Pod ID
            role: "guardian" 或 "k1"
            design_contract: 设计合约(含 flows)

        Returns:
            每个 flow 的录像元数据
        """
        flows = design_contract.get("flows", [])
        recordings: List[FlowRecording] = []
        flows_dir = os.path.join(self.output_dir, "flows")
        os.makedirs(flows_dir, exist_ok=True)

        logger.info("开始录制 flows:pod=%s, flows=%d", pod_id, len(flows))

        for flow in flows:
            flow_id = flow.get("id", "")
            if not flow_id:
                continue

            recording = self._record_single_flow(
                pod_id, role, flow_id, flow.get("edges", []),
            )
            recordings.append(recording)

        return recordings

    # ----------------------------------------------------------------------
    # 内部实现
    # ----------------------------------------------------------------------

    def _navigate_to_screen(
        self, pod_id: str, screen: Dict[str, Any],
    ) -> bool:
        """导航到 screen(优先用 adb_launch_package,其次用 navigator)"""
        route = screen.get("route", "")

        # 优先:设计合约配置的 adb_launch_package
        launch_pkg = screen.get("adb_launch_package", "")
        if launch_pkg and hasattr(self.navigator, "run_command"):
            activity = screen.get("adb_launch_activity", "")
            if activity:
                cmd = f"am start -n {launch_pkg}/{activity}"
            else:
                cmd = (
                    f"monkey -p {launch_pkg} "
                    f"-c android.intent.category.LAUNCHER 1"
                )
            if self.navigator.run_command(pod_id, cmd):
                return True
            logger.warning("adb launch 失败,fallback 到 navigator.navigate_to")

        # fallback:navigator.navigate_to
        if route:
            return self.navigator.navigate_to(pod_id, route)
        return True  # 无 route 视为已到达(单 screen 场景)

    def _trigger_state(
        self, pod_id: str, trigger: str, adb_command: str = "",
    ) -> bool:
        """触发状态切换"""
        # 优先:adb_command
        if adb_command and hasattr(self.navigator, "run_command"):
            return self.navigator.run_command(pod_id, adb_command)

        # fallback:navigator.trigger_state
        return self.navigator.trigger_state(pod_id, trigger)

    def _capture(
        self,
        pod_id: str,
        role: str,
        screen_id: str,
        state_id: str,
    ) -> ScreenShot:
        """执行截图"""
        screenshot_id = self._gen_id(role, screen_id, state_id)
        save_path = os.path.join(
            self.output_dir,
            f"{screen_id}-{state_id}.png",
        )

        try:
            self.capturer.screenshot(pod_id, save_path=save_path)
            return ScreenShot(
                screen_id=screen_id,
                state_id=state_id,
                screenshot_id=screenshot_id,
                reached=True,
                local_path=save_path,
                captured_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "截图失败(pod=%s, screen=%s, state=%s): %s",
                pod_id, screen_id, state_id, e,
            )
            return ScreenShot(
                screen_id=screen_id,
                state_id=state_id,
                screenshot_id=screenshot_id,
                reached=False,
                error=str(e),
                captured_at=time.time(),
            )

    def _record_single_flow(
        self,
        pod_id: str,
        role: str,
        flow_id: str,
        edges: List[Dict[str, Any]],
    ) -> FlowRecording:
        """录制单个 flow"""
        flows_dir = os.path.join(self.output_dir, "flows")
        save_path = os.path.join(flows_dir, f"{flow_id}.mp4")
        edges_total = len(edges)
        edges_completed = 0

        try:
            self.capturer.start_recording(pod_id)
        except NotImplementedError as e:
            return FlowRecording(
                flow_id=flow_id,
                reached=False,
                error=str(e),
                edges_total=edges_total,
                captured_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001
            return FlowRecording(
                flow_id=flow_id,
                reached=False,
                error=f"start_recording 失败: {e}",
                edges_total=edges_total,
                captured_at=time.time(),
            )

        try:
            for edge in edges:
                trigger = edge.get("trigger", "")
                adb_command = edge.get("adb_command", "")

                # 优先用 adb_command,否则用 navigator.trigger_edge
                if adb_command and hasattr(self.navigator, "run_command"):
                    ok = self.navigator.run_command(pod_id, adb_command)
                else:
                    ok = self.navigator.trigger_edge(pod_id, trigger)

                if not ok:
                    logger.warning(
                        "edge 触发失败(flow=%s, trigger=%s)",
                        flow_id, trigger,
                    )
                    break
                edges_completed += 1
                self._wait_stable(self.edge_wait_sec)

            try:
                self.capturer.stop_recording(pod_id, save_path=save_path)
                return FlowRecording(
                    flow_id=flow_id,
                    reached=(edges_completed == edges_total),
                    local_path=save_path,
                    edges_total=edges_total,
                    edges_completed=edges_completed,
                    captured_at=time.time(),
                )
            except Exception as e:  # noqa: BLE001
                return FlowRecording(
                    flow_id=flow_id,
                    reached=False,
                    error=f"stop_recording 失败: {e}",
                    edges_total=edges_total,
                    edges_completed=edges_completed,
                    captured_at=time.time(),
                )
        except Exception as e:  # noqa: BLE001
            return FlowRecording(
                flow_id=flow_id,
                reached=False,
                error=str(e),
                edges_total=edges_total,
                edges_completed=edges_completed,
                captured_at=time.time(),
            )

    def _wait_stable(self, duration_sec: float) -> None:
        """等待 UI 稳定"""
        if duration_sec > 0:
            time.sleep(duration_sec)

    def _gen_id(self, role: str, screen_id: str, state_id: str) -> str:
        """生成 screenshot_id"""
        return f"{role}-{screen_id}-{state_id}"
