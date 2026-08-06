"""云手机 Pod 资源池管理

依据架构文档:
- `00-总体架构设计.md` 第七章:Pod 规格配置、双端 Pod 配对策略
- `02-云手机与外设模拟.md` 第一章:Pod 规格矩阵
- `07-双端协同测试设计.md`:双端协同场景下的 Pod 配对与锁定

职责:
- 维护 Pod 元信息(id、规格、用途、ADB 连接信息)
- 维护 Pod 配对关系(Guardian + K1)
- 提供 Pod 借还机制(并发测试时避免冲突)
- 集成 MUA / SDK / ADB 客户端访问入口
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class PodPoolError(Exception):
    """Pod 资源池异常"""


class PodNotAvailableError(PodPoolError):
    """Pod 不可用(无空闲实例)"""


class PairNotAvailableError(PodPoolError):
    """Pod 配对不可用"""


# ============================================================================
# 数据模型
# ============================================================================


class PodRole(str, Enum):
    """Pod 角色"""

    GUARDIAN = "guardian"
    K1 = "k1"


class PodStatus(str, Enum):
    """Pod 状态"""

    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


@dataclass
class Pod:
    """云手机 Pod 元信息

    架构文档 02-第一章:Pod 规格矩阵
    """

    pod_id: str
    role: PodRole
    resolution: str  # 720x1280 或 640x480
    aosp_version: int
    package_name: str  # 测试 APP 包名
    eip: Optional[str] = None  # ADB 公网直连 EIP
    adb_port: int = 5555
    status: PodStatus = PodStatus.IDLE
    purpose: str = ""  # 用途描述(功能测试 / 回归测试 / 压测)
    last_used: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def adb_target(self) -> str:
        return f"{self.eip}:{self.adb_port}" if self.eip else ""


@dataclass
class PodPair:
    """双端 Pod 配对(Guardian + K1)

    架构文档 00-第 7.3 节:Pod 配对策略
    """

    pair_id: str
    guardian_pod_id: str
    k1_pod_id: str
    purpose: str = ""
    status: PodStatus = PodStatus.IDLE
    last_used: float = 0.0


# ============================================================================
# Pod Pool
# ============================================================================


class PodPool:
    """Pod 资源池

    提供线程安全的 Pod / Pair 借还机制。
    """

    def __init__(self):
        self._pods: Dict[str, Pod] = {}
        self._pairs: Dict[str, PodPair] = {}
        self._lock = threading.RLock()

    # ----------------------------------------------------------------------
    # 注册与配置
    # ----------------------------------------------------------------------

    def register_pod(self, pod: Pod) -> None:
        """注册一个 Pod"""
        with self._lock:
            if pod.pod_id in self._pods:
                logger.warning("Pod 已存在,覆盖: %s", pod.pod_id)
            self._pods[pod.pod_id] = pod
            logger.info(
                "注册 Pod: %s (role=%s, %s, AOSP %d)",
                pod.pod_id,
                pod.role.value,
                pod.resolution,
                pod.aosp_version,
            )

    def register_pair(self, pair: PodPair) -> None:
        """注册一个 Pod 配对"""
        with self._lock:
            # 验证两端都已注册
            if pair.guardian_pod_id not in self._pods:
                raise PodPoolError(
                    f"配对失败:Guardian Pod {pair.guardian_pod_id} 未注册"
                )
            if pair.k1_pod_id not in self._pods:
                raise PodPoolError(
                    f"配对失败:K1 Pod {pair.k1_pod_id} 未注册"
                )
            self._pairs[pair.pair_id] = pair
            logger.info(
                "注册 Pod 配对: %s (G=%s, K1=%s, %s)",
                pair.pair_id,
                pair.guardian_pod_id,
                pair.k1_pod_id,
                pair.purpose,
            )

    def load_from_config(self, config: Dict[str, Any]) -> None:
        """从配置加载 Pod 与配对

        配置结构参考 `config/env.template.json`:
            {
              "pods": {
                "guardian": {"pod_id": "...", "resolution": "720x1280", ...},
                "k1": {"pod_id": "...", "resolution": "640x480", ...},
                "instances": [{"pod_id": "...", "role": "guardian", ...}, ...]
              },
              "pairs": [{"pair_id": "...", "guardian_pod": "...", "k1_pod": "..."}]
            }
        """
        pods_cfg = config.get("pods", {})
        adb_cfg = config.get("adb", {})

        # 单实例配置(向后兼容 env.template.json 中的 guardian/k1 字段)
        for role_key, role in [("guardian", PodRole.GUARDIAN), ("k1", PodRole.K1)]:
            if role_key in pods_cfg:
                pod_cfg = pods_cfg[role_key]
                pod_id = pod_cfg.get("pod_id", "")
                if not pod_id or pod_id.startswith("<"):
                    continue  # 跳过占位符
                eip_key = f"{role_key}_eip"
                pod = Pod(
                    pod_id=pod_id,
                    role=role,
                    resolution=pod_cfg.get("resolution", ""),
                    aosp_version=pod_cfg.get("aosp_version", 0),
                    package_name=pod_cfg.get("package_name", ""),
                    eip=adb_cfg.get(eip_key),
                    adb_port=adb_cfg.get(f"{role_key}_port", 5555),
                    purpose=pod_cfg.get("purpose", f"{role_key} 测试"),
                )
                self.register_pod(pod)

        # 多实例配置(压测场景)
        for inst in pods_cfg.get("instances", []):
            pod_id = inst.get("pod_id", "")
            if not pod_id or pod_id.startswith("<"):
                continue
            role = PodRole(inst.get("role", "guardian"))
            pod = Pod(
                pod_id=pod_id,
                role=role,
                resolution=inst.get("resolution", ""),
                aosp_version=inst.get("aosp_version", 0),
                package_name=inst.get("package_name", ""),
                eip=inst.get("eip") or adb_cfg.get(f"{role.value}_eip"),
                adb_port=inst.get("adb_port", 5555),
                purpose=inst.get("purpose", ""),
            )
            self.register_pod(pod)

        # 配对配置
        for pair_cfg in config.get("pairs", []):
            pair = PodPair(
                pair_id=pair_cfg["pair_id"],
                guardian_pod_id=pair_cfg["guardian_pod"],
                k1_pod_id=pair_cfg["k1_pod"],
                purpose=pair_cfg.get("purpose", ""),
            )
            try:
                self.register_pair(pair)
            except PodPoolError as e:
                logger.warning("跳过配对 %s: %s", pair.pair_id, e)

    # ----------------------------------------------------------------------
    # 借还机制
    # ----------------------------------------------------------------------

    def acquire_pod(
        self,
        role: PodRole,
        purpose: str = "",
        timeout_sec: float = 0,
    ) -> Pod:
        """借出一个指定角色的空闲 Pod

        Args:
            role: guardian / k1
            purpose: 借用用途(记录到日志)
            timeout_sec: 等待空闲 Pod 的超时(0=不等待,无空闲立即抛异常)
        """
        start = time.time()
        while True:
            with self._lock:
                for pod in self._pods.values():
                    if pod.role == role and pod.status == PodStatus.IDLE:
                        pod.status = PodStatus.BUSY
                        pod.purpose = purpose
                        pod.last_used = time.time()
                        logger.info("借出 Pod: %s (%s, %s)", pod.pod_id, role.value, purpose)
                        return pod

            if timeout_sec <= 0 or time.time() - start >= timeout_sec:
                raise PodNotAvailableError(
                    f"无可用 {role.value} Pod(purpose={purpose})"
                )
            time.sleep(0.5)

    def release_pod(self, pod_id: str) -> None:
        """归还 Pod"""
        with self._lock:
            pod = self._pods.get(pod_id)
            if pod is None:
                logger.warning("归还的 Pod 未注册: %s", pod_id)
                return
            pod.status = PodStatus.IDLE
            pod.purpose = ""
            logger.info("归还 Pod: %s", pod_id)

    def acquire_pair(
        self,
        purpose: str = "",
        timeout_sec: float = 0,
    ) -> PodPair:
        """借出一个空闲的双端 Pod 配对

        架构文档 07:协同测试需要同时持有 Guardian 和 K1,必须配对借出避免死锁。
        """
        start = time.time()
        while True:
            with self._lock:
                # 找一个两端都空闲的配对
                for pair in self._pairs.values():
                    g = self._pods.get(pair.guardian_pod_id)
                    k = self._pods.get(pair.k1_pod_id)
                    if (
                        pair.status == PodStatus.IDLE
                        and g
                        and g.status == PodStatus.IDLE
                        and k
                        and k.status == PodStatus.IDLE
                    ):
                        # 同时锁定三个对象
                        pair.status = PodStatus.BUSY
                        g.status = PodStatus.BUSY
                        k.status = PodStatus.BUSY
                        pair.purpose = purpose
                        pair.last_used = time.time()
                        g.purpose = purpose
                        k.purpose = purpose
                        logger.info(
                            "借出 Pod 配对: %s (G=%s, K1=%s, %s)",
                            pair.pair_id,
                            pair.guardian_pod_id,
                            pair.k1_pod_id,
                            purpose,
                        )
                        return pair

            if timeout_sec <= 0 or time.time() - start >= timeout_sec:
                raise PairNotAvailableError(f"无可用 Pod 配对(purpose={purpose})")
            time.sleep(0.5)

    def release_pair(self, pair_id: str) -> None:
        """归还 Pod 配对(同时归还两端)"""
        with self._lock:
            pair = self._pairs.get(pair_id)
            if pair is None:
                logger.warning("归还的配对未注册: %s", pair_id)
                return
            pair.status = PodStatus.IDLE
            pair.purpose = ""
            for pod_id in (pair.guardian_pod_id, pair.k1_pod_id):
                pod = self._pods.get(pod_id)
                if pod:
                    pod.status = PodStatus.IDLE
                    pod.purpose = ""
            logger.info("归还 Pod 配对: %s", pair_id)

    # ----------------------------------------------------------------------
    # 查询接口
    # ----------------------------------------------------------------------

    def get_pod(self, pod_id: str) -> Pod:
        """获取 Pod 元信息(不借用)"""
        with self._lock:
            pod = self._pods.get(pod_id)
            if pod is None:
                raise PodPoolError(f"Pod 未注册: {pod_id}")
            return pod

    def get_pair(self, pair_id: str) -> PodPair:
        with self._lock:
            pair = self._pairs.get(pair_id)
            if pair is None:
                raise PodPoolError(f"配对未注册: {pair_id}")
            return pair

    def list_pods(self, role: Optional[PodRole] = None) -> List[Pod]:
        """列出所有 Pod(可按角色过滤)"""
        with self._lock:
            pods = list(self._pods.values())
            if role:
                pods = [p for p in pods if p.role == role]
            return pods

    def list_pairs(self) -> List[PodPair]:
        with self._lock:
            return list(self._pairs.values())

    def list_idle_pods(self, role: Optional[PodRole] = None) -> List[Pod]:
        """列出空闲 Pod"""
        with self._lock:
            return [
                p for p in self._pods.values()
                if p.status == PodStatus.IDLE
                and (role is None or p.role == role)
            ]

    def list_idle_pairs(self) -> List[PodPair]:
        """列出空闲配对"""
        with self._lock:
            return [p for p in self._pairs.values() if p.status == PodStatus.IDLE]

    def stats(self) -> Dict[str, Any]:
        """资源池统计"""
        with self._lock:
            total_g = sum(1 for p in self._pods.values() if p.role == PodRole.GUARDIAN)
            idle_g = sum(
                1 for p in self._pods.values()
                if p.role == PodRole.GUARDIAN and p.status == PodStatus.IDLE
            )
            total_k = sum(1 for p in self._pods.values() if p.role == PodRole.K1)
            idle_k = sum(
                1 for p in self._pods.values()
                if p.role == PodRole.K1 and p.status == PodStatus.IDLE
            )
            total_pairs = len(self._pairs)
            idle_pairs = sum(1 for p in self._pairs.values() if p.status == PodStatus.IDLE)
            return {
                "guardian": {"total": total_g, "idle": idle_g, "busy": total_g - idle_g},
                "k1": {"total": total_k, "idle": idle_k, "busy": total_k - idle_k},
                "pairs": {"total": total_pairs, "idle": idle_pairs, "busy": total_pairs - idle_pairs},
            }

    # ----------------------------------------------------------------------
    # 上下文管理(便捷借还)
    # ----------------------------------------------------------------------

    def pod_context(self, role: PodRole, purpose: str = ""):
        """Pod 借用上下文管理器

        用法:
            with pool.pod_context(PodRole.GUARDIAN, "smoke test") as pod:
                ...
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            pod = self.acquire_pod(role, purpose)
            try:
                yield pod
            finally:
                self.release_pod(pod.pod_id)

        return _ctx()

    def pair_context(self, purpose: str = ""):
        """配对借用上下文管理器

        用法:
            with pool.pair_context("binding test") as pair:
                guardian = pool.get_pod(pair.guardian_pod_id)
                k1 = pool.get_pod(pair.k1_pod_id)
                ...
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            pair = self.acquire_pair(purpose)
            try:
                yield pair
            finally:
                self.release_pair(pair.pair_id)

        return _ctx()
