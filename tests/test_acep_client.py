"""src/acep_client.py 单元测试

使用 mock 模拟 volcenginesdkacep / volcenginesdkcore 模块,避免依赖真实 SDK。
"""

from __future__ import annotations

import os
import time
from unittest import mock

import pytest

from src.credentials import Credentials
from src.acep_client import (
    ADBAddress,
    ACEPClient,
    ACEPError,
    ADBKeyNotConfigured,
    DEFAULT_POD_K1,
    DEFAULT_POD_APP_PORTRAIT,
    DEFAULT_PRODUCT_ID,
    DEFAULT_REGION,
    DEFAULT_SHARED_EIP,
    PodStatus,
)


# ============================================================================
# Mock 工厂:构造可注入的 acep / core 模块
# ============================================================================


class FakeRequest:
    """记录构造时的参数,便于断言"""

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def __getattr__(self, name):
        if name == "_kwargs":
            raise AttributeError(name)
        return self._kwargs.get(name)


class FakeResponse:
    """通用响应对象,按属性返回预设值"""

    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)


class FakePod:
    def __init__(self, online=True, intranet_ip="<POD_INTRANET_IP>",
                 eip_address="<YOUR_SHARED_EIP>", name="K1-设备",
                 image_id="<IMAGE_ID>", spec_code="basic.pro",
                 adb="", adb_status=0, adb_expire_time=0):
        self.online = online
        self.intranet_ip = intranet_ip
        self.eip = type("Eip", (), {"eip_address": eip_address})()
        self.pod_name = name
        self.image_id = image_id
        self.server_type_code = spec_code
        self.adb = adb
        self.adb_status = adb_status
        self.adb_expire_time = adb_expire_time


class FakeACEPApi:
    """记录所有调用的 ACEPApi mock"""

    def __init__(self):
        self.calls = []  # [(method_name, request)]
        self.responses = {}  # method_name -> response 或 callable

    def _record(self, method_name):
        def handler(request):
            self.calls.append((method_name, request))
            resp = self.responses.get(method_name)
            if callable(resp):
                return resp(request)
            return resp
        return handler

    def __getattr__(self, name):
        # 避免被 _record 拦截
        if name.startswith("_"):
            raise AttributeError(name)
        return self._record(name)


def build_fake_modules():
    """构造 (acep_module, core_module) 注入对象"""
    fake_api = FakeACEPApi()

    class FakeConfiguration:
        def __init__(self):
            self.ak = None
            self.sk = None
            self.region = None

    class FakeApiClient:
        def __init__(self, cfg):
            self.cfg = cfg

    # 构造一系列 Request 类(接受任意 kwargs)
    request_classes = {}
    # 我们需要在测试中按需补充 Request 类,这里默认创建几个常用的
    request_class_names = [
        "DetailPodRequest", "ListPodRequest",
        "PowerOnPodRequest", "PowerOffPodRequest", "RebootPodRequest",
        "AddAdbKeyRequest", "ListAdbKeyRequest",
        "BindAdbKeyPodsRequest", "UnbindAdbKeyPodsRequest", "PodAdbRequest",
        "RunSyncCommandRequest",
        "UploadAppRequest", "InstallAppRequest", "UninstallAppRequest",
        "LaunchAppRequest", "CloseAppRequest", "ListAppRequest",
        "PushFileRequest",
        "BatchScreenShotRequest", "StartRecordingRequest", "StopRecordingRequest",
        "GetPreSignedEdgeURLRequest",
    ]
    acep_module = type("FakeACEPModule", (), {})()
    for name in request_class_names:
        # 用闭包创建独立的 Request 类
        def make_cls(n):
            class Cls(FakeRequest):
                pass
            Cls.__name__ = n
            return Cls
        setattr(acep_module, name, make_cls(name))
    acep_module.ACEPApi = lambda api_client: fake_api

    core_module = type("FakeCoreModule", (), {})()
    core_module.Configuration = FakeConfiguration
    core_module.ApiClient = FakeApiClient

    return acep_module, core_module, fake_api


# ============================================================================
# PodStatus
# ============================================================================


class TestPodStatus:
    def test_from_pod_full(self):
        pod = FakePod(adb="1.2.3.4:5555", adb_status=1, adb_expire_time=1700000000)
        s = PodStatus.from_pod("pod-123", pod)
        assert s.pod_id == "pod-123"
        assert s.online is True
        assert s.intranet_ip == "<POD_INTRANET_IP>"
        assert s.eip_address == "<YOUR_SHARED_EIP>"
        assert s.name == "K1-设备"
        assert s.image_id == "<IMAGE_ID>"
        assert s.spec_code == "basic.pro"
        assert s.adb == "1.2.3.4:5555"
        assert s.adb_status == 1
        assert s.adb_expire_time == 1700000000

    def test_from_pod_missing_eip(self):
        pod = FakePod()
        pod.eip = None
        s = PodStatus.from_pod("p", pod)
        assert s.eip_address == ""

    def test_from_pod_missing_attributes(self):
        pod = type("Obj", (), {})()  # 无任何属性
        s = PodStatus.from_pod("p", pod)
        assert s.online is False
        assert s.intranet_ip == ""
        assert s.eip_address == ""
        assert s.adb == ""
        assert s.adb_status == 0
        assert s.adb_expire_time == 0


# ============================================================================
# ADBAddress
# ============================================================================


class TestADBAddress:
    def test_not_expired_when_zero(self):
        addr = ADBAddress(pod_id="p", address="1.2.3.4:5555")
        assert addr.is_expired is False

    def test_not_expired_future(self):
        addr = ADBAddress(pod_id="p", address="a", expire_at=time.time() + 100)
        assert addr.is_expired is False

    def test_expired_past(self):
        addr = ADBAddress(pod_id="p", address="a", expire_at=time.time() - 1)
        assert addr.is_expired is True


# ============================================================================
# ACEPClient 构造与默认常量
# ============================================================================


class TestACEPClientDefaults:
    def test_default_constants_match_p5(self):
        assert DEFAULT_REGION == "cn-shanghai"
        assert DEFAULT_PRODUCT_ID == "<YOUR_PRODUCT_ID>"
        assert DEFAULT_POD_K1 == "<K1_POD_ID>"
        assert DEFAULT_POD_APP_PORTRAIT == "<GUARDIAN_PORTRAIT_POD_ID>"
        assert DEFAULT_SHARED_EIP == "<YOUR_SHARED_EIP>"

    def test_construct_with_fake_modules(self):
        acep, core, fake_api = build_fake_modules()
        client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=acep,
            core_module=core,
        )
        assert client.credentials.ak == "AK"
        assert client.region == "cn-shanghai"
        assert client.product_id == DEFAULT_PRODUCT_ID
        # 构造时已实例化 ACEPApi
        assert client._api is fake_api

    def test_snake_case_conversion(self):
        assert ACEPClient._snake_case("DetailPod") == "detail_pod"
        assert ACEPClient._snake_case("PowerOnPod") == "power_on_pod"
        assert ACEPClient._snake_case("PodAdb") == "pod_adb"
        assert ACEPClient._snake_case("GetPreSignedEdgeURL") == "get_pre_signed_edge_url"
        assert ACEPClient._snake_case("AddAdbKey") == "add_adb_key"

    def test_import_acep_raises_when_not_installed(self):
        # 临时模拟 import 失败
        with mock.patch("builtins.__import__", side_effect=ImportError("no module")):
            with pytest.raises(ACEPError, match="未安装 volcenginesdkacep"):
                ACEPClient._import_acep()


# ============================================================================
# 实例管理
# ============================================================================


class TestInstanceManagement:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_detail_pod(self):
        self.fake_api.responses["detail_pod"] = FakeResponse(pod=FakePod())
        status = self.client.detail_pod(DEFAULT_POD_K1)
        assert status.pod_id == DEFAULT_POD_K1
        assert status.online is True
        # 验证调用参数
        method, req = self.fake_api.calls[-1]
        assert method == "detail_pod"
        assert req._kwargs["product_id"] == DEFAULT_PRODUCT_ID
        assert req._kwargs["pod_id"] == DEFAULT_POD_K1

    def test_list_pod(self):
        self.fake_api.responses["list_pod"] = FakeResponse(pods=[FakePod(), FakePod()])
        result = self.client.list_pod(max_results=10)
        assert len(result) == 2
        method, req = self.fake_api.calls[-1]
        assert method == "list_pod"
        assert req._kwargs["max_results"] == 10

    def test_list_pod_accepts_current_sdk_row_field(self):
        first = FakePod()
        first.pod_id = "pod-row-1"
        self.fake_api.responses["list_pod"] = FakeResponse(row=[first])

        result = self.client.list_pod()

        assert [item.pod_id for item in result] == ["pod-row-1"]

    def test_power_on_passes_list(self):
        self.fake_api.responses["power_on_pod"] = FakeResponse()
        self.client.power_on([DEFAULT_POD_K1])
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id_list"] == [DEFAULT_POD_K1]

    def test_power_off_passes_list(self):
        self.fake_api.responses["power_off_pod"] = FakeResponse()
        self.client.power_off(["p1", "p2"])
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id_list"] == ["p1", "p2"]

    def test_reboot(self):
        self.fake_api.responses["reboot_pod"] = FakeResponse()
        self.client.reboot([DEFAULT_POD_K1])
        assert self.fake_api.calls[-1][0] == "reboot_pod"


# ============================================================================
# ADB 管理
# ============================================================================


class TestADBManagement:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_add_adb_key_with_content(self):
        self.fake_api.responses["add_adb_key"] = FakeResponse(key_id="key-001")
        key_id = self.client.add_adb_key(
            key_name="agent-adb-key",
            public_key="ssh-rsa AAAA...",
        )
        assert key_id == "key-001"
        assert self.client._adb_key_id == "key-001"
        # 验证默认 auth_type=1(root), effect_type=1(业务维度)
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["auth_type"] == 1
        assert req._kwargs["effect_type"] == 1

    def test_add_adb_key_with_path(self, tmp_path):
        key_file = tmp_path / "adbkey.pub"
        key_file.write_text("ssh-rsa BBBB...")
        self.fake_api.responses["add_adb_key"] = FakeResponse(key_id="key-002")
        key_id = self.client.add_adb_key(
            key_name="agent-adb-key",
            public_key_path=str(key_file),
            auth_type=2,  # user
            effect_type=2,  # 实例维度
            annotation="test key",
        )
        assert key_id == "key-002"
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["public_key"] == "ssh-rsa BBBB..."
        assert req._kwargs["auth_type"] == 2
        assert req._kwargs["effect_type"] == 2
        assert req._kwargs["annotation"] == "test key"

    def test_add_adb_key_missing_file_raises(self):
        with pytest.raises(ADBKeyNotConfigured, match="ADB 公钥不存在"):
            self.client.add_adb_key(
                key_name="k",
                public_key_path="/nonexistent/key.pub",
            )

    def test_add_adb_key_no_key_id_raises(self):
        self.fake_api.responses["add_adb_key"] = FakeResponse()
        with pytest.raises(ACEPError, match="未返回 key_id"):
            self.client.add_adb_key(key_name="k", public_key="X")

    def test_list_adb_key_caches_first_id(self):
        self.fake_api.responses["list_adb_key"] = FakeResponse(keys=[
            type("K", (), {"key_id": "k1", "key_name": "n1", "public_key": "pk1"})(),
        ])
        result = self.client.list_adb_key()
        assert result[0]["key_id"] == "k1"
        assert self.client._adb_key_id == "k1"

    def test_bind_adb_key_pods_uses_cached_key(self):
        self.fake_api.responses["add_adb_key"] = FakeResponse(key_id="cached-k")
        self.client.add_adb_key(key_name="n", public_key="X")
        self.fake_api.responses["bind_adb_key_pods"] = FakeResponse()
        self.client.bind_adb_key_pods([DEFAULT_POD_K1])
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["key_id"] == "cached-k"
        assert req._kwargs["pod_id_list"] == [DEFAULT_POD_K1]

    def test_bind_adb_key_pods_without_key_raises(self):
        with pytest.raises(ADBKeyNotConfigured, match="未提供 key_id"):
            self.client.bind_adb_key_pods([DEFAULT_POD_K1])

    def test_pod_adb_enable_returns_address(self):
        # pod_adb 开启功能,detail_pod 返回 adb 地址
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="1.2.3.4:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        addr = self.client.pod_adb_enable(DEFAULT_POD_K1)
        assert addr.address == "1.2.3.4:5555"
        assert addr.pod_id == DEFAULT_POD_K1
        assert not addr.is_expired
        # 缓存
        assert self.client._adb_addresses[DEFAULT_POD_K1] is addr

    def test_pod_adb_enable_empty_adb_raises(self):
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="", adb_status=0)
        )
        with pytest.raises(ACEPError, match="detail_pod.adb 为空"):
            self.client.pod_adb_enable(DEFAULT_POD_K1, wait_seconds=0)

    def test_pod_adb_disable_clears_cache(self):
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="1.2.3.4:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        self.client.pod_adb_enable(DEFAULT_POD_K1)
        assert DEFAULT_POD_K1 in self.client._adb_addresses
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.client.pod_adb_disable(DEFAULT_POD_K1)
        assert DEFAULT_POD_K1 not in self.client._adb_addresses

    def test_get_adb_address_cached(self):
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="cached:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        first = self.client.get_adb_address(DEFAULT_POD_K1)
        # 第二次不应调用 API(缓存)
        self.fake_api.calls.clear()
        second = self.client.get_adb_address(DEFAULT_POD_K1)
        assert first is second
        assert self.fake_api.calls == []

    def test_get_adb_address_refresh(self):
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="first:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        self.client.get_adb_address(DEFAULT_POD_K1)
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="second:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        addr = self.client.get_adb_address(DEFAULT_POD_K1, refresh=True)
        assert addr.address == "second:5555"

    def test_get_adb_address_expired_refreshes(self):
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="first:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        addr = self.client.get_adb_address(DEFAULT_POD_K1)
        # 手动设置过期
        addr.expire_at = time.time() - 1
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="renewed:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        new_addr = self.client.get_adb_address(DEFAULT_POD_K1)
        assert new_addr.address == "renewed:5555"

    def test_ensure_adb_connect_command(self):
        self.fake_api.responses["pod_adb"] = FakeResponse()
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(adb="1.2.3.4:5555", adb_status=1, adb_expire_time=int(time.time()) + 3600)
        )
        cmd = self.client.ensure_adb_connect_command(DEFAULT_POD_K1)
        assert cmd == "adb connect 1.2.3.4:5555"


# ============================================================================
# 命令执行
# ============================================================================


class TestCommandExecution:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_run_sync_command(self):
        self.fake_api.responses["run_sync_command"] = FakeResponse(
            status="Success", command="ls", details=[]
        )
        result = self.client.run_sync_command([DEFAULT_POD_K1], "ls")
        assert result["status"] == "Success"
        assert result["command"] == "ls"
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id_list"] == [DEFAULT_POD_K1]
        assert req._kwargs["command"] == "ls"

    def test_run_sync_command_normalizes_sdk_status_list_as_details(self):
        pod_result = {"pod_id": DEFAULT_POD_K1, "success": True, "detail": "10\\n"}
        self.fake_api.responses["run_sync_command"] = FakeResponse(status=[pod_result])

        result = self.client.run_sync_command([DEFAULT_POD_K1], "getprop ro.build.version.release")

        assert result["status"] == [pod_result]
        assert result["details"] == [pod_result]

    def test_run_shell_wraps_single_pod(self):
        self.fake_api.responses["run_sync_command"] = FakeResponse(status="ok")
        self.client.run_shell(DEFAULT_POD_K1, "getprop")
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id_list"] == [DEFAULT_POD_K1]


# ============================================================================
# 应用管理
# ============================================================================


class TestAppManagement:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_upload_app(self):
        self.fake_api.responses["upload_app"] = FakeResponse(
            app_id="app-001", version_id="v-001"
        )
        info = self.client.upload_app("Test App", "https://example.com/a.apk")
        assert info["app_id"] == "app-001"
        assert info["version_id"] == "v-001"
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["app_name"] == "Test App"
        assert req._kwargs["download_url"] == "https://example.com/a.apk"
        assert req._kwargs["app_type"] == "apk"
        assert req._kwargs["parse_flag"] is True

    def test_install_app(self):
        self.fake_api.responses["install_app"] = FakeResponse()
        self.client.install_app([DEFAULT_POD_K1], "app-001", "v-001")
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id_list"] == [DEFAULT_POD_K1]
        assert req._kwargs["app_id"] == "app-001"
        assert req._kwargs["version_id"] == "v-001"

    def test_uninstall_app(self):
        self.fake_api.responses["uninstall_app"] = FakeResponse()
        self.client.uninstall_app([DEFAULT_POD_K1], "app-001")
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["app_id"] == "app-001"

    def test_launch_app(self):
        self.fake_api.responses["launch_app"] = FakeResponse()
        self.client.launch_app([DEFAULT_POD_K1], "com.lumi.guardian")
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["package_name"] == "com.lumi.guardian"

    def test_close_app(self):
        self.fake_api.responses["close_app"] = FakeResponse()
        self.client.close_app([DEFAULT_POD_K1], "com.lumi.guardian")
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["package_name"] == "com.lumi.guardian"

    def test_list_app(self):
        self.fake_api.responses["list_app"] = FakeResponse(apps=[
            type("A", (), {"app_id": "a1", "app_name": "n1", "app_type": "apk"})(),
        ])
        result = self.client.list_app()
        assert result[0]["app_id"] == "a1"

    def test_install_app_from_url(self):
        self.fake_api.responses["upload_app"] = FakeResponse(
            app_id="a1", version_id="v1"
        )
        self.fake_api.responses["install_app"] = FakeResponse()
        info = self.client.install_app_from_url(
            [DEFAULT_POD_K1], "Test", "https://example.com/a.apk"
        )
        assert info["app_id"] == "a1"
        assert info["version_id"] == "v1"
        # 应该调用两次:upload_app 和 install_app
        methods_called = [c[0] for c in self.fake_api.calls]
        assert "upload_app" in methods_called
        assert "install_app" in methods_called

    def test_install_app_from_url_missing_ids_raises(self):
        self.fake_api.responses["upload_app"] = FakeResponse()
        with pytest.raises(ACEPError, match="未返回"):
            self.client.install_app_from_url(
                [DEFAULT_POD_K1], "Test", "https://example.com/a.apk"
            )


# ============================================================================
# 文件推送
# ============================================================================


class TestFilePush:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_push_file(self):
        self.fake_api.responses["push_file"] = FakeResponse()
        self.client.push_file(
            [DEFAULT_POD_K1],
            download_url="https://example.com/data.zip",
            file_name="data.zip",
        )
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["download_url"] == "https://example.com/data.zip"
        assert req._kwargs["file_name"] == "data.zip"
        assert req._kwargs["target_directory"] == "/sdcard/Download/"
        assert req._kwargs["auto_unzip"] is False


# ============================================================================
# 媒体
# ============================================================================


class TestMedia:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_batch_screen_shot(self):
        self.fake_api.responses["batch_screen_shot"] = FakeResponse(details=[
            type("D", (), {
                "pod_id": DEFAULT_POD_K1,
                "path": "/sdcard/ss.png",
                "url": "https://example.com/ss.png",
            })(),
        ])
        result = self.client.batch_screen_shot([DEFAULT_POD_K1])
        assert len(result) == 1
        assert result[0]["pod_id"] == DEFAULT_POD_K1
        assert result[0]["path"] == "/sdcard/ss.png"
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["is_saved_on_pod"] is True

    def test_screenshot_single(self):
        self.fake_api.responses["batch_screen_shot"] = FakeResponse(details=[
            type("D", (), {"pod_id": DEFAULT_POD_K1, "path": "/ss.png", "url": "u"})(),
        ])
        result = self.client.screenshot(DEFAULT_POD_K1)
        assert result["pod_id"] == DEFAULT_POD_K1

    def test_screenshot_empty_returns_empty(self):
        self.fake_api.responses["batch_screen_shot"] = FakeResponse(details=[])
        result = self.client.screenshot(DEFAULT_POD_K1)
        assert result == {}

    def test_start_recording(self):
        self.fake_api.responses["start_recording"] = FakeResponse()
        self.client.start_recording(DEFAULT_POD_K1, duration_limit=30)
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id"] == DEFAULT_POD_K1
        assert req._kwargs["duration_limit"] == 30
        assert req._kwargs["round_id"]  # 自动生成

    def test_start_recording_with_explicit_round_id(self):
        self.fake_api.responses["start_recording"] = FakeResponse()
        self.client.start_recording(
            DEFAULT_POD_K1, duration_limit=15, round_id="my-round-001",
        )
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["round_id"] == "my-round-001"

    def test_stop_recording(self):
        self.fake_api.responses["stop_recording"] = FakeResponse(
            url="https://example.com/rec.mp4"
        )
        result = self.client.stop_recording(DEFAULT_POD_K1)
        assert result["url"] == "https://example.com/rec.mp4"


# ============================================================================
# 屏幕串流
# ============================================================================


class TestStream:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_get_pre_signed_edge_url(self):
        self.fake_api.responses["get_pre_signed_edge_url"] = FakeResponse(
            pre_signed_edge_url="https://edge.example.com/sign/..."
        )
        url = self.client.get_pre_signed_edge_url(DEFAULT_POD_K1, ttl=1800)
        assert url == "https://edge.example.com/sign/..."
        _, req = self.fake_api.calls[-1]
        assert req._kwargs["pod_id"] == DEFAULT_POD_K1
        assert req._kwargs["ttl"] == 1800

    def test_get_pre_signed_edge_url_empty_raises(self):
        self.fake_api.responses["get_pre_signed_edge_url"] = FakeResponse()
        with pytest.raises(ACEPError, match="未返回 URL"):
            self.client.get_pre_signed_edge_url(DEFAULT_POD_K1)


# ============================================================================
# 高级组合
# ============================================================================


class TestHighLevelFlows:
    def setup_method(self):
        self.acep, self.core, self.fake_api = build_fake_modules()
        self.client = ACEPClient(
            credentials=Credentials(ak="AK", sk="SK"),
            sdk_module=self.acep,
            core_module=self.core,
        )

    def test_connect_adb_ensures_powered_on(self):
        # 模拟 Pod 已开机,ADB 已启用返回地址
        self.fake_api.responses["detail_pod"] = FakeResponse(
            pod=FakePod(online=True, adb="1.2.3.4:5555", adb_status=1,
                        adb_expire_time=int(time.time()) + 3600)
        )
        self.fake_api.responses["pod_adb"] = FakeResponse()
        cmd = self.client.connect_adb(DEFAULT_POD_K1)
        assert cmd == "adb connect 1.2.3.4:5555"
        # 应该先查询状态,再启用 ADB
        methods = [c[0] for c in self.fake_api.calls]
        assert "detail_pod" in methods
        assert "pod_adb" in methods

    def test_install_apk_from_url_with_launch(self):
        self.fake_api.responses["detail_pod"] = FakeResponse(pod=FakePod(online=True))
        self.fake_api.responses["upload_app"] = FakeResponse(app_id="a1", version_id="v1")
        self.fake_api.responses["install_app"] = FakeResponse()
        self.fake_api.responses["launch_app"] = FakeResponse()
        result = self.client.install_apk_from_url(
            [DEFAULT_POD_K1],
            app_name="Test",
            download_url="https://example.com/a.apk",
            package_name="com.lumi.guardian",
            launch=True,
        )
        assert result["app_id"] == "a1"
        assert result["installed"] is True
        assert result["launched"] is True
        # 验证 launch_app 被调用
        methods = [c[0] for c in self.fake_api.calls]
        assert "launch_app" in methods

    def test_install_apk_from_url_launch_without_package_raises(self):
        self.fake_api.responses["detail_pod"] = FakeResponse(pod=FakePod(online=True))
        self.fake_api.responses["upload_app"] = FakeResponse(app_id="a1", version_id="v1")
        self.fake_api.responses["install_app"] = FakeResponse()
        with pytest.raises(ACEPError, match="launch=True 需要"):
            self.client.install_apk_from_url(
                [DEFAULT_POD_K1],
                app_name="Test",
                download_url="https://example.com/a.apk",
                launch=True,
            )
