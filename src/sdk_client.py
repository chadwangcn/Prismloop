"""火山引擎云手机客户端 SDK 封装

依据架构文档 `architecture/02-云手机与外设模拟.md` 实现:
- 输入事件:sendKeyCode / sendMouseKey / sendMulitTouch / sendImeComposition
- 截图录屏:screenShot / startRecording / stopRecording
- 音视频注入:cameraInject / audioInject(经 ADB broadcast)
- 物理按键:K1 五键映射(power/volume_up/volume_down/ai/camera)
- 应用控制:launchApp / closeApp
- 扬声器采集:配合 ffmpeg 自动分析

火山引擎云手机客户端 SDK 提供两种接入:
1. Native SDK(C/C++ .so/.dylib):高性能,需自行编译绑定
2. HTTP Bridge:由火山引擎控制台或自建桥接服务暴露 HTTP 接口

本封装默认使用 HTTP Bridge 模式(部署在能直连云手机内网的主机上),
同时保留对接 Native SDK 的扩展点(`SDKClient.set_transport`)。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class SDKError(Exception):
    """SDK 调用异常"""


class InjectError(SDKError):
    """音视频注入异常"""


# ============================================================================
# K1 物理按键映射(架构文档 02-第四章)
# ============================================================================


class K1Key(Enum):
    """K1 设备物理按键映射

    依据 `architecture/device/app/Android 测试设备规格.md` 标定结果。
    """

    POWER = {"keycode": 26, "linux_keycode": "0x74", "linux_name": "KEY_POWER"}
    VOLUME_UP = {"keycode": 24, "linux_keycode": "0x73", "linux_name": "KEY_VOLUMEUP"}
    VOLUME_DOWN = {"keycode": 25, "linux_keycode": "0x72", "linux_name": "KEY_VOLUMEDOWN"}
    AI = {"keycode": 67, "linux_keycode": "0x43", "linux_name": "KEY_F9"}
    CAMERA = {"keycode": 139, "linux_keycode": "0x8b", "linux_name": "KEY_F12"}

    @property
    def keycode(self) -> int:
        return self.value["keycode"]

    @property
    def linux_name(self) -> str:
        return self.value["linux_name"]


# 常用 Android 按键
KEYCODE_BACK = 4
KEYCODE_HOME = 3
KEYCODE_MENU = 82
KEYCODE_ENTER = 66


# ============================================================================
# 传输层抽象
# ============================================================================


class Transport:
    """SDK 传输层抽象,子类化以对接不同后端"""

    def call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class HTTPTransport(Transport):
    """HTTP 桥接传输

    假设部署了 SDK Bridge 服务(如 https://github.com/volcengine/vmsdk-bridge)
    在能直连云手机内网的主机上,暴露 RESTful 接口。
    """

    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{method}"
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise SDKError(f"HTTP 调用 {method} 失败: {e}") from e


# ============================================================================
# 录像分析结果
# ============================================================================


@dataclass
class AudioAnalysis:
    """扬声器录像音频分析结果"""

    mean_volume_db: float
    max_volume_db: float
    sample_rate: int
    channels: int
    duration_sec: float
    passed: bool
    threshold_db: float
    raw_output: str = ""


# ============================================================================
# SDK Client
# ============================================================================


class SDKClient:
    """云手机客户端 SDK 封装

    依据架构文档 02-第五章接口清单。
    """

    # ADB broadcast action 常量(火山引擎云手机虚拟外设)
    ACTION_CAMERA_INJECT = "com.volcengine.vphone.CAMERA_INJECT"
    ACTION_AUDIO_INJECT = "com.volcengine.vphone.AUDIO_INJECT"

    def __init__(
        self,
        transport: Transport,
        adb_client: Optional[Any] = None,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ):
        self.transport = transport
        self.adb_client = adb_client  # 用于音视频注入 broadcast
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    @classmethod
    def from_http_bridge(
        cls,
        base_url: str,
        adb_client: Optional[Any] = None,
        timeout: int = 30,
    ) -> "SDKClient":
        """从 HTTP Bridge 构造客户端"""
        return cls(HTTPTransport(base_url, timeout), adb_client)

    def set_transport(self, transport: Transport) -> None:
        """替换传输层(用于对接 Native SDK)"""
        self.transport = transport

    # ----------------------------------------------------------------------
    # 输入事件接口(架构文档 02-第 5.1 节)
    # ----------------------------------------------------------------------

    def send_key_code(
        self,
        pod_id: str,
        keycode: int,
        action: str = "down_up",
        duration_ms: int = 100,
    ) -> Dict[str, Any]:
        """键盘按键

        Args:
            pod_id: 云手机实例 ID
            keycode: Android keycode(如 67=AI, 139=拍照)
            action: down / up / down_up(默认完整按下并松开)
            duration_ms: down_up 模式下按住时长
        """
        return self._call("sendKeyCode", {
            "PodId": pod_id,
            "keycode": keycode,
            "action": action,
            "duration_ms": duration_ms,
        })

    def send_k1_key(
        self,
        pod_id: str,
        key: K1Key,
        action: str = "down_up",
        duration_ms: int = 100,
    ) -> Dict[str, Any]:
        """发送 K1 物理按键(便捷封装)"""
        logger.info("K1 按键: %s (keycode=%d, action=%s)", key.name, key.keycode, action)
        return self.send_key_code(pod_id, key.keycode, action, duration_ms)

    def send_mouse_key(
        self,
        pod_id: str,
        x: int,
        y: int,
        action: str = "down_up",
        button: str = "left",
    ) -> Dict[str, Any]:
        """鼠标按键(点击坐标)"""
        return self._call("sendMouseKey", {
            "PodId": pod_id,
            "x": x,
            "y": y,
            "action": action,
            "button": button,
        })

    def send_mouse_move(
        self,
        pod_id: str,
        x: int,
        y: int,
    ) -> Dict[str, Any]:
        """鼠标移动"""
        return self._call("sendMouseMove", {"PodId": pod_id, "x": x, "y": y})

    def send_mouse_wheel(
        self,
        pod_id: str,
        delta: int,
        x: int = 0,
        y: int = 0,
    ) -> Dict[str, Any]:
        """鼠标滚轮"""
        return self._call("sendMouseWheelArm", {
            "PodId": pod_id,
            "delta": delta,
            "x": x,
            "y": y,
        })

    def send_multi_touch(
        self,
        pod_id: str,
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """多点触控

        架构文档 02-第 5.2 节示例:
            events = [
                {"pointerId": 0, "x": 400, "y": 800, "action": "down"},
                {"pointerId": 1, "x": 800, "y": 800, "action": "down"},
                {"pointerId": 0, "x": 300, "y": 800, "action": "move"},
                {"pointerId": 1, "x": 900, "y": 800, "action": "move"},
                {"pointerId": 0, "action": "up"},
                {"pointerId": 1, "action": "up"},
            ]
        """
        return self._call("sendMulitTouch", {
            "PodId": pod_id,
            "events": events,
        })

    def swipe(
        self,
        pod_id: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> Dict[str, Any]:
        """滑动(便捷封装)

        架构文档 02-第 5.2 节:(540,1800) → (540,600),300ms
        """
        return self.send_multi_touch(pod_id, [
            {"x": x1, "y": y1, "action": "down"},
            {"x": x2, "y": y2, "action": "move", "duration_ms": duration_ms},
            {"x": x2, "y": y2, "action": "up"},
        ])

    def send_ime_composition(
        self,
        pod_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """输入法组合事件(中文输入)"""
        return self._call("sendImeComposition", {
            "PodId": pod_id,
            "text": text,
        })

    def send_edit_text_input(
        self,
        pod_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """文本替换(输入框内容替换)"""
        return self._call("sendEditTextInput", {
            "PodId": pod_id,
            "text": text,
        })

    def send_clipboard(
        self,
        pod_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """剪贴板同步"""
        return self._call("sendClipBoardMessage", {
            "PodId": pod_id,
            "text": text,
        })

    def shake(self, pod_id: str) -> Dict[str, Any]:
        """摇一摇"""
        return self._call("sendShakeEventToRemote", {"PodId": pod_id})

    # ----------------------------------------------------------------------
    # 屏幕 / 录像
    # ----------------------------------------------------------------------

    def screenshot(
        self,
        pod_id: str,
        save_path: Optional[str] = None,
    ) -> bytes:
        """截图

        Args:
            pod_id: 云手机实例 ID
            save_path: 保存路径(None 则只返回 bytes)

        Returns:
            PNG 图像 bytes
        """
        resp = self._call("screenShot", {"PodId": pod_id})
        # _call 已经返回 data 字段(若有 code 结构);否则是整个响应
        data_b64 = resp.get("data") if isinstance(resp, dict) else None
        if not data_b64:
            # 兼容直接返回 base64 字符串的 bridge
            if isinstance(resp, str):
                data_b64 = resp
            elif isinstance(resp, dict):
                data_b64 = resp.get("screenshot") or resp.get("image")
        if not data_b64:
            raise SDKError("截图响应缺少 data 字段")
        data = base64.b64decode(data_b64)
        if save_path:
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(data)
            logger.info("截图已保存: %s", save_path)
        return data

    def start_recording(self, pod_id: str) -> Dict[str, Any]:
        """开始录屏"""
        return self._call("startRecording", {"PodId": pod_id})

    def stop_recording(
        self,
        pod_id: str,
        save_path: Optional[str] = None,
    ) -> bytes:
        """停止录屏并返回视频 bytes"""
        resp = self._call("stopRecording", {"PodId": pod_id})
        data_b64 = resp.get("data") if isinstance(resp, dict) else None
        if not data_b64:
            if isinstance(resp, str):
                data_b64 = resp
            elif isinstance(resp, dict):
                data_b64 = resp.get("recording") or resp.get("video")
        if not data_b64:
            raise SDKError("录屏响应缺少 data 字段")
        data = base64.b64decode(data_b64)
        if save_path:
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(data)
            logger.info("录像已保存: %s", save_path)
        return data

    # ----------------------------------------------------------------------
    # 应用控制
    # ----------------------------------------------------------------------

    def launch_app(self, pod_id: str, package: str, activity: Optional[str] = None) -> Dict[str, Any]:
        """启动应用"""
        payload: Dict[str, Any] = {"PodId": pod_id, "package": package}
        if activity:
            payload["activity"] = activity
        return self._call("launchApp", payload)

    def close_app(self, pod_id: str, package: str) -> Dict[str, Any]:
        """关闭应用"""
        return self._call("closeApp", {"PodId": pod_id, "package": package})

    def set_remote_foreground(self, pod_id: str, package: str) -> Dict[str, Any]:
        """切换应用前台"""
        return self._call("setRemoteForeground", {"PodId": pod_id, "package": package})

    def rotate_screen(self, pod_id: str, orientation: str = "portrait") -> Dict[str, Any]:
        """屏幕旋转(portrait/landscape)"""
        return self._call("rotateScreen", {
            "PodId": pod_id,
            "orientation": orientation,
        })

    def set_nav_bar_status(self, pod_id: str, visible: bool = True) -> Dict[str, Any]:
        """导航栏开关"""
        return self._call("setNavBarStatus", {
            "PodId": pod_id,
            "visible": visible,
        })

    # ----------------------------------------------------------------------
    # 音视频注入(架构文档 02-第二章)
    # ----------------------------------------------------------------------

    def inject_camera_video(
        self,
        pod_id: str,
        video_path_on_pod: str,
        action: str = "start",
    ) -> Dict[str, Any]:
        """注入视频到虚拟摄像头

        架构文档 02-第 2.5 节:通过 ADB broadcast 调用火山引擎云手机内置接收器。

        Args:
            pod_id: 云手机实例 ID
            video_path_on_pod: 视频在云手机上的绝对路径(如 /sdcard/test.mp4)
            action: start / stop
        """
        if not self.adb_client:
            raise InjectError("音视频注入需要 ADB Client,请通过 SDKClient(adb_client=...) 传入")
        return self.adb_client.am_broadcast(
            pod_id,
            self.ACTION_CAMERA_INJECT,
            extras={"action": action, "file": video_path_on_pod},
        )

    def inject_microphone_audio(
        self,
        pod_id: str,
        audio_path_on_pod: str,
        action: str = "start",
    ) -> Dict[str, Any]:
        """注入音频到虚拟麦克风"""
        if not self.adb_client:
            raise InjectError("音视频注入需要 ADB Client,请通过 SDKClient(adb_client=...) 传入")
        return self.adb_client.am_broadcast(
            pod_id,
            self.ACTION_AUDIO_INJECT,
            extras={"action": action, "file": audio_path_on_pod},
        )

    def push_and_inject_video(
        self,
        pod_id: str,
        local_video_path: str,
        pod_dest: str = "/sdcard/",
    ) -> str:
        """推送本地视频到云手机并注入摄像头(一站式)

        Returns:
            视频在云手机上的路径
        """
        if not self.adb_client:
            raise InjectError("推送文件需要 ADB Client")
        if not os.path.exists(local_video_path):
            raise InjectError(f"本地视频不存在: {local_video_path}")

        remote_path = pod_dest + os.path.basename(local_video_path)
        logger.info("推送视频到云手机: %s -> %s", local_video_path, remote_path)
        self.adb_client.push(pod_id, local_video_path, remote_path)
        self.inject_camera_video(pod_id, remote_path, "start")
        return remote_path

    def push_and_inject_audio(
        self,
        pod_id: str,
        local_audio_path: str,
        pod_dest: str = "/sdcard/",
    ) -> str:
        """推送本地音频到云手机并注入麦克风"""
        if not self.adb_client:
            raise InjectError("推送文件需要 ADB Client")
        if not os.path.exists(local_audio_path):
            raise InjectError(f"本地音频不存在: {local_audio_path}")

        remote_path = pod_dest + os.path.basename(local_audio_path)
        logger.info("推送音频到云手机: %s -> %s", local_audio_path, remote_path)
        self.adb_client.push(pod_id, local_audio_path, remote_path)
        self.inject_microphone_audio(pod_id, remote_path, "start")
        return remote_path

    def stop_camera_inject(self, pod_id: str) -> Dict[str, Any]:
        """停止摄像头注入"""
        return self.inject_camera_video(pod_id, "", action="stop")

    def stop_audio_inject(self, pod_id: str) -> Dict[str, Any]:
        """停止麦克风注入"""
        return self.inject_microphone_audio(pod_id, "", action="stop")

    # ----------------------------------------------------------------------
    # 扬声器验证(架构文档 02-第三章)
    # ----------------------------------------------------------------------

    def analyze_recording_audio(
        self,
        recording_path: str,
        min_volume_db: float = -30.0,
    ) -> AudioAnalysis:
        """分析录像音频(ffmpeg volumedetect)

        架构文档 02-第 3.4 节自动化断言:
        - 提取音量信息
        - 解析 mean/max volume
        - 断言 mean > 阈值

        Args:
            recording_path: 录像文件本地路径
            min_volume_db: 平均音量阈值(dB),高于此值才认为扬声器有声音
        """
        if not shutil.which(self.ffmpeg_path):
            raise SDKError(f"未找到 ffmpeg: {self.ffmpeg_path}")

        # 1. 探测音频流信息
        probe = subprocess.run(
            [self.ffprobe_path, "-i", recording_path, "-show_streams", "-select_streams", "a", "-of", "json"],
            capture_output=True,
            text=True,
        )
        sample_rate = 0
        channels = 0
        duration = 0.0
        try:
            probe_data = json.loads(probe.stdout)
            if probe_data.get("streams"):
                stream = probe_data["streams"][0]
                sample_rate = int(stream.get("sample_rate", 0))
                channels = int(stream.get("channels", 0))
                duration = float(stream.get("duration", 0))
        except (ValueError, KeyError) as e:
            logger.warning("解析音频流信息失败: %s", e)

        # 2. 音量检测
        result = subprocess.run(
            [self.ffmpeg_path, "-i", recording_path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        stderr = result.stderr
        mean_db, max_db = self._parse_volumedetect(stderr)

        passed = mean_db > min_volume_db if mean_db > -100 else False
        return AudioAnalysis(
            mean_volume_db=mean_db,
            max_volume_db=max_db,
            sample_rate=sample_rate,
            channels=channels,
            duration_sec=duration,
            passed=passed,
            threshold_db=min_volume_db,
            raw_output=stderr[:2000],
        )

    @staticmethod
    def _parse_volumedetect(stderr: str) -> tuple[float, float]:
        """从 ffmpeg volumedetect 输出中解析音量"""
        mean_db = -100.0
        max_db = -100.0
        for line in stderr.splitlines():
            line = line.strip()
            if "mean_volume" in line:
                # 示例: [Parsed_volumedetect_0 @ 0x...] mean_volume: -27.3 dB
                parts = line.split(":")
                if len(parts) >= 2:
                    val = parts[-1].strip().replace("dB", "").strip()
                    try:
                        mean_db = float(val)
                    except ValueError:
                        pass
            elif "max_volume" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    val = parts[-1].strip().replace("dB", "").strip()
                    try:
                        max_db = float(val)
                    except ValueError:
                        pass
        return mean_db, max_db

    # ----------------------------------------------------------------------
    # 高级组合:轮询等待 UI 状态(架构文档 00-第 6.2 节)
    # ----------------------------------------------------------------------

    def wait_for_visual_state(
        self,
        pod_id: str,
        expected_description: str,
        mua_client: Any,
        timeout_sec: int = 30,
        interval_sec: float = 2.0,
        screenshot_dir: str = "/tmp/wait-screenshots",
    ) -> bool:
        """轮询截图 + MUA 视觉检测,等待预期 UI 状态出现

        架构文档 00-第 6.2 节:
            纯用户视角,不依赖后端回调,通过 UI 截图轮询验证。
        """
        import time as _time

        start = _time.time()
        os.makedirs(screenshot_dir, exist_ok=True)
        round_idx = 0

        while _time.time() - start < timeout_sec:
            round_idx += 1
            shot_path = os.path.join(screenshot_dir, f"{pod_id}-poll-{round_idx}.png")
            try:
                self.screenshot(pod_id, save_path=shot_path)
            except SDKError as e:
                logger.warning("轮询截图失败(round %d): %s", round_idx, e)
                _time.sleep(interval_sec)
                continue

            # 用 MUA 视觉理解检测(简单 prompt)
            try:
                prompt = (
                    f"请观察这张截图,判断是否满足以下描述。"
                    f"只回答 JSON: {{\"matched\": true/false}}。"
                    f"描述:{expected_description}"
                )
                result = mua_client.run_task_one_step(
                    run_name=f"visual-check-{pod_id}-{round_idx}",
                    pod_id=pod_id,
                    user_prompt=prompt,
                    max_step=1,
                    timeout=20,
                )
                # 实际场景应解析 StructOutput,这里简化
                if "true" in str(result).lower():
                    logger.info("UI 状态匹配(round %d)", round_idx)
                    return True
            except Exception as e:
                logger.warning("MUA 视觉检测异常(round %d): %s", round_idx, e)

            _time.sleep(interval_sec)

        logger.warning("等待 UI 状态超时(%ds): %s", timeout_sec, expected_description)
        return False

    # ----------------------------------------------------------------------
    # 内部
    # ----------------------------------------------------------------------

    def _call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用 SDK Bridge"""
        try:
            resp = self.transport.call(method, payload)
        except Exception as e:
            raise SDKError(f"SDK 调用 {method} 失败: {e}") from e

        if resp.get("code") and resp["code"] != 0:
            raise SDKError(f"SDK {method} 返回错误: {resp}")
        return resp.get("data", resp)
