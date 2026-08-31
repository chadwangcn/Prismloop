"""Pod injector HTTP 控制接入(MediaRunService 编排层)。

依据 `architecture/11-Harness媒体输入输出服务.md` §5.1:
- 编排层 12 种 action 契约不变,本模块把 Pod 内 injector 的 HTTP 控制通道
  (127.0.0.1:18080,经 adb forward)适配为 ExternalVideoAdapter / ExternalAudioAdapter
  与 VideoSourceResolver / AudioSourceResolver 协议实现。
- 帧节奏由 Pod 内 FrameFeeder 自驱(PoC 实测 33ms/帧),编排层 push 只做
  receipt 元数据记录,不产生逐帧 HTTP。
- fixture 播放源 lazy 启动:首次被消费时 POST /camera/sequence(启动/热切换)
  或 POST /audio/file(循环注入)。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, List, Mapping, Protocol, Sequence

from .audio_io import AudioFrameSource, AudioProfile, ExternalAudioAdapter
from .media_io import ExternalVideoAdapter, StreamProfile, VideoFrame, VideoFrameSource

INJECTOR_DEFAULT_HOST = "127.0.0.1"
INJECTOR_DEFAULT_PORT = 18080
POD_FIXTURE_ROOT = "/data/local/tmp/prismloop/fixtures"

# injector FrameFeeder 消费的像素格式:StreamProfile.pixel_format → manifest format
_POD_PIXEL_FORMATS = {"yuv420p": "i420", "rgba": "rgba"}


class InjectorUnavailableError(RuntimeError):
    """injector HTTP 通道不可达(连接失败/超时)。"""


class InjectorCommandError(RuntimeError):
    """injector 拒绝了控制命令(HTTP 200 body 携带 {"error":...})。"""


class InjectorProtocolError(RuntimeError):
    """injector 返回了无法解析的非 JSON 应答。"""


class PodInjectorClient:
    """Pod 内 injector HTTP 控制通道客户端。

    injector 的 HttpApi 恒以 HTTP 200 应答,命令错误在 JSON body 的 ``error``
    字段中;因此本客户端以 body 解析为准,而非状态码。

    线程约定:与 MediaRunService worker 一致,单线程串行调用。
    """

    def __init__(
        self,
        *,
        host: str = INJECTOR_DEFAULT_HOST,
        port: int = INJECTOR_DEFAULT_PORT,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds
        # lazy 启动语义的状态跟踪:stop 后清空,保证重开/回切重新下发
        self.current_camera_dir: str | None = None
        self.current_audio_path: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def status(self) -> Mapping[str, Any]:
        return self._request("GET", "/status")

    def camera_sequence(self, pod_dir: str, fps: int) -> Mapping[str, Any]:
        """开始播放或无断流热切换帧序列(FrameFeeder 不停循环)。"""
        result = self._request("POST", "/camera/sequence", {"dir": pod_dir, "fps": fps})
        self.current_camera_dir = pod_dir
        return result

    def camera_stop(self) -> Mapping[str, Any]:
        result = self._request("POST", "/camera/stop")
        self.current_camera_dir = None
        return result

    def audio_file(self, pod_path: str, sample_rate_hz: int, channels: int) -> Mapping[str, Any]:
        """PCM 文件循环注入(官方 recordByFile TYPE_CIRCLE)。"""
        result = self._request(
            "POST", "/audio/file", {"path": pod_path, "sampleRate": sample_rate_hz, "channels": channels}
        )
        self.current_audio_path = pod_path
        return result

    def audio_stop(self) -> Mapping[str, Any]:
        result = self._request("POST", "/audio/stop")
        self.current_audio_path = None
        return result

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise InjectorUnavailableError(f"injector unreachable at {self.base_url}{path}: {exc}") from exc
        except TimeoutError as exc:
            raise InjectorUnavailableError(f"injector timeout at {self.base_url}{path}") from exc
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InjectorProtocolError(f"injector returned non-JSON body: {payload[:120]!r}") from exc
        if not isinstance(parsed, dict):
            raise InjectorProtocolError(f"injector returned unexpected body: {payload[:120]!r}")
        if "error" in parsed:
            raise InjectorCommandError(f"injector rejected {path}: {parsed['error']}")
        return parsed


class PodVideoFixtureSource:
    """Pod 上 fixture 的播放源:首次消费时 lazy 下发 /camera/sequence。

    逻辑帧序列用于编排层 receipt 与 profile 校验;真实帧节奏由 Pod 内
    FrameFeeder 保证。读尽时抛 StopIteration,由 VideoSourceRouter 按
    end_of_source_policy 处理。
    """

    def __init__(self, *, pod_dir: str, profile: StreamProfile, frames: Sequence[bytes], client: PodInjectorClient) -> None:
        if not frames:
            raise ValueError("a pod video fixture must contain at least one frame")
        self.pod_dir = pod_dir
        self._profile = profile
        self._frames = list(frames)
        self._client = client
        self._position = 0

    @property
    def profile(self) -> StreamProfile:
        return self._profile

    def next_frame(self) -> VideoFrame:
        if self._client.current_camera_dir != self.pod_dir:
            self._client.camera_sequence(self.pod_dir, self._profile.fps)
        if self._position >= len(self._frames):
            raise StopIteration
        payload = self._frames[self._position]
        self._position += 1
        return VideoFrame(payload=payload, profile=self._profile)

    def rewind(self) -> None:
        self._position = 0


class PodAudioFixtureSource:
    """Pod 上 PCM fixture 的播放源:首次消费时 lazy 下发 /audio/file。"""

    def __init__(self, *, pod_path: str, source: AudioFrameSource, client: PodInjectorClient) -> None:
        self.pod_path = pod_path
        self._source = source
        self._client = client

    @property
    def profile(self) -> AudioProfile:
        return self._source.profile

    def next_frame(self):
        if self._client.current_audio_path != self.pod_path:
            profile = self._source.profile
            self._client.audio_file(self.pod_path, profile.sample_rate_hz, profile.channels)
        return self._source.next_frame()

    def rewind(self) -> None:
        self._source.rewind()


class PodInjectorVideoAdapter(ExternalVideoAdapter):
    """视频注入 adapter:生命周期映射到 injector HTTP 控制。

    帧数据不逐帧下发(帧节奏在 Pod 内),push_video_frame 仅维持协议。
    """

    def __init__(self, client: PodInjectorClient) -> None:
        self._client = client
        self._sessions = 0

    def open_external_video(self, stream_id: str, profile: StreamProfile) -> str:
        self._client.status()  # 探活:injector 不可达时 run 立即 error
        self._sessions += 1
        return f"pod-injector-video-{self._sessions}"

    def push_video_frame(
        self,
        session_id: str,
        frame: VideoFrame,
        *,
        sequence_number: int,
        timestamp_us: int,
    ) -> None:
        return None  # 帧由 Pod 内 FrameFeeder 自驱推送

    def close_external_video(self, session_id: str) -> None:
        self._client.camera_stop()


class PodInjectorAudioAdapter(ExternalAudioAdapter):
    """音频注入 adapter:close 映射 /audio/stop,启动由 fixture 源 lazy 触发。"""

    def __init__(self, client: PodInjectorClient) -> None:
        self._client = client
        self._sessions = 0

    def open_external_audio(self, source_id: str, profile: AudioProfile) -> str:
        self._sessions += 1
        return f"pod-injector-audio-{self._sessions}"

    def push_audio_frame(self, session_id: str, frame, *, sequence_number: int, timestamp_us: int) -> None:
        return None  # PCM 由 recordByFile(TYPE_CIRCLE) 在 Pod 内循环

    def close_external_audio(self, session_id: str) -> None:
        self._client.audio_stop()


class VideoFrameDecoder(Protocol):
    """把已校验的 camera.video artifact 解码为归一化帧源。"""

    def resolve(self, input_spec: Mapping[str, Any], profile: StreamProfile) -> VideoFrameSource:
        ...


class AudioFrameDecoder(Protocol):
    """把已校验的 microphone.* artifact 解码为固定时长 PCM 帧源。"""

    def resolve(self, input_spec: Mapping[str, Any]) -> AudioFrameSource:
        ...


PushFixture = Callable[[str, str], str]
"""把本地 fixture 目录内容放置到 Pod 的 remote_dir,返回 remote_dir。"""


def make_adb_push_fixture(adb_target: str, *, adb_path: str = "adb", timeout: int = 600) -> PushFixture:
    """基于 adb push 的 PushFixture 实现(adb forward 已建立的前提)。"""

    def push_fixture(local_dir: str, remote_dir: str) -> str:
        subprocess.run(
            [adb_path, "-s", adb_target, "shell", f"mkdir -p {remote_dir}"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            [adb_path, "-s", adb_target, "push", f"{local_dir}/.", remote_dir],
            check=True, capture_output=True, text=True, timeout=timeout,
        )
        return remote_dir

    return push_fixture


class PodFixtureVideoResolver:
    """input.stage 阶段:解码 → 写 Pod fixture(manifest.json+video.bin) → adb push。

    不启动注入;启动由返回的 PodVideoFixtureSource 在首次消费时 lazy 触发。
    """

    def __init__(
        self,
        *,
        decoder: VideoFrameDecoder,
        client: PodInjectorClient,
        push_fixture: PushFixture,
        remote_root: str = POD_FIXTURE_ROOT,
        staging_dir: str | None = None,
    ) -> None:
        self._decoder = decoder
        self._client = client
        self._push_fixture = push_fixture
        self._remote_root = remote_root.rstrip("/")
        self._staging_dir = staging_dir

    def resolve(self, input_spec: Mapping[str, Any], profile: StreamProfile) -> PodVideoFixtureSource:
        if input_spec.get("kind") != "camera.video":
            raise ValueError("PodFixtureVideoResolver only accepts camera.video inputs")
        manifest_format = _POD_PIXEL_FORMATS.get(profile.pixel_format)
        if manifest_format is None:
            raise ValueError(
                f"pod injector only supports yuv420p/rgba fixtures, got: {profile.pixel_format}"
            )
        decoded = self._decoder.resolve(input_spec, profile)
        frames: List[bytes] = []
        while True:
            try:
                frames.append(decoded.next_frame().payload)
            except StopIteration:
                break
        if not frames:
            raise ValueError("decoded fixture contains no frames")

        input_id = str(input_spec["input_id"])
        digest = str(input_spec["artifact"]["sha256"])[:8]
        remote_dir = f"{self._remote_root}/{input_id}-{digest}"
        with tempfile.TemporaryDirectory(dir=self._staging_dir, prefix="prismloop-pod-fixture-") as temp:
            fixture_dir = Path(temp) / "fixture"
            fixture_dir.mkdir()
            (fixture_dir / "video.bin").write_bytes(b"".join(frames))
            manifest = {
                "width": profile.width,
                "height": profile.height,
                "format": manifest_format,
                "fps": profile.fps,
                "file": "video.bin",
                "frames": len(frames),
            }
            (fixture_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            self._push_fixture(str(fixture_dir), remote_dir)
        return PodVideoFixtureSource(
            pod_dir=remote_dir, profile=profile, frames=frames, client=self._client
        )


class PodFixtureAudioResolver:
    """input.stage 阶段:PCM 解码 → adb push;启动由 PodAudioFixtureSource lazy 触发。"""

    def __init__(
        self,
        *,
        decoder: AudioFrameDecoder,
        client: PodInjectorClient,
        push_fixture: PushFixture,
        remote_root: str = POD_FIXTURE_ROOT,
        staging_dir: str | None = None,
    ) -> None:
        self._decoder = decoder
        self._client = client
        self._push_fixture = push_fixture
        self._remote_root = remote_root.rstrip("/")
        self._staging_dir = staging_dir

    def resolve(self, input_spec: Mapping[str, Any]) -> PodAudioFixtureSource:
        kind = input_spec.get("kind")
        if kind not in {"microphone.pcm", "microphone.audio"}:
            raise ValueError("PodFixtureAudioResolver only accepts microphone inputs")
        decoded = self._decoder.resolve(input_spec)
        pcm = bytearray()
        while True:
            try:
                pcm += decoded.next_frame().payload
            except StopIteration:
                break
        if not pcm:
            raise ValueError("decoded microphone fixture is empty")
        decoded.rewind()  # 播放源复用同一迭代器,恢复到首帧

        input_id = str(input_spec["input_id"])
        digest = str(input_spec["artifact"]["sha256"])[:8]
        remote_dir = f"{self._remote_root}/{input_id}-{digest}"
        with tempfile.TemporaryDirectory(dir=self._staging_dir, prefix="prismloop-pod-fixture-") as temp:
            fixture_dir = Path(temp) / "fixture"
            fixture_dir.mkdir()
            (fixture_dir / "audio.pcm").write_bytes(bytes(pcm))
            self._push_fixture(str(fixture_dir), remote_dir)
        return PodAudioFixtureSource(
            pod_path=f"{remote_dir}/audio.pcm", source=decoded, client=self._client
        )
