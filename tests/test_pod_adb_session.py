"""PodAdbSession 与 /healthz 部署探针的契约测试(架构文档 12 P2-P6)。

伪造边界:
- FakeAcep:pod_adb_enable 返回可控地址对象
- monkeypatch subprocess.run:按命令序列返回预设结果
- 假 injector HTTP server(复用 test_pod_injector 的 FakeInjectorServer)
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from types import SimpleNamespace

import pytest

from src.media_http import create_server
from src.media_service import Capability, MediaRunService, MemoryArtifactStore
from src.media_io import RecordingExternalVideoAdapter
from src.pod_adb_session import PodAdbSession, PodAdbSessionError, build_health_check


class FakeAdb:
    """拦截 subprocess.run,按 (argv 前缀) 规则应答。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.rules: dict[tuple, subprocess.CompletedProcess] = {}
        self.default = subprocess.CompletedProcess([], 0, "", "")

    def on(self, *argv_prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.rules[tuple(argv_prefix)] = subprocess.CompletedProcess(
            [], returncode, stdout, stderr
        )

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, result in self.rules.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return result
        return self.default


class FakeAcep:
    """pod_adb_enable 返回地址序列(模拟公网地址轮换)。"""

    def __init__(self, addresses: list[str]):
        self._addresses = list(addresses)
        self.calls = 0

    def pod_adb_enable(self, pod_id: str):
        self.calls += 1
        return SimpleNamespace(address=self._addresses[min(self.calls - 1, len(self._addresses) - 1)])


@pytest.fixture()
def fake_adb(monkeypatch):
    fake = FakeAdb()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def make_session(fake_adb, addresses=("1.2.3.4:10001",), injector_port=18080, **kwargs):
    fake_adb.on("adb", "connect")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "get-state", stdout="device\n")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "pm list packages cn.prismloop.mediainjector",
               stdout="package:cn.prismloop.mediainjector\n")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "pm list packages cn.prismloop.mediaprobe",
                stdout="package:cn.prismloop.mediaprobe\n")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "am force-stop")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "am start")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "forward")
    kwargs.setdefault("probe_apk", "/opt/fake/probe.apk")  # 已装场景不触达该路径
    return PodAdbSession(
        acep=FakeAcep(list(addresses)),
        pod_id="pod-123",
        injector_port=injector_port,
        injector_start_wait_seconds=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# P2 连接
# ---------------------------------------------------------------------------


def test_connect_resolves_address_and_verifies_device_state(fake_adb):
    session = make_session(fake_adb)
    assert session.connect() == "1.2.3.4:10001"
    assert ("adb", "connect", "1.2.3.4:10001") in [tuple(c[:3]) for c in fake_adb.calls]
    assert session.adb_target == "1.2.3.4:10001"


def test_connect_raises_when_device_not_ready(fake_adb):
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "get-state", stdout="offline\n")
    fake_adb.on("adb", "connect")
    session = PodAdbSession(acep=FakeAcep(["1.2.3.4:10001"]), pod_id="pod-123")
    with pytest.raises(PodAdbSessionError, match="device state"):
        session.connect()


def test_operations_require_connection(fake_adb):
    session = PodAdbSession(acep=FakeAcep(["1.2.3.4:10001"]), pod_id="pod-123")
    with pytest.raises(PodAdbSessionError, match="not connected"):
        session.ensure_apks()


# ---------------------------------------------------------------------------
# P4 APK 幂等部署
# ---------------------------------------------------------------------------


def test_ensure_apks_skips_installed_packages(fake_adb):
    session = make_session(fake_adb)
    session.connect()
    result = session.ensure_apks()
    assert result == {
        "cn.prismloop.mediainjector": "already_installed",
        "cn.prismloop.mediaprobe": "already_installed",
    }
    install_calls = [c for c in fake_adb.calls if len(c) > 3 and c[3] == "install"]
    assert install_calls == []


def test_ensure_apks_installs_missing_apk(fake_adb, tmp_path):
    apk = tmp_path / "injector.apk"
    apk.write_bytes(b"fake-apk")
    fake_adb.on("adb", "connect")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "get-state", stdout="device\n")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "pm list packages cn.prismloop.mediainjector",
                stdout="")  # 未安装
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "install", "-t", "-r", stdout="Success\n")
    session = PodAdbSession(acep=FakeAcep(["1.2.3.4:10001"]), pod_id="pod-123",
                            injector_apk=apk, probe_pkg=None, probe_apk=None)
    session.connect()
    result = session.ensure_apks()
    assert result["cn.prismloop.mediainjector"] == "installed"


def test_ensure_apks_fails_without_apk_path(fake_adb):
    fake_adb.on("adb", "connect")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "get-state", stdout="device\n")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "pm list packages cn.prismloop.mediainjector",
                stdout="")
    session = PodAdbSession(acep=FakeAcep(["1.2.3.4:10001"]), pod_id="pod-123",
                            probe_pkg=None, probe_apk=None)
    session.connect()
    with pytest.raises(PodAdbSessionError, match="no apk path"):
        session.ensure_apks()


# ---------------------------------------------------------------------------
# fixture 推送
# ---------------------------------------------------------------------------


def test_push_fixture_uses_adb_push(fake_adb, tmp_path):
    local = tmp_path / "fixture"
    local.mkdir()
    (local / "manifest.json").write_text("{}", encoding="utf-8")
    session = make_session(fake_adb)
    session.connect()
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "mkdir -p")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "push")
    assert session.push_fixture(str(local), "/data/local/tmp/f") == "/data/local/tmp/f"


def test_push_fixture_raises_on_missing_dir(fake_adb):
    session = make_session(fake_adb)
    session.connect()
    with pytest.raises(PodAdbSessionError, match="not found"):
        session.push_fixture("/nonexistent-dir", "/remote")


def test_push_fixture_raises_on_adb_failure(fake_adb, tmp_path):
    local = tmp_path / "fixture"
    local.mkdir()
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "shell", "mkdir -p")
    fake_adb.on("adb", "-s", "1.2.3.4:10001", "push", stderr="write failed",
                returncode=1)
    session = make_session(fake_adb)
    session.connect()
    with pytest.raises(PodAdbSessionError, match="adb push"):
        session.push_fixture(str(local), "/remote")


# ---------------------------------------------------------------------------
# P3 续租与地址轮换
# ---------------------------------------------------------------------------


def test_refresh_once_rotates_address_and_rebuilds_forward(fake_adb, monkeypatch):
    fake_adb.on("adb", "-s", "5.6.7.8:10002", "get-state", stdout="device\n")
    fake_adb.on("adb", "-s", "5.6.7.8:10002", "forward")
    session = make_session(fake_adb, addresses=("1.2.3.4:10001", "5.6.7.8:10002"))
    session.connect()
    assert session.adb_target == "1.2.3.4:10001"
    monkeypatch.setattr(session, "injector_reachable", lambda: True)  # injector 健康,无需自愈

    actions = session.refresh_once()
    assert actions == {"reconnected": True, "injector_restarted": False}
    assert session.adb_target == "5.6.7.8:10002"
    forwards = [c for c in fake_adb.calls if len(c) > 3 and c[3] == "forward" and "5.6.7.8:10002" in c]
    assert len(forwards) == 1  # 新地址 forward 重建


def test_refresh_once_restarts_injector_when_unreachable(fake_adb):
    # injector_reachable() 走真实 HTTP → 本地无 injector → 不可达 → 触发自愈重启。
    # start_injector 轮询超时(wait=0)抛错:自愈失败应显式上抛,由守护循环兜底捕获。
    session = make_session(fake_adb)
    session.connect()
    with pytest.raises(PodAdbSessionError, match="did not become ready"):
        session.refresh_once()
    am_starts = [c for c in fake_adb.calls if any("am start" in arg for arg in c)]
    assert am_starts  # 自愈路径确实尝试了 am start


def test_refresh_loop_runs_in_background_and_stops(fake_adb, monkeypatch):
    session = make_session(fake_adb, addresses=("1.2.3.4:10001",))
    session.connect()
    monkeypatch.setattr(PodAdbSession, "refresh_once", lambda self: {"reconnected": False})
    session._refresh_seconds = 0.05
    session.start_refresh_loop()
    import time

    time.sleep(0.2)
    session.stop_refresh_loop()
    session.shutdown()  # 不挂起即通过


# ---------------------------------------------------------------------------
# P6 健康探针
# ---------------------------------------------------------------------------


def _demo_service():
    return MediaRunService(
        artifact_store=MemoryArtifactStore(),
        video_adapter=RecordingExternalVideoAdapter(),
        video_source_resolver=_DemoResolver(),
        capabilities={"local/demo": {"camera.video.inject": Capability("camera.video.inject", "verified")}},
    )


class _DemoResolver:
    def resolve(self, input_spec, profile):
        from src.media_io import IterableVideoSource

        return IterableVideoSource(profile, [b"f"])


def test_healthz_returns_ok_without_health_check():
    server = create_server(_demo_service(), host="127.0.0.1", port=0)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
        assert payload == {"status": "ok", "checks": {}}
    finally:
        server.shutdown()
        server.server_close()


def test_healthz_reports_503_when_probe_fails():
    def failing_probe():
        raise RuntimeError("injector unreachable")

    server = create_server(_demo_service(), host="127.0.0.1", port=0, health_check=failing_probe)
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert excinfo.value.code == 503
        assert "injector unreachable" in excinfo.value.read().decode()
    finally:
        server.shutdown()
        server.server_close()


def test_build_health_check_merges_session_state(fake_adb):
    session = make_session(fake_adb)
    session.connect()
    probe = build_health_check(session, service_state=lambda: "running")
    payload = probe()
    assert payload["service"] == "running"
    assert payload["adb_connected"] is True
    assert payload["injector_reachable"] is False  # 无真实 injector
    assert payload["status"] == "error"  # health() 报告降级状态而非抛错
