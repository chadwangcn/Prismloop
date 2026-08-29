"""Provider-neutral continuous media I/O primitives.

The module deliberately contains no product, UI, APK, or test-case concept.
It keeps one external virtual-camera session alive while switching the content
source that supplies its frames.  A cloud-provider adapter implements the
small ``ExternalVideoAdapter`` protocol using its client external-source SDK.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Protocol


class MediaIOError(RuntimeError):
    """Base error for media I/O execution."""


class StreamNotFound(MediaIOError):
    """Raised when a stream id was not opened."""


class SourceNotStaged(MediaIOError):
    """Raised when a requested content source is absent."""


class ProfileMismatch(MediaIOError):
    """Raised when a source cannot be emitted by an opened camera stream."""


@dataclass(frozen=True)
class StreamProfile:
    """The immutable output profile of one virtual camera session."""

    width: int
    height: int
    fps: int
    pixel_format: str = "yuv420p"

    def __post_init__(self) -> None:
        if self.width < 16 or self.height < 16:
            raise ValueError("stream dimensions must be at least 16 pixels")
        if not 1 <= self.fps <= 120:
            raise ValueError("fps must be between 1 and 120")
        if self.pixel_format not in {"yuv420p", "nv12", "rgba"}:
            raise ValueError("unsupported pixel_format")

    @property
    def frame_interval_ns(self) -> int:
        return round(1_000_000_000 / self.fps)


@dataclass(frozen=True)
class VideoFrame:
    """One normalized raw video frame.

    ``payload`` is deliberately opaque to the router.  The provider adapter
    decides whether it receives I420, NV12, RGBA, or another profile-approved
    representation.
    """

    payload: bytes
    profile: StreamProfile


class VideoFrameSource(Protocol):
    """A pre-staged, normalized source of raw video frames."""

    @property
    def profile(self) -> StreamProfile:
        ...

    def next_frame(self) -> VideoFrame:
        """Return the next frame or raise StopIteration when the source ends."""
        ...

    def rewind(self) -> None:
        """Return the source to its first frame for loop policy."""
        ...


class ExternalVideoAdapter(Protocol):
    """The only cloud-provider boundary used by continuous camera routing.

    A Volcengine client-external-source implementation calls
    ``setVideoSourceType(external)`` exactly once in ``open_external_video``
    and then calls ``pushExternalVideoFrame`` from ``push_video_frame``.
    """

    def open_external_video(self, stream_id: str, profile: StreamProfile) -> str:
        """Open one long-lived external virtual-camera session."""
        ...

    def push_video_frame(
        self,
        session_id: str,
        frame: VideoFrame,
        *,
        sequence_number: int,
        timestamp_us: int,
    ) -> None:
        """Push exactly one frame without changing the source mode."""
        ...

    def close_external_video(self, session_id: str) -> None:
        """Close the virtual-camera session once all sources are finished."""
        ...


@dataclass
class StreamReceipt:
    """Machine-readable continuity evidence for one camera stream."""

    stream_id: str
    provider_session_id: str
    profile: StreamProfile
    max_interframe_gap_ms: float
    emitted_frame_count: int = 0
    max_observed_interframe_gap_ms: float = 0.0
    source_switch_count: int = 0
    source_session_restarts: int = 0
    discontinuity_count: int = 0
    active_source_id: Optional[str] = None
    source_history: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["profile"] = asdict(self.profile)
        return data


@dataclass
class _CameraStream:
    stream_id: str
    session_id: str
    profile: StreamProfile
    max_interframe_gap_ms: float
    end_of_source_policy: str
    active_source_id: str
    sources: Dict[str, VideoFrameSource]
    pending_source_id: Optional[str] = None
    sequence_number: int = 0
    last_emit_ns: Optional[int] = None
    last_frame: Optional[VideoFrame] = None
    closed: bool = False
    receipt: StreamReceipt | None = None


class IterableVideoSource:
    """Small deterministic frame source for tests and custom decoders."""

    def __init__(self, profile: StreamProfile, frames: Iterable[bytes]):
        self._profile = profile
        self._frames = list(frames)
        if not self._frames:
            raise ValueError("a video source must contain at least one frame")
        self._position = 0

    @property
    def profile(self) -> StreamProfile:
        return self._profile

    def next_frame(self) -> VideoFrame:
        if self._position >= len(self._frames):
            raise StopIteration
        payload = self._frames[self._position]
        self._position += 1
        return VideoFrame(payload=payload, profile=self._profile)

    def rewind(self) -> None:
        self._position = 0


class VideoSourceRouter:
    """Switches pre-staged sources without reopening a camera session.

    ``pump`` is intentionally explicit.  A production scheduler calls it at
    ``profile.fps``; tests can supply a deterministic monotonic timestamp.
    This makes both continuity and source-switch receipts independently
    verifiable without a cloud device.
    """

    def __init__(self, adapter: ExternalVideoAdapter, monotonic_ns=time.monotonic_ns):
        self._adapter = adapter
        self._monotonic_ns = monotonic_ns
        self._streams: Dict[str, _CameraStream] = {}
        self._lock = threading.RLock()

    def open(
        self,
        stream_id: str,
        initial_source_id: str,
        initial_source: VideoFrameSource,
        *,
        profile: StreamProfile,
        max_interframe_gap_ms: float,
        end_of_source_policy: str = "loop",
    ) -> StreamReceipt:
        """Open a single external source session and install its first source."""
        if max_interframe_gap_ms <= 0:
            raise ValueError("max_interframe_gap_ms must be positive")
        if end_of_source_policy not in {"loop", "hold_last_frame"}:
            raise ValueError("end_of_source_policy must be loop or hold_last_frame")
        self._ensure_profile(profile, initial_source.profile)
        with self._lock:
            if stream_id in self._streams and not self._streams[stream_id].closed:
                raise MediaIOError(f"camera stream already open: {stream_id}")
            session_id = self._adapter.open_external_video(stream_id, profile)
            receipt = StreamReceipt(
                stream_id=stream_id,
                provider_session_id=session_id,
                profile=profile,
                max_interframe_gap_ms=max_interframe_gap_ms,
                active_source_id=initial_source_id,
                source_history=[initial_source_id],
            )
            self._streams[stream_id] = _CameraStream(
                stream_id=stream_id,
                session_id=session_id,
                profile=profile,
                max_interframe_gap_ms=max_interframe_gap_ms,
                end_of_source_policy=end_of_source_policy,
                active_source_id=initial_source_id,
                sources={initial_source_id: initial_source},
                receipt=receipt,
            )
            return receipt

    def stage(self, stream_id: str, source_id: str, source: VideoFrameSource) -> None:
        """Pre-stage a normalized source; this never touches the camera session."""
        with self._lock:
            stream = self._get_open_stream(stream_id)
            self._ensure_profile(stream.profile, source.profile)
            stream.sources[source_id] = source

    def switch(self, stream_id: str, source_id: str) -> None:
        """Schedule a content switch at the boundary immediately before next frame."""
        with self._lock:
            stream = self._get_open_stream(stream_id)
            if source_id not in stream.sources:
                raise SourceNotStaged(f"source is not staged: {source_id}")
            stream.pending_source_id = source_id

    def pump(self, stream_id: str, *, now_ns: Optional[int] = None) -> StreamReceipt:
        """Emit one frame, applying any pending content switch atomically first."""
        with self._lock:
            stream = self._get_open_stream(stream_id)
            now_ns = self._monotonic_ns() if now_ns is None else now_ns
            if stream.pending_source_id is not None:
                stream.active_source_id = stream.pending_source_id
                stream.pending_source_id = None
                assert stream.receipt is not None
                stream.receipt.active_source_id = stream.active_source_id
                stream.receipt.source_switch_count += 1
                stream.receipt.source_history.append(stream.active_source_id)

            source = stream.sources[stream.active_source_id]
            frame = self._next_frame(stream, source)
            self._ensure_profile(stream.profile, frame.profile)

            if stream.last_emit_ns is not None:
                gap_ms = (now_ns - stream.last_emit_ns) / 1_000_000
                assert stream.receipt is not None
                stream.receipt.max_observed_interframe_gap_ms = max(
                    stream.receipt.max_observed_interframe_gap_ms, gap_ms
                )
                if gap_ms > stream.max_interframe_gap_ms:
                    stream.receipt.discontinuity_count += 1

            timestamp_us = now_ns // 1_000
            self._adapter.push_video_frame(
                stream.session_id,
                frame,
                sequence_number=stream.sequence_number,
                timestamp_us=timestamp_us,
            )
            stream.sequence_number += 1
            stream.last_emit_ns = now_ns
            stream.last_frame = frame
            assert stream.receipt is not None
            stream.receipt.emitted_frame_count += 1
            return stream.receipt

    def close(self, stream_id: str) -> StreamReceipt:
        """Close the provider session exactly once and return its final receipt."""
        with self._lock:
            stream = self._get_stream(stream_id)
            if not stream.closed:
                self._adapter.close_external_video(stream.session_id)
                stream.closed = True
            assert stream.receipt is not None
            return stream.receipt

    def receipt(self, stream_id: str) -> StreamReceipt:
        with self._lock:
            stream = self._get_stream(stream_id)
            assert stream.receipt is not None
            return stream.receipt

    @staticmethod
    def _ensure_profile(expected: StreamProfile, actual: StreamProfile) -> None:
        if expected != actual:
            raise ProfileMismatch(
                f"source profile {actual} does not match stream profile {expected}"
            )

    def _get_stream(self, stream_id: str) -> _CameraStream:
        try:
            return self._streams[stream_id]
        except KeyError as exc:
            raise StreamNotFound(f"camera stream not found: {stream_id}") from exc

    def _get_open_stream(self, stream_id: str) -> _CameraStream:
        stream = self._get_stream(stream_id)
        if stream.closed:
            raise MediaIOError(f"camera stream is closed: {stream_id}")
        return stream

    @staticmethod
    def _next_frame(stream: _CameraStream, source: VideoFrameSource) -> VideoFrame:
        try:
            return source.next_frame()
        except StopIteration:
            if stream.end_of_source_policy == "loop":
                source.rewind()
                return source.next_frame()
            if stream.last_frame is not None:
                return stream.last_frame
            raise MediaIOError(f"source ended before first frame: {stream.active_source_id}")


class RecordingExternalVideoAdapter:
    """In-memory adapter used by tests and local service integration checks."""

    def __init__(self) -> None:
        self.opens: List[tuple[str, StreamProfile]] = []
        self.frames: List[tuple[str, int, int, bytes]] = []
        self.closes: List[str] = []

    def open_external_video(self, stream_id: str, profile: StreamProfile) -> str:
        self.opens.append((stream_id, profile))
        return f"session-{len(self.opens)}"

    def push_video_frame(
        self,
        session_id: str,
        frame: VideoFrame,
        *,
        sequence_number: int,
        timestamp_us: int,
    ) -> None:
        self.frames.append((session_id, sequence_number, timestamp_us, frame.payload))

    def close_external_video(self, session_id: str) -> None:
        self.closes.append(session_id)
