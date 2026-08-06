"""凭证管理:从 macOS Keychain 读取 AK/SK

依据架构文档 `architecture/02-云手机与外设模拟.md` 第 1.5 节:
- AK/SK 不入库,存储在 macOS Keychain
- Agent 通过 `security find-generic-password` 读取

环境变量覆盖:
- `VOLC_AK` / `VOLC_SK`:直接传入凭证(优先级高于 Keychain)
- `VOLC_AK_KEYCHAIN_SERVICE` / `VOLC_SK_KEYCHAIN_SERVICE`:自定义 Keychain 服务名
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 默认 Keychain 服务名(与 cloud-phone-test-agent-guide.md 一致)
# ============================================================================


DEFAULT_AK_SERVICE = "<YOUR_AK_KEYCHAIN_SERVICE>"
DEFAULT_SK_SERVICE = "<YOUR_SK_KEYCHAIN_SERVICE>"


# ============================================================================
# 异常定义
# ============================================================================


class CredentialError(Exception):
    """凭证读取异常"""


# ============================================================================
# 凭证数据类
# ============================================================================


@dataclass(frozen=True)
class Credentials:
    """火山引擎 AK/SK 凭证"""

    ak: str
    sk: str

    def __post_init__(self) -> None:
        if not self.ak:
            raise CredentialError("AK 为空")
        if not self.sk:
            raise CredentialError("SK 为空")


# ============================================================================
# Keychain 读取
# ============================================================================


def read_keychain(service: str) -> str:
    """从 macOS Keychain 读取凭证

    Args:
        service: Keychain 服务名(如 "<YOUR_AK_KEYCHAIN_SERVICE>")

    Returns:
        凭证字符串

    Raises:
        CredentialError: 读取失败或凭证为空
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise CredentialError(
            "未找到 `security` 命令,仅 macOS Keychain 可用"
        ) from e
    except subprocess.CalledProcessError as e:
        raise CredentialError(
            f"从 Keychain 读取 {service} 失败: {e.stderr.strip() or e.returncode}"
        ) from e

    value = result.stdout.strip()
    if not value:
        raise CredentialError(f"Keychain 中 {service} 的值为空")
    return value


# ============================================================================
# 统一入口
# ============================================================================


def load_credentials(
    ak_service: str = DEFAULT_AK_SERVICE,
    sk_service: str = DEFAULT_SK_SERVICE,
    allow_env: bool = True,
) -> Credentials:
    """加载凭证,优先级:环境变量 > Keychain

    Args:
        ak_service: AK 的 Keychain 服务名
        sk_service: SK 的 Keychain 服务名
        allow_env: 是否允许从环境变量读取(默认允许)

    Returns:
        Credentials 对象
    """
    # 1. 环境变量(优先级最高,便于 CI/容器环境)
    if allow_env:
        ak = os.environ.get("VOLC_AK", "").strip()
        sk = os.environ.get("VOLC_SK", "").strip()
        if ak and sk:
            logger.debug("从环境变量读取凭证")
            return Credentials(ak=ak, sk=sk)
        if ak or sk:
            raise CredentialError(
                "环境变量 VOLC_AK/VOLC_SK 必须同时设置或同时缺失"
            )

    # 2. 环境变量覆盖 Keychain 服务名
    ak_service = os.environ.get("VOLC_AK_KEYCHAIN_SERVICE", ak_service)
    sk_service = os.environ.get("VOLC_SK_KEYCHAIN_SERVICE", sk_service)

    # 3. Keychain
    logger.debug("从 Keychain 读取凭证: %s / %s", ak_service, sk_service)
    ak = read_keychain(ak_service)
    sk = read_keychain(sk_service)
    return Credentials(ak=ak, sk=sk)
