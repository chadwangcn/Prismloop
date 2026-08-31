"""Pod injector HTTP 控制接入编排层的契约测试(架构文档 11 §5.1)。

用进程内假 injector HTTP server 复现 media-injector 的 HttpApi 行为
(恒 200 应答、错误在 body 的 error 字段),验证:
- PodInjectorClient 的命令映射与错误分类
- fixture 播放源的 lazy 启动 / 无断流热切换 / 回切重下发
- resolver 的 fixture 产出格式(manifest.json + video.bin / audio.pcm)
- MediaRunService 编排层端到端(视频热切换 + 音频循环 + wait 真实时钟)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.audio_io import AudioProfile, IterableAudioSource
from src.media_io import IterableVideoSource, StreamProfile
from src.media_service import Capability, MediaRunService, MemoryArtifactStore
from src.pod_injector import (
    InjectorCommandError,
    InjectorUnavailableError,
    PodFixtureAudioResolver,
    PodFixtureVideoResolver,
    PodInjectorAudioAdapter,
    PodInjectorClient,
    PodInjectorVideoAdapter,
)


class FakeInjectorServer:
    """进程内假 injector:复刻 HttpApi/InjectorService 的路由与应答约定。"""

    def __init__(self, *, fail_paths: set[str] | None = None) -> None:
        self.calls: list[tuple] = []
        self._fail_paths = fail_paths or set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默测试输出
                return

            def _respond(self, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != "/status":
                    return self._respond({"error": f"unknown path {self.path}"})
                outer.calls.append(("status",))
                self._respond({"service": "fake-injector", "camera": True, "audio": False})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path in outer._fail_paths:
                    return self._respond({"error": f"injected failure at {self.path}"})
                if self.path == "/camera/sequence":
                    outer.calls.append(("camera_sequence", body["dir"], body["fps"]))
                    return self._respond({"ok": True, "action": "camera/sequence"})
                if self.path == "/camera/stop":
                    outer.calls.append(("camera_stop",))
                    return self._respond({"ok": True, "action": "camera/stop"})
                if self.path == "/audio/file":
                    outer.calls.append(
                        ("audio_file", body["path"], body["sampleRate"], body["channels"])
                    )
                    return self._respond({"ok": True, "action": "audio/file"})
                if self.path == "/audio/stop":
                    outer.calls.append(("audio_stop",))
                    return self._respond({"ok": True, "action": "audio/stop"})
                return self._respond({"error": f"unknown path {self.path}"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "FakeInjectorServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def injector():
    server = FakeInjectorServer().start()
    yield server
    server.stop()


@pytest.fixture()
def client(injector):
    return PodInjectorClient(host="127.0.0.1", port=injector.port, timeout_seconds=5)


class RecordingPushFixture:
    """记录 push 调用并快照本地目录内容,供断言。"""

    def __init__(self) -> None:
        self.pushed: list[dict] = []

    def __call__(self, local_dir: str, remote_dir: str) -> str:
        snapshot = {}
        for path in sorted(__import__("pathlib").Path(local_dir).iterdir()):
            snapshot[path.name] = path.read_bytes()
        self.pushed.append({"remote_dir": remote_dir, "files": snapshot})
        return remote_dir


def make_video_decoder(frames_by_input_id: dict, profile: StreamProfile):
    class Decoder:
        def resolve(self, input_spec, stream_profile):
            assert stream_profile == profile
            return IterableVideoSource(profile, frames_by_input_id[input_spec["input_id"]])

    return Decoder()


def make_audio_decoder(sources_by_input_id: dict):
    class Decoder:
        def resolve(self, input_spec):
            return sources_by_input_id[input_spec["input_id"]]

    return Decoder()


def video_input(input_id: str, sha: str) -> dict:
    return {
        "input_id": input_id,
        "kind": "camera.video",
        "artifact": {
            "artifact_ref": f"artifact://fixtures/{input_id}.mp4",
            "sha256": sha,
            "media_type": "video/mp4",
        },
    }


def microphone_input(input_id: str, sha: str) -> dict:
    return {
        "input_id": input_id,
        "kind": "microphone.pcm",
        "artifact": {
            "artifact_ref": f"artifact://fixtures/{input_id}.pcm",
            "sha256": sha,
            "media_type": "application/octet-stream",
        },
        "audio_format": {"sample_rate_hz": 48000, "channels": 2, "sample_format": "s16le", "frame_duration_ms": 20},
    }


# ---------------------------------------------------------------------------
# PodInjectorClient
# ---------------------------------------------------------------------------


def test_client_maps_commands_and_tracks_state(injector, client):
    client.status()
    client.camera_sequence("/data/local/tmp/prismloop/fixtures/video-a", 30)
    assert client.current_camera_dir.endswith("/video-a")
    client.audio_file("/data/local/tmp/prismloop/fixtures/tone.pcm", 48000, 2)
    assert client.current_audio_path.endswith("/tone.pcm")
    client.camera_stop()
    client.audio_stop()
    assert client.current_camera_dir is None
    assert client.current_audio_path is None

    assert injector.calls == [
        ("status",),
        ("camera_sequence", "/data/local/tmp/prismloop/fixtures/video-a", 30),
        ("audio_file", "/data/local/tmp/prismloop/fixtures/tone.pcm", 48000, 2),
        ("camera_stop",),
        ("audio_stop",),
    ]


def test_client_raises_command_error_from_error_body():
    server = FakeInjectorServer(fail_paths={"/camera/sequence"}).start()
    try:
        client = PodInjectorClient(host="127.0.0.1", port=server.port, timeout_seconds=5)
        with pytest.raises(InjectorCommandError):
            client.camera_sequence("/data/local/tmp/x", 30)
    finally:
        server.stop()


def test_client_raises_unavailable_when_injector_down():
    client = PodInjectorClient(host="127.0.0.1", port=1, timeout_seconds=0.5)
    with pytest.raises(InjectorUnavailableError):
        client.status()


# ---------------------------------------------------------------------------
# fixture 播放源:lazy 启动 / 热切换 / 回切
# ---------------------------------------------------------------------------


def test_video_source_starts_lazily_and_hot_switches_without_stop(injector, client):
    profile = StreamProfile(width=64, height=64, fps=30)
    source_a = PodFixtureVideoResolver(
        decoder=make_video_decoder({"a": [b"a0", b"a1"]}, profile),
        client=client,
        push_fixture=lambda local, remote: remote,
    ).resolve(video_input("a", "a" * 64), profile)
    source_b = PodFixtureVideoResolver(
        decoder=make_video_decoder({"b": [b"b0", b"b1"]}, profile),
        client=client,
        push_fixture=lambda local, remote: remote,
    ).resolve(video_input("b", "b" * 64), profile)

    assert injector.calls == []  # input.stage 不启动注入

    source_a.next_frame()
    source_a.next_frame()
    assert injector.calls == [("camera_sequence", source_a.pod_dir, 30)]  # 幂等,只下发一次

    source_b.next_frame()
    assert injector.calls == [
        ("camera_sequence", source_a.pod_dir, 30),
        ("camera_sequence", source_b.pod_dir, 30),  # 热切换:无 camera_stop
    ]

    source_a.rewind()
    source_a.next_frame()  # A→B→A 回切
    assert injector.calls[-1] == ("camera_sequence", source_a.pod_dir, 30)


def test_video_source_loops_after_end_of_frames(injector, client):
    profile = StreamProfile(width=64, height=64, fps=30)
    source = PodFixtureVideoResolver(
        decoder=make_video_decoder({"a": [b"a0"]}, profile),
        client=client,
        push_fixture=lambda local, remote: remote,
    ).resolve(video_input("a", "a" * 64), profile)

    assert source.next_frame().payload == b"a0"
    with pytest.raises(StopIteration):
        source.next_frame()
    source.rewind()
    assert source.next_frame().payload == b"a0"
    assert len(injector.calls) == 1  # 循环消费不重复下发 sequence


def test_audio_source_starts_lazily_and_restarts_after_stop(injector, client):
    profile = AudioProfile(sample_rate_hz=48000, channels=2)
    frame = b"\x01" * profile.frame_bytes
    source = PodFixtureAudioResolver(
        decoder=make_audio_decoder({"tone": IterableAudioSource(profile, [frame])}),
        client=client,
        push_fixture=lambda local, remote: remote,
    ).resolve(microphone_input("tone", "c" * 64))

    assert injector.calls == []
    source.next_frame()
    assert injector.calls == [("audio_file", source.pod_path, 48000, 2)]

    client.audio_stop()
    source.rewind()
    source.next_frame()  # stop 后重开:重新下发 audio/file
    assert injector.calls == [
        ("audio_file", source.pod_path, 48000, 2),
        ("audio_stop",),
        ("audio_file", source.pod_path, 48000, 2),
    ]


# ---------------------------------------------------------------------------
# resolver 的 fixture 产出
# ---------------------------------------------------------------------------


def test_video_resolver_writes_manifest_and_pushes_fixture(injector, client):
    profile = StreamProfile(width=64, height=64, fps=30)
    push = RecordingPushFixture()
    source = PodFixtureVideoResolver(
        decoder=make_video_decoder({"a": [b"a0", b"a1"]}, profile),
        client=client,
        push_fixture=push,
        remote_root="/data/local/tmp/prismloop/fixtures",
    ).resolve(video_input("a", "a" * 64), profile)

    assert len(push.pushed) == 1
    pushed = push.pushed[0]
    assert pushed["remote_dir"] == "/data/local/tmp/prismloop/fixtures/a-aaaaaaaa"
    assert pushed["files"]["video.bin"] == b"a0a1"
    assert json.loads(pushed["files"]["manifest.json"]) == {
        "width": 64,
        "height": 64,
        "format": "i420",
        "fps": 30,
        "file": "video.bin",
        "frames": 2,
    }
    assert source.pod_dir == pushed["remote_dir"]
    assert injector.calls == []  # stage 只上传,不启动


def test_video_resolver_rejects_unsupported_pixel_format(injector, client):
    profile = StreamProfile(width=64, height=64, fps=30, pixel_format="nv12")
    resolver = PodFixtureVideoResolver(
        decoder=make_video_decoder({"a": [b"a0"]}, profile),
        client=client,
        push_fixture=lambda local, remote: remote,
    )
    with pytest.raises(ValueError, match="yuv420p/rgba"):
        resolver.resolve(video_input("a", "a" * 64), profile)


def test_audio_resolver_pushes_pcm_fixture(injector, client):
    profile = AudioProfile(sample_rate_hz=48000, channels=2)
    frames = [b"\x01" * profile.frame_bytes, b"\x02" * profile.frame_bytes]
    push = RecordingPushFixture()
    source = PodFixtureAudioResolver(
        decoder=make_audio_decoder({"tone": IterableAudioSource(profile, frames)}),
        client=client,
        push_fixture=push,
        remote_root="/data/local/tmp/prismloop/fixtures",
    ).resolve(microphone_input("tone", "c" * 64))

    assert len(push.pushed) == 1
    pushed = push.pushed[0]
    assert pushed["remote_dir"] == "/data/local/tmp/prismloop/fixtures/tone-cccccccc"
    assert pushed["files"]["audio.pcm"] == b"".join(frames)
    assert source.pod_path == f"{pushed['remote_dir']}/audio.pcm"


# ---------------------------------------------------------------------------
# MediaRunService 编排层端到端
# ---------------------------------------------------------------------------


def pod_capabilities():
    return {
        "cloud-phone/pod-injector": {
            "camera.video.inject": Capability("camera.video.inject", "verified"),
            "camera.continuous_stream_switch": Capability("camera.continuous_stream_switch", "verified"),
            "microphone.pcm.inject": Capability("microphone.pcm.inject", "verified"),
        }
    }


def build_pod_service(injector, client, *, frames, audio_sources=None):
    profile = StreamProfile(width=64, height=64, fps=30)
    push = RecordingPushFixture()
    waits: list[float] = []
    service = MediaRunService(
        artifact_store=MemoryArtifactStore(),
        video_adapter=PodInjectorVideoAdapter(client),
        video_source_resolver=PodFixtureVideoResolver(
            decoder=make_video_decoder(frames, profile),
            client=client,
            push_fixture=push,
        ),
        audio_adapter=PodInjectorAudioAdapter(client),
        audio_source_resolver=(
            PodFixtureAudioResolver(
                decoder=make_audio_decoder(audio_sources or {}),
                client=client,
                push_fixture=push,
            )
            if audio_sources is not None
            else None
        ),
        capabilities=pod_capabilities(),
        wait_sleep=waits.append,
    )
    return service, push, waits


def test_media_run_end_to_end_video_hot_switch(injector, client):
    frames = {"source-a": [b"a0", b"a1"], "source-b": [b"b0", b"b1"]}
    service, push, waits = build_pod_service(injector, client, frames=frames)
    request = {
        "schema_version": "prismloop.media-run-request.v1",
        "request_id": "media-req-pod-001",
        "idempotency_key": "pod-task-001",
        "environment_ref": "cloud-phone/pod-injector",
        "artifact_store_ref": "memory/prismloop-media",
        "inputs": [
            video_input("source-a", "a" * 64),
            video_input("source-b", "b" * 64),
        ],
        "camera_streams": [
            {
                "stream_id": "rear-camera",
                "initial_input_id": "source-a",
                "profile": {"width": 64, "height": 64, "fps": 30, "pixel_format": "yuv420p"},
                "max_interframe_gap_ms": 67,
            }
        ],
        "sequence": [
            {"step_id": "stage-a", "action": "input.stage", "input_id": "source-a"},
            {"step_id": "stage-b", "action": "input.stage", "input_id": "source-b"},
            {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
            {"step_id": "first-segment", "action": "wait", "duration_ms": 100},
            {"step_id": "switch", "action": "camera.stream.switch", "stream_id": "rear-camera", "input_id": "source-b"},
            {"step_id": "second-segment", "action": "wait", "duration_ms": 100},
            {"step_id": "close", "action": "camera.stream.close", "stream_id": "rear-camera"},
        ],
    }

    run_id = service.submit(request)
    result = service.get(run_id)

    assert result["status"] == "completed", result.get("error")
    # 编排映射:open 探活 → wait 触发 sequence(A) → switch 热切换 sequence(B) → close 停止
    assert injector.calls == [
        ("status",),
        ("camera_sequence", "/data/local/tmp/prismloop/fixtures/source-a-aaaaaaaa", 30),
        ("camera_sequence", "/data/local/tmp/prismloop/fixtures/source-b-bbbbbbbb", 30),
        ("camera_stop",),
    ]
    assert waits == [0.1, 0.1]  # wait 真实时钟钩子
    assert [item["remote_dir"] for item in push.pushed] == [
        "/data/local/tmp/prismloop/fixtures/source-a-aaaaaaaa",
        "/data/local/tmp/prismloop/fixtures/source-b-bbbbbbbb",
    ]
    receipt = result["stream_receipts"][0]
    assert receipt["source_switch_count"] == 1
    assert receipt["source_history"] == ["source-a", "source-b"]
    assert receipt["emitted_frame_count"] == 6  # 2×wait,每次 3 帧(30fps×100ms)
    assert receipt["source_session_restarts"] == 0
    assert receipt["discontinuity_count"] == 0


def test_media_run_end_to_end_audio_loop(injector, client):
    profile = AudioProfile(sample_rate_hz=48000, channels=2)
    tone = IterableAudioSource(profile, [b"\x01" * profile.frame_bytes])
    service, push, waits = build_pod_service(
        injector, client, frames={}, audio_sources={"tone": tone}
    )
    request = {
        "schema_version": "prismloop.media-run-request.v1",
        "request_id": "media-req-pod-002",
        "idempotency_key": "pod-task-002",
        "environment_ref": "cloud-phone/pod-injector",
        "artifact_store_ref": "memory/prismloop-media",
        "inputs": [microphone_input("tone", "c" * 64)],
        "sequence": [
            {"step_id": "stage", "action": "input.stage", "input_id": "tone"},
            {"step_id": "start", "action": "input.start", "input_id": "tone"},
            {"step_id": "hold", "action": "wait", "duration_ms": 100},
            {"step_id": "stop", "action": "input.stop", "input_id": "tone"},
        ],
    }

    run_id = service.submit(request)
    result = service.get(run_id)

    assert result["status"] == "completed", result.get("error")
    assert injector.calls == [
        ("audio_file", "/data/local/tmp/prismloop/fixtures/tone-cccccccc/audio.pcm", 48000, 2),
        ("audio_stop",),
    ]
    assert waits == [0.1]
    assert push.pushed[0]["remote_dir"] == "/data/local/tmp/prismloop/fixtures/tone-cccccccc"


def test_media_run_reports_error_when_injector_unreachable(client):
    dead_client = PodInjectorClient(host="127.0.0.1", port=1, timeout_seconds=0.5)
    profile = StreamProfile(width=64, height=64, fps=30)
    service = MediaRunService(
        artifact_store=MemoryArtifactStore(),
        video_adapter=PodInjectorVideoAdapter(dead_client),
        video_source_resolver=PodFixtureVideoResolver(
            decoder=make_video_decoder({"source-a": [b"a0"]}, profile),
            client=dead_client,
            push_fixture=lambda local, remote: remote,
        ),
        capabilities=pod_capabilities(),
    )
    request = {
        "schema_version": "prismloop.media-run-request.v1",
        "request_id": "media-req-pod-003",
        "idempotency_key": "pod-task-003",
        "environment_ref": "cloud-phone/pod-injector",
        "artifact_store_ref": "memory/prismloop-media",
        "inputs": [video_input("source-a", "a" * 64)],
        "camera_streams": [
            {
                "stream_id": "rear-camera",
                "initial_input_id": "source-a",
                "profile": {"width": 64, "height": 64, "fps": 30, "pixel_format": "yuv420p"},
                "max_interframe_gap_ms": 67,
            }
        ],
        "sequence": [
            {"step_id": "stage-a", "action": "input.stage", "input_id": "source-a"},
            {"step_id": "open", "action": "camera.stream.open", "stream_id": "rear-camera"},
        ],
    }

    run_id = service.submit(request)
    result = service.get(run_id)
    assert result["status"] == "error"
    assert "injector unreachable" in result["error"]
