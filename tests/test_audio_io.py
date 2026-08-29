"""PCM microphone routing is independent from video injection."""

from __future__ import annotations

from src.audio_io import AudioProfile, AudioSourceRouter, IterableAudioSource, RecordingExternalAudioAdapter


def test_pcm_microphone_router_keeps_one_session_and_loops_complete_frames():
    profile = AudioProfile(sample_rate_hz=8000, channels=1, sample_format="s16le", frame_duration_ms=20)
    adapter = RecordingExternalAudioAdapter()
    router = AudioSourceRouter(adapter)
    router.open("microphone", IterableAudioSource(profile, [b"\x01" * profile.frame_bytes]))

    router.pump("microphone", 0)
    router.pump("microphone", 20_000)
    router.close("microphone")
    router.close("microphone")

    assert len(adapter.opens) == 1
    assert [frame[1] for frame in adapter.frames] == [0, 1]
    assert adapter.closes == ["audio-session-1"]
