"""Artifact integrity and fixture normalization tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.media_artifacts import ArtifactIntegrityError, FilesystemArtifactStore
from src.media_io import StreamProfile
from src.media_sources import ArtifactVideoSourceResolver


def test_filesystem_artifact_store_rejects_digest_tampering(tmp_path):
    store = FilesystemArtifactStore(str(tmp_path), "filesystem/test-media")
    artifact = store.put_bytes("fixtures/item.bin", b"trusted", "application/octet-stream")
    tampered = dict(artifact)
    tampered["sha256"] = "0" * 64

    with pytest.raises(ArtifactIntegrityError, match="sha256 mismatch"):
        store.get_bytes(tampered)


def test_video_resolver_normalizes_verified_mp4_to_stream_profile(tmp_path):
    store = FilesystemArtifactStore(str(tmp_path / "artifacts"), "filesystem/test-media")
    fixture = Path(__file__).parents[1] / "assets/videos/test-pattern-640x480.mp4"
    artifact = store.put_bytes("fixtures/test-pattern.mp4", fixture.read_bytes(), "video/mp4")
    input_spec = {"input_id": "fixture", "kind": "camera.video", "artifact": artifact}
    profile = StreamProfile(width=64, height=64, fps=10, pixel_format="yuv420p")

    source = ArtifactVideoSourceResolver(store, staging_dir=str(tmp_path)).resolve(input_spec, profile)
    frame = source.next_frame()

    assert frame.profile == profile
    assert len(frame.payload) == 64 * 64 * 3 // 2
