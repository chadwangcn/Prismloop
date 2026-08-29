"""火山引擎云手机 ACEP OpenAPI 客户端封装

依据架构文档 `architecture/02-云手机与外设模拟.md` 第十章:
- 封装官方 `volcenginesdkacep` Python SDK
- 凭证从 macOS Keychain 读取(经 src/credentials.py)
- 覆盖:实例管理、ADB 管理、应用安装、文件推送、截屏录屏、屏幕串流
- 不含事件级输入(sendKeyCode/sendMouseKey),事件级输入走 ADB input 或可选的 SDK Bridge

参考:
- 云手机 API 概览:https://docs.volcengine.com/docs/6394/75747
- 应用管理:https://docs.volcengine.com/docs/6394/1223958
- 操作指南:docs/runbooks/p5/cloud-phone-test-agent-guide.md
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .credentials import Credentials, load_credentials

logger = logging.getLogger(__name__)


# ============================================================================
# 默认配置(与 P5 环境一致)
# ============================================================================


DEFAULT_REGION = "cn-shanghai"
DEFAULT_PRODUCT_ID = "<YOUR_PRODUCT_ID>"

# P5 环境 4 台 Pod
DEFAULT_POD_K1 = "<K1_POD_ID>"
DEFAULT_POD_K1_BACKUP = "<K1_BACKUP_POD_ID>"
DEFAULT_POD_APP_LANDSCAPE = "<GUARDIAN_LANDSCAPE_POD_ID>"
DEFAULT_POD_APP_PORTRAIT = "<GUARDIAN_PORTRAIT_POD_ID>"

DEFAULT_SHARED_EIP = "<YOUR_SHARED_EIP>"


# ============================================================================
# 异常定义
# ============================================================================


class ACEPError(Exception):
    """ACEP API 调用异常"""


class ADBKeyNotConfigured(ACEPError):
    """ADB Key 未配置"""


# ============================================================================
# 数据类
# ============================================================================


@dataclass
class PodStatus:
    """Pod 实例状态(ACEP detail_pod 返回)"""

    pod_id: str
    online: bool
    intranet_ip: str
    eip_address: str
    name: str = ""
    image_id: str = ""
    spec_code: str = ""
    # ADB 相关(启用 ADB 后才有值)
    adb: str = ""  # ADB 连接地址,格式 ip:port
    adb_status: int = 0  # 0=关闭, 1=开启
    adb_expire_time: int = 0  # Unix 时间戳,ADB 过期时间

    @classmethod
    def from_pod(cls, pod_id: str, pod_obj: Any) -> "PodStatus":
        """从 ACEP pod 对象构造(兼容属性缺失)"""
        eip_obj = getattr(pod_obj, "eip", None)
        return cls(
            pod_id=pod_id,
            online=bool(getattr(pod_obj, "online", False)),
            intranet_ip=getattr(pod_obj, "intranet_ip", "") or "",
            eip_address=getattr(eip_obj, "eip_address", "") or "" if eip_obj else "",
            name=getattr(pod_obj, "pod_name", "") or getattr(pod_obj, "name", "") or "",
            image_id=getattr(pod_obj, "image_id", "") or "",
            spec_code=getattr(pod_obj, "server_type_code", "") or getattr(pod_obj, "spec_code", "") or "",
            adb=getattr(pod_obj, "adb", "") or "",
            adb_status=int(getattr(pod_obj, "adb_status", 0) or 0),
            adb_expire_time=int(getattr(pod_obj, "adb_expire_time", 0) or 0),
        )


@dataclass
class ADBAddress:
    """ADB 连接地址(由 pod_adb(enable=True) 返回)"""

    pod_id: str
    address: str  # ip:port
    expire_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return self.expire_at > 0 and time.time() >= self.expire_at


# ============================================================================
# ACEP 客户端
# ============================================================================


class ACEPClient:
    """火山引擎云手机 ACEP OpenAPI 客户端

    封装官方 `volcenginesdkacep` SDK,提供:
    - 实例管理:detail_pod / list_pod / power_on / power_off / reboot
    - ADB 管理:add_adb_key / bind_adb_key_pods / pod_adb
    - 应用管理:upload_app / install_app / uninstall_app / launch_app / close_app / list_app
    - 命令执行:run_sync_command
    - 文件推送:push_file
    - 媒体:batch_screen_shot / start_recording / stop_recording
    - 串流:get_pre_signed_edge_url
    """

    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        region: str = DEFAULT_REGION,
        product_id: str = DEFAULT_PRODUCT_ID,
        sdk_module: Any = None,
        core_module: Any = None,
    ):
        """构造 ACEP 客户端

        Args:
            credentials: 凭证(为 None 时自动从 Keychain/环境变量加载)
            region: 地域(默认 cn-shanghai)
            product_id: 云手机产品 ID(默认 P5 环境)
            sdk_module: 可选,注入 volcenginesdkacep 模块(便于测试)
            core_module: 可选,注入 volcenginesdkcore 模块(便于测试)
        """
        self.credentials = credentials or load_credentials()
        self.region = region
        self.product_id = product_id

        # 延迟导入,允许未安装 SDK 时仍可加载本模块(测试时注入)
        if sdk_module is None:
            sdk_module = self._import_acep()
        if core_module is None:
            core_module = self._import_core()
        self._acep = sdk_module
        self._core = core_module

        # 构造 ApiClient
        cfg = core_module.Configuration()
        cfg.ak = self.credentials.ak
        cfg.sk = self.credentials.sk
        cfg.region = region
        self._api = sdk_module.ACEPApi(core_module.ApiClient(cfg))

        # ADB 连接地址缓存:{ pod_id: ADBAddress }
        self._adb_addresses: Dict[str, ADBAddress] = {}
        # ADB Key ID 缓存(添加后保存,避免重复添加)
        self._adb_key_id: Optional[str] = None

    # ----------------------------------------------------------------------
    # 延迟导入(便于测试 mock)
    # ----------------------------------------------------------------------

    @staticmethod
    def _import_acep() -> Any:
        try:
            import volcenginesdkacep as acep  # type: ignore
            return acep
        except ImportError as e:
            raise ACEPError(
                "未安装 volcenginesdkacep,请执行: pip install volcengine-python-sdk"
            ) from e

    @staticmethod
    def _import_core() -> Any:
        try:
            import volcenginesdkcore as core  # type: ignore
            return core
        except ImportError as e:
            raise ACEPError(
                "未安装 volcenginesdkcore,请执行: pip install volcengine-python-sdk"
            ) from e

    # ----------------------------------------------------------------------
    # 内部:请求构造
    # ----------------------------------------------------------------------

    def _req(self, request_class_name: str, **kwargs: Any) -> Any:
        """构造 ACEP Request 对象并调用同名 API 方法

        Args:
            request_class_name: Request 类名(如 "DetailPodRequest")
            **kwargs: 请求字段(不含 product_id,自动填充)

        Returns:
            API 响应对象
        """
        request_cls = getattr(self._acep, request_class_name, None)
        if request_cls is None:
            raise ACEPError(f"ACEP 模块缺少 {request_class_name}")

        # 自动填充 product_id(除非调用方显式传入)
        kwargs.setdefault("product_id", self.product_id)
        try:
            request = request_cls(**kwargs)
        except TypeError as e:
            raise ACEPError(
                f"构造 {request_class_name} 失败: {e}。传入字段: {list(kwargs.keys())}"
            ) from e

        # 调用同名 API 方法(如 detail_pod)
        method_name = self._snake_case(request_class_name.replace("Request", ""))
        method = getattr(self._api, method_name, None)
        if method is None:
            raise ACEPError(f"ACEPApi 缺少方法 {method_name}")

        try:
            return method(request)
        except Exception as e:
            raise ACEPError(f"调用 {method_name} 失败: {e}") from e

    @staticmethod
    def _snake_case(name: str) -> str:
        """DetailPod -> detail_pod

        处理连续大写字母(缩写词)作为整体,如 URL -> url,ADB -> adb。
        规则:
        - 大写字母后跟小写字母:在大写前加下划线(URLRequest -> url_request)
        - 连续大写字母:作为整体小写
        """
        if not name:
            return name
        result = []
        for i, ch in enumerate(name):
            if ch.isupper():
                # 判断是否需要在前面加下划线
                prev_lower = i > 0 and name[i - 1].islower()
                next_lower = i + 1 < len(name) and name[i + 1].islower()
                if prev_lower or (i > 0 and next_lower):
                    result.append("_")
                result.append(ch.lower())
            else:
                result.append(ch)
        return "".join(result)

    # ----------------------------------------------------------------------
    # 实例管理
    # ----------------------------------------------------------------------

    def detail_pod(self, pod_id: str) -> PodStatus:
        """查询实例详情"""
        resp = self._req("DetailPodRequest", pod_id=pod_id)
        pod_obj = getattr(resp, "pod", resp)
        return PodStatus.from_pod(pod_id, pod_obj)

    def list_pod(self, max_results: int = 50) -> List[PodStatus]:
        """列出所有 Pod，兼容 SDK 的 ``row`` / ``pods`` 列表字段。"""
        resp = self._req("ListPodRequest", max_results=max_results)
        pods = getattr(resp, "row", None)
        if pods is None:
            pods = getattr(resp, "pods", [])
        pods = pods or []
        return [PodStatus.from_pod(getattr(p, "pod_id", ""), p) for p in pods]

    def power_on(self, pod_ids: List[str]) -> Any:
        """开机(注意:pod_id_list 是列表)"""
        return self._req("PowerOnPodRequest", pod_id_list=pod_ids)

    def power_off(self, pod_ids: List[str]) -> Any:
        """关机"""
        return self._req("PowerOffPodRequest", pod_id_list=pod_ids)

    def reboot(self, pod_ids: List[str]) -> Any:
        """重启"""
        return self._req("RebootPodRequest", pod_id_list=pod_ids)

    # ----------------------------------------------------------------------
    # ADB 管理
    # ----------------------------------------------------------------------

    def add_adb_key(
        self,
        key_name: str,
        public_key: Optional[str] = None,
        public_key_path: Optional[str] = None,
        auth_type: int = 1,
        effect_type: int = 1,
        annotation: str = "",
    ) -> str:
        """添加 ADB 公钥到云手机产品(只需一次),返回 key_id

        Args:
            key_name: Key 名称(如 "agent-adb-key"),支持中英文数字,<=128 字符
            public_key: 公钥内容(与 public_key_path 二选一)
            public_key_path: 公钥文件路径(默认 ~/.android/adbkey.pub)
            auth_type: 权限类型,1=root(默认),2=user
            effect_type: 生效范围,1=业务维度(默认),2=实例维度
            annotation: 备注(可选,<=256 字符)
        """
        if public_key is None:
            path = public_key_path or os.path.expanduser("~/.android/adbkey.pub")
            if not os.path.exists(path):
                raise ADBKeyNotConfigured(
                    f"ADB 公钥不存在: {path}。请先执行 ssh-keygen 生成 ADB 密钥。"
                )
            with open(path, "r", encoding="utf-8") as f:
                public_key = f.read().strip()

        kwargs = {
            "key_name": key_name,
            "public_key": public_key,
            "auth_type": auth_type,
            "effect_type": effect_type,
        }
        if annotation:
            kwargs["annotation"] = annotation

        resp = self._req("AddAdbKeyRequest", **kwargs)
        key_id = getattr(resp, "key_id", None) or getattr(resp, "id", None)
        if not key_id:
            raise ACEPError("add_adb_key 未返回 key_id")
        self._adb_key_id = str(key_id)
        logger.info("ADB Key 已添加: %s (key_id=%s)", key_name, key_id)
        return self._adb_key_id

    def list_adb_key(self) -> List[Dict[str, Any]]:
        """列出已添加的 ADB Key"""
        resp = self._req("ListAdbKeyRequest")
        keys = getattr(resp, "keys", []) or []
        result = []
        for k in keys:
            result.append({
                "key_id": getattr(k, "key_id", "") or "",
                "key_name": getattr(k, "key_name", "") or "",
                "public_key": getattr(k, "public_key", "") or "",
            })
            # 缓存第一个 key_id
            if not self._adb_key_id and result[-1]["key_id"]:
                self._adb_key_id = result[-1]["key_id"]
        return result

    def bind_adb_key_pods(
        self,
        pod_ids: List[str],
        key_id: Optional[str] = None,
    ) -> Any:
        """绑定 ADB Key 到 Pod"""
        key_id = key_id or self._adb_key_id
        if not key_id:
            raise ADBKeyNotConfigured(
                "未提供 key_id 且未缓存,请先调用 add_adb_key 或 list_adb_key"
            )
        return self._req(
            "BindAdbKeyPodsRequest",
            key_id=key_id,
            pod_id_list=pod_ids,
        )

    def pod_adb_enable(self, pod_id: str, wait_seconds: int = 30) -> ADBAddress:
        """开启 Pod ADB,返回连接地址

        PodAdb API 仅切换开关,不返回地址。实际地址从 detail_pod 的 adb 字段获取,
        格式 ip:port,本地执行 `adb connect <address>` 即可。

        注意:PodAdb 调用后地址异步生效,需要轮询 detail_pod 等待 adb 字段出现。
        """
        # 1. 调用 PodAdb(enable=True) 开启功能
        self._req("PodAdbRequest", pod_id=pod_id, enable=True)

        # 2. 轮询查询 detail_pod 获取 ADB 地址(异步延迟)
        status = self.detail_pod(pod_id)
        if not status.adb:
            logger.info("ADB 地址异步未就绪,轮询等待(最多 %ds)...", wait_seconds)
            for i in range((max(0, wait_seconds) + 1) // 2):
                time.sleep(2)
                status = self.detail_pod(pod_id)
                logger.debug("  [%ds] adb=%r, adb_status=%s", (i + 1) * 2, status.adb, status.adb_status)
                if status.adb:
                    break
        if not status.adb:
            raise ACEPError(
                f"pod_adb(enable=True) 已调用,但轮询 {wait_seconds}s 后 detail_pod.adb 为空。"
                f"adb_status={status.adb_status}"
            )
        adb_addr = ADBAddress(
            pod_id=pod_id,
            address=status.adb,
            expire_at=float(status.adb_expire_time) if status.adb_expire_time else time.time() + 86400,
        )
        self._adb_addresses[pod_id] = adb_addr
        logger.info("Pod %s ADB 已启用: %s", pod_id, adb_addr.address)
        return adb_addr

    def pod_adb_disable(self, pod_id: str) -> Any:
        """关闭 Pod ADB"""
        self._adb_addresses.pop(pod_id, None)
        return self._req("PodAdbRequest", pod_id=pod_id, enable=False)

    def get_adb_address(self, pod_id: str, refresh: bool = False) -> ADBAddress:
        """获取 Pod 的 ADB 地址,过期或 refresh 时自动重新启用

        Args:
            pod_id: Pod ID
            refresh: 强制重新启用(忽略缓存)
        """
        addr = self._adb_addresses.get(pod_id)
        if addr and not addr.is_expired and not refresh:
            return addr
        return self.pod_adb_enable(pod_id)

    def ensure_adb_connect_command(self, pod_id: str) -> str:
        """获取 `adb connect <address>` 命令字符串"""
        addr = self.get_adb_address(pod_id)
        return f"adb connect {addr.address}"

    # ----------------------------------------------------------------------
    # 命令执行
    # ----------------------------------------------------------------------

    def run_sync_command(
        self,
        pod_ids: List[str],
        command: str,
    ) -> Dict[str, Any]:
        """同步执行 Shell 命令

        Args:
            pod_ids: 目标 Pod ID 列表(注意是列表)
            command: Shell 命令

        Returns:
            {"status": ..., "command": ..., "details": ...}
        """
        resp = self._req(
            "RunSyncCommandRequest",
            pod_id_list=pod_ids,
            command=command,
        )
        status = getattr(resp, "status", "")
        details = getattr(resp, "details", None)
        # The ACEP SDK currently returns per-Pod command results in ``status``
        # (a list), rather than in ``details``.  Preserve the scalar status
        # used by older SDK responses and normalize the per-Pod results for
        # callers through ``details``.
        if details is None:
            details = status if isinstance(status, list) else []

        return {
            "status": status,
            "command": getattr(resp, "command", command),
            "details": details,
        }

    def run_shell(self, pod_id: str, command: str) -> Dict[str, Any]:
        """便捷方法:在单个 Pod 上执行 Shell 命令"""
        return self.run_sync_command([pod_id], command)

    # ----------------------------------------------------------------------
    # 应用管理
    # ----------------------------------------------------------------------

    def upload_app(
        self,
        app_name: str,
        download_url: str,
        app_type: str = "apk",
        parse_flag: bool = True,
    ) -> Dict[str, Any]:
        """注册应用(提供 APK 下载 URL)

        云手机通过 URL 分发应用,不支持本地文件直传。
        APK 需先上传到可下载地址(TOS/CDN)。
        """
        resp = self._req(
            "UploadAppRequest",
            app_name=app_name,
            app_type=app_type,
            download_url=download_url,
            parse_flag=parse_flag,
        )
        return {
            "app_id": getattr(resp, "app_id", "") or "",
            "version_id": getattr(resp, "version_id", "") or "",
        }

    def install_app(
        self,
        pod_ids: List[str],
        app_id: str,
        version_id: str,
    ) -> Any:
        """安装应用到指定 Pod"""
        return self._req(
            "InstallAppRequest",
            pod_id_list=pod_ids,
            app_id=app_id,
            version_id=version_id,
        )

    def uninstall_app(
        self,
        pod_ids: List[str],
        app_id: str,
    ) -> Any:
        """卸载应用"""
        return self._req(
            "UninstallAppRequest",
            pod_id_list=pod_ids,
            app_id=app_id,
        )

    def launch_app(
        self,
        pod_ids: List[str],
        package_name: str,
    ) -> Any:
        """启动应用"""
        return self._req(
            "LaunchAppRequest",
            pod_id_list=pod_ids,
            package_name=package_name,
        )

    def close_app(
        self,
        pod_ids: List[str],
        package_name: str,
    ) -> Any:
        """关闭应用"""
        return self._req(
            "CloseAppRequest",
            pod_id_list=pod_ids,
            package_name=package_name,
        )

    def list_app(self, max_results: int = 50) -> List[Dict[str, Any]]:
        """查询已上传的应用"""
        resp = self._req("ListAppRequest", max_results=max_results)
        apps = getattr(resp, "apps", []) or []
        return [
            {
                "app_id": getattr(a, "app_id", "") or "",
                "app_name": getattr(a, "app_name", "") or "",
                "app_type": getattr(a, "app_type", "") or "",
            }
            for a in apps
        ]

    def install_app_from_url(
        self,
        pod_ids: List[str],
        app_name: str,
        download_url: str,
    ) -> Dict[str, Any]:
        """一站式:上传应用并安装到指定 Pod

        Args:
            pod_ids: 目标 Pod ID 列表
            app_name: 应用名称
            download_url: APK 下载 URL

        Returns:
            {"app_id": ..., "version_id": ...}
        """
        info = self.upload_app(app_name, download_url)
        if not info["app_id"] or not info["version_id"]:
            raise ACEPError(f"upload_app 未返回 app_id/version_id: {info}")
        self.install_app(pod_ids, info["app_id"], info["version_id"])
        return info

    # ----------------------------------------------------------------------
    # 文件推送
    # ----------------------------------------------------------------------

    def push_file(
        self,
        pod_ids: List[str],
        download_url: str,
        file_name: str,
        target_directory: str = "/sdcard/Download/",
        auto_unzip: bool = False,
    ) -> Any:
        """推送文件到云手机(URL 分发,不支持本地直传)"""
        return self._req(
            "PushFileRequest",
            pod_id_list=pod_ids,
            download_url=download_url,
            file_name=file_name,
            target_directory=target_directory,
            auto_unzip=auto_unzip,
        )

    # ----------------------------------------------------------------------
    # 媒体(截屏 / 录屏)
    # ----------------------------------------------------------------------

    def batch_screen_shot(
        self,
        pod_ids: List[str],
        is_saved_on_pod: bool = True,
    ) -> List[Dict[str, Any]]:
        """批量截屏

        Args:
            pod_ids: 目标 Pod ID 列表
            is_saved_on_pod: 是否保存到 Pod 本地(之后用 pull_file 拉取)

        Returns:
            [{"pod_id": ..., "path": ..., "url": ...}]
        """
        resp = self._req(
            "BatchScreenShotRequest",
            pod_id_list=pod_ids,
            is_saved_on_pod=is_saved_on_pod,
        )
        details = getattr(resp, "details", []) or []
        result = []
        for d in details:
            result.append({
                "pod_id": getattr(d, "pod_id", "") or "",
                "path": getattr(d, "path", "") or getattr(d, "file_path", "") or "",
                "url": getattr(d, "url", "") or getattr(d, "download_url", "") or "",
            })
        return result

    def screenshot(self, pod_id: str) -> Dict[str, Any]:
        """便捷方法:单个 Pod 截屏"""
        results = self.batch_screen_shot([pod_id])
        return results[0] if results else {}

    def start_recording(
        self,
        pod_id: str,
        duration_limit: int = 60,
        round_id: Optional[str] = None,
    ) -> Any:
        """开始录屏

        Args:
            pod_id: 目标 Pod ID(单个,非列表)
            duration_limit: 最大录制时长(秒)
            round_id: 录屏唯一标识(必填,5 分钟内不可重复)。
                      不传则自动生成 `rec-<timestamp>`。
        """
        if round_id is None:
            round_id = f"rec-{int(time.time())}"
        return self._req(
            "StartRecordingRequest",
            pod_id=pod_id,
            duration_limit=duration_limit,
            round_id=round_id,
        )

    def stop_recording(self, pod_id: str) -> Dict[str, Any]:
        """停止录屏"""
        resp = self._req("StopRecordingRequest", pod_id=pod_id)
        return {
            "url": getattr(resp, "url", "") or getattr(resp, "download_url", "") or "",
            "path": getattr(resp, "path", "") or getattr(resp, "file_path", "") or "",
        }

    # ----------------------------------------------------------------------
    # 屏幕串流(投屏)
    # ----------------------------------------------------------------------

    def get_pre_signed_edge_url(
        self,
        pod_id: str,
        ttl: int = 3600,
    ) -> str:
        """获取屏幕串流 URL(投屏)

        Args:
            pod_id: 目标 Pod ID
            ttl: URL 有效期(秒)

        Returns:
            预签名串流 URL
        """
        resp = self._req(
            "GetPreSignedEdgeURLRequest",
            pod_id=pod_id,
            ttl=ttl,
        )
        url = getattr(resp, "pre_signed_edge_url", "") or getattr(resp, "url", "") or ""
        if not url:
            raise ACEPError("get_pre_signed_edge_url 未返回 URL")
        return url

    # ----------------------------------------------------------------------
    # 高级组合
    # ----------------------------------------------------------------------

    def ensure_pods_powered_on(self, pod_ids: List[str]) -> None:
        """确保多个 Pod 已开机"""
        statuses = []
        for pod_id in pod_ids:
            try:
                statuses.append((pod_id, self.detail_pod(pod_id)))
            except ACEPError as e:
                logger.warning("查询 Pod %s 状态失败: %s", pod_id, e)
                statuses.append((pod_id, None))

        offline = [pid for pid, s in statuses if s is None or not s.online]
        if offline:
            logger.info("开机: %s", offline)
            self.power_on(offline)

            # 轮询等待开机完成
            deadline = time.time() + 120
            while time.time() < deadline and offline:
                time.sleep(5)
                still_offline = []
                for pid in offline:
                    try:
                        s = self.detail_pod(pid)
                        if s.online:
                            logger.info("Pod %s 已开机", pid)
                        else:
                            still_offline.append(pid)
                    except ACEPError:
                        still_offline.append(pid)
                offline = still_offline
                if not offline:
                    break
            if offline:
                raise ACEPError(f"Pod 开机超时: {offline}")

    def connect_adb(self, pod_id: str) -> str:
        """便捷方法:确保 Pod 已开机 → 启用 ADB → 返回 adb connect 命令

        Returns:
            adb connect 命令字符串,本地执行即可
        """
        self.ensure_pods_powered_on([pod_id])
        addr = self.get_adb_address(pod_id)
        return f"adb connect {addr.address}"

    def install_apk_from_url(
        self,
        pod_ids: List[str],
        app_name: str,
        download_url: str,
        package_name: Optional[str] = None,
        launch: bool = False,
    ) -> Dict[str, Any]:
        """一站式:开机 → 上传应用 → 安装 → (可选)启动

        Args:
            pod_ids: 目标 Pod ID 列表
            app_name: 应用名称
            download_url: APK 下载 URL
            package_name: 包名(launch=True 时必填)
            launch: 是否启动应用

        Returns:
            {"app_id": ..., "version_id": ..., "installed": True, "launched": bool}
        """
        self.ensure_pods_powered_on(pod_ids)
        info = self.install_app_from_url(pod_ids, app_name, download_url)
        result = {**info, "installed": True, "launched": False}
        if launch:
            if not package_name:
                raise ACEPError("launch=True 需要提供 package_name")
            self.launch_app(pod_ids, package_name)
            result["launched"] = True
        return result
