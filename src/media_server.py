"""Runnable bootstrap for the Prismloop Media I/O Harness HTTP service.

Only the local recording/demo adapter is constructed here.  A real Volc
adapter must be added through a separate bootstrap once the client SDK bridge
for external raw-frame injection has been verified.  This prevents a local
test transport from accidentally being selected for a cloud environment.
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Callable, Mapping

from .media_http import create_server
from .media_async import AsyncMediaRunService
from .media_config import MediaServiceSettings
from .media_io import IterableVideoSource, RecordingExternalVideoAdapter, StreamProfile
from .media_service import ArtifactStore, Capability, MemoryArtifactStore
from .media_sources import ArtifactAudioSourceResolver, ArtifactVideoSourceResolver
from .media_state import MediaRunStore
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Prismloop Media I/O Harness service")
    parser.add_argument("--demo", action="store_true", help="run only the local recording adapter")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state-db")
    args = parser.parse_args()
    if not args.demo:
        parser.error(
            "no production provider bootstrap is available yet; use --demo only for local contract checks"
        )
    settings = MediaServiceSettings.from_env()
    host = args.host or settings.listen_host
    port = args.port or settings.listen_port
    state_db = args.state_db or settings.state_db_path
    service = build_demo_service(state_db)
    service.start()
    server = create_server(service, host=host, port=port)
    print(f"Prismloop Media I/O demo service listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
