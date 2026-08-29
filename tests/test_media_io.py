"""Tests for the provider-neutral continuous virtual-camera router."""

from __future__ import annotations

import pytest

from src.media_io import (
    IterableVideoSource,
    ProfileMismatch,
    RecordingExternalVideoAdapter,
    SourceNotStaged,
    StreamProfile,
    VideoSourceRouter,
)


@pytest.fixture
def profile() -> StreamProfile:
    return StreamProfile(width=720, height=1280, fps=30, pixel_format="yuv420p")


def test_switches_content_without_reopening_virtual_camera(profile: StreamProfile):
    adapter = RecordingExternalVideoAdapter()
    router = VideoSourceRouter(adapter)
    source_a = IterableVideoSource(profile, [b"a0", b"a1"])
    source_b = IterableVideoSource(profile, [b"b0", b"b1"])

    router.open("rear-camera", "source-a", source_a, profile=profile, max_interframe_gap_ms=40)
    router.stage("rear-camera", "source-b", source_b)
    router.pump("rear-camera", now_ns=0)
    router.switch("rear-camera", "source-b")
    receipt = router.pump("rear-camera", now_ns=33_000_000)

    assert len(adapter.opens) == 1
    assert adapter.frames == [
        ("session-1", 0, 0, b"a0"),
        ("session-1", 1, 33_000, b"b0"),
    ]
    assert receipt.source_session_restarts == 0
    assert receipt.discontinuity_count == 0
    assert receipt.source_switch_count == 1
    assert receipt.source_history == ["source-a", "source-b"]


def test_gap_is_recorded_as_continuity_evidence(profile: StreamProfile):
    adapter = RecordingExternalVideoAdapter()
    router = VideoSourceRouter(adapter)
    source = IterableVideoSource(profile, [b"frame"])
    router.open("rear-camera", "source", source, profile=profile, max_interframe_gap_ms=40)

    router.pump("rear-camera", now_ns=0)
    receipt = router.pump("rear-camera", now_ns=100_000_000)

    assert receipt.max_observed_interframe_gap_ms == pytest.approx(100.0)
    assert receipt.discontinuity_count == 1


def test_switch_requires_pre_staged_source(profile: StreamProfile):
    router = VideoSourceRouter(RecordingExternalVideoAdapter())
    source = IterableVideoSource(profile, [b"frame"])
    router.open("rear-camera", "source", source, profile=profile, max_interframe_gap_ms=40)

    with pytest.raises(SourceNotStaged):
        router.switch("rear-camera", "missing")


def test_staged_source_must_match_immutable_stream_profile(profile: StreamProfile):
    router = VideoSourceRouter(RecordingExternalVideoAdapter())
    source = IterableVideoSource(profile, [b"frame"])
    router.open("rear-camera", "source", source, profile=profile, max_interframe_gap_ms=40)
    mismatched = IterableVideoSource(
        StreamProfile(width=640, height=480, fps=30, pixel_format="yuv420p"), [b"frame"]
    )

    with pytest.raises(ProfileMismatch):
        router.stage("rear-camera", "mismatch", mismatched)


def test_loop_policy_keeps_camera_stream_alive_at_end_of_video(profile: StreamProfile):
    adapter = RecordingExternalVideoAdapter()
    router = VideoSourceRouter(adapter)
    source = IterableVideoSource(profile, [b"only"])
    router.open("rear-camera", "source", source, profile=profile, max_interframe_gap_ms=40)

    router.pump("rear-camera", now_ns=0)
    router.pump("rear-camera", now_ns=33_000_000)

    assert [item[-1] for item in adapter.frames] == [b"only", b"only"]
    assert len(adapter.opens) == 1


def test_hold_last_frame_policy_keeps_emitting_final_frame(profile: StreamProfile):
    adapter = RecordingExternalVideoAdapter()
    router = VideoSourceRouter(adapter)
    source = IterableVideoSource(profile, [b"final"])
    router.open(
        "rear-camera",
        "source",
        source,
        profile=profile,
        max_interframe_gap_ms=40,
        end_of_source_policy="hold_last_frame",
    )

    router.pump("rear-camera", now_ns=0)
    router.pump("rear-camera", now_ns=33_000_000)

    assert [item[-1] for item in adapter.frames] == [b"final", b"final"]


def test_close_happens_once(profile: StreamProfile):
    adapter = RecordingExternalVideoAdapter()
    router = VideoSourceRouter(adapter)
    source = IterableVideoSource(profile, [b"frame"])
    router.open("rear-camera", "source", source, profile=profile, max_interframe_gap_ms=40)

    router.close("rear-camera")
    router.close("rear-camera")

    assert adapter.closes == ["session-1"]
