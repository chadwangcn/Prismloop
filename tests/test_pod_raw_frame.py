"""The Pod raw-frame adapter forwards metadata without inventing a transport."""

from __future__ import annotations

from src.media_io import StreamProfile, VideoFrame
from src.pod_raw_frame import PodRawFrameAdapter, RecordingPodRawFrameTransport


def test_pod_raw_frame_adapter_forwards_stable_session_and_frame_metadata():
    transport = RecordingPodRawFrameTransport()
    adapter = PodRawFrameAdapter(transport)
    profile = StreamProfile(width=64, height=64, fps=30)

    session_id = adapter.open_external_video("rear-camera", profile)
    adapter.push_video_frame(session_id, VideoFrame(b"frame", profile), sequence_number=7, timestamp_us=1234)
    adapter.close_external_video(session_id)

    assert transport.opens == [("rear-camera", profile)]
    assert transport.frames == [(session_id, 7, 1234, b"frame")]
    assert transport.closes == [session_id]
