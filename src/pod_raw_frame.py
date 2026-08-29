"""Pod raw-frame injection boundary.

The official SDK transport is intentionally not guessed here.  A verified
native/official bridge implements ``PodRawFrameTransport``; this adapter only
enforces the Harness lifecycle and frame metadata at that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol

from .media_io import ExternalVideoAdapter, StreamProfile, VideoFrame
from .audio_io import AudioFrame, AudioProfile, ExternalAudioAdapter


class PodRawFrameTransport(Protocol):
    def open_video(self, stream_id: str, profile: StreamProfile) -> str:
        ...

    def push_video(
        self, session_id: str, payload: bytes, *, width: int, height: int, pixel_format: str, sequence_number: int, timestamp_us: int
    ) -> None:
        ...

    def close_video(self, session_id: str) -> None:
        ...

    def open_audio(self, source_id: str, profile: AudioProfile) -> str:
        ...

    def push_audio(
        self, session_id: str, payload: bytes, *, sample_rate_hz: int, channels: int, sample_format: str, sequence_number: int, timestamp_us: int
    ) -> None:
        ...

    def close_audio(self, session_id: str) -> None:
        ...


class PodRawFrameAdapter(ExternalVideoAdapter):
    """Maps continuous harness video frames to a verified Pod raw-frame bridge."""

    def __init__(self, transport: PodRawFrameTransport) -> None:
        self._transport = transport

    def open_external_video(self, stream_id: str, profile: StreamProfile) -> str:
        return self._transport.open_video(stream_id, profile)

    def push_video_frame(
        self, session_id: str, frame: VideoFrame, *, sequence_number: int, timestamp_us: int
    ) -> None:
        self._transport.push_video(
            session_id,
            frame.payload,
            width=frame.profile.width,
            height=frame.profile.height,
            pixel_format=frame.profile.pixel_format,
            sequence_number=sequence_number,
            timestamp_us=timestamp_us,
        )

    def close_external_video(self, session_id: str) -> None:
        self._transport.close_video(session_id)


class PodRawAudioAdapter(ExternalAudioAdapter):
    """Maps independently clocked PCM microphone frames to the Pod bridge."""

    def __init__(self, transport: PodRawFrameTransport) -> None:
        self._transport = transport

    def open_external_audio(self, source_id: str, profile: AudioProfile) -> str:
        return self._transport.open_audio(source_id, profile)

    def push_audio_frame(
        self, session_id: str, frame: AudioFrame, *, sequence_number: int, timestamp_us: int
    ) -> None:
        self._transport.push_audio(
            session_id,
            frame.payload,
            sample_rate_hz=frame.profile.sample_rate_hz,
            channels=frame.profile.channels,
            sample_format=frame.profile.sample_format,
            sequence_number=sequence_number,
            timestamp_us=timestamp_us,
        )

    def close_external_audio(self, session_id: str) -> None:
        self._transport.close_audio(session_id)


@dataclass
class RecordingPodRawFrameTransport:
    """Protocol fixture for adapter tests, never a cloud-phone implementation."""

    opens: List[tuple[str, StreamProfile]]
    frames: List[tuple[str, int, int, bytes]]
    closes: List[str]

    def __init__(self) -> None:
        self.opens = []
        self.frames = []
        self.closes = []

    def open_video(self, stream_id: str, profile: StreamProfile) -> str:
        self.opens.append((stream_id, profile))
        return f"pod-raw-session-{len(self.opens)}"

    def push_video(
        self, session_id: str, payload: bytes, *, width: int, height: int, pixel_format: str, sequence_number: int, timestamp_us: int
    ) -> None:
        self.frames.append((session_id, sequence_number, timestamp_us, payload))

    def close_video(self, session_id: str) -> None:
        self.closes.append(session_id)

    def open_audio(self, source_id: str, profile: AudioProfile) -> str:
        return f"pod-raw-audio-session-{len(self.opens) + 1}"

    def push_audio(
        self, session_id: str, payload: bytes, *, sample_rate_hz: int, channels: int, sample_format: str, sequence_number: int, timestamp_us: int
    ) -> None:
        return None

    def close_audio(self, session_id: str) -> None:
        return None
