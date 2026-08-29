"""Client-SDK external-source injection boundary.

Volc cloud phones expose external camera and microphone injection through a
client SDK session, not through the ACEP management API or ADB. The Harness
therefore talks to a privately configured client gateway. The gateway owns
the vendor SDK session and any short-lived credentials; this module receives
only standardised frames and opaque session ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol

from .audio_io import AudioFrame, AudioProfile, ExternalAudioAdapter
from .media_io import ExternalVideoAdapter, StreamProfile, VideoFrame


class ClientExternalFrameGateway(Protocol):
    """Private bridge to a verified vendor client-SDK runtime.

    Implementations must set source type once at session open, preserve its
    MediaStreamTrack, and keep Pod ids, STS material, and temporary URLs out
    of public MediaRun data and evidence artifacts.
    """

    def open_video(self, stream_id: str, profile: StreamProfile) -> str: ...

    def push_video(
        self, session_id: str, payload: bytes, *, width: int, height: int,
        pixel_format: str, sequence_number: int, timestamp_us: int,
    ) -> None: ...

    def close_video(self, session_id: str) -> None: ...

    def open_audio(self, source_id: str, profile: AudioProfile) -> str: ...

    def push_audio(
        self, session_id: str, payload: bytes, *, sample_rate_hz: int,
        channels: int, sample_format: str, sequence_number: int, timestamp_us: int,
    ) -> None: ...

    def close_audio(self, session_id: str) -> None: ...


class ClientExternalVideoAdapter(ExternalVideoAdapter):
    """Forwards continuous camera frames to the managed client gateway."""

    def __init__(self, gateway: ClientExternalFrameGateway) -> None:
        self._gateway = gateway

    def open_external_video(self, stream_id: str, profile: StreamProfile) -> str:
        return self._gateway.open_video(stream_id, profile)

    def push_video_frame(
        self, session_id: str, frame: VideoFrame, *, sequence_number: int, timestamp_us: int
    ) -> None:
        self._gateway.push_video(
            session_id, frame.payload, width=frame.profile.width, height=frame.profile.height,
            pixel_format=frame.profile.pixel_format, sequence_number=sequence_number, timestamp_us=timestamp_us,
        )

    def close_external_video(self, session_id: str) -> None:
        self._gateway.close_video(session_id)


class ClientExternalAudioAdapter(ExternalAudioAdapter):
    """Forwards independently clocked PCM microphone frames to the gateway."""

    def __init__(self, gateway: ClientExternalFrameGateway) -> None:
        self._gateway = gateway

    def open_external_audio(self, source_id: str, profile: AudioProfile) -> str:
        return self._gateway.open_audio(source_id, profile)

    def push_audio_frame(
        self, session_id: str, frame: AudioFrame, *, sequence_number: int, timestamp_us: int
    ) -> None:
        self._gateway.push_audio(
            session_id, frame.payload, sample_rate_hz=frame.profile.sample_rate_hz,
            channels=frame.profile.channels, sample_format=frame.profile.sample_format,
            sequence_number=sequence_number, timestamp_us=timestamp_us,
        )

    def close_external_audio(self, session_id: str) -> None:
        self._gateway.close_audio(session_id)


@dataclass
class RecordingClientExternalFrameGateway:
    """Protocol fixture only; it is not a cloud-phone SDK implementation."""

    video_opens: List[tuple[str, StreamProfile]]
    video_frames: List[tuple[str, int, int, bytes]]
    video_closes: List[str]
    audio_opens: List[tuple[str, AudioProfile]]
    audio_frames: List[tuple[str, int, int, bytes]]
    audio_closes: List[str]

    def __init__(self) -> None:
        self.video_opens, self.video_frames, self.video_closes = [], [], []
        self.audio_opens, self.audio_frames, self.audio_closes = [], [], []

    def open_video(self, stream_id: str, profile: StreamProfile) -> str:
        self.video_opens.append((stream_id, profile))
        return f"client-video-session-{len(self.video_opens)}"

    def push_video(self, session_id: str, payload: bytes, **metadata: int | str) -> None:
        self.video_frames.append((session_id, int(metadata["sequence_number"]), int(metadata["timestamp_us"]), payload))

    def close_video(self, session_id: str) -> None:
        self.video_closes.append(session_id)

    def open_audio(self, source_id: str, profile: AudioProfile) -> str:
        self.audio_opens.append((source_id, profile))
        return f"client-audio-session-{len(self.audio_opens)}"

    def push_audio(self, session_id: str, payload: bytes, **metadata: int | str) -> None:
        self.audio_frames.append((session_id, int(metadata["sequence_number"]), int(metadata["timestamp_us"]), payload))

    def close_audio(self, session_id: str) -> None:
        self.audio_closes.append(session_id)
