"""Verified artifact staging and ffmpeg normalization for video fixtures."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .audio_io import AudioProfile, IterableAudioSource
from .media_artifacts import BinaryArtifactStore
from .media_io import IterableVideoSource, StreamProfile


class MediaNormalizationError(RuntimeError):
    """A fixture could not be decoded into the stream's immutable profile."""


class ArtifactVideoSourceResolver:
    """Downloads a verified video artifact and normalizes it to raw video frames.

    Normalized frames are held only for the active run.  Their source artifact
    remains immutable in TOS; callers cannot inject a local file path or a
    transient signed URL through the public request contract.
    """

    def __init__(
        self, artifact_store: BinaryArtifactStore, *, staging_dir: str | None = None, ffmpeg_path: str = "ffmpeg"
    ) -> None:
        self._artifact_store = artifact_store
        self._staging_dir = staging_dir
        self._ffmpeg_path = ffmpeg_path

    def resolve(self, input_spec: Mapping[str, Any], profile: StreamProfile) -> IterableVideoSource:
        if input_spec.get("kind") != "camera.video":
            raise MediaNormalizationError("ArtifactVideoSourceResolver only accepts camera.video inputs")
        artifact = input_spec["artifact"]
        payload = self._artifact_store.get_bytes(artifact)
        suffix = self._suffix_for(str(artifact.get("media_type", "")))
        with tempfile.TemporaryDirectory(dir=self._staging_dir, prefix="prismloop-video-") as temp_dir:
            fixture_path = Path(temp_dir) / f"fixture{suffix}"
            fixture_path.write_bytes(payload)
            raw = self._decode(fixture_path, profile)
        frame_size = self._frame_size(profile)
        if not raw or len(raw) % frame_size != 0:
            raise MediaNormalizationError("ffmpeg did not produce a complete normalized raw video frame")
        return IterableVideoSource(profile, [raw[offset : offset + frame_size] for offset in range(0, len(raw), frame_size)])

    def _decode(self, fixture_path: Path, profile: StreamProfile) -> bytes:
        pixel_format = profile.pixel_format
        # hflip,vflip:注入链路补偿。PodRawFrame 链路(SDK putVideoFrame → 虚拟摄像头 →
        # 消费端按 sensorOrientation 旋转显示)整体给推送帧叠加 180° 旋转
        # (四象限图案实验验证,与流尺寸无关),预旋转 180° 使画面正立。
        command = [
            self._ffmpeg_path,
            "-v", "error",
            "-i", str(fixture_path),
            "-an",
            "-vf", f"hflip,vflip,scale={profile.width}:{profile.height}:flags=lanczos,fps={profile.fps}",
            "-pix_fmt", pixel_format,
            "-f", "rawvideo",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except FileNotFoundError as exc:
            raise MediaNormalizationError(f"ffmpeg not found: {self._ffmpeg_path}") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.decode("utf-8", errors="replace").strip()
            raise MediaNormalizationError(f"ffmpeg normalization failed: {message}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaNormalizationError("ffmpeg normalization timed out") from exc
        return completed.stdout

    @staticmethod
    def _frame_size(profile: StreamProfile) -> int:
        pixels = profile.width * profile.height
        if profile.pixel_format in {"yuv420p", "nv12"}:
            return pixels * 3 // 2
        if profile.pixel_format == "rgba":
            return pixels * 4
        raise MediaNormalizationError(f"unsupported pixel format: {profile.pixel_format}")

    @staticmethod
    def _suffix_for(media_type: str) -> str:
        return {
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }.get(media_type, ".media")


class ArtifactAudioSourceResolver:
    """Stages verified PCM/WAV/AAC artifacts into fixed-duration PCM frames."""

    def __init__(
        self, artifact_store: BinaryArtifactStore, *, staging_dir: str | None = None, ffmpeg_path: str = "ffmpeg"
    ) -> None:
        self._artifact_store = artifact_store
        self._staging_dir = staging_dir
        self._ffmpeg_path = ffmpeg_path

    def resolve(self, input_spec: Mapping[str, Any]) -> IterableAudioSource:
        kind = input_spec.get("kind")
        if kind not in {"microphone.pcm", "microphone.audio"}:
            raise MediaNormalizationError("ArtifactAudioSourceResolver only accepts microphone inputs")
        profile = AudioProfile(**input_spec["audio_format"])
        artifact = input_spec["artifact"]
        payload = self._artifact_store.get_bytes(artifact)
        if kind == "microphone.audio":
            payload = self._decode_audio(payload, str(artifact.get("media_type", "")), profile)
        if not payload:
            raise MediaNormalizationError("microphone source is empty")
        frames = []
        for offset in range(0, len(payload), profile.frame_bytes):
            frame = payload[offset : offset + profile.frame_bytes]
            if len(frame) < profile.frame_bytes:
                frame += b"\x00" * (profile.frame_bytes - len(frame))
            frames.append(frame)
        return IterableAudioSource(profile, frames)

    def _decode_audio(self, payload: bytes, media_type: str, profile: AudioProfile) -> bytes:
        suffix = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac", "audio/mp4": ".m4a"}.get(
            media_type, ".audio"
        )
        with tempfile.TemporaryDirectory(dir=self._staging_dir, prefix="prismloop-audio-") as temp_dir:
            fixture_path = Path(temp_dir) / f"fixture{suffix}"
            fixture_path.write_bytes(payload)
            command = [
                self._ffmpeg_path,
                "-v", "error",
                "-i", str(fixture_path),
                "-ac", str(profile.channels),
                "-ar", str(profile.sample_rate_hz),
                "-f", profile.sample_format,
                "pipe:1",
            ]
            try:
                completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            except FileNotFoundError as exc:
                raise MediaNormalizationError(f"ffmpeg not found: {self._ffmpeg_path}") from exc
            except subprocess.CalledProcessError as exc:
                message = exc.stderr.decode("utf-8", errors="replace").strip()
                raise MediaNormalizationError(f"ffmpeg audio normalization failed: {message}") from exc
        return completed.stdout
