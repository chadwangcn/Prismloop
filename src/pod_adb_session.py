"""PodAdbSession:云手机 Pod 的 ADB 生命周期管理(架构文档 12 P2-P4)。

职责(从 poc 脚本下沉为服务内组件):
- P2 连接:ACEP ``pod_adb_enable`` 解析公网 ADB 地址 → ``adb connect`` → forward 注入端口
- P3 续租:公网 ADB 地址是临时的(TTL ~3600s),后台线程周期性重解析、
  地址变化时重连并重建 forward;injector 失联时自愈重启
- P4 APK 幂等部署:injector / media-probe 缺失时自动 ``adb install``

线程约定:``_lock`` 保护 adb_target 状态;refresh 线程 daemon,stop 后退出。
凭证:由调用方传入已装配好的 ACEP client(容器内 credentials_source=env)。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Mapping, Optional

INJECTOR_ACTIVITY = "{pkg}/.MainActivity"


class PodAdbSessionError(RuntimeError):
    """ADB 会话操作失败(连接/安装/推送/forward)。"""


class PodAdbSession:
    """单 Pod 的 ADB 会话与 injector 进程守护。"""

    def __init__(
        self,
        *,
        acep,
        pod_id: str,
        injector_pkg: str = "cn.prismloop.mediainjector",
        injector_apk: str | Path | None = None,
        probe_pkg: str | None = "cn.prismloop.mediaprobe",
        probe_apk: str | Path | None = None,
        injector_port: int = 18080,
        adb_path: str = "adb",
        refresh_seconds: float = 1800.0,
        injector_start_wait_seconds: float = 30.0,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._acep = acep
        self._pod_id = pod_id
        self._injector_pkg = injector_pkg
        self._injector_apk = injector_apk
        self._probe_pkg = probe_pkg
        self._probe_apk = probe_apk
        self._injector_port = injector_port
        self._adb_path = adb_path
        self._refresh_seconds = refresh_seconds
        self._start_wait = injector_start_wait_seconds
        self._log = logger or (lambda msg: None)
        self._lock = threading.Lock()
        self._adb_target: str | None = None
        self._refresh_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ 属性

    @property
    def adb_target(self) -> str | None:
        with self._lock:
            return self._adb_target

    @property
    def injector_base_url(self) -> str:
        return f"http://127.0.0.1:{self._injector_port}"

    # ------------------------------------------------------------- P2 连接

    def connect(self) -> str:
        """解析当前公网 ADB 地址并连接;返回 adb target(如 1.2.3.4:10001)。"""
        addr = self._acep.pod_adb_enable(self._pod_id)
        target = addr.address
        subprocess.run(
            [self._adb_path, "connect", target],
            capture_output=True, text=True, timeout=30,
        )
        state = subprocess.run(
            [self._adb_path, "-s", target, "get-state"],
            capture_output=True, text=True, timeout=30,
        )
        if "device" not in state.stdout:
            raise PodAdbSessionError(f"adb device state not ready for {target}: {state.stdout.strip()}{state.stderr.strip()}")
        with self._lock:
            self._adb_target = target
        self._log(f"pod adb connected: {target}")
        return target

    def _require_target(self) -> str:
        target = self.adb_target
        if target is None:
            raise PodAdbSessionError("pod adb session not connected yet; call connect() first")
        return target

    def _adb(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        target = self._require_target()
        return subprocess.run(
            [self._adb_path, "-s", target, *args],
            capture_output=True, text=True, timeout=timeout,
        )

    # ------------------------------------------------------------ P4 APK 部署

    def ensure_apks(self) -> dict:
        """幂等安装 injector / media-probe;返回每个包的安装结果。"""
        results: dict = {}
        pairs: List[tuple] = [(self._injector_pkg, self._injector_apk)]
        if self._probe_pkg and self._probe_apk:
            pairs.append((self._probe_pkg, self._probe_apk))
        for pkg, apk in pairs:
            r = self._adb("shell", f"pm list packages {pkg}")
            if pkg in r.stdout:
                results[pkg] = "already_installed"
                continue
            if apk is None:
                raise PodAdbSessionError(f"{pkg} not installed and no apk path configured")
            r = self._adb("install", "-t", "-r", str(apk), timeout=300)
            ok = r.returncode == 0 and "Success" in (r.stdout + r.stderr)
            if not ok:
                raise PodAdbSessionError(f"install {apk.name} for {pkg} failed: {r.stdout}{r.stderr}"[:500])
            results[pkg] = "installed"
            self._log(f"installed {pkg} from {apk}")
        return results

    # ------------------------------------------------------- injector 守护

    def start_injector(self) -> dict:
        """(重)启动 Pod 内 injector APP 并建立 forward;轮询 /status 直到就绪。"""
        self._adb("shell", f"am force-stop {self._injector_pkg}")
        self._adb("shell", "am start -n " + INJECTOR_ACTIVITY.format(pkg=self._injector_pkg))
        self._adb("forward", f"tcp:{self._injector_port}", f"tcp:{self._injector_port}")
        deadline = time.monotonic() + self._start_wait
        last_error = "timeout"
        while time.monotonic() < deadline:
            try:
                status = self._injector_status()
                self._log("injector ready")
                return status
            except Exception as exc:  # noqa: BLE001 - 启动期瞬时失败(拒连/重置/半开)统一重试
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(1.0)
        raise PodAdbSessionError(f"injector did not become ready within {self._start_wait}s: {last_error}")

    def _injector_status(self) -> dict:
        with urllib.request.urlopen(f"{self.injector_base_url}/status", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def injector_reachable(self) -> bool:
        try:
            self._injector_status()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ fixture 推送

    def push_fixture(self, local_dir: str, remote_dir: str) -> str:
        """PushFixture 协议实现:本地目录内容 → Pod remote_dir。"""
        if not Path(local_dir).is_dir():
            raise PodAdbSessionError(f"local fixture dir not found: {local_dir}")
        self._adb("shell", f"mkdir -p {remote_dir}")
        r = self._adb("push", f"{local_dir}/.", remote_dir, timeout=1200)
        if r.returncode != 0:
            raise PodAdbSessionError(f"adb push {local_dir} -> {remote_dir} failed: {r.stderr[:300]}")
        return remote_dir

    # ------------------------------------------------------------ 屏幕采集

    def capture_screenshot(self) -> bytes:
        """`adb exec-out screencap -p` 截屏,返回 PNG 字节(二进制安全)。"""
        target = self._require_target()
        r = subprocess.run(
            [self._adb_path, "-s", target, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=60,
        )
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
            raise PodAdbSessionError(
                f"adb screencap failed: rc={r.returncode} stderr={r.stderr[:200]!r}"
            )
        return r.stdout

    # --------------------------------------------------------------- P3 续租

    def start_refresh_loop(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._stop.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name="pod-adb-refresh", daemon=True
        )
        self._refresh_thread.start()

    def stop_refresh_loop(self) -> None:
        self._stop.set()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self._refresh_seconds):
            try:
                self.refresh_once()
            except Exception as exc:  # noqa: BLE001 - 守护线程不允许退出
                self._log(f"pod adb refresh failed: {exc}")

    def refresh_once(self) -> dict:
        """一次续租:重解析地址,必要时重连;injector 失联则自愈重启。

        返回本次动作摘要(供测试与健康检查使用)。
        """
        actions: dict = {"reconnected": False, "injector_restarted": False}
        addr = self._acep.pod_adb_enable(self._pod_id)
        new_target = addr.address
        if new_target != self.adb_target:
            subprocess.run(
                [self._adb_path, "connect", new_target],
                capture_output=True, text=True, timeout=30,
            )
            with self._lock:
                self._adb_target = new_target
            self._adb("forward", f"tcp:{self._injector_port}", f"tcp:{self._injector_port}")
            actions["reconnected"] = True
            self._log(f"pod adb address rotated -> {new_target}, forward rebuilt")
        if not self.injector_reachable():
            self.start_injector()
            actions["injector_restarted"] = True
        return actions

    # ----------------------------------------------------------------- 健康

    def health(self) -> dict:
        """P6 部署探针:进程存活(隐含)+ ADB 连接 + injector 可达。"""
        target = self.adb_target
        checks = {
            "pod_id": self._pod_id,
            "adb_target": target,
            "adb_connected": False,
            "injector_reachable": False,
        }
        if target is not None:
            r = subprocess.run(
                [self._adb_path, "-s", target, "get-state"],
                capture_output=True, text=True, timeout=30,
            )
            checks["adb_connected"] = "device" in r.stdout
        checks["injector_reachable"] = self.injector_reachable()
        checks["status"] = "ok" if checks["adb_connected"] and checks["injector_reachable"] else "error"
        if checks["status"] == "error":
            checks["error"] = "pod adb disconnected or injector unreachable"
        return checks

    # ----------------------------------------------------------------- 编排

    def bootstrap(self) -> dict:
        """完整初始化:连接 → APK 幂等部署 → injector 启动 → 开启续租线程。"""
        self.connect()
        apks = self.ensure_apks()
        injector = self.start_injector()
        self.start_refresh_loop()
        return {"apks": apks, "injector": injector}

    def shutdown(self) -> None:
        self.stop_refresh_loop()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2.0)


def build_health_check(session: PodAdbSession, service_state: Optional[Callable[[], str]] = None) -> Callable[[], Mapping]:
    """组装部署探针:PodAdbSession 健康状态(附加可选的服务状态)。"""

    def health() -> Mapping:
        payload = dict(session.health())
        if service_state is not None:
            payload["service"] = service_state()
        return payload

    return health
