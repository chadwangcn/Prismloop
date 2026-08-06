"""配置加载与客户端工厂

依据架构文档:
- `02-云手机与外设模拟.md` 第十章:ACEP OpenAPI 接入方式
- `01-MUA-OpenAPI集成方案.md` 第十章:配置文件规范与加载

支持两种凭证来源:
1. macOS Keychain(推荐,通过 `credentials_source: keychain`)
2. 环境变量 VOLC_AK/VOLC_SK(CI/容器环境)
3. 配置文件直接写入(仅用于本地测试,不推荐)

向后兼容:
- 旧版 `mua.ak`/`mua.sk` 仍可用,但若为占位符则报错
- 新版 `acep.credentials_source: keychain` 优先
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .adb_client import ADBClient
from .credentials import Credentials, load_credentials
from .mua_client import MUAClient
from .pod_pool import PodPool
from .sdk_client import SDKClient

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class ConfigError(Exception):
    """配置错误"""


# ============================================================================
# 配置数据类
# ============================================================================


@dataclass
class AppConfig:
    """全局配置(已验证)"""

    raw: Dict[str, Any]
    acep: Dict[str, Any]
    mua: Dict[str, Any]
    pods: Dict[str, Any]
    tos: Dict[str, Any]
    adb: Dict[str, Any]
    webhook: Dict[str, Any]
    sdk_bridge: Dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AppConfig":
        return cls(
            raw=raw,
            acep=raw.get("acep", {}),
            mua=raw.get("mua", {}),
            pods=raw.get("pods", {}),
            tos=raw.get("tos", {}),
            adb=raw.get("adb", {}),
            webhook=raw.get("webhook", {}),
            sdk_bridge=raw.get("sdk_bridge", {}),
        )

    @property
    def product_id(self) -> str:
        """云手机产品 ID(ACEP)"""
        return self.acep.get("product_id", "")

    @property
    def region(self) -> str:
        return self.acep.get("region", "cn-shanghai")


# ============================================================================
# 配置加载
# ============================================================================


def load_config(env_path: str = "config/env.local.json") -> AppConfig:
    """加载环境配置

    优先查找 env.local.json,其次 env.template.json(模板含真实 Pod ID 但不含 AK/SK)。
    """
    candidates = [
        env_path,
        os.path.join(os.getcwd(), env_path),
        os.path.join(os.getcwd(), "config/env.template.json"),
    ]
    found_path = None
    for path in candidates:
        if os.path.exists(path):
            found_path = path
            break
    if found_path is None:
        raise FileNotFoundError(
            f"配置文件不存在: {env_path}\n"
            "请复制 config/env.template.json 为 config/env.local.json 并填入凭证。"
        )

    with open(found_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    config = AppConfig.from_dict(raw)
    _validate_config(config, is_template=found_path.endswith("env.template.json"))
    return config


def _validate_config(config: AppConfig, is_template: bool = False) -> None:
    """校验配置必填项

    Args:
        config: 应用配置
        is_template: 是否从模板加载(模板含真实 Pod ID 但 AK/SK 为占位符)
    """
    # ACEP 必填项
    required_acep = [
        ("acep.region", config.acep.get("region")),
        ("acep.product_id", config.acep.get("product_id")),
    ]
    for key, value in required_acep:
        if not value or (isinstance(value, str) and value.startswith("<")):
            raise ConfigError(f"缺少必填配置或仍为占位符: {key}")

    # Pod 必填项
    required_pods = [
        ("pods.guardian.pod_id", config.pods.get("guardian", {}).get("pod_id")),
        ("pods.k1.pod_id", config.pods.get("k1", {}).get("pod_id")),
    ]
    for key, value in required_pods:
        if not value or (isinstance(value, str) and value.startswith("<")):
            raise ConfigError(f"缺少必填配置或仍为占位符: {key}")

    # TOS 必填项
    if not config.tos.get("bucket") or (
        isinstance(config.tos.get("bucket"), str) and config.tos["bucket"].startswith("<")
    ):
        raise ConfigError("缺少必填配置: tos.bucket")

    # AK/SK:从模板加载时允许占位符(凭证走 Keychain/环境变量)
    if not is_template:
        creds_source = config.acep.get("credentials_source", "keychain")
        if creds_source == "config":
            # 显式指定从配置文件读取 AK/SK(不推荐)
            ak = config.mua.get("ak", "")
            sk = config.mua.get("sk", "")
            if not ak or ak.startswith("<") or not sk or sk.startswith("<"):
                raise ConfigError(
                    "acep.credentials_source=config 但 mua.ak/sk 为空或占位符"
                )


# ============================================================================
# 凭证加载
# ============================================================================


def load_credentials_from_config(config: AppConfig) -> Credentials:
    """根据配置加载凭证

    策略:
    1. 若 acep.credentials_source=keychain(默认),从 Keychain 读取
    2. 若 =env,从环境变量读取
    3. 若 =config,从 mua.ak/sk 读取
    4. 若缺失,自动尝试 Keychain → 环境变量
    """
    source = config.acep.get("credentials_source", "keychain")

    if source == "config":
        ak = config.mua.get("ak", "")
        sk = config.mua.get("sk", "")
        if not ak or not sk or ak.startswith("<") or sk.startswith("<"):
            raise ConfigError("credentials_source=config 但 AK/SK 未配置")
        return Credentials(ak=ak, sk=sk)

    if source == "env":
        ak = os.environ.get("VOLC_AK", "").strip()
        sk = os.environ.get("VOLC_SK", "").strip()
        if not ak or not sk:
            raise ConfigError("credentials_source=env 但 VOLC_AK/VOLC_SK 未设置")
        return Credentials(ak=ak, sk=sk)

    if source == "keychain":
        ak_service = config.acep.get(
            "ak_keychain_service", "<YOUR_AK_KEYCHAIN_SERVICE>"
        )
        sk_service = config.acep.get(
            "sk_keychain_service", "<YOUR_SK_KEYCHAIN_SERVICE>"
        )
        return load_credentials(
            ak_service=ak_service, sk_service=sk_service, allow_env=True
        )

    raise ConfigError(f"未知的 credentials_source: {source}")


# ============================================================================
# 客户端工厂
# ============================================================================


def create_acep_client(
    config: AppConfig,
    credentials: Optional[Credentials] = None,
    sdk_module: Any = None,
    core_module: Any = None,
):
    """构造 ACEP Client

    Args:
        config: 应用配置
        credentials: 可选,凭证(为 None 时按配置自动加载)
        sdk_module: 可选,注入 volcenginesdkacep 模块(测试用)
        core_module: 可选,注入 volcenginesdkcore 模块(测试用)
    """
    from .acep_client import ACEPClient

    if credentials is None:
        credentials = load_credentials_from_config(config)
    return ACEPClient(
        credentials=credentials,
        region=config.region,
        product_id=config.product_id,
        sdk_module=sdk_module,
        core_module=core_module,
    )


def create_mua_client(
    config: AppConfig,
    credentials: Optional[Credentials] = None,
) -> MUAClient:
    """构造 MUA Client

    若未单独提供 MUA 的 AK/SK,复用 ACEP 的凭证(同一组 AK/SK 可调用多服务)。
    """
    mua = config.mua
    if credentials is None:
        # MUA 凭证优先级:config.mua.ak/sk > Keychain/环境变量
        ak = mua.get("ak", "")
        sk = mua.get("sk", "")
        if ak and sk and not ak.startswith("<") and not sk.startswith("<"):
            credentials = Credentials(ak=ak, sk=sk)
        else:
            credentials = load_credentials_from_config(config)
    return MUAClient(
        ak=credentials.ak,
        sk=credentials.sk,
        region=mua.get("region", "cn-north-1"),
        service=mua.get("service", "ipaas"),
        version=mua.get("version", "2023-08-01"),
        product_id=mua.get("product_id"),
    )


def create_adb_client(config: AppConfig, acep_client: Any = None) -> ADBClient:
    """构造 ADB Client

    若提供 acep_client,则从 ACEP 动态获取 ADB 地址;否则从 config.adb 读取静态配置。

    Args:
        config: 应用配置
        acep_client: 可选,ACEP 客户端(用于动态获取 ADB 地址)
    """
    client = ADBClient(
        connect_timeout=config.adb.get("connect_timeout", 10),
        command_timeout=config.adb.get("command_timeout", 30),
    )

    # 若提供 ACEP 客户端,所有 Pod 的 ADB 地址由 ACEP 动态获取
    # 否则从 config.adb 读取静态 EIP/Port
    for role_key in ("guardian", "guardian_backup", "k1", "k1_backup"):
        pod_cfg = config.pods.get(role_key, {})
        pod_id = pod_cfg.get("pod_id")
        if not pod_id:
            continue

        if acep_client is not None:
            # 动态获取地址:此时不注册静态地址,由 ADBClient.ensure_connected 时再查 ACEP
            # 但为保持向后兼容,我们预填入共享 EIP,端口由 ACEP 返回的地址决定
            # (实际使用时由调用方通过 acep_client.get_adb_address() 获取)
            shared_eip = config.acep.get("shared_eip")
            if shared_eip:
                client.register_pod(pod_id, shared_eip)
        else:
            # 静态配置回退
            eip_key = f"{role_key}_eip"
            eip = config.adb.get(eip_key)
            if eip and not eip.startswith("<"):
                port = config.adb.get(f"{role_key}_port", 5555)
                client.register_pod(pod_id, eip, port)

    # 多实例兼容(旧版 pods.instances)
    for inst in config.pods.get("instances", []):
        pod_id = inst.get("pod_id")
        if pod_id and not pod_id.startswith("<") and inst.get("eip"):
            client.register_pod(
                pod_id,
                inst["eip"],
                inst.get("adb_port", 5555),
            )

    return client


def create_sdk_client(
    config: AppConfig,
    bridge_base_url: Optional[str] = None,
    adb_client: Optional[ADBClient] = None,
) -> SDKClient:
    """构造 SDK Client(可选事件级输入扩展层)

    默认禁用,仅当 config.sdk_bridge.enabled=true 时构造。
    """
    bridge_cfg = config.sdk_bridge
    if not bridge_cfg.get("enabled", False):
        logger.info(
            "SDK Bridge 未启用(默认走 ACEP+ADB)。"
            "如需事件级输入,在 config 中设置 sdk_bridge.enabled=true"
        )
        # 返回一个未启用的占位客户端
        return SDKClient.from_http_bridge(
            bridge_base_url or "http://localhost:9090",
            adb_client=adb_client,
        )

    if bridge_base_url is None:
        bridge_base_url = bridge_cfg.get("base_url", "http://localhost:9090")
    return SDKClient.from_http_bridge(bridge_base_url, adb_client=adb_client)


def create_pod_pool(config: AppConfig) -> PodPool:
    """从配置构造 Pod 资源池"""
    pool = PodPool()
    pool.load_from_config(config.raw)
    return pool
