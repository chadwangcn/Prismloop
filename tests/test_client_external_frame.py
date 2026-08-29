"""The client-SDK bridge preserves raw frame metadata and session identity."""

from __future__ import annotations

from src.audio_io import AudioFrame, AudioProfile
from src.client_external_frame import (
    ClientExternalAudioAdapter,
    ClientExternalVideoAdapter,
    RecordingClientExternalFrameGateway,
)
from src.media_io import StreamProfile, VideoFrame


def test_client_external_adapters_forward_video_and_pcm_without_provider_data():
    gateway = RecordingClientExternalFrameGateway()
    video = ClientExternalVideoAdapter(gateway)
    audio = ClientExternalAudioAdapter(gateway)
    video_profile = StreamProfile(width=64, height=64, fps=30)
    audio_profile = AudioProfile(sample_rate_hz=8_000, channels=1, frame_duration_ms=20)

    video_session = video.open_external_video("rear-camera", video_profile)
    video.push_video_frame(video_session, VideoFrame(b"frame", video_profile), sequence_number=3, timestamp_us=100)
    video.close_external_video(video_session)

    audio_session = audio.open_external_audio("microphone", audio_profile)
    payload = b"\x00" * audio_profile.frame_bytes
    audio.push_audio_frame(audio_session, AudioFrame(payload, audio_profile), sequence_number=4, timestamp_us=120)
    audio.close_external_audio(audio_session)

    assert gateway.video_frames == [(video_session, 3, 100, b"frame")]
    assert gateway.audio_frames == [(audio_session, 4, 120, payload)]
    assert gateway.video_closes == [video_session]
    assert gateway.audio_closes == [audio_session]
