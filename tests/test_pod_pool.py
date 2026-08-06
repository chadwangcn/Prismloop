"""Pod Pool 单元测试"""

from __future__ import annotations

import threading
import time

import pytest

from src.pod_pool import (
    Pod,
    PodNotAvailableError,
    PodPool,
    PodRole,
    PodStatus,
    PodPair,
    PairNotAvailableError,
)


# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
def sample_pod_pool() -> PodPool:
    pool = PodPool()
    pool.register_pod(Pod(
        pod_id="pod-guardian-01",
        role=PodRole.GUARDIAN,
        resolution="720x1280",
        aosp_version=11,
        package_name="com.lumi.guardian",
        eip="1.1.1.1",
    ))
    pool.register_pod(Pod(
        pod_id="pod-k1-01",
        role=PodRole.K1,
        resolution="640x480",
        aosp_version=10,
        package_name="com.lumi.k1device",
        eip="2.2.2.2",
    ))
    pool.register_pair(PodPair(
        pair_id="pair-01",
        guardian_pod_id="pod-guardian-01",
        k1_pod_id="pod-k1-01",
        purpose="功能测试",
    ))
    return pool


# ============================================================================
# 注册与配置
# ============================================================================


def test_register_pod():
    pool = PodPool()
    pod = Pod(
        pod_id="pod-1",
        role=PodRole.GUARDIAN,
        resolution="720x1280",
        aosp_version=11,
        package_name="com.lumi.guardian",
    )
    pool.register_pod(pod)
    assert pool.get_pod("pod-1") == pod


def test_register_pair_validates_pods():
    pool = PodPool()
    with pytest.raises(Exception):
        pool.register_pair(PodPair(
            pair_id="p",
            guardian_pod_id="missing-guardian",
            k1_pod_id="missing-k1",
        ))


def test_load_from_config_single_instance():
    config = {
        "pods": {
            "guardian": {
                "pod_id": "pod-g-1",
                "resolution": "720x1280",
                "aosp_version": 11,
                "package_name": "com.lumi.guardian",
            },
            "k1": {
                "pod_id": "pod-k-1",
                "resolution": "640x480",
                "aosp_version": 10,
                "package_name": "com.lumi.k1device",
            },
        },
        "adb": {
            "guardian_eip": "1.1.1.1",
            "guardian_port": 5555,
            "k1_eip": "2.2.2.2",
            "k1_port": 5555,
        },
    }
    pool = PodPool()
    pool.load_from_config(config)
    assert len(pool.list_pods()) == 2
    g = pool.get_pod("pod-g-1")
    assert g.role == PodRole.GUARDIAN
    assert g.eip == "1.1.1.1"


def test_load_from_config_skips_placeholders():
    config = {
        "pods": {
            "guardian": {"pod_id": "<PLACEHOLDER>"},
            "k1": {"pod_id": "<PLACEHOLDER>"},
        },
        "adb": {},
    }
    pool = PodPool()
    pool.load_from_config(config)
    assert len(pool.list_pods()) == 0


# ============================================================================
# 借还机制
# ============================================================================


def test_acquire_pod_basic(sample_pod_pool):
    pod = sample_pod_pool.acquire_pod(PodRole.GUARDIAN, purpose="test")
    assert pod.pod_id == "pod-guardian-01"
    assert pod.status == PodStatus.BUSY


def test_acquire_pod_unavailable_raises():
    pool = PodPool()
    with pytest.raises(PodNotAvailableError):
        pool.acquire_pod(PodRole.GUARDIAN)


def test_release_pod_makes_idle(sample_pod_pool):
    pod = sample_pod_pool.acquire_pod(PodRole.GUARDIAN)
    sample_pod_pool.release_pod(pod.pod_id)
    assert sample_pod_pool.get_pod(pod.pod_id).status == PodStatus.IDLE


def test_acquire_pair_locks_both(sample_pod_pool):
    pair = sample_pod_pool.acquire_pair(purpose="dual-test")
    assert pair.pair_id == "pair-01"
    # 两端都应忙碌
    assert sample_pod_pool.get_pod("pod-guardian-01").status == PodStatus.BUSY
    assert sample_pod_pool.get_pod("pod-k1-01").status == PodStatus.BUSY


def test_acquire_pair_unavailable_when_one_busy(sample_pod_pool):
    # 先借出 Guardian
    sample_pod_pool.acquire_pod(PodRole.GUARDIAN)
    # 配对应该不可用
    with pytest.raises(PairNotAvailableError):
        sample_pod_pool.acquire_pair(timeout_sec=0.1)


def test_release_pair_makes_both_idle(sample_pod_pool):
    pair = sample_pod_pool.acquire_pair()
    sample_pod_pool.release_pair(pair.pair_id)
    assert sample_pod_pool.get_pod("pod-guardian-01").status == PodStatus.IDLE
    assert sample_pod_pool.get_pod("pod-k1-01").status == PodStatus.IDLE


# ============================================================================
# 上下文管理器
# ============================================================================


def test_pod_context_manager(sample_pod_pool):
    with sample_pod_pool.pod_context(PodRole.GUARDIAN, "ctx-test") as pod:
        assert pod.pod_id == "pod-guardian-01"
        assert pod.status == PodStatus.BUSY
    # 退出后应为空闲
    assert sample_pod_pool.get_pod("pod-guardian-01").status == PodStatus.IDLE


def test_pair_context_manager(sample_pod_pool):
    with sample_pod_pool.pair_context("ctx-dual") as pair:
        assert pair.pair_id == "pair-01"
        assert sample_pod_pool.get_pod("pod-k1-01").status == PodStatus.BUSY
    assert sample_pod_pool.get_pod("pod-guardian-01").status == PodStatus.IDLE
    assert sample_pod_pool.get_pod("pod-k1-01").status == PodStatus.IDLE


# ============================================================================
# 并发安全
# ============================================================================


def test_concurrent_acquire_pod_is_safe():
    """多线程同时借出,确保同一时刻不会重复借出同一个 Pod"""
    pool = PodPool()
    for i in range(3):
        pool.register_pod(Pod(
            pod_id=f"pod-g-{i}",
            role=PodRole.GUARDIAN,
            resolution="720x1280",
            aosp_version=11,
            package_name="com.lumi.guardian",
        ))

    # 记录被借出的 Pod ID 及其借出时刻,用于事后检查是否存在重叠
    events = []  # (timestamp, action, pod_id)
    lock = threading.Lock()

    def worker():
        try:
            pod = pool.acquire_pod(PodRole.GUARDIAN, timeout_sec=0.5)
            t0 = time.time()
            with lock:
                events.append((t0, "acquire", pod.pod_id))
            time.sleep(0.05)
            with lock:
                events.append((time.time(), "release", pod.pod_id))
            pool.release_pod(pod.pod_id)
        except PodNotAvailableError:
            pass

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 验证:任意时刻同时被借出的 Pod 不重复
    # 用事件流计算每个时刻的活跃 Pod 集合
    active: set = set()
    max_concurrent = 0
    for ts, action, pod_id in sorted(events):
        if action == "acquire":
            assert pod_id not in active, f"Pod {pod_id} 被重复借出"
            active.add(pod_id)
            max_concurrent = max(max_concurrent, len(active))
        else:
            active.discard(pod_id)

    assert max_concurrent > 0
    assert max_concurrent <= 3  # 不超过 Pod 总数


# ============================================================================
# 查询与统计
# ============================================================================


def test_list_pods_by_role(sample_pod_pool):
    guardian = sample_pod_pool.list_pods(role=PodRole.GUARDIAN)
    k1 = sample_pod_pool.list_pods(role=PodRole.K1)
    assert len(guardian) == 1
    assert len(k1) == 1


def test_stats(sample_pod_pool):
    stats = sample_pod_pool.stats()
    assert stats["guardian"]["total"] == 1
    assert stats["guardian"]["idle"] == 1
    assert stats["k1"]["total"] == 1
    assert stats["pairs"]["total"] == 1
