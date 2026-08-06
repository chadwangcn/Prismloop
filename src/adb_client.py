"""ADB 公网直连调试通道

依据架构文档 `architecture/02-云手机与外设模拟.md` 第六章:
- ADB 公网直连仅供调试,火山引擎声明不建议用于生产环境
- 单次连接默认有效时长 24 小时
- 用于本地直接操作云手机

同时依据 `architecture/03-APK构建流水线.md` 第六章:
- APK 安装与更新(pm install -r / -d / uninstall+install)
- 应用版本查询
- 安装失败自动处理

设计要点:
- 通过 EIP+Port 远程连接云手机(adb connect <eip>:5555)
- 用 subprocess 调 adb 命令,不引入纯 Python ADB 客户端(避免额外依赖)
- 提供连接池与重连机制
- 供 SDKClient 调用以实现音视频注入 broadcast
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class ADBError(Exception):
    """ADB 调用异常"""


class InstallError(ADBError):
    """APK 安装失败"""


class VersionMismatchError(ADBError):
    """APK 版本不匹配"""


# ============================================================================
# 连接管理
# ============================================================================


@dataclass
class PodConnection:
    """单个云手机的 ADB 连接"""

    pod_id: str
    eip: str
    port: int = 5555
    connected: bool = False
    last_connect_time: float = 0.0
    # ADB 连接默认有效 24h,留 1 小时余量
    ttl_sec: int = 23 * 3600

    @property
    def target(self) -> str:
        return f"{self.eip}:{self.port}"

    def is_stale(self) -> bool:
        """判断连接是否过期需要重连"""
        if not self.connected:
            return True
        return (time.time() - self.last_connect_time) > self.ttl_sec


# ============================================================================
# ADB Client
# ============================================================================


class ADBClient:
    """ADB 调试通道客户端

    架构文档 02-第六章:ADB 公网直连(调试用)
    架构文档 03-第六章:APK 安装与更新流程
    """

    def __init__(
        self,
        adb_path: Optional[str] = None,
        connect_timeout: int = 10,
        command_timeout: int = 30,
        retry_attempts: int = 2,
    ):
        self.adb_path = adb_path or shutil.which("adb") or "adb"
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.retry_attempts = retry_attempts
        self._connections: Dict[str, PodConnection] = {}

    def register_pod(self, pod_id: str, eip: str, port: int = 5555) -> None:
        """注册一个 Pod 的连接信息"""
        self._connections[pod_id] = PodConnection(pod_id=pod_id, eip=eip, port=port)
        logger.info("已注册 Pod ADB 连接: %s -> %s:%d", pod_id, eip, port)

    def ensure_connected(self, pod_id: str) -> str:
        """确保 Pod 的 ADB 连接可用,返回 target

        若未连接或已过期,自动重新连接。
        """
        conn = self._connections.get(pod_id)
        if conn is None:
            raise ADBError(f"Pod {pod_id} 未注册,请先调用 register_pod()")

        if not conn.is_stale():
            return conn.target

        logger.info("ADB 重连 Pod: %s (%s)", pod_id, conn.target)
        self._run_adb(["connect", conn.target], timeout=self.connect_timeout)

        # 验证连接
        devices = self._list_devices()
        if conn.target not in devices:
            raise ADBError(f"ADB 连接失败: {conn.target} 不在设备列表中")

        conn.connected = True
        conn.last_connect_time = time.time()
        logger.info("ADB 连接成功: %s", conn.target)
        return conn.target

    def disconnect(self, pod_id: str) -> None:
        """主动断开 Pod 连接"""
        conn = self._connections.get(pod_id)
        if conn is None:
            return
        try:
            self._run_adb(["disconnect", conn.target])
        except ADBError as e:
            logger.warning("断开 %s 失败: %s", conn.target, e)
        conn.connected = False

    # ----------------------------------------------------------------------
    # 通用 shell 命令
    # ----------------------------------------------------------------------

    def shell(self, pod_id: str, command: str, timeout: Optional[int] = None) -> str:
        """执行 adb shell 命令

        Args:
            pod_id: 目标 Pod ID
            command: shell 命令字符串
            timeout: 超时秒(默认使用 self.command_timeout)
        """
        target = self.ensure_connected(pod_id)
        return self._run_adb(
            ["-s", target, "shell", command],
            timeout=timeout or self.command_timeout,
        )

    def shell_batch(self, pod_id: str, commands: List[str]) -> List[str]:
        """批量执行 shell 命令(顺序执行)"""
        results = []
        for cmd in commands:
            results.append(self.shell(pod_id, cmd))
        return results

    # ----------------------------------------------------------------------
    # 文件操作
    # ----------------------------------------------------------------------

    def push(self, pod_id: str, local_path: str, remote_path: str) -> str:
        """推送文件到云手机"""
        if not os.path.exists(local_path):
            raise ADBError(f"本地文件不存在: {local_path}")
        target = self.ensure_connected(pod_id)
        logger.info("推送文件: %s -> %s:%s", local_path, target, remote_path)
        return self._run_adb(
            ["-s", target, "push", local_path, remote_path],
            timeout=max(self.command_timeout, 120),
        )

    def pull(self, pod_id: str, remote_path: str, local_path: str) -> str:
        """从云手机拉取文件"""
        target = self.ensure_connected(pod_id)
        local_dir = os.path.dirname(os.path.abspath(local_path))
        os.makedirs(local_dir, exist_ok=True)
        logger.info("拉取文件: %s:%s -> %s", target, remote_path, local_path)
        return self._run_adb(
            ["-s", target, "pull", remote_path, local_path],
            timeout=max(self.command_timeout, 120),
        )

    # ----------------------------------------------------------------------
    # 截图与录屏(架构文档 02-第 6.2 节)
    # ----------------------------------------------------------------------

    def screenshot(self, pod_id: str, local_path: str) -> str:
        """截图并拉取到本地"""
        remote = "/sdcard/_adb_screenshot.png"
        self.shell(pod_id, f"screencap -p {remote}")
        self.pull(pod_id, remote, local_path)
        self.shell(pod_id, f"rm -f {remote}")
        logger.info("截图已保存: %s", local_path)
        return local_path

    def screenrecord(
        self,
        pod_id: str,
        local_path: str,
        duration_sec: int = 30,
        bit_rate: int = 8000000,
    ) -> str:
        """录屏并拉取到本地(Android 4.4+,最长 180s)"""
        if duration_sec > 180:
            raise ADBError("screenrecord 单次最长 180 秒")
        remote = "/sdcard/_adb_screenrecord.mp4"
        logger.info("开始录屏 %ds: %s", duration_sec, pod_id)
        self.shell(
            pod_id,
            f"screenrecord {remote} --time-limit {duration_sec} --bit-rate {bit_rate}",
            timeout=duration_sec + 30,
        )
        self.pull(pod_id, remote, local_path)
        self.shell(pod_id, f"rm -f {remote}")
        logger.info("录屏已保存: %s", local_path)
        return local_path

    # ----------------------------------------------------------------------
    # 输入事件(架构文档 02-第 6.2 节)
    # ----------------------------------------------------------------------

    def input_tap(self, pod_id: str, x: int, y: int) -> str:
        """点击坐标"""
        return self.shell(pod_id, f"input tap {x} {y}")

    def input_swipe(
        self,
        pod_id: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> str:
        """滑动"""
        return self.shell(pod_id, f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def input_long_press(self, pod_id: str, x: int, y: int, duration_ms: int = 1000) -> str:
        """长按(用 swipe 同坐标实现)"""
        return self.input_swipe(pod_id, x, y, x, y, duration_ms)

    def input_text(self, pod_id: str, text: str) -> str:
        """输入文本(仅英文/数字)"""
        # 转义 shell 特殊字符
        safe_text = text.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<")
        return self.shell(pod_id, f"input text '{safe_text}'")

    def input_text_chinese(self, pod_id: str, text: str) -> str:
        """输入中文(需 ADBKeyboard 输入法)"""
        return self.am_broadcast(
            pod_id, "ADB_INPUT_TEXT", extras={"msg": text}
        )

    def input_keyevent(self, pod_id: str, keycode: int) -> str:
        """发送按键事件"""
        return self.shell(pod_id, f"input keyevent {keycode}")

    # ----------------------------------------------------------------------
    # broadcast(用于音视频注入,架构文档 02-第 2.5 节)
    # ----------------------------------------------------------------------

    def am_broadcast(
        self,
        pod_id: str,
        action: str,
        extras: Optional[Dict[str, str]] = None,
        component: Optional[str] = None,
    ) -> str:
        """发送 broadcast

        Args:
            pod_id: 目标 Pod
            action: broadcast action(如 com.volcengine.vphone.CAMERA_INJECT)
            extras: 额外参数(--es key value)
            component: 指定组件(如 com.pkg/.Receiver)
        """
        cmd_parts = ["am", "broadcast", "-a", action]
        for k, v in (extras or {}).items():
            cmd_parts.extend(["--es", k, v])
        if component:
            cmd_parts.extend(["-n", component])
        return self.shell(pod_id, " ".join(cmd_parts))

    # ----------------------------------------------------------------------
    # APK 安装与更新(架构文档 03-第六章)
    # ----------------------------------------------------------------------

    def install(
        self,
        pod_id: str,
        apk_path: str,
        replace: bool = True,
        allow_downgrade: bool = False,
    ) -> str:
        """安装 APK

        Args:
            pod_id: 目标 Pod
            apk_path: 本地 APK 路径
            replace: 覆盖安装(pm install -r)
            allow_downgrade: 允许降级(pm install -d)
        """
        if not os.path.exists(apk_path):
            raise ADBError(f"APK 文件不存在: {apk_path}")

        target = self.ensure_connected(pod_id)
        cmd = ["-s", target, "install"]
        if replace:
            cmd.append("-r")
        if allow_downgrade:
            cmd.append("-d")
        cmd.append(apk_path)

        logger.info("安装 APK: %s (pod=%s)", apk_path, pod_id)
        result = self._run_adb(cmd, timeout=max(self.command_timeout, 180))
        if "Success" not in result:
            raise InstallError(f"APK 安装失败: {result}")
        return result

    def safe_install(
        self,
        pod_id: str,
        apk_path: str,
        package_name: str,
    ) -> str:
        """安全安装(含失败处理)

        架构文档 03-第 6.3 节:
        - INSTALL_FAILED_VERSION_DOWNGRADE → 降级安装(-d)
        - INSTALL_FAILED_UPDATE_INCOMPATIBLE → 卸载后重装
        - INSUFFICIENT_STORAGE → 清理缓存重试
        - 其他失败 → 抛出异常
        """
        try:
            return self.install(pod_id, apk_path, replace=True, allow_downgrade=False)
        except InstallError as e:
            err_msg = str(e)

            if "INSTALL_FAILED_VERSION_DOWNGRADE" in err_msg:
                logger.info("版本降级,改用 -d 安装")
                return self.install(pod_id, apk_path, replace=True, allow_downgrade=True)

            if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in err_msg:
                logger.info("签名不兼容,卸载后重装(会清除应用数据)")
                self.uninstall(pod_id, package_name)
                return self.install(pod_id, apk_path, replace=False)

            if "INSUFFICIENT_STORAGE" in err_msg:
                logger.info("存储空间不足,清理 Pod 缓存后重试")
                self.shell(pod_id, f"pm clear {package_name}")
                # 同时清理其他临时文件
                self.shell(pod_id, "rm -rf /sdcard/*.tmp /data/local/tmp/*.apk 2>/dev/null")
                return self.install(pod_id, apk_path, replace=True)

            raise

    def uninstall(self, pod_id: str, package_name: str) -> str:
        """卸载应用"""
        logger.info("卸载应用: %s (pod=%s)", package_name, pod_id)
        result = self.shell(pod_id, f"pm uninstall {package_name}")
        if "Success" not in result:
            raise InstallError(f"卸载失败: {result}")
        return result

    def is_package_installed(self, pod_id: str, package_name: str) -> bool:
        """判断包是否已安装"""
        result = self.shell(pod_id, f"pm list packages {package_name}")
        return f"package:{package_name}" in result

    def get_app_version_name(self, pod_id: str, package_name: str) -> str:
        """获取应用 versionName"""
        result = self.shell(pod_id, f"dumpsys package {package_name} | grep versionName")
        # 示例: versionName=1.2.0
        match = re.search(r"versionName=(\S+)", result)
        return match.group(1) if match else ""

    def get_app_version_code(self, pod_id: str, package_name: str) -> int:
        """获取应用 versionCode"""
        result = self.shell(pod_id, f"dumpsys package {package_name} | grep versionCode")
        match = re.search(r"versionCode=(\d+)", result)
        return int(match.group(1)) if match else 0

    def clear_app_data(self, pod_id: str, package_name: str) -> str:
        """清除应用数据"""
        return self.shell(pod_id, f"pm clear {package_name}")

    # ----------------------------------------------------------------------
    # 设备信息
    # ----------------------------------------------------------------------

    def get_device_info(self, pod_id: str) -> Dict[str, str]:
        """获取设备基础信息"""
        props = {
            "android_version": "ro.build.version.release",
            "sdk_version": "ro.build.version.sdk",
            "model": "ro.product.model",
            "brand": "ro.product.brand",
            "screen_size": "wm size",
            "screen_density": "wm density",
        }
        info = {}
        for key, prop in props.items():
            try:
                if prop.startswith("wm "):
                    result = self.shell(pod_id, prop)
                    # 示例: Physical size: 720x1280
                    if "size:" in result:
                        info[key] = result.split("size:")[-1].strip()
                    elif "density:" in result:
                        info[key] = result.split("density:")[-1].strip()
                else:
                    info[key] = self.shell(pod_id, f"getprop {prop}").strip()
            except ADBError:
                info[key] = ""
        return info

    def list_devices(self) -> List[str]:
        """列出所有已连接设备(含 target)"""
        return self._list_devices()

    # ----------------------------------------------------------------------
    # 内部实现
    # ----------------------------------------------------------------------

    def _run_adb(self, args: List[str], timeout: int = 30) -> str:
        """执行 adb 命令(带重试)"""
        cmd = [self.adb_path] + args
        last_err: Optional[ADBError] = None

        for attempt in range(1, self.retry_attempts + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if proc.returncode != 0:
                    # adb 命令失败但非崩溃,可能是设备未连接等
                    err = proc.stderr.strip() or proc.stdout.strip()
                    if "device not found" in err or "device offline" in err:
                        # 连接丢失,尝试重连一次
                        if attempt < self.retry_attempts:
                            logger.warning("设备未连接,重试(%d/%d): %s", attempt, self.retry_attempts, err)
                            time.sleep(1)
                            continue
                    raise ADBError(f"adb 命令失败({proc.returncode}): {err}")
                return proc.stdout.strip()
            except subprocess.TimeoutExpired as e:
                last_err = ADBError(f"adb 命令超时({timeout}s): {' '.join(args)}")
                logger.warning("adb 超时(%d/%d): %s", attempt, self.retry_attempts, args)
                if attempt < self.retry_attempts:
                    time.sleep(1)
                    continue
                raise last_err from e

        if last_err:
            raise last_err
        raise ADBError("未知 ADB 错误")

    def _list_devices(self) -> List[str]:
        """列出 adb devices 中的设备 target"""
        try:
            output = self._run_adb(["devices"], timeout=self.connect_timeout)
        except ADBError:
            return []
        targets = []
        for line in output.splitlines()[1:]:  # 跳过 "List of devices attached"
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                targets.append(parts[0])
        return targets
