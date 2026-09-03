"""Runnable bootstrap for the Prismloop Media I/O Harness HTTP service.

两种运行形态(架构文档 12):
- ``--demo``:本地契约验证(recording adapter,无云依赖)
- ``--provider pod-injector``:生产形态,装配 PodAdbSession(P2-P4)+ Pod 注入
  adapter(P1)。部署环境变量见 config/media-harness.template.env。
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any, Callable, Mapping

from .media_http import create_server
from .media_async import AsyncMediaRunService
from .media_config import MediaServiceSettings
from .media_capture import CapturedMedia
from .media_io import IterableVideoSource, RecordingExternalVideoAdapter, StreamProfile
from .media_artifacts import FilesystemArtifactStore
from .media_service import ArtifactStore, Capability, MemoryArtifactStore
from .media_sources import ArtifactAudioSourceResolver, ArtifactVideoSourceResolver
from .media_state import MediaRunStore
from .pod_adb_session import PodAdbSession, build_health_check
from .pod_injector import (
    POD_FIXTURE_ROOT,
    PodFixtureAudioResolver,
    PodFixtureVideoResolver,
    PodInjectorAudioAdapter,
    PodInjectorClient,
    PodInjectorVideoAdapter,
    PushFixture,
)


class DeterministicDemoVideoResolver:
    """Creates deterministic non-empty frames for local service I/O checks only."""

    def resolve(self, input_spec: Mapping[str, Any], profile: StreamProfile) -> IterableVideoSource:
        digest = input_spec["artifact"]["sha256"]
        return IterableVideoSource(profile, [f"{input_spec['input_id']}:{digest[:12]}".encode()])


def build_demo_service(state_db_path: str = ":memory:") -> AsyncMediaRunService:
    """Build a non-provider demo service for local HTTP contract verification."""
    return AsyncMediaRunService(
        store=MediaRunStore(state_db_path),
        artifact_store=MemoryArtifactStore(),
        video_adapter=RecordingExternalVideoAdapter(),
        video_source_resolver=DeterministicDemoVideoResolver(),
        capabilities={
            "local/demo": {
                "camera.video.inject": Capability("camera.video.inject", "verified", "local recording adapter"),
                "camera.continuous_stream_switch": Capability(
                    "camera.continuous_stream_switch", "verified", "local recording adapter"
                ),
            }
        },
    )


def build_pod_injector_service(
    *,
    environment_ref: str,
    artifact_store: ArtifactStore,
    push_fixture: PushFixture,
    state_db_path: str = ":memory:",
    injector_host: str = "127.0.0.1",
    injector_port: int = 18080,
    remote_root: str = POD_FIXTURE_ROOT,
    request_timeout_seconds: float = 20.0,
    wait_sleep: Callable[[float], None] | None = time.sleep,
    ffmpeg_path: str = "ffmpeg",
    capture_screenshot: Callable[[], bytes] | None = None,
    ui_session=None,
) -> AsyncMediaRunService:
    """组装 Pod 裸数据注入路径的 MediaRun 服务(架构文档 11 §5.1)。

    前提(由调用方保证):Pod 内 injector APP 已运行、`adb forward` 已建立,
    ``push_fixture`` 能把本地目录放置到 Pod 文件系统。能力账本以 2026-08-31
    PoC 实测为准;``capture_screenshot``(PodAdbSession.capture_screenshot)
    提供 screen.image.capture 能力(adb screencap,冒烟 2026-09-02);
    ``ui_session``(PodAdbSession)提供 ui.interact / ui.tree 能力
    (adb input / uiautomator,冒烟 2026-09-03)。
    """

    class _PodScreenCaptureAdapter:
        """MediaCaptureAdapter:截图走 adb screencap;录屏/扬声器保持未验证。"""

        def __init__(self, capture: Callable[[], bytes]) -> None:
            self._capture = capture

        def capture_screenshot(self):
            return CapturedMedia(self._capture(), "image/png")

        def start_screen_video(self) -> None:
            raise RuntimeError("screen video capture is not verified for this adapter")

        def stop_screen_video(self):
            raise RuntimeError("screen video capture is not verified for this adapter")

        def start_speaker_audio(self) -> None:
            raise RuntimeError("speaker audio capture is not verified for this adapter")

        def stop_speaker_audio(self):
            raise RuntimeError("speaker audio capture is not verified for this adapter")

    class _PodUiInteractAdapter:
        """UiInteractAdapter:adb input / uiautomator,委托 PodAdbSession。"""

        def __init__(self, session) -> None:
            self._session = session

        def tap(self, x: int, y: int) -> None:
            self._session.ui_tap(x, y)

        def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
            self._session.ui_swipe(x1, y1, x2, y2, duration_ms)

        def text(self, value: str) -> None:
            self._session.ui_text(value)

        def key(self, keycode: int) -> None:
            self._session.ui_key(keycode)

        def launch_app(self, package: str, activity: str | None) -> None:
            self._session.ui_launch_app(package, activity)

        def dump(self) -> bytes:
            return self._session.ui_dump()

    client = PodInjectorClient(
        host=injector_host, port=injector_port, timeout_seconds=request_timeout_seconds
    )
    capabilities = {
        environment_ref: {
            "camera.video.inject": Capability(
                "camera.video.inject", "verified", "pod injector PoC 2026-08-31"
            ),
            "camera.continuous_stream_switch": Capability(
                "camera.continuous_stream_switch", "verified", "pod injector PoC 2026-08-31"
            ),
            "microphone.pcm.inject": Capability(
                "microphone.pcm.inject", "verified", "pod injector PoC 2026-08-31"
            ),
        }
    }
    capture_adapter = None
    if capture_screenshot is not None:
        capture_adapter = _PodScreenCaptureAdapter(capture_screenshot)
        capabilities[environment_ref]["screen.image.capture"] = Capability(
            "screen.image.capture", "verified", "adb screencap smoke 2026-09-02"
        )
    ui_adapter = None
    if ui_session is not None:
        ui_adapter = _PodUiInteractAdapter(ui_session)
        capabilities[environment_ref]["ui.interact"] = Capability(
            "ui.interact", "verified", "adb input smoke 2026-09-03"
        )
        capabilities[environment_ref]["ui.tree"] = Capability(
            "ui.tree", "verified", "uiautomator dump smoke 2026-09-03"
        )
    return AsyncMediaRunService(
        store=MediaRunStore(state_db_path),
        artifact_store=artifact_store,
        video_adapter=PodInjectorVideoAdapter(client),
        video_source_resolver=PodFixtureVideoResolver(
            decoder=ArtifactVideoSourceResolver(artifact_store, ffmpeg_path=ffmpeg_path),
            client=client,
            push_fixture=push_fixture,
            remote_root=remote_root,
        ),
        audio_adapter=PodInjectorAudioAdapter(client),
        audio_source_resolver=PodFixtureAudioResolver(
            decoder=ArtifactAudioSourceResolver(artifact_store, ffmpeg_path=ffmpeg_path),
            client=client,
            push_fixture=push_fixture,
            remote_root=remote_root,
        ),
        capture_adapter=capture_adapter,
        ui_adapter=ui_adapter,
        wait_sleep=wait_sleep,
        capabilities=capabilities,
    )


def build_production_pod_service(
    *,
    pod_id: str,
    environment_ref: str | None = None,
    state_db_path: str = ":memory:",
    injector_port: int = 18080,
    adb_refresh_seconds: float = 1800.0,
    injector_apk: str | None = None,
    probe_apk: str | None = None,
    remote_root: str = POD_FIXTURE_ROOT,
    wait_sleep: Callable[[float], None] = time.sleep,
    ffmpeg_path: str = "ffmpeg",
    logger: Callable[[str], None] | None = None,
    acep_client: Any = None,
) -> tuple[AsyncMediaRunService, PodAdbSession]:
    """生产装配(P1):PodAdbSession + Pod 注入服务(架构文档 12)。

    凭证经 ``acep_client`` 注入(容器内 credentials_source=env);若未提供则
    从标准 config 加载(env.local.json / 环境变量)。
    返回 (service, session);调用方负责 session.bootstrap() / shutdown()。
    """
    if acep_client is None:
        from .config import load_config, create_acep_client

        acep_client = create_acep_client(load_config())
    session = PodAdbSession(
        acep=acep_client,
        pod_id=pod_id,
        injector_apk=injector_apk,
        probe_apk=probe_apk,
        injector_port=injector_port,
        refresh_seconds=adb_refresh_seconds,
        logger=logger or (lambda msg: print(f"[pod-adb] {msg}", flush=True)),
    )
    # 本机部署形态:输入媒体经共享目录 data/artifacts 提供(调用方与本机共享文件系统,
    # artifact_ref = artifact://local/prismloop-media/<相对路径>)。容器化后替换为 TOS。
    artifact_root = os.environ.get("PRISMLOOP_ARTIFACT_ROOT", "data/artifacts")
    artifact_store = FilesystemArtifactStore(
        artifact_root, store_ref=os.environ.get("PRISMLOOP_ARTIFACT_STORE_REF", "local/prismloop-media")
    )
    service = build_pod_injector_service(
        environment_ref=environment_ref or f"volc/pod/{pod_id}",
        artifact_store=artifact_store,
        push_fixture=session.push_fixture,
        capture_screenshot=session.capture_screenshot,
        ui_session=session,
        state_db_path=state_db_path,
        injector_port=injector_port,
        remote_root=remote_root,
        wait_sleep=wait_sleep,
        ffmpeg_path=ffmpeg_path,
    )
    return service, session


def main() -> int:
    parser = argparse.ArgumentParser(description="Prismloop Media I/O Harness service")
    parser.add_argument(
        "--provider",
        choices=["demo", "pod-injector"],
        default="demo",
        help="demo=本地契约验证;pod-injector=生产(Pod 裸数据注入,架构文档 12)",
    )
    parser.add_argument("--demo", action="store_true", help="等价于 --provider demo")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state-db")
    args = parser.parse_args()
    if args.demo and args.provider != "demo":
        parser.error("--demo conflicts with --provider")

    settings = MediaServiceSettings.from_env()
    host = args.host or settings.listen_host
    port = args.port or settings.listen_port
    state_db = args.state_db or settings.state_db_path

    session: PodAdbSession | None = None
    health_check = None
    if args.provider == "pod-injector":
        pod_id = os.environ.get("PRISMLOOP_POD_ID", "").strip()
        if not pod_id:
            parser.error("--provider pod-injector requires PRISMLOOP_POD_ID to be set")
        injector_port = int(os.environ.get("PRISMLOOP_INJECTOR_PORT", "18080"))
        service, session = build_production_pod_service(
            pod_id=pod_id,
            environment_ref=os.environ.get("PRISMLOOP_ENVIRONMENT_REF") or None,
            state_db_path=state_db,
            injector_port=injector_port,
            adb_refresh_seconds=float(os.environ.get("PRISMLOOP_ADB_REFRESH_SECONDS", "1800")),
            injector_apk=os.environ.get("PRISMLOOP_INJECTOR_APK") or None,
            probe_apk=os.environ.get("PRISMLOOP_PROBE_APK") or None,
        )
        print(f"[bootstrap] pod adb session init (pod_id={pod_id})...", flush=True)
        summary = session.bootstrap()
        print(f"[bootstrap] {summary.get('apks')}", flush=True)
        health_check = build_health_check(session)
    else:
        service = build_demo_service(state_db)

    service.start()
    server = create_server(service, host=host, port=port, health_check=health_check)
    mode = args.provider
    print(f"Prismloop Media I/O service ({mode}) listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        service.stop()
        if session is not None:
            session.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
