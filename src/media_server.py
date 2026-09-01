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
from .media_io import IterableVideoSource, RecordingExternalVideoAdapter, StreamProfile
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
) -> AsyncMediaRunService:
    """组装 Pod 裸数据注入路径的 MediaRun 服务(架构文档 11 §5.1)。

    前提(由调用方保证):Pod 内 injector APP 已运行、`adb forward` 已建立,
    ``push_fixture`` 能把本地目录放置到 Pod 文件系统。能力账本以 2026-08-31
    PoC 实测为准。
    """
    client = PodInjectorClient(
        host=injector_host, port=injector_port, timeout_seconds=request_timeout_seconds
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
        wait_sleep=wait_sleep,
        capabilities={
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
        },
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
    service = build_pod_injector_service(
        environment_ref=environment_ref or f"volc/pod/{pod_id}",
        artifact_store=MemoryArtifactStore(),
        push_fixture=session.push_fixture,
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
