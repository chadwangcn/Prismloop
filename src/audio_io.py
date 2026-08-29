"""Provider-neutral continuous PCM microphone primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Protocol


@dataclass(frozen=True)
class AudioProfile:
    sample_rate_hz: int
    channels: int
    sample_format: str = "s16le"
    frame_duration_ms: int = 20

    def __post_init__(self) -> None:
        if not 8000 <= self.sample_rate_hz <= 192000:
            raise ValueError("sample_rate_hz must be between 8000 and 192000")
        if self.channels not in {1, 2}:
            raise ValueError("channels must be 1 or 2")
        if self.sample_format not in {"s16le", "f32le"}:
            raise ValueError("unsupported sample_format")
        if not 5 <= self.frame_duration_ms <= 100:
            raise ValueError("frame_duration_ms must be between 5 and 100")

    @property
    def frame_bytes(self) -> int:
        bytes_per_sample = 2 if self.sample_format == "s16le" else 4
        return self.sample_rate_hz * self.channels * bytes_per_sample * self.frame_duration_ms // 1000


@dataclass(frozen=True)
class AudioFrame:
    payload: bytes
    profile: AudioProfile


class AudioFrameSource(Protocol):
    @property
    def profile(self) -> AudioProfile:
        ...

    def next_frame(self) -> AudioFrame:
        ...

    def rewind(self) -> None:
        ...


class ExternalAudioAdapter(Protocol):
    def open_external_audio(self, source_id: str, profile: AudioProfile) -> str:
        ...

    def push_audio_frame(self, session_id: str, frame: AudioFrame, *, sequence_number: int, timestamp_us: int) -> None:
        ...

    def close_external_audio(self, session_id: str) -> None:
        ...


class IterableAudioSource:
    def __init__(self, profile: AudioProfile, frames: Iterable[bytes]) -> None:
        self._profile = profile
        self._frames = list(frames)
        if not self._frames:
            raise ValueError("an audio source must contain at least one frame")
        if any(len(frame) != profile.frame_bytes for frame in self._frames):
            raise ValueError("audio frame does not match profile frame size")
        self._position = 0

    @property
    def profile(self) -> AudioProfile:
        return self._profile

    def next_frame(self) -> AudioFrame:
        if self._position >= len(self._frames):
            raise StopIteration
        frame = AudioFrame(self._frames[self._position], self._profile)
        self._position += 1
        return frame

    def rewind(self) -> None:
        self._position = 0


class AudioSourceRouter:
    """Keeps a microphone session alive while emitting deterministic PCM frames."""

    def __init__(self, adapter: ExternalAudioAdapter) -> None:
        self._adapter = adapter
        self._sessions: dict[str, tuple[str, AudioProfile, AudioFrameSource, int, bool]] = {}

    def open(self, source_id: str, source: AudioFrameSource) -> str:
        if source_id in self._sessions and not self._sessions[source_id][4]:
            raise ValueError(f"microphone source already open: {source_id}")
        session_id = self._adapter.open_external_audio(source_id, source.profile)
        self._sessions[source_id] = (session_id, source.profile, source, 0, False)
        return session_id

    def pump(self, source_id: str, timestamp_us: int) -> None:
        session_id, profile, source, sequence_number, closed = self._sessions[source_id]
        if closed:
            raise ValueError(f"microphone source is closed: {source_id}")
        try:
            frame = source.next_frame()
        except StopIteration:
            source.rewind()
            frame = source.next_frame()
        if frame.profile != profile:
            raise ValueError("audio source profile changed while microphone session was open")
        self._adapter.push_audio_frame(session_id, frame, sequence_number=sequence_number, timestamp_us=timestamp_us)
        self._sessions[source_id] = (session_id, profile, source, sequence_number + 1, False)

    def close(self, source_id: str) -> None:
        session_id, profile, source, sequence_number, closed = self._sessions[source_id]
        if not closed:
            self._adapter.close_external_audio(session_id)
            self._sessions[source_id] = (session_id, profile, source, sequence_number, True)


class RecordingExternalAudioAdapter:
    def __init__(self) -> None:
        self.opens: List[tuple[str, AudioProfile]] = []
        self.frames: List[tuple[str, int, int, bytes]] = []
        self.closes: List[str] = []

    def open_external_audio(self, source_id: str, profile: AudioProfile) -> str:
        self.opens.append((source_id, profile))
        return f"audio-session-{len(self.opens)}"

    def push_audio_frame(self, session_id: str, frame: AudioFrame, *, sequence_number: int, timestamp_us: int) -> None:
        self.frames.append((session_id, sequence_number, timestamp_us, frame.payload))

    def close_external_audio(self, session_id: str) -> None:
        self.closes.append(session_id)
